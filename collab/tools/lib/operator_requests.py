"""operator_requests — the durable operator-request queue the dashboard writes and the driver consumes.

A human action on a *paused* candidate must survive the driver process: when the driver escalated a
handoff and exited, "retry it" or "adopt the current source as its candidate" has to be honoured even
though no driver is running to hear it. The dashboard therefore does not poke a live process — it writes
an immutable request file ``<collab>/autopilot/requests/<hid>.json``, and the driver consumes pending
requests at the top of its loop (on its next start, however much later).

One open request per handoff (a new one supersedes the old — the operator's latest intent wins). A request
is inert data ([C36]): it can only ever make the driver *re-drive* a handoff it already owns — it never
advances or deletes a handoff itself, and the §18.3 evidence contract still gates every close. Consuming a
request is a plain file delete; a request for an unknown/closed handoff is consumed and skipped.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

_LIB = str(Path(__file__).resolve().parent)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import collab_common as cc  # noqa: E402

# Request actions (ADR-0003): re-drive a paused handoff (fresh builder attempt, new budget epoch), or adopt
# the current on-disk source as the candidate (no builder attempt — the operator vouches for it; the
# contract still gates the close). Both open a human-authorized budget epoch.
RETRY = "retry"
ADOPT = "adopt"
_ACTIONS = (RETRY, ADOPT)

# A handoff id is a short zero-padded integer — never a path fragment. This is the sole guard against a
# hostile id widening the write/read below the requests dir.
_HID_RE = re.compile(r"^\d{1,9}$")


class BadRequest(cc.CollabError):
    """A malformed operator request (bad hid or unknown action)."""


def _dir(collab) -> Path:
    return Path(collab) / "autopilot" / "requests"


def _validate_hid(hid) -> str:
    if not isinstance(hid, str) or not _HID_RE.fullmatch(hid):
        raise BadRequest(f"invalid handoff id for a request: {hid!r}")
    return hid


def _path(collab, hid: str) -> Path:
    return _dir(collab) / f"{_validate_hid(hid)}.json"


def _now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def write(collab, hid: str, action: str, *, by: str = "dashboard", note: str | None = None,
          now_ts: str | None = None) -> dict:
    """File a durable operator request for ``hid`` (atomic). Supersedes any open request for the same
    handoff — the operator's latest intent wins. Raises :class:`BadRequest` on a bad id or unknown action."""
    _validate_hid(hid)
    if action not in _ACTIONS:
        raise BadRequest(f"unknown request action {action!r}; expected one of {_ACTIONS}")
    rec = {"schema_version": "0.1", "hid": hid, "action": action, "requested_by": by,
           "requested_ts": now_ts or _now_utc(), "note": note}
    p = _path(collab, hid)
    p.parent.mkdir(parents=True, exist_ok=True)
    cc.safe_write(p, json.dumps(rec, separators=(",", ":")) + "\n")
    return rec


def get(collab, hid: str) -> dict | None:
    """The open request for ``hid`` (validated action), or ``None`` if there is none / it is malformed."""
    try:
        doc = json.loads(_path(collab, hid).read_text("utf-8"))
    except BadRequest:
        raise
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("action") not in _ACTIONS:
        return None
    doc.setdefault("hid", hid)
    return doc


def pending(collab) -> list[dict]:
    """Every open, well-formed request, sorted by numeric handoff id. Best-effort: an absent dir or a torn
    file yields fewer entries, never a crash. A stray non-``<digits>.json`` file is ignored."""
    out: list[dict] = []
    try:
        files = sorted(_dir(collab).glob("*.json"))
    except OSError:
        return out
    for f in files:
        hid = f.stem
        if not _HID_RE.fullmatch(hid):
            continue
        rec = get(collab, hid)
        if rec is not None:
            out.append(rec)
    out.sort(key=lambda r: int(r["hid"]))
    return out


def consume(collab, hid: str) -> bool:
    """Remove the request for ``hid`` once the driver has acted on it. Idempotent; returns True if a file
    was removed. A consumed request is never re-honoured (the operator re-files if they want it again)."""
    try:
        _path(collab, hid).unlink()
        return True
    except (OSError, BadRequest):
        return False
