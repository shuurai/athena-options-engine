"""Approve-page URL. Uses the tunnel origin when the orchestrator has one."""

from __future__ import annotations

import os


def control_page_url(token: str) -> str:
    base = os.environ.get("ATHENA_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not base:
        port = os.environ.get("ATHENA_CONTROL_PORT", "").strip()
        if not port:
            return ""
        base = f"http://127.0.0.1:{port}"
    return f"{base}/c/{token}"
