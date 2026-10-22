"""The words a user sees for chain and link statuses (display only).

The stored values stay as they are: chain status 'Broken' | 'Strained' | 'Holding' | 'No build', link status
red | amber | green | none (status_rules, the API contracts, the URL filters). Prose written for a reader
(Home lines, Events "changed", alert headlines and bodies) uses these words instead
(docs/methods/status-rules-v0.1.md, "Display words"; the web app's src/lib/status-labels.ts).
"""

from __future__ import annotations

from typing import Any

CHAIN_LABEL = {"Broken": "At risk", "Strained": "Watch", "Holding": "No flags", "No build": "Not started"}
LINK_LABEL = {"red": "Red flag", "amber": "Warning", "green": "No flags", "none": "No data"}

# The chain status after "site ..." in a sentence: "Project Jupiter now at risk".
CHAIN_CLAUSE = {"Broken": "at risk", "Strained": "on watch", "Holding": "has no flags",
                "No build": "marked not started"}
# A link status after "... link now": "lenders link now a red flag".
LINK_CLAUSE = {"red": "a red flag", "amber": "a warning", "green": "no flags", "none": "no data"}


def chain_label(status: Any) -> str:
    """'At risk' for 'Broken'; anything else as given ('n/d' for none)."""
    return CHAIN_LABEL.get(str(status), "n/d" if status is None else str(status))


def link_label(status: Any) -> str:
    """'Red flag' for 'red'; anything else as given ('n/d' for none)."""
    return LINK_LABEL.get(str(status), "n/d" if status is None else str(status))
