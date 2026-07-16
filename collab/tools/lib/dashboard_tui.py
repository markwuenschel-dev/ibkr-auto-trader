"""dashboard_tui — a stdlib, dependency-free live console view of an autopilot run.

Run in a second terminal while ``autopilot.py`` drives a collab. Polls :func:`dashboard_core.snapshot`
and redraws in place (ANSI), showing the seats, round progress, the handoff board, and a live event
feed with per-round latency. Human-in-the-loop keys pause/resume the loop and approve (advance) a
handoff — the driver itself never advances state ([C36]).

Windows-first: keyboard input via ``msvcrt`` (non-blocking, so the redraw stays smooth); a POSIX
``termios``/``select`` fallback is provided so the module also runs on Linux/macOS. No ``curses``.
"""

from __future__ import annotations

import os
import sys
import time
from contextlib import suppress
from pathlib import Path

_LIB = str(Path(__file__).resolve().parent)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import collab_common as cc  # noqa: E402
import dashboard_core as dc  # noqa: E402
import handoff_core as hc  # noqa: E402

# --------------------------------------------------------------------------- #
# ANSI helpers
# --------------------------------------------------------------------------- #

_RESET = "\x1b[0m"
_STYLE = {
    "dim": "\x1b[2m",
    "bold": "\x1b[1m",
    "red": "\x1b[31m",
    "green": "\x1b[32m",
    "yellow": "\x1b[33m",
    "blue": "\x1b[34m",
    "magenta": "\x1b[35m",
    "cyan": "\x1b[36m",
    "inv": "\x1b[7m",
}
_PHASE_COLOR = {
    "thinking": "cyan",
    "sleeping": "blue",
    "idle": "dim",
    "paused": "yellow",
    "capped": "magenta",
    "done": "green",
}


def _c(text: str, *styles: str) -> str:
    pre = "".join(_STYLE[s] for s in styles if s in _STYLE)
    return f"{pre}{text}{_RESET}" if pre else text


def _enable_vt() -> None:
    """Turn on ANSI escape processing (Win10+ conhost needs this; no-op elsewhere) and make stdout
    tolerant of the Unicode glyphs we draw (a cp1252 console would otherwise raise on '●'/'→')."""
    if os.name == "nt":
        os.system("")  # side effect: enables VT100 processing on the attached console
    with suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # py3.7+


def _fmt_age(sec) -> str:
    if sec is None:
        return "  -"
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    return f"{sec // 3600}h"


def _fmt_ms(ms) -> str:
    if ms is None:
        return "-"
    if ms < 1000:
        return f"{int(ms)}ms"
    return f"{ms / 1000:.1f}s"


# --------------------------------------------------------------------------- #
# keyboard (Windows primary, POSIX fallback)
# --------------------------------------------------------------------------- #

try:
    import msvcrt  # Windows

    def _read_key():
        """Return one pressed key (str) or None if none waiting. Non-blocking."""
        if not msvcrt.kbhit():
            return None
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):  # a function/arrow key: consume + ignore the second byte
            if msvcrt.kbhit():
                msvcrt.getwch()
            return None
        return ch

    class _RawInput:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

except ImportError:  # POSIX
    import select
    import termios
    import tty

    def _read_key():
        dr, _, _ = select.select([sys.stdin], [], [], 0)
        return sys.stdin.read(1) if dr else None

    class _RawInput:
        def __enter__(self):
            self._fd = sys.stdin.fileno()
            self._old = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
            return self

        def __exit__(self, *a):
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)
            return False


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #


