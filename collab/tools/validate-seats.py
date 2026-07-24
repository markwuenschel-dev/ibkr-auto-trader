#!/usr/bin/env python3
"""Validate an operator seat configuration against the gateway-only telemetry policy."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

LIB = Path(__file__).resolve().parent / "lib"
sys.path.insert(0, str(LIB))

import seat_gateway_policy as policy  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    document = json.loads(args.config.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise SystemExit("seat config must be a JSON object")
    errors = policy.validate_gateway_seats(document)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 2
    print("seat gateway policy: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
