"""Typed, pure resolution of risk-tiered adversarial assurance plans.

This module deliberately does not dispatch a model or mutate a handoff: it
converts the lane and seat configuration plus a candidate's guardrails into one
frozen ``VerificationPlan``. The candidate lifecycle hands that same object to
candidate identity and lane execution instead of resolving policy twice.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_LIB = str(Path(__file__).resolve().parent)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import adapter_profiles as adapters  # noqa: E402, I001
import collab_common as cc  # noqa: E402


HIGH_RISK_GUARDRAILS = frozenset(
    {
        "money",
        "execution",
        "auth",
        "broker",
        "integration",
        "market-data",
        "time",
        "data-integrity",
        "concurrency",
    }
)
_ROLE_ACCESS = {
    "builder": adapters.WRITE,
    "reviewer": adapters.READ_TEST,
    "breaker": adapters.READ_TEST,
    "verifier": adapters.READ_TEST,
}
_ASSESSMENT_ROLES = frozenset({"reviewer", "breaker", "verifier"})
_PROFILE_IDS = ("baseline", "high-risk-diverse")
_ALIASES = {
    "marketdata": "market-data",
    "market-data-causality": "market-data",
    "data-integrity-under-concurrent-autopilots": "data-integrity",
    "untrusted-output": "untrusted-agent-output",
}


class VerificationPlanError(cc.CollabError):
    """The assurance configuration cannot safely produce a candidate plan."""


@dataclass(frozen=True)
class LaneSpec:
    """One named assurance contract and its deterministic guardrail selectors."""

    id: str
    title: str
    category: str
    checklist: tuple[str, ...]
    baseline_guardrails: tuple[str, ...]
    high_risk_guardrails: tuple[str, ...]
    always_baseline: bool = False
    revision: str = ""

    @property
    def fingerprint(self) -> str:
        return "lane:" + _digest(self.identity_data())[:16]

    def identity_data(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "category": self.category,
            "checklist": list(self.checklist),
            "baseline_guardrails": list(self.baseline_guardrails),
            "high_risk_guardrails": list(self.high_risk_guardrails),
            "always_baseline": self.always_baseline,
            "revision": self.revision,
        }


@dataclass(frozen=True)
class AssessmentProfile:
    """The compiled, repository-capable breaker/verifier pair for one pass."""

    id: str
    breaker_seat: str
    verifier_seat: str
    breaker_model: str
    verifier_model: str
    breaker_provider: str
    verifier_provider: str
    breaker_execution_fingerprint: str
    verifier_execution_fingerprint: str
    fingerprint: str
    # Runtime details are deliberately kept beside the identity attributes: the exact compiled
    # profile that contributed to the candidate id is also the profile the lane runner dispatches.
    breaker_cmd: tuple[str, ...] = ()
    verifier_cmd: tuple[str, ...] = ()
    breaker_system: str = ""
    verifier_system: str = ""
    breaker_timeout: float = 600.0
    verifier_timeout: float = 600.0
    breaker_unset_env: tuple[str, ...] = ()
    verifier_unset_env: tuple[str, ...] = ()

    @property
    def provider_set(self) -> tuple[str, ...]:
        return tuple(sorted({self.breaker_provider, self.verifier_provider}))

    def identity_data(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "breaker": {
                "seat": self.breaker_seat,
                "model": self.breaker_model,
                "provider": self.breaker_provider,
                "execution_fingerprint": self.breaker_execution_fingerprint,
            },
            "verifier": {
                "seat": self.verifier_seat,
                "model": self.verifier_model,
                "provider": self.verifier_provider,
                "execution_fingerprint": self.verifier_execution_fingerprint,
            },
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class LanePass:
    """One breaker→verifier pair over one or more selected contract specifications."""

    id: str
    profile: AssessmentProfile
    specs: tuple[LaneSpec, ...]
    composite: bool

    @property
    def contract_ids(self) -> tuple[str, ...]:
        return tuple(spec.id for spec in self.specs)

    @property
    def composite_payload(self) -> dict[str, Any]:
        """A fresh JSON-safe prompt payload for the one high-risk composite pair.

        The pass itself stays immutable; callers may format or add transient prompt text to this
        returned copy without changing what candidate identity means.
        """
        return {
            "pass": self.id,
            "composite": self.composite,
            "profile": self.profile.identity_data(),
            "contracts": list(self.contract_ids),
            "checklists": [
                {"id": spec.id, "title": spec.title, "items": list(spec.checklist)} for spec in self.specs
            ],
        }

    def identity_data(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "composite": self.composite,
            "profile": self.profile.identity_data(),
            "contracts": [spec.identity_data() for spec in self.specs],
        }


@dataclass(frozen=True)
class VerificationPlan:
    """The single immutable assurance decision for one candidate.

    ``identity_payload`` is canonical JSON rather than a mutable mapping.  It is safe to hand to
    candidate-id code verbatim, while ``identity_data`` is available for dashboard/debug rendering.
    """

    lane_config_revision: str
    prompt_revision: str
    assessment_profile_revision: str
    guardrails: tuple[str, ...]
    baseline: LanePass
    high_risk: LanePass | None
    identity_payload: str
    identity_digest: str

    @property
    def passes(self) -> tuple[LanePass, ...]:
        return (self.baseline,) if self.high_risk is None else (self.baseline, self.high_risk)

    def identity_data(self) -> dict[str, Any]:
        return json.loads(self.identity_payload)


def normalize_guardrail(value: object) -> str:
    """Return the canonical guardrail token used by configuration selection."""
    if not isinstance(value, str):
        raise VerificationPlanError(f"guardrail must be a string, got {type(value).__name__}")
    token = value.strip().casefold()
    token = re.sub(r"[\s_]+", "-", token)
    token = re.sub(r"-+", "-", token)
    if not token:
        raise VerificationPlanError("guardrail must not be empty")
    return _ALIASES.get(token, token)


def normalize_guardrails(guardrails: Sequence[object] | None) -> tuple[str, ...]:
    """Normalize, deduplicate, and sort user/config guardrails deterministically."""
    if guardrails is None:
        return ()
    if isinstance(guardrails, str) or not isinstance(guardrails, Sequence):
        raise VerificationPlanError("guardrails must be a sequence of strings")
    return tuple(sorted({normalize_guardrail(value) for value in guardrails}))


def load_lane_specs(path: str | Path) -> tuple[LaneSpec, ...]:
    """Read and parse a version-2 lane configuration without involving runtime execution."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise VerificationPlanError(f"cannot read lane configuration {path}: {exc}") from exc
    return parse_lane_specs(raw)


