"""run_history.py — per-run telemetry archive for the autopilot driver (telemetry-history feature).

The live surfaces (``logs/events.jsonl`` + ``autopilot/status.json``) are *current-run* views: the
event log is rotated clean at every ``autopilot.run()`` start and status.json is merge-overwritten in
place. That is exactly what a "what is the driver doing RIGHT NOW" dashboard wants, but it means the
moment a run ends its evidence is about to be trampled by the next run — which is how a real finding
(the lost 027 lane result) vanished. This module is the DURABLE side: on ANY exit from ``run()`` the
driver archives the just-finished run into ``<collab>/autopilot/history/<run_uid>/`` and writes a
``run.json`` roll-up so the run is inspectable and comparable long after the live feed moved on.

Design posture (mirrors autopilot's [C15] best-effort observability): archiving is telemetry, not
correctness — every public entry point here is best-effort and must NEVER raise into the driver. A run
that produced real work must not be reported as failed because a history copy hit a locked file.

Layout of a single archived run::

    <collab>/autopilot/history/<run_uid>/
        run.json          # the schema-B roll-up (built by build_summary)
        events.jsonl      # a copy of that run's event feed
        status.json       # the driver's final status snapshot
        verification/     # copied *.ledger.json evidence ledgers

``run_uid`` is minted once by the driver as ``<started_ts_compact>-<pid>`` (see autopilot.run); it is
time-sortable, so ``prune`` keeps the newest N by plain name sort.
"""

from __future__ import annotations

import calendar
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

_LIB = str(Path(__file__).resolve().parent)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import collab_common as cc  # noqa: E402
import autopilot as _ap  # noqa: E402  (no cycle: autopilot imports run_history only lazily, inside run())


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #


def _history_root(collab) -> Path:
    return Path(collab) / "autopilot" / "history"


def _verification_dir(collab) -> Path:
    return Path(collab) / "autopilot" / "verification"


# --------------------------------------------------------------------------- #
# small parse helpers (all tolerant — telemetry aggregation never raises)
# --------------------------------------------------------------------------- #


def _read_events(events_path) -> list[dict]:
    """Parse a JSONL event feed into a list of dicts, SKIPPING any torn/partial line (a crash can leave a
    half-written final line). Never raises — a missing/unreadable file yields ``[]``."""
    out: list[dict] = []
    try:
        text = Path(events_path).read_text("utf-8", errors="replace")
    except (OSError, ValueError):
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue  # torn/partial JSON — skip, never raise
        if isinstance(ev, dict):
            out.append(ev)
    return out


