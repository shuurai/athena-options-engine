"""Paper drafts only. This package never talks to IB."""

from athena_strategy.drafts import decide, ensure_paper_draft, issue_link, use_link
from athena_strategy.phases import evaluate_watchlist

__all__ = ["decide", "ensure_paper_draft", "evaluate_watchlist", "issue_link", "use_link"]
