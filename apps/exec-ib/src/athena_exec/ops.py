"""Allowlisted restart requests. No arbitrary shell."""

from __future__ import annotations

import os
import shlex
import socket
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
    raise ValueError("ops command must be health, logs, athena restart, or gateway restart")


def gateway_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """True when the Gateway port accepts a connection. This does not log in."""
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


def log_tail(path: Path, lines: int = 20) -> str:
    if not path.is_file():
        return "No log file yet."
    kept = path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    return "\n".join(kept)[-1500:] or "No log file yet."


def consume_restart() -> bool:
    if not RESTART_FLAG.is_file():
        return False
    RESTART_FLAG.unlink()
    return True
