"""contracts — the handoff as a typed, schema-validated artifact (ARCHITECTURE.md §7.2/§7.4).

A handoff is a Markdown file: a ``---``-delimited frontmatter block of simple ``key: value``
lines, then a body of ``## Section`` headings. §7.2 ("typed contracts") says the handoff must
stop being free text and become a *validated artifact*: an explicit shape whose required keys,
enumerated values, and required sections can be checked before it is acted on. §7.4 ("handoff
loss") makes the downstream cost of a handoff *measurable*: loss is the fraction of an
upstream's **explicit, machine-checkable constraints** that are absent from — or contradicted
by — the downstream artifact that claims to satisfy it.

This module is stdlib-only and deliberately specific to the shape above. The frontmatter parser
is a minimal ``key: value`` reader (with one special case for the bracketed ``guardrails`` list);
it is NOT a YAML implementation and must not be reused as one. Validation is hand-rolled against
``telemetry/contracts/handoff.schema.json`` — that JSON file is the documentation/source of
truth, but no jsonschema library enforces it (project constraint).

Constraints are **declared, typed fields** (§7.2): a ``## Constraints`` section of ``- [ID] text``
bullets, e.g. ``- [C1] paper-trading only``. Declaring them with stable IDs — rather than scraping
prose — is what makes ``handoff_loss`` measure retention *by identity*, so a downstream revision that
drops a requirement is detected instead of laundered into a fuzzy substring match. A lossy prose
scrape survives only as a legacy fallback for handoffs that declare none.

Public API:
    parse_handoff(path)                       -> dict
    validate_handoff(obj)                     -> list[str]
    declared_constraints(obj)                 -> dict[str, str]   # {id: text}, the typed path
    extract_constraints(obj)                  -> set[str]         # declared IDs, else scraped prose
    handoff_loss(upstream_obj, downstream_obj)-> dict
"""

from __future__ import annotations

import re
from pathlib import Path

# --------------------------------------------------------------------------- #
# Schema rules (kept in lockstep with telemetry/contracts/handoff.schema.json)
# --------------------------------------------------------------------------- #

REQUIRED_FRONTMATTER = ("to", "from", "id", "title", "priority", "date", "status")
REQUIRED_SECTIONS = ("Summary",)
PRIORITY_VALUES = ("high", "normal", "low")
STATUS_VALUES = ("pending", "claimed", "done")

#: The typed-constraint section and its ``- [ID] text`` bullet form (the preferred path).
_CONSTRAINTS_SECTION = "Constraints"
_DECLARED_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s*\[(?P<id>[^\]]+)\]\s*(?P<text>.*\S)\s*$")

#: Body sections whose bullet lines are scraped as constraints when none are declared (§7.4 legacy).
_CONSTRAINT_BULLET_SECTIONS = ("Request", "Risks & Questions")

