"""watch — answer "is anything actually running?" in one screen, from the durable surfaces only.

The dashboard reports the driver's *phase* ("thinking") but nothing about the agentic call in flight, and
a round is where all the wall-clock goes. So a live driver and a hung one look identical for minutes.

This prints, once a second:
  * LEASE     — the board lease's heartbeat age. THIS is liveness: the driver renews it every 30s, so an
                age under the 90s TTL means the process is alive and holding the board. Nothing else on
                the dashboard answers this question.
  * SEATS     — the actual seat subprocesses burning CPU right now (the model call in flight).
  * STATUS    — phase/stage/round/hid + heartbeat age of status.json.
  * EVENTS    — the last few real events, appended as they happen.

Read-only: opens nothing but files the driver already writes. Ctrl+C to quit.

    python collab/tools/watch.py --collab .
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


def _load(p: Path):
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception:
        return None


def _age(epoch) -> float | None:
    try:
        return time.time() - float(epoch)
    except Exception:
        return None


def _seats() -> list[str]:
    """Seat subprocesses in flight — the model call actually running, if any."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='claude.exe' OR Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -like '*seat*' -or $_.CommandLine -like '*claude -p*' } | "
             "ForEach-Object { \"$($_.ProcessId) "
             "$($_.CommandLine.Substring(0,[Math]::Min(60,$_.CommandLine.Length)))\" }"],
            capture_output=True, text=True, timeout=8,
        )
        return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    except Exception:
        return []


def _events(p: Path, n: int) -> list[str]:
    try:
        lines = p.read_text("utf-8", errors="replace").splitlines()[-n:]
    except Exception:
        return []
    out = []
    for ln in lines:
        try:
            d = json.loads(ln)
        except Exception:
            continue
        ts = str(d.get("ts", ""))[-9:]
        stage = str(d.get("stage", ""))
        act = str((d.get("decision") or {}).get("action", ""))
        rc = str((d.get("decision") or {}).get("reason_codes", ""))[:44]
        out.append(f"  {ts}  {stage:26} {act:11} {rc}")
    return out


def main() -> int:
    ap_ = argparse.ArgumentParser(prog="watch")
    ap_.add_argument("--collab", required=True)
    ap_.add_argument("--interval", type=float, default=1.0)
    a = ap_.parse_args()
    root = Path(a.collab).resolve()
    lease_p, status_p = root / "autopilot" / "active.lease", root / "autopilot" / "status.json"
    events_p = root / "logs" / "events.jsonl"
    tick = 0
    while True:
        lease, status = _load(lease_p), _load(status_p)
        print("\033[2J\033[H", end="")  # clear
        print(f"watch · {root.name} · {time.strftime('%H:%M:%S')}\n" + "=" * 72)

        if lease:
            age = _age(lease.get("heartbeat_epoch"))
            live = age is not None and age < 90
            mark = "RUNNING" if live else "STALE -> driver is dead or wedged"
            print(f"LEASE   {mark}   hid={lease.get('hid')}  heartbeat={age:.0f}s ago  (TTL 90s)")
            print(f"        run={lease.get('run_uid')}  pid={lease.get('pid')}")
        else:
            print("LEASE   no lease -> no driver holds the board")

        seats = _seats() if tick % 3 == 0 or not hasattr(main, "_s") else main._s  # throttle: ps is slow
        main._s = seats
        idle = "" if seats else "  (none — between rounds)"
        print(f"\nSEATS   {len(seats)} model call(s) in flight" + idle)
        for s in seats[:4]:
            print(f"        {s}")

        if status:
            sage = None
            if status.get("updated_ts"):
                _p = time.strptime(status["updated_ts"], "%Y-%m-%dT%H:%M:%SZ")
                sage = _age(time.mktime(_p) - time.timezone)
            print(f"\nSTATUS  phase={status.get('phase')}  stage={status.get('stage')}  "
                  f"round={status.get('round')}/{status.get('max_rounds')}  hid={status.get('current_hid')}")
            if sage is not None:
                print(f"        status heartbeat {sage:.0f}s ago")

        ev = _events(events_p, 6)
        if ev:
            print("\nEVENTS")
            print("\n".join(ev))
        print("\n" + "-" * 72 + "\nCtrl+C to quit")
        tick += 1
        time.sleep(a.interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