def render(snap: dict, selected: int, confirm: str | None, notice: str | None) -> str:
    """Build the full frame as one string (caller positions the cursor and clears trailing lines)."""
    st = snap.get("status") or {}
    phase = st.get("phase") or ("paused" if snap.get("paused") else "—")
    pcolor = _PHASE_COLOR.get(phase, "bold")
    rnd, mx = st.get("round", 0), st.get("max_rounds", 0)
    name = Path(snap["collab"]).name
    L: list[str] = []

    # header
    banner = _c(f" AUTOPILOT · {name} ", "bold", "inv")
    prog = f"round {rnd}/{mx}" if mx else "round —"
    L.append(f"{banner}  {prog}  phase:{_c(phase, pcolor)}" + (f"  pid:{st['pid']}" if st.get("pid") else ""))
    if snap.get("paused"):
        L.append(_c("  ‖ PAUSED — the loop is idling; press r to resume ", "yellow", "inv"))
    L.append("")

    # seats
    seats = snap.get("seats") or {}
    active = st.get("active_seat")
    L.append(_c("SEATS", "bold"))
    if seats:
        for sname, cfg in seats.items():
            mark = _c("● thinking", "cyan") if sname == active else _c("○ idle", "dim")
            model = (cfg or {}).get("model") or "?"
            hid = f" on {st.get('current_hid')}" if sname == active and st.get("current_hid") else ""
            L.append(f"  {sname:<10} {mark:<22} {_c(model, 'dim')}{hid}")
    else:
        L.append(_c("  (no seats.json found for --home)", "dim"))
    last_lat = st.get("last_latency_ms")
    if last_lat is not None:
        L.append(_c(f"  last round: {_fmt_ms(last_lat)}", "dim"))
    if st.get("last_error"):
        L.append("  " + _c(f"last error: {st['last_error']}", "red"))
    L.append("")

    # board
    counts = snap.get("counts") or {}
    csum = "  ".join(f"{s} {counts.get(s, 0)}" for s in ("pending", "claimed", "done", "archive"))
    L.append(_c("BOARD", "bold") + f"   {csum}")
    openh = snap.get("open") or []
    if openh:
        for i, r in enumerate(openh):
            sel = i == selected
            cur = _c(">", "bold", "green") if sel else " "
            route = f"{r.get('from') or '?'}→{r.get('to') or '?'}"
            line = f"  {cur} {r['id']:<5} {r['state']:<8} {route:<22} {_fmt_age(r.get('age_s')):>4}"
            L.append(_c(line, "inv") if sel else line)
    else:
        L.append(_c("  (no open handoffs)", "dim"))
    L.append("")

    # event feed
    L.append(_c("FEED", "bold") + _c("   (newest last)", "dim"))
    for ev in (snap.get("events") or [])[-12:]:
        L.append("  " + _fmt_event(ev))
    L.append("")

    # notices / confirm / keys
    if confirm:
        L.append(_c(f"  Approve (advance) {confirm} to done? [y/N] ", "yellow", "inv"))
    elif notice:
        L.append("  " + _c(notice, "green"))
    L.append(
        _c("  keys: ", "dim")
        + _c("p", "bold")
        + " pause  "
        + _c("r", "bold")
        + " resume  "
        + _c("j/k", "bold")
        + " select  "
        + _c("a", "bold")
        + " approve  "
        + _c("q", "bold")
        + " quit"
    )
    return "\n".join(L)


def _fmt_event(ev: dict) -> str:
    ts = (ev.get("ts") or "")[11:19]  # HH:MM:SS
    stage = ev.get("stage") or "?"
    role = ev.get("role") or "?"
    dec = ev.get("decision") or {}
    action = dec.get("action") or ""
    art = (ev.get("artifact") or "").replace("handoff:", "")
    metrics = ev.get("metrics") or {}
    lat = metrics.get("latency_ms")

    if stage == "autopilot.round" and action == "reply":
        newid = next(
            (rc.split(":", 1)[1] for rc in dec.get("reason_codes", []) if rc.startswith("new:")), "?"
        )
        bar = _bar(lat)
        return (
            _c(f"{ts} ", "dim") + _c(f"{role} answered {art} → {newid}", "green") + f"  {_fmt_ms(lat)} {bar}"
        )
    if stage == "autopilot.round" and action == "fail":
        msg = (ev.get("failure") or {}).get("message", "")[:40]
        return _c(f"{ts} ", "dim") + _c(f"{role} FAILED {art}: {msg}", "red")
    if stage == "autopilot.round" and action == "start":
        return _c(f"{ts} ", "dim") + _c(f"{role} thinking on {art}", "cyan")
    if stage == "autopilot.control":
        return _c(f"{ts} ", "dim") + _c(f"· {action} ({role})", "yellow")
    if stage == "autopilot.idle":
        return _c(f"{ts} · idle", "dim")
    if stage == "autopilot.pause":
        return _c(f"{ts} ", "dim") + _c(f"· cap reached ({action})", "magenta")
    if stage == "handoff.done":
        return _c(f"{ts} ", "dim") + _c(f"✓ {art} → done", "green")
    if stage == "handoff.create":
        return _c(f"{ts} ", "dim") + f"+ created {art}"
    if stage == "review":
        return _c(f"{ts} ", "dim") + f"claim {art}"
    return _c(f"{ts} {stage} {action} {art}", "dim")


