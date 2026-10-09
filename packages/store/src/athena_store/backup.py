"""Write a JSON snapshot of the paper tables. No S3 upload."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import psycopg

TABLES = (
    "settings_versions",
    "market_bars",
    "drafts",
    "approvals",
    "fills",
    "control_links",
    "symbol_phases",
    "orders",
    "positions",
    "halts",
    "nav_marks",
    "news_events",
    "calendar_marks",
    "settings_audits",
    "option_plans",
    "iv_marks",
    "mock_signals",
)


def write_snapshot(conn: psycopg.Connection, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    dump: dict[str, list[dict[str, object]]] = {}
    with conn.cursor() as cur:
        for table in TABLES:
            cur.execute(f"SELECT * FROM {table}")
            columns = [item.name for item in cur.description]
            dump[table] = [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"athena-{stamp}.json"
    path.write_text(json.dumps(dump, default=str), encoding="utf-8")
    return path
