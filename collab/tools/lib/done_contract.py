"""done_contract — the Autonomous Done-Transition Contract evaluator (ARCHITECTURE.md §18.3, autonomous rev).

``evaluate()`` decides whether a handoff MAY be advanced to ``done/`` autonomously by checking the twelve
conditions of the contract against the verification ledger (lanes.py), the live source tree, and the
handoff state. It is **pure**: it reads evidence and returns a verdict, and **never transitions state** — the
driver performs the ``hc.done`` CAS only on a satisfied verdict, so the ``[[SIGNOFF]]`` token is necessary
but not sufficient ([C36]/[C38], §18). Separation of authority is condition 2: the approving reviewer must
be a different seat than the builder (no self-approval).
"""

from __future__ import annotations

import calendar
import hashlib
import json
import os
import sys
import time
from pathlib import Path

_LIB = str(Path(__file__).resolve().parent)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import gate_runner as gr  # noqa: E402
import handoff_core as hc  # noqa: E402
import lanes  # noqa: E402  (lanes->autopilot; autopilot must not import done_contract at top — no cycle)

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _verdict(conditions: list[dict]) -> dict:
    satisfied = all(c["status"] == "pass" for c in conditions)
    pre = json.dumps(conditions, sort_keys=True, separators=(",", ":"))
    return {
        "satisfied": satisfied,
        "conditions": conditions,
        "hash": hashlib.sha256(pre.encode("utf-8")).hexdigest(),
    }


def _fresh_and_not_scratchpad(ledger: dict) -> tuple[bool, str]:
    """Condition 8: evidence is not throwaway scratchpad output, and the source is no newer than the
    ledger that attests it (a source edit after the ledger => stale evidence)."""
    manifest = ledger.get("source_manifest") or {}
    base_raw = ledger.get("source_base")
    if not manifest or not base_raw:
        return False, "no source manifest/base to attest freshness"
    base_res = Path(base_raw).resolve()
    if any(part.casefold() == "scratchpad" for part in base_res.parts):
        return False, f"source_base under a scratchpad dir (throwaway evidence): {base_raw}"
    try:
        ledger_epoch = calendar.timegm(time.strptime(ledger.get("generated_ts", ""), _TS_FMT))
    except ValueError, TypeError:
        return False, "unparseable ledger timestamp"
    newest = 0.0
    for rel in manifest:
        try:
            newest = max(newest, (base_res / rel).stat().st_mtime)
        except OSError:
            return False, f"attested file missing: {rel}"
    if newest > ledger_epoch + 2:  # 2s slack for filesystem/clock granularity
        return False, "source modified after the ledger was generated (stale evidence)"
    return True, "evidence is tracked (non-scratchpad) and no newer than the ledger"


def _reviewer_preflight_ok(ledger: dict, reviewer_seat: str) -> tuple[bool, str]:
    """Condition 11: the signing reviewer proved REPO AWARENESS before autonomous sign-off ([C42], §18).

    A text-only "I reviewed it" is not repo-aware review — the driver captures a trusted preflight (git
    toplevel + status/diff + ``pytest --collect-only``) and the files under review. Fail-closed if the
    block is missing or any element is unproven. Enforced HERE (the contract), not in a harness, so no
    other ``hc.done`` caller can bypass it."""
    pre = ledger.get("reviewer_preflight")
    if not isinstance(pre, dict):
        return False, "no reviewer_preflight block (repo awareness not proven)"
    if (pre.get("seat") or "").strip().casefold() != (reviewer_seat or "").strip().casefold():
        return False, f"preflight seat {pre.get('seat')!r} != signing reviewer {reviewer_seat!r}"
    if pre.get("repo_access") is not True:
        return False, "repo_access not true (no git repo awareness)"
    cmds = pre.get("commands") or {}
    if (cmds.get("git_rev_parse") or {}).get("exit_code") != 0:
        return False, "git rev-parse --show-toplevel did not succeed"
    if (cmds.get("pytest_collect_only") or {}).get("exit_code") != 0:
        return False, "pytest --collect-only did not succeed"
    files = pre.get("inspected_files")
    if not isinstance(files, list) or not files:
        return False, "no inspected/cited file paths"
    root = pre.get("repo_root")
    try:
        root_res = Path(root).resolve() if root else None
    except OSError, ValueError:
        root_res = None
    if root_res is None:
        return False, "no repo_root to bound inspected files"
    for f in files:
        if not isinstance(f, str) or not f or os.path.isabs(f) or f.startswith(("/", "\\")):
            return False, f"inspected path not relative: {f!r}"
        try:
            res = (root_res / f).resolve()
        except OSError, ValueError:
            return False, f"inspected path unresolvable: {f!r}"
        if root_res != res and root_res not in res.parents:  # escapes repo root — refuse
            return False, f"inspected path escapes repo root: {f!r}"
    return True, f"reviewer {reviewer_seat!r} proved repo awareness ({len(files)} inspected files)"


