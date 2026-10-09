"""INIT then IDLE. Loads settings. Does not send orders."""

from __future__ import annotations

import asyncio
import logging
import os
import threading

from athena_bots import start_telegram_polling
from athena_exec.accounts import active_accounts, stamp_legacy_accounts, trading_mode
from athena_exec.ops import consume_restart
from athena_orchestrator.control import allowlist_from_env, start_control_server
from athena_orchestrator.paper import run_paper_cycle
from athena_orchestrator.tunnel import start_tunnel, stop_tunnel
from athena_settings import seed_standard_if_empty
from athena_store import apply_migrations, connect

log = logging.getLogger("athena")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


async def run_loop() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is not set. Copy .env.example to .env.")

    sleep_seconds = _env_int("ATHENA_LOOP_SECONDS", 300)
    cycles = _env_int("ATHENA_LOOP_CYCLES", 0)

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

    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if telegram_token and os.environ.get("TELEGRAM_MODE", "polling") == "polling":
        start_telegram_polling(database_url, telegram_token, allowlist_from_env())
        log.info("telegram polling")

    def cycle() -> None:
        with connect(database_url) as conn:
            run_paper_cycle(conn)

    cycle()
    completed = 0
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
        cycle()
        log.info("health ok cycle=%s state=IDLE", completed)
