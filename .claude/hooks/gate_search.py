"""G1 enforcement: a search that finds nothing must never become a claim that nothing exists.

Fires on PostToolUse for Grep/Glob. Injects context ONLY when the search returned zero
hits -- that is the exact moment the model is about to write "X doesn't exist".

Why this exists: on 2026-07-15 a case-sensitive grep for four invented synonyms
("domain-agnostic", "reusable", "generic", "portable") returned nothing, and that
failed guess was reported to the user as a fact about their repo for six days. The
docs actually say "Agent-agnostic" (collab-kit-architecture.md:19) and
"Reusable_Core_Domain-Agnostic_Agent_Loop" (paper-trading-roadmap.md:96). Every miss
was reachable with `grep -i`.

stdlib only, per the repo's zero-third-party-Python tenet (collab-kit-architecture.md:20).
"""

import json
import sys


def _zero_hits(tool_name: str, resp: object) -> bool:
    """True only when we can positively confirm the search found nothing.

    Unknown shapes return False: we stay silent rather than fire a false alarm.
    """
    if not isinstance(resp, dict):
        return False

    if tool_name == "Glob":
        for key in ("filenames", "files", "matches"):
            val = resp.get(key)
            if isinstance(val, list):
                return len(val) == 0
        if isinstance(resp.get("numFiles"), int):
            return resp["numFiles"] == 0
        return False

    mode = resp.get("mode")
    if mode == "files_with_matches":
        if isinstance(resp.get("numFiles"), int):
            return resp["numFiles"] == 0
        val = resp.get("filenames")
        return isinstance(val, list) and len(val) == 0
    if mode == "content":
        if isinstance(resp.get("numLines"), int):
            return resp["numLines"] == 0
        content = resp.get("content")
        return isinstance(content, str) and content.strip() == ""
    if mode == "count":
        content = resp.get("content")
        if isinstance(content, str):
            return content.strip() == ""
        return False

    # Fall back to generic shapes.
    if isinstance(resp.get("numFiles"), int):
        return resp["numFiles"] == 0
    return False


def main() -> int:
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        return 0

    tool_name = data.get("tool_name", "")
    if tool_name not in ("Grep", "Glob"):
        return 0

    tool_input = data.get("tool_input") or {}
    if not _zero_hits(tool_name, data.get("tool_response")):
        return 0

    pattern = tool_input.get("pattern", "<unknown>")
    case_sensitive = tool_name == "Grep" and not tool_input.get("-i")

    lines = [
        "*** GATE G1 -- ZERO HITS. THIS IS NOT EVIDENCE OF ABSENCE. ***",
        "",
        f"The search for `{pattern}` returned nothing. That result is compatible with:",
        "  (a) the thing is not there, OR",
        "  (b) you guessed the wrong vocabulary, OR",
        "  (c) the case is different, OR",
        "  (d) you searched the wrong path/glob.",
        "You cannot tell these apart from this result. Nothing here licenses an absence claim.",
        "",
        "YOU MAY NOT WRITE: \"X doesn't exist\", \"there's no Y\", \"nothing references Z\",",
        "\"I checked/grepped all of them\", or \"it isn't written down anywhere\".",
        "",
        "PERMITTED SENTENCE (the only one):",
        f"  \"I searched for `{pattern}` and got no hits; I have NOT confirmed absence.\"",
        "",
        "TO ACTUALLY CLAIM ABSENCE you must Read the specific file(s) and cite the",
        "absolute path you read. Reading beats guessing synonyms.",
    ]

    if case_sensitive:
        lines += [
            "",
            "!! THIS SEARCH WAS CASE-SENSITIVE (no -i). This is the exact defect that cost",
            "   the user six days on 2026-07-15: a case-sensitive grep for invented synonyms",
            "   'missed' docs that plainly say 'Agent-agnostic' and",
            "   'Reusable_Core_Domain-Agnostic_Agent_Loop'. Re-run with -i, and with the",
            "   vocabulary the repo actually uses, before concluding anything.",
        ]

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": "\n".join(lines),
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
