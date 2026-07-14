"""telegram_bridge — zero-dep phone bridge over a file protocol (collab-kit slice 5, §A7).

Puts the human in the loop over Telegram via stdlib ``urllib`` long-poll ([C26]). Telegram is
OPTIONAL ([C30]) and swap-able ([C31]) — the interface is the outbox/inbox files; the core never
imports this module.

File protocol (under ``$COLLAB_HOME``)::

    agent -> phone:  outbox/<ts>-<project>.md  --sent-->  outbox/archive/   (archived ONLY on ok, [C29])
    phone -> agent:  /c <project> <msg>  -->  inbox/live/<project>/from-user-<ts>.md  (watcher surfaces)

Auth ([C27]): bring-your-own @BotFather token via ``TELEGRAM_BOT_TOKEN``. The first inbound message
learns and locks the chat id (``logs/telegram-chat.lock``); ``TELEGRAM_CHAT_ID`` set explicitly
overrides (public bots). The bridge answers exactly one chat.

The Telegram HTTP call is injected as ``tg=`` so the whole protocol is testable without a network.

Operational contract — **run ONE bridge instance per $COLLAB_HOME** (now ENFORCED):
  * **Singleton enforced:** ``run()`` takes an OS-level exclusive lock (``logs/telegram-bridge.lock``,
    ``msvcrt``/``fcntl``) held for the process lifetime; a second bridge on the same home fails fast
    instead of silently double-sending and racing the getUpdates offset. The kernel releases the lock
    on process exit *including crash*, so there is no stale-lock-forever problem (a TTL lock can't be
    used here — a single cycle's 50s long-poll exceeds any sane TTL).
  * **Outbound is path-safe:** ``send_outbox`` forwards only *regular* ``outbox/*.md`` files — symlinks
    (which could point at a secret and leak it to Telegram), FIFOs, devices and directories are skipped
    in place, and content is read with a bounded cap so an attacker-planted multi-GB file can't be
    slurped into memory. A message over Telegram's 4096-char limit is truncated for the phone with a
    ``…[truncated]`` marker; the full file is always preserved in ``outbox/archive/``.
  * **At-least-once outbound:** a crash between send and archive re-sends (never drops).
  * **Trust-on-first-use auth ([C27]):** whoever messages first is locked in (a warning is logged).
    For a public bot, where an attacker could message before you, pin the chat with ``TELEGRAM_CHAT_ID``.
  * **Untrusted input is fail-safe:** malformed/adversarial getUpdates payloads (non-list, non-dict
    updates, bad ``update_id``, non-string text) are skipped and never crash the daemon. A clobbered
    ``logs/telegram-offset`` fails safe to 0, which re-plays Telegram's retained backlog once (bounded
    by Telegram's ~24h retention) — a duplicate flood, never a crash or a drop.
  * **Agent responsibility:** agents MUST publish ``outbox/*.md`` files *atomically* (tmp + ``os.replace``,
    as ``collab_common.safe_write`` does) — the bridge has no write-completion signal and will forward a
    partially-written file if it reads one mid-write. (Symlinks/non-regular files are rejected outright.)
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collab_common as cc
import registry

_API = "https://api.telegram.org/bot{token}/{method}"
_MAX_MSG = 4000  # Telegram hard-limits a message to 4096 chars
_READ_CAP = 512 * 1024  # bounded outbox read: never slurp an attacker-planted multi-GB file into memory


def _tg(token: str, method: str, **params):
    """POST to the Telegram Bot API via stdlib urllib ([C26]). Returns ``result`` or raises."""
    url = _API.format(token=token, method=method)
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=65) as resp:
        payload = json.loads(resp.read().decode())
    if not payload.get("ok"):
        raise cc.CollabError(f"telegram {method} failed: {payload}")
    return payload.get("result")


# --------------------------------------------------------------------------- #
# layout
# --------------------------------------------------------------------------- #


def _outbox(home) -> Path:
    return Path(home) / "outbox"


def _archive(home) -> Path:
    return _outbox(home) / "archive"


def _inbox(home, project: str) -> Path:
    return Path(home) / "inbox" / "live" / cc.slugify(project)  # slugify: traversal defense ([C28])


def _chatlock(home) -> Path:
    return Path(home) / "logs" / "telegram-chat.lock"


def _offsetfile(home) -> Path:
    return Path(home) / "logs" / "telegram-offset"


# --------------------------------------------------------------------------- #
# chat-id learn-and-lock ([C27])
# --------------------------------------------------------------------------- #


def resolve_chat_id(home):
    """The chat the bridge answers: ``TELEGRAM_CHAT_ID`` override, else the learned+locked id, else None."""
    env = os.environ.get("TELEGRAM_CHAT_ID")
    if env:
        return env
    try:
        return _chatlock(home).read_text("utf-8").strip() or None
    except FileNotFoundError, OSError:
        return None


def _learn_chat_id(home, chat_id) -> None:
    if os.environ.get("TELEGRAM_CHAT_ID"):
        return  # explicit override — never learn/relock
    lock = _chatlock(home)
    if lock.exists():
        return
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        cc.exclusive_create(lock, f"{chat_id}\n")  # atomic first-writer-wins (no last-writer race, DEFECT 4)
    except FileExistsError:
        return  # a concurrent first-inbound already locked the chat
    # Make trust-on-first-use visible: whoever messages first is locked in. For a public bot
    # (where an attacker could message before you), pin the chat with TELEGRAM_CHAT_ID instead.
    print(
        f"telegram-bridge: locked to chat {chat_id} on first inbound (trust-on-first-use); "
        f"set TELEGRAM_CHAT_ID to pin a specific chat for a public bot.",
        file=sys.stderr,
    )


# --------------------------------------------------------------------------- #
# outbound: send outbox, archive ONLY on confirmed delivery ([C29])
# --------------------------------------------------------------------------- #


def send_outbox(home, token, chat_id, *, tg=_tg, warned=None) -> list:
    """Forward each *regular* ``outbox/*.md`` to the chat; archive it only after Telegram returns ok.

    Path-safety (same rule the watcher applies to ``pending/``): ``lstat`` + ``S_ISREG`` reject symlinks
    (a symlink could point at a secret and leak its contents to Telegram), FIFOs, devices and directories
    — they are skipped in place (warned once), never read. Content is read with a bounded cap so an
    attacker-planted multi-GB file cannot be slurped into memory.
    """
    if chat_id is None:
        return []
    ob = _outbox(home)
    if not ob.exists():
        return []
    if warned is None:
        warned = set()
    sent = []
    for p in sorted(ob.glob("*.md")):
        try:
            st = p.lstat()  # lstat: never follow a symlink before we know it's a regular file
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):  # symlink / FIFO / device / dir — skip, don't read/send
            if p.name not in warned:
                print(
                    f"telegram-bridge: skipping non-regular outbox file (not sent): {p.name}", file=sys.stderr
                )
                warned.add(p.name)
            continue
        try:
            with open(p, "rb") as fh:
                raw = fh.read(_READ_CAP)  # bounded read: memory is capped regardless of on-disk size
        except OSError:
            continue
        text = raw.decode("utf-8", errors="replace")
        if len(text) > _MAX_MSG:  # visible truncation, not silent data loss — full file stays in archive/
            marker = f"\n…[truncated — full text in outbox/archive/{p.name}]"
            text = text[: _MAX_MSG - len(marker)] + marker
        tg(token, "sendMessage", chat_id=chat_id, text=text)  # raises => not archived => re-sent
        _archive(home).mkdir(parents=True, exist_ok=True)
        os.replace(p, _archive(home) / p.name)  # archive ONLY after confirmed send ([C29])
        sent.append(p.name)
    return sent


# --------------------------------------------------------------------------- #
# inbound: /c <project> <msg> -> inbox, registry-validated ([C28])
# --------------------------------------------------------------------------- #


def _load_offset(home) -> int:
    try:
        return int(_offsetfile(home).read_text("utf-8").strip())
    except FileNotFoundError, OSError, ValueError:
        return 0


def _save_offset(home, offset: int) -> None:
    _offsetfile(home).parent.mkdir(parents=True, exist_ok=True)
    cc.safe_write(_offsetfile(home), f"{offset}\n")


def poll_updates(home, token, *, tg=_tg) -> list:
    """Long-poll ``getUpdates``; route ``/c <project> <msg>`` into the inbox for a *registered* project."""
    start = _load_offset(home)
    offset = start
    updates = tg(token, "getUpdates", offset=offset + 1, timeout=50)
    if not isinstance(updates, list):  # untrusted API result — only a list is safe to iterate
        return []
    written = []
    for u in updates:
        if not isinstance(u, dict):
            continue
        try:
            offset = max(offset, int(u.get("update_id")))
        except TypeError, ValueError:
            continue  # malformed update_id (never from the real API) — skip, don't crash the daemon
        msg = u.get("message")
        if not isinstance(msg, dict):
            continue
        chat_obj = msg.get("chat")
        chat = chat_obj.get("id") if isinstance(chat_obj, dict) else None
        # A real Telegram chat id is a non-zero int (negative for groups). Reject spoofed/missing/junk
        # ids so we never lock to None/''/a list and never DISABLE the one-chat filter with an empty
        # lock (DEFECT 5: an empty lock makes resolve_chat_id return None -> filter routes everyone).
        if not (isinstance(chat, int) and not isinstance(chat, bool) and chat != 0):
            continue
        text = msg.get("text")
        text = text.strip() if isinstance(text, str) else ""  # non-text message (photo/etc.) -> ignore
        _learn_chat_id(home, chat)
        locked = resolve_chat_id(home)
        if locked is None or str(chat) != str(locked):
            continue  # answer exactly one chat ([C27]); never route while unlocked
        if not text.startswith("/c "):
            continue
        project, _, body = text[3:].strip().partition(" ")
        try:
            proj = cc.slugify(project)  # traversal/junk defense ([C28])
        except ValueError:
            continue
        if registry.resolve(proj, home=home) is None:
            continue  # unknown project rejected, NOT created ([C28])
        d = _inbox(home, proj)
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"from-user-{time.time_ns()}.md"
        # Bound + de-control the untrusted body before writing: cap size (disk-fill DoS), drop control
        # chars incl. embedded newlines (a /c message is one line) so the inbox file can't carry forged
        # structure (`## Constraints`), NUL bytes, or terminal escapes for whatever reads it.
        clean = "".join(c for c in body.strip() if c >= " " or c == "\t")[:_MAX_MSG]
        cc.safe_write(f, f"{clean}\n")
        written.append(str(f))
    if offset > start:
        _save_offset(home, offset)
    return written


def _acquire_singleton(home):
    """Take an OS-level exclusive lock on ``logs/telegram-bridge.lock`` for the process lifetime.

    Enforces the "run ONE bridge per $COLLAB_HOME" invariant: a second bridge on the same home fails
    fast (``CollabError``) instead of silently double-sending outbound and racing the getUpdates offset
    backwards (adversarial lanes #2/#3). The kernel drops the lock when the holder exits — *including a
    crash* — so, unlike a PID-file or a TTL lock, there is no stale-lock-forever problem and no risk of
    a legitimately-slow cycle (the 50s ``getUpdates`` long-poll) being mistaken for a dead holder.

    Returns the open file object; the caller MUST keep it referenced — closing it (or exiting) releases
    the lock. Raises ``cc.CollabError`` if another bridge already holds it.
    """
    p = Path(home) / "logs" / "telegram-bridge.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    # The caller must retain this handle for the process lifetime: closing it releases the OS lock.
    f = open(p, "a+")  # noqa: SIM115
    try:
        if os.name == "nt":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)  # non-blocking exclusive lock on byte 0
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        f.close()
        raise cc.CollabError(
            "another telegram-bridge is already running for this $COLLAB_HOME — run ONE bridge per home "
            "(two bridges double-send outbound and race the getUpdates offset)"
        ) from exc
    return f


def run(home=None, *, once=False, interval=2.0, tg=_tg) -> int:
    cc.load_dotenv()  # pick up TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID from <kit root>/.env if not already in env
    home = home or cc.resolve_collab_home()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("telegram-bridge: TELEGRAM_BOT_TOKEN not set (Telegram is optional, [C30])", file=sys.stderr)
        return 1
    try:
        guard = _acquire_singleton(home)  # [C29] one bridge per home — enforced, not just documented
    except cc.CollabError as e:
        print(f"telegram-bridge: {e}", file=sys.stderr)
        return 1
    warned_outbox: set = (
        set()
    )  # persist across cycles so a skipped non-regular file warns once, not every tick
    try:
        if not os.environ.get("TELEGRAM_CHAT_ID") and not _chatlock(home).exists():
            print(
                "telegram-bridge: no chat pinned — trust-on-first-use is active; the FIRST inbound chat "
                "will be locked in. Set TELEGRAM_CHAT_ID for a public bot.",
                file=sys.stderr,
            )
        while True:
            try:
                send_outbox(home, token, resolve_chat_id(home), tg=tg, warned=warned_outbox)
                poll_updates(home, token, tg=tg)
            except Exception as e:  # a network/parse error must NEVER kill a long-running daemon
                print(f"telegram-bridge: {type(e).__name__}: {e}", file=sys.stderr)
            if once:
                break
            time.sleep(interval)
    finally:
        guard.close()  # release the singleton lock (also freed by the OS on process exit)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="telegram-bridge", description="collab-kit phone bridge (optional)")
    p.add_argument("--home")
    p.add_argument("--once", action="store_true")
    args = p.parse_args(sys.argv[1:] if argv is None else argv)
    return run(args.home, once=args.once)


if __name__ == "__main__":
    sys.exit(main())