def _bar(ms) -> str:
    """A tiny latency bar, ~1 block per 500ms, capped at 20."""
    if not ms:
        return ""
    n = min(20, max(1, int(ms / 500)))
    return _c("▇" * n, "dim")


# --------------------------------------------------------------------------- #
# main loop
# --------------------------------------------------------------------------- #


def run_tui(collab, home=None, *, interval: float = 1.0) -> int:
    """Live TUI loop. Returns 0 on clean quit. Restores the terminal on exit."""
    _enable_vt()
    collab = str(collab)
    selected = 0
    confirm: str | None = None  # handoff id awaiting a y/n approve confirmation
    notice: str | None = None
    snap = dc.snapshot(collab, home)
    last_poll = time.monotonic()
    dirty = True

    sys.stdout.write("\x1b[2J\x1b[H\x1b[?25l")  # clear, home, hide cursor
    sys.stdout.flush()
    try:
        with _RawInput():
            while True:
                now = time.monotonic()
                if now - last_poll >= interval:
                    snap = dc.snapshot(collab, home)
                    last_poll = now
                    dirty = True
                    if notice and now - _notice_at[0] > 3.0:
                        notice = None

                key = _read_key()
                if key:
                    dirty = True
                    openh = snap.get("open") or []
                    if confirm is not None:
                        if key in ("y", "Y"):
                            notice = _do_approve(collab, confirm)
                            _notice_at[0] = time.monotonic()
                            snap = dc.snapshot(collab, home)
                        confirm = None
                    elif key in ("q", "Q", "\x03"):  # q or Ctrl-C
                        break
                    elif key in ("p", "P"):
                        dc.set_paused(collab, True, by="dashboard-tui")
                        snap = dc.snapshot(collab, home)
                    elif key in ("r", "R"):
                        dc.set_paused(collab, False, by="dashboard-tui")
                        snap = dc.snapshot(collab, home)
                    elif key in ("j", "J") and openh:
                        selected = min(selected + 1, len(openh) - 1)
                    elif key in ("k", "K") and openh:
                        selected = max(selected - 1, 0)
                    elif key in ("a", "A") and openh:
                        selected = min(selected, len(openh) - 1)
                        confirm = openh[selected]["id"]

                openh = snap.get("open") or []
                selected = max(0, min(selected, len(openh) - 1)) if openh else 0

                if dirty:
                    frame = render(snap, selected, confirm, notice)
                    sys.stdout.write("\x1b[H" + frame.replace("\n", "\x1b[K\n") + "\x1b[K\x1b[J")
                    sys.stdout.flush()
                    dirty = False
                time.sleep(0.05)
    finally:
        sys.stdout.write("\x1b[?25h\n")  # restore cursor
        sys.stdout.flush()
    return 0


_notice_at = [0.0]  # module-level clock for the transient notice (avoids threading a timestamp around)


def _do_approve(collab, hid: str) -> str:
    """HUMAN OVERRIDE from the TUI. No evidence is checked, so it is recorded as an override.

    The TUI is a single-keystroke surface with no prompt loop, so it cannot collect a typed reason; it
    states the one that is actually true — a keypress here, by whoever is at the terminal — rather than
    inventing a justification. ``handoff done --reason`` and the web dashboard take a real one.
    """
    try:
        r = dc.advance_handoff(
            collab,
            hid,
            actor=os.environ.get("USER") or "tui",
            reason="human override from the dashboard TUI (no evidence checked)",
        )
        return f"HUMAN OVERRIDE {hid} → {r['state']}" if r.get("changed") else f"{hid} already {r['state']}"
    except hc.HandoffNotFound:
        return f"{hid} not found"
    except cc.CollabError as e:
        return f"approve failed: {e}"
