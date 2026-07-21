"""dashboard_core — the shared read/act layer behind the autopilot dashboards (TUI + web).

One module, two readers: :mod:`dashboard_tui` and :mod:`dashboard_web` both poll :func:`snapshot`
and route their controls through :func:`set_paused` / :func:`advance_handoff`. Nothing here renders —
it only reads the durable surfaces the driver already produces and exposes the few *human* actions the
dashboard is allowed to take.

Data sources (all reused, nothing new invented):
  * ``<collab>/autopilot/status.json`` — the driver's live heartbeat (:func:`autopilot._write_status`).
  * ``<collab>/logs/events.jsonl``     — the append-only audit stream (:mod:`trace` / :mod:`handoff_events`).
  * the handoff state machine           — authoritative board via :func:`handoff_core.list_handoffs`.
  * ``seats.json``                       — seat -> model, via :func:`autopilot.load_seats`.

Safety ([C36], autonomous rev — §18): a handoff reaches ``done/`` two ways, both audited: the *driver*
may advance it autonomously ONLY when the §18.3 evidence contract is satisfied (independent approver +
clean verification ledger + source==tested — see :mod:`done_contract`), and :func:`advance_handoff` here
is the HUMAN OVERRIDE (an explicit keypress / button). Separation of authority — no seat approves its own
work — is the boundary, not a mandatory human gate. Pause/resume/stop are reversible flags in
``control.json`` — they only ever idle the loop, never touch a handoff.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path

_LIB = str(Path(__file__).resolve().parent)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
# Reuse autopilot's path/telemetry helpers (the single source of truth for the layout).
import adapter_profiles as adapter_profiles  # noqa: E402
import autopilot as ap  # noqa: E402
import collab_common as cc  # noqa: E402
import contracts  # noqa: E402
import escalation as esc  # noqa: E402
import handoff_core as hc  # noqa: E402
import handoff_events as he  # noqa: E402
import operator_requests as opreq  # noqa: E402
import registry  # noqa: E402
import transitions as _transitions  # noqa: E402
import verification as _verification  # noqa: E402
import verification_plan as verification_plan  # noqa: E402

_trace = ap._trace  # the by-path-loaded local trace module (stdlib-shadowing safe)


# --------------------------------------------------------------------------- #
# live status + human control files
# --------------------------------------------------------------------------- #


def read_status(collab) -> dict | None:
    """The driver's live status, or ``None`` if it has never run (no status.json yet)."""
    try:
        doc = json.loads(ap._status_path(collab).read_text("utf-8"))
        return doc if isinstance(doc, dict) else None
    except OSError, ValueError:
        return None


def read_control(collab) -> dict:
    """Full control file (paused/stop + who/when). Missing/corrupt -> the safe running default."""
    default = {
        "schema_version": "0.1",
        "paused": False,
        "stop": False,
        "requested_ts": None,
        "requested_by": None,
        # Carried through so a mid-run set_max_rounds survives BOTH this read and the next
        # _write_control: the key was absent from this default, so the dict-comprehension below
        # dropped it on read, and _write_control (which rewrites the whole dict) then erased it from
        # disk on any later set_paused/set_stop. None = no override; the driver keeps its launch cap
        # (mirrors autopilot._read_control) (INT-027).
        "max_rounds": None,
    }
    try:
        doc = json.loads(ap._control_path(collab).read_text("utf-8"))
        if not isinstance(doc, dict):
            return default
        default.update({k: doc.get(k, default[k]) for k in default})
        default["paused"] = bool(doc.get("paused", False))
        default["stop"] = bool(doc.get("stop", False))
        return default
    except OSError, ValueError:
        return default


def _write_control(collab, **fields) -> dict:
    """Merge ``fields`` into control.json and atomically re-publish it. Returns the new control dict."""
    ctrl = read_control(collab)
    ctrl.update(fields)
    ctrl["requested_ts"] = ap._now_utc()
    p = ap._control_path(collab)
    p.parent.mkdir(parents=True, exist_ok=True)  # atomic_write does not create parents
    cc.safe_write(p, json.dumps(ctrl, separators=(",", ":")) + "\n")
    return ctrl


def set_paused(collab, value: bool, *, by: str = "dashboard") -> dict:
    """Pause (True) or resume (False) the driver. Reversible; only ever idles the loop ([C36])."""
    return _write_control(collab, paused=bool(value), requested_by=by)


def set_stop(collab, value: bool = True, *, by: str = "dashboard") -> dict:
    """Ask the driver to exit gracefully at the next pass. Reversible (no handoff is touched)."""
    return _write_control(collab, stop=bool(value), requested_by=by)


def set_max_rounds(collab, n, *, by: str = "dashboard") -> dict:
    """Cap the driver's per-run round budget via control.json (mirrors :func:`set_stop`).

    ``n`` must be an int in ``1..50`` — an out-of-range or non-int value raises :class:`ValueError`
    rather than writing a budget the driver can't honour. Takes effect when the driver next reads
    control (reversible; no handoff is touched)."""
    if isinstance(n, bool) or not isinstance(n, int):
        raise ValueError(f"max_rounds must be an int, got {type(n).__name__}")
    if not (1 <= n <= 50):
        raise ValueError(f"max_rounds must be in 1..50, got {n}")
    return _write_control(collab, max_rounds=int(n), requested_by=by)


# --------------------------------------------------------------------------- #
# event stream + board
# --------------------------------------------------------------------------- #


_events_cache: dict[str, tuple] = {}  # resolved_path -> (mtime_ns, size, events)
_events_lock = threading.Lock()


