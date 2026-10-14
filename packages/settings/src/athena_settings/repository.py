"""Read and seed the active settings row."""

from __future__ import annotations

import psycopg
from psycopg.types.json import Jsonb

from athena_settings.model import SettingsPayload, standard_payload


class SettingsVersion(SettingsPayload):
    id: int
    author: str
    note: str


def _payload_from_db(value: object) -> SettingsPayload:
    if isinstance(value, str):
        return SettingsPayload.model_validate_json(value)
    return SettingsPayload.model_validate(value)


def seed_standard_if_empty(conn: psycopg.Connection, account_id: str) -> bool:
    """Insert a Standard row when this account has none. Returns True if inserted."""
    existing = conn.execute(
        "SELECT id FROM settings_versions WHERE active AND account_id = %s",
        (account_id,),
    ).fetchone()
    if existing is not None:
        return False
    payload = standard_payload()
    next_id = conn.execute("SELECT coalesce(max(id), 0) + 1 FROM settings_versions").fetchone()[0]
    conn.execute(
        """
        INSERT INTO settings_versions (id, author, note, payload, active, account_id)
        VALUES (%s, %s, %s, %s, true, %s)
        """,
        (next_id, "seed", "Standard profile", Jsonb(payload.model_dump()), account_id),
    )
    conn.commit()
    return True


KNOBS = {
    "roll_dte": ("strategy", "roll_dte", 1, 60),
    "profit_take_pct": ("strategy", "profit_take_pct", 1, 100),
    "max_contracts": ("risk", "max_contracts_per_symbol", 1, 20),
    "wash_min_negative": ("news", "wash_min_negative", 1, 20),
    "draft_ttl": ("notify", "draft_ttl_minutes", 15, 240),
}


def save_settings(
    conn: psycopg.Connection,
    account_id: str,
    payload: SettingsPayload,
    author: str,
    note: str,
) -> tuple[int, str]:
    """Store a new active version and an audit diff. Privilege cannot change here."""
    current = load_active(conn, account_id)
    old = _payload_of(current)
    if payload.privilege != old.privilege:
        raise ValueError("privilege stays all_orders")
    changes = _diff(old, payload)
    if not changes:
        raise ValueError("no change")
    conn.execute(
        "UPDATE settings_versions SET active = false WHERE active AND account_id = %s",
        (account_id,),
    )
    next_id = conn.execute("SELECT coalesce(max(id), 0) + 1 FROM settings_versions").fetchone()[0]
    conn.execute(
        """
        INSERT INTO settings_versions (id, author, note, payload, active, account_id)
        VALUES (%s, %s, %s, %s, true, %s)
        """,
        (next_id, author, note, Jsonb(payload.model_dump()), account_id),
    )
    conn.execute(
        """
        INSERT INTO settings_audits (account_id, settings_id, author, note, changes)
        VALUES (%s, %s, %s, %s, %s)
        """,
        (account_id, next_id, author, note, changes),
    )
    conn.commit()
    return next_id, changes


def set_knob(conn: psycopg.Connection, account_id: str, key: str, raw: str, author: str) -> str:
    """Change one allowed knob and return the audit line."""
    if key == "privilege":
        raise ValueError("privilege stays all_orders")
    if key not in KNOBS:
        known = ", ".join(sorted(KNOBS))
        raise ValueError(f"unknown setting. Use {known}")
    section, field, low, high = KNOBS[key]
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("value must be a number") from exc
    if not low <= value <= high:
        raise ValueError(f"{key} must be {low} to {high}")
    current = _payload_of(load_active(conn, account_id))
    updated = getattr(current, section).model_copy(update={field: value})
    payload = current.model_copy(update={section: updated})
    new_id, changes = save_settings(conn, account_id, payload, author, f"set {key}")
    return f"{account_id} v{new_id} {changes}"


def format_settings(version: SettingsVersion) -> str:
    payload = _payload_of(version)
    return "\n".join(
        [
            f"v{version.id}",
            f"privilege {payload.privilege}",
            f"roll_dte {payload.strategy.roll_dte}",
            f"profit_take_pct {payload.strategy.profit_take_pct}",
            f"max_contracts {payload.risk.max_contracts_per_symbol}",
            f"wash_min_negative {payload.news.wash_min_negative}",
            f"draft_ttl {payload.notify.draft_ttl_minutes}",
        ]
    )


def _payload_of(version: SettingsVersion) -> SettingsPayload:
    data = version.model_dump()
    return SettingsPayload.model_validate({key: data[key] for key in SettingsPayload.model_fields})


_SECRET_LEAVES = {"api_key", "bot_token", "deepseek_token", "openrouter_token", "claude_token", "kimi_token"}


def _diff(old: SettingsPayload, new: SettingsPayload) -> str:
    before = _flat(old.model_dump())
    after = _flat(new.model_dump())
    lines = []
    for key in sorted(set(before) | set(after)):
        old_value = before.get(key)
        new_value = after.get(key)
        if old_value == new_value:
            continue
        if key.rsplit(".", 1)[-1] in _SECRET_LEAVES:
            lines.append(f"{key} {_secret_label(old_value)} -> {_secret_label(new_value)}")
            continue
        lines.append(f"{key} {old_value} -> {new_value}")
    return "\n".join(lines)


def _secret_label(value: object) -> str:
    if value in (None,):
        return "inherit"
    if value == "":
        return "cleared"
    return "set"


def _flat(value: object, prefix: str = "") -> dict[str, object]:
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for key, item in value.items():
            out.update(_flat(item, f"{prefix}{key}."))
        return out
    return {prefix[:-1]: value}


def sync_standing_agents(
    conn: psycopg.Connection,
    *,
    names: tuple[str, ...] = ("Narrator", "CSP"),
    author: str = "athena",
    note: str = "week-dip tag + Narrator open gate",
    account_ids: list[str] | None = None,
) -> list[tuple[str, int]]:
    """Replace named agents with standing defaults on each active settings row.

    Stored agents override defaults at resolve time, so live books need a new
    settings_versions row. ZZ* accounts are skipped (they keep empty agents).
    """
    from athena_settings.prompts import default_agents

    standing = default_agents()
    wanted = [name for name in names if name in standing]
    if not wanted:
        return []
    if account_ids is None:
        rows = conn.execute(
            """
            SELECT DISTINCT account_id
            FROM settings_versions
            WHERE active
            ORDER BY account_id
            """
        ).fetchall()
        account_ids = [str(row[0]) for row in rows]
    written: list[tuple[str, int]] = []
    for account_id in account_ids:
        if account_id.upper().startswith("ZZ"):
            continue
        current = _payload_of(load_active(conn, account_id))
        agents = dict(current.agents)
        changed = False
        for name in wanted:
            fresh = standing[name]
            old = agents.get(name)
            if (
                old is not None
                and old.system == fresh.system
                and old.execution == fresh.execution
                and old.reasoning == fresh.reasoning
            ):
                continue
            agents[name] = fresh
            changed = True
        if not changed:
            continue
        payload = current.model_copy(update={"agents": agents})
        new_id, _changes = save_settings(conn, account_id, payload, author, note)
        written.append((account_id, new_id))
    return written


def load_active(conn: psycopg.Connection, account_id: str) -> SettingsVersion:
    row = conn.execute(
        """
        SELECT id, author, note, payload
        FROM settings_versions
        WHERE active AND account_id = %s
        """,
        (account_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"no active settings version for {account_id}")
    payload = _payload_from_db(row[3])
    return SettingsVersion(
        id=row[0],
        author=row[1],
        note=row[2],
        **payload.model_dump(),
    )
