"""dashboard — entry point for the autopilot monitoring dashboard (collab-kit).

A decoupled, read-plus-control view of an autopilot run: launch it in a second terminal while
``autopilot`` drives a collab. Two surfaces over one shared data layer (:mod:`dashboard_core`):

    dashboard --collab <path>                 # live terminal TUI (default)
    dashboard --collab <path> --web [--port N]  # local web dashboard at http://127.0.0.1:PORT

``--home`` sets ``$COLLAB_HOME`` (for ``seats.json``); it defaults to ``resolve_collab_home()``. The
dashboard only ever reads the run's durable surfaces and issues the human-gated controls (pause/resume,
approve) — the driver itself is untouched.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import collab_common as cc


def main(argv=None) -> int:
    cc.load_dotenv()  # so a home resolved from .env / seat models line up with what the driver sees
    p = argparse.ArgumentParser(prog="dashboard", description="collab-kit autopilot monitoring dashboard")
    p.add_argument("--collab", required=True, help="collab path to watch")
    p.add_argument("--home", help="$COLLAB_HOME (for seats.json); defaults to resolve_collab_home()")
    p.add_argument("--web", action="store_true", help="serve the web dashboard instead of the terminal TUI")
    p.add_argument("--port", type=int, default=8787, help="web dashboard port (with --web); default 8787")
    p.add_argument("--interval", type=float, default=1.0, help="TUI poll interval seconds; default 1.0")
    args = p.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        home = args.home or str(cc.resolve_collab_home())
    except cc.CollabError as e:
        print(f"dashboard: {e}", file=sys.stderr)
        return 1

    if args.web:
        import dashboard_web

        return dashboard_web.serve(args.collab, home, port=args.port)
    import dashboard_tui

    return dashboard_tui.run_tui(args.collab, home, interval=args.interval)


if __name__ == "__main__":
    sys.exit(main())
