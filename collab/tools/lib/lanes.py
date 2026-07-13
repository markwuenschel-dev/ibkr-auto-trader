"""lanes — the adversarial-lane runner (collab-kit ARCHITECTURE.md §10 pipeline / §18.2, autonomous rev).

§10 described a breaker→refute pipeline as a Claude-Code ``Workflow`` (``diff-regression-hunt.workflow.js``)
that was never built — against the stdlib-only tenet (§1). This is that pipeline re-homed as a stdlib runner
over the driver's already-hardened backend substrate (``autopilot._cli_runner``: ``shell=False``, prompt on
stdin, output capped at the process boundary, under a timeout — [C39]):

    per required lane:
      stage 1 breaker  → an agent tries to BREAK the change (concrete trigger, not opinion)
      stage 2 verify   → an INDEPENDENT verifier tries to REFUTE each finding, defaulting REJECTED
                         unless it cites an exact code path + a concrete trigger
    → verification ledger  <collab>/autopilot/verification/<hid>.ledger.json

**Independence is structural** ([C36] separation of authority, §18): the builder (`from`), the breaker, and
the verifier must be three distinct seats — a seat can never verify its own work. Agent stdout is untrusted
DATA ([C38]): breaker/verifier text is sanitized and stored as artifacts; only the machine-parsed verdict
markers drive the ledger. The ledger is what the done-contract (§18.3) consumes — this module never
transitions handoff state.
"""

from __future__ import annotations

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_LIB = str(Path(__file__).resolve().parent)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import autopilot as ap  # noqa: E402  (base module — never imports lanes/done_contract at top, so no cycle)
import collab_common as cc  # noqa: E402
import contracts  # noqa: E402
import gate_runner as gr  # noqa: E402
import handoff_core as hc  # noqa: E402
import run_budget as rb  # noqa: E402