def parse_lane_specs(config: Mapping[str, Any]) -> tuple[LaneSpec, ...]:
    """Validate a version-2 lanes document into immutable, canonical ``LaneSpec`` records."""
    if not isinstance(config, Mapping):
        raise VerificationPlanError("lane configuration must be an object")
    if config.get("version") != 2:
        raise VerificationPlanError("lane configuration must use version 2")
    raw_specs = config.get("lane_specs")
    if not isinstance(raw_specs, list) or not raw_specs:
        raise VerificationPlanError("lane configuration version 2 requires a non-empty 'lane_specs' list")

    specs: list[LaneSpec] = []
    ids: set[str] = set()
    for index, raw in enumerate(raw_specs, 1):
        if not isinstance(raw, Mapping):
            raise VerificationPlanError(f"lane_specs[{index}] must be an object")
        ident = _text(raw.get("id"), f"lane_specs[{index}].id")
        if ident in ids:
            raise VerificationPlanError(f"duplicate lane spec id {ident!r}")
        ids.add(ident)
        title = _text(raw.get("title"), f"lane spec {ident!r}.title")
        category = _text(raw.get("category"), f"lane spec {ident!r}.category")
        if category not in {"generic", "trading"}:
            raise VerificationPlanError(
                f"lane spec {ident!r}.category must be 'generic' or 'trading', got {category!r}"
            )
        checklist = _text_tuple(raw.get("checklist"), f"lane spec {ident!r}.checklist", required=True)
        selection = raw.get("selection") or {}
        if not isinstance(selection, Mapping):
            raise VerificationPlanError(f"lane spec {ident!r}.selection must be an object")
        baseline = _guardrail_tuple(
            selection.get("baseline", raw.get("baseline_guardrails", ())),
            f"lane spec {ident!r}.selection.baseline",
        )
        high_risk = _guardrail_tuple(
            selection.get("high-risk", raw.get("high_risk_guardrails", ())),
            f"lane spec {ident!r}.selection.high-risk",
        )
        unknown_high = set(high_risk) - HIGH_RISK_GUARDRAILS
        if unknown_high:
            raise VerificationPlanError(
                f"lane spec {ident!r} has unknown high-risk guardrail(s): {', '.join(sorted(unknown_high))}"
            )
        always_baseline = raw.get("always_baseline", False)
        if not isinstance(always_baseline, bool):
            raise VerificationPlanError(f"lane spec {ident!r}.always_baseline must be a boolean")
        if not (always_baseline or baseline or high_risk):
            raise VerificationPlanError(f"lane spec {ident!r} has no selection trigger")
        specs.append(
            LaneSpec(
                id=ident,
                title=title,
                category=category,
                checklist=checklist,
                baseline_guardrails=baseline,
                high_risk_guardrails=high_risk,
                always_baseline=always_baseline,
                revision=str(raw.get("revision") or config.get("revision") or ""),
            )
        )
    return tuple(sorted(specs, key=lambda spec: spec.id))


