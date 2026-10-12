"""Public Approve page via a Cloudflare quick tunnel. No account required."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

log = logging.getLogger("athena")

_URL = re.compile(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com")
_proc: subprocess.Popen[str] | None = None


def start_tunnel(port: int) -> str:
    """Start cloudflared and return the public https origin. Empty if it cannot start."""
    if os.environ.get("ATHENA_TUNNEL", "cloudflared").strip().lower() in {"", "off", "0", "false"}:
        return ""
    binary = _binary()
    if not binary:
        log.error("cloudflared is not installed")
        return ""
    global _proc
    _proc = subprocess.Popen(
        [binary, "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    found = threading.Event()
    holder: list[str] = []

    def _read() -> None:
        assert _proc is not None and _proc.stdout is not None
        for line in _proc.stdout:
            text = line.rstrip()
            if text:
                log.info("tunnel %s", text)
            match = _URL.search(text)
            if match and not holder:
                holder.append(match.group(0))
                found.set()

    threading.Thread(target=_read, name="cloudflared", daemon=True).start()
    if not found.wait(timeout=25) or not holder:
        log.error("cloudflared did not return a public url")
        stop_tunnel()
        return ""
    os.environ["ATHENA_PUBLIC_BASE_URL"] = holder[0]
    _wait_ready(holder[0])
    return holder[0]


def _wait_ready(origin: str) -> None:
    """Quick tunnels answer 530 until the edge is ready."""
    for _ in range(8):
        try:
            with urlopen(f"{origin}/c/ready", timeout=10) as response:
                if response.status == 200:
                    return
        except HTTPError as exc:
            if exc.code < 500:
                return
        except URLError:
            pass
        time.sleep(2)
    log.info("tunnel warming up")


def stop_tunnel() -> None:
    global _proc
    if _proc is None or _proc.poll() is not None:
        _proc = None
        return
    _proc.terminate()
    _proc = None


def _binary() -> str:
    found = shutil.which("cloudflared")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/cloudflared", "/usr/local/bin/cloudflared"):
        if Path(candidate).is_file():
            return candidate
    return ""
