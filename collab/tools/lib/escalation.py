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

import sys
from pathlib import Path

_LIB = str(Path(__file__).resolve().parent)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import collab_common as cc  # noqa: E402

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
    return p


def read(collab, hid: str) -> dict | None:
    """``{"hid", "markdown"}`` for an open escalation, or ``None`` if there is none."""
    try:
        return {"hid": hid, "markdown": _path(collab, hid).read_text("utf-8")}
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
        return True
    except OSError:
        return False
