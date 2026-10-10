"""Closed classifier badges for the symbol pool. DSH may only pick from this list."""

CLASSIFIERS: tuple[tuple[str, str, str], ...] = (
    ("MAG7", "blue", "One of the seven largest US tech names."),
    ("MEGA_CAP", "blue", "Among the largest listed companies."),
    ("AI_NARRATIVE", "teal", "The story trades on AI spending."),
    ("SEMIS", "violet", "Semiconductor supply chain."),
    ("HIGH_BETA", "amber", "Moves more than the market."),
    ("CYCLICAL", "amber", "Tied to the economic cycle."),
    ("DEFENSIVE", "teal", "Steady demand, lower swing."),
    ("DIVIDEND", "green", "Held for the payout."),
    ("STRONG_FCF", "green", "Throws off free cash flow."),
    ("WEAK_MARGIN", "red", "Thin or unstable margins."),
)

CLASSIFIER_BADGE = {code: badge for code, badge, _meaning in CLASSIFIERS}
CLASSIFIER_MEANING = {code: meaning for code, _badge, meaning in CLASSIFIERS}
CLASSIFIER_CODES = frozenset(CLASSIFIER_BADGE)


def clean_classifiers(raw: str, limit: int = 12) -> str:
    """Keep glossary tokens, in order, without duplicates."""
    parts: list[str] = []
    for item in (raw or "").replace(";", ",").split(","):
        token = "".join(char for char in item.strip().upper() if char.isalnum() or char == "_")
        if token in CLASSIFIER_CODES and token not in parts:
            parts.append(token)
    return ",".join(parts[:limit])
