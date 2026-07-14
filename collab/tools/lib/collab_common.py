r"""collab_common — foundational path/IO/lock primitives for collab-kit (slice 1).

Single source of truth for: path resolution, ``slugify`` (path-traversal defense),
atomic/safe/exclusive writes, and a race-safe, **fenced** mkdir lock. stdlib only.

The companion ``collab_common.sh`` is intentionally thin: it performs the *same* path
resolution for the bash shim and MUST NOT reimplement locking or atomic IO — those live
here so there is exactly one implementation of the concurrency-critical logic.

Approval-bar invariant (handoff 001, rev 3)
-------------------------------------------
A process may perform a lock-protected *final mutation* only if it currently holds the
canonical lock directory AND ``<lockdir>/meta.json``'s ``owner_token`` matches its
``LockHandle.owner_token``. Enforce with ``handle.assert_current()`` immediately before the
mutation. Release and stale-breaking are **rename-capture** operations: a process only ever
destroys a directory it has atomically captured to a private name and then confirmed is its
own — so a resurrected stale owner can never delete a newer owner's live lock in place
(closes the release/break TOCTOU found in adversarial verification).

Path-resolution names (Amendment B)
-----------------------------------
- ``SCRIPT_PATH`` — resolved real path of the running script (symlinks followed).
- ``SCRIPT_DIR``  — ``SCRIPT_PATH.parent``.
- ``KIT_ROOT``    — collab-kit repo root, found by walking up for the root signature
  (a dir containing both ``tools/`` and ``install.sh``, or a ``.collab-kit`` marker).
- ``TOOLS_DIR``   — ``KIT_ROOT / "tools"``.

On Windows, an MSYS/Cygwin ``/c/...`` path handed to native Python (e.g. inherited from a
Git-Bash shim) is normalized to ``C:/...`` before resolution, so bash-shim and direct-Python
entry points compute the *same* canonical directory rather than a bogus ``C:\c\...`` split.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import time
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("collab_common")

# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #


class CollabError(Exception):
    """Base for all collab-kit errors raised by this module."""


class LockTimeout(CollabError):
    """Raised when a lock could not be acquired within ``acquire_timeout``."""


class LockBroken(CollabError):
    """Raised when the canonical lock no longer carries this handle's token.

    Signals either that our lock was broken (our critical section ran past ``ttl``
    and someone declared us stale) or that a different owner now holds the lock.
    On release this means: we removed nothing that belongs to another owner.
    """


# --------------------------------------------------------------------------- #
# Path resolution (Amendment B)
# --------------------------------------------------------------------------- #

_MAX_WALK_LEVELS = 6
_MSYS_DRIVE = re.compile(r"^/([A-Za-z])(/.*)?$")

#: Windows reserved device names that must never become a bare path component.
_WIN_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


def _normalize_msys_path(raw: str) -> str:
    r"""On Windows, turn an MSYS ``/c/Users/..`` path into ``C:/Users/..``.

    Native ``Path('/c/Users').resolve()`` yields drive-relative garbage ``C:\c\Users``;
    normalizing first is what keeps a Git-Bash-provided ``COLLAB_HOME`` and a direct-Python
    ``COLLAB_HOME`` pointing at the *same* lock tree.
    """
    if os.name == "nt":
        m = _MSYS_DRIVE.match(raw)
        if m:
            return f"{m.group(1)}:{m.group(2) or '/'}"
    return raw


def _script_dir() -> Path:
    """SCRIPT_DIR for this module (symlinks resolved)."""
    return Path(__file__).resolve().parent


def _looks_like_kit_root(d: Path) -> bool:
    if (d / ".collab-kit").exists():
        return True
    return (d / "tools").is_dir() and (d / "install.sh").exists()


def resolve_kit_root(start: Path | None = None) -> Path:
    """Locate the collab-kit repo root.

    Order: ``$COLLAB_KIT_ROOT`` env override (what ``install.sh`` embeds — the robust path
    on Windows, where ``ln -s`` is unreliable and runtime symlink resolution cannot be
    trusted) → else walk upward from the running script for the root *signature* (a dir with
    ``tools/`` + ``install.sh``, or a ``.collab-kit`` marker). Does NOT assume a fixed depth
    (a shim lives in ``tools/``; the core in ``tools/lib/``).

    Raises:
        CollabError: if no kit-root signature is found within ``_MAX_WALK_LEVELS``.
    """
    env = os.environ.get("COLLAB_KIT_ROOT")
    if env:
        return Path(_normalize_msys_path(env)).expanduser().resolve()
    here = (start or _script_dir()).resolve()
    for candidate in (here, *here.parents[:_MAX_WALK_LEVELS]):
        if _looks_like_kit_root(candidate):
            return candidate
    raise CollabError(
        f"could not locate collab-kit root above {here} "
        f"(looked for a dir with tools/ + install.sh, or a .collab-kit marker)"
    )


def resolve_collab_home() -> Path:
    """Resolve ``$COLLAB_HOME`` (env → fallback KIT_ROOT), canonicalized.

    Canonicalizing prevents two spellings of the same directory from mapping to two
    different lock directories — a correctness requirement for the lock, not cosmetics.
    An MSYS ``/c/..`` env value is normalized to Windows form first.
    """
    env = os.environ.get("COLLAB_HOME")
    base = Path(_normalize_msys_path(env)) if env else resolve_kit_root()
    return base.expanduser().resolve()


def load_dotenv(path: str | os.PathLike | None = None) -> None:
    """Load ``KEY=value`` pairs from a ``.env`` file into ``os.environ`` (stdlib, no python-dotenv).

    Best-effort: a missing/unreadable file is a no-op. A value already present in the real environment
    WINS (``setdefault``), so a shell-exported secret always overrides the file. Default location:
    ``$COLLAB_ENV_FILE`` if set, else ``<kit root>/.env``. ``#`` comments, blank lines, optional
    surrounding quotes and a leading ``export`` are handled; a line without ``=`` is skipped.
    """
    if path is None:
        override = os.environ.get("COLLAB_ENV_FILE")
        if override:
            path = override
        else:
            try:
                path = resolve_kit_root() / ".env"
            except CollabError:
                return  # no kit root found -> nothing to load
    try:
        text = Path(path).read_text("utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        if key:
            os.environ.setdefault(key, val.strip().strip('"').strip("'"))


def collab_root(name: str) -> Path:
    """Path to a single collab's root under ``$COLLAB_HOME`` (name is slug-sanitized).

    NOTE: ``slugify`` guarantees a safe path component, but it is many-to-one — it is NOT
    an isolation boundary between distinct projects/users. Cross-user isolation must rest on
    the registry (only known projects are addressable), not on slug uniqueness.
    """
    return resolve_collab_home() / slugify(name)


# --------------------------------------------------------------------------- #
# slugify — early path-traversal defense
# --------------------------------------------------------------------------- #

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_SLUG_VALID = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def slugify(s: str) -> str:
    """Reduce arbitrary (untrusted) text to a safe path component ``^[a-z0-9][a-z0-9-]*$``.

    Neutralizes traversal (``../``, ``..\\``, absolute paths) and Unicode tricks, and
    mangles reserved Windows device names so they can't become bare components.

    Raises:
        ValueError: if the input reduces to nothing usable (e.g. ``".."``, ``"  "``).
    """
    if not isinstance(s, str):
        raise ValueError(f"slugify expects str, got {type(s).__name__}")
    normalized = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_STRIP.sub("-", normalized.lower()).strip("-")
    if not slug:
        raise ValueError(f"input {s!r} reduces to an empty slug")
    if slug in _WIN_RESERVED:
        # 'reserved--' cannot be produced by any normal input: a run of non-alnum collapses
        # to a SINGLE '-', so a double-dash prefix is collision-free.
        slug = f"reserved--{slug}"
    if not _SLUG_VALID.match(slug):  # defensive: should always hold by construction
        raise ValueError(f"input {s!r} produced invalid slug {slug!r}")
    return slug


# --------------------------------------------------------------------------- #
# Atomic / safe / exclusive writes
# --------------------------------------------------------------------------- #


def _to_bytes(data: str | bytes) -> bytes:
    return data.encode("utf-8") if isinstance(data, str) else data


def _tmp_name(p: Path) -> Path:
    # pid + time_ns + random: unique across processes AND concurrent same-process writers.
    return p.with_name(f"{p.name}.tmp.{os.getpid()}.{time.time_ns()}.{os.urandom(4).hex()}")


def atomic_write(path: str | os.PathLike, data: str | bytes) -> None:
    """Write ``data`` to ``path`` atomically via a temp file + ``os.replace``.

    Low-level: on Windows ``os.replace`` raises ``PermissionError`` if the target is
    currently open by a reader. Callers that want that handled should use ``safe_write``.
    Any failure (including a failed write) leaves no temp file behind and the target intact.
    """
    p = Path(path)
    tmp = _tmp_name(p)
    try:
        tmp.write_bytes(_to_bytes(data))
        os.replace(tmp, p)
    except BaseException:
        with suppress(OSError):
            tmp.unlink(missing_ok=True)  # no temp-file leftovers on any failure
        raise


def safe_write(
    path: str | os.PathLike,
    data: str | bytes,
    *,
    retries: int = 5,
    backoff: float = 0.02,
) -> None:
    """``atomic_write`` with bounded retry on the transient Windows reader-lock case.

    Retries only ``PermissionError`` (target briefly open by a watcher/editor). Any other
    ``OSError`` is re-raised immediately so real failures are never masked. Bounded — a
    reader holding the file open forever fails loudly rather than hanging.

    Raises:
        CollabError: if the replace still fails after ``retries`` attempts (chained from
            the last underlying ``PermissionError``).
        OSError: for any non-PermissionError filesystem failure.
    """
    attempts = max(1, retries)
    last: PermissionError | None = None
    for attempt in range(attempts):
        try:
            atomic_write(path, data)
            return
        except PermissionError as exc:
            last = exc
            if attempt < attempts - 1:
                time.sleep(backoff * (2**attempt))
    raise CollabError(
        f"could not replace {os.fspath(path)} after {attempts} tries; a reader may be holding it open"
    ) from last


def _write_all(fd: int, data: bytes) -> None:
    """Write *all* of ``data`` to ``fd``, looping over short writes.

    Raises ``CollabError`` if ``os.write`` reports no progress on a non-empty buffer, so a
    pathological fd can never spin this into an infinite loop.
    """
    mv = memoryview(data)
    off = 0
    while off < len(mv):
        n = os.write(fd, mv[off:])
        if n == 0:
            raise CollabError(f"os.write made no progress ({len(mv) - off} bytes remaining)")
        off += n


def _best_effort_unlink(p: Path) -> None:
    with suppress(OSError):
        p.unlink(missing_ok=True)


def _close_quietly(fd: int) -> None:
    """Close ``fd``, swallowing a close-time OSError so it cannot mask the real outcome."""
    with suppress(OSError):
        os.close(fd)


def _fsync_parent_best_effort(path: Path) -> None:
    """fsync ``path``'s parent directory so the new entry is durable.

    Best-effort and **never raises**: opening a directory for fsync is unsupported on Windows
    (and some filesystems). Critically, this runs *after* the atomic publish, so it must not
    raise — a failure here must not make a successfully-committed record look failed.
    """
    try:
        dfd = os.open(os.fspath(path.parent), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        _close_quietly(dfd)


def exclusive_create(path: str | os.PathLike, data: str | bytes) -> None:
    """Create ``path`` with ``data`` atomically, durably, and only if it does not exist.

    The commit primitive for new-file records (e.g. handoff id allocation). Invariant:
    **the destination is either absent or contains the complete intended payload — it never
    becomes visible as an empty or partial final file.** A write/fsync failure cannot poison
    the state machine with a half-written ``pending/NNN-slug.md``.

    How: stage into a durable same-dir temp, then publish with a no-overwrite hard link so
    the final name appears only after the bytes are already complete and fsynced:

      1. write all bytes to a unique same-dir temp (``O_CREAT|O_EXCL``); loop short writes
      2. ``fsync`` the temp
      3. ``os.link(temp, final)`` — atomic, fails if ``final`` exists, never exposes ``final``
         before content is complete
      4. best-effort fsync the parent dir; unlink the temp; best-effort fsync the parent again

    Same-directory temp keeps the link on one filesystem. Accepted residual: a crash after the
    link but before the unlink leaves a *complete* final file plus a temp leak (cleanup debt,
    never corruption).

    Caller contract:
      * ``path`` must already be canonical/native — do NOT hand a Git-Bash ``/c/..`` string
        here; normalization is the resolver layer's job (``collab_root``/``resolve_*``).
      * Exclusivity is keyed on the *full path*. To use this as an id-uniqueness backstop
        (slice 2), the committed name must be keyed on the **id alone** (e.g. reserve
        ``pending/{id:03d}.md``) — two racers who pick the same id but different slugs would
        each publish a *different* path and both succeed. Under a correct ``collab_lock`` this
        never arises; it only matters as the second line of defense if the lock is broken.

    Raises:
        FileExistsError: if ``path`` already exists (caller re-allocates and retries).
        CollabError: if hard-link publish is unavailable on this filesystem.
    """
    final = Path(path)
    payload = _to_bytes(data)
    tmp = _tmp_name(final)  # same dir -> same filesystem for the link
    # O_BINARY: without it, Windows opens a TEXT fd and os.write translates \n -> \r\n,
    # silently corrupting any newline-bearing payload. 0 on POSIX. Matches atomic_write bytes.
    fd = os.open(
        os.fspath(tmp),
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
        0o644,
    )
    try:
        _write_all(fd, payload)
        os.fsync(fd)
    except BaseException:
        _close_quietly(fd)
        _best_effort_unlink(tmp)  # no partial temp lingers past a failed stage
        raise
    else:
        _close_quietly(fd)
    # Publish: atomic, no-overwrite. `final` becomes visible only now, already complete.
    try:
        os.link(tmp, final)
    except FileExistsError:
        _best_effort_unlink(tmp)  # destination already committed; leave it untouched
        raise
    except OSError as exc:  # incl. filesystems without hard-link support
        _best_effort_unlink(tmp)
        raise CollabError(f"could not hard-link publish {os.fspath(final)}: {exc}") from exc
    _fsync_parent_best_effort(final)
    _best_effort_unlink(tmp)
    _fsync_parent_best_effort(final)


# --------------------------------------------------------------------------- #
# Race-safe, fenced mkdir lock (Amendment A)
# --------------------------------------------------------------------------- #


@dataclass
class LockHandle:
    """Handle to a held lock. Carries the fencing ``owner_token``."""

    path: Path
    owner_token: str
    acquired_at: float = field(default_factory=time.time)

    @property
    def meta_path(self) -> Path:
        return self.path / "meta.json"

    def _current_token(self) -> str | None:
        m = _read_json_or_none(self.meta_path)
        return None if m is None else m.get("owner_token")

    def is_current(self) -> bool:
        """Advisory: True iff the canonical lock still carries our token."""
        return self._current_token() == self.owner_token

    def assert_current(self) -> None:
        """Fence: call immediately before any final mutation.

        Raises:
            LockBroken: if the lock is gone or now owned by a different token
                (fail-safe: missing/partial ``meta.json`` counts as broken).
        """
        if self._current_token() != self.owner_token:
            raise LockBroken(f"lock {self.path} no longer holds our token")


def _read_json_or_none(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text("utf-8"))
    except FileNotFoundError, ValueError, OSError:
        return None


def _new_owner_token() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{time.time_ns()}:{os.urandom(8).hex()}"


def _unique_sibling(path: Path, tag: str) -> Path:
    return path.with_name(f"{path.name}.{tag}.{time.time_ns()}.{os.getpid()}.{os.urandom(4).hex()}")


def _robust_rmtree(path: Path, *, warn_on_partial: bool = False) -> None:
    """rmtree tolerant of the transient Windows 'file open by reader' case."""
    for attempt in range(5):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            time.sleep(0.02 * (2**attempt))
    if warn_on_partial:
        log.warning("could not fully remove %s; it may linger until TTL ages it out", path)
    shutil.rmtree(path, ignore_errors=True)


def is_lock_held(lockdir: str | os.PathLike, *, ttl: float = 30.0) -> bool:
    """ADVISORY ONLY — inherently racy (TOCTOU).

    For diagnostics / watcher display. MUST NOT gate control flow — use ``collab_lock``
    for mutual exclusion. Returns True iff the lock dir exists and is not older than ``ttl``.
    """
    p = Path(lockdir)
    try:
        return (time.time() - p.stat().st_mtime) <= ttl
    except FileNotFoundError:
        return False


def _fenced_release(lockdir: Path, token: str) -> bool:
    """Release our own lock without ever deleting another owner's lock in place.

    Rename-capture: atomically move ``lockdir`` to a private name (single winner), then
    confirm the captured dir is ours before removing it. If — in the tiny window after our
    fast-path check — a breaker took over and we captured *their* fresh lock, restore it and
    report not-released.

    Returns:
        True  if we cleanly removed our own lock.
        False if the lock was already broken/taken over (we removed nothing of another's).
    """
    # Fast path: meta already shows we were taken over → don't even try to capture.
    m = _read_json_or_none(lockdir / "meta.json")
    if m is None or m.get("owner_token") != token:
        return False
    priv = _unique_sibling(lockdir, "releasing")
    try:
        os.rename(lockdir, priv)  # atomic single-winner capture
    except FileNotFoundError, PermissionError:
        return False  # already gone/broken
    m2 = _read_json_or_none(priv / "meta.json")
    if m2 is not None and m2.get("owner_token") == token:
        _robust_rmtree(priv, warn_on_partial=True)  # safe: no one else can touch our private name
        return True
    # Captured a foreign/newer lock in the race window — restore it, delete nothing of theirs.
    try:
        os.rename(priv, lockdir)
    except OSError:
        log.error("release captured a foreign lock at %s and could not restore it", priv)
    return False


@contextmanager
def collab_lock(
    lockdir: str | os.PathLike,
    *,
    ttl: float = 30.0,
    acquire_timeout: float = 60.0,
    poll: float = 0.02,
) -> Iterator[LockHandle]:
    """Acquire a race-safe, fenced coarse lock via atomic ``mkdir``.

    Semantics (handoff 001 rev 3, hardened by adversarial verification):
      * ``mkdir`` succeeds  -> acquire; commit our ``owner_token`` to ``meta.json``.
      * lock exists, fresh  -> wait (poll) until it frees or ``acquire_timeout``.
      * lock exists, stale  -> **capture-verify break**: atomically rename to a private
        graveyard, re-check the captured dir is still stale (else restore — someone
        re-acquired in the gap), then remove and re-compete. The break winner does NOT
        assume ownership; ownership is real only after a fresh ``mkdir`` + token commit.
      * release             -> rename-capture (``_fenced_release``): never deletes another
        owner's lock in place.

    ``ttl`` is per-call-site: set it generously for legitimately long critical sections. A
    live-but-slow holder that exceeds ``ttl`` can be declared stale — call ``assert_current``
    immediately before the final mutation (and catch ``LockBroken`` to retry); that fence,
    not the ``with`` block alone, is what upholds mutual exclusion in the stale window.

    Raises:
        LockTimeout: acquisition exceeded ``acquire_timeout``.
        LockBroken: on release, if our lock was broken before we could release it (and the
            ``with`` body did not itself raise).
    """
    lockdir = Path(lockdir)
    token = _new_owner_token()
    deadline = time.monotonic() + acquire_timeout
    handle: LockHandle | None = None

    while handle is None:
        if time.monotonic() >= deadline:
            raise LockTimeout(f"could not acquire {lockdir} within {acquire_timeout}s")
        try:
            os.mkdir(lockdir)
        except FileExistsError:
            try:
                age = time.time() - lockdir.stat().st_mtime
            except FileNotFoundError:
                continue  # vanished between mkdir and stat; retry immediately
            if age <= ttl:
                time.sleep(poll)
                continue
            # Stale: capture-verify break. rename CLEARS the obstruction, it does NOT acquire.
            graveyard = _unique_sibling(lockdir, "broken")
            try:
                os.rename(lockdir, graveyard)
            except FileNotFoundError, PermissionError:
                time.sleep(poll)
                continue  # another process broke/holds it; loop and wait
            try:
                captured_age = time.time() - graveyard.stat().st_mtime
            except FileNotFoundError:
                continue
            if captured_age <= ttl:
                # We captured a FRESH lock (someone re-acquired in the gap) — restore it.
                try:
                    os.rename(graveyard, lockdir)
                except OSError:
                    log.error("break captured a fresh lock at %s and could not restore it", graveyard)
                time.sleep(poll)
                continue
            log.warning(
                "broke stale lock %s (age=%.1fs, old_meta=%r)",
                lockdir,
                captured_age,
                _read_json_or_none(graveyard / "meta.json"),
            )
            _robust_rmtree(graveyard)
            continue  # re-enter the loop; a fresh mkdir decides ownership
        else:
            # We own the dir. Commit our token before handing back a handle.
            try:
                meta = {
                    "owner_token": token,
                    "pid": os.getpid(),
                    "host": socket.gethostname(),
                    "acquired_at": time.time(),
                    "ttl": ttl,
                }
                atomic_write(lockdir / "meta.json", json.dumps(meta))
            except FileNotFoundError:
                # Broken out of the mkdir->meta window: a breaker renamed lockdir away before
                # we committed our token (only reachable when ttl << critical section). We
                # never acquired — re-compete instead of crashing.
                continue
            except OSError:
                _robust_rmtree(lockdir)  # never return a handle without a committed token
                raise
            handle = LockHandle(path=lockdir, owner_token=token)

    body_failed = False
    try:
        yield handle
    except BaseException:
        body_failed = True
        raise
    finally:
        released = _fenced_release(lockdir, token)
        # Only surface LockBroken if the body itself didn't already raise (never mask it).
        if not released and not body_failed:
            raise LockBroken(
                f"lock {lockdir} was broken/taken over before release; removed nothing of another's"
            )
