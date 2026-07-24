"""candidate_assessment — the deep module that owns a candidate's assessment policy (ADR-0003 D4).

The orchestrator (`autopilot.py`) dispatches model calls and drives board transitions; this module
decides. It owns candidate identity, the cross-candidate finding-history merge, the outcome
classification (the ADR-0003 D1 merge table), and cache/retry eligibility — so the hard policy lives
in ONE testable place, not spread through the driver loop. It never dispatches a model call itself:
the orchestrator hands it already-collected reviewer + lane results.

Three operations:

- `prepare(...)`   → compute the candidate id, look up a cached completed assessment (a hit means
                     ZERO new model calls), and surface any partial evidence a retry could reuse.
- `complete(...)`  → merge findings into the per-handoff ledger, classify the aggregate outcome, and
                     persist the immutable completed assessment.
- `retry(...)`     → finish an `infrastructure_blocked` / `verification_incomplete` assessment by
                     reusing prior successful evidence and only the newly-supplied missing work —
                     NEVER the builder.

Persistence under ``autopilot/assessments/<hid>/``: ``<candidate>.json`` (immutable completed
aggregate), ``<candidate>.partial.json`` (retry evidence, kept separate), ``findings.json`` (the
persistent per-handoff finding ledger). All writes are atomic; a completed assessment refuses to be
overwritten with a different outcome (immutable history).
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path

_LIB = str(Path(__file__).resolve().parent)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import collab_common as cc  # noqa: E402
import run_budget  # noqa: E402

# ---- outcomes (immutable aggregate result) -------------------------------------------------- #
APPROVED = "approved"
REPAIR_REQUIRED = "repair_required"
INFRASTRUCTURE_BLOCKED = "infrastructure_blocked"
VERIFICATION_INCOMPLETE = "verification_incomplete"
_OUTCOMES = (APPROVED, REPAIR_REQUIRED, INFRASTRUCTURE_BLOCKED, VERIFICATION_INCOMPLETE)

# ---- finding severity / status -------------------------------------------------------------- #
BLOCKING = "blocking"
ADVISORY = "advisory"
OPEN = "open"
FIXED = "fixed"
# Only evidence-backed findings in these categories can block progress (ADR-0003 D1).
BLOCKING_CATEGORIES = frozenset({"correctness", "contract", "safety", "regression"})


class MalformedReview(cc.CollabError):
    """The reviewer's output could not be parsed into a whole-candidate report. Drives
    `verification_incomplete` (a pause), never a silent pass."""


class AssessmentImmutableViolation(cc.CollabError):
    """A completed assessment for a candidate id already exists with a DIFFERENT outcome."""


# --------------------------------------------------------------------------------------------- #
# Records
# --------------------------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Candidate:
    """One immutable snapshot of the work_owner's output plus the plan/rubric/seat it is judged under.
    Its id extends `run_budget.candidate_id` (source + verification plan) with the contract revision,
    the assessment-plan revision, the reviewer rubric, and the seat-profile fingerprint, so a changed
    rubric or seat also mints a new candidate — evidence can never be reused under a different lens."""

    handoff_id: str
    candidate_id: str
    source_manifest_hash: str
    contract_revision: str
    assessment_plan_revision: str
    reviewer_rubric_hash: str
    seat_profile_fingerprint: str
    source_files: tuple[str, ...] = ()

    @classmethod
    def compute(
        cls,
        handoff_id: str,
        *,
        source_manifest: dict,
        source_roots,
        test_command,
        lane_config,
        contract_revision: str = "",
        assessment_plan_revision: str = "",
        reviewer_rubric: str = "",
        seat_profile_fingerprint: str = "",
    ) -> Candidate:
        inner = run_budget.candidate_id(
            source_manifest,
            source_roots=source_roots,
            test_command=test_command,
            lane_config=lane_config,
        )
        rubric_hash = _sha(reviewer_rubric or "")
        payload = {
            "inner": inner,
            "contract_revision": contract_revision,
            "assessment_plan_revision": assessment_plan_revision,
            "reviewer_rubric": rubric_hash,
            "seat_profile_fingerprint": seat_profile_fingerprint,
        }
        cid = "cand:" + _sha(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str))
        return cls(
            handoff_id=handoff_id,
            candidate_id=cid,
            source_manifest_hash=_sha(json.dumps(source_manifest or {}, sort_keys=True, default=str)),
            contract_revision=contract_revision,
            assessment_plan_revision=assessment_plan_revision,
            reviewer_rubric_hash=rubric_hash,
            seat_profile_fingerprint=seat_profile_fingerprint,
            source_files=tuple(
                sorted(str(path) for path in (source_manifest or {}) if path != "__builder_output__")
            ),
        )


@dataclass(frozen=True)
class Finding:
    """One issue attributed to a candidate. Only an OPEN, evidence-backed, blocking-category finding
    can block progress; everything else (including a blocking claim with no evidence) is advisory."""

    fingerprint: str
    source: str  # "reviewer" | "lane:<name>"
    severity: str  # BLOCKING | ADVISORY
    category: str  # correctness | contract | safety | regression | style | ...
    evidence: str
    remediation: str = ""
    status: str = OPEN
    first_seen_candidate: str = ""
    last_seen_candidate: str = ""

    def blocks(self) -> bool:
        return (
            self.severity == BLOCKING
            and self.status == OPEN
            and self.category in BLOCKING_CATEGORIES
            and bool((self.evidence or "").strip())
        )

    @classmethod
    def from_dict(cls, d: dict) -> Finding:
        source = str(d.get("source", "reviewer"))
        category = str(d.get("category", "correctness"))
        severity = str(d.get("severity", BLOCKING))
        evidence = str(d.get("evidence", ""))
        remediation = str(d.get("remediation", ""))
        fp = d.get("fingerprint") or _sha(f"{source}|{category}|{d.get('title', '')}|{evidence}")
        return cls(
            fingerprint=fp,
            source=source,
            severity=severity,
            category=category,
            evidence=evidence,
            remediation=remediation,
            status=str(d.get("status", OPEN)),
            first_seen_candidate=str(d.get("first_seen_candidate", "")),
            last_seen_candidate=str(d.get("last_seen_candidate", "")),
        )


@dataclass(frozen=True)
class ReviewerReport:
    """One whole-candidate reviewer decision. Read-only: `edited_code` MUST be False — a reviewer that
    changed the source it judged is not a reviewer, and parsing rejects it."""

    candidate_id: str
    requirement_coverage: dict
    blocking_findings: tuple
    advisory_findings: tuple
    provisional: bool = True
    edited_code: bool = False

    @classmethod
    def parse(cls, raw, *, candidate_id: str) -> ReviewerReport:
        """Parse a structured reviewer report (a dict, or a JSON string). Anything that is not a
        well-formed report raises `MalformedReview` — a pause, never a silent pass."""
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError) as e:
                raise MalformedReview(f"reviewer output is not valid JSON: {e}") from None
        if not isinstance(raw, dict):
            raise MalformedReview("reviewer report must be a JSON object")
        if raw.get("edited_code"):
            raise MalformedReview("reviewer reported edited_code=true — an approver must not mutate source")
        try:
            blocking = tuple(Finding.from_dict(x) for x in (raw.get("blocking_findings") or []))
            advisory = tuple(Finding.from_dict(x) for x in (raw.get("advisory_findings") or []))
        except (AttributeError, TypeError) as e:
            raise MalformedReview(f"reviewer findings are malformed: {e}") from None
        coverage = raw.get("requirement_coverage")
        if not isinstance(coverage, dict):
            coverage = {}
        return cls(
            candidate_id=candidate_id,
            requirement_coverage=coverage,
            blocking_findings=blocking,
            advisory_findings=advisory,
            provisional=True,
            edited_code=False,
        )


@dataclass(frozen=True)
class CandidateAssessment:
    """The immutable aggregate result of assessing one candidate."""

    handoff_id: str
    candidate_id: str
    outcome: str
    unresolved_findings: tuple
    advisory_findings: tuple
    lane_ledger_ref: str | None = None
    cause: dict | None = None
    budget_snapshot: dict | None = None
    completed_ts: str = ""
    requirement_coverage: dict | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["unresolved_findings"] = [asdict(f) for f in self.unresolved_findings]
        d["advisory_findings"] = [asdict(f) for f in self.advisory_findings]
        return d


# --------------------------------------------------------------------------------------------- #
# Finding ledger (persistent, per-handoff, across candidates)
# --------------------------------------------------------------------------------------------- #


class FindingLedger:
    """The per-handoff record of every finding across its candidates, each `open` or `fixed`. `done`
    must resolve every prior confirmed (blocking) finding, so a defect found on candidate N cannot be
    laundered by a candidate N+1 that merely stopped triggering it (ADR-0002 D3)."""

    def __init__(self, collab, hid: str) -> None:
        self._path = _findings_path(collab, hid)
        self._by_fp: dict[str, Finding] = {}

    @classmethod
    def load(cls, collab, hid: str) -> FindingLedger:
        led = cls(collab, hid)
        led._load()
        return led

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text("utf-8"))
        except FileNotFoundError:
            return
        except OSError, ValueError:
            raise cc.CollabError(
                f"finding ledger {self._path} is corrupt — refusing to proceed (a lost finding record "
                f"could launder a prior defect); inspect/repair or remove it"
            ) from None
        for d in data.get("findings", []) if isinstance(data, dict) else []:
            f = Finding.from_dict(d)
            self._by_fp[f.fingerprint] = f

    def merge(self, findings, *, candidate_id: str) -> None:
        """Fold this candidate's findings in: refresh/open those present, add the new, and mark
        `fixed` any previously-open finding this NEW candidate no longer triggers."""
        seen: set[str] = set()
        for f in findings:
            seen.add(f.fingerprint)
            prior = self._by_fp.get(f.fingerprint)
            if prior is not None:
                self._by_fp[f.fingerprint] = replace(
                    prior,
                    status=OPEN,
                    severity=f.severity,
                    category=f.category,
                    evidence=f.evidence or prior.evidence,
                    remediation=f.remediation or prior.remediation,
                    last_seen_candidate=candidate_id,
                )
            else:
                self._by_fp[f.fingerprint] = replace(
                    f,
                    first_seen_candidate=f.first_seen_candidate or candidate_id,
                    last_seen_candidate=candidate_id,
                    status=OPEN,
                )
        for fp, fnd in list(self._by_fp.items()):
            if fp not in seen and fnd.status == OPEN and fnd.last_seen_candidate != candidate_id:
                self._by_fp[fp] = replace(fnd, status=FIXED)

    def unresolved(self) -> tuple:
        """Findings still open (not fixed), most-severe first."""
        openf = [f for f in self._by_fp.values() if f.status == OPEN]
        return tuple(sorted(openf, key=lambda f: (not f.blocks(), f.fingerprint)))

    def all(self) -> tuple:
        return tuple(self._by_fp.values())

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"findings": [asdict(f) for f in self._by_fp.values()]}
        cc.safe_write(self._path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


# --------------------------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------------------------- #


def prepare(collab, hid: str, *, candidate: Candidate) -> dict:
    """Given a computed candidate, report cache + retry state WITHOUT any model call.

    Returns ``{"candidate", "cached", "partial"}`` — ``cached`` is a completed assessment to reuse
    (zero new reviewer/lane calls) if this exact candidate was already assessed; ``partial`` is prior
    successful evidence a retry can reuse.
    """
    return {
        "candidate": candidate,
        "cached": load_assessment(collab, hid, candidate.candidate_id),
        "partial": load_partial(collab, hid, candidate.candidate_id),
    }


def classify(reviewer_report, lane_ledger: dict, unresolved) -> tuple[str, dict | None]:
    """The ADR-0003 D1 merge table. Returns ``(outcome, cause)``.

    Order matters: an infrastructure failure or an unparseable review can never be laundered into a
    clean pass, so they are decided before the finding merge is trusted.
    """
    if lane_ledger.get("tool_error"):
        return INFRASTRUCTURE_BLOCKED, dict(lane_ledger["tool_error"])
    if reviewer_report is None:
        return VERIFICATION_INCOMPLETE, {"reason": "malformed_review"}
    if lane_ledger.get("incomplete") or int(lane_ledger.get("overflow", 0) or 0) > 0:
        return VERIFICATION_INCOMPLETE, {
            "reason": "findings_overflow",
            "overflow": int(lane_ledger.get("overflow", 0) or 0),
            "unverified": lane_ledger.get("unverified", []),
        }
    # Clean run: repair if ANY open blocker survives (reviewer evidence-backed or lane-confirmed),
    # else approved. A lane refutation only clears its own finding; it never erases a reviewer blocker.
    blocking = [f for f in unresolved if f.blocks()]
    return (REPAIR_REQUIRED if blocking else APPROVED), None


def complete(
    collab,
    hid: str,
    candidate: Candidate,
    *,
    reviewer_report,
    lane_ledger: dict,
    budget_snapshot: dict | None = None,
    lane_ledger_ref: str | None = None,
    now_ts: str | None = None,
) -> CandidateAssessment:
    """Merge findings, classify the aggregate outcome, and persist. A clean run (reviewer + lanes ok)
    updates the finding ledger and saves an immutable completed assessment; an infra/incomplete run
    persists partial evidence separately for a later `retry`."""
    ts = now_ts or _iso_now()
    lane_ledger = lane_ledger or {}

    # Only trust the finding merge on a run that actually completed review + verification.
    infra_or_incomplete = (
        bool(lane_ledger.get("tool_error"))
        or reviewer_report is None
        or bool(lane_ledger.get("incomplete"))
        or int(lane_ledger.get("overflow", 0) or 0) > 0
    )

    if infra_or_incomplete:
        outcome, cause = classify(reviewer_report, lane_ledger, ())
        assessment = CandidateAssessment(
            handoff_id=hid,
            candidate_id=candidate.candidate_id,
            outcome=outcome,
            unresolved_findings=(),
            advisory_findings=(),
            lane_ledger_ref=lane_ledger_ref,
            cause=cause,
            budget_snapshot=budget_snapshot,
            completed_ts=ts,
            requirement_coverage=(
                dict(reviewer_report.requirement_coverage) if reviewer_report is not None else {}
            ),
        )
        save_partial(
            collab,
            hid,
            candidate.candidate_id,
            {
                "reviewer_report": _report_dict(reviewer_report),
                "lane_ledger": lane_ledger,
                "outcome": outcome,
                "cause": cause,
            },
        )
        return assessment

    if reviewer_report is None:  # narrowed by infra_or_incomplete above; retained for static proof
        raise AssertionError("reviewer_report unexpectedly absent on completed assessment path")
    ledger = FindingLedger.load(collab, hid)
    findings = (
        list(reviewer_report.blocking_findings)
        + list(reviewer_report.advisory_findings)
        + _lane_findings(lane_ledger)
    )
    ledger.merge(findings, candidate_id=candidate.candidate_id)
    ledger.save()
    unresolved = ledger.unresolved()
    outcome, cause = classify(reviewer_report, lane_ledger, unresolved)
    advisories = tuple(f for f in unresolved if not f.blocks())
    assessment = CandidateAssessment(
        handoff_id=hid,
        candidate_id=candidate.candidate_id,
        outcome=outcome,
        unresolved_findings=tuple(f for f in unresolved if f.blocks()),
        advisory_findings=advisories,
        lane_ledger_ref=lane_ledger_ref,
        cause=cause,
        budget_snapshot=budget_snapshot,
        completed_ts=ts,
        requirement_coverage=dict(reviewer_report.requirement_coverage),
    )
    save_assessment(collab, assessment)
    return assessment


def retry(
    collab,
    hid: str,
    candidate: Candidate,
    *,
    reviewer_report=None,
    lane_ledger: dict | None = None,
    budget_snapshot: dict | None = None,
    now_ts: str | None = None,
) -> CandidateAssessment:
    """Complete a previously infra/incomplete assessment by reusing stored partial evidence and only
    the newly-supplied missing work. NEVER invokes the builder (the candidate is unchanged)."""
    partial = load_partial(collab, hid, candidate.candidate_id) or {}
    # Run only the missing work: a freshly-supplied lane pass SUPERSEDES the prior (failed/partial)
    # one; if none is supplied, the stored lane evidence is reused as-is. Likewise for the reviewer.
    merged_ledger = dict(lane_ledger) if lane_ledger is not None else dict(partial.get("lane_ledger") or {})
    report = reviewer_report
    if report is None and partial.get("reviewer_report") is not None:
        report = ReviewerReport.parse(partial["reviewer_report"], candidate_id=candidate.candidate_id)
    return complete(
        collab,
        hid,
        candidate,
        reviewer_report=report,
        lane_ledger=merged_ledger,
        budget_snapshot=budget_snapshot,
        now_ts=now_ts,
    )


# --------------------------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------------------------- #


def _assessment_dir(collab, hid: str) -> Path:
    return Path(collab) / "autopilot" / "assessments" / cc.slugify(hid)


def _findings_path(collab, hid: str) -> Path:
    return _assessment_dir(collab, hid) / "findings.json"


def assessment_path(collab, hid: str, candidate_id: str) -> Path:
    return _assessment_dir(collab, hid) / f"{_cand_slug(candidate_id)}.json"


def _partial_path(collab, hid: str, candidate_id: str) -> Path:
    return _assessment_dir(collab, hid) / f"{_cand_slug(candidate_id)}.partial.json"


def save_assessment(collab, assessment: CandidateAssessment) -> Path:
    """Persist a COMPLETED assessment immutably. Refuses to overwrite an existing record for the same
    candidate with a DIFFERENT outcome (history is immutable)."""
    path = assessment_path(collab, assessment.handoff_id, assessment.candidate_id)
    if path.exists():
        try:
            prior = json.loads(path.read_text("utf-8"))
        except OSError, ValueError:
            prior = None
        if isinstance(prior, dict) and prior.get("outcome") not in (None, assessment.outcome):
            raise AssessmentImmutableViolation(
                f"assessment {path} already recorded outcome {prior.get('outcome')!r}; "
                f"refusing to overwrite with {assessment.outcome!r}"
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    cc.safe_write(path, json.dumps(assessment.to_dict(), indent=2, sort_keys=True) + "\n")
    return path


def load_assessment(collab, hid: str, candidate_id: str) -> CandidateAssessment | None:
    path = assessment_path(collab, hid, candidate_id)
    try:
        d = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return None
    except OSError, ValueError:
        raise cc.CollabError(f"assessment {path} is corrupt — refusing to reuse it") from None
    return _assessment_from_dict(d)


def save_partial(collab, hid: str, candidate_id: str, evidence: dict) -> Path:
    path = _partial_path(collab, hid, candidate_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    cc.safe_write(path, json.dumps(evidence, indent=2, sort_keys=True, default=str) + "\n")
    return path


def load_partial(collab, hid: str, candidate_id: str) -> dict | None:
    path = _partial_path(collab, hid, candidate_id)
    try:
        return json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return None
    except OSError, ValueError:
        return None


# --------------------------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------------------------- #


def _lane_findings(lane_ledger: dict) -> list:
    """Confirmed lane blockers become blocking correctness findings; refutations contribute nothing."""
    out = []
    for c in lane_ledger.get("confirmed") or []:
        d = dict(c)
        d.setdefault("source", f"lane:{d.get('lane', 'unknown')}")
        d.setdefault("severity", BLOCKING)
        d.setdefault("category", "correctness")
        out.append(Finding.from_dict(d))
    return out


def _report_dict(report) -> dict | None:
    if report is None:
        return None
    return {
        "requirement_coverage": report.requirement_coverage,
        "blocking_findings": [asdict(f) for f in report.blocking_findings],
        "advisory_findings": [asdict(f) for f in report.advisory_findings],
        "edited_code": report.edited_code,
    }


def _assessment_from_dict(d: dict) -> CandidateAssessment:
    return CandidateAssessment(
        handoff_id=d.get("handoff_id", ""),
        candidate_id=d.get("candidate_id", ""),
        outcome=d.get("outcome", ""),
        unresolved_findings=tuple(Finding.from_dict(x) for x in d.get("unresolved_findings", [])),
        advisory_findings=tuple(Finding.from_dict(x) for x in d.get("advisory_findings", [])),
        lane_ledger_ref=d.get("lane_ledger_ref"),
        cause=d.get("cause"),
        budget_snapshot=d.get("budget_snapshot"),
        completed_ts=d.get("completed_ts", ""),
        requirement_coverage=d.get("requirement_coverage") or {},
    )


def _cand_slug(candidate_id: str) -> str:
    # candidate_id is "cand:<hex>"; keep it filesystem-safe and short-ish.
    return cc.slugify(candidate_id.replace("cand:", "cand-")[:64])


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _iso_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
