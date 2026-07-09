"""watcher — persistent handoff watcher (collab-kit slice 4, architecture §A6).

Polls a collab's ``handoffs/pending/`` and surfaces a new handoff *addressed to this seat* the
moment it appears, so a reviewer is pinged when the builder requests review (and vice versa).
stdlib only, polling (no inotify/ReadDirectoryChangesW — they differ per OS, [C22]).

Guarantees:
  [C20] read-only over ``handoffs/`` — never mutates handoff state; only writes its own seen-set.
  [C21] the seen-set is persisted (``logs/watch-<seat>.state``, atomic ``safe_write``) so a restart
        never re-announces a handoff it already surfaced.
  [C23] surfaces a handoff only when frontmatter ``to:`` matches this watcher's seat.
  [C24] a malformed/unreadable file in ``pending/`` never crashes the loop — warn once, keep going.
  [C25] one parameterized implementation (``--seat``); ``watch-for-<seat>`` names are thin wrappers.

Explicit semantics (production contract):
  * **At-least-once delivery (B3).** ``_persist_merge`` (merge-under-lock + fence) guarantees no
    lost seen-state and **no re-announcement across restart** ([C21]). It does NOT guarantee
    exactly-once: two *concurrently live* same-seat watchers may each announce the same new handoff
    once (duplicate notification) before either persists. Run one watcher per seat to avoid dupes;
    duplicates are harmless (a notification, not a state change).
  * **Routing is immutable (B5).** A handoff's ``to:`` (and ``id``) is fixed at creation — content
    (summary/constraints/body) may be edited, routing may not. The watcher records non-matching
    handoffs in its seen-set, so *re-addressing* a handoff after creation is unsupported: to reach a
    different seat, create a NEW handoff (the id ledger guarantees a fresh id).
  * **Only regular files are parsed (B4):** symlinks/FIFOs/devices/oversized files in ``pending/``
    are skipped (warn once), never read.
  * **Corrupt seen-state self-heals (B2):** an unreadable state file is quarantined and the backlog
    re-seeded (not re-announced).
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import stat
import sys
import time
from pathlib import Path

#: A handoff is prose — never megabytes. Files above this are skipped (huge-file DoS guard).
_MAX_HANDOFF_BYTES = 512 * 1024

_LIB = str(Path(__file__).resolve().parent)
sys.path.insert(0, _LIB)
import collab_common as cc  # noqa: E402
import contracts  # noqa: E402
import handoff_core as hc  # noqa: E402
import registry  # noqa: E402


def _load_local(alias: str, filename: str):
    spec = importlib.util.spec_from_file_location(alias, Path(_LIB) / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_trace = _load_local("collab_trace", "trace.py")


def _emit_safe(fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except Exception as e:  # telemetry is observability, never fail the watch loop on it
        print(f"warning: watch telemetry emit failed: {e}", file=sys.stderr)


def _state_path(collab, seat: str) -> Path:
    return Path(collab) / "logs" / f"watch-{cc.slugify(seat)}.state"


def _load_seen(state: Path) -> set:
    try:
        return set(state.read_text("utf-8").split())
    except (FileNotFoundError, OSError, ValueError):
        return set()


def _read_state(state: Path) -> tuple:
    """Read the seen-set, distinguishing **absent** from **corrupt** so recovery is fail-safe (B2).

    Returns ``(seen, corrupt)``. A non-utf-8/unreadable state file is ``corrupt=True`` (the caller
    quarantines it and re-seeds the backlog rather than re-announcing it — a bare empty read would
    make ``watch`` skip cold-start seeding and re-announce everything, violating [C21])."""
    try:
        return set(state.read_text("utf-8").split()), False
    except FileNotFoundError:
        return set(), False
    except (OSError, ValueError):
        return set(), True


def _quarantine(state: Path) -> None:
    """Move a corrupt state file aside (kept for inspection) so the watcher can start fresh."""
    try:
        state.replace(state.with_name(f"{state.name}.corrupt.{time.time_ns()}"))
    except OSError:
        pass


def _save_seen(state: Path, seen: set) -> None:
    state.parent.mkdir(parents=True, exist_ok=True)
    cc.safe_write(state, "\n".join(sorted(seen)) + "\n")  # atomic ([C21])


def _lockdir(state: Path) -> Path:
    return state.with_name(state.name + ".lock")


def _persist_merge(state: Path, seen: set) -> set:
    """Persist the seen-set as a MERGE with whatever is on disk, under a per-seat lock ([C21]).

    ``_save_seen`` alone writes the whole in-memory set, so two concurrent same-seat watchers would
    lost-update each other (one clobbers the other's announced ids -> re-announce on restart). Taking
    the lock and re-reading+unioning closes that. Returns the merged set (adopt it in-memory)."""
    state.parent.mkdir(parents=True, exist_ok=True)  # logs/ must exist before the lockdir mkdir
    with cc.collab_lock(_lockdir(state), ttl=10.0, acquire_timeout=30.0) as h:
        merged = _load_seen(state) | seen
        h.assert_current()  # fence before the commit (slice-1 approval-bar invariant, B1)
        _save_seen(state, merged)
    return merged


def _tick(collab, seat: str, seen: set, warned: set, *, state: Path, log: str | None) -> list:
    """One poll: announce new pending handoffs addressed to ``seat``. Returns newly-announced ids."""
    seen |= _load_seen(state)  # refresh from disk: see other watchers' saves (reduces double-announce)
    seat_key = seat.strip().casefold()
    announced = []
    grew = False
    for h in hc.list_handoffs(collab, "pending"):  # read-only ([C20])
        hid = h["id"]
        if hid in seen:
            continue
        p = Path(h["path"])
        try:
            st = p.lstat()  # lstat: do NOT follow a symlink
        except OSError:
            continue
        # Only parse REGULAR files: rejects symlinks (info-disclosure), FIFOs/devices/sockets (can
        # hang read_text), and dirs. A handoff is always a plain file (B4).
        if not stat.S_ISREG(st.st_mode):
            if hid not in warned:
                print(f"[watch:{seat}] warning: skipping non-regular file in pending/: {p}", file=sys.stderr)
                warned.add(hid)
            continue
        if st.st_size > _MAX_HANDOFF_BYTES:  # huge-file DoS guard (B4)
            if hid not in warned:
                print(f"[watch:{seat}] warning: skipping oversized pending file ({st.st_size} bytes): {p}", file=sys.stderr)
                warned.add(hid)
            continue
        try:
            obj = contracts.parse_handoff(p)
        except Exception as e:  # [C24] never crash on a bad file; warn once (re-tried next tick)
            if hid not in warned:
                print(f"[watch:{seat}] warning: could not read {p}: {e}", file=sys.stderr)
                warned.add(hid)
            continue
        fm = obj.get("frontmatter") or {}
        # [C23] case/space-insensitive seat match, so `to: Reviewer` isn't silently dropped for `reviewer`
        if (fm.get("to") or "").strip().casefold() != seat_key:
            seen.add(hid)  # not ours — remember so we don't re-parse (persisted below)
            grew = True
            continue
        title = (fm.get("title") or "").strip().strip('"')
        print(f"[watch:{seat}] NEW handoff {hid} for you: {title!r} (from {fm.get('from')!r}, priority {fm.get('priority')})")
        if log:
            _emit_safe(_trace.emit, log, run_id=f"watch-{seat}", stage="review", role=seat,
                       artifact=f"handoff:{hid}", span_id=f"{hid}:watch",
                       decision={"action": "route", "reason_codes": ["watch:new-handoff"]})
        seen.add(hid)
        announced.append(hid)
        grew = True
    if grew:
        seen |= _persist_merge(state, seen)  # merge-under-lock; persists announced AND non-matching ([C21])
    return announced


def _inbox_dir(home, project: str) -> Path:
    return Path(home) / "inbox" / "live" / cc.slugify(project)


def _inbox_lockdir(home, project: str) -> Path:
    return _inbox_dir(home, project) / ".drain.lock"


#: An inbox message is one line of prose (the bridge caps Telegram bodies at 4000 chars), but a local
#: process could drop a huge ``from-user-*.md`` here — cap the read like ``_tick`` caps ``pending/``.
_MAX_INBOX_BYTES = 64 * 1024


def drain_inbox(home, project: str, *, seat: str, log: str | None = None) -> list:
    """Surface + CONSUME the Telegram->agent messages the bridge left in ``inbox/live/<project>/`` (§A6).

    **Single-consumer, at-least-once.** Because this is the human-control channel, the drain runs under a
    per-project lock (``.drain.lock``) with an ``assert_current`` fence before the archive move (the
    slice-1 approval-bar invariant), so two concurrently-live watchers do NOT double-surface a message in
    the normal case — one drains while the other skips this tick. It is NOT exactly-once across a crash:
    a crash after printing but before the move re-surfaces that one message on the next drain — the same
    at-least-once contract as handoff watching (a repeated notification, never a state change).

    Only *regular* queue files are consumed (``lstat`` + ``S_ISREG`` reject symlinks/FIFOs/devices), and a
    file over ``_MAX_INBOX_BYTES`` is skipped (huge-file guard). Returns the drained file paths.
    """
    d = _inbox_dir(home, project)
    if not d.exists():
        return []
    if not any(d.glob("from-user-*.md")):  # cheap idle check: don't take the drain lock on an empty inbox
        return []
    archive = d / "archive"
    drained: list = []
    try:
        # Short acquire_timeout: if a peer watcher already holds the drain lock it is consuming — skip
        # this tick rather than stall the whole watch loop (single-consumer, no double-surface).
        with cc.collab_lock(_inbox_lockdir(home, project), ttl=10.0, acquire_timeout=2.0) as h:
            for p in sorted(d.glob("from-user-*.md")):
                try:
                    st = p.lstat()  # lstat: never follow a symlink
                    if not stat.S_ISREG(st.st_mode):  # only regular queue files (no symlink/FIFO/dir)
                        continue
                    if st.st_size > _MAX_INBOX_BYTES:  # huge-file guard (a local process could drop MBs)
                        print(f"[watch:{seat}] warning: skipping oversized inbox file ({st.st_size} bytes): {p}", file=sys.stderr)
                        continue
                    body = p.read_text("utf-8").strip()
                except (OSError, ValueError):
                    continue
                print(f"[watch:{seat}] MESSAGE from you ({cc.slugify(project)}): {body}")
                if log:
                    _emit_safe(_trace.emit, log, run_id=f"watch-{seat}", stage="review", role="human",
                               artifact=f"inbox:{p.name}", span_id=p.name,
                               decision={"action": "route", "reason_codes": ["inbox:user-message"]})
                try:
                    archive.mkdir(parents=True, exist_ok=True)
                    h.assert_current()  # fence before the consume (approval-bar invariant): still the owner
                    os.replace(p, archive / p.name)  # consume under the lock -> single-consumer
                except (OSError, cc.LockBroken):
                    pass  # lost the lock or move failed: leave the file -> re-surfaced next drain (at-least-once)
                drained.append(str(p))
    except (cc.LockTimeout, cc.LockBroken):
        return drained  # a peer watcher holds/broke the drain lock and will consume — skip this tick
    return drained


def watch(collab, *, seat: str, interval: float = 2.0, once: bool = False, catch_up: bool = False,
          log: str | None = None, project: str | None = None, home=None) -> list:
    """Watch ``collab``'s pending/ for handoffs addressed to ``seat``.

    Cold start (no prior state): unless ``catch_up``, seed the seen-set with the existing pending
    backlog so only genuinely-new arrivals are announced. ``once`` runs a single tick (testable).
    """
    state = _state_path(collab, seat)
    seen, corrupt = _read_state(state)
    if corrupt:
        print(f"[watch:{seat}] warning: seen-state unreadable/corrupt; quarantining and re-seeding: {state}", file=sys.stderr)
        _quarantine(state)
    warned: set = set()
    # Cold start OR corrupt-recovery: seed the backlog into seen (don't announce it), so a corrupt
    # state file recovers by re-seeding rather than re-announcing every pending handoff (B2, [C21]).
    if (corrupt or not state.exists()) and not catch_up:
        seen |= {h["id"] for h in hc.list_handoffs(collab, "pending")}
        seen |= _persist_merge(state, seen)
    proj = project or Path(collab).name  # inbox is keyed by the project (collab) name
    try:
        inbox_home = str(home) if home is not None else str(cc.resolve_collab_home())
    except cc.CollabError:
        inbox_home = None
    all_new = []
    while True:
        all_new += _tick(collab, seat, seen, warned, state=state, log=log)
        if inbox_home is not None:
            drain_inbox(inbox_home, proj, seat=seat, log=log)  # §A6: surface Telegram->agent messages
        if once:
            break
        time.sleep(interval)
    return all_new


def watch_all(*, seat: str, interval: float = 2.0, once: bool = False, home=None,
              catch_up: bool = False) -> dict:
    """Cross-collab watch: one tick per registered collab — announce new handoffs AND drain each
    collab's Telegram inbox (``inbox/live/<name>/``, §A6). Returns ``{name: [new handoff ids]}``.
    """
    out: dict = {}
    while True:
        h = str(home) if home is not None else str(cc.resolve_collab_home())  # for inbox paths
        for name, ent in sorted(registry.load(h)["collabs"].items()):
            root = ent["root"]
            if not Path(root).is_dir():  # skip a missing/non-dir root cleanly — don't materialize it
                print(f"[watch-all:{seat}] skip {name}: root not found or not a directory: {root}", file=sys.stderr)
                continue
            state = _state_path(root, seat)
            try:
                seen, corrupt = _read_state(state)
                if corrupt:
                    print(f"[watch-all:{seat}] {name}: seen-state corrupt; quarantining", file=sys.stderr)
                    _quarantine(state)
                if (corrupt or not state.exists()) and not catch_up:
                    seen |= {h["id"] for h in hc.list_handoffs(root, "pending")}
                    seen |= _persist_merge(state, seen)
                new = _tick(root, seat, seen, set(), state=state, log=None)
                drain_inbox(h, name, seat=seat, log=None)  # §A6 inbox drain, keyed by registered name
            except (cc.CollabError, OSError) as e:  # a bad/unreadable collab must not sink the sweep
                print(f"[watch-all:{seat}] skip {name}: {e}", file=sys.stderr)
                new = []
            if new:
                out.setdefault(name, []).extend(new)
        if once:
            break
        time.sleep(interval)
    return out


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="watch", description="collab-kit handoff watcher")
    p.add_argument("--seat", required=True, help="the seat this watcher belongs to (e.g. reviewer, builder)")
    p.add_argument("--collab", help="collab path (single-collab watch); omit with --all")
    p.add_argument("--all", action="store_true", help="watch every registered collab")
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--once", action="store_true", help="single tick then exit (for scripts/tests)")
    p.add_argument("--catch-up", action="store_true", dest="catch_up", help="also announce the existing backlog")
    p.add_argument("--log", help="optional JSONL trace path")
    return p


def main(argv=None) -> int:
    args = _build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.all:
            watch_all(seat=args.seat, interval=args.interval, once=args.once, catch_up=args.catch_up)
        elif args.collab:
            watch(args.collab, seat=args.seat, interval=args.interval, once=args.once,
                  catch_up=args.catch_up, log=args.log)
        else:
            print("watch: pass --collab <path> or --all", file=sys.stderr)
            return 1
    except cc.CollabError as e:  # e.g. a corrupt registry -> clean message, not a raw traceback
        print(f"watch: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