def resolve_assessment_profiles(seats_document: Mapping[str, Any]) -> dict[str, AssessmentProfile]:
    """Compile and validate the two configuration-owned assessment profiles.

    This is intentionally stricter than ``autopilot.load_seats``: it validates only the managed
    assurance surface and leaves every unrelated/non-assessment seat alone.  A profile cannot use
    a text-only adapter, an access policy weaker than ``read_test``, the same execution twice, or a
    provider overlapping the baseline's provider set.
    """
    if not isinstance(seats_document, Mapping):
        raise VerificationPlanError("seats configuration must be an object")
    models = seats_document.get("models")
    seats = seats_document.get("seats")
    profiles = seats_document.get("assessment_profiles")
    if not isinstance(models, Mapping):
        raise VerificationPlanError("seats configuration requires a 'models' catalog")
    if not isinstance(seats, Mapping):
        raise VerificationPlanError("seats configuration requires a 'seats' object")
    if not isinstance(profiles, Mapping):
        raise VerificationPlanError("seats configuration requires an 'assessment_profiles' object")

    profile_revision = _text(
        seats_document.get("assessment_profile_revision"), "assessment_profile_revision"
    )
    _validate_role_seats(seats, models)
    raw_profiles = {profile_id: profiles.get(profile_id) for profile_id in _PROFILE_IDS}
    for profile_id, raw in raw_profiles.items():
        if not isinstance(raw, Mapping):
            raise VerificationPlanError(f"assessment profile {profile_id!r} is required")

    materialized: dict[str, dict[str, dict[str, str]]] = {}

    def expand(profile_id: str, stack: tuple[str, ...] = ()) -> dict[str, dict[str, str]]:
        if profile_id in materialized:
            return materialized[profile_id]
        if profile_id in stack:
            lineage = " -> ".join((*stack, profile_id))
            raise VerificationPlanError(f"assessment profile inheritance cycle: {lineage}")
        raw = raw_profiles.get(profile_id)
        if not isinstance(raw, Mapping):
            raise VerificationPlanError(f"unknown assessment profile {profile_id!r}")
        inherited = raw.get("extends")
        if inherited is None:
            state = {"breaker": {"seat": "breaker"}, "verifier": {"seat": "verifier"}}
        else:
            parent = _text(inherited, f"assessment profile {profile_id!r}.extends")
            state = {role: dict(executor) for role, executor in expand(parent, (*stack, profile_id)).items()}
        for field in ("breaker", "verifier"):
            if field in raw:
                state[field] = _merge_executor(state[field], raw[field], profile_id, field)
        overrides = raw.get("overrides", {})
        if not isinstance(overrides, Mapping):
            raise VerificationPlanError(f"assessment profile {profile_id!r}.overrides must be an object")
        unknown_roles = set(overrides) - {"breaker", "verifier"}
        if unknown_roles:
            raise VerificationPlanError(
                f"assessment profile {profile_id!r} may only override breaker/verifier, "
                f"not {sorted(unknown_roles)}"
            )
        for role, override in overrides.items():
            state[role] = _merge_executor(state[role], override, profile_id, role)
        for role in ("breaker", "verifier"):
            # The baseline profile normally inherits its model from the
            # canonical logical seat.  That makes a dashboard model switch
            # meaningful and lets the profile validator reject a switch that
            # would collapse provider diversity.  A profile may still pin an
            # explicit model (the high-risk profile does) when that is the
            # policy requirement.
            if not state[role].get("model"):
                seat_cfg = seats.get(role)
                state[role]["model"] = _text(
                    seat_cfg.get("model") if isinstance(seat_cfg, Mapping) else None,
                    f"logical role seat {role!r}.model",
                )
        materialized[profile_id] = state
        return state

    resolved: dict[str, AssessmentProfile] = {}
    for profile_id in _PROFILE_IDS:
        selected = expand(profile_id)
        breaker = _compile_profile_executor(profile_id, "breaker", selected["breaker"], seats, models)
        verifier = _compile_profile_executor(profile_id, "verifier", selected["verifier"], seats, models)
        if breaker["execution_fingerprint"] == verifier["execution_fingerprint"]:
            raise VerificationPlanError(
                f"assessment profile {profile_id!r} breaker/verifier execution fingerprints must differ"
            )
        profile_data = {
            "id": profile_id,
            "revision": profile_revision,
            "breaker": breaker,
            "verifier": verifier,
        }
        resolved[profile_id] = AssessmentProfile(
            id=profile_id,
            breaker_seat="breaker",
            verifier_seat="verifier",
            breaker_model=breaker["model"],
            verifier_model=verifier["model"],
            breaker_provider=breaker["provider"],
            verifier_provider=verifier["provider"],
            breaker_execution_fingerprint=breaker["execution_fingerprint"],
            verifier_execution_fingerprint=verifier["execution_fingerprint"],
            fingerprint="profile:" + _digest(profile_data)[:16],
            breaker_cmd=tuple(breaker["cmd"]),
            verifier_cmd=tuple(verifier["cmd"]),
            breaker_system=breaker["system"],
            verifier_system=verifier["system"],
            breaker_timeout=breaker["timeout"],
            verifier_timeout=verifier["timeout"],
            breaker_unset_env=tuple(breaker["unset_env"]),
            verifier_unset_env=tuple(verifier["unset_env"]),
        )

    baseline = resolved["baseline"]
    high = resolved["high-risk-diverse"]
    overlap = set(baseline.provider_set) & set(high.provider_set)
    if overlap:
        raise VerificationPlanError(
            "high-risk-diverse providers must be disjoint from baseline; overlapping provider(s): "
            + ", ".join(sorted(overlap))
        )
    return resolved


