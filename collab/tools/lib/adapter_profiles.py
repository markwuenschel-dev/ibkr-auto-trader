"""adapter_profiles — typed model adapters + enforced seat policies (ADR-0003 D3).

The driver runs a *seat* as an argv over stdin/stdout. Historically a seat's runnable argv was
built by blindly concatenating the model catalog's launch template with the seat's raw
``model_args`` (`autopilot.load_seats:244`). That composition has no idea which *adapter* the model
launches, so a Claude-only permission flag (``--permission-mode``/``--allowedTools``) attached to a
seat whose model is an OpenAI adapter is handed straight to that adapter, which exits 2 — the live
030 failure — and, worse, an "assessment" seat can silently be granted write/edit capability it must
never have.

This module makes capability **typed and enforced**:

- A `SeatPolicy` states a seat's *intent* — its role and its access level
  (``write`` / ``read`` / ``read_test``) — adapter-neutrally.
- An `AdapterProfile` (Claude / OpenAI-repo / OpenAI-compatible / legacy) knows how to render ONLY
  the flags its own CLI understands for a given policy, and whether it can *enforce* that policy at
  all.
- `compile_seat` resolves a seat's model to its adapter and, for a **managed** seat (one that
  declares ``role``/``access``), renders the argv from the policy — an adapter that cannot enforce
  the policy is a fatal error, raised *before* any argv is produced. For a **legacy** seat (raw
  ``model_args``, no policy) it keeps the old composition but **guards** against foreign flags, so
  the cross-adapter crash is impossible even before migration. An explicit-``cmd`` seat is verbatim
  and non-switchable.

Nothing here dispatches a model or touches the network — it is pure argv/policy logic plus a small
seat-change audit log, so it is fully unit-testable.
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

_LIB = str(Path(__file__).resolve().parent)
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import collab_common as cc  # noqa: E402

# ---- adapter ids ---------------------------------------------------------------------------- #
CLAUDE = "claude"
OPENAI_REPO = "openai-repo"
OPENAI_COMPAT = "openai-compatible"
LEGACY = "legacy"

# ---- access levels (SeatPolicy.access) ------------------------------------------------------ #
WRITE = "write"  # builder: file writes + allow-listed command run
READ = "read"  # reviewer: read-only; no write, no command run
READ_TEST = "read_test"  # breaker/verifier: read + run allow-listed test commands, NO source writes
_ACCESS = (WRITE, READ, READ_TEST)

# The allow-listed check tools an assessment (read_test) or builder (write) seat may run. Kept in one
# place so the Claude allow-list and the OpenAI adapter's _RUN_ALLOW stay conceptually aligned.
_TEST_BASH_TOOLS = (
    "Bash(uv run pytest:*)",
    "Bash(python -m pytest:*)",
    "Bash(uv run ruff:*)",
    "Bash(ruff check:*)",
)

_ROLE_DEFAULT_ACCESS = {
    "builder": WRITE,
    "reviewer": READ,
    "breaker": READ_TEST,
    "verifier": READ_TEST,
}


@dataclass(frozen=True)
class SeatPolicy:
    """A seat's intent, adapter-neutral. `access` is the enforced capability level; `tool_policy`
    carries non-capability config an adapter may consume (e.g. ``{"api": "responses"}``)."""

    role: str
    access: str
    tool_policy: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.access not in _ACCESS:
            raise cc.CollabError(f"unknown access level {self.access!r} (expected one of {_ACCESS})")


# --------------------------------------------------------------------------------------------- #
# Adapter profiles
# --------------------------------------------------------------------------------------------- #


class AdapterProfile:
    """Base adapter. Subclasses know their capability flags, so they can (a) render only the flags
    they understand for a policy, (b) strip their own capability flags from a launch template to
    recover a clean base, and (c) reject a foreign flag that would crash them."""

    id = LEGACY
    switchable = False
    # capability flags this adapter owns, by arity, for stripping + foreign detection.
    _zero: frozenset[str] = frozenset()  # store_true flags (no argument)
    _one: frozenset[str] = frozenset()  # flags taking exactly one argument
    _variadic: frozenset[str] = frozenset()  # flags taking 1..N args until the next --flag

    def supports(self, policy: SeatPolicy) -> tuple[bool, str]:
        return False, "legacy explicit-command seat is not policy-managed"

    def render_argv(self, policy: SeatPolicy) -> list[str]:
        return []

    def base_argv(self, template: list[str]) -> list[str]:
        """The launch template with THIS adapter's capability flags removed, so the policy fully
        determines capability (a ``-build`` template that baked in ``--write`` becomes the clean
        base). Config flags (``--repo-root``, ``--base``, ``--model``, ``--api``, …) are kept."""
        return _strip_flags(template, self._zero, self._one, self._variadic)

    def foreign_reason(self, args: list[str]) -> str | None:
        """A human reason iff `args` contains a flag this adapter does not understand (would crash
        it), else None. Used to guard the legacy ``model_args`` path against the 030 leak."""
        return None


class ClaudeAdapter(AdapterProfile):
    id = CLAUDE
    switchable = True
    _one = frozenset({"--permission-mode"})
    _variadic = frozenset({"--allowedTools", "--disallowedTools"})

    def supports(self, policy: SeatPolicy) -> tuple[bool, str]:
        return True, ""  # claude -p can express all three access levels

    def render_argv(self, policy: SeatPolicy) -> list[str]:
        if policy.access == WRITE:
            # Edits auto-accepted; the test/lint bash tools allow-listed.
            return ["--permission-mode", "acceptEdits", "--allowedTools", *_TEST_BASH_TOOLS]
        if policy.access == READ:
            # Plan mode is strictly read-only: no edits, no command execution.
            return ["--permission-mode", "plan"]
        # READ_TEST: default permission mode (NOT acceptEdits) + only the test bash tools allow-listed,
        # so edits are never auto-approved (denied in non-interactive -p) but checks can run.
        return ["--allowedTools", *_TEST_BASH_TOOLS]

    def foreign_reason(self, args: list[str]) -> str | None:
        bad = [a for a in args if a in _OPENAI_ONLY_FLAGS]
        if bad:
            return f"passes OpenAI-adapter flags {bad} the claude CLI does not understand"
        return None


class OpenAIRepoAdapter(AdapterProfile):
    id = OPENAI_REPO
    switchable = True
    _zero = frozenset({"--write", "--run-checks"})

    def __init__(self, *, run_checks_supported: bool = True) -> None:
        # `read_test` is only expressible once the adapter binary has the decoupled --run-checks mode.
        self.run_checks_supported = run_checks_supported

    def supports(self, policy: SeatPolicy) -> tuple[bool, str]:
        if policy.access == READ_TEST and not self.run_checks_supported:
            return False, (
                "the OpenAI repo adapter cannot enforce 'read_test' (run checks without writes) "
                "until it supports --run-checks; do not grant it --write for an assessment seat"
            )
        return True, ""

    def render_argv(self, policy: SeatPolicy) -> list[str]:
        caps: list[str] = []
        if policy.access == WRITE:
            caps.append("--write")
        elif policy.access == READ_TEST:
            caps.append("--run-checks")
        # READ -> base repo adapter is already read-only; no capability flag.
        api = policy.tool_policy.get("api")
        if api:
            caps += ["--api", str(api)]
        return caps

    def foreign_reason(self, args: list[str]) -> str | None:
        bad = [a for a in args if a in _CLAUDE_ONLY_FLAGS]
        if bad:
            return f"passes Claude-only flags {bad} this OpenAI adapter does not understand"
        return None


class OpenAICompatibleAdapter(AdapterProfile):
    """The text-only, single-shot adapter — no repo access at all. It can back a `read` seat (it
    reads the handoff text), but can never write or run checks."""

    id = OPENAI_COMPAT
    switchable = True

    def supports(self, policy: SeatPolicy) -> tuple[bool, str]:
        if policy.access == READ:
            return True, ""
        return False, (
            f"the text-only OpenAI adapter has no repo access, so it cannot enforce "
            f"{policy.access!r} (it can only back a read-only reviewer)"
        )

    def render_argv(self, policy: SeatPolicy) -> list[str]:
        api = policy.tool_policy.get("api")
        return ["--api", str(api)] if api else []

    def foreign_reason(self, args: list[str]) -> str | None:
        bad = [a for a in args if a in _CLAUDE_ONLY_FLAGS]
        if bad:
            return f"passes Claude-only flags {bad} this OpenAI adapter does not understand"
        return None


class LegacyCommandAdapter(AdapterProfile):
    """An explicit-``cmd`` seat with no model: run verbatim, never model-switchable, never
    policy-managed. It stays runnable so unmigrated custom commands keep working."""

    id = LEGACY
    switchable = False


_CLAUDE_ONLY_FLAGS = frozenset({"--permission-mode", "--allowedTools", "--disallowedTools"})
_OPENAI_ONLY_FLAGS = frozenset(
    {
        "--write",
        "--run-checks",
        "--repo-root",
        "--base",
        "--key-env",
        "--api",
        "--run-timeout",
        "--max-steps",
        "--max-bytes",
    }
)


def _strip_flags(
    argv: list[str], zero: frozenset[str], one: frozenset[str], variadic: frozenset[str]
) -> list[str]:
    """Remove capability flags (and their arguments) from a launch template."""
    out: list[str] = []
    i = 0
    n = len(argv)
    while i < n:
        tok = argv[i]
        if tok in zero:
            i += 1
        elif tok in one:
            i += 2  # flag + its single argument
        elif tok in variadic:
            i += 1
            while i < n and not argv[i].startswith("--"):
                i += 1
        else:
            out.append(tok)
            i += 1
    return out


def adapter_for(template: list[str], *, run_checks_supported: bool = True) -> AdapterProfile:
    """Classify a model launch template into its adapter profile."""
    toks = [str(t) for t in template]
    if toks and toks[0] == "claude":
        return ClaudeAdapter()
    if any(t.replace("\\", "/").endswith("openai-repo-seat.py") for t in toks):
        return OpenAIRepoAdapter(run_checks_supported=run_checks_supported)
    if any(t.replace("\\", "/").endswith("openai-compatible-seat.py") for t in toks):
        return OpenAICompatibleAdapter()
    return LegacyCommandAdapter()


def _role_for(name: str, cfg: dict) -> str:
    role = (cfg.get("role") or "").strip()
    if role:
        return role
    return name if name in _ROLE_DEFAULT_ACCESS else "worker"


def compile_seat(name: str, cfg: dict, models: dict, *, run_checks_supported: bool = True) -> dict:
    """Compile one seat config into a runnable, capability-checked spec.

    Returns ``{cmd, adapter, switchable, policy, model, unset_env}``. Raises ``cc.CollabError`` on an
    absent model, an adapter that cannot enforce a managed policy, or a legacy ``model_args`` tail
    that carries a flag the resolved adapter would choke on (the 030 crash — refused up front).
    """
    # Explicit-cmd / human seat (no model): verbatim, non-switchable.
    if not (isinstance(cfg, dict) and cfg.get("model")):
        cmd = cfg.get("cmd")
        return {
            "cmd": list(cmd) if isinstance(cmd, list) else cmd,
            "adapter": LEGACY,
            "switchable": False,
            "policy": None,
            "model": None,
            "unset_env": list(cfg["unset_env"]) if cfg.get("unset_env") else None,
        }

    spec = models.get(cfg["model"])
    if not (isinstance(spec, dict) and isinstance(spec.get("cmd"), list) and spec["cmd"]):
        raise cc.CollabError(
            f"seat {name!r} references model {cfg['model']!r} absent from the 'models' catalog"
        )
    template = [str(a) for a in spec["cmd"]]
    adapter = adapter_for(template, run_checks_supported=run_checks_supported)
    # The model's env drops (e.g. dropping ANTHROPIC_API_KEY for the subscription) are inherited if
    # the seat doesn't set its own — same precedence as the old load_seats.
    unset_env = (
        list(spec["unset_env"])
        if spec.get("unset_env")
        else (list(cfg["unset_env"]) if cfg.get("unset_env") else None)
    )

    # Managed seat: declares role and/or access -> the adapter renders its own argv from the policy.
    if cfg.get("access") or cfg.get("role"):
        role = _role_for(name, cfg)
        access = cfg.get("access") or _ROLE_DEFAULT_ACCESS.get(role, READ)
        policy = SeatPolicy(role=role, access=access, tool_policy=cfg.get("tool_policy") or {})
        ok, reason = adapter.supports(policy)
        if not ok:
            raise cc.CollabError(f"seat {name!r}: {reason}")
        argv = adapter.base_argv(template) + adapter.render_argv(policy)
        return {
            "cmd": argv,
            "adapter": adapter.id,
            "switchable": adapter.switchable,
            "policy": {"role": role, "access": access, "tool_policy": policy.tool_policy},
            "model": cfg["model"],
            "unset_env": unset_env,
        }

    # Legacy model_args path: compose as before, but REFUSE a foreign flag (the 030 crash).
    margs = cfg.get("model_args") or []
    if not isinstance(margs, list):
        raise cc.CollabError(f"seat {name!r}: 'model_args' must be a list")
    margs = [str(a) for a in margs]
    reason = adapter.foreign_reason(margs)
    if reason:
        raise cc.CollabError(
            f"seat {name!r}: model {cfg['model']!r} uses the {adapter.id} adapter, but its "
            f"model_args {reason}. Migrate this seat to a managed 'access' policy "
            f"(builder=write, reviewer=read, breaker/verifier=read_test) so the adapter renders "
            f"its own flags — see SEATS.md."
        )
    return {
        "cmd": template + margs,
        "adapter": adapter.id,
        "switchable": adapter.switchable,
        "policy": None,
        "model": cfg["model"],
        "unset_env": unset_env,
    }


def seat_profile_fingerprint(compiled: dict) -> str:
    """A stable digest of a seat's capability profile (adapter + model + policy). Feeds the candidate
    identity (ADR-0003 D4) so a changed seat/model/policy mints a new candidate."""
    payload = {
        "adapter": compiled.get("adapter"),
        "model": compiled.get("model"),
        "policy": compiled.get("policy"),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "seat:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def audit_seat_change(collab, name: str, old: dict | None, new: dict | None, *, by: str, ts: str) -> Path:
    """Append a seat-change record to ``autopilot/seats/audit.jsonl`` (dashboard provenance)."""
    path = Path(collab) / "autopilot" / "seats" / "audit.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": ts, "seat": name, "by": by, "old": old, "new": new}
    line = json.dumps(rec, sort_keys=True) + "\n"
    with cc.collab_lock(path.parent / ".auditlock"):
        prior = path.read_text("utf-8") if path.exists() else ""
        cc.safe_write(path, prior + line)
    return path
