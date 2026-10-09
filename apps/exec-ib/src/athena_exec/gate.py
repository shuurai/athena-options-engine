"""Decide whether an approved draft may become an IB order."""

from __future__ import annotations

from decimal import Decimal

SOFT_BLOCKS = {
    "trading is halted",
    "outside athena slot",
}


def live_block_reason(
    *,
    slot: str,
    draft_status: str,
    halted: bool,
    account_id: str,
    instrument: str | None,
    limit_price: Decimal | None,
) -> str | None:
    """Return why placeOrder must not be called. None means the gate is open."""
    if draft_status != "approved":
        return "draft is not approved"
    if halted:
        return "trading is halted"
    if slot != "athena":
        return "outside athena slot"
    if not account_id:
        return "account id is empty"
    if instrument not in {"STK", "OPT"}:
        return "draft has no stock limit contract"
    if limit_price is None or limit_price <= 0:
        return "draft has no limit price"
    return None