def resolve_verification_plan(
    lanes_config: Mapping[str, Any], seats_document: Mapping[str, Any], *, guardrails: Sequence[object] | None
) -> VerificationPlan:
    """Resolve one candidate's complete baseline plus optional high-risk composite assurance plan."""
    specs = parse_lane_specs(lanes_config)
    profiles = resolve_assessment_profiles(seats_document)
    lane_config_revision = _text(lanes_config.get("revision"), "lane configuration revision")
    prompt_revision = _text(lanes_config.get("prompt_revision"), "lane configuration prompt_revision")
    profile_revision = _text(
        seats_document.get("assessment_profile_revision"), "assessment_profile_revision"
    )
    normalized = normalize_guardrails(guardrails)
    selected = set(normalized)

    baseline_specs = tuple(
        spec
        for spec in specs
        if spec.always_baseline or bool(set(spec.baseline_guardrails) & selected)
    )
    if not baseline_specs:
        raise VerificationPlanError("baseline plan must contain at least the change-regression contract")
    high_requested = bool(selected & HIGH_RISK_GUARDRAILS)
    high_specs = tuple(spec for spec in specs if set(spec.high_risk_guardrails) & selected)
    if high_requested and not high_specs:
        raise VerificationPlanError(
            "safety-critical guardrails require a matching high-risk composite contract; "
            "refusing baseline fallback"
        )

    baseline = LanePass(id="baseline", profile=profiles["baseline"], specs=baseline_specs, composite=False)
    high = (
        LanePass(
            id="high-risk-diverse",
            profile=profiles["high-risk-diverse"],
            specs=high_specs,
            composite=True,
        )
        if high_requested
        else None
    )
    payload = {
        "schema": "verification-plan-v1",
        "lane_config_revision": lane_config_revision,
        "prompt_revision": prompt_revision,
        "assessment_profile_revision": profile_revision,
        "guardrails": list(normalized),
        "passes": [lane_pass.identity_data() for lane_pass in (baseline,) if lane_pass is not None]
        + ([high.identity_data()] if high is not None else []),
    }
    identity_payload = _canonical_json(payload)
    return VerificationPlan(
        lane_config_revision=lane_config_revision,
        prompt_revision=prompt_revision,
        assessment_profile_revision=profile_revision,
        guardrails=normalized,
        baseline=baseline,
        high_risk=high,
        identity_payload=identity_payload,
        identity_digest="plan:" + hashlib.sha256(identity_payload.encode("utf-8")).hexdigest(),
    )


