"""handoff_core — the handoff state machine + id allocation (collab-kit slice 2).

The first real consumer of the slice-1 substrate (``collab_common``). Honors the composition
constraints declared in handoff 001 / 002:

  [C4]/[C9]  id-uniqueness is committed by ``exclusive_create`` on an ID-ONLY ledger path
             (``.ids/{NNN}.id``), never the slugged filename — so even a broken lock cannot
             duplicate an id (the ``os.link`` no-overwrite arbitrates).
  [C5]       a ``LockBroken`` raised AFTER the reservation commit is success, never retried
             (retrying would double-allocate). Only pre-commit failures retry.
  [C6]       the critical section is short; ``ttl`` is generous and per-call configurable.
  [C10]      state transitions are a single-winner atomic ``os.link`` CAS. (``os.replace`` is NOT
             single-winner on Windows: ``MoveFileExW`` lets N racers all "succeed", so multiple
             agents would believe they exclusively claimed the same handoff. A no-overwrite hard
             link is a true compare-and-swap — exactly one process creates the destination.) The
             directory is the SOLE source of truth; the ``status`` frontmatter records only the
             creation-time state and is deliberately NOT updated on transition — derive the current
             state from the directory (``state_of``), never from the field.

  Crash residual (deliberate, audited): a crash between the id-reservation commit and the content
  write leaves a permanent reserved id with no file (ids are never reused, [C4]/[C9]); surface it
  via ``orphaned_ids``, never via ``list_handoffs`` (the content view), and never auto-recover.

Layout::

    <collab>/handoffs/
      pending/  claimed/  done/  archive/     # {NNN}-{slug}.md content files
      .ids/     {NNN}.id                       # permanent id ledger (single id-allocation source)
      .idlock/                                 # collab_lock dir for id allocation
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from contextlib import suppress
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collab_common as cc
import transitions as _transitions

STATES = ("pending", "claimed", "done", "archive")
_STATE_ORDER = {s: i for i, s in enumerate(STATES)}  # higher = more advanced
_TRANSITIONS = {
    "claim": ("pending", "claimed"),
    "done": ("claimed", "done"),
    "archive": ("done", "archive"),
    # The one BACKWARD edge, and the only way out of ``claimed`` other than ``done``. It exists solely to
    # un-strand an ORPHAN: a handoff whose driver died mid-work, leaving it claimed with nobody working it
    # and nothing selecting it (``_next_root`` scans ``pending`` only). See ``autopilot._reclaim_orphans``
    # for the caller's safety conditions -- this primitive does NOT judge whether a reclaim is warranted.
    "reclaim": ("claimed", "pending"),
}

_ID_PREFIX_RE = re.compile(r"^(\d+)")
_MD_RE = re.compile(r"^(\d+)-(.*)\.md$")
_FENCE = "---"


class HandoffNotFound(cc.CollabError):
    """No content file exists for this handoff id in any state (CLI exit 4)."""


class HandoffConflict(cc.CollabError):
    """The transition cannot proceed — wrong current state, or the move race was lost (CLI exit 3)."""


# --------------------------------------------------------------------------- #
# Layout helpers
# --------------------------------------------------------------------------- #


def _handoffs(collab) -> Path:
    return Path(collab) / "handoffs"


def _state_dir(collab, state: str) -> Path:
    if state not in STATES:
        raise cc.CollabError(f"unknown state {state!r}")
    return _handoffs(collab) / state


def _ids_dir(collab) -> Path:
    return _handoffs(collab) / ".ids"


def ensure_layout(collab) -> None:
    for s in STATES:
        _state_dir(collab, s).mkdir(parents=True, exist_ok=True)
    _ids_dir(collab).mkdir(parents=True, exist_ok=True)


def _next_id(collab) -> int:
    """1 + max numeric id across the ``.ids`` ledger AND the numeric prefixes of existing
    ``{NNN}-*.md`` content files in every state dir. Caller MUST hold the id lock.

    Scanning the content files (not just the ledger) means a hand-made/legacy handoff that
    predates the CLI — e.g. ``pending/001-foo.md`` with no ``.ids/001.id`` — is still counted, so
    the manual→CLI migration cannot re-allocate an id that already labels a file on disk. The
    ``.ids/{NNN}.id`` ``exclusive_create`` reservation ([C4]/[C9]) remains the id-uniqueness
    backstop; this scan only raises the allocation floor, it never weakens the commit.
    """
    mx = 0
    for p in _ids_dir(collab).glob("*.id"):
        m = _ID_PREFIX_RE.match(p.stem)
        if m:
            mx = max(mx, int(m.group(1)))
    for s in STATES:
        for p in _state_dir(collab, s).glob("*.md"):
            m = _MD_RE.match(p.name)
            if m:
                mx = max(mx, int(m.group(1)))
    return mx + 1


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _reject_unsafe_scalar(name: str, value) -> None:
    """Fail closed on frontmatter-injection: a scalar may not carry a newline/CR/control char
    (which would forge another ``key: value`` line, and — the parser being last-write-wins — could
    overwrite ``id``/``status``) nor be the bare ``---`` fence (which would truncate frontmatter).
    Escaping is not viable: the sink is a hand-rolled non-YAML parser that treats quotes literally.
    """
    s = str(value)
    if any(ch in "\r\n" or ord(ch) < 0x20 for ch in s):
        raise cc.CollabError(f"frontmatter field {name!r} contains a newline or control character")
    if s.strip() == _FENCE:
        raise cc.CollabError(f"frontmatter field {name!r} may not be the fence {_FENCE!r}")


_BODY_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s", re.M)
_BODY_DECLARED_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s*\[[^\]]+\]", re.M)


def _reject_unsafe_body(body: str) -> None:
    """Prevent a free-text body from forging document structure — a Markdown heading (which could
    open a ``## Constraints`` section) or a ``- [ID]`` declared-constraint bullet. Both would let a
    body-only attacker fabricate typed constraints and poison identity-addressed ``handoff_loss``
    (§7.4). Structure must come only through the typed ``constraints=`` param.
    """
    if not body:
        return
    if _BODY_HEADING_RE.search(body):
        raise cc.CollabError("handoff body may not open a Markdown heading ('#'…); use typed fields")
    if _BODY_DECLARED_RE.search(body):
        raise cc.CollabError(
            "handoff body may not contain declared-constraint bullets '- [ID]'; use constraints="
        )


def render_handoff(
    *, to, from_, hid, title, priority, date, status="pending", body="", constraints=None
) -> str:
    """Render a typed handoff artifact (frontmatter + Summary [+ Constraints]). See contracts.py.

    Interpolated frontmatter scalars are validated (``_reject_unsafe_scalar``) and the body is
    checked for structure injection (``_reject_unsafe_body``), so an attacker-controlled ``title``
    or ``body`` cannot forge frontmatter keys, truncate the block, or fake a Constraints section.
    """
    for _n, _v in (
        ("to", to),
        ("from", from_),
        ("id", hid),
        ("title", title),
        ("priority", priority),
        ("date", date),
        ("status", status),
    ):
        _reject_unsafe_scalar(_n, _v)
    _reject_unsafe_body(body)
    fm = "\n".join(
        [
            "---",
            f"to: {to}",
            f"from: {from_}",
            f"id: {hid}",
            f"title: {title}",
            f"priority: {priority}",
            f"date: {date}",
            f"status: {status}",
            "---",
        ]
    )
    parts = [fm, "", f"## Summary\n\n{(body.strip() or title)}\n"]
    if constraints:
        lines = "\n".join(f"- [{cid}] {txt}" for cid, txt in constraints)
        parts.append(f"## Constraints\n\n{lines}\n")
    return "\n".join(parts)


# --------------------------------------------------------------------------- #
# create — the constraint-critical path
# --------------------------------------------------------------------------- #


def create(
    collab,
    *,
    to,
    from_,
    title,
    priority="normal",
    date=None,
    body="",
    constraints=None,
    id_width=3,
    ttl=30.0,
    acquire_timeout=60.0,
) -> dict:
    """Allocate the next id and write a new handoff into ``pending/``.

    Honors [C4]/[C5]/[C6]/[C9]: id reserved on an id-only ledger path via ``exclusive_create``
    under a short-held lock; pre-commit id collisions retry; a post-commit ``LockBroken`` is
    treated as success and never retried.

    Returns ``{"id", "slug", "path", "state"}``.
    """
    ensure_layout(collab)
    date = date or time.strftime("%Y-%m-%d", time.gmtime())
    # Validate attacker-controlled inputs BEFORE consuming an id, so a rejected injection never
    # leaks an orphan reservation (verification-lane finding). render_handoff re-validates (defense
    # in depth). slugify(title) also runs here — an unsluggable title fails before any id is burned.
    for _n, _v in (("to", to), ("from", from_), ("title", title), ("priority", priority), ("date", date)):
        _reject_unsafe_scalar(_n, _v)
    _reject_unsafe_body(body)
    slug = cc.slugify(title)
    lockdir = _handoffs(collab) / ".idlock"
    committed: dict | None = None

    while committed is None:
        try:
            with cc.collab_lock(lockdir, ttl=ttl, acquire_timeout=acquire_timeout) as h:
                nid = _next_id(collab)
                hid = f"{nid:0{id_width}d}"
                h.assert_current()  # fence before the reservation commit ([C6])
                sentinel = _ids_dir(collab) / f"{hid}.id"
                try:
                    cc.exclusive_create(sentinel, hid + "\n")  # [C4]/[C9] id-only atomic commit
                except FileExistsError:
                    continue  # pre-commit id collision -> recompute + retry (retry-safe, [C5])
                # id is ours. Write the slugged content file (also atomic, unique id => no clash).
                final = _state_dir(collab, "pending") / f"{hid}-{slug}.md"
                content = render_handoff(
                    to=to,
                    from_=from_,
                    hid=f"{hid}-{slug}",
                    title=title,
                    priority=priority,
                    date=date,
                    body=body,
                    constraints=constraints,
                )
                cc.exclusive_create(final, content)
                committed = {"id": hid, "slug": slug, "path": str(final), "state": "pending"}
        except cc.LockBroken:
            if committed is not None:
                break  # [C5] the reservation already committed -> do NOT retry
            continue  # broken before any commit -> safe to retry

    return committed


# --------------------------------------------------------------------------- #
# transitions — single atomic rename ([C10])
# --------------------------------------------------------------------------- #


def _find(collab, hid, states=STATES):
    for s in states:
        for p in _state_dir(collab, s).glob(f"{hid}-*.md"):
            return s, p
    return None, None


def _same_file(a: Path, b: Path) -> bool:
    """True iff a and b are the SAME on-disk file (hard links to one inode/file-id)."""
    try:
        sa, sb = a.stat(), b.stat()
    except FileNotFoundError:
        return False
    return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)


def _find_all(collab, hid) -> list:
    out = []
    for s in STATES:
        for p in _state_dir(collab, s).glob(f"{hid}-*.md"):
            out.append((s, p))
    return out


def _reconcile(collab, hid):
    """Authoritative (state, path) for hid, healing transition crash residuals ([C10]).

    A crash between ``os.link`` and ``os.unlink`` can leave hid hard-linked in two state dirs.
    The MOST-ADVANCED state is authoritative; stale same-inode links to less-advanced states are
    unlinked (content survives in the advanced state — no data loss), completing the interrupted
    transition. Two same-id files that are NOT the same inode are a corruption signal and raise.
    Returns ``(None, None)`` if hid has no content file.
    """
    matches = _find_all(collab, hid)
    if not matches:
        return (None, None)
    if len(matches) == 1:
        return matches[0]
    matches.sort(key=lambda sp: _STATE_ORDER[sp[0]])
    best_state, best_path = matches[-1]
    for s, p in matches[:-1]:
        if _same_file(p, best_path):
            with suppress(FileNotFoundError):
                os.unlink(p)  # heal: drop the stale, less-advanced link
        else:
            raise cc.CollabError(
                f"handoff {hid} present in {s!r} and {best_state!r} as DIFFERENT files (corruption)"
            )
    return (best_state, best_path)


def _transition(collab, hid, action) -> dict:
    frm, to = _TRANSITIONS[action]
    _reconcile(collab, hid)  # heal any transition-crash residual before deciding the source state
    _, src = _find(collab, hid, states=(frm,))
    if src is None:
        cur_state, _ = _reconcile(collab, hid)
        if cur_state is None:
            raise HandoffNotFound(f"handoff {hid} not found")
        raise HandoffConflict(f"handoff {hid} is in {cur_state!r}, not {frm!r} (cannot {action})")
    dst = _state_dir(collab, to) / src.name
    # The destination dir can legitimately be absent: a state dir disappears once its last handoff moves
    # out (an empty dir is also untrackable by git, so a clone never has one). Without this, os.link
    # raises FileNotFoundError for the MISSING PARENT and the handler below reports it as "lost the race"
    # -- a real, blocking failure disguised as a benign concurrency outcome. Creating the dir does not
    # weaken the CAS: the link below is still the single-winner operation.
    with suppress(OSError):
        dst.parent.mkdir(parents=True, exist_ok=True)
    # [C10] os.replace is NOT single-winner on Windows (MoveFileExW lets N racers all succeed).
    # A no-overwrite hard link IS a true CAS: exactly one process creates dst.
    try:
        os.link(src, dst)
    except FileNotFoundError:
        # src vanished under us -- a genuine lost race (the dst dir is guaranteed to exist by now).
        raise HandoffConflict(f"handoff {hid} already moved (lost the {action} race)") from None
    except FileExistsError:
        # dst already exists. If it's the SAME file as src (a crashed-winner residual, or a
        # concurrent loser whose winner linked the same inode), complete the move by cleaning up
        # src — but do NOT become a winner. This preserves single-winner AND heals the residual.
        if _same_file(src, dst):
            with suppress(FileNotFoundError):
                os.unlink(src)
        raise HandoffConflict(f"handoff {hid} already moved (lost the {action} race)") from None
    with suppress(FileNotFoundError):
        os.unlink(src)
    return {"id": hid, "from": frm, "to": to, "path": str(dst)}


def claim(collab, hid) -> dict:
    return _transition(collab, hid, "claim")


def reclaim(collab, hid) -> dict:
    """Move an ORPHANED handoff ``claimed -> pending`` so it can be selected again.

    Backward, unlike every other transition, which makes the crash-residual healing in :func:`_reconcile`
    read oddly here and is worth stating: ``_reconcile`` treats the MOST-ADVANCED state as authoritative,
    so a crash between the ``os.link`` and the ``os.unlink`` leaves the file in both dirs and heals to
    ``claimed`` -- i.e. the reclaim is silently undone rather than half-applied. That is safe: the caller
    re-runs the reclaim on the next start and it converges. Never data loss, only a retry.

    This is a mechanical move with NO policy: it does not know whether the handoff is genuinely orphaned
    or deliberately parked for a human. The caller owns that judgement (``autopilot._reclaim_orphans``).
    """
    return _transition(collab, hid, "reclaim")


def done(collab, hid, *, kind, actor, reason=None, receipt=None, candidate_id=None) -> dict:
    """Close a handoff, ON THE RECORD. ``kind`` is mandatory: no caller may close anonymously.

    ``kind=transitions.KIND_AUTONOMOUS`` requires a ``receipt`` (the done-contract hash); ``KIND_HUMAN``
    requires an explicit ``reason``. See :mod:`transitions`.

    **Why the signature changed (2026-07-15 audit).** ``done(collab, hid)`` took no provenance and wrote
    none, and the directory-is-the-state design ([C10], see module docstring) means the closed file is
    byte-identical either way. Three of four production callers reached ``done/`` with no contract at all
    -- ``dashboard_core.advance_handoff`` (a 2-field HTTP POST that also auto-claims, so it could close
    work never built) and ``handoff_cli.cmd_done``. Provenance survived only as a best-effort trace line,
    so a dropped emit made a human click read as an autonomous close. Requiring ``kind`` here, at the CAS
    every path funnels through, is what makes that unrepresentable rather than merely discouraged.

    Validation runs BEFORE the transition, so an unlabelled close never moves a file. The record is
    written AFTER the CAS commits: a crash in between leaves a ``done`` handoff whose provenance reads
    ``UNRECORDED``, which is never treated as verified.
    """
    _transitions.validate(kind=kind, actor=actor, reason=reason, receipt=receipt)
    out = _transition(collab, hid, "done")
    _transitions.write(
        collab, hid, kind=kind, actor=actor, reason=reason, receipt=receipt, candidate_id=candidate_id
    )
    return out


def archive(collab, hid) -> dict:
    return _transition(collab, hid, "archive")


# --------------------------------------------------------------------------- #
# read-only views
# --------------------------------------------------------------------------- #


def list_handoffs(collab, state=None) -> list[dict]:
    """Handoffs, deduplicated to the authoritative (most-advanced) state ([C10]).

    Always scans all state dirs so a transition-crash residual (same id hard-linked in two dirs)
    is reported ONCE, at its most-advanced state — never as two rows and never at a stale less-
    advanced state. Read-only: it does not heal the residual (``state_of``/transitions do); it just
    refuses to report it twice. ``state`` filters the deduplicated result.
    """
    by_id: dict[str, dict] = {}
    for s in STATES:
        d = _state_dir(collab, s)
        if not d.exists():
            continue
        for p in sorted(d.glob("*.md")):
            m = _MD_RE.match(p.name)
            if not m:
                continue
            hid = m.group(1)
            prev = by_id.get(hid)
            if prev is None or _STATE_ORDER[s] > _STATE_ORDER[prev["state"]]:
                by_id[hid] = {"id": hid, "slug": m.group(2), "state": s, "path": str(p)}
    out = [h for h in by_id.values() if state is None or h["state"] == state]
    return sorted(out, key=lambda h: h["id"])


def show(collab, hid) -> str:
    _, p = _reconcile(collab, hid)
    if p is None:
        raise HandoffNotFound(f"handoff {hid} not found")
    return p.read_text(encoding="utf-8")


def state_of(collab, hid) -> str | None:
    """Authoritative current state of a handoff, derived from the DIRECTORY ([C10]).

    The `status` frontmatter field is creation-time metadata only and must not be trusted as
    current state; this is the correct way to ask "where is handoff N?". Heals any transition-crash
    residual (reports the most-advanced state). Returns ``None`` if the handoff has no content file
    (e.g. an orphaned reserved id — see ``orphaned_ids``).
    """
    return _reconcile(collab, hid)[0]


def orphaned_ids(collab) -> list[str]:
    """Reserved ledger ids (``.ids/{NNN}.id``) with no content file in any state ([C9] audit).

    A crash between the id-reservation commit and the content write leaves a PERMANENT reserved id
    with no ``{NNN}-*.md``. Ids are never reused, so such gaps are the expected residual — surfaced
    here for audit, never through ``list_handoffs`` (the content view) and never auto-recovered
    (a recovering ``create`` carries different content and would forge a handoff under the orphan's
    id). Returns the sorted zero-padded ids.
    """
    have_content = {h["id"] for h in list_handoffs(collab)}
    ids = _ids_dir(collab)
    if not ids.exists():
        return []
    out = []
    for p in ids.glob("*.id"):
        m = _ID_PREFIX_RE.match(p.stem)
        if m and p.stem not in have_content:
            out.append(p.stem)
    return sorted(out)


# --------------------------------------------------------------------------- #
# ActiveHandoffLease — the board-level exclusive control lease (ADR-0003 D2)
# --------------------------------------------------------------------------- #
#
# `claim` is a PER-HANDOFF os.link CAS: it guarantees exactly one winner for ONE handoff, but it
# does NOT stop two driver processes from each claiming a DIFFERENT handoff and running in parallel.
# The one-handoff-at-a-time invariant (ADR-0001) was therefore only emergent — a property of the
# single-driver loop shape, not an enforced lock. The lease makes it real: a run acquires the board
# BEFORE it claims any handoff and holds it (renewed by the driver heartbeat) until done / a terminal
# pause / reopen. A second run cannot acquire while a LIVE lease is held; a STALE lease (heartbeat
# past its TTL — a crashed driver) is reclaimable, and every reclaim is audited.

_LEASE_TTL_S = 90.0  # a lease whose heartbeat is older than this is stale and reclaimable


class LeaseHeld(cc.CollabError):
    """The board lease is held by another live run — this run may not select or claim work."""

    def __init__(self, holder: dict) -> None:
        super().__init__(
            f"board lease held by run {holder.get('run_uid')!r} on handoff {holder.get('hid')!r} "
            f"(pid {holder.get('pid')}) — refusing to start a second concurrent driver"
        )
        self.holder = holder


def _lease_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class ActiveHandoffLease:
    """One run's grip on the board. Every acquire/renew/release is serialised by the cross-process
    ``collab_lock`` and persisted atomically to ``autopilot/active.lease``.

    ``now`` (a ``time.time``-style float clock) is injectable so staleness is deterministic in tests.
    """

    def __init__(
        self, collab, run_uid: str, *, pid: int | None = None, ttl: float = _LEASE_TTL_S, now=time.time
    ) -> None:
        self._collab = Path(collab)
        self._run_uid = str(run_uid)
        self._pid = int(pid) if pid is not None else os.getpid()
        self._ttl = float(ttl)
        self._now = now
        self._path = self._collab / "autopilot" / "active.lease"
        self._lockdir = self._collab / "autopilot" / ".leaselock"
        self._audit_path = self._collab / "autopilot" / "lease-audit.jsonl"

    # ---- public API ------------------------------------------------------- #
    def acquire(self, hid: str) -> dict:
        """Acquire (or renew) the board for ``hid``. Raises ``LeaseHeld`` if a live foreign lease
        exists, or ``CollabError`` if this run already holds the board for a DIFFERENT handoff."""
        self._ensure()
        with cc.collab_lock(self._lockdir):
            rec = self._read()
            if rec is not None and rec.get("run_uid") != self._run_uid:
                if not self._is_stale(rec):
                    raise LeaseHeld(rec)
                self._audit("reclaim_stale", reclaimed_from=rec)  # crashed prior run — take the board
            elif rec is not None and rec.get("run_uid") == self._run_uid and rec.get("hid") != hid:
                raise cc.CollabError(
                    f"run {self._run_uid!r} already holds the board lease for {rec.get('hid')!r}; "
                    f"cannot claim {hid!r} too — release the current slice first (one board-level "
                    f"claimed handoff, ADR-0003 D2)"
                )
            now = float(self._now())
            ts = _lease_ts()
            new = {
                "run_uid": self._run_uid,
                "hid": hid,
                "pid": self._pid,
                "acquired_ts": ts,
                "acquired_epoch": now,
                "heartbeat_ts": ts,
                "heartbeat_epoch": now,
            }
            # A re-acquire of the SAME slice by the SAME run preserves the original acquire time.
            if rec is not None and rec.get("run_uid") == self._run_uid and rec.get("hid") == hid:
                new["acquired_ts"] = rec.get("acquired_ts", ts)
                new["acquired_epoch"] = rec.get("acquired_epoch", now)
            self._write(new)
            return new

    def renew(self) -> bool:
        """Refresh the heartbeat iff this run still holds the lease. Returns False if it was lost."""
        self._ensure()
        with cc.collab_lock(self._lockdir):
            rec = self._read()
            if not rec or rec.get("run_uid") != self._run_uid:
                return False
            rec["heartbeat_ts"] = _lease_ts()
            rec["heartbeat_epoch"] = float(self._now())
            self._write(rec)
            return True

    def release(self) -> bool:
        """Release the board iff this run holds it. Idempotent; returns False if it did not hold it."""
        self._ensure()
        with cc.collab_lock(self._lockdir):
            rec = self._read()
            if rec and rec.get("run_uid") == self._run_uid:
                with suppress(FileNotFoundError):
                    self._path.unlink()
                self._audit("release", hid=rec.get("hid"))
                return True
            return False

    def holder(self) -> dict | None:
        """The current lease record (no lock — a diagnostic snapshot), or None if the board is free."""
        return self._read()

    # ---- internals -------------------------------------------------------- #
    def _ensure(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _is_stale(self, rec: dict) -> bool:
        hb = rec.get("heartbeat_epoch")
        if hb is None:
            return True  # a record with no heartbeat can never prove liveness
        return (float(self._now()) - float(hb)) >= self._ttl

    def _read(self) -> dict | None:
        try:
            data = json.loads(self._path.read_text("utf-8"))
        except FileNotFoundError:
            return None
        except OSError, ValueError:
            # A torn lease cannot be trusted to prove a live holder; treat as reclaimable rather than
            # deadlocking the board forever. safe_write makes this near-impossible in practice.
            return None
        return data if isinstance(data, dict) else None

    def _write(self, rec: dict) -> None:
        cc.safe_write(self._path, json.dumps(rec, indent=2, sort_keys=True) + "\n")

    def _audit(self, event: str, **extra) -> None:
        rec = {"ts": _lease_ts(), "event": event, "run_uid": self._run_uid, "pid": self._pid, **extra}
        prior = self._audit_path.read_text("utf-8") if self._audit_path.exists() else ""
        cc.safe_write(self._audit_path, prior + json.dumps(rec, sort_keys=True) + "\n")
