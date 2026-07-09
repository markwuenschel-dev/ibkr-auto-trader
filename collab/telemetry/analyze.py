"""analyze.py — derive §6.3 / §5.6 / §9 metrics from a trace JSONL, no ML required.

Turns the retrofitted slice-1 trace into the seed metrics the later layers consume:
per-lane yield (unique-catch), evidence-ladder distribution, confirmed-vs-noise (the reject-
precision denominator), convergence trajectory, and the honest B6 waiver reality.
"""

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


def load(path):
    return [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]


def main(path):
    ev = load(path)
    findings = [e for e in ev if e.get("stage") == "finding"]
    lanes = [e for e in ev if e.get("stage") == "verify.lane"]
    tests = [e for e in ev if e.get("stage") == "test.run"]

    print(f"trace: {path}")
    print(f"events={len(ev)}  lanes-run={len(lanes)}  findings={len(findings)}  test-runs={len(tests)}")
    print("test trajectory: " + " -> ".join(f"{t['metrics']['passed']}/{t['metrics']['total']}" for t in tests))

    fs = [e["finding"] for e in findings]
    print("severity:      ", dict(Counter(f["severity"] for f in fs)))
    print("evidence_level:", dict(Counter(f["evidence_level"] for f in fs)))
    print("verdict:       ", dict(Counter(f["verdict"] for f in fs)))

    real = {"confirmed", "plausible"}
    per_lane = defaultdict(lambda: {"found": 0, "real": 0})
    for e in findings:
        lane = e["role"].replace("breaker:", "")
        per_lane[lane]["found"] += 1
        if e["finding"]["verdict"] in real:
            per_lane[lane]["real"] += 1
    for e in lanes:  # include clean lanes (0 findings) so yield is honest
        lane = e["role"].replace("breaker:", "")
        per_lane.setdefault(lane, {"found": 0, "real": 0})
    print("per-lane yield (real defects / findings):")
    for lane, d in sorted(per_lane.items()):
        print(f"  {lane:22s} {d['real']}/{d['found']}")

    highs = [f for f in fs if f["severity"] == "high" and f["verdict"] == "confirmed"]
    print(f"HIGH confirmed bugs: {len(highs)} -> {[f['title'] for f in highs]}")

    became = [f for f in fs if f.get("fixed_by_test")]
    print(f"findings that became an executable oracle (regression test): {len(became)}/{len(fs)}")

    conv = [f for f in fs if len(f.get("converged_lanes") or []) > 1]
    print(f"multi-lane converged findings (higher-confidence, §6.3): {len(conv)} -> {[f['finding_id'] for f in conv]}")

    waived = sum(1 for e in ev if (e.get("risk") or {}).get("waived"))
    print(f"events with a waiver: {waived}  <- B6 verify-everything ceiling; the savings engine (§6) must beat this")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "telemetry/traces/slice-01-collab-common.jsonl")