def _validate_role_seats(seats: Mapping[str, Any], models: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    compiled: dict[str, dict[str, Any]] = {}
    for role, expected_access in _ROLE_ACCESS.items():
        cfg = seats.get(role)
        if not isinstance(cfg, Mapping):
            raise VerificationPlanError(f"required logical role seat {role!r} is missing")
        if cfg.get("backend") != "cli":
            raise VerificationPlanError(f"logical role seat {role!r} must use backend 'cli'")
        if cfg.get("role") != role:
            raise VerificationPlanError(f"logical role seat {role!r} must declare role={role!r}")
        if cfg.get("access") != expected_access:
            raise VerificationPlanError(
                f"logical role seat {role!r} must declare access={expected_access!r}"
            )
        try:
            out = adapters.compile_seat(role, dict(cfg), dict(models))
        except cc.CollabError as exc:
            raise VerificationPlanError(f"logical role seat {role!r} does not compile safely: {exc}") from exc
        if role in _ASSESSMENT_ROLES:
            _require_assessment_executor(role, out)
        compiled[role] = out
    for name, cfg in seats.items():
        if name not in _ROLE_ACCESS and isinstance(cfg, Mapping) and cfg.get("role") in _ROLE_ACCESS:
            raise VerificationPlanError(
                f"logical role {cfg['role']!r} is duplicated by non-canonical seat {name!r}"
            )
    return compiled


def _compile_profile_executor(
    profile_id: str,
    role: str,
    selected: Mapping[str, str],
    seats: Mapping[str, Any],
    models: Mapping[str, Any],
) -> dict[str, Any]:
    seat = selected.get("seat")
    if seat != role:
        raise VerificationPlanError(
            f"assessment profile {profile_id!r} {role} must use logical seat {role!r}, not {seat!r}"
        )
    cfg = seats[role]
    overlay = dict(cfg)
    overlay["model"] = selected["model"]
    try:
        compiled = adapters.compile_seat(role, overlay, dict(models))
    except cc.CollabError as exc:
        raise VerificationPlanError(
            f"assessment profile {profile_id!r} {role} does not compile safely: {exc}"
        ) from exc
    _require_assessment_executor(f"{profile_id}.{role}", compiled)
    return {
        "model": str(compiled.get("model") or ""),
        "provider": str(compiled.get("provider") or ""),
        "execution_fingerprint": adapters.execution_fingerprint(compiled),
        "cmd": tuple(str(part) for part in compiled.get("cmd") or ()),
        "system": str(cfg.get("system") or ""),
        "timeout": float(cfg.get("timeout", 600.0)),
        "unset_env": tuple(str(name) for name in compiled.get("unset_env") or ()),
    }


def _require_assessment_executor(label: str, compiled: Mapping[str, Any]) -> None:
    policy = compiled.get("policy") or {}
    if policy.get("access") != adapters.READ_TEST:
        raise VerificationPlanError(f"assessment executor {label!r} must enforce read_test access")
    if not compiled.get("repository_capable"):
        raise VerificationPlanError(f"assessment executor {label!r} must be repository-capable")
    if not compiled.get("provider"):
        raise VerificationPlanError(f"assessment executor {label!r} model requires provider metadata")


def _merge_executor(
    prior: Mapping[str, str], raw: object, profile_id: str, role: str
) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        raise VerificationPlanError(f"assessment profile {profile_id!r}.{role} must be an object")
    unexpected = set(raw) - {"seat", "model"}
    if unexpected:
        raise VerificationPlanError(
            f"assessment profile {profile_id!r}.{role} has unsupported field(s): {sorted(unexpected)}"
        )
    out = dict(prior)
    if "seat" in raw and _text(raw["seat"], f"assessment profile {profile_id!r}.{role}.seat") != role:
        raise VerificationPlanError(
            f"assessment profile {profile_id!r}.{role} must retain logical seat {role!r}"
        )
    out["seat"] = role
    if "model" in raw:
        out["model"] = _text(raw["model"], f"assessment profile {profile_id!r}.{role}.model")
    return out


def _guardrail_tuple(value: object, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise VerificationPlanError(f"{label} must be a list of guardrail strings")
    normalized = tuple(normalize_guardrail(item) for item in value)
    if len(set(normalized)) != len(normalized):
        raise VerificationPlanError(f"{label} must not repeat a guardrail")
    return tuple(sorted(normalized))


def _text_tuple(value: object, label: str, *, required: bool = False) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise VerificationPlanError(f"{label} must be a list of strings")
    items = tuple(_text(item, label) for item in value)
    if required and not items:
        raise VerificationPlanError(f"{label} must not be empty")
    return items


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise VerificationPlanError(f"{label} must be a non-empty string")
    return value.strip()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
