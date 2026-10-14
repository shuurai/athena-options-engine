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
from athena_settings import cycle_book, load_active, open_symbols, seed_standard_if_empty, standard_payload
from athena_settings.resolve import control_flag, itick_key
from athena_store.backup import prune_snapshots, write_snapshot
from athena_store.s3 import sync_snapshot
from athena_bots import notify_draft, notify_expired, notify_fill
from athena_bots.public_url import control_page_url
from athena_exec.slots import draft_allowed, observe_allowed, slot_state
from athena_strategy import evaluate_watchlist, issue_link
from athena_strategy.agents import run_observe, run_signaled
from athena_strategy.wheel import slot_split_enabled
from athena_strategy.drafts import expire_pending
from athena_strategy.iv import record_iv_marks
from athena_strategy.mock import prepare_mock_orders
from athena_strategy.budget import apply_cycle_budget
from athena_strategy.manage import manage_book
from athena_orchestrator.runtime import finish, mark
from athena_orchestrator.sim import cycle_gate, symbols_for
from athena_strategy.news import ingest_news

log = logging.getLogger("athena")


def extra_sync_ids(settings, account: str, session_ids: set[str]) -> list[str]:
    """Books to sync on this connection. An account with its own turn is left out."""
    found = []
    for entry in settings.extra_accounts:
        other = entry.account_id
        if not other or other == account or other in session_ids:
            continue
        if control_flag(settings, other, "sync", False):
            found.append(other)
    return found


def run_paper_cycle(conn: psycopg.Connection) -> None:
    mark(0, "running", "ib session")
    accounts = active_accounts()
    books = []
    for account in accounts:
        seed_standard_if_empty(conn, account)
        books.append((account, cycle_book(load_active(conn, account))))

    gates = {account: cycle_gate(conn, account) for account, _settings in books}
    live = [(account, settings) for account, settings in books if gates[account][0] == "live"]
    session_ids = {account for account, _settings in books}

    if live:
        for account, settings in live:
            mark(0, "running", f"session {account}")
            _live_session(conn, settings, account, session_ids)
        watchlist = _union_watchlist(live)
        base_url = live[0][1].itick.base_url
        news_settings = live[0][1]
        finish(0, "session=ok")
        _shared_market(conn, watchlist, base_url, news_settings)
    elif books:
        mark(0, "skipped", "simulation")
        finish(0, "sim")
        mark(1, "skipped", "live quotes off")
        finish(1, "bars=0")
        mark(2, "skipped", "live news off")
        finish(2, "news=0")
        watchlist = []
    else:
        log.info("ib_session=idle no account")
        payload = standard_payload()
        watchlist = list(payload.watchlist)
        finish(0, "session=idle")
        _shared_market(conn, watchlist, payload.itick.base_url, payload)

    mark(4, "running")
    iv_count = 0
    if live or not books:
        today = datetime.now(ZoneInfo("Asia/Hong_Kong")).date()
        iv_count += record_iv_marks(conn, watchlist, today)
        log.info("iv_marks=%s", iv_count)
    for account, _settings in books:
        mode, day = gates[account]
        if mode == "play" and day is not None:
            iv_count += record_iv_marks(conn, open_symbols(_settings, symbols_for(conn, account)), day)
    finish(4, f"iv={iv_count}")

    phase_count = 0
    draft_count = 0
    for account, settings in books:
        mode, day = gates[account]
        _expire(conn, account, settings)
        mark(5, "running", f"phases {account}")
        if mode == "live":
            phases, drafts = _live_drafts(conn, settings, account)
        elif mode == "play" and day is not None:
            phases, drafts = _replay_drafts(conn, settings, account, day)
        else:
            log.info("simulation account=%s mode=%s", account, mode)
            mark(6, "running", f"drafts {account}")
            continue
        phase_count += phases
        draft_count += drafts
        mark(6, "running", f"drafts {account}")
    finish(5, f"phases={phase_count}")
    finish(6, f"drafts={draft_count}")
    _backup(conn, books)


def _union_watchlist(books: list) -> list[str]:
    watchlist: list[str] = []
    for _account, settings in books:
        for symbol in settings.watchlist:
            if symbol not in watchlist:
                watchlist.append(symbol)
    return watchlist


