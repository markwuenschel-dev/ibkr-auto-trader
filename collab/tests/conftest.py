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
import sys
from pathlib import Path

import pytest

_KIT = Path(__file__).resolve().parent.parent
_LIB = _KIT / "tools" / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

_TEXT_ADAPTER = "C:/repo/collab/tools/adapters/openai-compatible-seat.py"


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


@pytest.fixture
def v2_seats():
    """Return the ``write_v2_seats(home, ...)`` helper (call it with whatever ``home`` the test drives)."""
    return write_v2_seats