def _conformance_ok(ledger: dict, *, candidate_id=None) -> tuple[bool, str]:
    """Condition 12: the ledger carries a SATISFIED, candidate-bound conformance record (ADR-0005).

    Fail-closed at every step, and enforced here rather than in a harness for the same reason as
    condition 11: no other ``hc.done`` caller can then bypass it.

    Requires, in order:
      * a conformance record exists — absence is refusal, never "nothing to check";
      * it is bound to THIS candidate — an unbound record could be replayed from another candidate,
        which is the replay condition 5's ``matches_checkout`` exists to stop, one level up;
      * it carries a contract digest and at least one requirement id — a contract proving nothing
        would satisfy this vacuously, exactly how condition 3 passed with zero required passes;
      * no incomplete evidence — disagreement/malformed/unresolvable is UNKNOWN, and unknown is not a
        pass;
      * every result is a validated ``met``, with coverage matching the declared ids exactly.
    """
    record = ledger.get("conformance")
    if not isinstance(record, dict):
        return False, "no conformance record in the ledger"
    if not record.get("contract_digest"):
        return False, "conformance record carries no contract digest"
    bound = record.get("candidate_id")
    if candidate_id is not None and bound != candidate_id:
        return False, f"conformance evidence is bound to {bound!r}, not this candidate {candidate_id!r}"
    if record.get("incomplete"):
        inc = record["incomplete"]
        return False, f"conformance incomplete ({inc.get('reason')}): {inc.get('detail')}"
    ids = record.get("requirement_ids") or []
    results = record.get("results") or []
    if not ids:
        return False, "conformance contract declares no requirements"
    unmet = [r.get("id") for r in results if r.get("status") != "met"]
    if unmet:
        return False, f"requirements not met: {sorted(unmet)}"
    covered = sorted({r.get("id") for r in results})
    if covered != sorted(ids):
        return False, f"conformance coverage mismatch: declared={sorted(ids)} covered={covered}"
    return True, f"{len(ids)} requirement(s) independently confirmed met: {sorted(ids)}"