_FENCE = "---"
_HEADING_RE = re.compile(r"^##\s+(.*?)\s*$")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.*\S)\s*$")
#: An imperative constraint line: contains a standalone must/required/shall token.
_IMPERATIVE_RE = re.compile(r"\b(?:must|required|require|shall)\b", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def _parse_frontmatter(block: str) -> dict:
    """Parse the ``---``-fenced frontmatter block into a dict.

    Minimal and specific to the handoff shape: ``key: value`` per line, with the single
    special case that ``guardrails`` may be a bracketed list ``[a, b, c]`` which becomes a
    ``list[str]``. Blank lines are ignored; a line without a colon is skipped (defensive).
    Not a YAML parser — do not extend it into one.
    """
    fm: dict = {}
    for line in block.splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key == "guardrails":
            fm[key] = _parse_bracket_list(value)
        else:
            fm[key] = value
    return fm


def _parse_bracket_list(value: str) -> list[str]:
    """Parse ``[a, b, c]`` (or a bare ``a, b, c``) into a list of trimmed strings."""
    inner = value.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    return [item.strip() for item in inner.split(",") if item.strip()]


def _split_sections(body: str) -> dict:
    """Split a Markdown body into ``{heading_text: section_text}`` on ``## `` headings.

    Text before the first ``##`` heading (if any) is ignored — the handoff shape puts all
    meaningful content under headings. Section text excludes the heading line itself.
    """
    sections: dict = {}
    current: str | None = None
    buf: list[str] = []
    for line in body.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = m.group(1).strip()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return sections


def parse_handoff(path) -> dict:
    """Parse a handoff Markdown file into a structured object (§7.2).

    Returns:
        ``{"frontmatter": {...}, "sections": {name: text}, "raw": <full file text>}``.

    The frontmatter is the content between the first two lines that are exactly ``---``; if no
    such fence pair exists, ``frontmatter`` is ``{}`` and the whole file is treated as body.
    ``raw`` is retained verbatim because §7.4 handoff-loss retention is a substring test against
    the downstream's raw text.
    """
    raw = Path(path).read_text(encoding="utf-8")
    lines = raw.splitlines()

    frontmatter: dict = {}
    body = raw
    if lines and lines[0].strip() == _FENCE:
        for i in range(1, len(lines)):
            if lines[i].strip() == _FENCE:
                frontmatter = _parse_frontmatter("\n".join(lines[1:i]))
                body = "\n".join(lines[i + 1:])
                break

    return {
        "frontmatter": frontmatter,
        "sections": _split_sections(body),
        "raw": raw,
    }


# --------------------------------------------------------------------------- #
# Validation (hand-rolled against handoff.schema.json — §7.2)
# --------------------------------------------------------------------------- #


def validate_handoff(obj: dict) -> list[str]:
    """Validate a parsed handoff against the schema's rules; ``[]`` means valid (§7.2).

    Checks, in order: every required frontmatter key is present and non-empty; ``priority`` and
    ``status`` are within their enumerations; every required body section is present. Each
    problem yields one human-readable error string naming the offending field/section, so a
    malformed handoff surfaces *all* of its faults at once rather than one at a time.
    """
    errors: list[str] = []
    fm = obj.get("frontmatter", {}) or {}
    sections = obj.get("sections", {}) or {}

    for key in REQUIRED_FRONTMATTER:
        value = fm.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            errors.append(f"missing required frontmatter key: {key!r}")

    priority = fm.get("priority")
    if priority is not None and priority not in PRIORITY_VALUES:
        errors.append(
            f"invalid priority {priority!r}: must be one of {list(PRIORITY_VALUES)}"
        )

    status = fm.get("status")
    if status is not None and status not in STATUS_VALUES:
        errors.append(
            f"invalid status {status!r}: must be one of {list(STATUS_VALUES)}"
        )

    for name in REQUIRED_SECTIONS:
        if name not in sections or not sections.get(name, "").strip():
            errors.append(f"missing required body section: {name!r}")

    return errors


# --------------------------------------------------------------------------- #
# Explicit constraints & handoff loss (§7.4)
# --------------------------------------------------------------------------- #


def _normalize(text: str) -> str:
    """Normalize a constraint to a lowercased, whitespace-collapsed, trimmed string."""
    return re.sub(r"\s+", " ", text).strip().lower()


def declared_constraints(obj: dict) -> dict[str, str]:
    """Return the handoff's **declared, typed constraints** as ``{id: text}`` (§7.2/§7.4).

    Parsed from a ``## Constraints`` section of ``- [ID] text`` bullets (e.g.
    ``- [C1] paper-trading only``). Declaring constraints with stable IDs is what lets
    ``handoff_loss`` measure retention *by identity* — a downstream that drops ``[C2]`` is
    detectable, not laundered into a fuzzy substring hit. Returns ``{}`` when none are declared.
    """
    sections = obj.get("sections", {}) or {}
    text = sections.get(_CONSTRAINTS_SECTION)
    out: dict[str, str] = {}
    if not text:
        return out
    for line in text.splitlines():
        m = _DECLARED_RE.match(line)
        if m:
            out[m.group("id").strip()] = m.group("text").strip()
    return out


def _scrape_constraints(obj: dict) -> set[str]:
    """Best-effort, LOSSY constraint harvest from prose — the legacy fallback (§7.4).

    Used only when a handoff declares no typed ``## Constraints`` section. Harvests guardrails,
    ``Request``/``Risks & Questions`` bullets, and imperative ``must``/``required``/``shall`` lines
    (normalized). This is exactly the naive path §7.4 warns about: it over-collects prose fragments
    and cannot match by identity, so prefer declared constraints wherever measurement matters.
    """
    constraints: set[str] = set()

    fm = obj.get("frontmatter", {}) or {}
    for guard in fm.get("guardrails", []) or []:
        norm = _normalize(guard)
        if norm:
            constraints.add(norm)

    sections = obj.get("sections", {}) or {}
    for name in _CONSTRAINT_BULLET_SECTIONS:
        text = sections.get(name)
        if not text:
            continue
        for line in text.splitlines():
            m = _BULLET_RE.match(line)
            if m:
                norm = _normalize(m.group(1))
                if norm:
                    constraints.add(norm)

    # Imperative lines anywhere in the body sections.
    for text in sections.values():
        for line in (text or "").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if _IMPERATIVE_RE.search(stripped):
                bullet = _BULLET_RE.match(line)
                payload = bullet.group(1) if bullet else stripped.lstrip("#").strip()
                norm = _normalize(payload)
                if norm:
                    constraints.add(norm)

    return constraints


def extract_constraints(obj: dict) -> set[str]:
    """Return a handoff's explicit constraints as a set (§7.4).

    Prefers **declared** typed constraints (returns their stable IDs); falls back to the lossy
    prose scrape only when the handoff declares none. The declared path is deterministic and
    identity-addressable; the scrape path is best-effort (see ``_scrape_constraints``).
    """
    declared = declared_constraints(obj)
    if declared:
        return set(declared)
    return _scrape_constraints(obj)


def handoff_loss(upstream_obj: dict, downstream_obj: dict) -> dict:
    """Measure handoff loss across one upstream->downstream edge (ARCHITECTURE.md §7.4).

    Loss = the fraction of the upstream's explicit constraints absent from the downstream. Two
    modes, chosen by whether the upstream declares typed constraints:

      * ``"declared"`` — upstream has a ``## Constraints`` section (``- [ID] text``). A constraint
        is retained iff the downstream **also declares that ID** or references ``[ID]`` verbatim.
        Identity-addressed — this is the trustworthy path, and the reason to declare constraints.
      * ``"scraped"`` — upstream declares none, so constraints are harvested from prose and
        retention is a permissive normalized-substring test. Lossy; flags dropped constraints only.

    Returns ``{"mode", "upstream": n, "retained": r, "dropped": [...], "loss_ratio": (n-r)/n}``;
    ``dropped`` is sorted; ``n == 0`` yields ``0.0``.
    """
    declared = declared_constraints(upstream_obj)
    if declared:
        mode = "declared"
        keys = set(declared)
        down_ids = set(declared_constraints(downstream_obj))
        down_raw = downstream_obj.get("raw", "")
        dropped = sorted(
            cid for cid in keys if cid not in down_ids and f"[{cid}]" not in down_raw
        )
    else:
        mode = "scraped"
        keys = _scrape_constraints(upstream_obj)
        down_raw_norm = _normalize(downstream_obj.get("raw", ""))
        dropped = sorted(c for c in keys if c not in down_raw_norm)

    n = len(keys)
    retained = n - len(dropped)
    return {
        "mode": mode,
        "upstream": n,
        "retained": retained,
        "dropped": dropped,
        "loss_ratio": (n - retained) / n if n else 0.0,
    }
