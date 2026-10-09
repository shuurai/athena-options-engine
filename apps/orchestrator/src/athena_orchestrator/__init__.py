"""Athena orchestrator."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from athena_orchestrator.loop import run_loop


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
    asyncio.run(run_loop())
