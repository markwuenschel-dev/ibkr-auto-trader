"""self_host_smoke — the self-hosting production harness (collab-kit slice 7).

Proves collab-kit can close a REAL slice through its own autonomous machinery and leave an auditable
evidence bundle — not a happy-path demo. It creates a DISPOSABLE collab workspace, seeds a real slice
(the shipped ``closeout_report.py`` is the source under review), and drives the genuine closeout
decision with the SAME primitives the driver uses:

    claim -> ap._autoclose_ledger (real tests + adversarial lanes + reviewer repo-preflight)
          -> done_contract.evaluate  -> hc.done + on_autonomous_done  ONLY on a satisfied verdict.

The transition is caused solely by ``done_contract.evaluate(...).satisfied`` ([C36]/§18). ``--inject``
runs deliberate NEGATIVE scenarios (self-approval, source drift, test failure, a confirmed unresolved
finding, a missing ledger, a missing reviewer preflight) — each must leave the handoff ``claimed`` and
never reach ``done``. A closeout evidence bundle is written under ``<collab>/autopilot/closeout/<hid>/``.

Disposable by default (a temp workspace, real collab state untouched). ``--real`` runs the same decision
against the LIVE repo source + the real test suite (manual proof, slow — not run in CI). Agents are always
scripted (no network).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_LIB = str(Path(__file__).resolve().parent)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import autopilot as ap  # noqa: E402
import closeout_report as cr  # noqa: E402
import collab_common as cc  # noqa: E402
import done_contract as dcon  # noqa: E402
import handoff_core as hc  # noqa: E402
import handoff_events as he  # noqa: E402
import lanes  # noqa: E402

EXIT_OK, EXIT_USAGE = 0, 1

SCENARIOS = (
    "clean",
    "self-approval",
    "missing-ledger",
    "source-drift",
    "test-failure",
    "missing-preflight",
    "confirmed-finding",
)
GUARDRAILS = ["path-safety", "data-integrity", "bounded-autonomy", "untrusted-agent-output"]
_TITLE = "Add closeout-report command for autonomous closeout evidence summaries"
BUNDLE_FILES = (
    "summary.json",
    "reviewer.md",
    "lanes.json",
    "tests.json",
    "source_manifest.json",
    "done_contract.json",
)


def _seats() -> dict:
    """Builder + independent breaker/verifier + a can_sign_off reviewer (three distinct lane seats)."""
    return {
        "builder": {"backend": "cli", "cmd": ["fake-builder"], "system": "builder"},
        "grok": {"backend": "cli", "cmd": ["fake-grok"], "system": "breaker"},
        "gemini": {"backend": "cli", "cmd": ["fake-gemini"], "system": "verifier"},
        "reviewer": {"backend": "cli", "cmd": ["fake-reviewer"], "system": "reviewer", "can_sign_off": True},
    }


def _scripted_runner(inject: str):
    """A deterministic fake agent (no network), routed by the seat's cmd[0]. The breaker/verifier drive the
    lanes; ``confirmed-finding`` makes the breaker report a defect the verifier CONFIRMS."""

    def run(cmd, prompt, *, timeout, **kw):
        who = cmd[0]
        if "reviewer" in who:
            return "Reviewed against source; inspected closeout_report.py.\n[[SIGNOFF]]"
        if "grok" in who or "breaker" in who:
            return (
                "FINDING: closeout_report.collect -> a crafted ledger yields a wrong verdict"
                if inject == "confirmed-finding"
                else "NO-FINDING"
            )
        if "gemini" in who or "verifier" in who:
            return (
                "VERDICT: CONFIRMED closeout_report.collect crafted-ledger trigger reproduces"
                if inject == "confirmed-finding"
                else "VERDICT: REFUTED"
            )
        return "ok"

    return run


def _inject_guardrails(collab, hid: str, guardrails: list[str]) -> None:
    """Add a ``guardrails:`` frontmatter line so the lane runner derives the autopilot risk class."""
    _, p = hc._reconcile(collab, hid)
    txt = Path(p).read_text("utf-8")
    txt = re.sub(r"(?m)^(status:.*\n)", r"\1guardrails: [" + ", ".join(guardrails) + "]\n", txt, count=1)
    Path(p).write_text(txt, "utf-8")


def _corruptor(inject: str, collab, hid: str, src_base: Path):
    """Return a callable that corrupts one piece of evidence AFTER the ledger is built (or ``None``)."""
    if inject == "source-drift":

        def c():
            (src_base / "closeout_report.py").write_text("# drift after manifest\n", encoding="utf-8")

        return c
    if inject == "missing-preflight":

        def c():
            led = lanes.read_ledger(collab, hid) or {}
            led["reviewer_preflight"] = None
            lanes.write_ledger(collab, hid, led)

        return c
    return None


def _write_bundle(collab, hid: str, verdict: dict) -> Path:
    """Write the auditable closeout bundle under ``<collab>/autopilot/closeout/<hid>/``. Reuses the shipped
    ``closeout_report`` feature to render ``summary.json`` + ``reviewer.md``."""
    ledger = lanes.read_ledger(collab, hid) or {}
    summary = cr.collect(collab, hid)
    d = Path(collab) / "autopilot" / "closeout" / cc.slugify(hid)
    d.mkdir(parents=True, exist_ok=True)

    def _w(name, obj):
        cc.safe_write(d / name, json.dumps(obj, indent=2, sort_keys=True) + "\n")

    _w("summary.json", summary)
    cc.safe_write(d / "reviewer.md", cr.render_markdown(summary))
    _w(
        "lanes.json",
        {
            "required": summary["lanes"]["required"],
            "ran": summary["lanes"]["ran"],
            "lanes": ledger.get("lanes") or [],
            "blockers": ledger.get("blockers") or [],
        },
    )
    _w("tests.json", ledger.get("tests") or {})
    _w(
        "source_manifest.json",
        {"source_base": ledger.get("source_base"), "source_manifest": ledger.get("source_manifest") or {}},
    )
    _w("done_contract.json", verdict)  # the verdict CAPTURED at decision time (state=claimed)
    return d


def run_smoke(*, inject: str = "clean", workspace=None, real: bool = False, collab=None) -> dict:
    """Run one disposable autonomous-closeout scenario end-to-end and return an auditable result dict."""
    if inject not in SCENARIOS:
        raise cc.CollabError(f"unknown scenario {inject!r}; choose one of {SCENARIOS}")

    kit = Path(cc.resolve_kit_root())
    ws = Path(workspace) if workspace else Path(tempfile.mkdtemp(prefix="collab-selfhost-"))
    ws.mkdir(parents=True, exist_ok=True)
    collab_p = Path(collab) if collab else (ws / "c")
    home = ws / "home"
    home.mkdir(parents=True, exist_ok=True)

    # --- seed the slice under review + the test evidence target ---
    if real:  # review the LIVE repo source + run the real suite (manual proof, not CI)
        src_base = kit
        source_roots = ["tools/lib/closeout_report.py"]
        test_file = kit / "tests" / "test_closeout_report.py"
    else:  # disposable: the shipped feature's bytes are the source under review
        src_base = ws / "src"
        src_base.mkdir(parents=True, exist_ok=True)
        shutil.copy2(kit / "tools" / "lib" / "closeout_report.py", src_base / "closeout_report.py")
        source_roots = ["*.py"]
        ok = inject != "test-failure"
        test_file = ws / "test_slice.py"
        test_file.write_text(f"def test_slice():\n    assert {ok!s}\n", encoding="utf-8")
        subprocess.run(["git", "init"], cwd=str(ws), capture_output=True)  # preflight git needs a repo

    # --- create + claim the real handoff ---
    builder = "reviewer" if inject == "self-approval" else "builder"  # self-approval: from == reviewer
    hid = hc.create(collab_p, to="reviewer", from_=builder, title=_TITLE, body="please review")["id"]
    _inject_guardrails(collab_p, hid, GUARDRAILS)
    hc.claim(collab_p, hid)

    seats = _seats()
    closeout = {
        "breaker": "grok",
        "verifier": "gemini",
        "source_base": str(src_base),
        "source_roots": source_roots,
        "test_path": str(test_file),
        # This synthetic slice's authoritative whole-checkout gate. done_contract condition 5 closes
        # only on an authoritative exit 0 -- ``test_path`` alone is a PARTIAL result and cannot close
        # a handoff (see collab/tests/test_verification.py). ``--inject test-failure`` makes this
        # command exit non-zero, which is what must keep the handoff ``claimed``.
        "verify_command": [sys.executable, "-m", "pytest", str(test_file), "-q"],
    }
    runner = _scripted_runner(inject)
    log = ap._log_default(collab_p)

    # --- the genuine closeout decision (the same primitives the driver's run_round uses) ---
    if inject != "missing-ledger":
        ap._autoclose_ledger(
            collab_p, hid, builder, closeout, seats=seats, runner=runner, log=log, reviewer_seat="reviewer"
        )
    corrupt = _corruptor(inject, collab_p, hid, src_base)
    if corrupt:
        corrupt()
    verdict = dcon.evaluate(collab_p, hid, seats=seats, reviewer_seat="reviewer", builder_seat=builder)
    if verdict["satisfied"]:
        hc.done(collab_p, hid)  # the transition is caused ONLY by a satisfied verdict
        ap._emit_safe(
            he.on_autonomous_done,
            log,
            ap._run_id(collab_p),
            hid,
            span_id=f"{hid}:signoff",
            parent_span_id=None,
            reviewer="reviewer",
            contract_hash=verdict["hash"],
        )

    final_state = hc.state_of(collab_p, hid)
    bundle_dir = _write_bundle(collab_p, hid, verdict)
    return {
        "scenario": inject,
        "real": real,
        "workspace": str(ws),
        "collab": str(collab_p),
        "home": str(home),
        "handoff_id": hid,
        "run_id": ap._run_id(collab_p),
        "final_state": final_state,
        "reached_done": final_state == "done",
        "satisfied": verdict["satisfied"],
        "contract_hash": verdict["hash"],
        "unmet": [c["name"] for c in verdict["conditions"] if c["status"] != "pass"],
        "bundle_dir": str(bundle_dir),
    }


def _render_result(r: dict) -> str:
    L = [
        f"self-host smoke — scenario '{r['scenario']}'{' (--real)' if r['real'] else ''}",
        f"  workspace : {r['workspace']}",
        f"  collab    : {r['collab']}",
        f"  handoff   : {r['handoff_id']}  (run {r['run_id']})",
        f"  satisfied : {r['satisfied']}",
        f"  final     : {r['final_state']}  (reached done: {r['reached_done']})",
        f"  bundle    : {r['bundle_dir']}",
    ]
    if r["unmet"]:
        L.append(f"  unmet     : {', '.join(r['unmet'])}")
    return "\n".join(L) + "\n"


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    p = argparse.ArgumentParser(
        prog="self-host-smoke", description="self-hosting autonomous-closeout harness (disposable)"
    )
    p.add_argument(
        "--inject",
        choices=SCENARIOS,
        default="clean",
        help="run a negative scenario (must be blocked); default 'clean' must reach done",
    )
    p.add_argument(
        "--real",
        action="store_true",
        help="review the LIVE repo source + run the real suite (manual, slow; not CI)",
    )
    p.add_argument("--collab", help="use this collab path instead of a disposable one (may touch real state)")
    p.add_argument("--workspace", help="use this workspace dir instead of a fresh temp dir")
    p.add_argument("--format", choices=("text", "json"), default="text")
    try:
        args = p.parse_args(argv)
    except SystemExit as e:
        return EXIT_OK if e.code in (0, None) else EXIT_USAGE
    try:
        r = run_smoke(inject=args.inject, real=args.real, collab=args.collab, workspace=args.workspace)
    except (cc.CollabError, hc.HandoffNotFound, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE
    sys.stdout.write(
        json.dumps(r, indent=2, sort_keys=True) + "\n" if args.format == "json" else _render_result(r)
    )
    # Exit code encodes the invariant: clean MUST reach done; a negative scenario MUST be blocked.
    expected_done = args.inject == "clean"
    return EXIT_OK if r["reached_done"] == expected_done else EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
