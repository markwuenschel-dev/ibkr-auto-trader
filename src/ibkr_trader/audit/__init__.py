"""audit — the decision audit log (PT-12): structured JSON + human-readable line for EVERY decision.

Every approve and every reject is recorded (audit-completeness invariant). Distinct from §8 telemetry:
telemetry is the system's own trace record; the audit log is the trading decision record a human reviews.
Empty until PT-12.
"""
