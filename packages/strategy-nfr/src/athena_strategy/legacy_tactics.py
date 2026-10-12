"""Previous opening rules. The cycle does not call this module."""

from athena_strategy.tags import OVERBOUGHT, OVERSOLD, SUPPORT_TOUCH


def choose_tactic(phase: str, tags: set[str], has_shares: bool, has_short: bool = False) -> str:
    """Pick the tactic for this phase. HOLD when there is no edge."""
    del has_short
    if phase in {"P4", "P5"} and OVERSOLD in tags and SUPPORT_TOUCH in tags:
        return "OPEN_CSP"
    if phase == "P5" and OVERBOUGHT in tags and has_shares:
        return "OPEN_CC"
    return "HOLD"