def read_events(collab) -> list[dict]:
    """ALL valid parsed events from ``<collab>/logs/events.jsonl``, oldest-first.

    Malformed lines are skipped defensively (a half-written final line during a concurrent append
    must never crash the dashboard). Returns ``[]`` if the log does not exist yet.

    Memoized on ``(mtime_ns, size)``: the driver appends ~once per round (seconds apart), so most 1 s
    dashboard polls hit the cache and do only an ``os.stat`` — this is what keeps the full-log read
    (needed for accurate stats) cheap without an on-disk format change. A stat error falls back to a
    full read (correctness over speed).
    """
    p = Path(ap._log_default(collab))
    key = str(p.resolve())
    try:
        st = p.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        sig = None  # missing/unstattable -> skip the cache, attempt a direct read below
    if sig is not None:
        with _events_lock:
            cached = _events_cache.get(key)
        if cached is not None and cached[:2] == sig:
            return cached[2]
    try:
        lines = p.read_text("utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue  # a torn/partial line (e.g. a concurrent append) — skip it, never crash
        if isinstance(ev, dict):
            out.append(ev)
    if sig is not None:
        with _events_lock:
            _events_cache[key] = (sig[0], sig[1], out)
    return out


def tail_events(collab, limit: int = 200) -> list[dict]:
    """The last ``limit`` VALID events, oldest-first (a torn line never shrinks the window)."""
    return read_events(collab)[-max(0, limit) :]


def _num(v):
    """``v`` if it is a real number (not bool), else None — telemetry metrics are best-effort."""
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def run_stats(events: list[dict], *, series_n: int = 40) -> dict:
    """Aggregate ``autopilot.round`` telemetry into per-seat + overall stats and a latency series.

    A completed round is the ``turn`` DONE event the driver emits (:func:`autopilot._dispatch_seat`); a
    ``fail`` event is a backend failure. Pure function over an already-read event list (no I/O). Fails are
    counted but excluded from the latency average and do not move ``last_ms`` (which stays the last
    successful turn). Defensive: non-dict events and non-numeric metrics are skipped.
    """
    seats: dict[str, dict] = {}
    overall = {"rounds": 0, "fails": 0, "_sum": 0.0, "_n": 0}
    series: list[dict] = []

    def seat(name):
        return seats.setdefault(
            name, {"rounds": 0, "fails": 0, "_sum": 0.0, "_n": 0, "last_ms": None, "total_resp_bytes": 0}
        )

    for ev in events:
        if not isinstance(ev, dict) or ev.get("stage") != "autopilot.round":
            continue
        name = ev.get("role") or "?"
        action = (ev.get("decision") or {}).get("action")
        metrics = ev.get("metrics") or {}
        lat = _num(metrics.get("latency_ms"))
        if action == "turn":
            s = seat(name)
            s["rounds"] += 1
            overall["rounds"] += 1
            if lat is not None:
                s["_sum"] += lat
                s["_n"] += 1
                s["last_ms"] = lat
                overall["_sum"] += lat
                overall["_n"] += 1
                hid = (ev.get("artifact") or "").replace("handoff:", "")
                series.append({"ms": lat, "seat": name, "hid": hid, "ts": ev.get("ts")})
            rb = _num(metrics.get("resp_bytes"))
            if rb is not None:
                s["total_resp_bytes"] += rb
        elif action == "fail":
            s = seat(name)
            s["fails"] += 1
            overall["fails"] += 1

    def finish(d):
        avg = round(d["_sum"] / d["_n"], 1) if d["_n"] else None
        out = {k: v for k, v in d.items() if not k.startswith("_")}
        out["avg_ms"] = avg
        return out

    return {
        "seats": {n: finish(d) for n, d in seats.items()},
        "overall": finish(overall),
        "latency_series": series[-max(0, series_n) :],
    }


def _escalation_reason(rec: dict | None) -> str | None:
    """The short reason tag (e.g. ``verification_incomplete``) pulled from an escalation record's H1,
    or ``None`` when there is no escalation. Falls back to ``"escalated"`` when the file has no tag."""
    if not rec:
        return None
    m = re.search(r"\[([a-z][a-z0-9_]+)\]", rec.get("markdown", ""))
    return m.group(1) if m else "escalated"


def _row(collab, h: dict) -> dict:
    """One board row: id/slug/state plus routing (to/from), age in seconds, whether it is PARKED behind
    an escalation, and — for a closed handoff — HOW it closed.

    A row in ``done`` says nothing about authority on its own: the file is byte-identical whether the
    contract closed it or a human clicked through. ``closed_by``/``closed_label`` carry the persisted
    transition kind so the panel can render a human override as an override.

    ``escalated``/``escalation_reason`` come from the on-disk escalation store (``autopilot/escalations``):
    a pending/claimed handoff with an escalation is AWAITING A HUMAN — a driver refuses to auto-run it, so
    a Start that finds only such work idles silently. The panel needs this to say so instead of going blank.
    """
    to, frm = ap._to_from(Path(h["path"]))
    try:
        age = round(time.time() - os.path.getmtime(h["path"]), 1)
    except OSError:
        age = None
    tr = _transitions.read(collab, h["id"]) if h["state"] in ("done", "archive") else None
    try:
        esc_rec = esc.read(collab, h["id"]) if h["state"] in ("pending", "claimed") else None
    except Exception:
        esc_rec = None  # an unreadable escalation store must never crash the board
    return {
        "id": h["id"],
        "slug": h["slug"],
        "state": h["state"],
        "to": to or None,
        "from": frm or None,
        "age_s": age,
        "escalated": esc_rec is not None,
        "escalation_reason": _escalation_reason(esc_rec),
        "closed_by": (tr or {}).get("kind"),
        "closed_label": _transitions.label_of(tr) if h["state"] in ("done", "archive") else None,
        "closed_actor": (tr or {}).get("actor"),
        "closed_reason": (tr or {}).get("reason"),
        "closed_autonomously": _transitions.is_autonomous(tr),
    }


def board(collab) -> dict:
    """Handoffs grouped by state: ``{"pending":[row...], "claimed":[...], "done":[...], "archive":[...]}``.

    Rows within a state are sorted by id. Uses the authoritative directory view
    (:func:`handoff_core.list_handoffs`, which dedups transition-crash residuals).
    """
    grouped: dict[str, list] = {s: [] for s in hc.STATES}
    try:
        for h in hc.list_handoffs(collab):
            grouped.setdefault(h["state"], []).append(_row(collab, h))
    except cc.CollabError:
        pass  # unreadable/absent collab -> empty board, never a crash
    for rows in grouped.values():
        rows.sort(key=lambda r: r["id"])
    return grouped


def open_handoffs(collab) -> list[dict]:
    """Flat, id-sorted list of the actionable (pending+claimed) handoffs — the selection surface."""
    b = board(collab)
    return sorted(b.get("pending", []) + b.get("claimed", []), key=lambda r: r["id"])


def _seat_models(home) -> dict:
    """``{seat: {backend, launcher, model}}``. ``launcher`` is ``cmd[0]`` (e.g. ``claude``/``python``);
    ``model`` is the seat's chosen catalog id (``cfg["model"]`` — e.g. ``opus``/``gpt-5.5``) when the seat
    uses the ``models`` catalog, else the token right after a ``--model``/``-m`` flag in an explicit ``cmd``.
    ONLY these two tokens are surfaced — never the full argv — so base URLs, ``--key-env``, and file paths
    can never leak into the web page or a shared screenshot."""
    out: dict[str, dict] = {}
    try:
        seats = ap.load_seats(home)
    except Exception:
        return out  # missing/None home or corrupt seats.json — seat labels are optional, never fatal
    for name, cfg in seats.items():
        if not isinstance(cfg, dict):
            continue
        cmd = cfg.get("cmd")
        launcher = cmd[0] if isinstance(cmd, list) and cmd and isinstance(cmd[0], str) else None
        model = cfg.get("model") if isinstance(cfg.get("model"), str) else None
        if model is None and isinstance(cmd, list):  # explicit-cmd seat: fall back to a --model/-m flag
            for i, tok in enumerate(cmd):
                if tok in ("--model", "-m") and i + 1 < len(cmd) and isinstance(cmd[i + 1], str):
                    model = cmd[i + 1]
                    break
        out[name] = {"backend": cfg.get("backend"), "launcher": launcher, "model": model}
    return out


def set_seat_model(home, seat, model, *, by: str = "dashboard") -> dict:
    """Point a CLI seat at a different catalog model — the "any model in any seat" control ([C34]).

    Rewrites the seat's ``"model"`` id in ``seats.json`` (which :func:`autopilot.load_seats` composes into a
    runnable ``cmd`` from the top-level ``"models"`` catalog). Validated hard: the seat must exist AND be a
    ``backend == "cli"`` seat (human/web seats have no model), and ``model`` must be a known catalog id — a
    A bad seat/model raises :class:`collab_common.CollabError` rather than writing a
    configuration the driver cannot run.
    An existing explicit ``"cmd"`` on the seat is deleted so composition takes over; everything else
    (``model_args``, ``system``, ``can_sign_off``, ``timeout``, the catalog, closeout, notes) is preserved.
    The WHOLE doc is atomically re-published ([data-integrity]). Takes effect on the NEXT driver start (the
    running driver already holds its composed seats in memory)."""
    f = ap._seats_file(home)
    try:
        doc = json.loads(f.read_text("utf-8"))
        if not isinstance(doc, dict) or not isinstance(doc.get("seats"), dict):
            raise ValueError("missing 'seats' object")
    except (OSError, ValueError) as e:
        raise cc.CollabError(f"cannot read seats config {f}: {e}") from e
    seats = doc["seats"]
    cfg = seats.get(seat)
    if not isinstance(cfg, dict) or cfg.get("backend") != "cli":
        raise cc.CollabError(f"seat {seat!r} is not an existing CLI seat — cannot set its model")
    models = doc.get("models") if isinstance(doc.get("models"), dict) else {}
    if model not in models:
        valid = ", ".join(sorted(str(k) for k in models)) or "(none)"
        raise cc.CollabError(f"model {model!r} is not in the 'models' catalog; valid ids: {valid}")
    # Work on a detached candidate document.  The switch is only persisted
    # after both the selected seat and (for a v2 four-role config) the whole
    # managed assurance topology compile safely.
    candidate = json.loads(json.dumps(doc))
    candidate_cfg = candidate["seats"][seat]
    candidate_cfg["model"] = model
    candidate_cfg.pop("cmd", None)  # composition from the catalog takes over any explicit argv
    try:
        candidate_models = candidate.get("models") if isinstance(candidate.get("models"), dict) else {}
        adapter_profiles.compile_seat(seat, candidate_cfg, candidate_models)
        if "assessment_profiles" in candidate:
            verification_plan.resolve_assessment_profiles(candidate)
    except cc.CollabError as exc:
        raise cc.CollabError(f"refusing model switch for {seat!r}: {exc}") from exc
    cc.safe_write(f, json.dumps(candidate, indent=2, ensure_ascii=False) + "\n")
    return {"seat": seat, "model": model, "by": by}


def _latest_lanes(collab, *, run_uid: str | None = None, hid: str | None = None) -> dict | None:
    """Summary of the most recent adversarial-lane ledger for the dashboard lane matrix. Best-effort:
    ``None`` if no matching ledger exists. Surfaces only lane names, seat names, verdict counts, and the
    test pass flag — no repo paths or finding text (those stay in the reply artifacts / handoff viewer).

    ``run_uid``/``hid`` scope the search. Passing ``run_uid`` is what makes this honest: unscoped, this
    returned the newest ledger ON DISK — any handoff, any run — so a previous run's lane matrix rendered
    as the live one. A ledger with no ``run_uid`` predates the stamp and can never be proven to belong to
    the asked-for run, so it is EXCLUDED rather than assumed current: absent evidence must read as absent,
    not as someone else's evidence.
    """
    vdir = Path(collab) / "autopilot" / "verification"
    try:
        if hid is not None:
            # Ledgers for a handoff live under ``verification/<slugify(hid)>/`` (lanes.ledger_path); the
            # flat ``verification/<slugify(hid)>.ledger.json`` is the pre-v2 path. Scope to just this hid
            # instead of walking (rglob + per-file stat) EVERY run's ledgers on each ~1s poll — the
            # ``doc["hid"]`` filter below already required this hid, so narrowing WHERE we look does not
            # change WHAT we return.
            slug = cc.slugify(str(hid))
            found = list((vdir / slug).glob("*.ledger.json")) + list(vdir.glob(f"{slug}.ledger.json"))
        else:
            # No hid to scope by (idle / between runs): fall back to the full tree, matched by run_uid.
            found = list(vdir.rglob("*.ledger.json"))
        ledgers = sorted(found, key=lambda p: p.stat().st_mtime)
    except OSError:
        return None
    data = None
    for p in reversed(ledgers):  # newest first; take the newest that actually belongs to the asked-for run
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except OSError, ValueError:
            continue
        if not isinstance(doc, dict):
            continue
        if run_uid is not None and doc.get("run_uid") != run_uid:
            continue
        if hid is not None and str(doc.get("hid")) != str(hid):
            continue
        data = doc
        break
    if data is None:
        return None
    lanes = []
    for ln in data.get("lanes") or []:
        if not isinstance(ln, dict):
            continue
        profile = ln.get("profile") if isinstance(ln.get("profile"), dict) else {}
        breaker = ln.get("breaker_seat") or ((profile.get("breaker") or {}).get("seat"))
        verifier = ln.get("verifier_seat") or ((profile.get("verifier") or {}).get("seat"))
        lanes.append(
            {
                "lane": ln.get("pass") or ln.get("lane"),
                "pass": ln.get("pass") or ln.get("lane"),
                "profile": profile.get("id"),
                "contracts": ln.get("contracts") or [],
                "composite": bool(ln.get("composite")),
                "ran": bool(ln.get("ran")),
                "incomplete": bool(ln.get("incomplete")),
                "confirmed": len(ln.get("confirmed") or []),
                "refuted": len(ln.get("refuted") or []),
                "breaker": breaker,
                "verifier": verifier,
            }
        )
    return {
        "hid": data.get("hid"),
        "run_uid": data.get("run_uid"),
        "lanes": lanes,
        # A pytest-only record also carries passed=True. Rendering that as a green "tests ✓" chip is
        # the same conflation the done-gate made: report the LABEL and the authoritative verdict, so a
        # partial result cannot read as a full one on the panel.
        "tests_passed": bool((data.get("tests") or {}).get("passed")),
        "verification_green": _verification.is_green(data.get("tests") or {}),
        "verification_label": _verification.label_of(data.get("tests") or {}),
        "blockers": len(data.get("blockers") or []),
        "incomplete": bool(data.get("incomplete")),
        "plan_digest": data.get("verification_plan_digest"),
        "generated_ts": data.get("generated_ts"),
    }


# --------------------------------------------------------------------------- #
# run history (archived past runs + the live run, for the history/compare UI)
# --------------------------------------------------------------------------- #
#
# History lives on disk (contract A) at ``<collab>/autopilot/history/<run_uid>/run.json`` (+ an archived
# ``events.jsonl``, ``status.json`` and copied ledgers). We DECOUPLE from the producer by reading those
# JSON files directly — never importing run_history — so the dashboard and the archiver evolve independently.

# The run_uid path-safety guardrail: an id may only ever be a single, separator-free path component.
# A hostile id (``../x``, ``a/b``, an absolute path) must never widen the resolve below the history root.
_RUN_UID_RE = re.compile(r"^[0-9A-Za-z._-]{1,64}$")


def _history_root(collab) -> Path:
    """``<collab>/autopilot/history`` — the parent of every archived run dir."""
    return Path(collab) / "autopilot" / "history"


def _validate_run_uid(run_uid) -> str:
    """Return ``run_uid`` if it is a safe single path component, else raise :class:`ValueError`.

    Shape-only — the regex still admits ``.``/``..``; the *strict* defence is the resolve-under-root
    check in :func:`_run_dir`, which rejects any id that escapes the history root."""
    if not isinstance(run_uid, str) or not _RUN_UID_RE.fullmatch(run_uid):
        raise ValueError(f"invalid run_uid: {run_uid!r}")
    return run_uid


def _run_dir(collab, run_uid) -> Path:
    """Resolve ``<history>/<run_uid>`` strictly, refusing any path that is not a direct child of the
    history root (the real guardrail against ``..``/symlink escapes that the regex alone can't stop)."""
    _validate_run_uid(run_uid)
    root = _history_root(collab).resolve()
    run_dir = (root / run_uid).resolve()
    if run_dir.parent != root:
        raise ValueError(f"run_uid {run_uid!r} escapes the history root")
    return run_dir


# The list-UI summary fields carried out of a run.json (contract B). Missing keys default to None so a
# partially-written archive never crashes the list.
_SUMMARY_KEYS = (
    "run_uid",
    "started_ts",
    "ended_ts",
    "phase_final",
    "max_rounds",
    "rounds_total",
    "calls",
    "duration_ms",
    "lanes",
    "signoff",
    "seats",
    "terminal_reason",
    "escalations",
    "handoffs_touched",
)


def _run_summary(doc: dict) -> dict:
    """Project a run.json dict down to the fields the history list UI needs (contract B)."""
    return {k: doc.get(k) for k in _SUMMARY_KEYS}


def _load_run_json(collab, run_uid) -> dict:
    """Parse ``<history>/<run_uid>/run.json`` (path-safe). Raises :class:`ValueError` on a bad id;
    returns ``{}`` if the file is missing/torn/unreadable (best-effort, never crashes the poll)."""
    try:
        doc = json.loads((_run_dir(collab, run_uid) / "run.json").read_text("utf-8"))
        return doc if isinstance(doc, dict) else {}
    except ValueError:
        raise
    except OSError:
        return {}


def _current_summary(collab) -> dict | None:
    """Synthesize a list summary for the LIVE run from status.json, flagged ``current`` — or ``None``
    if no run is active (never ran, crashed, or the last run reached a terminal phase).

    The live lease — not ``phase`` — decides. A driver killed mid-round leaves a non-terminal phase
    behind forever, which made this synthesize a "current" row for a process that no longer exists.
    """
    if driver_running(collab) is None:
        return None  # nothing holds the board: there is no current run, whatever status.json still says
    st = read_status(collab)
    if not isinstance(st, dict):
        return None
    phase = st.get("phase")
    if phase in ("done", "capped"):
        return None  # a terminal run is history, not "current"
    ctrl = read_control(collab)
    summary: dict = {k: None for k in _SUMMARY_KEYS}
    summary.update(
        {
            "run_uid": st.get("run_uid") or "(current)",
            "started_ts": st.get("started_ts"),
            "ended_ts": None,
            "phase_final": phase,
            "max_rounds": ctrl.get("max_rounds")
            if isinstance(ctrl.get("max_rounds"), int)
            else st.get("max_rounds"),
            "rounds_total": st.get("round"),
            "lanes": _latest_lanes(collab, run_uid=st.get("run_uid"), hid=st.get("current_hid")),
            "terminal_reason": st.get("pause_reason"),  # the live pause cause (candidate lifecycle), if any
            "current": True,
        }
    )
    return summary


def list_runs(collab) -> list[dict]:
    """Every archived run's summary, NEWEST FIRST, with the live run (if any) synthesized in front.

    Reads ``<collab>/autopilot/history/*/run.json`` directly (contract A/B). Best-effort: unreadable/torn
    run.json files are skipped, a missing history dir yields ``[]`` — the poll never crashes. Archived
    runs are sorted by ``run_uid`` descending (the ids are time-sortable); the live run, when a run is
    active, is prepended flagged ``{"current": True}``."""
    out: list[dict] = []
    try:
        paths = list(_history_root(collab).glob("*/run.json"))
    except OSError:
        paths = []
    summaries: list[dict] = []
    for p in paths:
        try:
            doc = json.loads(p.read_text("utf-8"))
        except OSError, ValueError:
            continue  # missing/torn/unreadable archive — skip, never crash
        if not isinstance(doc, dict):
            continue
        summary = _run_summary(doc)
        summary.setdefault("run_uid", doc.get("run_uid") or p.parent.name)
        summaries.append(summary)
    summaries.sort(key=lambda s: str(s.get("run_uid") or ""), reverse=True)  # newest first
    current = _current_summary(collab)
    if current is not None:
        out.append(current)
    out.extend(summaries)
    return out


def run_detail(collab, run_uid) -> dict:
    """Full detail for one archived run: its summary, an events tail, and a derived lane summary.

    ``run_uid`` is validated (:func:`_validate_run_uid`) and resolved strictly under the history root
    (:func:`_run_dir`) — the id can never contain a path separator or escape the archive. Reads the run's
    ARCHIVED ``events.jsonl`` (not the live log), tailing the last 200 valid lines (torn lines skipped).
    Raises :class:`ValueError` on a bad id; a missing archive yields empty sections (best-effort)."""
    run_dir = _run_dir(collab, run_uid)  # raises ValueError on a bad/escaping id
    doc = _load_run_json(collab, run_uid)
    events: list[dict] = []
    try:
        lines = (run_dir / "events.jsonl").read_text("utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue  # torn/partial line — skip, never crash
        if isinstance(ev, dict):
            events.append(ev)
    return {"summary": doc, "events": events[-200:], "lanes": doc.get("lanes") or {}}


def _as_dict(v) -> dict:
    """``v`` if it is a dict, else ``{}`` — normalizes a best-effort JSON field for safe ``.get``."""
    return v if isinstance(v, dict) else {}


def _delta_num(a, b):
    """``b - a`` when both are real numbers (bool excluded), else ``None`` — best-effort numeric delta."""
    na, nb = _num(a), _num(b)
    return nb - na if na is not None and nb is not None else None


def _delta_cat(a, b) -> dict:
    """A categorical delta: the two values plus whether they changed."""
    return {"a": a, "b": b, "changed": a != b}


def _delta_seat_map(a, b) -> dict:
    """Per-seat numeric delta across the UNION of seats in ``a`` and ``b`` (missing side treated absent)."""
    a, b = _as_dict(a), _as_dict(b)
    return {seat: _delta_num(a.get(seat), b.get(seat)) for seat in sorted(set(a) | set(b))}


def compare_runs(collab, a, b) -> dict:
    """Diff two runs ``a`` and ``b`` for the compare UI: ``{"a":<summary>, "b":<summary>, "deltas":{...}}``.

    Both ids are validated + resolved under the history root. Numeric fields (rounds_total, calls,
    duration_ms, max_rounds, lanes.confirmed/refuted, per-seat seat_calls & seat_latency_ms) report
    ``b - a``; categorical fields (phase_final, signoff.result, git_sha, per-seat seats/model) report
    ``{a, b, changed}``. Raises :class:`ValueError` on a bad id; a missing run just yields empty summaries."""
    da = _load_run_json(collab, a)  # each validates + path-checks its id
    db = _load_run_json(collab, b)
    la, lb = _as_dict(da.get("lanes")), _as_dict(db.get("lanes"))
    sa, sb = _as_dict(da.get("signoff")), _as_dict(db.get("signoff"))
    deltas = {
        "rounds_total": _delta_num(da.get("rounds_total"), db.get("rounds_total")),
        "calls": _delta_num(da.get("calls"), db.get("calls")),
        "duration_ms": _delta_num(da.get("duration_ms"), db.get("duration_ms")),
        "max_rounds": _delta_num(da.get("max_rounds"), db.get("max_rounds")),
        "seat_calls": _delta_seat_map(da.get("seat_calls"), db.get("seat_calls")),
        "seat_latency_ms": _delta_seat_map(da.get("seat_latency_ms"), db.get("seat_latency_ms")),
        "lanes": {
            "confirmed": _delta_num(la.get("confirmed"), lb.get("confirmed")),
            "refuted": _delta_num(la.get("refuted"), lb.get("refuted")),
        },
        "phase_final": _delta_cat(da.get("phase_final"), db.get("phase_final")),
        "signoff_result": _delta_cat(sa.get("result"), sb.get("result")),
        "git_sha": _delta_cat(da.get("git_sha"), db.get("git_sha")),
        "seats": _seat_model_deltas(da.get("seats"), db.get("seats")),
    }
    return {"a": _run_summary(da), "b": _run_summary(db), "deltas": deltas}


def _seat_model_deltas(a, b) -> dict:
    """Per-seat model change across both runs' ``seats`` maps: ``{seat: {a, b, changed}}``."""
    a, b = _as_dict(a), _as_dict(b)
    return {seat: _delta_cat(a.get(seat), b.get(seat)) for seat in sorted(set(a) | set(b))}


def _epitaph_hid(status: dict, collab) -> tuple[str | None, list[str]]:
    """Resolve WHICH handoff(s) the finished run worked on, for the epitaph.

    ``status["current_hid"]`` cannot answer this. It is a LIVENESS field meaning "the handoff a seat
    is working on right now", and autopilot deliberately clears it to None the moment work stops
    (``autopilot.py`` writes ``current_hid=None`` on the done/paused/idle/post-seat paths). So by the
    time an epitaph is wanted, it is *always* None — the epitaph could never name a handoff, for any
    run, ever. Reading a liveness field to describe a corpse is a category error.

    The durable answer is ``handoffs_touched`` in the archived ``run.json``, which survives the run.
    Order: live field (a torn/racing read may still have it) -> durable history -> the hid prefix that
    ``last_error`` conventionally carries ("035 not closed (stalled); awaiting human").
    """
    hid = status.get("current_hid")
    touched: list[str] = []

    run_uid = status.get("run_uid")
    if collab is not None and run_uid:
        try:
            doc = _load_run_json(collab, run_uid)
        except ValueError:
            doc = {}  # bad/hostile run_uid: the poll must never crash over an epitaph
        raw = doc.get("handoffs_touched")
        if isinstance(raw, list):
            touched = [str(h) for h in raw if h not in (None, "")]

    if not hid and touched:
        hid = touched[-1]
    if not hid:
        m = re.match(r"\s*(\d{1,9})\b", str(status.get("last_error") or ""))
        if m:
            hid = m.group(1)
    return (str(hid) if hid else None), touched


def _last_run(status: dict | None, collab=None) -> dict | None:
    """The epitaph for a run that is no longer live: just enough to say WHICH run ended and WHEN, for the
    "no run active — last: 030 · ended 20:04" line. Deliberately not the run's data — the live panels go
    empty instead of rendering a corpse (see :func:`snapshot`).

    ``handoffs_touched`` is carried too: a run that worked 030, 031, 034 then stalled on 035 has one
    ``hid`` but four handoffs the operator needs to see."""
    if not isinstance(status, dict):
        return None
    hid, touched = _epitaph_hid(status, collab)
    return {
        "run_uid": status.get("run_uid"),
        "hid": hid,
        "handoffs_touched": touched,
        "phase_final": status.get("phase"),
        "pause_reason": status.get("pause_reason"),
        "started_ts": status.get("started_ts"),
        "ended_ts": status.get("ended_ts") or status.get("updated_ts"),
        "last_error": status.get("last_error"),
    }


def snapshot(collab, home=None) -> dict:
    """The single poll call both readers use — a self-contained view of the run right now.

    Merges the live status heartbeat, the control flags, the grouped board + counts, a tail of the
    event stream, the seat->model map, and (best-effort) the cross-collab registry rollup.

    LIVENESS IS THE BOARD LEASE, NOT ``status.json``. A driver that crashed, was killed, or exited never
    got to write a terminal status, so status.json keeps describing a run that stopped hours ago — which
    is exactly how a dead run's phase, feed, and lane matrix kept rendering as "now". The lease heartbeat
    is the only surface a dead process cannot keep fresh, so it is the one we trust.

    When no driver is live, every RUN-SCOPED panel is emptied (``status``, ``events``, ``stats``,
    ``lanes``) and ``last_run`` carries the epitaph. DURABLE state is NOT run-scoped and stays: the board
    (pending/claimed/done is true regardless of who is running), operator ``requests`` (queued intent for
    the next driver), ``seats``, and the ``runs`` history. Emptying those would be its own lie.
    """
    holder = driver_running(collab)  # the live lease, or None
    live = holder is not None
    status = read_status(collab)
    run_uid = (status or {}).get("run_uid") if live else None
    b = board(collab)
    counts = {s: len(rows) for s, rows in b.items()}
    ctrl = read_control(collab)
    evs = read_events(collab) if live else []  # one read feeds both the feed tail and the stats aggregation
    try:
        rollup = registry.status(home)
    except Exception:
        rollup = None  # no collabs.json / unreadable registry — optional context, never fatal
    try:
        catalog = sorted(k for k in ap.load_models(home) if not str(k).startswith("_"))
    except Exception:
        catalog = []  # None/missing home — the model picker is optional, never fatal
    try:
        requests = opreq.pending(collab)  # durable operator retry/adopt requests awaiting the driver
    except Exception:
        requests = []
    return {
        "collab": str(collab),
        "ts": ap._now_utc(),
        # --- run-scoped: present ONLY while a driver holds a live lease ---
        "live": live,
        "run_uid": run_uid,
        "status": status if live else None,
        "last_run": None if live else _last_run(status, collab),
        "events": evs[-60:],
        "stats": run_stats(evs),
        "lanes": _latest_lanes(collab, run_uid=run_uid, hid=(status or {}).get("current_hid"))
        if live
        else None,
        # --- durable: true regardless of whether anything is running ---
        "control": ctrl,
        "paused": bool(ctrl.get("paused")),
        "stop": bool(ctrl.get("stop")),
        "requests": requests,
        "driver_running": live,
        "board": b,
        "counts": counts,
        "open": open_handoffs(collab),
        "seats": _seat_models(home),
        "models_catalog": catalog,
        "rollup": rollup,
        "runs": list_runs(collab)[:25],  # newest ~25 for the history/compare UI (best-effort; kept cheap)
    }


# --------------------------------------------------------------------------- #
# human actions ([C36]: the driver never does these — a person, through the dashboard, does)
# --------------------------------------------------------------------------- #


def handoff_view(collab, hid: str) -> dict:
    """Read a handoff for the dashboard's reply viewer: whitelisted frontmatter + its body text.

    The id is resolved to a path through the state machine (``hc._reconcile``), never by joining the
    client string into a path. ``body_text`` reuses :func:`autopilot._substance`, which resolves an
    ``AUTOPILOT_REPLY`` pointer to the real reply artifact (path-constrained to the replies dir, 256KB
    cap [C28]). ``body_text`` is UNTRUSTED agent output [C38] — the caller must render it as text
    (``<pre>``/``textContent``), never as HTML. Only whitelisted frontmatter keys are returned so a
    hostile frontmatter field cannot leak into the page.
    """
    state, path = hc._reconcile(collab, hid)
    if path is None:
        raise hc.HandoffNotFound(f"handoff {hid} not found")
    parsed = contracts.parse_handoff(Path(path))
    fm = parsed.get("frontmatter") or {}
    safe = {k: fm.get(k) for k in ("to", "from", "title", "priority", "date", "status")}
    body = ap._substance(collab, Path(path))
    is_reply = bool(ap._POINTER_RE.search(parsed.get("raw") or ""))
    return {"id": hid, "state": state, "frontmatter": safe, "body_text": body, "is_reply": is_reply}


def narrative_view(collab, hid: str) -> dict:
    """The human-readable narrative of a handoff for the dashboard's "What happened" card.

    Returns ``{"id", "state", "markdown"}``. ``markdown`` is UNTRUSTED text ([C38]) — it stitches agent
    reply prose — so the web layer MUST render it as text (a safe structural pass), never as raw HTML.
    Read-only: :func:`narrative.build` transitions nothing and runs no agents. Raises
    :class:`handoff_core.HandoffNotFound` for an unknown id."""
    import narrative

    md = narrative.build(collab, hid)
    return {"id": hid, "state": hc.state_of(collab, hid), "markdown": md}


def advance_handoff(collab, hid: str, *, actor: str, reason: str) -> dict:
    """HUMAN OVERRIDE: a person advances a handoff to ``done`` on their own authority.

    ``pending`` -> claim then done; ``claimed`` -> done. ``done``/``archive`` are a no-op.
    Raises :class:`handoff_core.HandoffNotFound` for an unknown id.

    This path checks NO evidence — no ledger, no verdict, no verification receipt — and it deliberately
    stays that way: an operator must be able to close what the machinery cannot. ``actor`` and ``reason``
    are therefore mandatory, and the transition is persisted as
    :data:`transitions.KIND_HUMAN`. It is never labelled verified by any surface.

    Note it also auto-claims a ``pending`` handoff, so this can close work that was never built. That is
    the operator's prerogative and precisely why the override must be legible in the record rather than
    inferred from a best-effort log line (2026-07-15 audit).
    """
    state = hc.state_of(collab, hid)
    if state is None:
        raise hc.HandoffNotFound(f"handoff {hid} not found")
    if state in ("done", "archive"):
        return {"id": hid, "state": state, "changed": False}
    if state == "pending":
        hc.claim(collab, hid)
    hc.done(collab, hid, kind=_transitions.KIND_HUMAN, actor=actor, reason=reason)
    log, rid = ap._log_default(collab), ap._run_id(collab)
    ap._emit_safe(he.on_done, log, rid, hid, span_id=f"{hid}:done", parent_span_id=None)
    ap._emit_safe(
        _trace.emit,
        log,
        run_id=rid,
        stage="autopilot.control",
        role="human",
        artifact=f"handoff:{hid}",
        decision={
            "action": "human_override",
            "reason_codes": ["dashboard:advance", f"by:{actor}", f"reason:{reason[:80]}"],
            "confidence": None,
        },
    )
    try:  # attach the human-readable narrative to the handoff too (a human approval is still a closeout)
        import narrative

        narrative.write(collab, hid)
    except Exception:  # the summary is best-effort; the approval already stands regardless
        pass
    return {"id": hid, "state": "done", "changed": True}


def nudge(collab, hid: str) -> dict:
    """Re-queue a stuck handoff as a NEW pending handoff. Reads the stuck handoff's routing and creates a
    fresh pending handoff re-addressed to the same seat, referencing the original, and leaves the original
    where it is. Stays inside the reversible create-only envelope ([C36]).

    Cloning is this function's POINT, not a workaround for a missing edge: it re-asks a seat for work while
    preserving the original thread. It is NOT orphan recovery — leaving the original claimed is precisely
    what strands it, since ``_next_root`` scans ``pending`` only. A handoff whose driver died is un-stranded
    by :func:`handoff_core.reclaim` via ``autopilot._reclaim_orphans``, which MOVES it back to ``pending``.

    Returns the new ``{id, slug, path, state}``.
    """
    _state, path = hc._reconcile(collab, hid)
    if path is None:
        raise hc.HandoffNotFound(f"handoff {hid} not found")
    fm = contracts.parse_handoff(Path(path)).get("frontmatter") or {}
    to, frm = (fm.get("to") or "").strip(), (fm.get("from") or "").strip()
    if not to:
        raise cc.CollabError(f"handoff {hid} has no 'to' seat — cannot re-queue")
    body = f"Re-queued from {hid} by the dashboard; the original is stuck in claimed/."
    return hc.create(collab, to=to, from_=frm or "dashboard", title=f"re-queue of {hid}", body=body)


def reopen_handoff(collab, hid: str, *, action: str = "retry", by: str = "dashboard") -> dict:
    """RETRY a paused candidate (ADR-0003 reopen=retry). This is the operator's "give it another go" on a
    handoff the driver escalated and left in ``claimed`` (or one still ``pending``).

    It does NOT move handoff state — it files a DURABLE operator request (:mod:`operator_requests`) the
    driver consumes on its next loop pass (honoured even if no driver is running now). ``action="retry"``
    runs a fresh builder attempt; ``action="adopt"`` adopts the current on-disk source as the candidate.
    Either way the driver opens a new human-authorized budget epoch and the §18.3 contract still gates any
    close — a reopen can never force a ``done`` ([C36]). Raises :class:`handoff_core.HandoffNotFound` for an
    unknown id and :class:`handoff_core.HandoffConflict` for an already-closed one (nothing to retry)."""
    act = opreq.RETRY if action == "retry" else opreq.ADOPT if action == "adopt" else None
    if act is None:
        raise cc.CollabError(f"unknown reopen action {action!r}; expected 'retry' or 'adopt'")
    state = hc.state_of(collab, hid)
    if state is None:
        raise hc.HandoffNotFound(f"handoff {hid} not found")
    if state not in ("pending", "claimed"):
        raise hc.HandoffConflict(
            f"handoff {hid} is {state}; only a paused (pending/claimed) handoff can be retried"
        )
    rec = opreq.write(collab, hid, act, by=by)
    log, rid = ap._log_default(collab), ap._run_id(collab)
    ap._emit_safe(
        _trace.emit,
        log,
        run_id=rid,
        stage="autopilot.control",
        role="human",
        artifact=f"handoff:{hid}",
        decision={"action": "reopen", "reason_codes": [f"request:{act}", f"by:{by}"], "confidence": None},
    )
    return {"id": hid, "state": state, "action": rec["action"], "queued": True}


def driver_running(collab) -> dict | None:
    """The live driver's board-lease record if a driver is running (fresh heartbeat within the TTL), else
    ``None``. A stale lease (crashed driver, heartbeat past the TTL) reads as not-running — the same rule
    the lease itself uses to allow a reclaim."""
    try:
        holder = hc.ActiveHandoffLease(collab, "dashboard-probe").holder()
    except Exception:
        return None
    if not isinstance(holder, dict):
        return None
    hb = holder.get("heartbeat_epoch")
    if hb is None or (time.time() - float(hb)) >= hc._LEASE_TTL_S:
        return None
    return holder


def _spawn_detached(cmd: list) -> int:
    """Launch the driver as a detached background process and return its pid. Best-effort cross-platform
    detach so the dashboard request returns immediately and the driver outlives it."""
    import subprocess

    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)
    return proc.pid


def start_driver(
    collab, home=None, *, max_rounds=None, by: str = "dashboard", watch: bool = True, spawn=None
) -> dict:
    """Launch the autopilot driver against ``collab`` as a detached background process — the dashboard's
    "start" affordance. Refuses (``CollabError``) if a driver is already running (a live board lease), so a
    second concurrent driver can never be spawned (ADR-0003 D2). ``spawn`` is injectable for tests; it
    defaults to a detached :func:`subprocess.Popen`. Returns ``{collab, pid, started, by}``."""
    holder = driver_running(collab)
    if holder is not None:
        raise cc.CollabError(
            f"a driver is already running for this collab (run {holder.get('run_uid')!r}, "
            f"pid {holder.get('pid')}) — stop it before starting another"
        )
    # "Start" and a leftover ``stop`` are contradictory intents, and stop is STICKY: nothing else clears it
    # (no /api/unstop; set_stop is only ever called with True), so a stop from a previous session survives
    # indefinitely. Spawning over it yields a driver that returns at its first loop pass having touched
    # nothing, and reports phase="done" — a FALSE green that reads as "there was no work to do". The
    # operator pressed Start; that is unambiguous. Clear it here, where the intent is expressed. ([C36]:
    # this only ever un-idles the loop — no handoff is touched.)
    if read_control(collab).get("stop"):
        _write_control(collab, stop=False, requested_by=f"start:{by}")
    cmd = [sys.executable, str(Path(ap.__file__).resolve()), "--collab", str(collab)]
    if home:
        cmd += ["--home", str(home)]
    if watch:
        cmd += ["--watch"]
    if max_rounds is not None:
        cmd += ["--max-rounds", str(int(max_rounds))]
    pid = (spawn or _spawn_detached)(cmd)
    log, rid = ap._log_default(collab), ap._run_id(collab)
    ap._emit_safe(
        _trace.emit,
        log,
        run_id=rid,
        stage="autopilot.control",
        role="human",
        decision={"action": "start", "reason_codes": [f"by:{by}", f"pid:{pid}"], "confidence": None},
    )
    return {"collab": str(collab), "pid": pid, "started": True, "by": by}
