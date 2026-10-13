"""Athena orchestrator."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from athena_orchestrator.loop import run_loop
from athena_orchestrator.runtime import install_activity_log, install_log_trail


def load_env_file() -> None:
    """Fill missing variables from .env. Does not override the process environment."""
    path = Path(".env")
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def main() -> None:
    load_env_file()
    logging.basicConfig(
        level=os.environ.get("ATHENA_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log_path = Path("var/athena.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.getLogger().addHandler(logging.FileHandler(log_path, encoding="utf-8"))
    install_activity_log()
    install_log_trail()
    asyncio.run(run_loop())
