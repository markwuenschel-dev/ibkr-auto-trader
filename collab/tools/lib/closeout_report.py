"""closeout_report — read-only autonomous-closeout evidence summary (collab-kit slice 7).

Renders the evidence the autonomous-closeout machinery (§18) already produces — the verification ledger
(lanes.py), the recomputed done-contract verdict (done_contract.py), the source manifest, test + lane
results, the reviewer repo-preflight (condition 11), the ``autonomous_done`` audit event, and the final
handoff state — into ONE auditable summary (markdown or JSON), so a human can audit a run without
scraping raw logs.

STRICTLY READ-ONLY ([C37]): it transitions nothing (no ``done``/``archive``), runs no agents, opens no
network. Every input is read through the existing 006 readers; the done-contract it calls is pure. The
verdict is recomputed exactly as the driver computed it — ``reviewer_seat`` is the *signing* reviewer
(``reviewer_preflight.seat``), ``builder_seat`` the handoff author — so the condition table matches the
real closeout decision, not a re-derivation with different seats.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from contextlib import suppress
from pathlib import Path

_LIB = str(Path(__file__).resolve().parent)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import collab_common as cc  # noqa: E402
import contracts  # noqa: E402
import done_contract as dcon  # noqa: E402
import handoff_core as hc  # noqa: E402
import lanes  # noqa: E402
import registry  # noqa: E402
import transitions as _transitions  # noqa: E402
import verification as _v  # noqa: E402

EXIT_OK, EXIT_USAGE, EXIT_NOTFOUND = 0, 1, 4


def _resolve_collab(name_or_path) -> Path:
    """A collab is a registry name OR a filesystem path (mirrors handoff_cli._resolve_collab)."""
    s = str(name_or_path)
    p = Path(name_or_path).expanduser()
    if p.exists() or "/" in s or os.sep in s:
        return p
    root = registry.resolve(name_or_path)
    return root if root is not None else p


def _events(collab) -> list[dict]:
    p = Path(collab) / "logs" / "events.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        with suppress(ValueError):
            out.append(json.loads(line))
    return out


def _manifest_digest(manifest: dict) -> str:
    pre = json.dumps(manifest or {}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(pre.encode("utf-8")).hexdigest()


def collect(collab, hid: str) -> dict:
    """Gather the read-only closeout evidence summary for a handoff. Never mutates anything."""
    collab = str(_resolve_collab(collab))
    state = hc.state_of(collab, hid)
    _, path = hc._reconcile(collab, hid)  # read-only reconcile: authoritative (state, path)
    fm: dict = {}
    title = None
    if path is not None:
        fm = contracts.parse_handoff(Path(path)).get("frontmatter") or {}
        title = fm.get("title")

    ledger = lanes.read_ledger(collab, hid)
    led = ledger or {}
    pre = led.get("reviewer_preflight") or {}
    builder_seat = led.get("builder_seat") or fm.get("from") or ""
    # The SIGNING reviewer (recorded in the preflight), not the verifier lane seat — so the recomputed
    # verdict reproduces the driver's evaluate() call and its condition table exactly.
    reviewer_seat = pre.get("seat") or led.get("reviewer_seat") or fm.get("to") or ""

    verdict = dcon.evaluate(collab, hid, seats={}, reviewer_seat=reviewer_seat, builder_seat=builder_seat)

    lane_list = led.get("lanes") or []
    breaker = next((ln.get("breaker_seat") for ln in lane_list if ln.get("breaker_seat")), None)
    verifier = next(
        (ln.get("verifier_seat") for ln in lane_list if ln.get("verifier_seat")), None
    ) or led.get("reviewer_seat")
    required = lanes.ledger_required_passes(led, lanes.load_lanes())
    ran = sorted(lanes.ledger_ran_passes(led))
    manifest = led.get("source_manifest") or {}
    tests = led.get("tests") or {}
    blockers = led.get("blockers") or []
    autonomous_done = any(
        e.get("stage") in ("handoff.autonomous_done", "autopilot.autonomous_done")
        and e.get("artifact") == f"handoff:{hid}"
        for e in _events(collab)
    )

    return {
        "handoff_id": hid,
        "title": title,
        "status": state,
        "final_state": state,
        "ledger_present": ledger is not None,
        "seats": {
            "builder": builder_seat,
            "reviewer": reviewer_seat,
            "breaker": breaker,
            "verifier": verifier,
        },
        "source_base": led.get("source_base"),
        "source_manifest": {"file_count": len(manifest), "digest12": _manifest_digest(manifest)[:12]},
        # Carry the LABEL, not a bare boolean: "passed: yes" for a pytest-only run is the exact
        # conflation that let a lint/type-broken checkout read as verified (2026-07-15).
        "tests": {
            "passed": tests.get("passed"),
            "run_id": tests.get("run_id"),
            "green": _v.is_green(tests),
            "label": _v.label_of(tests),
            "exit_code": tests.get("exit_code"),
            "command": tests.get("command"),
            "start_sha": tests.get("start_sha"),
            "end_sha": tests.get("end_sha"),
            "checkout_stable": tests.get("checkout_stable"),
        },
        "lanes": {
            "required": required,
            "ran": ran,
            "missing": sorted(set(required) - set(ran)),
            "incomplete": bool(led.get("incomplete")),
            "plan_digest": led.get("verification_plan_digest"),
        },
        "reviewer_preflight": {
            "present": bool(led.get("reviewer_preflight")),
            "seat": pre.get("seat"),
            "repo_access": pre.get("repo_access"),
            "inspected_files": pre.get("inspected_files") or [],
        },
        "blockers": blockers,
        "accepted_residuals": led.get("accepted_residuals") or [],
        "done_contract": {
            "satisfied": verdict["satisfied"],
            "hash": verdict["hash"],
            "conditions": verdict["conditions"],
        },
        "autonomous_done_event": autonomous_done,
        # WHO closed it and on what authority, read from the transition record that ``hc.done`` writes
        # as part of the transition itself. (A re-evaluated contract on an already-``done`` handoff fails
        # condition 10 by design — the driver may only fire from ``claimed`` — so the record, carrying the
        # receipt satisfied at transition time, is the evidence, not a fresh evaluate().)
        "transition": _transitions.summary(collab, hid),
        # Authoritative "was this a valid autonomous closeout". Previously `autonomous_done_event and
        # state == "done"`, where the event is emitted best-effort via _emit_safe AFTER the CAS: a
        # dropped log line silently reported a genuine autonomous close as a human one, and nothing
        # distinguished a human override from a verified close on the artifact at all. The transition
        # record is written by the transition, so absence means "not autonomous" rather than "lost".
        "closed_autonomously": bool(
            _transitions.is_autonomous(_transitions.read(collab, hid)) and state == "done"
        ),
    }


def render_json(summary: dict) -> str:
    return json.dumps(summary, indent=2, sort_keys=True) + "\n"


def _yn(v) -> str:
    return "yes" if v is True else ("no" if v is False else "—")


def render_markdown(summary: dict) -> str:
    s = summary
    seats, dc, pre = s["seats"], s["done_contract"], s["reviewer_preflight"]
    L = []
    L.append(f"# Closeout report — {s['handoff_id']}")
    if s.get("title"):
        L.append(f"*{s['title']}*")
    L.append("")
    L.append(f"- **Final state:** `{s['final_state']}`")
    # Lead with HOW it closed. "Closed autonomously: no" is not the same statement as "a human
    # overrode the gate, and here is who and why" — the second is the one a reader needs.
    tr = s.get("transition") or {}
    L.append(f"- **Closed by:** **{tr.get('label') or _transitions.LABEL_UNRECORDED}**")
    if tr.get("human_override"):
        L.append(f"  - actor: `{tr.get('actor') or '—'}` · at `{tr.get('ts') or '—'}`")
        L.append(f"  - override reason: *{tr.get('reason') or '—'}*")
        L.append("  - ⚠ no authoritative verification receipt backs this closure")
    elif tr.get("autonomous"):
        L.append(
            f"  - reviewer: `{tr.get('actor') or '—'}` · at `{tr.get('ts') or '—'}` · "
            f"receipt `{(tr.get('receipt') or '')[:12]}`"
        )
    L.append(f"- **Closed autonomously:** **{_yn(s['closed_autonomously'])}**")
    L.append(f"- **Done-contract (re-evaluated now):** {_yn(dc['satisfied'])}  (hash `{dc['hash'][:12]}`)")
    L.append(f"- **Autonomous_done event:** {_yn(s['autonomous_done_event'])}")
    L.append(f"- **Ledger present:** {_yn(s['ledger_present'])}")
    L.append("")
    L.append("## Seats")
    L.append(
        f"- builder: `{seats['builder'] or '—'}` · reviewer: `{seats['reviewer'] or '—'}` · "
        f"breaker: `{seats['breaker'] or '—'}` · verifier: `{seats['verifier'] or '—'}`"
    )
    L.append("")
    L.append("## Source (source==tested)")
    L.append(f"- base: `{s['source_base'] or '—'}`")
    L.append(
        f"- manifest: {s['source_manifest']['file_count']} files "
        f"(digest `{s['source_manifest']['digest12']}`)"
    )
    L.append("")
    L.append("## Tests & lanes")
    t = s["tests"]
    L.append(f"- verification: **{t['label']}**")
    L.append(f"- authoritatively green: {_yn(t['green'])}  (run `{t['run_id'] or '—'}`)")
    if t.get("command"):
        L.append(f"- command: `{' '.join(t['command'])}` → exit {t['exit_code']}")
    if t.get("start_sha"):
        moved = "" if t.get("checkout_stable") else "  ⚠ checkout MOVED mid-run"
        L.append(f"- checkout: `{(t['start_sha'] or '')[:12]}` → `{(t['end_sha'] or '')[:12]}`{moved}")
    L.append(f"- lanes required: {s['lanes']['required'] or '—'}")
    L.append(f"- lanes ran: {s['lanes']['ran'] or '—'}")
    if s["lanes"]["missing"]:
        L.append(f"- lanes MISSING: {s['lanes']['missing']}")
    L.append("")
    L.append("## Reviewer repo-preflight (condition 11)")
    L.append(
        f"- present: {_yn(pre['present'])} · seat: `{pre['seat'] or '—'}` · "
        f"repo_access: {_yn(pre['repo_access'])}"
    )
    L.append(f"- inspected files: {pre['inspected_files'] or '—'}")
    L.append("")
    L.append("## Confirmed blockers")
    if s["blockers"]:
        for b in s["blockers"]:
            L.append(
                f"- `{b.get('id')}` [{b.get('lane')}] fixed={_yn(b.get('fixed'))} "
                f"regression={b.get('regression_test') or '—'}: {b.get('description')}"
            )
    else:
        L.append("- none")
    L.append("")
    L.append(f"## Accepted residuals\n- {s['accepted_residuals'] or 'none'}")
    L.append("")
    L.append("## Done-contract conditions")
    L.append("| # | condition | status | detail |")
    L.append("|---|---|---|---|")
    for c in dc["conditions"]:
        detail = str(c.get("detail", "")).replace("|", "\\|")[:140]
        L.append(f"| {c['id']} | {c['name']} | {c['status']} | {detail} |")
    L.append("")
    return "\n".join(L) + "\n"


def render(collab, hid: str, fmt: str = "markdown") -> str:
    """Convenience: collect + render in one call (used by the self-host bundle writer)."""
    summary = collect(collab, hid)
    return render_json(summary) if fmt == "json" else render_markdown(summary)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    p = argparse.ArgumentParser(
        prog="closeout-report", description="read-only autonomous-closeout evidence summary"
    )
    p.add_argument("collab", help="collab name (registry) or path")
    p.add_argument("hid", help="handoff id")
    p.add_argument("--format", choices=("markdown", "json"), default="markdown")
    try:
        args = p.parse_args(argv)
    except SystemExit as e:
        return EXIT_OK if e.code in (0, None) else EXIT_USAGE
    try:
        summary = collect(args.collab, args.hid)
    except (cc.CollabError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE
    if summary["final_state"] is None and not summary["ledger_present"]:
        print(f"error: handoff {args.hid!r} not found (no state, no ledger)", file=sys.stderr)
        return EXIT_NOTFOUND
    sys.stdout.write(render_markdown(summary) if args.format == "markdown" else render_json(summary))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
