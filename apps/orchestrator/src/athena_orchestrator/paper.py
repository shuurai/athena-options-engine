"""One cycle: each active account, shared bars, a local snapshot."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg

from athena_exec import run_session
from athena_exec.accounts import active_accounts
from athena_marketdata import write_watchlist_bars
from athena_settings import load_active, seed_standard_if_empty, standard_payload
from athena_store.backup import write_snapshot
from athena_store.s3 import sync_snapshot
from athena_bots import notify_draft, notify_expired
from athena_bots.public_url import control_page_url
from athena_strategy import evaluate_watchlist, issue_link
from athena_strategy.drafts import expire_pending
from athena_strategy.iv import record_iv_marks
from athena_strategy.mock import prepare_mock_orders
from athena_strategy.manage import manage_book
from athena_strategy.news import ingest_news

log = logging.getLogger("athena")


def run_paper_cycle(conn: psycopg.Connection) -> None:
    accounts = active_accounts()
    books = []
    for account in accounts:
        seed_standard_if_empty(conn, account)
        books.append((account, load_active(conn, account)))

    if books:
        for account, settings in books:
            prepare_mock_orders(conn, settings, account)
            run_session(conn, settings, account)
        watchlist: list[str] = []
        base_url = books[0][1].itick.base_url
        for _account, settings in books:
            for symbol in settings.watchlist:
                if symbol not in watchlist:
                    watchlist.append(symbol)
    else:
        log.info("ib_session=idle no account")
        payload = standard_payload()
        watchlist = list(payload.watchlist)
        base_url = payload.itick.base_url

    written = write_watchlist_bars(
        conn,
        watchlist,
        base_url,
        os.environ.get("ITICK_API_KEY", ""),
    )
    log.info("bars=%s", written)

    news_settings = books[0][1] if books else standard_payload()
    try:
        log.info("news=%s", ingest_news(conn, watchlist, news_settings))
    except Exception as exc:
        log.error("news intake failed: %s", exc)

    today = datetime.now(ZoneInfo("Asia/Hong_Kong")).date()
    log.info("iv_marks=%s", record_iv_marks(conn, watchlist, today))

    for account, settings in books:
        ttl = settings.notify.draft_ttl_minutes
        for draft_id, symbol, tactic, draft_account in expire_pending(conn, account, ttl):
            log.info("draft expired id=%s symbol=%s", draft_id, symbol)
            try:
                notify_expired(draft_id, symbol, tactic, draft_account)
            except RuntimeError as exc:
                log.error("%s", exc)
        draft_ids = evaluate_watchlist(conn, settings, account)
        draft_ids.extend(manage_book(conn, settings, account))
        for draft_id in draft_ids:
            token = issue_link(conn, draft_id, ttl_minutes=ttl)
            row = conn.execute(
                """
                SELECT symbol, tactic, contracts, phase, reason, account_id
                FROM drafts WHERE id = %s
                """,
                (draft_id,),
            ).fetchone()
            symbol, tactic, contracts, phase, reason, draft_account = row
            control_url = control_page_url(token)
            log.info("control_token=%s account=%s", token, draft_account)
            log.info("feishu stub")
            try:
                notify_draft(
                    draft_id,
                    symbol,
                    tactic,
                    contracts,
                    phase,
                    reason,
                    control_url,
                    draft_account,
                )
            except RuntimeError as exc:
                log.error("%s", exc)

    directory = Path(os.environ.get("ATHENA_LOCAL_BACKUP_DIR", "var/backups"))
    path = write_snapshot(conn, directory)
    log.info("backup=%s", path.name)
    try:
        key = sync_snapshot(path)
    except Exception as exc:
        log.error("s3 sync failed: %s", exc)
        return
    if key:
        log.info("s3=%s", key)
