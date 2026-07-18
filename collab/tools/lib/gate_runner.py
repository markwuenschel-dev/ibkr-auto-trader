r"""gate_runner — the fail-closed, hash-pinned quality-gate runner (ARCHITECTURE.md §5.4 / §13).

One entrypoint — ``gate run <artifact>`` — loads a *versioned, hash-pinned* ruleset (a list of
tiers), executes the tiers in ascending order, and **early-exits the moment a blocking check in a
tier fails** (fail-closed): later tiers never run. Every check emits one JSONL trace event
(``stage="gate.check"``) and the run emits a summary event (``stage="gate.run"``) carrying the
``ruleset_hash`` — so a trace records *exactly which ruleset* produced *which verdict*.

Exit-code contract (the CI convention):
  * ``0``  — every executed tier passed.
  * ``N``  — the tier number of the FIRST tier with a failing blocking check.
  * ``2``  — bad CLI arguments.

Caveat: because the exit code IS the first-failing tier number, a *blocking* failure in **tier 0**
would collide with the success code 0. Tier 0 is therefore reserved for advisory/informational
checks; blocking checks should live in tiers >= 1 (the shipped ``default.json`` follows this).

Ruleset shape (stdlib JSON, self-contained)::

    {
      "name": "default", "version": "1.0",
      "tiers": [
        {"tier": 1, "name": "contract",
         "checks": [{"name": "...", "kind": "required_fields",
                     "severity": "blocking",
                     "params": {"path": "{artifact}", "fields": ["id"]}}]}
      ]
    }

``{artifact}`` in any ``params.path`` is substituted with the artifact path; an omitted ``path``
defaults to the artifact itself. A ruleset may also be a bare ``[ ...tiers... ]`` list.

Supported ``kind``s (all stdlib, no third-party deps):
  * ``json_valid``      — ``params.path`` parses as JSON.
  * ``required_fields`` — ``params.path`` is JSON and every ``params.fields`` key is present
                          and non-null.
  * ``file_exists``     — ``params.path`` exists on disk.
  * ``pytest``          — shell out to ``python -m pytest <params.path> -q``; pass iff rc == 0.
  * ``source_consistency`` — the live tree still matches a claimed source manifest (ARCHITECTURE.md §17,
                          autonomous revision). params: ``manifest`` (path to a ``{path: sha256}`` JSON,
                          default = the artifact) + ``base`` (root the manifest's paths are relative to,
                          default = the kit root). Fail-closed on an empty manifest, a missing/changed
                          file, or a path that escapes ``base``.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# --- import siblings without polluting the global module namespace ----------- #
# trace.py's module name is the stdlib name "trace"; load it by path as a private alias so we
# never shadow (or get shadowed by) the stdlib module. collab_common has a unique name.
_LIB_DIR = Path(__file__).resolve().parent
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

import collab_common as cc  # noqa: E402


def _load_by_path(alias: str, filename: str):
    spec = importlib.util.spec_from_file_location(alias, _LIB_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_trace = _load_by_path("collab_trace", "trace.py")

ROLE = "gate"
VALID_SEVERITIES = ("blocking", "advisory")


# --------------------------------------------------------------------------- #
# Ruleset loading + hash-pinning
# --------------------------------------------------------------------------- #


def _load_ruleset(ruleset_path: str | os.PathLike) -> dict:
    """Load and normalize a ruleset to ``{"tiers": [...], ...}``.

    Accepts either an object with a ``tiers`` list or a bare list of tiers.

    Raises:
        cc.CollabError: if the file is missing, unparseable, or has no tiers.
    """
    p = Path(ruleset_path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise cc.CollabError(f"ruleset not found: {p}") from exc
    except ValueError as exc:
        raise cc.CollabError(f"ruleset {p} is not valid JSON: {exc}") from exc
    ruleset = {"tiers": raw} if isinstance(raw, list) else raw
    if not isinstance(ruleset, dict) or not isinstance(ruleset.get("tiers"), list):
        raise cc.CollabError(f"ruleset {p} has no 'tiers' list")
    return ruleset


def _canonical(obj) -> str:
    """Deterministic JSON text: the pre-image for the ruleset hash (order-independent)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def ruleset_hash(ruleset: dict) -> str:
    """sha256 of the canonical JSON of ``ruleset`` — the 'pin' in hash-pinned."""
    return hashlib.sha256(_canonical(ruleset).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Individual check kinds
# --------------------------------------------------------------------------- #


def _resolve_path(params: dict, artifact: str) -> str:
    """Resolve a check's target path: ``{artifact}`` token -> artifact; omitted -> artifact."""
    raw = params.get("path")
    if raw is None:
        return artifact
    return str(raw).replace("{artifact}", artifact)


def _read_json(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _check_json_valid(params, artifact):
    path = _resolve_path(params, artifact)
    try:
        _read_json(path)
    except FileNotFoundError:
        return "fail", f"{path}: not found"
    except ValueError as exc:
        return "fail", f"{path}: invalid JSON ({exc})"
    return "pass", f"{path}: parses as JSON"


def _check_required_fields(params, artifact):
    path = _resolve_path(params, artifact)
    fields = params.get("fields") or []
    try:
        data = _read_json(path)
    except FileNotFoundError:
        return "fail", f"{path}: not found"
    except ValueError as exc:
        return "fail", f"{path}: invalid JSON ({exc})"
    if not isinstance(data, dict):
        return "fail", f"{path}: JSON is not an object"
    missing = [f for f in fields if data.get(f) is None]
    if missing:
        return "fail", f"{path}: missing/null fields {missing}"
    return "pass", f"{path}: all required fields present ({fields})"


def _check_file_exists(params, artifact):
    path = _resolve_path(params, artifact)
    if Path(path).exists():
        return "pass", f"{path}: exists"
    return "fail", f"{path}: does not exist"


def _check_pytest(params, artifact):
    path = _resolve_path(params, artifact)
    # L3 execution oracle: a real subprocess so the gate reflects the actual test run, not an
    # in-process import that could contaminate this interpreter's state.
    argv = [sys.executable, "-m", "pytest", path, "-q"]
    # Record the RESOLVED invocation in the detail so the trace answers "which tests ran under this
    # tier", not just "pytest passed" — a ruleset can narrow `params.path` to a single file and earn
    # a tier pass, and that resolved path/flags must be auditable from the ledger (INT-032). The
    # interpreter path is volatile/machine-specific, so report the command from `pytest` onward.
    resolved = " ".join(["pytest", *argv[3:]])
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode == 0:
        return "pass", f"{resolved}: rc=0"
    tail = (proc.stdout or proc.stderr or "").strip().splitlines()[-1:] or [""]
    return "fail", f"{resolved}: rc={proc.returncode} ({tail[0]})"


def _resolve_path_key(params: dict, key: str, artifact: str) -> str | None:
    """Like ``_resolve_path`` but for an arbitrary param key; ``None`` if the key is absent."""
    raw = params.get(key)
    return None if raw is None else str(raw).replace("{artifact}", artifact)


def _sha256_file(path: str | os.PathLike) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_manifest(roots: list[str], base: str | os.PathLike) -> dict[str, str]:
    """``{repo-relative POSIX path: sha256}`` for every regular (non-symlink) file matching any glob in
    ``roots`` under ``base``. Deterministic (sorted) — the source==tested attestation pre-image, produced
    at evidence-time (e.g. ``handoff bundle --emit-manifest``) and re-verified at closeout (§17)."""
    base_p = Path(base)
    out: dict[str, str] = {}
    for pattern in roots:
        for fp in base_p.glob(pattern):
            if fp.is_file() and not fp.is_symlink():
                out[fp.relative_to(base_p).as_posix()] = _sha256_file(fp)
    return dict(sorted(out.items()))


def verify_manifest(claimed: dict, base) -> tuple[bool, str]:
    """Compare a claimed ``{path: sha256}`` manifest against the live tree under ``base`` (§17). Returns
    ``(ok, detail)``. Fail-closed on an empty manifest, any missing/changed attested file, or a path that
    escapes ``base``. Shared by the ``source_consistency`` gate kind and the done-contract (§18.3)."""
    if not isinstance(claimed, dict) or not claimed:
        return False, "manifest empty or not an object — nothing attested"
    try:
        base_p = Path(base) if base else Path(cc.resolve_kit_root())
    except cc.CollabError:
        base_p = Path.cwd()
    base_res = base_p.resolve()
    missing, changed = [], []
    for rel, want in sorted(claimed.items()):
        fp = base_p / rel
        try:
            res = fp.resolve()
        except OSError:
            missing.append(rel)
            continue
        if base_res != res and base_res not in res.parents:  # path escapes base — refuse
            missing.append(rel)
            continue
        if not fp.is_file() or fp.is_symlink():
            missing.append(rel)
            continue
        if _sha256_file(fp) != want:
            changed.append(rel)
    if missing or changed:
        return False, (
            f"source drift vs manifest ({len(claimed)} attested): "
            f"missing/invalid={missing[:5]} changed={changed[:5]}"
        )
    return True, f"source==tested: all {len(claimed)} attested files match under {base_p}"


def _check_source_consistency(params, artifact):
    """Verify the live tree still matches a claimed source manifest (§17). Fail-closed on an empty
    manifest, any missing/changed attested file, or a manifest path that escapes ``base``."""
    manifest_path = _resolve_path_key(params, "manifest", artifact) or artifact
    base_raw = _resolve_path_key(params, "base", artifact)
    try:
        claimed = _read_json(manifest_path)
    except FileNotFoundError:
        return "fail", f"{manifest_path}: manifest not found"
    except ValueError as exc:
        return "fail", f"{manifest_path}: invalid JSON ({exc})"
    ok, detail = verify_manifest(claimed, base_raw)
    return ("pass" if ok else "fail"), detail


_KINDS = {
    "json_valid": _check_json_valid,
    "required_fields": _check_required_fields,
    "file_exists": _check_file_exists,
    "pytest": _check_pytest,
    "source_consistency": _check_source_consistency,
}


def _run_check(check: dict, artifact: str) -> tuple[str, str]:
    kind = check.get("kind")
    fn = _KINDS.get(kind)
    if fn is None:
        return "fail", f"unknown check kind {kind!r}"
    params = check.get("params") or {}
    try:
        return fn(params, artifact)
    except Exception as exc:  # a check must fail closed, never crash the runner
        return "fail", f"{kind} raised {type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- #
# The runner
# --------------------------------------------------------------------------- #


def _new_run_id(artifact: str) -> str:
    try:
        stem = cc.slugify(Path(artifact).name)
    except ValueError:
        stem = "artifact"
    return f"gate-{stem}-{time.time_ns()}"


def _span_id() -> str:
    return f"{time.time_ns():x}{os.urandom(3).hex()}"


def _default_log_path() -> str:
    try:
        root = cc.resolve_kit_root()
    except cc.CollabError:
        root = Path.cwd()
    return str(root / "telemetry" / "traces" / "gate-run.jsonl")


def run_gates(
    artifact: str,
    ruleset_path: str | os.PathLike,
    *,
    log_path: str | None = None,
    only_tier: int | None = None,
) -> dict:
    """Run a hash-pinned ruleset against ``artifact``, fail-closed, emitting a trace per check.

    Executes tiers in ascending ``tier`` order. Within a tier every check runs; if any
    **blocking** check fails, the tier fails and NO later tier runs (early exit). Advisory
    checks are recorded but never block. ``only_tier`` restricts execution to a single tier.

    Returns:
        dict: ``{"passed", "first_failing_tier", "results", "ruleset_hash", "run_id"}``.
    """
    ruleset = _load_ruleset(ruleset_path)
    rhash = ruleset_hash(ruleset)
    log = log_path or _default_log_path()
    run_id = _new_run_id(artifact)
    run_span = _span_id()

    tiers = sorted(ruleset["tiers"], key=lambda t: int(t.get("tier", 0)))
    results: list[dict] = []
    first_failing_tier: int | None = None

    for tier in tiers:
        tnum = int(tier.get("tier", 0))
        if only_tier is not None and tnum != only_tier:
            continue
        tier_blocking_failed = False
        for check in tier.get("checks", []):
            severity = check.get("severity", "blocking")
            status, detail = _run_check(check, artifact)
            gate = {
                "name": check.get("name", check.get("kind", "?")),
                "status": status,
                "severity": severity,
                "tier": tnum,
            }
            result = {**gate, "kind": check.get("kind"), "detail": detail}
            results.append(result)
            _trace.emit(
                log,
                run_id=run_id,
                stage="gate.check",
                role=ROLE,
                artifact=artifact,
                span_id=_span_id(),
                parent_span_id=run_span,
                decision={
                    "action": "accept" if status == "pass" else "reject",
                    "reason_codes": [] if status == "pass" else [gate["name"]],
                    "confidence": None,
                },
                gates=[gate],
                ruleset_hash=rhash,
                detail=detail,
            )
            if status == "fail" and severity == "blocking":
                tier_blocking_failed = True
        if tier_blocking_failed:
            first_failing_tier = tnum
            break  # EARLY EXIT — fail-closed: later tiers do not run

    passed = first_failing_tier is None
    _trace.emit(
        log,
        run_id=run_id,
        stage="gate.run",
        role=ROLE,
        artifact=artifact,
        span_id=run_span,
        decision={
            "action": "accept" if passed else "reject",
            "reason_codes": [] if passed else [f"tier-{first_failing_tier}-failed"],
            "confidence": None,
        },
        gates=[
            {"name": r["name"], "status": r["status"], "severity": r["severity"], "tier": r["tier"]}
            for r in results
        ],
        metrics={
            "passed": sum(1 for r in results if r["status"] == "pass"),
            "total": len(results),
            "files": 0,
            "latency_ms": None,
            "cost_usd": None,
        },
        ruleset_hash=rhash,
        first_failing_tier=first_failing_tier,
        overall="pass" if passed else "fail",
    )

    return {
        "passed": passed,
        "first_failing_tier": first_failing_tier,
        "results": results,
        "ruleset_hash": rhash,
        "run_id": run_id,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gate", description="fail-closed quality-gate runner")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run the ruleset against an artifact")
    run.add_argument("artifact", help="path to the artifact under gate")
    run.add_argument("--ruleset", required=True, help="path to the ruleset JSON")
    run.add_argument("--tier", type=int, default=None, help="run only this tier number")
    run.add_argument("--report", action="store_true", help="print a human-readable summary")
    run.add_argument("--log", default=None, help="JSONL trace path (default telemetry/traces/gate-run.jsonl)")
    return parser


def _print_report(outcome: dict, ruleset_path: str) -> None:
    print(f"gate run: {'PASS' if outcome['passed'] else 'FAIL'}")
    print(f"  ruleset:      {ruleset_path}")
    print(f"  ruleset_hash: {outcome['ruleset_hash'][:16]}...")
    print(f"  run_id:       {outcome['run_id']}")
    for r in outcome["results"]:
        mark = "PASS" if r["status"] == "pass" else "FAIL"
        print(f"  [T{r['tier']}] {mark} {r['severity']:8s} {r['name']}: {r['detail']}")
    if not outcome["passed"]:
        print(f"  first failing tier: {outcome['first_failing_tier']}")


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Returns the exit code (0 pass; first-failing-tier on fail; 2 bad args)."""
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse exits on bad args / --help
        return int(exc.code) if exc.code is not None else 2

    if args.command != "run":
        return 2

    try:
        outcome = run_gates(
            args.artifact,
            args.ruleset,
            log_path=args.log,
            only_tier=args.tier,
        )
    except cc.CollabError as exc:
        print(f"gate: {exc}", file=sys.stderr)
        return 2

    if args.report:
        _print_report(outcome, args.ruleset)

    if outcome["passed"]:
        return 0
    return outcome["first_failing_tier"] or 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
