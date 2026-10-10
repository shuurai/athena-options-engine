"""Closed glossary of news event types. Badge colors match the console mock."""

from __future__ import annotations

# code, badge class, one-line meaning
NEWS_TYPES: tuple[tuple[str, str, str], ...] = (
    ("GENERIC_NEGATIVE", "red", "A negative headline with no named event."),
    ("GENERIC_POSITIVE", "green", "A positive headline with no named event."),
    ("NARRATIVE_ATTACK", "red", "Clustered negative spin, often from a weak source."),
    ("FED_RATE_DECISION", "blue", "A Fed or FOMC rate decision."),
    ("EARNINGS_RELEASE", "blue", "An earnings release or earnings date."),
    ("AI_REGULATION", "amber", "AI rules, disclosure, or a regulatory proposal."),
    ("SUPPLY_CHAIN", "amber", "A supply, memory, or component constraint."),
    ("ANTITRUST_FINE", "amber", "An antitrust case, fine, or competition probe."),
    ("GENERIC", "", "A headline that fits none of the named types."),
)

NEWS_TYPE_BADGE = {code: badge for code, badge, _meaning in NEWS_TYPES}
NEWS_TYPE_MEANING = {code: meaning for code, _badge, meaning in NEWS_TYPES}
NEWS_TYPE_CODES = frozenset(NEWS_TYPE_BADGE)