def evaluate(
    collab,
    hid: str,
    *,
    seats: dict,
    reviewer_seat: str,
    builder_seat: str,
    lanes_cfg: dict | None = None,
    candidate_id: str | None = None,
) -> dict:
    """Return ``{"satisfied": bool, "conditions": [{id,name,status,detail}...], "hash": sha256}``.

    Pure — never mutates handoff state. ``reviewer_seat`` is the seat proposing the sign-off; ``builder_seat``
    is the handoff author (the driver passes the inbound's ``from``). With a ``candidate_id`` it attests
    the IMMUTABLE per-candidate ledger for that exact candidate (ADR-0002 D3); without one it reads the
    legacy per-handoff ledger.
    """
    conds: list[dict] = []

    def _c(cid: int, name: str, ok, detail) -> bool:
        conds.append({"id": cid, "name": name, "status": "pass" if ok else "fail", "detail": str(detail)})
        return bool(ok)

    cfg = lanes_cfg if lanes_cfg is not None else lanes.load_lanes()
    ledger = lanes.read_ledger(collab, hid, candidate_id=candidate_id)
    rseat = (reviewer_seat or "").strip().casefold()
    bseat = (builder_seat or "").strip().casefold()
    state = hc.state_of(collab, hid)

    # 1 — builder implementation evidence
    _c(
        1,
        "builder-evidence",
        ledger is not None and bool((ledger or {}).get("source_manifest")),
        "ledger + non-empty source manifest present" if ledger else "no verification ledger",
    )

    if ledger is None:  # nothing to attest against — conditions 2..8 cannot hold
        for cid, name in [
            (2, "independent-approver"),
            (3, "lanes-ran"),
            (4, "blockers-fixed"),
            (5, "blocker-regressions"),
            (6, "residuals-explicit"),
            (7, "source==tested"),
            (8, "no-stale-evidence"),
        ]:
            _c(cid, name, False, "no verification ledger")
        _c(9, "approval-recorded", bool(rseat), "reviewer seat present" if rseat else "no reviewer seat")
        _c(10, "state-machine-safety", state in ("pending", "claimed"), f"state={state}")
        _c(11, "reviewer-repo-preflight", False, "no verification ledger")
        _c(12, "spec-conformance", False, "no verification ledger")
        return _verdict(conds)

    blockers = ledger.get("blockers") or []
    l_reviewer = (ledger.get("reviewer_seat") or "").strip().casefold()
    l_builder = (ledger.get("builder_seat") or "").strip().casefold()

    # 2 — independent approver (no self-approval)
    _c(
        2,
        "independent-approver",
        bool(rseat)
        and bool(bseat)
        and rseat != bseat
        and l_reviewer
        and l_builder
        and l_reviewer != l_builder,
        f"reviewer={reviewer_seat!r} builder={builder_seat!r} "
        f"ledger(reviewer={l_reviewer!r},builder={l_builder!r})",
    )

    # 3 — a RESOLVED v2 plan is present AND every pass in it ran.  A v2 ledger owns its plan, so a
    # later config edit cannot change what this candidate had to prove.  Incomplete/tool-error
    # evidence cannot be treated as a clean pass.
    #
    # The plan's PRESENCE is load-bearing, not decoration (ADR-0005). A legacy ledger carries no
    # ``verification_plan``, so ``ledger_required_passes`` falls back to mutable current config —
    # which is how a candidate assessed by the generic fan-out (no seat validation, a text-only
    # adapter admissible as a verifier) could still satisfy this condition. ``lanes.run_lanes``
    # remains available for direct/manual use; it just cannot reach autonomous done.
    plan = ledger.get("verification_plan")
    plan_ok = isinstance(plan, dict) and bool(ledger.get("verification_plan_digest"))
    required = set(lanes.ledger_required_passes(ledger, cfg))
    ran = lanes.ledger_ran_passes(ledger)
    complete = not bool(ledger.get("incomplete")) and not bool(ledger.get("tool_error"))
    _c(
        3,
        "lanes-ran",
        plan_ok and required <= ran and complete,
        f"resolved_plan={plan_ok} required={sorted(required)} ran={sorted(ran)} complete={complete}",
    )

    # 4 — every confirmed blocker fixed
    unfixed = [b.get("id") for b in blockers if not b.get("fixed")]
    _c(4, "blockers-fixed", not unfixed, f"unfixed={unfixed}")

    # 5 — every blocker has a regression AND the checkout is AUTHORITATIVELY green.
    #
    # ``tests.passed is True`` used to be the whole test here. That boolean can come from a
    # pytest-only run, which says nothing about lint or types, and the contract read it as though it
    # attested the checkout (2026-07-15). ``verification.is_green`` is the only reader permitted to
    # conclude "this checkout passed": it demands scripts/verify.py, exit 0, over a checkout that did
    # not move mid-run. A pytest-only record cannot satisfy it by construction.
    # A green receipt from an EARLIER checkout would otherwise replay forever, closing work it never
    # examined; matches_checkout pins it to the tree on disk now.
    import verification as _v

    verification = ledger.get("tests") or {}
    green = _v.is_green(verification)
    fresh, fresh_detail = _v.matches_checkout(verification, ledger.get("source_base") or ".")
    missing_reg = [b.get("id") for b in blockers if not b.get("regression_test")]
    _c(
        5,
        "blocker-regressions",
        not missing_reg and green and fresh,
        f"verification={_v.label_of(verification)} green={green} "
        f"checkout={fresh_detail} missing_regressions={missing_reg}",
    )

    # 6 — accepted residuals explicit
    _c(
        6,
        "residuals-explicit",
        isinstance(ledger.get("accepted_residuals"), list),
        "accepted_residuals list present",
    )

    # 7 — source == tested
    ok7, detail7 = gr.verify_manifest(ledger.get("source_manifest") or {}, ledger.get("source_base"))
    _c(7, "source==tested", ok7, detail7)

    # 8 — no stale / scratchpad evidence
    ok8, detail8 = _fresh_and_not_scratchpad(ledger)
    _c(8, "no-stale-evidence", ok8, detail8)

    # 9 — approval event will be recorded (driver emits on_autonomous_done on the transition)
    _c(9, "approval-recorded", bool(rseat), "reviewer seat present" if rseat else "no reviewer seat")

    # 10 — same state-machine safety as manual closeout (the driver uses the identical hc.done CAS)
    _c(10, "state-machine-safety", state in ("pending", "claimed"), f"state={state}")

    # 11 — the signing reviewer proved repo awareness (repo-aware preflight, not text-only review)
    ok11, detail11 = _reviewer_preflight_ok(ledger, reviewer_seat)
    _c(11, "reviewer-repo-preflight", ok11, detail11)

    # 12 — every DECLARED requirement was independently confirmed present (ADR-0005).
    #
    # Conditions 1..11 gate mechanical evidence: a manifest, seat independence, a green authoritative
    # gate, freshness. None of them can see a requirement the change simply OMITS -- nothing is wrong,
    # something is absent, and absence raises no finding. On 2026-07-16 handoff 035 satisfied every
    # one of those conditions with a binding that was structurally None.
    #
    # Sourced from the ledger, like every other condition: the reviewer's [met] prose remains advisory
    # (narrative.py) and is NOT read here. Prose that grades itself is what failed.
    ok12, detail12 = _conformance_ok(ledger, candidate_id=candidate_id)
    _c(12, "spec-conformance", ok12, detail12)

    return _verdict(conds)
