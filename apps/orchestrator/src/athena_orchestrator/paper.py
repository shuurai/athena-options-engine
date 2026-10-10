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
from athena_settings.resolve import control_flag, itick_key
from athena_store.backup import prune_snapshots, write_snapshot
from athena_store.s3 import sync_snapshot
from athena_bots import notify_draft, notify_expired, notify_fill
from athena_bots.public_url import control_page_url
from athena_exec.slots import slot_state
from athena_strategy import evaluate_watchlist, issue_link
from athena_strategy.agents import run_signaled
from athena_strategy.drafts import expire_pending
from athena_strategy.iv import record_iv_marks
from athena_strategy.mock import prepare_mock_orders
from athena_strategy.manage import manage_book
from athena_orchestrator.runtime import finish, mark
from athena_orchestrator.sim import cycle_gate, symbols_for
from athena_strategy.news import ingest_news

log = logging.getLogger("athena")


def run_paper_cycle(conn: psycopg.Connection) -> None:
    mark(0, "running", "ib session")
    accounts = active_accounts()
    books = []
    for account in accounts:
        seed_standard_if_empty(conn, account)
        books.append((account, load_active(conn, account)))

    gates = {account: cycle_gate(conn, account) for account, _settings in books}
    if books and any(mode != "live" for mode, _day in gates.values()):
        _replay_cycle(conn, books, gates)
        return

    if books:
        for account, settings in books:
            trading_on = control_flag(settings, account, "trading", True)
            sync_on = control_flag(settings, account, "sync", True)
            also_sync = [
                entry.account_id
                for entry in settings.extra_accounts
                if entry.account_id != account and control_flag(settings, entry.account_id, "sync", False)
            ]
            if trading_on:
                prepare_mock_orders(conn, settings, account)
            for account_id, symbol, tactic, action, draft_id, price in run_session(
                conn, settings, account, sync=sync_on, trading=trading_on, also_sync=also_sync,
            ):
                log.info("fill notice symbol=%s price=%s", symbol, price)
                try:
                    notify_fill(account_id, symbol, tactic, action, draft_id, price)
                except RuntimeError as exc:
                    log.error("%s", exc)
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
    finish(0, "session=ok" if books else "session=idle")

    mark(1, "running")
    written = write_watchlist_bars(
        conn,
        watchlist,
        base_url,
        itick_key(books[0][1]) if books else os.environ.get("ITICK_API_KEY", ""),
    )
    log.info("bars=%s", written)
    finish(1, f"bars={written}")

    mark(2, "running")
    news_settings = books[0][1] if books else standard_payload()
    try:
        stored = ingest_news(conn, watchlist, news_settings)
        log.info("news=%s", stored)
        finish(2, f"news={stored}")
    except Exception as exc:
        log.error("news intake failed: %s", exc)
        finish(2, "news failed")
        mark(3, "skipped", "dsh skipped")

    mark(4, "running")
    today = datetime.now(ZoneInfo("Asia/Hong_Kong")).date()
    iv_count = record_iv_marks(conn, watchlist, today)
    log.info("iv_marks=%s", iv_count)
    finish(4, f"iv={iv_count}")

    phase_count = 0
    draft_count = 0
    for account, settings in books:
        ttl = settings.notify.draft_ttl_minutes
        for draft_id, symbol, tactic, draft_account in expire_pending(conn, account, ttl):
            log.info("draft expired id=%s symbol=%s", draft_id, symbol)
            try:
                notify_expired(draft_id, symbol, tactic, draft_account)
            except RuntimeError as exc:
                log.error("%s", exc)
        mark(5, "running")
        if control_flag(settings, account, "trading", True):
            draft_ids = evaluate_watchlist(conn, settings, account)
            if slot_state(datetime.now(ZoneInfo("Asia/Hong_Kong")), settings.ib) == "athena":
                draft_ids.extend(run_signaled(conn, settings, account))
        else:
            draft_ids = []
        phase_count += len(settings.watchlist)
        mark(6, "running")
        if control_flag(settings, account, "trading", True):
            draft_ids.extend(manage_book(conn, settings, account))
        draft_count += len(draft_ids)
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

    finish(5, f"phases={phase_count}")
    finish(6, f"drafts={draft_count}")
    mark(7, "running")
    directory = Path(os.environ.get("ATHENA_LOCAL_BACKUP_DIR", "var/backups"))
    path = write_snapshot(conn, directory)
    retain = books[0][1].retain_local_days if books else 14
    removed = prune_snapshots(directory, retain)
    log.info("backup=%s pruned=%s", path.name, removed)
    finish(7, f"backup={path.name} pruned={removed}")
    try:
        key = sync_snapshot(path)
    except Exception as exc:
        log.error("s3 sync failed: %s", exc)
        return
    if key:
        log.info("s3=%s", key)


def _replay_cycle(conn: psycopg.Connection, books: list, gates: dict) -> None:
    """One stored day. Live session, quotes, and news stay off."""
    mark(0, "skipped", "simulation")
    finish(0, "sim")
    mark(1, "skipped", "live quotes off")
    finish(1, "bars=0")
    mark(2, "skipped", "live news off")
    finish(2, "news=0")
    mark(4, "running")
    iv_count = 0
    phase_count = 0
    draft_count = 0
    for account, settings in books:
        mode, day = gates[account]
        ttl = settings.notify.draft_ttl_minutes
        for draft_id, symbol, tactic, draft_account in expire_pending(conn, account, ttl):
            log.info("draft expired id=%s symbol=%s", draft_id, symbol)
            try:
                notify_expired(draft_id, symbol, tactic, draft_account)
            except RuntimeError as exc:
                log.error("%s", exc)
        if mode != "play" or day is None:
            log.info("simulation account=%s mode=%s", account, mode)
            continue
        names = symbols_for(conn, account)
        scoped = settings.model_copy(update={"watchlist": names})
        iv_count += record_iv_marks(conn, names, day)
        mark(5, "running")
        draft_ids = []
        if control_flag(settings, account, "trading", True):
            draft_ids = evaluate_watchlist(conn, scoped, account)
            draft_ids.extend(run_signaled(conn, scoped, account))
            draft_ids.extend(manage_book(conn, scoped, account, day))
        phase_count += len(names)
        draft_count += len(draft_ids)
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
            log.info("control_token=%s account=%s replay=%s", token, draft_account, day)
            try:
                notify_draft(
                    draft_id, symbol, tactic, contracts, phase, reason,
                    control_page_url(token), draft_account,
                )
            except RuntimeError as exc:
                log.error("%s", exc)
    finish(4, f"iv={iv_count}")
    finish(5, f"phases={phase_count}")
    mark(6, "running")
    finish(6, f"drafts={draft_count}")
    mark(7, "running")
    directory = Path(os.environ.get("ATHENA_LOCAL_BACKUP_DIR", "var/backups"))
    path = write_snapshot(conn, directory)
    retain = books[0][1].retain_local_days if books else 14
    removed = prune_snapshots(directory, retain)
    log.info("backup=%s pruned=%s", path.name, removed)
    finish(7, f"backup={path.name} pruned={removed}")
    try:
        key = sync_snapshot(path)
    except Exception as exc:
        log.error("s3 sync failed: %s", exc)
        return
    if key:
        log.info("s3=%s", key)