def _read_status(collab) -> dict:
    """The driver's final status.json as a dict (``{}`` if absent/corrupt)."""
    try:
        doc = json.loads(_ap._status_path(collab).read_text("utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def _norm_seats(seats) -> dict:
    """Normalize a seats snapshot to ``{seat: model|None}``. Accepts either a raw ``load_seats`` mapping
    (``{seat: {cfg...}}``) or an already-reduced ``{seat: model}`` map, so callers can pass whichever they
    have on hand."""
    out: dict = {}
    if isinstance(seats, dict):
        for name, val in seats.items():
            out[name] = val.get("model") if isinstance(val, dict) else val
    return out


def _int(s) -> int:
    try:
        return int(str(s).strip())
    except (TypeError, ValueError):
        return 0


def _parse_ts(s) -> int | None:
    """UTC epoch seconds for a ``%Y-%m-%dT%H:%M:%SZ`` timestamp, or None."""
    if not isinstance(s, str) or not s:
        return None
    try:
        return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, OverflowError):
        return None


# --------------------------------------------------------------------------- #
# schema-B roll-up
# --------------------------------------------------------------------------- #


def build_summary(collab, run_uid, *, seats=None, started_ts=None, pid=None, max_rounds=None,
                  watch=None, git_sha=None, events_path=None) -> dict:
    """Aggregate a run's ``events.jsonl`` into the schema-B ``run.json`` roll-up.

    Robust to torn/partial JSON lines (skipped, never fatal). Identity/context fields (started_ts, pid,
    max_rounds, watch, git_sha, seats) are taken from the explicit kwargs when given, else recovered from
    the run's ``status.json`` (which the driver stamps with ``run_uid``/``run_seats``/``git_sha`` at start).
    Terminal fields (``phase_final``, ``last_error``, ``ended_ts``) are merged from the final status.

    Definitions (documented, consistent):
      * A round DONE event is ``stage="autopilot.round"`` carrying ``metrics.latency_ms`` (the ``turn`` and
        ``fail`` variants; the ``start`` variant carries only ``round_no`` and is NOT counted).
      * ``calls`` == ``rounds_total`` == the count of round DONE events — i.e. how many times a seat's agent
        was actually invoked this run. ``seat_calls[seat]``/``seat_latency_ms[seat]`` partition that by role.
    """
    events = _read_events(events_path or _ap._log_default(collab))
    status = _read_status(collab)

    started_ts = started_ts if started_ts is not None else status.get("started_ts")
    pid = pid if pid is not None else status.get("pid")
    max_rounds = max_rounds if max_rounds is not None else status.get("max_rounds")
    watch = watch if watch is not None else status.get("watch")
    git_sha = git_sha if git_sha is not None else status.get("git_sha")
    seats_map = _norm_seats(seats if seats is not None else status.get("run_seats"))

    seat_calls: dict = {}
    seat_latency: dict = {}
    by_lane: dict = {}
    lanes_confirmed = 0
    lanes_refuted = 0
    rounds_total = 0
    handoffs: list = []
    signoff = {"result": "none", "unmet": []}
    # ADR-0003 candidate-lifecycle tallies (truthful terminals): per-outcome assessment counts, the number
    # of durable escalations, and the reason of the LAST escalation this run wrote (the terminal cause).
    outcomes: dict = {}
    escalations = 0
    terminal_reason = None

    for ev in events:
        stage = ev.get("stage")
        role = ev.get("role")
        decision = ev.get("decision")
        if not isinstance(decision, dict):
            decision = {}
        metrics = ev.get("metrics")
        if not isinstance(metrics, dict):
            metrics = {}

        art = ev.get("artifact")
        if isinstance(art, str) and art.startswith("handoff:"):
            hid = art[len("handoff:"):]
            if hid and hid not in handoffs:
                handoffs.append(hid)

        if stage == "autopilot.round" and "latency_ms" in metrics:
            rounds_total += 1
            if role:
                seat_calls[role] = seat_calls.get(role, 0) + 1
                lat = metrics.get("latency_ms")
                if isinstance(lat, (int, float)) and not isinstance(lat, bool):
                    seat_latency[role] = round(seat_latency.get(role, 0.0) + lat, 1)
        elif stage == "autopilot.lane" and decision.get("action") == "lane":
            codes = decision.get("reason_codes") or []
            lane = None
            conf = ref = 0
            for code in codes:
                if not isinstance(code, str):
                    continue
                if code.startswith("lane:"):
                    lane = code[len("lane:"):]
                elif code.startswith("confirmed:"):
                    conf = _int(code[len("confirmed:"):])
                elif code.startswith("refuted:"):
                    ref = _int(code[len("refuted:"):])
            if lane is not None:
                d = by_lane.setdefault(lane, {"confirmed": 0, "refuted": 0})
                d["confirmed"] += conf
                d["refuted"] += ref
            lanes_confirmed += conf
            lanes_refuted += ref
        elif stage == "autopilot.assessment":
            # ADR-0003: one candidate assessment. Tally outcomes for the run roll-up (approved/
            # repair_required/infrastructure_blocked/verification_incomplete).
            for code in (decision.get("reason_codes") or []):
                if isinstance(code, str) and code.startswith("outcome:"):
                    oc = code[len("outcome:"):]
                    outcomes[oc] = outcomes.get(oc, 0) + 1
        elif stage == "autopilot.escalation":
            # A durable pause was written — the truthful terminal cause of this handoff's drive.
            escalations += 1
            for code in (decision.get("reason_codes") or []):
                if isinstance(code, str) and code.startswith("reason:"):
                    terminal_reason = code[len("reason:"):]
            unmet = [c[len("unmet:"):] for c in (decision.get("reason_codes") or [])
                     if isinstance(c, str) and c.startswith("unmet:")]
            signoff = {"result": "escalated", "reason": terminal_reason, "unmet": unmet}
        elif stage == "autopilot.signoff_blocked":  # legacy pre-candidate event — kept for old archives
            codes = decision.get("reason_codes") or []
            unmet = [c[len("unmet:"):] if isinstance(c, str) and c.startswith("unmet:") else c
                     for c in codes]
            signoff = {"result": "blocked", "unmet": unmet}
        elif stage in ("autopilot.autonomous_done", "handoff.autonomous_done"):
            signoff = {"result": "signed", "unmet": []}
            terminal_reason = "closed"

    start_epoch = _parse_ts(started_ts)
    ended_ts = status.get("updated_ts") or status.get("ended_ts")
    end_epoch = _parse_ts(ended_ts)
    duration_ms = int((end_epoch - start_epoch) * 1000) if (start_epoch is not None and
                                                            end_epoch is not None) else None

    return {
        "run_uid": run_uid,
        "collab": str(collab),
        "started_ts": started_ts,
        "ended_ts": ended_ts,
        "pid": pid,
        "phase_final": status.get("phase"),
        "last_error": status.get("last_error"),
        "max_rounds": max_rounds,
        "rounds_total": rounds_total,
        "calls": rounds_total,
        "duration_ms": duration_ms,
        "git_sha": git_sha,
        "watch": watch,
        "seats": seats_map,
        "seat_calls": seat_calls,
        "seat_latency_ms": seat_latency,
        "lanes": {"confirmed": lanes_confirmed, "refuted": lanes_refuted, "by_lane": by_lane},
        "signoff": signoff,
        "outcomes": outcomes,
        "escalations": escalations,
        "terminal_reason": terminal_reason if terminal_reason is not None else status.get("pause_reason"),
        "handoffs_touched": handoffs,
    }


# --------------------------------------------------------------------------- #
# archive / prune
# --------------------------------------------------------------------------- #


def archive_run(collab, run_uid) -> Path | None:
    """Snapshot the just-finished run into ``<collab>/autopilot/history/<run_uid>/``.

    Copies the live ``events.jsonl`` + ``status.json`` and every ``verification/*.ledger.json``, then writes
    ``run.json`` built from the ARCHIVED events copy (so the roll-up matches exactly what was archived).
    Best-effort/never raises ([C15]-style): a failure returns ``None`` and is logged, never propagated into
    the driver's exit path."""
    # The dir name IS the run_uid so a caller (e.g. the dashboard) can locate a run's archive straight from
    # the run_uid stamped in status.json. run_uid is minted from time+pid so it is already path-safe and
    # time-sortable; strip anything unexpected WITHOUT changing case (don't slugify — that would lowercase
    # the canonical id and desync the dir name from run.json's run_uid), and fall back if it reduces to junk.
    safe_uid = re.sub(r"[^A-Za-z0-9-]", "", str(run_uid))
    if not safe_uid or set(safe_uid) <= {"-"}:
        safe_uid = "run"
    root = _history_root(collab) / safe_uid
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"[run_history] could not create history dir: {e}", file=sys.stderr)
        return None

    events_dst = root / "events.jsonl"
    # events.jsonl
    try:
        src = Path(_ap._log_default(collab))
        if src.exists():
            cc.safe_write(events_dst, src.read_text("utf-8", errors="replace"))
    except (OSError, cc.CollabError) as e:
        print(f"[run_history] could not copy events.jsonl: {e}", file=sys.stderr)
    # status.json
    try:
        sp = _ap._status_path(collab)
        if sp.exists():
            cc.safe_write(root / "status.json", sp.read_text("utf-8", errors="replace"))
    except (OSError, cc.CollabError) as e:
        print(f"[run_history] could not copy status.json: {e}", file=sys.stderr)
    # verification/*.ledger.json
    try:
        vdir = _verification_dir(collab)
        if vdir.is_dir():
            (root / "verification").mkdir(parents=True, exist_ok=True)
            for f in sorted(vdir.glob("*.ledger.json")):
                try:
                    cc.safe_write(root / "verification" / f.name, f.read_text("utf-8", errors="replace"))
                except (OSError, cc.CollabError):
                    continue  # one bad ledger must not abort the archive
    except OSError as e:
        print(f"[run_history] could not copy ledgers: {e}", file=sys.stderr)
    # run.json roll-up
    try:
        summary = build_summary(collab, run_uid,
                                events_path=str(events_dst) if events_dst.exists() else None)
        cc.safe_write(root / "run.json", json.dumps(summary, indent=2, sort_keys=False) + "\n")
    except Exception as e:  # broad: run.json is telemetry, never worth raising into the driver
        print(f"[run_history] could not write run.json: {e}", file=sys.stderr)
    return root


def prune(collab, keep: int = 25) -> None:
    """Keep the newest ``keep`` archived runs (name is time-sortable) and remove the rest. Best-effort."""
    try:
        root = _history_root(collab)
        if not root.is_dir():
            return
        dirs = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda p: p.name)
        stale = dirs if keep <= 0 else dirs[:-keep]
        for d in stale:
            shutil.rmtree(d, ignore_errors=True)
    except OSError as e:
        print(f"[run_history] prune failed: {e}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# git
# --------------------------------------------------------------------------- #


def git_sha(collab) -> str | None:
    """HEAD sha of the collab repo (``git -C <collab> rev-parse HEAD``), or None on any failure."""
    try:
        p = subprocess.run(["git", "-C", str(collab), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    sha = (p.stdout or "").strip()
    return sha or None
