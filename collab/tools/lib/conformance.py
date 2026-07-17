"""conformance — the candidate-bound spec-conformance contract (ADR-0005).

The ten generic lanes in ``telemetry/lanes.json`` are *defect* probes: a breaker attacks the change
and a verifier adjudicates what the breaker raised. That shape is blind to an entire class of failure
— a requirement the change silently OMITS. Nothing is wrong; something is simply absent, and absence
raises no finding.

2026-07-16 made the gap concrete. Handoff 035's reviewer (grok-4.5) emitted a conformance block
marking the generation binding ``[met]``, citing ``planner.py:102-114`` (a real, resolvable range)
and ``test_planner.py:65-66`` (a real test that asserts *different* fields). The binding was in fact
structurally ``None``. Nothing caught it: pyright is blind (the field is ``Any = None``), the tests
were the builder's own, no lane raised it, and the reviewer's itemization is advisory by design
(``narrative.py``). A field being *written* proved nothing about what it *reads*.

So conformance is adjudicated here, against DECLARED, TYPED constraints:

* Requirements are ``contracts.declared_constraints`` — the existing ``## Constraints`` /
  ``- [ID] text`` scheme (§7.2), already injection-defended by ``handoff_core._reject_unsafe_body``.
  This module deliberately does NOT parse handoffs itself: a second parser would be a second truth.
* The compiled contract is digested and bound into candidate identity, so changed constraints or a
  changed protocol cannot reuse prior evidence.
* Two independent assessors must each return one strict record per declared ID, and they must AGREE.

Honest bound: this enforces *completeness and attribution*, not truth. Every mechanical check here is
about coverage, shape, and whether a citation RESOLVES — not whether the cited line proves the claim.
Grok's false ``[met]`` cited real, resolvable lines and would pass every check in this file; what
would have caught it is the independent verifier disagreeing. That is a real improvement over one
model's unverified paragraph, and it is not proof. A requirement that can be expressed as a type or a
test should be one — those are checked by ``scripts/verify.py``, which cannot be talked out of a
verdict.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import collab_common as cc
import contracts as _contracts

#: Bump when the evidence protocol changes shape. It is bound into the contract digest, so a protocol
#: change invalidates prior evidence rather than silently reinterpreting it.
PROTOCOL_REVISION = "spec-conformance-v1"

#: Outcome vocabulary for one requirement.
MET = "met"
PARTIAL = "partial"
MISSING = "missing"
_STATUSES = (MET, PARTIAL, MISSING)

#: ``path:line`` or ``path:line-line``, repo-relative. Anchored so a bare path cannot pass as evidence.
_POINTER_RE = re.compile(r"^(?P<path>[^:\s][^:]*):(?P<start>\d+)(?:-(?P<end>\d+))?$")


class ConformanceError(cc.CollabError):
    """The conformance contract could not be compiled or its evidence could not be trusted."""


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class ConformanceContract:
    """The declared requirements one candidate must prove, frozen and digested."""

    hid: str
    requirements: tuple[tuple[str, str], ...]  # sorted (id, text)
    protocol_revision: str
    digest: str

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(rid for rid, _ in self.requirements)

    def identity_data(self) -> dict[str, Any]:
        return {
            "hid": self.hid,
            "protocol_revision": self.protocol_revision,
            "requirements": [{"id": rid, "text": text} for rid, text in self.requirements],
        }


def compile_contract(collab, hid: str, *, protocol_revision: str = PROTOCOL_REVISION) -> ConformanceContract:
    """Compile handoff ``hid``'s declared constraints into a frozen, digested contract.

    Raises ``ConformanceError`` when the handoff declares none. That refusal is the point: an
    autonomous close is a claim that the spec was met, and a handoff with no typed requirements makes
    that claim unfalsifiable. It fails BEFORE any model work rather than closing vacuously.
    """
    import handoff_core as hc

    # The DIRECTORY is the authoritative state ([C10]); resolve the real path from it and let
    # contracts.parse_handoff own the parsing.
    state = hc.state_of(collab, hid)
    if state is None:
        raise ConformanceError(f"handoff {hid} has no content file")
    path = next((Path(h["path"]) for h in hc.list_handoffs(collab, state) if h["id"] == hid), None)
    if path is None:
        raise ConformanceError(f"cannot locate handoff {hid} on disk")
    try:
        obj = _contracts.parse_handoff(path)
    except (OSError, ValueError) as exc:
        raise ConformanceError(f"cannot parse handoff {hid}: {exc}") from exc

    declared = _contracts.declared_constraints(obj)
    if not declared:
        raise ConformanceError(
            f"handoff {hid} declares no typed constraints; autonomous closure requires a "
            f"'## Constraints' section of '- [ID] text' bullets (see SEATS.md / ADR-0005)"
        )
    requirements = tuple(sorted((rid, text) for rid, text in declared.items()))
    payload = {
        "hid": hid,
        "protocol_revision": protocol_revision,
        "requirements": [{"id": rid, "text": text} for rid, text in requirements],
    }
    digest = "conformance:" + hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return ConformanceContract(
        hid=hid, requirements=requirements, protocol_revision=protocol_revision, digest=digest
    )


def build_prompt(contract: ConformanceContract, *, role: str) -> str:
    """The strict-JSON conformance protocol for one assessor role ('assessor' | 'verifier').

    The discipline in this prompt is the 2026-07-16 post-mortem written as an instruction: an
    assignment is not a binding until the READER is traced to a real source. grok marked the
    generation binding met because ``decision_generation=generation`` appeared in the diff; the
    value it read resolved to nothing. The 'writers vs readers' rule is stated explicitly because
    that is precisely the check a capable model skips under shipping pressure.
    """
    # Fail closed on an unknown role. Treating every non-'assessor' string as the verifier would let
    # a typo silently hand BOTH seats the assessor stance, collapsing the independence this gate is
    # built on into two identical opinions — while still reporting two agreeing reports.
    stances = {
        "assessor": "You are the CONFORMANCE ASSESSOR. Judge each requirement on its merits.",
        "verifier": (
            "You are the INDEPENDENT CONFORMANCE VERIFIER. Another assessor has judged these same "
            "requirements; you are being asked separately and MUST reach your own conclusion from the "
            "code. Default to 'missing' when you cannot find the reader."
        ),
    }
    if role not in stances:
        raise ConformanceError(f"unknown conformance role {role!r} (want one of {tuple(stances)})")
    lines = "\n".join(f"- [{rid}] {text}" for rid, text in contract.requirements)
    stance = stances[role]
    return f"""{stance}

