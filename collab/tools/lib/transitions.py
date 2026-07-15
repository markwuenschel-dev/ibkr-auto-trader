"""transitions — WHO closed a handoff, and on what authority.

The directory is the state ([C10], ``handoff_core``): a handoff is ``done`` because its file sits in
``done/``. That is single-winner and crash-safe, and it is also completely silent about provenance --
the file in ``done/`` is byte-identical whether a satisfied 11-condition contract put it there or a
human clicked a button. Before 2026-07-15 that distinction survived only as a best-effort trace line
(``_emit_safe``), so a dropped log entry made an autonomous close and a human override indistinguishable,
and every reporting surface had to *infer* which had happened.

This module makes the distinction a persisted fact written on the transition itself:

  * :data:`KIND_AUTONOMOUS` -- the done-contract was satisfied; carries the ``receipt`` (contract hash,
    plus the candidate it attests). Only ``autopilot``/``self_host_smoke`` can honestly claim it.
  * :data:`KIND_HUMAN` -- a person decided. Carries an ``actor`` and a mandatory free-text ``reason``,
    and is NEVER labelled as verified anywhere.

**Human override stays possible.** It is not a failure mode to be designed out -- an operator must be
able to close a handoff the machinery cannot. What it may not do is *look like* verification. So the
reason is required (an override with no stated reason is not an override, it is an accident) and
:func:`is_autonomous` is the single reader that grants the verified label.

**Fail-closed on absence.** A missing or unparseable record reads as :data:`LABEL_UNRECORDED` -- never
as autonomous. The record is written AFTER the CAS commits, so a crash in between leaves a ``done``
handoff with unknown provenance; unknown must never mean verified.

Layout::

    <collab>/handoffs/.transitions/{NNN}.json
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collab_common as cc

KIND_AUTONOMOUS = "autonomous_verified"
KIND_HUMAN = "human_override"
KINDS = (KIND_AUTONOMOUS, KIND_HUMAN)

# The words each kind is allowed to use. No kind but AUTONOMOUS may contain "VERIFIED"/"GREEN".
LABEL_AUTONOMOUS = "AUTONOMOUS VERIFIED — closed on an authoritative verification receipt"
LABEL_HUMAN = "HUMAN OVERRIDE — closed by a person; NOT authoritatively verified"
LABEL_UNRECORDED = "UNRECORDED — no transition record; provenance unknown"

_DIRNAME = ".transitions"


def _dir(collab) -> Path:
    return Path(collab) / "handoffs" / _DIRNAME


def _path(collab, hid) -> Path:
    return _dir(collab) / f"{hid}.json"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate(*, kind, actor, reason=None, receipt=None) -> dict:
    """Check a proposed transition BEFORE the CAS; raise ``CollabError`` if it may not be claimed.

    Called first by :func:`handoff_core.done` so an unlabelled or dishonest close never moves a file.
    The asymmetry is deliberate: an autonomous close must produce a ``receipt`` it cannot invent, and a
    human override must produce a ``reason`` a machine cannot invent.
    """
    if kind not in KINDS:
        raise cc.CollabError(f"transition kind must be one of {KINDS}, got {kind!r}")
    if not isinstance(actor, str) or not actor.strip():
        raise cc.CollabError(f"transition actor is required (kind={kind!r})")
    if kind == KIND_HUMAN and (not isinstance(reason, str) or not reason.strip()):
        raise cc.CollabError("a human override requires an explicit reason")
    if kind == KIND_AUTONOMOUS and (not isinstance(receipt, str) or not receipt.strip()):
        raise cc.CollabError("an autonomous_verified transition requires a verification receipt")
    return {
        "kind": kind,
        "actor": actor.strip(),
        "reason": reason.strip() if isinstance(reason, str) and reason.strip() else None,
        "receipt": receipt.strip() if isinstance(receipt, str) and receipt.strip() else None,
    }


def write(collab, hid, *, kind, actor, reason=None, receipt=None, candidate_id=None) -> dict:
    """Persist the transition record. Atomic (tmp + ``os.replace``) so a reader never sees a partial."""
    rec = validate(kind=kind, actor=actor, reason=reason, receipt=receipt)
    rec.update({"id": hid, "ts": _now(), "candidate_id": candidate_id, "to": "done"})
    d = _dir(collab)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{hid}.{os.getpid()}.{time.time_ns()}.tmp"
    tmp.write_text(json.dumps(rec, sort_keys=True, indent=2), encoding="utf-8")
    os.replace(tmp, _path(collab, hid))
    return rec


def read(collab, hid) -> dict | None:
    """The transition record, or ``None`` when absent/corrupt. ``None`` is never autonomous."""
    try:
        data = json.loads(_path(collab, hid).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("kind") in KINDS else None


def is_autonomous(rec) -> bool:
    """The ONLY place allowed to say a close was autonomously verified.

    Demands the receipt as well as the kind: ``{"kind": "autonomous_verified"}`` with nothing to point
    at is a claim, not evidence.
    """
    return bool(
        isinstance(rec, dict)
        and rec.get("kind") == KIND_AUTONOMOUS
        and isinstance(rec.get("receipt"), str)
        and rec.get("receipt", "").strip()
    )


def is_human_override(rec) -> bool:
    return bool(isinstance(rec, dict) and rec.get("kind") == KIND_HUMAN)


def label_of(rec) -> str:
    """The words a rendering surface must use. Fail-closed: unknown provenance is not verified."""
    if is_autonomous(rec):
        return LABEL_AUTONOMOUS
    if is_human_override(rec):
        return LABEL_HUMAN
    return LABEL_UNRECORDED


def summary(collab, hid) -> dict:
    """The render-ready provenance block every reporting surface should use."""
    rec = read(collab, hid)
    return {
        "kind": (rec or {}).get("kind"),
        "actor": (rec or {}).get("actor"),
        "ts": (rec or {}).get("ts"),
        "reason": (rec or {}).get("reason"),
        "receipt": (rec or {}).get("receipt"),
        "candidate_id": (rec or {}).get("candidate_id"),
        "autonomous": is_autonomous(rec),
        "human_override": is_human_override(rec),
        "label": label_of(rec),
    }
