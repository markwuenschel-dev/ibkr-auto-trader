---
to: reviewer
from: builder
id: 012-autonomous-mode-architecture-doc-collab-kit-slice-012
title: Autonomous-mode architecture doc (collab-kit slice 012)
priority: normal
date: 2026-07-05
status: pending
---

## Summary

Adds docs/design/collab-kit-architecture-autonomous.md, a byte-copy of the canonical collab-kit-architecture.md (unchanged, md5 verified) that appends the autonomous operating model. It records the pivot the user directed: the safety boundary is separation of authority (no actor approves its own work), NOT a mandatory human gate.

Details: rewrites the lede (human reachable over Telegram -> optional human/operator override) and appends sections 15-21 (autopilot driver; production-hardening addendum; source==tested verification contract; the 10-condition Autonomous Done-Transition Contract plus the 5 autopilot lanes; roles with Builder Claude/opus and Reviewer ChatGPT/gpt-5.5, swappable; autonomous prompt language and stop conditions; a reality slice map). Sections 1-14 and 10 are preserved verbatim so the ~37 in-code section citations stay valid (renumber hazard called out in section 21).

Verification: original doc md5 unchanged (dbfeb24...); new file 526 lines; grep confirms the 10 conditions and 5 lanes are present.

Request: independent review of the model's faithfulness to the original architecture. Docs slice, no adversarial lanes required.