For EACH requirement below, decide: met | partial | missing.

A requirement is 'met' ONLY if you traced it in the ACTUAL repository:
  * Find the READER, not the writer. `x = y` is NOT a binding until you follow `y` to a real source
    and confirm it resolves to a non-default value. A field assigned from an attribute that does not
    exist reads as None and is 'missing', however convincing the assignment looks.
  * A cited test must assert THAT requirement, not a neighbouring one. If no test covers it, say so
    in `evidence` and do not let the absence of a test become a 'met'.
  * `source` MUST be a repo-relative `path:line` or `path:line-line` that you actually read.
Uncertain? That is 'missing'. An unfalsifiable 'met' is worse than an honest 'missing'.

REQUIREMENTS ({len(contract.requirements)}):
{lines}

Reply with ONLY this JSON object — no prose, no markdown outside the fence:
{{
  "contract_digest": "{contract.digest}",
  "requirements": [
    {{"id": "<requirement id>", "status": "met|partial|missing",
      "source": "path:line or path:line-line", "test": "path:line or null",
      "evidence": "<what you actually read that proves this>"}}
  ]
}}
Emit exactly one record per requirement id, for every id listed above — no more, no fewer."""


def resolve_pointer(pointer: str, *, source_base, roots_ok=None) -> tuple[bool, str]:
    """Does ``path:line[-line]`` resolve inside ``source_base``? Returns ``(ok, detail)``.

    Fail-closed, and deliberately narrow: this proves a citation POINTS AT SOMETHING REAL. It cannot
    prove the cited line supports the claim. Path traversal is refused the same way condition 11 does.
    """
    if not isinstance(pointer, str) or not pointer.strip():
        return False, "empty evidence pointer"
    m = _POINTER_RE.match(pointer.strip())
    if not m:
        return False, f"malformed pointer {pointer!r} (want 'path:line' or 'path:line-line')"
    rel, start = m.group("path"), int(m.group("start"))
    end = int(m.group("end")) if m.group("end") else start
    if start < 1 or end < start:
        return False, f"bad line range in {pointer!r}"
    if Path(rel).is_absolute() or ".." in Path(rel).parts:
        return False, f"pointer {pointer!r} must be repo-relative and must not escape the repo"
    try:
        base = Path(source_base).resolve()
        target = (base / rel).resolve()
    except OSError as exc:
        return False, f"cannot resolve {pointer!r}: {exc}"
    if base != target and base not in target.parents:
        return False, f"pointer {pointer!r} escapes the source base"
    if not target.is_file():
        return False, f"pointer {pointer!r} names no file"
    if roots_ok is not None and not roots_ok(target):
        return False, f"pointer {pointer!r} is outside the permitted source roots"
    try:
        total = len(target.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError as exc:
        return False, f"cannot read {rel}: {exc}"
    if end > total:
        return False, f"pointer {pointer!r} exceeds {rel} ({total} lines)"
    return True, f"{rel}:{start}-{end}"


def parse_report(raw: str, contract: ConformanceContract) -> tuple[dict[str, dict], str | None]:
    """Parse one assessor's strict-JSON report. Returns ``(by_id, error)``; error means untrusted.

    Strict by construction — a report that is malformed, drops an ID, invents one, or duplicates one
    is ``verification_incomplete``, never a partial pass. Silence must not read as agreement.
    """
    if not isinstance(raw, str) or not raw.strip():
        return {}, "empty conformance report"
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        first, last = text.find("{"), text.rfind("}")
        if first == -1 or last <= first:
            return {}, "conformance report is not JSON"
        text = text[first : last + 1]
    try:
        doc = json.loads(text)
    except ValueError as exc:
        return {}, f"conformance report is not valid JSON: {exc}"
    if not isinstance(doc, dict):
        return {}, "conformance report must be a JSON object"
    if doc.get("contract_digest") != contract.digest:
        return {}, (
            f"conformance report is bound to {doc.get('contract_digest')!r}, "
            f"expected {contract.digest!r} (stale or mismatched evidence)"
        )
    records = doc.get("requirements")
    if not isinstance(records, list):
        return {}, "conformance report has no 'requirements' list"

    by_id: dict[str, dict] = {}
    for rec in records:
        if not isinstance(rec, dict):
            return {}, "each requirement record must be an object"
        rid = rec.get("id")
        if not isinstance(rid, str) or not rid.strip():
            return {}, "a requirement record has no id"
        rid = rid.strip()
        if rid in by_id:
            return {}, f"duplicate record for requirement {rid!r}"
        status = rec.get("status")
        if status not in _STATUSES:
            return {}, f"requirement {rid!r} has invalid status {status!r} (want one of {_STATUSES})"
        by_id[rid] = {
            "id": rid,
            "status": status,
            "source": rec.get("source"),
            "test": rec.get("test"),
            "evidence": str(rec.get("evidence") or "").strip(),
        }
    declared = set(contract.ids)
    got = set(by_id)
    if got != declared:
        missing = sorted(declared - got)
        extra = sorted(got - declared)
        return {}, f"conformance coverage mismatch: missing={missing} unexpected={extra}"
    return by_id, None


def reconcile(
    contract: ConformanceContract,
    assessor_raw: str,
    verifier_raw: str,
    *,
    source_base,
    roots_ok=None,
) -> dict:
    """Adjudicate two independent reports into one conformance record.

    ``satisfied`` requires ALL of: both reports parse strictly; both cover exactly the declared IDs;
    both agree per ID; every ID is ``met``; and every ``met`` cites a source pointer that RESOLVES.

    Everything else refuses. Disagreement is ``incomplete``, not a tie broken toward shipping — if two
    assessors disagree about whether a requirement is met, the honest state is "unknown", and unknown
    must never close. Confirmed ``missing``/``partial`` become blockers carrying the requirement text,
    so the builder gets the exact unmet item rather than a vague send-back.
    """
    record: dict[str, Any] = {
        "contract_digest": contract.digest,
        "protocol_revision": contract.protocol_revision,
        "requirement_ids": list(contract.ids),
        "results": [],
        "blockers": [],
        "incomplete": None,
        "satisfied": False,
    }

    assessor, err = parse_report(assessor_raw, contract)
    if err:
        record["incomplete"] = {"reason": "assessor", "detail": err}
        return record
    verifier, err = parse_report(verifier_raw, contract)
    if err:
        record["incomplete"] = {"reason": "verifier", "detail": err}
        return record

    texts = dict(contract.requirements)
    results, blockers = [], []
    for rid in contract.ids:
        a, v = assessor[rid], verifier[rid]
        if a["status"] != v["status"]:
            record["incomplete"] = {
                "reason": "disagreement",
                "detail": f"requirement {rid!r}: assessor={a['status']} verifier={v['status']}",
            }
            return record
        status = a["status"]
        entry = {
            "id": rid,
            "text": texts[rid],
            "status": status,
            # BOTH records are retained. Keeping only the assessor's would make the verifier's
            # agreement unauditable after the fact — a reader could not tell whether the second
            # opinion cited the same code or merely echoed the verdict.
            "assessor": {k: a.get(k) for k in ("source", "test", "evidence")},
            "verifier": {k: v.get(k) for k in ("source", "test", "evidence")},
        }
        if status == MET:
            # A 'met' with no resolvable source is an unsupported claim. Refuse it as incomplete
            # rather than count it: an unverifiable pass is exactly what this gate exists to stop.
            #
            # BOTH sides are checked. Validating only the assessor bought a fiction of "two
            # independent resolvable citations" while the verifier could cite ../outside.py:1 and
            # still close — a hole in the exact property this gate advertises.
            for who, rec in (("assessor", a), ("verifier", v)):
                ok, detail = resolve_pointer(
                    rec.get("source") or "", source_base=source_base, roots_ok=roots_ok
                )
                if not ok:
                    record["incomplete"] = {
                        "reason": "evidence",
                        "detail": f"requirement {rid!r} {who} source: {detail}",
                    }
                    return record
                entry[who]["source_resolved"] = detail
        # A cited test must resolve too, whatever the status: a pointer offered as evidence and never
        # checked is worse than no pointer, because it reads as corroboration. A null test is allowed
        # (the assessor is saying "no test covers this"), a fabricated one is not.
        for who, rec in (("assessor", a), ("verifier", v)):
            if rec.get("test") is None:
                continue
            ok, detail = resolve_pointer(rec.get("test") or "", source_base=source_base, roots_ok=roots_ok)
            if not ok:
                record["incomplete"] = {
                    "reason": "evidence",
                    "detail": f"requirement {rid!r} {who} test: {detail}",
                }
                return record
            entry[who]["test_resolved"] = detail
        if status != MET:
            blockers.append(
                {
                    "id": f"conformance:{rid}",
                    "lane": "spec-conformance",
                    "fixed": False,
                    "regression_test": None,
                    "description": f"[{rid}] {texts[rid]} — {status}: {a.get('evidence') or 'no evidence'}",
                }
            )
        results.append(entry)

    record["results"] = results
    record["blockers"] = blockers
    record["satisfied"] = not blockers
    return record
