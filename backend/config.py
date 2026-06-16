"""Standing rules and portal-specific config for the lead agent.

These are the policies that must hold REGARDLESS of what a user types in a prompt.
Keeping them here, out of the LLM's control, is what makes junk filtering and deal
classification deterministic and auditable instead of prompt-dependent.
"""
from __future__ import annotations

# Leads marked junk/unqualified/disqualified/discarded NEVER appear in results, no
# matter the prompt. Matched case-insensitively as substrings of `hs_lead_status` and
# `lifecyclestage` (against both the stored value and its display label), so it catches
# e.g. "Junk Lead", "Unqualified", "Demo Completed - Disqualified", "Discarded".
# Recoverable states (Stalled, Demo No Show, Cancelled) are intentionally NOT junk.
JUNK_LEAD_KEYWORDS: list[str] = ["junk", "unqualified", "disqualified", "discarded"]

# Close-lost reasons that are still RECOVERABLE (timing / no response / bandwidth)
# vs. hard-dead (not a fit / no budget / chose a competitor). Used to decide whether
# a closed-lost deal may be included in a reactivation pull (test A1).
RECOVERABLE_LOSS_KEYWORDS: list[str] = [
    "timing", "timeline", "no response", "non-responsive", "unresponsive",
    "ghosted", "no reply", "bandwidth", "resource", "capacity", "on hold",
    "revisit", "later", "not now", "follow up",
]
HARD_DEAD_LOSS_KEYWORDS: list[str] = [
    "not a fit", "not the right fit", "no budget", "budget", "too expensive",
    "price", "competitor", "chose a competitor", "went with", "lost to",
]