# Machine-parseable verdict markers (agent stdout is DATA — only these drive control state, [C38]).
_FINDING_RE = re.compile(r"^\s*FINDING:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_NOFINDING_RE = re.compile(r"^\s*NO-?FINDING\b", re.IGNORECASE | re.MULTILINE)
_CONFIRMED_RE = re.compile(r"^\s*VERDICT:\s*CONFIRMED\b", re.IGNORECASE | re.MULTILINE)

#: Cap on concurrently-running guardrail lanes (each is an independent breaker->verifier subprocess pair).
_MAX_LANE_WORKERS = 6


# --------------------------------------------------------------------------- #
# lane configuration (risk class -> required lanes)
# --------------------------------------------------------------------------- #


def load_lanes(path: str | Path | None = None) -> dict:
    """Load ``telemetry/lanes.json`` (risk classes + guardrail→lane map). ``{}`` if absent/unresolvable."""
    if path is None:
        try:
            path = Path(cc.resolve_kit_root()) / "telemetry" / "lanes.json"
        except cc.CollabError:
            return {}
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def required_lanes(guardrails, cfg: dict) -> list[str]:
    """The lanes a handoff must pass, from its ``guardrails``: the union of every risk class whose trigger
    guardrails are a subset of the handoff's, plus any per-guardrail lanes. Sorted, deterministic."""
    g = {str(x).strip().casefold() for x in (guardrails or [])}
    lanes: set[str] = set()
    for rc in (cfg.get("risk_classes") or {}).values():
        trig = {str(x).casefold() for x in rc.get("guardrails", [])}
        if trig and trig <= g:
            lanes.update(rc.get("required_lanes", []))
    gmap = cfg.get("guardrail_lanes") or {}
    for guard in g:
        lanes.update(gmap.get(guard, []))
    return sorted(lanes)


# --------------------------------------------------------------------------- #
# the ledger
# --------------------------------------------------------------------------- #


def _cand_slug(candidate_id: str) -> str:
    return cc.slugify(candidate_id.replace("cand:", "cand-")[:64])


def ledger_path(collab, hid: str, candidate_id: str | None = None) -> Path:
    """The verification ledger path. With a ``candidate_id`` it is an IMMUTABLE per-candidate ledger
    (``verification/<hid>/<candidate>.ledger.json``, ADR-0002 D3) — never overwritten by a later
    candidate; without one it is the legacy per-handoff file (kept for pre-candidate callers)."""
    base = Path(collab) / "autopilot" / "verification"
    if candidate_id:
        return base / cc.slugify(hid) / f"{_cand_slug(candidate_id)}.ledger.json"
    return base / f"{cc.slugify(hid)}.ledger.json"


def write_ledger(collab, hid: str, ledger: dict, candidate_id: str | None = None) -> Path:
    p = ledger_path(collab, hid, candidate_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    cc.safe_write(p, json.dumps(ledger, indent=2, sort_keys=True) + "\n")  # atomic (§16)
    return p


def read_ledger(collab, hid: str, candidate_id: str | None = None) -> dict | None:
    p = ledger_path(collab, hid, candidate_id)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _cmd_str(cmd) -> str:
    """A redacted, human-readable rendering of a seat argv for an infrastructure-error cause."""
    return " ".join(str(a) for a in (cmd or []))


def _dispatch(runner, cmd, prompt, *, timeout, unset_env, budget):
    """Charge one verification model call (ADR-0002 D6: reserve BEFORE dispatch, spent even if it then
    fails), then run the backend. `BudgetExceeded` / `CollabError` propagate to the lane handler."""
    if budget is not None:
        budget.charge(rb.VERIFICATION_CALL)
    return runner(cmd, prompt, timeout=timeout, unset_env=unset_env)


# --------------------------------------------------------------------------- #
# independence + prompts
# --------------------------------------------------------------------------- #


def _assert_independent(builder_seat: str, breaker_seat: str, verifier_seat: str) -> None:
    """Separation of authority (§18): builder, breaker, verifier must be three distinct seats."""
    trio = [str(s).strip().casefold() for s in (builder_seat, breaker_seat, verifier_seat)]
    if "" in trio or len(set(trio)) != 3:
        raise cc.CollabError(
            f"lane independence violated: builder={builder_seat!r} breaker={breaker_seat!r} "
            f"verifier={verifier_seat!r} must be three distinct non-empty seats (no self-verification)")


def _breaker_system(lane: str, seat_system: str | None) -> str:
    base = (seat_system.strip() + "\n\n") if seat_system else ""
    return (base + f"You are the BREAKER for the '{lane}' adversarial lane. Try to BREAK the change below. "
            "For each real defect emit one line 'FINDING: <exact code path> -> <concrete trigger: the inputs "
            "that produce the wrong output/crash>'. An opinion without a concrete reproducing trigger does "
            "not count. If you find nothing, emit exactly 'NO-FINDING'.")


def _verifier_system(lane: str, seat_system: str | None) -> str:
    base = (seat_system.strip() + "\n\n") if seat_system else ""
    return (base + f"You are an INDEPENDENT VERIFIER for the '{lane}' lane. Try to REFUTE the finding below. "
            "Default to REJECTED. Emit 'VERDICT: CONFIRMED <path> <trigger>' ONLY if you can cite an exact "
            "code path AND a concrete reproducing trigger; otherwise emit 'VERDICT: REFUTED'.")


# --------------------------------------------------------------------------- #
# the runner
# --------------------------------------------------------------------------- #


def run_lane(collab, hid: str, lane: str, *, seats: dict, breaker_seat: str, verifier_seat: str,
             builder_seat: str, runner=ap._cli_runner, log: str | None = None,
             budget=None) -> dict:
    """Run one adversarial lane (breaker → independent verifier) and return its result dict.

    When a ``budget`` is supplied, every breaker/verifier dispatch is charged a ``VERIFICATION_CALL``
    BEFORE it runs and the verifier loop is capped at ``max_findings_per_lane`` (ADR-0002 D7); the
    un-verified excess is surfaced (``overflow``/``unverified``), never dropped. A backend that exits
    nonzero (e.g. an incompatible seat argv) is captured as a structured ``tool_error`` — an
    ``infrastructure_blocked`` signal — instead of raising and killing the fan-out."""
    _assert_independent(builder_seat, breaker_seat, verifier_seat)
    log = log or ap._log_default(collab)
    _, path = hc._reconcile(collab, hid)
    if path is None:
        raise hc.HandoffNotFound(f"handoff {hid} not found")
    content = ap._substance(collab, Path(path))  # the change under test (pointer-deref, path-constrained)
    bcfg, vcfg = ap._cli_seat(seats, breaker_seat), ap._cli_seat(seats, verifier_seat)
    if bcfg is None or vcfg is None:
        raise cc.CollabError(f"lane {lane}: breaker/verifier must both be CLI seats")
    rid = ap._run_id(collab)

    def _tool_error(seat, cfg, exc) -> dict:
        return {"lane": lane, "ran": False, "confirmed": [], "refuted": [], "overflow": 0,
                "unverified": [], "tool_error": {"lane": lane, "seat": seat,
                                                 "cmd": _cmd_str(cfg.get("cmd")), "error": str(exc)}}

    def _incomplete(which, confirmed, refuted, unverified) -> dict:
        return {"lane": lane, "ran": False, "confirmed": confirmed, "refuted": refuted,
                "overflow": len(unverified), "unverified": unverified,
                "incomplete": {"reason": "budget", "which": which}}

    # ---- stage 1: breaker ------------------------------------------------- #
    try:
        braw = _dispatch(runner, list(bcfg["cmd"]),
                         ap._build_prompt(_breaker_system(lane, bcfg.get("system")), content),
                         timeout=float(bcfg.get("timeout", ap._DEFAULT_TIMEOUT)),
                         unset_env=bcfg.get("unset_env"), budget=budget)
    except rb.BudgetExceeded as e:
        return _incomplete(e.which, [], [], [])
    except cc.CollabError as e:
        return _tool_error(breaker_seat, bcfg, e)
    breaker_artifact = ap._write_reply(collab, f"{breaker_seat}-breaker-{lane}", ap._sanitize(braw))
    findings = [] if _NOFINDING_RE.search(braw) else _FINDING_RE.findall(braw)

    # ---- findings cap (ADR-0002 D7): verify at most max_findings_per_lane; surface the excess ---- #
    overflow, unverified = 0, []
    if budget is not None and findings:
        split = budget.cap_lane_findings(len(findings))
        if split["overflow"] > 0:
            unverified = findings[split["verify"]:]
            overflow = split["overflow"]
            findings = findings[: split["verify"]]

    ap._emit_safe(ap._trace.emit, log, run_id=rid, stage="autopilot.lane",
                  role=breaker_seat, artifact=f"handoff:{hid}", span_id=f"{hid}:lane:{lane}:breaker",
                  decision={"action": "breaker",
                            "reason_codes": [f"lane:{lane}", f"findings:{len(findings)}"],
                            "confidence": None})

    # ---- stage 2: independent verifier per finding ------------------------ #
    confirmed, refuted, verifier_artifact = [], [], None
    for idx, finding in enumerate(findings, 1):
        vprompt = ap._build_prompt(_verifier_system(lane, vcfg.get("system")),
                                   f"Finding to refute:\n{finding}\n\nChange under test:\n{content}")
        try:
            vraw = _dispatch(runner, list(vcfg["cmd"]), vprompt,
                             timeout=float(vcfg.get("timeout", ap._DEFAULT_TIMEOUT)),
                             unset_env=vcfg.get("unset_env"), budget=budget)
        except rb.BudgetExceeded as e:
            return _incomplete(e.which, confirmed, refuted, unverified + list(findings[idx - 1:]))
        except cc.CollabError as e:
            return _tool_error(verifier_seat, vcfg, e)
        verifier_artifact = ap._write_reply(collab, f"{verifier_seat}-verify-{lane}", ap._sanitize(vraw))
        is_confirmed = bool(_CONFIRMED_RE.search(vraw))
        (confirmed if is_confirmed else refuted).append(finding)
        ap._emit_safe(ap._trace.emit, log, run_id=rid, stage="autopilot.lane",
                      role=verifier_seat, artifact=f"handoff:{hid}", span_id=f"{hid}:lane:{lane}:v{idx}",
                      decision={"action": "verdict",
                                "reason_codes": [f"lane:{lane}", f"finding:{idx}/{len(findings)}",
                                                 "verdict:CONFIRMED" if is_confirmed else "verdict:REFUTED"],
                                "confidence": None})

    ap._emit_safe(ap._trace.emit, log, run_id=rid, stage="autopilot.lane",
                  role=verifier_seat, artifact=f"handoff:{hid}", span_id=f"{hid}:lane:{lane}",
                  decision={"action": "lane",
                            "reason_codes": [f"lane:{lane}", f"confirmed:{len(confirmed)}",
                                             f"refuted:{len(refuted)}"], "confidence": None})
    return {"lane": lane, "ran": True, "breaker_seat": breaker_seat, "verifier_seat": verifier_seat,
            "breaker_artifact": breaker_artifact, "verifier_artifact": verifier_artifact,
            "confirmed": confirmed, "refuted": refuted, "overflow": overflow, "unverified": unverified}


def run_lanes(collab, hid: str, *, seats: dict, breaker_seat: str, verifier_seat: str,
              builder_seat: str | None = None, guardrails=None, lanes_cfg: dict | None = None,
              source_roots=None, source_base=None, tests: dict | None = None,
              reviewer_seat: str | None = None, test_path=None,
              runner=ap._cli_runner, log: str | None = None,
              budget=None, candidate_id: str | None = None) -> dict:
    """Run every required lane for a handoff, assemble the verification ledger, write it, and return it.

    ``builder_seat``/``guardrails`` default to the handoff's ``from``/``guardrails`` frontmatter. Confirmed
    findings become ledger ``blockers`` (initially ``fixed=false`` — the done-contract, §18.3, requires each
    fixed + regression-tested before closeout). ``source_roots``+``source_base`` attach the source manifest.

    With a ``candidate_id`` the ledger is written to an IMMUTABLE per-candidate path and, once written,
    reused verbatim on a re-run of the identical candidate (zero new model calls). With a ``budget`` the
    lanes charge ``VERIFICATION_CALL`` per breaker/verifier and are capped at ``max_findings_per_lane``;
    a lane tool-failure surfaces as ``tool_error`` (infrastructure_blocked) and an over-cap or
    budget-exhausted lane as ``incomplete``/``overflow`` (verification_incomplete) — never a silent pass.
    """
    cfg = lanes_cfg if lanes_cfg is not None else load_lanes()
    _, path = hc._reconcile(collab, hid)
    if path is None:
        raise hc.HandoffNotFound(f"handoff {hid} not found")
    fm = contracts.parse_handoff(Path(path)).get("frontmatter") or {}
    if builder_seat is None:
        builder_seat = (fm.get("from") or "").strip()
    if guardrails is None:
        guardrails = fm.get("guardrails") or []

    lanes = required_lanes(guardrails, cfg)
    manifest = gr.source_manifest(source_roots, source_base) if (source_roots and source_base) else {}
    log = log or ap._log_default(collab)

    # Per-candidate cache: an identical candidate id means identical source + plan, hence identical
    # findings — the immutable ledger already on disk is reused VERBATIM (no reviewer/lane calls). This is
    # the only cache that short-circuits the whole ledger; the candidate ledger is immutable by design.
    if candidate_id is not None:
        existing = read_ledger(collab, hid, candidate_id=candidate_id)
        if existing is not None:
            ap._emit_safe(ap._trace.emit, log, run_id=ap._run_id(collab), stage="autopilot.lane",
                          role="autopilot", artifact=f"handoff:{hid}", span_id=f"{hid}:lanes:cached",
                          decision={"action": "lanes_cached",
                                    "reason_codes": [f"lanes:{len(lanes)}", "candidate-unchanged"],
                                    "confidence": None})
            return existing

    results = None
    if candidate_id is None:
        # Legacy per-handoff cache: reuse the prior LANE RESULTS if they ran the SAME lanes over an
        # IDENTICAL source manifest (source-unchanged => findings-unchanged), but still fall through to
        # rebuild the ledger so a fresh reviewer preflight / tests block is re-attached (a lane-cache hit
        # must never resurrect a stale preflight — cond. 11). Skips only the slow breaker/verifier suite.
        prior = read_ledger(collab, hid)
        prior_by_lane = ({r.get("lane"): r for r in (prior.get("lanes") or [])}
                         if isinstance(prior, dict) else {})
        if manifest and prior and prior.get("source_manifest") == manifest \
                and all(prior_by_lane.get(lane, {}).get("ran") for lane in lanes):
            ap._emit_safe(ap._trace.emit, log, run_id=ap._run_id(collab), stage="autopilot.lane",
                          role="autopilot", artifact=f"handoff:{hid}", span_id=f"{hid}:lanes:cached",
                          decision={"action": "lanes_cached",
                                    "reason_codes": [f"lanes:{len(lanes)}", "source-unchanged"],
                                    "confidence": None})
            results = [prior_by_lane[lane] for lane in lanes]

    if results is None:
        if lanes:
            # Parallel fan-out: the guardrail lanes are independent — shared *read-only* source, unique
            # per-lane artifact names, and trace.emit serializes its append under a file lock — so
            # wall-clock becomes the slowest single lane, not the sum. ``executor.map`` preserves order.
            # A shared RunBudget handle is charged atomically under its own lock (single process/lease).
            with ThreadPoolExecutor(max_workers=min(len(lanes), _MAX_LANE_WORKERS)) as ex:
                results = list(ex.map(
                    lambda lane: run_lane(collab, hid, lane, seats=seats, breaker_seat=breaker_seat,
                                          verifier_seat=verifier_seat, builder_seat=builder_seat,
                                          runner=runner, log=log, budget=budget),
                    lanes))
        else:
            results = []

    blockers = [{"id": f"{r['lane']}-{i + 1}", "lane": r["lane"], "description": f,
                 "fixed": False, "regression_test": None}
                for r in results for i, f in enumerate(r.get("confirmed") or [])]
    # Aggregate the truthful-terminal signals (ADR-0003 D1): the first tool failure -> infrastructure;
    # any over-cap / budget-exhausted lane -> verification_incomplete (the un-verified excess is named).
    tool_error = next((r["tool_error"] for r in results if r.get("tool_error")), None)
    overflow = sum(int(r.get("overflow", 0) or 0) for r in results)
    unverified = [u for r in results for u in (r.get("unverified") or [])]
    incomplete = bool(overflow) or any(r.get("incomplete") for r in results)

    preflight = (ap._capture_preflight(source_base, test_path, reviewer_seat, manifest)
                 if (reviewer_seat and source_base) else None)
    ledger = {
        "hid": hid,
        "candidate_id": candidate_id,
        "generated_ts": ap._now_utc(),
        "guardrails": list(guardrails),
        "builder_seat": builder_seat,
        "reviewer_seat": verifier_seat,
        "source_base": str(source_base) if source_base else None,
        "source_manifest": manifest,
        "tests": tests or {"passed": None, "run_id": None},
        "reviewer_preflight": preflight,
        "lanes": results,
        "blockers": blockers,
        "accepted_residuals": [],
        "tool_error": tool_error,
        "overflow": overflow,
        "unverified": unverified,
        "incomplete": incomplete,
    }
    write_ledger(collab, hid, ledger, candidate_id)
    return ledger
