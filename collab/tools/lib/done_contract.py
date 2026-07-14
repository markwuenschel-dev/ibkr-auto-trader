"""done_contract — the Autonomous Done-Transition Contract evaluator (ARCHITECTURE.md §18.3, autonomous rev).

``evaluate()`` decides whether a handoff MAY be advanced to ``done/`` autonomously by checking the eleven
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

    # 3 — every pass in the immutable resolved plan ran.  A v2 ledger owns
    # its plan, so a later config edit cannot change what this candidate had to
    # prove.  Incomplete/tool-error evidence cannot be treated as a clean pass.
    required = set(lanes.ledger_required_passes(ledger, cfg))
    ran = lanes.ledger_ran_passes(ledger)
    complete = not bool(ledger.get("incomplete")) and not bool(ledger.get("tool_error"))
    _c(
        3,
        "lanes-ran",
        required <= ran and complete,
        f"required={sorted(required)} ran={sorted(ran)} complete={complete}",
    )

    # 4 — every confirmed blocker fixed
    unfixed = [b.get("id") for b in blockers if not b.get("fixed")]
    _c(4, "blockers-fixed", not unfixed, f"unfixed={unfixed}")

    # 5 — every blocker has a regression AND the recorded test run passed
    tests_passed = (ledger.get("tests") or {}).get("passed") is True
    missing_reg = [b.get("id") for b in blockers if not b.get("regression_test")]
    _c(
        5,
        "blocker-regressions",
        not missing_reg and tests_passed,
        f"tests_passed={tests_passed} missing_regressions={missing_reg}",
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

    return _verdict(conds)
