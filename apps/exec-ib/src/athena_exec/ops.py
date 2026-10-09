"""Allowlisted restart requests. No arbitrary shell."""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

RESTART_FLAG = Path("var/athena.restart")


def ops_action(actor: str, allowlist: set[str], command: str) -> str:
    if actor not in allowlist:
        raise PermissionError("actor is not on the Telegram allowlist")
    text = command.strip()
    if text == "athena restart":
        RESTART_FLAG.parent.mkdir(parents=True, exist_ok=True)
        RESTART_FLAG.write_text(actor, encoding="utf-8")
        return "Athena restart scheduled"
    if text == "gateway restart":
        raw = os.environ.get("ATHENA_GATEWAY_RESTART_CMD", "").strip()
        if not raw:
            return "ATHENA_GATEWAY_RESTART_CMD is empty"
        subprocess.run(shlex.split(raw), check=False)
        return "Gateway restart command started"
    raise ValueError("ops command must be athena restart or gateway restart")


def consume_restart() -> bool:
    if not RESTART_FLAG.is_file():
        return False
    RESTART_FLAG.unlink()
    return True
