"""Tests for contracts.py — typed handoff contract + handoff loss (ARCHITECTURE.md §7.2/§7.4).

Run:  python -m pytest tests/test_contracts.py -v   (from the collab-kit repo root)
stdlib only: pytest + tempfile.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Make the core importable whether run from repo root or elsewhere.
_LIB = Path(__file__).resolve().parent.parent / "tools" / "lib"
sys.path.insert(0, str(_LIB))

import contracts  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

WELL_FORMED = """\
---
to: builder
from: orchestrator
id: 042
title: Make the handoff a typed artifact
priority: high
date: 2026-07-03
status: pending
revision: rev 1
guardrails: [concurrency, path-safety]
---

## Summary
Turn the handoff into a schema-validated artifact.

## Request
- The parser MUST be stdlib only.
- Guardrails must be carried downstream.

## Risks & Questions
- Should we support nested lists?
"""

MALFORMED = """\
---
to: builder
from: orchestrator
title: Broken handoff missing id and with bad priority
priority: urgent
date: 2026-07-03
status: pending
---

## Details
No summary section here on purpose.
"""


def _write_tmp(text: str) -> str:
    fd = tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, encoding="utf-8"
    )
    fd.write(text)
    fd.close()
    return fd.name


# --------------------------------------------------------------------------- #
# (a) well-formed handoff parses and validates clean
# --------------------------------------------------------------------------- #


def test_well_formed_handoff_validates_clean():
    path = _write_tmp(WELL_FORMED)
    obj = contracts.parse_handoff(path)

    assert obj["frontmatter"]["id"] == "042"
    assert obj["frontmatter"]["priority"] == "high"
    assert obj["frontmatter"]["status"] == "pending"
    assert obj["frontmatter"]["guardrails"] == ["concurrency", "path-safety"]
    assert "Summary" in obj["sections"]
    assert obj["raw"] == WELL_FORMED

    errors = contracts.validate_handoff(obj)
    assert errors == [], f"expected no errors, got: {errors}"


# --------------------------------------------------------------------------- #
# (b) malformed handoff surfaces each problem
# --------------------------------------------------------------------------- #


def test_malformed_handoff_reports_each_problem():
    path = _write_tmp(MALFORMED)
    obj = contracts.parse_handoff(path)
    errors = contracts.validate_handoff(obj)

    blob = " | ".join(errors).lower()
    assert errors, "malformed handoff should produce errors"
    # missing required 'id'
    assert "id" in blob
    # bad priority enum ('urgent' is not allowed)
    assert "priority" in blob and "urgent" in blob
    # missing required Summary section
    assert "summary" in blob


# --------------------------------------------------------------------------- #
# (c) handoff_loss: downstream drops one of three upstream constraints
# --------------------------------------------------------------------------- #


def test_handoff_loss_drops_one_of_three():
    upstream = {
        "frontmatter": {"guardrails": ["concurrency", "path-safety", "atomicity"]},
        "sections": {},
        "raw": "",
    }
    # Downstream mentions concurrency and path-safety, but never 'atomicity'.
    downstream = {
        "frontmatter": {},
        "sections": {},
        "raw": (
            "## Summary\nWe honored concurrency guarantees and kept path-safety "
            "checks in place throughout.\n"
        ),
    }

    result = contracts.handoff_loss(upstream, downstream)
    assert result["upstream"] == 3
    assert result["retained"] == 2
    assert result["dropped"] == ["atomicity"]
    assert abs(result["loss_ratio"] - (1 / 3)) < 1e-9


# --------------------------------------------------------------------------- #
# (d) handoff_loss: lossless pair
# --------------------------------------------------------------------------- #


def test_handoff_loss_lossless_pair():
    upstream = {
        "frontmatter": {"guardrails": ["concurrency", "path-safety", "atomicity"]},
        "sections": {},
        "raw": "",
    }
    downstream = {
        "frontmatter": {},
        "sections": {},
        "raw": (
            "We preserved concurrency, maintained path-safety, and guaranteed "
            "atomicity end to end."
        ),
    }

    result = contracts.handoff_loss(upstream, downstream)
    assert result["upstream"] == 3
    assert result["retained"] == 3
    assert result["dropped"] == []
    assert result["loss_ratio"] == 0.0


# --------------------------------------------------------------------------- #
# extract_constraints harvests all three sources
# --------------------------------------------------------------------------- #


def test_extract_constraints_covers_all_sources():
    path = _write_tmp(WELL_FORMED)
    obj = contracts.parse_handoff(path)
    constraints = contracts.extract_constraints(obj)

    # guardrails
    assert "concurrency" in constraints
    assert "path-safety" in constraints
    # bullet under Request + imperative
    assert "the parser must be stdlib only." in constraints
    assert "guardrails must be carried downstream." in constraints


# --------------------------------------------------------------------------- #
# Declared, typed constraints (§7.2) — the identity-addressed path
# --------------------------------------------------------------------------- #

_DECLARED_UP = """\
---
to: builder
from: reviewer
id: 100
title: Upstream with declared constraints
priority: high
date: 2026-07-04
status: pending
---

## Summary
Upstream that declares its constraints as typed fields.

## Constraints
- [C1] paper-trading only until reviewed
- [C2] max 1% risk per trade
- [C3] every order passes Risk & Sizing
"""


def _declared_down(dropped_ids):
    kept = [
        f"- [{cid}] {txt}"
        for cid, txt in (("C1", "paper-trading only"), ("C2", "1% risk"), ("C3", "risk gate"))
        if cid not in dropped_ids
    ]
    return {
        "frontmatter": {},
        "sections": {"Constraints": "\n".join(kept)},
        "raw": "## Constraints\n" + "\n".join(kept) + "\n",
    }


def test_declared_constraints_parsed_by_id():
    obj = contracts.parse_handoff(_write_tmp(_DECLARED_UP))
    dc = contracts.declared_constraints(obj)
    assert set(dc) == {"C1", "C2", "C3"}
    assert dc["C2"] == "max 1% risk per trade"
    # extract_constraints prefers declared IDs when present
    assert contracts.extract_constraints(obj) == {"C1", "C2", "C3"}


def test_handoff_loss_declared_by_id_drops_one():
    up = contracts.parse_handoff(_write_tmp(_DECLARED_UP))
    down = _declared_down(dropped_ids={"C2"})  # downstream drops constraint C2
    res = contracts.handoff_loss(up, down)
    assert res["mode"] == "declared"
    assert res["upstream"] == 3
    assert res["retained"] == 2
    assert res["dropped"] == ["C2"]
    assert abs(res["loss_ratio"] - (1 / 3)) < 1e-9


def test_handoff_loss_declared_retained_via_raw_reference():
    # A downstream that references [C2] in prose (without re-declaring it) still retains it.
    up = contracts.parse_handoff(_write_tmp(_DECLARED_UP))
    down = {"frontmatter": {}, "sections": {}, "raw": "We carried [C1], [C2] and [C3] forward."}
    res = contracts.handoff_loss(up, down)
    assert res["mode"] == "declared"
    assert res["dropped"] == []
    assert res["loss_ratio"] == 0.0
