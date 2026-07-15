"""narrative — the human-readable story of a handoff ("what happened, and why it mattered").

Where :mod:`closeout_report` renders the *forensic* evidence (contract conditions, hashes, source==tested,
lane tallies), this module renders the *narrative*: plain English a person can read without decoding
telemetry. It answers four questions —

  * **Why did this matter?**   — the handoff's own ``## Summary`` prose (human/reviewer-written).
  * **What was asked for?**    — the ``## Deliverables`` bullets, condensed.
  * **How did it unfold?**     — one line per builder/reviewer turn, each seeded from what that agent
                                 actually wrote (its reply artifact), so the account is real, not invented.
  * **Where did it land?**     — done / capped / stalled, signed-off-autonomously or not, tests, rounds.

Nothing here calls a model or the network — it only *stitches text that already exists* (the handoff body
and the reply artifacts the driver already persisted), so the summary can never hallucinate a turn that did
not happen. It is STRICTLY READ-ONLY when *building*; :func:`write` is the only writer, and it only ever
appends an idempotent, clearly-delimited block to the handoff + a copy under ``autopilot/summaries/``.

Safety ([C38]): reply/handoff bodies are UNTRUSTED agent output. This module returns them as DATA (a
markdown string); any HTML surface (the dashboard) must render that string as text, never as markup. Reply
artifacts are read only from inside ``<collab>/autopilot/replies/`` (a pointer that escapes is ignored),
mirroring :func:`autopilot._substance` ([C28]).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

_LIB = str(Path(__file__).resolve().parent)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import autopilot as ap  # noqa: E402  (path/byte-cap helpers — single source of truth for the layout)
import closeout_report as cr  # noqa: E402  (reuse the evidence facts; never re-derive them here)
import collab_common as cc  # noqa: E402
import contracts  # noqa: E402
import handoff_core as hc  # noqa: E402
import verification as _verification  # noqa: E402

EXIT_OK, EXIT_USAGE, EXIT_NOTFOUND = 0, 1, 4

_START = "<!-- autopilot-narrative:{hid} -->"
_END = "<!-- /autopilot-narrative:{hid} -->"
# A block already embedded in a handoff, matched non-greedily so a re-generate replaces (never duplicates) it.
_BLOCK_RE = re.compile(
    r"\n*<!-- autopilot-narrative:(?P<hid>[^\s>]+) -->.*?<!-- /autopilot-narrative:(?P=hid) -->\n*",
    re.S,
)
# Phrases that mark a reply as a *blocker* (the agent stopped rather than doing the work) — surfaced so a
# capped/stalled run reads as "it hit a wall", not "it quietly did nothing".
_BLOCKER_HINTS = (
    "flag a blocker",
    "i can't do this",
    "i cannot do this",
    "out of scope",
    "i have to stop",
    "cannot proceed",
    "can't proceed",
    "## the problem",
    "i won't",
    "blocked —",
    "blocked -",
)


# --------------------------------------------------------------------------- #
# reading the durable surfaces (all read-only, all defensive)
# --------------------------------------------------------------------------- #


def _read_jsonl(path: Path) -> list[dict]:
    """Every valid JSON object in a ``.jsonl`` file, oldest-first. A torn/partial final line (a concurrent
    append) is skipped, never fatal — this is observability data, not a source of truth."""
    try:
        lines = path.read_text("utf-8").splitlines()
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
            continue
        if isinstance(ev, dict):
            out.append(ev)
    return out


def _run_feeds(collab) -> list[tuple]:
    """``[(run_uid, started_ts, events)]`` for every archived run, NEWEST FIRST (run_uids are time-sortable).

    The per-run ``autopilot/history/<uid>/events.jsonl`` is scoped to a single run, so it yields a *coherent*
    turn sequence — unlike the shared live log, where reruns of the same handoff interleave."""
    root = Path(collab) / "autopilot" / "history"
    try:
        dirs = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
    except OSError:
        return []
    feeds = []
    for d in dirs:
        events = _read_jsonl(d / "events.jsonl")
        if not events:
            continue
        started = None
        with contextlib.suppress(OSError, ValueError):
            started = (json.loads((d / "run.json").read_text("utf-8")) or {}).get("started_ts")
        feeds.append((d.name, started, events))
    return feeds


def _as_list(v) -> list[str]:
    """Normalise a frontmatter field that may arrive as a real list OR as a raw ``"[a, b]"`` string (the
    handoff frontmatter parser leaves some inline lists unparsed) into a clean list of strings."""
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("[") and s.endswith("]"):
            s = s[1:-1]
        return [p.strip() for p in s.split(",") if p.strip()]
    return []


def _art_hid(ev: dict) -> str:
    return str(ev.get("artifact") or "").replace("handoff:", "")


def _round_of(ev: dict):
    """The round number of a turn. Prefer ``metrics.round_no``; fall back to the ``r<N>:<seat>`` span id —
    the driver stamps round_no on the *start* event, not always on the *reply* event that carries the path."""
    m = (ev.get("metrics") or {}).get("round_no")
    if isinstance(m, int) and not isinstance(m, bool):
        return m
    mm = re.match(r"r(\d+):", str(ev.get("span_id") or ""))
    return int(mm.group(1)) if mm else None


def _is_turn(ev: dict, hid: str) -> bool:
    """A builder/reviewer *turn* on ``hid`` — a round event that persisted a reply artifact. Tolerant of the
    two action spellings the driver has used over time (``turn`` and ``reply``); the ``reply:`` reason code
    (the artifact path) is the real signal."""
    if not isinstance(ev, dict) or ev.get("stage") != "autopilot.round" or _art_hid(ev) != hid:
        return False
    dec = ev.get("decision") or {}
    if dec.get("action") not in ("turn", "reply"):
        return False
    return any(str(rc).startswith("reply:") for rc in (dec.get("reason_codes") or []))


def _reply_relpath(ev: dict) -> str | None:
    for rc in (ev.get("decision") or {}).get("reason_codes") or []:
        rc = str(rc)
        if rc.startswith("reply:"):
            return rc[len("reply:") :]
    return None


def _read_reply(collab, relpath: str | None) -> str:
    """The text of a reply artifact, read ONLY from inside ``<collab>/autopilot/replies/`` (a path that
    escapes is refused — [C28]) and byte-capped like every other agent-output read."""
    if not relpath:
        return ""
    try:
        base = ap._replies_dir(collab).resolve()
        art = (Path(collab) / relpath).resolve()
    except OSError:
        return ""
    if base != art.parent and base not in art.parents:
        return ""
    try:
        with open(art, "rb") as fh:
            return fh.read(ap._MAX_RESP_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return ""


# --------------------------------------------------------------------------- #
# prose extraction — turn what an agent wrote into one readable line
# --------------------------------------------------------------------------- #


def _strip_marks(s: str) -> str:
    """Drop inline markdown emphasis/code marks so a gist reads as prose (the dashboard re-styles structure
    from the block markers, not from leftover ``**``/`` ` `` inside a line)."""
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _gist(text: str, limit: int = 280) -> tuple[str, bool]:
    """A one-line gist of a reply plus whether it reads as a *blocker*.

    Heuristic, deliberately simple: skip headings/tables/fences, take the first substantive prose lines up
    to ``limit`` chars. It reflects what the agent actually opened with — the honest signal — rather than
    guessing intent. Returns ``("", False)`` for empty input."""
    text = text or ""
    blocker = any(h in text[:700].lower() for h in _BLOCKER_HINTS)
    picked: list[str] = []
    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            if picked:
                break  # blank line after we have content — end of the opening paragraph
            continue
        if s.startswith(("#", "|", "```", "---", ">")):
            continue  # structure, not prose
        s = re.sub(r"^[-*]\s+", "", s)  # a leading bullet marker
        picked.append(s)
        if sum(len(p) for p in picked) >= limit:
            break
    gist = _strip_marks(" ".join(picked))
    if len(gist) > limit:
        gist = gist[: limit - 1].rstrip() + "…"
    return gist, blocker


def _paragraphs(text: str, n: int = 2, limit: int = 620) -> str:
    """The first ``n`` paragraphs of a section, capped — enough of the 'why' to be meaningful without
    pasting the whole design doc. Blank-line separated; each paragraph collapsed to one line."""
    text = (text or "").strip()
    if not text:
        return ""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    kept = [_strip_marks(p) for p in paras[:n]]
    out = "\n\n".join(kept)
    if len(out) > limit:
        out = out[: limit - 1].rstrip() + "…"
    return out


def _deliverable_lines(text: str, cap: int = 8) -> list[str]:
    """The lead of each ``## Deliverables`` bullet (up to ``cap``), condensed to the file/what before any
    long dash — so the reader sees *what was built*, not the full spec of each item."""
    out: list[str] = []
    for raw in (text or "").splitlines():
        m = re.match(r"^\s*[-*]\s+(.*)$", raw)
        if not m:
            continue
        item = _strip_marks(m.group(1))
        item = re.split(r"\s[—-]\s", item, maxsplit=1)[0].strip()  # keep the lead (before " — ")
        if item:
            out.append(item)
        if len(out) >= cap:
            break
    return out


# --------------------------------------------------------------------------- #
# collect + render
# --------------------------------------------------------------------------- #


def _clock(started: str | None) -> str:
    """``HH:MM`` for a timestamp from TODAY; ``Mon D, HH:MM`` for any other day. Unparseable -> raw string.

    The date is load-bearing, not decoration: a bare ``18:20`` on a run from a previous day reads as "just
    now", so stale history renders as a live failure and the operator debugs a ghost. Say the day whenever
    it isn't today. Timestamps are UTC (``...Z``), so compare against UTC today.
    """
    s = str(started or "")
    if not ("T" in s and len(s) >= 16):
        return s
    hhmm = s[11:16]
    if s[:10] == time.strftime("%Y-%m-%d", time.gmtime()):
        return hhmm
    try:
        d = datetime.strptime(s[:10], "%Y-%m-%d")
    except ValueError:
        return hhmm  # time parsed but the date didn't — a bare clock beats nothing
    return f"{d.strftime('%b')} {d.day}, {hhmm}"


def _run_label(uid: str | None, started: str | None, is_current: bool = False) -> str:
    """A human run label: ``Current run`` / ``Previous run`` + a plain clock time, instead of a raw run_uid +
    ISO timestamp. ``is_current`` is decided by the caller (:func:`_choose_events` — the live run when its
    turns post-date ``status.started_ts``)."""
    if not uid and not started:
        return "the live run"
    who = "Current run" if is_current else "Previous run"
    clk = _clock(started)
    return f"{who} · {clk}" if clk else who


_CONF_RE = re.compile(r"^\s*[-*]\s*\[\s*(met|partial|missing)\s*\]\s*(.+?)\s*$", re.IGNORECASE)


def _conformance(text: str | None) -> list[dict]:
    """Parse a reviewer's spec-conformance itemization from a reply — lines of the form
    ``- [met|partial|missing] <item>`` (the reviewer checks each ADR-contract /
    Definition-of-done item against the diff). Returns ``[{status, item}, …]``; ``[]`` if none.
    Advisory evidence is surfaced in the summary,
    NOT gated on by the done-contract (a reviewer that ignores its own itemization can still sign off)."""
    out: list[dict] = []
    for line in str(text or "").splitlines():
        m = _CONF_RE.match(line)
        if m:
            out.append({"status": m.group(1).lower(), "item": m.group(2).strip()})
    return out


def _read_status(collab) -> dict:
    """The driver's live status.json (``{}`` if absent/torn)."""
    try:
        st = json.loads(ap._status_path(collab).read_text("utf-8"))
        return st if isinstance(st, dict) else {}
    except OSError, ValueError:
        return {}


def _model_map(collab, *, is_current: bool = True) -> dict:
    """``{role: model}`` for the run being narrated, from status.json's ``run_seats`` — used only to label
    turns (e.g. "builder (gpt-5.6-terra)").

    ``run_seats`` describes the run status.json currently names, so it is ONLY valid when we are narrating
    that run. Applying it to an archived run's turns prints those turns under models that never produced
    them — a fabricated attribution. When the story is not the current run's, label the roles plainly and
    say nothing about the model rather than say something false.
    """
    if not is_current:
        return {}
    rs = _read_status(collab).get("run_seats")
    return rs if isinstance(rs, dict) else {}


def _choose_events(collab, hid: str) -> tuple:
    """``(run_label, events, is_current)`` for the run whose story to tell.

    Prefer the **LIVE** run first: its turns are in ``logs/events.jsonl`` but NOT yet archived to history, so
    a lookup that only scanned history would fall back to a PRIOR run of the same hid and show its (possibly
    failed) turns — mislabeled with the current run's models. We scope the live log to events at/after the
    run's ``started_ts`` so an earlier run's turns for the same hid can't bleed in. Only if no live run is
    driving this hid do we fall back to the newest archived run, which is labelled as the past run it is.

    Once a run HAS started, exhausting both lookups means this hid has no turns in the current run and
    none in any archived run — so any turns still sitting in the log belong to a run we cannot identify
    (``_run_id`` is the collab slug, not the run_uid, so events carry no run field). Returning that pile
    labelled "the live run" would present unattributable work as this run's, so we return no turns at
    all. Only when NO run has ever started here — no status.json, nothing to be confused with — is the
    unscoped log safe to narrate.
    """
    log_events = _read_jsonl(Path(ap._log_default(collab)))
    status = _read_status(collab)
    started = status.get("started_ts")
    if started:
        live = [e for e in log_events if str(e.get("ts") or "") >= str(started)]
        if any(_is_turn(e, hid) for e in live):
            return _run_label(status.get("run_uid"), started, is_current=True), live, True
    for uid, s, events in _run_feeds(collab):
        if any(_is_turn(e, hid) for e in events):
            return _run_label(uid, s, is_current=False), events, False
    if not started:
        return _run_label(None, None), log_events, True  # no run ever ran: nothing to mistake this for
    return _run_label(status.get("run_uid"), started, is_current=True), [], True


def collect(collab, hid: str) -> dict:
    """Gather everything the narrative needs. Read-only. Raises :class:`handoff_core.HandoffNotFound` if the
    id resolves to no handoff (no state, no file)."""
    collab = str(cr._resolve_collab(collab))
    state, path = hc._reconcile(collab, hid)
    if path is None:
        raise hc.HandoffNotFound(f"handoff {hid} not found")

    parsed = contracts.parse_handoff(Path(path))
    fm = parsed.get("frontmatter") or {}
    sections = parsed.get("sections") or {}

    evidence: dict = {}
    try:
        evidence = cr.collect(collab, hid)  # state/tests/lanes/done-contract/autonomous_done — the facts
    except Exception:
        evidence = {}

    esc = None
    try:
        import escalation as _esc

        esc = _esc.read(collab, hid)  # an open terminal-fix escalation for this handoff, if any
    except Exception:
        esc = None

    run_label, events, is_current = _choose_events(collab, hid)
    models = _model_map(collab, is_current=is_current)
    turns = []
    conformance: list[
        dict
    ] = []  # the reviewer's spec-conformance itemization (last review that emits one wins)
    for ev in events:
        if not _is_turn(ev, hid):
            continue
        metrics = ev.get("metrics") or {}
        role = ev.get("role") or "?"
        reply_text = _read_reply(collab, _reply_relpath(ev))
        gist, blocker = _gist(reply_text, limit=280)
        conf = _conformance(reply_text)
        if conf:
            conformance = conf  # events are oldest-first -> the final reviewer sign-off's itemization stands
        turns.append(
            {
                "round": _round_of(ev),
                "role": role,
                "model": models.get(role),
                "ts": ev.get("ts"),
                "latency_ms": metrics.get("latency_ms"),
                "gist": gist,
                "blocker": blocker,
            }
        )
    turns.sort(key=lambda t: (t["round"] if isinstance(t["round"], int) else 1_000, t["ts"] or ""))

    last_reply = _read_reply(collab, _reply_relpath(_last_turn_event(events, hid))) if turns else ""
    last_gist, last_blocker = _gist(last_reply, limit=520)

    cap_codes = (f"root:{hid}", "outcome:capped")
    capped = any(
        e.get("stage") == "autopilot.pause"
        and any(str(rc) in cap_codes for rc in ((e.get("decision") or {}).get("reason_codes") or []))
        for e in events
    )
    blocked = [
        ", ".join(str(rc) for rc in ((e.get("decision") or {}).get("reason_codes") or []))
        for e in events
        if e.get("stage") == "autopilot.signoff_blocked" and _art_hid(e) == hid
    ]

    return {
        "hid": hid,
        "title": fm.get("title"),
        "state": state,
        "why": _paragraphs(sections.get("Summary", "")),
        "guardrails": _as_list(fm.get("guardrails")),
        "depends_on": _as_list(fm.get("depends_on")),
        "adr": fm.get("adr"),
        "deliverables": _deliverable_lines(sections.get("Deliverables", "")),
        "dod": _deliverable_lines(sections.get("Definition of done", "")),
        "contract": _deliverable_lines(sections.get("The contract (ADR decisions)", "")),
        "conformance": conformance,
        "run_label": run_label,
        "turns": turns,
        "last_turn": {"gist": last_gist, "blocker": last_blocker} if turns else None,
        "signed_off": bool(evidence.get("autonomous_done_event")),
        "closed_autonomously": bool(evidence.get("closed_autonomously")),
        "tests_passed": (evidence.get("tests") or {}).get("passed"),
        "verification_green": _verification.is_green(evidence.get("tests") or {}),
        "verification_label": _verification.label_of(evidence.get("tests") or {}),
        "capped": capped,
        "signoff_blocked": blocked,
        "escalated": bool(esc),
    }


def _last_turn_event(events: list[dict], hid: str) -> dict | None:
    best = None
    for ev in events:
        if _is_turn(ev, hid):
            best = ev  # events are oldest-first, so the last match is the final turn
    return best


def _fmt_ms(ms) -> str:
    if not isinstance(ms, (int, float)):
        return ""
    return f"{round(ms)}ms" if ms < 1000 else f"{ms / 1000:.1f}s"


def _bottom_line(d: dict) -> str:
    n = len([t for t in d["turns"] if t.get("round") is not None]) or len(d["turns"])
    rounds = f"{n} round{'s' if n != 1 else ''}"
    if d.get("escalated"):
        return (
            "**⚠ Escalated to you** — the auto-fix loop could not clear a lane-confirmed defect after "
            "one attempt and handed it to the terminal for a human fix."
        )
    if d["state"] == "done" and d["closed_autonomously"]:
        return (
            f"**Signed off and shipped autonomously** after {rounds} — the evidence contract was satisfied."
        )
    if d["state"] == "done":
        return f"**Marked done** after {rounds} (approved by a human, or finished out of band)."
    if d["capped"]:
        return (
            f"**Ran the full round budget ({rounds}) without an autonomous sign-off** — "
            f"the gate held. Nothing shipped on its own; your call."
        )
    if d["last_turn"] and d["last_turn"]["blocker"]:
        return f"**Stalled on a blocker** after {rounds} — an agent stopped rather than guess. Needs a human."
    if d["signoff_blocked"]:
        return (
            f"**A sign-off was blocked** by the evidence contract after {rounds} — see the conditions below."
        )
    if d["state"] in ("claimed",):
        return f"**In progress** — {rounds} exchanged so far, not yet signed off."
    return f"**Queued** — addressed and waiting; {rounds} so far."


def render_markdown(d: dict) -> str:
    hid = d["hid"]
    L: list[str] = [_START.format(hid=hid)]
    head = f"# What happened — {hid}"
    if d.get("title"):
        head += f" · {d['title']}"
    L.append(head)
    L.append("")
    L.append(_bottom_line(d))
    L.append("")

    if d.get("escalated"):
        L.append("## ⚠ Needs your fix")
        L.append(
            "A lane-confirmed defect survived one autonomous fix attempt, so the driver stopped and "
            f"escalated it to you. The verified defect + reproduction are in `autopilot/escalations/"
            f"{hid}.md`."
        )
        L.append("")

    if d.get("why"):
        L.append("## Why this mattered")
        L.append(d["why"])
        meta = []
        if d["guardrails"]:
            meta.append("Guardrails: " + ", ".join(str(g) for g in d["guardrails"]))
        if d["depends_on"]:
            meta.append("Depends on: " + ", ".join(str(x) for x in d["depends_on"]))
        if d.get("adr"):
            meta.append("Design of record: " + str(d["adr"]))
        for m in meta:
            L.append(f"- {m}")
        L.append("")

    if d.get("deliverables"):
        L.append("## What was asked for")
        for item in d["deliverables"]:
            L.append(f"- {item}")
        L.append("")

    if d.get("contract") or d.get("dod"):
        L.append("## The contract & definition of done")
        for item in d.get("contract") or []:
            L.append(f"- {item}")
        for item in d.get("dod") or []:
            L.append(f"- {item}")
        L.append("")

    if d.get("turns"):
        L.append(f"## How it unfolded — {d['run_label']}")
        for t in d["turns"]:
            rd = f"Round {t['round']}" if t.get("round") is not None else "Turn"
            who = t["role"] + (f", {t['model']}" if t.get("model") else "")
            lat = _fmt_ms(t.get("latency_ms"))
            tag = " ⚠ blocker —" if t.get("blocker") else " —"
            head = f"- **{rd} · {who}"
            head += f" ({lat})**" if lat else "**"
            L.append(f"{head}{tag} {t['gist'] or '(no readable content)'}")
        L.append("")

    if d.get("last_turn"):
        L.append("## The last turn")
        pre = "⚠ " if d["last_turn"]["blocker"] else ""
        L.append(pre + (d["last_turn"]["gist"] or "(no readable content)"))
        L.append("")

    L.append("## Where it landed")
    L.append(f"- Final state: **{d['state'] or 'unknown'}**")
    so = "yes" if d["closed_autonomously"] else ("recorded" if d["signed_off"] else "no")
    L.append(f"- Signed off autonomously: **{so}**")
    # Name what actually ran. "Tests: passed" for a pytest-only run reads as a verified checkout.
    # A summary dict predating these keys renders UNVERIFIED rather than raising -- fail closed, and
    # never silently fall back to the bare boolean this line exists to stop trusting.
    L.append(f"- Verification: **{d.get('verification_label') or _verification.LABEL_UNVERIFIED}**")
    L.append(f"- Authoritatively green: **{'yes' if d.get('verification_green') else 'no'}**")
    if d["signoff_blocked"]:
        L.append(f"- Sign-off blocked: {'; '.join(d['signoff_blocked'])[:200]}")
    if d.get("conformance"):
        marks = {"met": "✓", "partial": "~", "missing": "✗"}
        missing = sum(1 for c in d["conformance"] if c["status"] in ("partial", "missing"))
        L.append(
            f"- Spec conformance (reviewer itemization) — {len(d['conformance'])} items, {missing} unmet:"
        )
        for c in d["conformance"]:
            L.append(f"    - {marks.get(c['status'], '•')} **{c['status']}** — {c['item']}")
    L.append("")
    L.append(f"_Full evidence audit: `closeout-report <collab> {hid}`._")
    L.append(_END.format(hid=hid))
    return "\n".join(L) + "\n"


def build(collab, hid: str) -> str:
    """Collect + render the narrative markdown in one call (read-only)."""
    return render_markdown(collect(collab, hid))


# --------------------------------------------------------------------------- #
# the only writer: persist the narrative onto the handoff + a durable copy
# --------------------------------------------------------------------------- #


def _upsert_block(path: Path, hid: str, block: str) -> None:
    """Append ``block`` to the handoff, or replace the existing narrative block in place — idempotent, so
    re-generating never stacks duplicates. Atomic via :func:`collab_common.safe_write`."""
    try:
        text = path.read_text("utf-8")
    except OSError:
        return
    if _BLOCK_RE.search(text):
        new = _BLOCK_RE.sub("\n\n" + block, text, count=1)
    else:
        new = text.rstrip() + "\n\n" + block
    cc.safe_write(path, new)


def write(collab, hid: str) -> Path:
    """Build the narrative and persist it two ways: a durable ``autopilot/summaries/<hid>.md`` (what the
    dashboard reads) and an idempotent block appended to the handoff itself (so the story travels *with* the
    handoff). Returns the summaries path. Read-only up to the two writes; safe to call repeatedly."""
    collab = str(cr._resolve_collab(collab))
    md = build(collab, hid)
    sp = Path(collab) / "autopilot" / "summaries" / f"{hid}.md"
    sp.parent.mkdir(parents=True, exist_ok=True)
    cc.safe_write(sp, md)
    _, path = hc._reconcile(collab, hid)
    if path is not None:
        _upsert_block(Path(path), hid, md)
    return sp


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # the narrative is prose with unicode (arrows, ⚠); never let a cp1252 console truncate it
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(
        prog="narrative", description="human-readable narrative of a handoff (what happened, and why)"
    )
    p.add_argument("collab", help="collab name (registry) or path")
    p.add_argument("hid", help="handoff id")
    p.add_argument(
        "--write",
        action="store_true",
        help="persist onto the handoff + autopilot/summaries/ instead of printing",
    )
    try:
        args = p.parse_args(argv)
    except SystemExit as e:
        return EXIT_OK if e.code in (0, None) else EXIT_USAGE
    try:
        if args.write:
            path = write(args.collab, args.hid)
            print(f"wrote {path}")
        else:
            sys.stdout.write(build(args.collab, args.hid))
    except hc.HandoffNotFound as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_NOTFOUND
    except (cc.CollabError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
