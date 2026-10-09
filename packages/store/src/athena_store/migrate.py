"""Apply numbered SQL files once. Tracks them in schema_migrations."""

from __future__ import annotations

from importlib.resources import files

import psycopg

MIGRATION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


def connect(database_url: str) -> psycopg.Connection:
    return psycopg.connect(database_url)


def _statements(sql: str) -> list[str]:
    parts = [part.strip() for part in sql.split(";")]
    return [part for part in parts if part]


def apply_migrations(conn: psycopg.Connection) -> list[str]:
    """Apply pending migrations. Returns the versions applied this call."""
    conn.execute(MIGRATION_TABLE)
    applied = {
        row[0]
        for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
    }
    folder = files("athena_store").joinpath("migrations")
    names = sorted(
        entry.name
        for entry in folder.iterdir()
        if entry.name.endswith(".sql")
    )
    new_versions: list[str] = []
    for name in names:
        if name in applied:
            continue
        sql = folder.joinpath(name).read_text(encoding="utf-8")
        with conn.cursor() as cur:
            for statement in _statements(sql):
                cur.execute(statement)
        conn.execute(
            "INSERT INTO schema_migrations (version) VALUES (%s)",
            (name,),
        )
        new_versions.append(name)
    conn.commit()
    return new_versions


def settings_version_count(conn: psycopg.Connection) -> int:
    row = conn.execute("SELECT count(*) FROM settings_versions").fetchone()
    return int(row[0])