def _live_session(conn, settings, account: str, session_ids: set[str]) -> None:
    trading_on = control_flag(settings, account, "trading", True)
    sync_on = control_flag(settings, account, "sync", True)
    if trading_on:
        prepare_mock_orders(conn, settings, account)
    noticed = run_session(
        conn,
        settings,
        account,
        sync=sync_on,
        trading=trading_on,
        also_sync=extra_sync_ids(settings, account, session_ids),
    )
    for notice in noticed:
        account_id, symbol, tactic, action, draft_id, price = notice[:6]
        shares = notice[6] if len(notice) > 6 else None
        multiplier = notice[7] if len(notice) > 7 else None
        commission = notice[8] if len(notice) > 8 else None
        commission_ccy = notice[9] if len(notice) > 9 else ""
        sec_type = notice[10] if len(notice) > 10 else ""
        log.info("fill notice symbol=%s price=%s qty=%s", symbol, price, shares)
        try:
            notify_fill(
                account_id, symbol, tactic, action, draft_id, price, shares,
                multiplier, commission, commission_ccy, sec_type,
            )
        except RuntimeError as exc:
            log.error("%s", exc)


def _shared_market(conn, watchlist: list[str], base_url: str, settings) -> None:
    mark(1, "running")
    written = write_watchlist_bars(conn, watchlist, base_url, itick_key(settings))
    log.info("bars=%s", written)
    finish(1, f"bars={written}")
    mark(2, "running")
    try:
        stored = ingest_news(conn, watchlist, settings)
        log.info("news=%s", stored)
        finish(2, f"news={stored}")
    except Exception as exc:
        log.error("news intake failed: %s", exc)
        finish(2, "news failed")
        mark(3, "skipped", "dsh skipped")


def _expire(conn, account: str, settings) -> None:
    ttl = settings.notify.draft_ttl_minutes
    for draft_id, symbol, tactic, draft_account in expire_pending(conn, account, ttl):
        log.info("draft expired id=%s symbol=%s", draft_id, symbol)
        try:
            notify_expired(draft_id, symbol, tactic, draft_account)
        except RuntimeError as exc:
            log.error("%s", exc)


def _live_drafts(conn, settings, account: str) -> tuple[int, int]:
    ttl = settings.notify.draft_ttl_minutes
    if control_flag(settings, account, "trading", True):
        draft_ids = evaluate_watchlist(conn, settings, account)
        draft_ids.extend(manage_book(conn, settings, account))
        now = datetime.now(ZoneInfo("Asia/Hong_Kong"))
        if slot_split_enabled():
            if draft_allowed(now, settings.ib, settings.strategy.draft_slots):
                draft_ids.extend(run_signaled(conn, settings, account))
            elif observe_allowed(now, settings.ib, settings.strategy.observe_slots):
                run_observe(conn, settings, account)
        elif slot_state(now, settings.ib) == "athena":
            draft_ids.extend(run_signaled(conn, settings, account))
        draft_ids = apply_cycle_budget(conn, settings, account, draft_ids)
    else:
        draft_ids = []
    _notify_live_drafts(conn, draft_ids, ttl)
    return len(settings.watchlist), len(draft_ids)


def _replay_drafts(conn, settings, account: str, day) -> tuple[int, int]:
    names = open_symbols(settings, symbols_for(conn, account))
    scoped = settings.model_copy(update={"watchlist": names})
    ttl = settings.notify.draft_ttl_minutes
    draft_ids = []
    if control_flag(settings, account, "trading", True):
        draft_ids = evaluate_watchlist(conn, scoped, account)
        draft_ids.extend(manage_book(conn, scoped, account, day))
        draft_ids.extend(run_signaled(conn, scoped, account))
        draft_ids = apply_cycle_budget(conn, scoped, account, draft_ids)
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
    return len(names), len(draft_ids)


def _notify_live_drafts(conn, draft_ids: list, ttl: int) -> None:
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
                control_page_url(token),
                draft_account,
            )
        except RuntimeError as exc:
            log.error("%s", exc)


def _backup(conn, books: list) -> None:
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
