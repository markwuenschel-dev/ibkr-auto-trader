"""escalation — the terminal-fix hand-off for a verified defect the auto-fix loop could not clear.

Policy (user-chosen): when the adversarial lanes CONFIRM a defect, the driver makes ONE informed autonomous
builder fix attempt; if the lanes STILL confirm a defect after that, it STOPS auto-fixing and writes an
escalation here — a human-readable record of the verified defect(s) plus their reproduction, addressed to
the terminal operator. This is the "call to you" end of the loop: the driver never thrashes on a bug it
can't fix; it hands a well-scoped, reproduced defect to a human/expert.

Read-only for everyone except the driver (which writes) and whoever fixes the bug (which :func:`clear`s it
once the lanes are clean). The markdown is UNTRUSTED-derived (it embeds lane finding text) — any HTML
surface must render it as text ([C38])."""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

_LIB = str(Path(__file__).resolve().parent)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import collab_common as cc  # noqa: E402
import operational_state as ops  # noqa: E402

_START = "<!-- escalation:{hid} -->"
_END = "<!-- /escalation:{hid} -->"


def _dir(collab) -> Path:
    return Path(collab) / "autopilot" / "escalations"


def _path(collab, hid: str) -> Path:
    return _dir(collab) / f"{hid}.md"


def render(
    hid: str,
    blockers: list,
    *,
    attempts: int,
    title: str | None = None,
    run_uid: str | None = None,
    reason: str | None = None,
    cause: dict | None = None,
) -> str:
    """The escalation markdown: what was confirmed, how many auto-fixes were tried, and how to clear it.

    ``reason``/``cause`` are what make this honest. A stop is not evidence of a defect: the lanes can
    stop because a TOOL died (``infrastructure_blocked``) having proven nothing about the code. Rendering
    every stop as "⚠ Verified defect — needs a terminal fix" told the operator to go hunt a bug that no
    lane ever found, in code no lane ever finished checking. Say which of the two actually happened, and
    when it was the tool, name the tool failure instead of blaming the change.
    """
    n = len(blockers or [])
    L = [_START.format(hid=hid)]
    L.append(
        "<!-- escalation-meta:"
        + json.dumps(
            {
                "schema_version": "1.0",
                "reason": reason or "escalated",
                "severity": "warning",
                "run_uid": run_uid,
                "attempts": attempts,
                "required_action": "retry_or_adopt",
            },
            separators=(",", ":"),
        )
        + " -->"
    )
    if n:
        head = f"# ⚠ Verified defect — needs a terminal fix: {hid}"
    elif reason == "infrastructure_blocked":
        head = f"# ⚠ Stopped by a TOOL failure — no defect was confirmed: {hid}"
    elif reason == "verification_incomplete":
        head = f"# ⚠ Verification did not finish — no defect was confirmed: {hid}"
    else:
        head = f"# ⚠ Stopped without confirming a defect: {hid}"
    if title:
        head += f" · {title}"
    L.append(head)
    L.append("")
    if n:
        L.append(
            f"The adversarial lanes CONFIRMED **{n} defect{'s' if n != 1 else ''}**, and **{attempts} "
            f"autonomous fix attempt{'s' if attempts != 1 else ''}** did not clear them. The auto-fix loop "
            f"stopped here rather than thrash — this needs a human/expert fix."
        )
    else:
        L.append(
            f"The adversarial lanes CONFIRMED **0 defects** — nothing about this change has been shown to "
            f"be wrong. The run stopped after **{attempts} autonomous fix attempt"
            f"{'s' if attempts != 1 else ''}**"
            + (f" because: `{reason}`." if reason else ".")
            + " This is a TOOLING/PROCESS stop, NOT a code defect: the evidence is missing, not damning."
        )
    if run_uid:
        L.append("")
        L.append(f"_Run: {run_uid}._")
    if not n and cause:
        L.append("")
        L.append("## What actually failed")
        for k in ("lane", "seat", "error", "cmd"):
            if cause.get(k):
                L.append(f"- **{k}**: `{str(cause[k]).strip()}`")
    L.append("")
    L.append("## Confirmed defects")
    if blockers:
        for b in blockers:
            loc = str(b.get("description") or "").strip().replace("\n", " ")
            L.append(f"- **[{b.get('lane', 'lane')}]** {loc}")
            reg = b.get("regression_test")
            if reg:
                L.append(f"  - regression test: `{reg}`")
    else:
        L.append("- (none — no lane confirmed a finding)")
    L.append("")
    L.append("## How to clear it")
    if n:
        L.append(
            "1. Fix the cited code. 2. Re-queue the handoff (or re-run the lanes). "
            "3. Once the lanes are clean, delete this file (`escalation.clear`)."
        )
    else:
        L.append(
            "1. Fix the tool/environment named above. 2. Re-run the verification — an `adopt` request "
            "re-assesses the source already on disk and does NOT re-run the builder. 3. Once the lanes "
            "complete, delete this file (`escalation.clear`)."
        )
    L.append(_END.format(hid=hid))
    return "\n".join(L) + "\n"


