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
    hid: str, blockers: list, *, attempts: int, title: str | None = None, run_uid: str | None = None
) -> str:
    """The escalation markdown: what was confirmed, how many auto-fixes were tried, and how to clear it."""
    L = [_START.format(hid=hid)]
    head = f"# ⚠ Verified defect — needs a terminal fix: {hid}"
    if title:
        head += f" · {title}"
    L.append(head)
    L.append("")
    n = len(blockers or [])
    L.append(
        f"The adversarial lanes CONFIRMED **{n} defect{'s' if n != 1 else ''}**, and **{attempts} "
        f"autonomous fix attempt{'s' if attempts != 1 else ''}** did not clear them. The auto-fix loop "
        f"stopped here rather than thrash — this needs a human/expert fix."
    )
    if run_uid:
        L.append("")
        L.append(f"_Run: {run_uid}._")
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
        L.append("- (no structured findings were recorded in the ledger)")
    L.append("")
    L.append("## How to clear it")
    L.append(
        "1. Fix the cited code. 2. Re-queue the handoff (or re-run the lanes). "
        "3. Once the lanes are clean, delete this file (`escalation.clear`)."
    )
    L.append(_END.format(hid=hid))
    return "\n".join(L) + "\n"


def write(
    collab, hid: str, blockers: list, *, attempts: int, title: str | None = None, run_uid: str | None = None
) -> Path:
    """Persist the escalation to ``autopilot/escalations/<hid>.md`` (atomic). Returns the path."""
    p = _path(collab, hid)
    p.parent.mkdir(parents=True, exist_ok=True)
    cc.safe_write(p, render(hid, blockers, attempts=attempts, title=title, run_uid=run_uid))
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
