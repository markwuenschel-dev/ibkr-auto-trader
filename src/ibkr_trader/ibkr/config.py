"""config — the frozen IBKR connection settings for the PT-3 session (decision ④).

This is deliberately **separate** from ``ibkr_trader.config`` (the mode/risk control plane): this file
holds only *where and how we dial the Gateway*, not *what we are allowed to do once connected*. Values are
env-sourced with paper-first defaults (``127.0.0.1:7497`` = TWS/Gateway paper socket).

One value is intentionally **not** env-sourced: ``readonly``. The read-only data session is a
defense-in-depth order-block (decision ③) that stays read-only permanently — execution opens its own
writable session later (PT-10) under a different clientId. Leaving ``readonly`` out of the env surface
means ops cannot flip the structural order-block with a stray environment variable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# env var names (host/port/client_id/account are env-sourced; timeout/readonly are not — see module docs).
ENV_HOST = "IBKR_HOST"
ENV_PORT = "IBKR_PORT"
ENV_CLIENT_ID = "IBKR_CLIENT_ID"
ENV_ACCOUNT = "IBKR_ACCOUNT"

# Paper-first defaults. 7497 is the TWS paper socket (7496 = live TWS, 4002/4001 = Gateway paper/live).
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PAPER_PORT = 7497
DEFAULT_CLIENT_ID = 1  # the data session's clientId; execution (PT-10) takes a distinct one.
DEFAULT_CONNECT_TIMEOUT = 10.0


def _env_str(name: str) -> str | None:
    """Return a non-empty env value, else ``None`` (an empty/unset var is 'not configured')."""
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


@dataclass(frozen=True)
class IbkrConnectionConfig:
    """Immutable connection settings for one IBKR data session.

    ``account`` unset (``None``) means "resolve at connect": use the sole account if there is exactly one,
    else fail closed (decision ④). ``readonly`` is ``True`` and stays that way for the data session.
    """

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PAPER_PORT
    client_id: int = DEFAULT_CLIENT_ID
    account: str | None = None
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    readonly: bool = True

    @classmethod
    def from_env(
        cls,
        *,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        readonly: bool = True,
    ) -> IbkrConnectionConfig:
        """Build from ``IBKR_HOST``/``IBKR_PORT``/``IBKR_CLIENT_ID``/``IBKR_ACCOUNT``, paper defaults
        filling any gaps. ``connect_timeout``/``readonly`` are caller-supplied, not env-sourced."""
        port_raw = _env_str(ENV_PORT)
        client_raw = _env_str(ENV_CLIENT_ID)
        return cls(
            host=_env_str(ENV_HOST) or DEFAULT_HOST,
            port=int(port_raw) if port_raw is not None else DEFAULT_PAPER_PORT,
            client_id=int(client_raw) if client_raw is not None else DEFAULT_CLIENT_ID,
            account=_env_str(ENV_ACCOUNT),
            connect_timeout=connect_timeout,
            readonly=readonly,
        )
