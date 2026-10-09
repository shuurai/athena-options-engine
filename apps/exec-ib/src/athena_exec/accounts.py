"""Which IB accounts the current mode may touch."""

from __future__ import annotations

import os


def trading_mode() -> str:
    """paper or live. The old approve_gated_live name means live."""
    raw = os.environ.get("ATHENA_TRADING_MODE", "paper").strip().lower()
    if raw == "approve_gated_live":
        return "live"
    if raw == "live":
        return "live"
    return "paper"


def active_accounts() -> list[str]:
    """Paper mode is the paper account. Live mode is every id in the list."""
    if trading_mode() == "live":
        raw = os.environ.get("ATHENA_IB_ACCOUNT_IDS", "")
        return [part.strip() for part in raw.split(",") if part.strip()]
    paper = os.environ.get("ATHENA_IB_PAPER_ACCOUNT_ID", "").strip()
    return [paper] if paper else []


def stamp_legacy_accounts(conn) -> int:
    """Mark unlabeled rows with the paper account. Does not delete anything."""
    paper = os.environ.get("ATHENA_IB_PAPER_ACCOUNT_ID", "").strip()
    if not paper:
        return 0
    updated = 0
    for table in (
        "drafts",
        "fills",
        "approvals",
        "halts",
        "settings_versions",
        "symbol_phases",
        "orders",
        "positions",
    ):
        cursor = conn.execute(
            f"UPDATE {table} SET account_id = %s WHERE account_id = ''",
            (paper,),
        )
        updated += cursor.rowcount
    conn.commit()
    return updated
