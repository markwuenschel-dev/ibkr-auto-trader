"""handoff_cli — the ``handoff`` command-line entry (collab-kit slice 3).

A THIN argparse layer over ``handoff_core`` + ``registry`` ([C14]) — no state/id logic here. Every
state-changing command emits a telemetry trace event ([C15]). Exit codes are mapped from TYPED
``handoff_core`` errors, never string parsing ([C16]):

    0 ok · 1 bad-args/general error · 3 conflict (already-claimed/wrong-state) · 4 not-found

Command surface (subcommand-first, mirroring ``gate run <artifact>``)::

    handoff create <collab> --to R --title "..." [--from F --priority P --body B]
    handoff list   <collab> [--state pending|claimed|done|archive]
    handoff claim|show|done|archive|state  <collab> <id>
    handoff orphans <collab>
    handoff register <name> --root <path> [--reviewer R --repo URL]
    handoff status
    handoff new <name> [--reviewer R]
    handoff bundle <collab> <id> [<id>...] [--emit-manifest --base P --roots G...]

``<collab>`` is a registry name OR a path; when omitted it falls back to ``$HANDOFF_ROOT`` (which
``collab-handoff`` binds). ``bundle`` requires an explicit ``<collab>`` (its ids are variadic, so the
``$HANDOFF_ROOT`` fallback would be ambiguous). stdlib only.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

_LIB = str(Path(__file__).resolve().parent)
sys.path.insert(0, _LIB)
import collab_common as cc  # noqa: E402
import handoff_core as hc  # noqa: E402
import handoff_events as he  # noqa: E402
import registry  # noqa: E402


def _load_local(alias: str, filename: str):
    """Load a sibling module by path — immune to stdlib name-shadowing (our ``trace.py`` vs the
    stdlib ``trace``; a plain ``import trace`` would bind whichever is already in sys.modules)."""
    spec = importlib.util.spec_from_file_location(alias, Path(_LIB) / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_trace = _load_local("collab_trace", "trace.py")

EXIT_OK, EXIT_USAGE, EXIT_CONFLICT, EXIT_NOTFOUND = 0, 1, 3, 4


# --------------------------------------------------------------------------- #
# collab resolution + telemetry helpers
# --------------------------------------------------------------------------- #


def _resolve_collab(name_or_path) -> Path:
    """A collab is a registry name OR a filesystem path."""
    s = str(name_or_path)
    p = Path(name_or_path).expanduser()
    if p.exists() or "/" in s or os.sep in s:
        return p
    root = registry.resolve(name_or_path)
    return root if root is not None else p


def _collab_from(args) -> Path:
    label = getattr(args, "collab", None) or os.environ.get("HANDOFF_ROOT")
    if not label:
        raise cc.CollabError("no collab given (pass <collab> or set $HANDOFF_ROOT)")
    return _resolve_collab(label)


def _run_id(collab: Path) -> str:
    try:
        return cc.slugify(collab.name or "collab")
    except ValueError:
        return "collab"


def _log(collab: Path) -> str:
    d = collab / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return str(d / "events.jsonl")


def _emit_safe(fn, *args, **kwargs) -> None:
    """Telemetry is observability, not correctness ([C15]). A committed state change ([C10]: the
    directory is the sole source of truth) must NEVER be reported as failed because an append-only
    audit write failed or its log lock was contended. Log to stderr and continue.
    """
    try:
        fn(*args, **kwargs)
    except Exception as e:  # deliberately broad: emit is fire-and-forget, state already committed
        print(f"warning: telemetry emit failed (state change already committed): {e}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


def cmd_create(args) -> int:
    collab = _collab_from(args)
    r = hc.create(collab, to=args.to, from_=args.from_, title=args.title,
                  priority=args.priority, body=args.body or "")  # [C19] guard lives in render_handoff
    _emit_safe(he.on_create, _log(collab), _run_id(collab), r["id"], span_id=r["id"], title=args.title)  # [C15]
    print(r["id"])
    return EXIT_OK


def cmd_list(args) -> int:
    collab = _collab_from(args)
    for h in hc.list_handoffs(collab, args.state):
        print(f"{h['id']}  {h['state']:8}  {h['slug']}")
    return EXIT_OK


def cmd_claim(args) -> int:
    collab = _collab_from(args)
    hc.claim(collab, args.id)  # raises HandoffNotFound/HandoffConflict -> mapped in main ([C16])
    _emit_safe(he.on_claim, _log(collab), _run_id(collab), args.id, span_id=f"{args.id}:claim",
               parent_span_id=None, by=os.environ.get("USER") or "cli")  # [C15]
    print(f"{args.id} -> claimed")
    return EXIT_OK


def cmd_done(args) -> int:
    collab = _collab_from(args)
    hc.done(collab, args.id)
    _emit_safe(he.on_done, _log(collab), _run_id(collab), args.id, span_id=f"{args.id}:done", parent_span_id=None)  # [C15]
    print(f"{args.id} -> done")
    return EXIT_OK


def cmd_archive(args) -> int:
    collab = _collab_from(args)
    hc.archive(collab, args.id)
    _emit_safe(_trace.emit, _log(collab), run_id=_run_id(collab), stage="handoff.archive", role="builder",
               artifact=f"handoff:{args.id}", span_id=f"{args.id}:archive",
               decision={"action": "accept", "reason_codes": ["cli:archive"]})  # [C15]; distinct archive stage
    print(f"{args.id} -> archived")
    return EXIT_OK


def cmd_show(args) -> int:
    sys.stdout.write(hc.show(_collab_from(args), args.id))
    return EXIT_OK


def cmd_state(args) -> int:
    st = hc.state_of(_collab_from(args), args.id)
    if st is None:
        print("(no content file)")
        return EXIT_NOTFOUND
    print(st)
    return EXIT_OK


def cmd_orphans(args) -> int:
    for i in hc.orphaned_ids(_collab_from(args)):
        print(i)
    return EXIT_OK


def cmd_bundle(args) -> int:
    """Assemble N handoffs (+ their dereferenced reply artifacts) into ONE review package (JSON stdout).

    Reuses ``dashboard_core.handoff_view`` — zero new deref/path logic ([C28]/[C38]): each entry carries
    whitelisted frontmatter + the pointer-resolved ``body_text`` (UNTRUSTED agent data — render as text).
    With ``--emit-manifest`` it attaches a ``{path: sha256}`` source manifest so a reviewer's approval can
    be pinned to exact source bytes (the source==tested attestation the ``source_consistency`` gate
    re-verifies at closeout, §17).
    """
    collab = _collab_from(args)
    import dashboard_core as dc  # local: keeps the deref primitive's imports off the hot CLI path
    pkg = {
        "generated_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "collab": str(collab),
        "bundle": [dc.handoff_view(str(collab), hid) for hid in args.ids],  # raises HandoffNotFound -> exit 4
    }
    if args.emit_manifest:
        import gate_runner as gr
        base = Path(args.base).expanduser() if args.base else collab
        pkg["source_base"] = str(base)
        pkg["source_manifest"] = gr.source_manifest(args.roots or ["**/*.py"], base)
    json.dump(pkg, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return EXIT_OK


def cmd_register(args) -> int:
    e = registry.register(args.name, args.root, reviewer=args.reviewer, repo=args.repo)
    print(f"registered {e['name']} -> {e['root']}")
    return EXIT_OK


def cmd_status(args) -> int:
    for s in registry.status():
        print(f"{s['name']:20}  {s['counts']}  oldest_pending={s['oldest_pending_age_s']}s")
    return EXIT_OK


def cmd_new(args) -> int:
    root = cc.collab_root(args.name)  # $COLLAB_HOME/<slug>
    hc.ensure_layout(root)  # minimal scaffold; full newproject (clone+templates) is a later slice
    e = registry.register(args.name, root, reviewer=args.reviewer)
    print(f"scaffolded + registered {e['name']} at {e['root']}")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# parser + dispatch
# --------------------------------------------------------------------------- #


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="handoff", description="collab-kit handoff CLI")
    sub = p.add_subparsers(dest="cmd")

    def with_collab(sp):
        sp.add_argument("collab", nargs="?", help="collab name (registry) or path; else $HANDOFF_ROOT")

    c = sub.add_parser("create", help="create a handoff in pending/")
    with_collab(c)
    c.add_argument("--to", required=True)
    c.add_argument("--from", dest="from_", default="builder")
    c.add_argument("--title", required=True)
    c.add_argument("--priority", default="normal", choices=("high", "normal", "low"))
    c.add_argument("--body", default="")
    c.set_defaults(func=cmd_create)

    ls = sub.add_parser("list", help="list handoffs")
    with_collab(ls)
    ls.add_argument("--state", choices=hc.STATES)
    ls.set_defaults(func=cmd_list)

    for name, fn in (("claim", cmd_claim), ("show", cmd_show), ("done", cmd_done),
                     ("archive", cmd_archive), ("state", cmd_state)):
        sp = sub.add_parser(name, help=f"{name} a handoff")
        with_collab(sp)
        sp.add_argument("id")
        sp.set_defaults(func=fn)

    orp = sub.add_parser("orphans", help="list reserved-but-orphaned ids")
    with_collab(orp)
    orp.set_defaults(func=cmd_orphans)

    reg = sub.add_parser("register", help="register a collab name -> root")
    reg.add_argument("name")
    reg.add_argument("--root", required=True)
    reg.add_argument("--reviewer")
    reg.add_argument("--repo")
    reg.set_defaults(func=cmd_register)

    st = sub.add_parser("status", help="cross-collab overview")
    st.set_defaults(func=cmd_status)

    nw = sub.add_parser("new", help="scaffold + register a collab (minimal)")
    nw.add_argument("name")
    nw.add_argument("--reviewer")
    nw.set_defaults(func=cmd_new)

    bd = sub.add_parser("bundle", help="assemble handoffs + reply artifacts into one JSON review package")
    bd.add_argument("collab", help="collab name (registry) or path (required — bundle has no $HANDOFF_ROOT fallback)")
    bd.add_argument("ids", nargs="+", help="one or more handoff ids to bundle")
    bd.add_argument("--emit-manifest", action="store_true",
                    help="attach a {path: sha256} source manifest (source==tested attestation)")
    bd.add_argument("--base", help="root the manifest paths are relative to (default: the collab path)")
    bd.add_argument("--roots", nargs="+", help="glob(s) for the manifest (default: **/*.py)")
    bd.set_defaults(func=cmd_bundle)
    return p


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        # argparse exits 0 for --help (success), 2 for a parse error. Preserve help's success.
        return EXIT_OK if e.code in (0, None) else EXIT_USAGE
    if not getattr(args, "func", None):
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    try:
        return args.func(args)
    except hc.HandoffNotFound as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_NOTFOUND
    except hc.HandoffConflict as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_CONFLICT
    except (cc.CollabError, ValueError) as e:
        # CollabError = mapped domain error; ValueError = e.g. slugify-empty title/name — a bad-args
        # input error, NOT a crash. Both -> clean exit 1, never an escaping traceback ([C16]).
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
