"""ibkr_trader — a paper-first automated trading system for a small IBKR taxable account.

This is the first *coding domain-pack* (Reusable Core §12) built on the collab-kit substrate. The
pipeline seams — domain / ibkr / strategy / risk / execution / state / audit — are the packages below;
they fill in over slices PT-1…PT-15 (see docs/design/paper-trading-roadmap.md). PT-0 ships only the
skeleton, the §8 telemetry envelope, the PAPER-default control plane, and the §12 pack declaration.

Safety spine (holds from the first line of real logic): PAPER is the default mode; live is rejected
unless enabled by reviewed config; strategy APIs emit `StrategyIntent`; trusted execution code must
enforce the no-direct-strategy-orders release gate; every decision is audited. Mint guards are
provenance / accidental-bypass controls, not an in-process security boundary.
"""

__version__ = "0.0.0"