def write(
    collab,
    hid: str,
    blockers: list,
    *,
    attempts: int,
    title: str | None = None,
    run_uid: str | None = None,
    reason: str | None = None,
    cause: dict | None = None,
) -> Path:
    """Persist the escalation to ``autopilot/escalations/<hid>.md`` (atomic). Returns the path."""
    p = _path(collab, hid)
    p.parent.mkdir(parents=True, exist_ok=True)
    cc.safe_write(
        p,
        render(hid, blockers, attempts=attempts, title=title, run_uid=run_uid, reason=reason, cause=cause),
    )
    timestamp = (
        datetime.fromtimestamp(p.stat().st_mtime, UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    ops.record_transition(
        collab,
        hid,
        ops.OperationalState.ESCALATED,
        reason=reason or "escalated",
        source="escalation_store",
        actor="autopilot",
        run_id=run_uid,
        event_ts=timestamp,
        conditions=("parked",),
        escalation_severity="warning",
        escalation_reason=reason or "escalated",
        escalation_ts=timestamp,
        required_action="retry_or_adopt",
    )
    return p


def read(collab, hid: str) -> dict | None:
    """Structured metadata + markdown for an open escalation, else ``None``."""
    try:
        path = _path(collab, hid)
        markdown = path.read_text("utf-8")
        meta: dict = {}
        meta_match = re.search(r"<!-- escalation-meta:(\{[^\n]*\}) -->", markdown)
        metadata_status = "legacy"
        if meta_match:
            try:
                parsed = json.loads(meta_match.group(1))
                if isinstance(parsed, dict) and parsed.get("schema_version") == "1.0":
                    meta = parsed
                    metadata_status = "healthy"
                else:
                    metadata_status = "malformed"
            except ValueError:
                metadata_status = "malformed"
        reason_match = re.search(r"\[([a-z][a-z0-9_]+)\]", markdown)
        run_match = re.search(r"_Run:\s*([^._\s]+)", markdown)
        timestamp = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        )
        return {
            "hid": hid,
            "markdown": markdown,
            "reason": meta.get("reason") or (reason_match.group(1) if reason_match else "escalated"),
            "severity": meta.get("severity") or "warning",
            "timestamp": timestamp,
            "run_uid": meta.get("run_uid") or (run_match.group(1) if run_match else None),
            "required_action": meta.get("required_action") or "retry_or_adopt",
            "metadata_status": metadata_status,
        }
    except OSError:
        return None


def pending(collab) -> list:
    """The hids with an open (unfixed) escalation, sorted. Best-effort; ``[]`` if the dir is absent."""
    try:
        return sorted(p.stem for p in _dir(collab).glob("*.md"))
    except OSError:
        return []


def clear(collab, hid: str) -> bool:
    """Remove the escalation once the defect is fixed. Returns True if a file was removed."""
    try:
        _path(collab, hid).unlink()
        ops.record_transition(
            collab,
            hid,
            ops.OperationalState.RETRYING,
            reason="escalation_cleared",
            source="escalation_store",
            actor="operator",
            required_action="start_driver",
        )
        return True
    except OSError:
        return False
