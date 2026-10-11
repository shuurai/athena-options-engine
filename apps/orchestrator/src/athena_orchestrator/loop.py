"""INIT then IDLE. Loads settings. Does not send orders."""

from __future__ import annotations

import asyncio
import logging
import os
import threading

from athena_bots import start_telegram_polling
from athena_exec.accounts import active_accounts, stamp_legacy_accounts, trading_mode
from athena_exec.session import refresh_display_rates
from athena_exec.ops import consume_restart
from athena_orchestrator.backfill import start_history_backfill
from athena_orchestrator.console import start_console_server
from athena_orchestrator.control import allowlist_from_env, start_control_server
from athena_orchestrator.paper import run_paper_cycle
from athena_orchestrator.runtime import begin_running, begin_waiting, mark, note_loop, note_telegram
from athena_strategy.dsh import set_score_listener
from athena_orchestrator.tunnel import start_tunnel, stop_tunnel
from athena_settings import load_active, seed_standard_if_empty
from athena_store import apply_migrations, connect

log = logging.getLogger("athena")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def _cycle_sleep(database_url: str, fallback: int) -> int:
    """Minutes from Settings, in seconds. The env value is only a fallback."""
    with connect(database_url) as conn:
        accounts = active_accounts()
        if not accounts:
            return fallback
        try:
            minutes = load_active(conn, accounts[0]).cycle_minutes
        except RuntimeError:
            return fallback
    return max(1, minutes) * 60


async def run_loop() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is not set. Copy .env.example to .env.")

    fallback = _env_int("ATHENA_LOOP_SECONDS", 300)
    cycles = _env_int("ATHENA_LOOP_CYCLES", 0)
    sleep_seconds = _cycle_sleep(database_url, fallback)
    note_loop(sleep_seconds)
    set_score_listener(lambda status, detail="": mark(3, status, detail))

    log.info("state=INIT")
    with connect(database_url) as conn:
        applied = apply_migrations(conn)
        if applied:
            log.info("migrations=%s", ",".join(applied))
        stamped = stamp_legacy_accounts(conn)
        if stamped:
            log.info("stamped_accounts=%s", stamped)
        accounts = active_accounts()
        for account in accounts:
            if seed_standard_if_empty(conn, account):
                log.info("settings_seed account=%s profile=standard", account)

    accounts = active_accounts()
    log.info("state=IDLE mode=%s accounts=%s", trading_mode(), ",".join(accounts) or "none")
    if not accounts:
        log.info("no account configured for this mode")

    control_port = os.environ.get("ATHENA_CONTROL_PORT", "")
    if control_port:
        server = start_control_server(database_url, int(control_port), allowlist_from_env())
        threading.Thread(target=server.serve_forever, daemon=True).start()
        log.info("control_port=%s", control_port)
        public = start_tunnel(int(control_port))
        if public:
            log.info("control_url=%s", public)

    console_port = _env_int("ATHENA_CONSOLE_PORT", 8788)
    console = start_console_server(database_url, console_port, allowlist_from_env())
    threading.Thread(target=console.serve_forever, daemon=True).start()
    log.info("console_port=%s", console_port)
    start_history_backfill(database_url)
    threading.Thread(
        target=refresh_display_rates, args=(database_url,), daemon=True, name="fx-rates"
    ).start()

    if os.environ.get("TELEGRAM_MODE", "polling") == "polling":
        start_telegram_polling(database_url)
        note_telegram(True)
        log.info("telegram polling")

    def cycle() -> None:
        with connect(database_url) as conn:
            run_paper_cycle(conn)

    completed = 1
    begin_running(completed)
    cycle()
    begin_waiting(sleep_seconds)
    while cycles == 0 or completed < cycles:
        waited = 0
        while waited < sleep_seconds:
            if consume_restart():
                log.info("ops restart")
                stop_tunnel()
                return
            step = min(1, sleep_seconds - waited)
            await asyncio.sleep(step)
            waited += step
        completed += 1
        begin_running(completed)
        cycle()
        sleep_seconds = _cycle_sleep(database_url, fallback)
        note_loop(sleep_seconds)
        begin_waiting(sleep_seconds)
        log.info("health ok cycle=%s state=IDLE", completed)
