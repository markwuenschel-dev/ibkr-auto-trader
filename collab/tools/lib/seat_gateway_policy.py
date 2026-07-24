"""Validate that in-scope model seats use the canonical LiteLLM gateway adapters."""

from __future__ import annotations

from typing import Any

_APPROVED_ADAPTERS = {"openai-compatible-seat.py", "openai-repo-seat.py"}


def _uses_gateway_adapter(command: Any) -> bool:
    if not isinstance(command, list):
        return False
    return any(
        str(part).replace("\\", "/").rsplit("/", 1)[-1] in _APPROVED_ADAPTERS
        for part in command
    )


def validate_gateway_seats(document: dict[str, Any]) -> list[str]:
    """Return deterministic policy errors for CLI seats that bypass LiteLLM."""
    raw_models = document.get("models")
    raw_seats = document.get("seats")
    models: dict[str, Any] = raw_models if isinstance(raw_models, dict) else {}
    seats: dict[str, Any] = raw_seats if isinstance(raw_seats, dict) else {}
    errors: list[str] = []
    for name, raw_seat in sorted(seats.items()):
        if not isinstance(raw_seat, dict) or raw_seat.get("backend") != "cli":
            continue
        model = raw_seat.get("model")
        if not isinstance(model, str) or not model:
            errors.append(f"seat {name!r} has no model alias")
            continue
        raw_model = models.get(model)
        if not isinstance(raw_model, dict):
            errors.append(f"seat {name!r} references undefined model {model!r}")
            continue
        raw_command = raw_model.get("cmd")
        command: list[Any] = raw_command if isinstance(raw_command, list) else []
        if not _uses_gateway_adapter(command):
            errors.append(
                f"seat {name!r} model {model!r} bypasses the approved LiteLLM gateway adapters"
            )
            continue
        try:
            model_index = command.index("--model") + 1
            requested = command[model_index]
        except (ValueError, IndexError):
            errors.append(f"seat {name!r} model {model!r} gateway command has no --model alias")
            continue
        if requested != model:
            errors.append(
                f"seat {name!r} selects model {model!r} but its gateway command requests {requested!r}"
            )
    return errors
