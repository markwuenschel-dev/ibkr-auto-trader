"""app — the process entrypoint + (eventually) the bounded main loop + heartbeat/watchdog (PT-13).

At PT-0 this only proves the control plane wires up: resolve settings (PAPER by default), open a §8
telemetry run, emit a bootstrap event, and exit. The rebalance→risk→execution loop lands at PT-13.
"""

from __future__ import annotations

import sys

from . import __version__
from .config import Settings, submission_allowed
from .pack import TRADING_PACK
from .telemetry import Emitter


def bootstrap(settings: Settings | None = None, emitter: Emitter | None = None) -> dict:
    """Wire up the control plane and emit one §8 bootstrap event. Returns the event (for tests/inspection)."""
    settings = settings or Settings()
    emitter = emitter or Emitter()
    mode = settings.effective_mode()
    return emitter.emit(
        stage="app.bootstrap",
        agent_role="orchestrator",
        task_id="PT-0",
        decision={
            "action": "accept",
            "reason_codes": [f"mode:{mode}", f"pack:{TRADING_PACK.name}"],
            "confidence": None,
        },
        metrics={"version": __version__},
        gates=[
            {
                "name": "paper-default",
                "status": "pass" if settings.paper_only else "advisory",
                "ruleset_hash": None,
                "severity": "blocking",
            }
        ],
        risk=None,  # fail-closed bootstrap: the calibrated risk layer (§6/§11) is not built yet
    )


def main(argv: list[str] | None = None) -> int:
    settings = Settings()
    mode = settings.effective_mode()
    event = bootstrap(settings)
    print(
        f"ibkr_trader {__version__} — mode={mode} "
        f"submission_allowed={submission_allowed(mode)} pack={TRADING_PACK.name} "
        f"(oracle={TRADING_PACK.oracle}); telemetry run {event['run_id']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
