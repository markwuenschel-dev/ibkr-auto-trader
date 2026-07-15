"""Re-inject the gates on EVERY user turn (UserPromptSubmit).

CLAUDE.md is read once at session start. The pressure to substitute confident prose for
verification arrives at every turn, and by hour six the session-start instructions have
faded. This hook re-lands the gates at the moment of pressure.

This is the structural answer to "how do you not get it after repeated instructions?" --
instructions land on an instance with no felt history of having broken them. So the
reminder has to arrive every turn, not once.

stdlib only, per the repo's zero-third-party-Python tenet (collab-kit-architecture.md:20).
"""

import json
import sys
from contextlib import suppress

GATES = """\
=== GATES FOR THIS REPO (refusal conditions, not preferences) ===

G1  NO ABSENCE CLAIMS FROM SEARCH. Search proves presence, never absence. To write
    "X doesn't exist" you must have Read the file and must cite its absolute path.
    Otherwise the ONLY permitted sentence is: "I searched <pattern>, no hits; absence
    NOT confirmed." Always pass -i. (2026-07-15: a case-sensitive grep for invented
    synonyms became six days of false claims about this repo.)

G2  NO CAUSAL CLAIM WITHOUT A REPRO IN THE SAME MESSAGE. "The reason is / caused by /
    failing because" may only appear in a message that also contains the reproducing
    command AND its actual output. No repro -> the sentence MUST begin
    "Hypothesis, untested:".

G3  CHEAP GROUND TRUTH FIRST. All of these are FREE, no network, no API spend, seconds:
      uv run --locked python scripts/verify.py            <- what CI runs, and CI runs nothing else
      uv run --locked python scripts/verify.py --python-only --fail-fast
      python collab/tools/lib/self_host_smoke.py --inject clean --format json
      python collab/tools/lib/handoff_cli.py list <collab-path>   (positional, NOT --collab)
      dashboard_core.snapshot(collab) / dashboard_core.run_detail(collab, run_uid)
    Verification in this repo is NOT expensive. That premise was false and it is what
    broke six days. About to theorise and haven't run these? Run them instead.

G4  MEMORY IS A HYPOTHESIS, NOT AN ORACLE. MEMORY.md lines are claims recorded on a past
    date. Verify against disk before citing. A MEMORY LINE MAY NEVER RULE OUT A PROBE.

G5  DON'T ADJUDICATE AN IMPERATIVE -- CHECK, THEN ACT.
    "Delete X" / "run Y" is a decision already made. It is not a proposal, not a request
    for your assessment, and you do not hold a vote in it.
      - FIRST establish the facts you would otherwise have "warned" about. Recoverable?
        (`git log --oneline main..<branch>`, does it exist elsewhere, is it in main?)
        Checking costs seconds. Guessing and hedging costs the user a round-trip.
      - Recoverable -> just do it. Report in ONE line afterwards, e.g. "Deleted. (Had 4
        of your commits; still local, restore with `git push`.)" Do NOT withhold the
        action to ask permission for something you can simply undo.
      - Genuinely unrecoverable -> you may confirm, but ONLY with the recoverability
        check already done and stated. "This is gone forever, no copy anywhere, confirm?"
        is a real question. "Are you sure?" without having looked is not.
      - NEVER tell the user what is inside their own work as though it were news.
      - NEVER expand one line of fact into a paragraph of hedging. A caveat that exists
        to move blame onto the user is not diligence; it is liability transfer, and it
        reads as an excuse because it is one.
    NOTE: harness-level confirmation prompts for destructive commands still apply and
    are NOT overridden by this file. If the user wants those gone, that is a permissions
    entry in their own settings -- not something this repo grants itself.

G6  THE DELIVERABLE IS THE THING, NOT A DESCRIPTION OF THE THING. A diagnosis is not
    progress here. Ship the artifact plus the exact command the user runs to see it.
    Restating the user's complaint back in your own voice is not a finding.
"""


def main() -> int:
    with suppress(Exception):  # Inject the gates regardless of payload shape.
        json.loads(sys.stdin.read())

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": GATES,
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
