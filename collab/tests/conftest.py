"""Shared test fixtures for the collab-kit suite.

The v2 assurance catalog lives here because autonomous closeout now REQUIRES one (ADR-0005): every
driver test reaching ``autopilot.run`` must have a valid ``seats.json`` on disk, or
``_resolve_assurance_plan`` fails closed with ``infrastructure_blocked`` before any builder dispatch.

The catalog is derived from the CHECKED-IN ``seats.example.json`` rather than hand-rolled, so these
tests exercise the exact artifact operators migrate from — a drift between the example and the
resolver fails the suite instead of surprising an operator mid-migration.

Deliberately an explicit helper, not an autouse fixture. A fixture that silently satisfied the
assurance gate everywhere is precisely the failure mode the gate exists to prevent: the machinery sat
inert because "no catalog" was indistinguishable from "configured". Tests asserting the fail-closed
path simply do not call it.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

# HARD COST KILL — collab/.env often has LITELLM_VIRTUAL_KEY=sk-… for seats.
# Seat adapters load that file via _load_dotenv(); blank every live credential
# here so pytest never bills the gateway / providers.
for _cost_env in (
    "LITELLM_VIRTUAL_KEY",
    "LITELLM_BASE_URL",
    "LITELLM_MODEL",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "XAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "SEAT_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
):
    os.environ[_cost_env] = ""
os.environ["LLG_HERMETIC"] = "1"
os.environ["COLLAB_HERMETIC"] = "1"

_KIT = Path(__file__).resolve().parent.parent
_LIB = _KIT / "tools" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

_TEXT_ADAPTER = "C:/repo/collab/tools/adapters/openai-compatible-seat.py"


@pytest.fixture(autouse=True)
def _hermetic_no_live_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-blank credentials every test; adapters also refuse under LLG_HERMETIC."""
    for key in (
        "LITELLM_VIRTUAL_KEY",
        "LITELLM_BASE_URL",
        "LITELLM_MODEL",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "XAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "SEAT_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
    ):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("LLG_HERMETIC", "1")
    monkeypatch.setenv("COLLAB_HERMETIC", "1")


def v2_seats_document(*, closeout=None) -> dict:
    """The checked-in v2 example catalog (four repo-capable roles + both assessment profiles)."""
    cfg = json.loads((_KIT / "seats.example.json").read_text(encoding="utf-8"))
    if closeout is not None:
        cfg["closeout"] = closeout
    return cfg


def text_only_verifier_document(*, closeout=None) -> dict:
    """The v2 catalog with a TEXT-ONLY verifier — invalid for an assessment role (ADR-0004 D1/D2).

    The baseline profile binds ``{"seat": "verifier"}`` by reference, so repointing the verifier seat
    at a text-only adapter is enough to make the profile invalid.
    """
    cfg = v2_seats_document(closeout=closeout)
    cfg["models"]["gemini-3.5-flash"] = {
        "provider": "google",
        "cmd": [
            "python",
            _TEXT_ADAPTER,
            "--base",
            "https://generativelanguage.googleapis.com/v1beta/openai",
            "--model",
            "gemini-3.5-flash",
            "--key-env",
            "GEMINI_API_KEY",
            "--api",
            "auto",
        ],
    }
    cfg["seats"]["verifier"]["model"] = "gemini-3.5-flash"
    return cfg


def write_v2_seats(home, document: dict | None = None, *, closeout=None) -> Path:
    """Write a valid v2 assurance catalog to ``<home>/seats.json`` and return the path."""
    path = Path(home)
    path.mkdir(parents=True, exist_ok=True)
    seats = path / "seats.json"
    seats.write_text(json.dumps(document or v2_seats_document(closeout=closeout), indent=2), encoding="utf-8")
    return seats


def is_conformance_prompt(prompt: str) -> bool:
    """True when this dispatch is the spec-conformance pair rather than a defect lane.

    The conformance pair reuses the BASELINE profile, so its cmd is identical to the baseline lane's;
    only the prompt differs. A fake runner therefore has to route on the prompt, exactly as a real
    model would have to read it.
    """
    return "CONFORMANCE ASSESSOR" in (prompt or "") or "CONFORMANCE VERIFIER" in (prompt or "")


def conformance_reply(prompt: str, *, status="met", source="src/m.py:1", test=None) -> str:
    """A well-formed conformance report answering whatever contract the prompt carries.

    Parses the digest and requirement ids back out of the prompt so the reply is genuinely bound to
    the contract under test — a hard-coded digest would drift the moment a fixture's constraints
    change, and would pass for the wrong reason.
    """
    digest = re.search(r'"contract_digest":\s*"([^"]+)"', prompt or "")
    # Deduped, order-preserving: the handoff's own '## Constraints' section is appended after the
    # requirement list, so each id appears TWICE in the prompt. Emitting both would be a duplicate
    # record — which the parser rightly refuses.
    ids = list(dict.fromkeys(re.findall(r"^- \[([^\]]+)\]", prompt or "", re.MULTILINE)))
    return json.dumps(
        {
            "contract_digest": digest.group(1) if digest else "conformance:unknown",
            "requirements": [
                {"id": rid, "status": status, "source": source, "test": test, "evidence": "read it"}
                for rid in ids
            ],
        }
    )


@pytest.fixture
def v2_seats():
    """Return the ``write_v2_seats(home, ...)`` helper (call it with whatever ``home`` the test drives)."""
    return write_v2_seats
