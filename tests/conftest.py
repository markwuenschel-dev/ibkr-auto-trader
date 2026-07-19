"""Root pytest hermetic guards (IBKR trading package suite).

Blank LiteLLM / provider credentials so a developer collab/.env or shell env
cannot leak into unit tests that might spawn tools or import adapters.
"""

from __future__ import annotations

import os

import pytest

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
):
    os.environ[_cost_env] = ""
os.environ["LLG_HERMETIC"] = "1"


@pytest.fixture(autouse=True)
def _hermetic_no_live_llm(monkeypatch: pytest.MonkeyPatch) -> None:
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
    ):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("LLG_HERMETIC", "1")
