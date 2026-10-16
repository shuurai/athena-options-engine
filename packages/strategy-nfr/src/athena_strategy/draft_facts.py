"""Deterministic fact pack for draft Reason text and LLM prompts."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

ATM_BAND = Decimal("0.01")


def moneyness(
    spot: Decimal | None,
    strike: Decimal | None,
    right: str | None,
) -> str | None:
    """ATM within 1% of strike; else ITM / OTM from put or call."""
    if spot is None or strike is None or strike <= 0 or not right:
        return None
    side = right.strip().upper()[:1]
    if side not in {"P", "C"}:
        return None
    distance = abs(spot - strike) / strike
    if distance <= ATM_BAND:
        return "ATM"
    if side == "P":
        return "ITM" if spot < strike else "OTM"
    return "ITM" if spot > strike else "OTM"


def format_draft_reason(sentence: str, counsel: str = "") -> str:
    """Sentence plus optional Counsel block for drafts.reason."""
    text = (sentence or "").strip()
    note = (counsel or "").strip()
    if text and note:
        return f"{text}\n\nCounsel: {note}"
    return text or note


def premium_captured_pct(avg_price: Decimal, market_price: Decimal | None) -> Decimal | None:
    if avg_price <= 0 or market_price is None:
        return None
    return (avg_price - market_price) / avg_price * Decimal(100)


def build_fact_lines(
    *,
    symbol: str,
    tactic: str,
    ticket_reason: str = "",
    spot: Decimal | None = None,
    strike: Decimal | None = None,
    right: str | None = None,
    expiry: date | None = None,
    today: date | None = None,
    avg_price: Decimal | None = None,
    market_price: Decimal | None = None,
    tags: list[str] | tuple[str, ...] | None = None,
) -> list[str]:
    """Short factual lines for prompts and fallback Reason text."""
    lines: list[str] = []
    code = (ticket_reason or tactic or "").strip()
    if code:
        lines.append(f"Ticket {code}.")
    captured = None
    if avg_price is not None:
        captured = premium_captured_pct(avg_price, market_price)
        if captured is not None:
            lines.append(f"Premium captured {captured:.0f}%.")
    label = moneyness(spot, strike, right)
    side = (right or "").strip().upper()[:1]
    if spot is not None and strike is not None and side in {"P", "C"}:
        wing = "P" if side == "P" else "C"
        gap = spot - strike
        pct = abs(gap) / strike * Decimal(100) if strike > 0 else Decimal(0)
        where = label or "—"
        direction = "below" if gap < 0 else "above" if gap > 0 else "at"
        lines.append(
            f"Spot {spot:.2f} vs {strike:g}{wing}: {where} "
            f"({abs(gap):.2f} {direction}, {pct:.1f}%)."
        )
    dte = None
    if expiry is not None and today is not None:
        dte = (expiry - today).days
        lines.append(f"DTE {dte}.")
    outlook = _assignment_outlook(label, side, dte)
    if outlook:
        lines.append(outlook)
    outcome = _assignment_outcome(label, side, spot, strike, avg_price, market_price, captured)
    if outcome:
        lines.append(outcome)
    if tags:
        lines.append("Tags: " + ", ".join(tags) + ".")
    return lines


def fact_pack_reason(**kwargs) -> str:
    """Multi-sentence fallback when the LLM does not return a sentence."""
    return " ".join(build_fact_lines(**kwargs))


def fact_pack_prompt_block(**kwargs) -> str:
    return "\n".join(build_fact_lines(**kwargs))


def _assignment_outlook(label: str | None, side: str, dte: int | None) -> str:
    if label is None:
        return ""
    if label == "ITM":
        return "Assignment risk is elevated (ITM)."
    if label == "ATM" and dte is not None and dte <= 2:
        return "Assignment / pin risk is elevated (ATM, short DTE)."
    if label == "OTM":
        return "Assignment unlikely while clearly OTM."
    if label == "ATM":
        return "Near ATM — assignment risk is material into expiry."
    return ""


def _assignment_outcome(
    label: str | None,
    side: str,
    spot: Decimal | None,
    strike: Decimal | None,
    avg_price: Decimal | None,
    market_price: Decimal | None,
    captured: Decimal | None,
) -> str:
    if side == "P" and label == "ITM" and strike is not None:
        credit = avg_price if avg_price is not None else Decimal(0)
        remaining = market_price if market_price is not None else Decimal(0)
        kept = credit - remaining if credit > 0 else Decimal(0)
        effective = strike - kept if kept > 0 else strike
        return (
            f"At expiry, assignment means long 100 shares at {strike:g} "
            f"(effective ~{effective:.2f} if remaining credit is kept). "
            "Closing now locks the captured premium and avoids assignment."
        )
    if side == "C" and label == "ITM" and strike is not None:
        return (
            f"At expiry, assignment means shares called away at {strike:g}. "
            "Closing keeps the short if you want to avoid the sale."
        )
    if side == "P" and label == "OTM" and captured is not None and captured >= 50:
        return "Closing frees capital while keeping most of the credit; assignment is unlikely."
    if label == "ATM":
        return "Near the strike into expiry — closing locks premium; holding accepts pin / assignment risk."
    return ""
