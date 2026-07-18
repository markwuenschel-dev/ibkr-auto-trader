"""Tests for gate_runner (Workstream C). stdlib + pytest only.

Run:  python -m pytest tests/test_gate_runner.py -v   (from the collab-kit repo root)

Covers: (a) all-pass -> passed/exit 0; (b) L1 required_fields failure -> first_failing_tier==1
and later (L3) tier NOT run; (c) early-exit honours ascending tier order; (d) ruleset_hash is
stable across identical rulesets; (e) per-check JSONL events are emitted and parseable.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Make the core importable whether run from repo root or elsewhere.
_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

import gate_runner as gr  # noqa: E402

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _write_json(path: Path, obj) -> Path:
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


def _read_events(log: Path) -> list[dict]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _check_events(log: Path) -> list[dict]:
    return [e for e in _read_events(log) if e.get("stage") == "gate.check"]


def _emitted_check_names(log: Path) -> set[str]:
    names = set()
    for e in _check_events(log):
        for g in e.get("gates", []):
            names.add(g["name"])
    return names


def _tiny_passing_test(tmp_path: Path) -> Path:
    t = tmp_path / "test_tiny_ok.py"
    t.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    return t


# --------------------------------------------------------------------------- #
# (a) all tiers pass -> passed True, exit 0
# --------------------------------------------------------------------------- #


def test_all_pass_returns_passed_and_exit_zero(tmp_path):
    artifact = _write_json(tmp_path / "artifact.json", {"id": "A1", "kind": "widget", "payload": {"x": 1}})
    tiny = _tiny_passing_test(tmp_path)
    ruleset = _write_json(
        tmp_path / "rs.json",
        {
            "name": "t",
            "version": "1",
            "tiers": [
                {
                    "tier": 1,
                    "name": "contract",
                    "checks": [
                        {
                            "name": "fields",
                            "kind": "required_fields",
                            "severity": "blocking",
                            "params": {"path": "{artifact}", "fields": ["id", "kind"]},
                        }
                    ],
                },
                {
                    "tier": 3,
                    "name": "oracle",
                    "checks": [
                        {
                            "name": "tests",
                            "kind": "pytest",
                            "severity": "blocking",
                            "params": {"path": str(tiny)},
                        }
                    ],
                },
            ],
        },
    )
    log = tmp_path / "trace.jsonl"

    out = gr.run_gates(str(artifact), str(ruleset), log_path=str(log))
    assert out["passed"] is True
    assert out["first_failing_tier"] is None
    assert all(r["status"] == "pass" for r in out["results"])

    # main() -> exit code 0
    code = gr.main(["run", str(artifact), "--ruleset", str(ruleset), "--log", str(log)])
    assert code == 0


def test_pytest_check_records_resolved_argv_in_trace(tmp_path):
    # INT-032: the pytest kind passes on rc==0, but a ruleset can narrow `params.path` to a single
    # file and earn a tier pass. The resolved path AND flags must be reconstructable from the trace,
    # so "which tests actually ran under this tier" is auditable from the ledger, not just "passed".
    artifact = _write_json(tmp_path / "artifact.json", {"id": "A1", "kind": "widget"})
    tiny = _tiny_passing_test(tmp_path)
    ruleset = _write_json(
        tmp_path / "rs.json",
        {
            "name": "t",
            "version": "1",
            "tiers": [
                {
                    "tier": 3,
                    "name": "oracle",
                    "checks": [
                        {"name": "tests", "kind": "pytest", "severity": "blocking", "params": {"path": str(tiny)}}
                    ],
                }
            ],
        },
    )
    log = tmp_path / "trace.jsonl"
    out = gr.run_gates(str(artifact), str(ruleset), log_path=str(log))

    pytest_result = next(r for r in out["results"] if r["kind"] == "pytest")
    # the exact narrowed path and the -q flag are reconstructable from the recorded detail
    assert pytest_result["detail"].startswith(f"pytest {tiny} -q")
    # and the same detail rides the emitted gate.check trace event
    events = [e for e in _check_events(log) if any(g["name"] == "tests" for g in e.get("gates", []))]
    assert events and str(tiny) in events[0]["detail"]


# --------------------------------------------------------------------------- #
# (b) L1 required_fields failure -> first_failing_tier==1, later tier NOT run
# --------------------------------------------------------------------------- #


def test_l1_failure_early_exits_before_l3(tmp_path):
    # artifact is missing "payload" -> the L1 required_fields check fails (blocking).
    artifact = _write_json(tmp_path / "artifact.json", {"id": "A1", "kind": "widget"})
    tiny = _tiny_passing_test(tmp_path)
    ruleset = _write_json(
        tmp_path / "rs.json",
        {
            "tiers": [
                {
                    "tier": 1,
                    "name": "contract",
                    "checks": [
                        {
                            "name": "L1-fields",
                            "kind": "required_fields",
                            "severity": "blocking",
                            "params": {"path": "{artifact}", "fields": ["id", "kind", "payload"]},
                        }
                    ],
                },
                {
                    "tier": 3,
                    "name": "oracle",
                    "checks": [
                        {
                            "name": "L3-tests",
                            "kind": "pytest",
                            "severity": "blocking",
                            "params": {"path": str(tiny)},
                        }
                    ],
                },
            ]
        },
    )
    log = tmp_path / "trace.jsonl"

    out = gr.run_gates(str(artifact), str(ruleset), log_path=str(log))
    assert out["passed"] is False
    assert out["first_failing_tier"] == 1

    # The L3 (pytest) tier must NOT have run: no gate.check event for it.
    names = _emitted_check_names(log)
    assert "L1-fields" in names
    assert "L3-tests" not in names
    # and no pytest-kind check in the recorded results
    assert all(r["kind"] != "pytest" for r in out["results"])

    # main() -> exit code == first failing tier == 1
    code = gr.main(["run", str(artifact), "--ruleset", str(ruleset), "--log", str(log)])
    assert code == 1


# --------------------------------------------------------------------------- #
# (c) early-exit honours ASCENDING tier order (even if listed out of order)
# --------------------------------------------------------------------------- #


def test_early_exit_respects_ascending_tier_order(tmp_path):
    artifact = _write_json(tmp_path / "artifact.json", {"id": "A1"})
    missing = tmp_path / "nope.json"  # deliberately absent
    # Tiers are listed OUT of order (3, 1, 2). The runner must sort ascending, so tier 1 runs
    # first, fails (file_exists on a missing file), and tiers 2 and 3 never run.
    ruleset = _write_json(
        tmp_path / "rs.json",
        {
            "tiers": [
                {
                    "tier": 3,
                    "name": "third",
                    "checks": [
                        {
                            "name": "T3",
                            "kind": "file_exists",
                            "severity": "blocking",
                            "params": {"path": str(artifact)},
                        }
                    ],
                },
                {
                    "tier": 1,
                    "name": "first",
                    "checks": [
                        {
                            "name": "T1",
                            "kind": "file_exists",
                            "severity": "blocking",
                            "params": {"path": str(missing)},
                        }
                    ],
                },
                {
                    "tier": 2,
                    "name": "second",
                    "checks": [
                        {
                            "name": "T2",
                            "kind": "file_exists",
                            "severity": "blocking",
                            "params": {"path": str(artifact)},
                        }
                    ],
                },
            ]
        },
    )
    log = tmp_path / "trace.jsonl"

    out = gr.run_gates(str(artifact), str(ruleset), log_path=str(log))
    assert out["passed"] is False
    assert out["first_failing_tier"] == 1
    names = _emitted_check_names(log)
    assert names == {"T1"}  # only the first (ascending) tier ran


# --------------------------------------------------------------------------- #
# (d) ruleset_hash is stable for identical rulesets (and changes when content changes)
# --------------------------------------------------------------------------- #


def test_ruleset_hash_is_stable_and_content_addressed(tmp_path):
    body = {
        "name": "rs",
        "version": "1",
        "tiers": [
            {
                "tier": 1,
                "name": "c",
                "checks": [
                    {
                        "name": "f",
                        "kind": "file_exists",
                        "severity": "advisory",
                        "params": {"path": "{artifact}"},
                    }
                ],
            }
        ],
    }
    r1 = _write_json(tmp_path / "a.json", body)
    r2 = _write_json(tmp_path / "b.json", body)  # identical content, different filename
    artifact = _write_json(tmp_path / "artifact.json", {"id": "A1"})

    out1 = gr.run_gates(str(artifact), str(r1), log_path=str(tmp_path / "l1.jsonl"))
    out2 = gr.run_gates(str(artifact), str(r2), log_path=str(tmp_path / "l2.jsonl"))
    assert out1["ruleset_hash"] == out2["ruleset_hash"]

    # A changed ruleset yields a different hash.
    changed = dict(body, version="2")
    assert gr.ruleset_hash(changed) != gr.ruleset_hash(body)


# --------------------------------------------------------------------------- #
# (e) per-check JSONL events are emitted and parseable
# --------------------------------------------------------------------------- #


def test_per_check_jsonl_events_are_emitted_and_parseable(tmp_path):
    artifact = _write_json(tmp_path / "artifact.json", {"id": "A1", "kind": "w"})
    ruleset = _write_json(
        tmp_path / "rs.json",
        {
            "tiers": [
                {
                    "tier": 1,
                    "name": "c",
                    "checks": [
                        {
                            "name": "chk-valid",
                            "kind": "json_valid",
                            "severity": "blocking",
                            "params": {"path": "{artifact}"},
                        },
                        {
                            "name": "chk-fields",
                            "kind": "required_fields",
                            "severity": "advisory",
                            "params": {"path": "{artifact}", "fields": ["id"]},
                        },
                    ],
                },
            ]
        },
    )
    log = tmp_path / "trace.jsonl"

    out = gr.run_gates(str(artifact), str(ruleset), log_path=str(log))
    assert out["passed"] is True

    events = _read_events(log)
    checks = [e for e in events if e.get("stage") == "gate.check"]
    runs = [e for e in events if e.get("stage") == "gate.run"]

    # one event per check + exactly one summary
    assert len(checks) == 2
    assert len(runs) == 1

    # every event carries the ruleset_hash and a parseable gates[] record
    rhash = out["ruleset_hash"]
    for e in checks:
        assert e["ruleset_hash"] == rhash
        assert e["role"] == "gate"
        g = e["gates"][0]
        assert g["name"] in {"chk-valid", "chk-fields"}
        assert g["status"] in {"pass", "fail"}
        assert g["tier"] == 1

    summary = runs[0]
    assert summary["ruleset_hash"] == rhash
    assert summary["overall"] == "pass"
    assert summary["metrics"]["total"] == 2


# --------------------------------------------------------------------------- #
# (f) source_consistency check kind (§17 — source == tested)
# --------------------------------------------------------------------------- #


def _src_tree(tmp_path: Path) -> Path:
    base = tmp_path / "repo"
    (base / "pkg").mkdir(parents=True)
    (base / "pkg" / "a.py").write_text("print('a')\n", encoding="utf-8")
    (base / "pkg" / "b.py").write_text("print('b')\n", encoding="utf-8")
    return base


def _src_ruleset(tmp_path: Path, base: Path, *, extra_tier=False) -> Path:
    tiers = [
        {
            "tier": 1,
            "name": "src",
            "checks": [
                {
                    "name": "src==tested",
                    "kind": "source_consistency",
                    "severity": "blocking",
                    "params": {"manifest": "{artifact}", "base": str(base)},
                }
            ],
        }
    ]
    if extra_tier:
        tiers.append(
            {
                "tier": 3,
                "name": "later",
                "checks": [
                    {
                        "name": "never-runs",
                        "kind": "file_exists",
                        "severity": "blocking",
                        "params": {"path": str(base)},
                    }
                ],
            }
        )
    return _write_json(tmp_path / "rs.json", {"tiers": tiers})


def test_source_manifest_is_deterministic_and_relative(tmp_path):
    base = _src_tree(tmp_path)
    m1 = gr.source_manifest(["pkg/*.py"], base)
    m2 = gr.source_manifest(["pkg/*.py"], base)
    assert m1 == m2  # deterministic
    assert set(m1) == {"pkg/a.py", "pkg/b.py"}  # repo-relative POSIX keys
    assert all(len(h) == 64 for h in m1.values())  # sha256 hex


def test_source_consistency_passes_when_identical(tmp_path):
    base = _src_tree(tmp_path)
    manifest = _write_json(tmp_path / "m.json", gr.source_manifest(["pkg/*.py"], base))
    out = gr.run_gates(str(manifest), str(_src_ruleset(tmp_path, base)), log_path=str(tmp_path / "t.jsonl"))
    assert out["passed"] is True


def test_source_consistency_blocks_on_one_byte_drift(tmp_path):
    base = _src_tree(tmp_path)
    manifest = _write_json(tmp_path / "m.json", gr.source_manifest(["pkg/*.py"], base))
    (base / "pkg" / "a.py").write_text("print('a')  # changed\n", encoding="utf-8")  # drift AFTER capture
    log = tmp_path / "t.jsonl"
    out = gr.run_gates(str(manifest), str(_src_ruleset(tmp_path, base, extra_tier=True)), log_path=str(log))
    assert out["passed"] is False
    assert out["first_failing_tier"] == 1
    assert "never-runs" not in _emitted_check_names(log)  # fail-closed: later tier skipped


def test_source_consistency_fails_on_missing_file(tmp_path):
    base = _src_tree(tmp_path)
    manifest = _write_json(tmp_path / "m.json", gr.source_manifest(["pkg/*.py"], base))
    (base / "pkg" / "b.py").unlink()  # attested file deleted
    out = gr.run_gates(str(manifest), str(_src_ruleset(tmp_path, base)), log_path=str(tmp_path / "t.jsonl"))
    assert out["passed"] is False


def test_source_consistency_fails_on_empty_manifest(tmp_path):
    # An empty manifest attests nothing -> must fail closed, never silently pass.
    manifest = _write_json(tmp_path / "m.json", {})
    out = gr.run_gates(
        str(manifest), str(_src_ruleset(tmp_path, tmp_path)), log_path=str(tmp_path / "t.jsonl")
    )
    assert out["passed"] is False


def test_source_consistency_refuses_escaping_path(tmp_path):
    # A manifest whose key escapes base must not hash outside base -> treated as missing (fail).
    base = _src_tree(tmp_path)
    (tmp_path / "secret.py").write_text("SECRET\n", encoding="utf-8")
    manifest = _write_json(tmp_path / "m.json", {"../secret.py": gr._sha256_file(tmp_path / "secret.py")})
    out = gr.run_gates(str(manifest), str(_src_ruleset(tmp_path, base)), log_path=str(tmp_path / "t.jsonl"))
    assert out["passed"] is False


def test_shipped_autonomous_done_ruleset_loads(tmp_path):
    rs = Path(__file__).resolve().parent.parent / "telemetry" / "rulesets" / "autonomous-done.json"
    loaded = gr._load_ruleset(rs)
    assert any(c.get("kind") == "source_consistency" for t in loaded["tiers"] for c in t.get("checks", []))
