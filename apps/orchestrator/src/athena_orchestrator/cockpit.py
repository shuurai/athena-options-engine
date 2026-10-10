"""Cockpit facts from stored rows and real probes. Empty series stay empty."""

from __future__ import annotations

import os
import socket
import time
from datetime import date, datetime, time as clock, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from athena_exec.halt import halt, resume
from athena_exec.ops import gateway_open, ops_action
from athena_exec.slots import HKT
from athena_settings import load_active
from athena_settings.model import SettingsPayload
from athena_settings.repository import save_settings
from athena_store.backup import prune_snapshots, write_snapshot
from athena_store.s3 import sync_snapshot
from athena_strategy.iv import iv_rank

from . import runtime
from .money import convert, ledger, marked, report_currency
from .version import __version__

DASH = "—"
_TAG_CLASS = {
    "NEWS_WASH": "amber",
    "HIGH_IV": "teal",
    "LOW_IV": "green",
    "PRICE_WEAK": "red",
    "VOLUME_SPIKE": "amber",
    "EARNINGS_NEAR": "amber",
}


def sleeve(conn, account: str, now: datetime | None = None) -> dict:
    """NLV, premium, cash reserve, and pending drafts for one account."""
    today = (now or datetime.now(HKT)).astimezone(HKT).date()
    nav_rows = conn.execute(
        """
        SELECT day, last_nav FROM nav_marks
        WHERE account_id = %s AND day >= %s
        ORDER BY day
        """,
        (account, today - timedelta(days=182)),
    ).fetchall()
    currency = report_currency(conn, account)
    base, _balances, rates = ledger(conn, account)
    source = base or "HKD"
    weekly = _weekly(nav_rows)
    latest = nav_rows[-1][1] if nav_rows else None
    change = _change(weekly)
    latest_shown = None if latest is None else convert(Decimal(latest), source, currency, source, rates)
    if latest is not None and latest_shown is None:
        change = f"no {currency} rate"
        chart_weeks = []
    else:
        moved_rows = []
        for day, amount in nav_rows:
            moved_rows.append((day, float(convert(Decimal(amount), source, currency, source, rates))))
        chart_weeks = _pad_weeks(_weekly(moved_rows), today)
    raw_flows = _flows(conn, account, today - timedelta(days=182))
    priced = _priced(raw_flows, source, currency, rates)
    if priced is None:
        premium_text, months, pnl = DASH, [], []
        premium_meta = f"no {currency} rate"
    else:
        premium, trades, months, pnl = _months(priced, today)
        premium_text = marked(premium, currency) if trades else DASH
        premium_meta = _premium_meta(trades, months)
    cash_text, cash_pct, cash_meta = _cash(conn, account, latest, currency, source, rates)
    pending, ttl = _pending(conn, account, now or datetime.now(HKT))
    return {
        "nav": marked(latest_shown, currency) if latest_shown is not None else DASH,
        "change": change,
        "account": account,
        "currency": currency,
        "premium": premium_text,
        "premium_meta": premium_meta,
        "cash": cash_text,
        "cash_pct": cash_pct,
        "cash_meta": cash_meta,
        "pending": pending,
        "ttl": ttl,
        "nlv": [point[1] for point in chart_weeks],
        "nlv_labels": _month_labels(chart_weeks),
        "months": months,
        "pnl": pnl,
    }


def schedule(now: datetime, athena_slots: str, mobile_slots: str) -> list[dict]:
    """Today's windows. Gaps between them are idle."""
    local = now.astimezone(HKT)
    windows = [(start, end, "Athena") for start, end in _parse_slots(athena_slots)]
    windows += [(start, end, "Mobile") for start, end in _parse_slots(mobile_slots)]
    windows.sort(key=lambda item: item[0])
    rows = []
    cursor = windows[0][0] if windows else None
    weekend = local.weekday() >= 5
    clock_now = local.timetz().replace(tzinfo=None)
    for start, end, owner in windows:
        if cursor is not None and start > cursor:
            rows.append(_slot_row(cursor, start, "Idle", clock_now, weekend))
        rows.append(_slot_row(start, end, owner, clock_now, weekend))
        cursor = end
    return rows


def watchlist(conn, account: str, symbols: list[str], min_samples: int, today: date | None = None) -> list[dict]:
    from athena_orchestrator.pulse import ib_move

    day = today or datetime.now(HKT).date()
    rows = []
    for symbol in symbols:
        phase = conn.execute(
            """
            SELECT phase, tags FROM symbol_phases
            WHERE account_id = %s AND symbol = %s
            """,
            (account, symbol),
        ).fetchone()
        price, change, tone = ib_move(conn, symbol)
        proxies = conn.execute(
            """
            SELECT iv_proxy FROM iv_marks
            WHERE symbol = %s
            ORDER BY day DESC
            LIMIT 26
            """,
            (symbol,),
        ).fetchall()
        samples = [Decimal(row[0]) for row in reversed(proxies)]
        rank = iv_rank(samples, samples[-1], min_samples) if samples else None
        positions = _positions(conn, account, symbol)
        plan = conn.execute(
            """
            SELECT expiry, put_strike, call_strike FROM option_plans
            WHERE symbol = %s
            """,
            (symbol,),
        ).fetchone()
        draft = conn.execute(
            """
            SELECT 1 FROM drafts
            WHERE account_id = %s AND symbol = %s AND status = 'pending'
            LIMIT 1
            """,
            (account, symbol),
        ).fetchone()
        rows.append(_watch_row(symbol, phase, price, change, tone, samples, rank, positions, plan, draft is not None, day))
    return rows


def health_report(conn) -> dict:
    """Five checks. A missing setup is not counted as healthy."""
    checks = [
        _orchestrator_check(),
        _itick_check(conn),
        _store_check(conn),
        _backup_check(),
        _telegram_check(conn),
    ]
    ok = sum(1 for item in checks if item["ok"])
    state = "ok" if ok == 5 else "warn" if ok >= 3 else "down"
    return {"ok": ok, "total": 5, "state": state, "checks": checks}


def run_ops(conn, account: str, actor: str, allowlist: set[str], command: str) -> str:
    if actor not in allowlist:
        raise PermissionError("actor is not on the allowlist")
    text = command.strip()
    if text == "health":
        report = health_report(conn)
        lines = [f"{item['name']}: {item['text']}" for item in report["checks"]]
        return f"{report['ok']}/{report['total']}\n" + "\n".join(lines)
    if text == "backup":
        return _backup_now(conn, account)
    if text == "halt":
        halt(conn, account, "console halt")
        return "Trading halted on this account"
    if text == "resume":
        resume(conn, account)
        return "Trading resumed on this account"
    if text in {"athena restart", "gateway restart"}:
        return ops_action(actor, allowlist, text)
    raise ValueError("unknown ops command")


def slot_windows(athena_slots: str, mobile_slots: str) -> list[dict]:
    """Editable rows. The stored strings stay the engine format."""
    rows = [
        {"owner": "Athena", "start": start.strftime("%H:%M"), "end": end.strftime("%H:%M")}
        for start, end in _parse_slots(athena_slots)
    ]
    rows += [
        {"owner": "Mobile", "start": start.strftime("%H:%M"), "end": end.strftime("%H:%M")}
        for start, end in _parse_slots(mobile_slots)
    ]
    rows.sort(key=lambda row: row["start"])
    return rows


def save_slots(conn, account: str, actor: str, athena_slots: str, mobile_slots: str) -> str:
    if not _parse_slots(athena_slots) or not _parse_slots(mobile_slots):
        raise ValueError("each slot list needs a start-end window")
    overlap = _overlap(athena_slots, mobile_slots)
    if overlap:
        raise ValueError(overlap)
    current = load_active(conn, account)
    payload = SettingsPayload.model_validate(current.model_dump())
    payload.ib.athena_slots = " ".join(athena_slots.split())
    payload.ib.mobile_slots = " ".join(mobile_slots.split())
    version, _changes = save_settings(conn, account, payload, f"console:{actor}", "slot windows")
    return f"Settings version {version}. Privilege stays {payload.privilege}."


def _backup_now(conn, account: str) -> str:
    directory = Path(os.environ.get("ATHENA_LOCAL_BACKUP_DIR", "var/backups"))
    path = write_snapshot(conn, directory)
    days = load_active(conn, account).retain_local_days if account else 14
    removed = prune_snapshots(directory, days)
    try:
        key = sync_snapshot(path)
    except Exception as exc:
        return f"Backup {path.name}. Pruned {removed}. Sync failed: {exc}"
    if key:
        return f"Backup {path.name}. Pruned {removed}. Sync {key}."
    return f"Backup {path.name}. Pruned {removed}. Sync off."


def _flows(conn, account: str, since: date) -> list[tuple[datetime, Decimal]]:
    rows = conn.execute(
        """
        SELECT f.created_at, f.price, f.contracts, f.symbol, o.action, o.multiplier
        FROM fills f
        JOIN LATERAL (
            SELECT action, multiplier FROM orders
            WHERE draft_id = f.draft_id AND account_id = f.account_id
            ORDER BY id
            LIMIT 1
        ) o ON true
        WHERE f.account_id = %s AND f.draft_id IS NOT NULL AND f.created_at >= %s
        """,
        (account, datetime.combine(since, clock.min, HKT)),
    ).fetchall()
    flows = []
    for created, price, contracts, symbol, action, multiplier in rows:
        if action not in {"SELL", "BUY"}:
            continue
        signed = Decimal(price) * Decimal(contracts) * Decimal(multiplier or "100")
        if action == "BUY":
            signed = -signed
        flows.append((created, signed, _flow_currency(symbol)))
    return flows


def _flow_currency(symbol: str) -> str:
    from athena_exec.broker import stock_venue

    return stock_venue(symbol)[1]


def _priced(flows, source: str, currency: str, rates: dict) -> list | None:
    """Premium in the global currency. One missing rate blanks the total."""
    if not flows:
        return []
    priced = []
    for created, signed, native in flows:
        moved = convert(signed, native, currency, source, rates)
        if moved is None:
            return None
        priced.append((created, moved))
    return priced


def _months(flows: list[tuple[datetime, Decimal]], today: date) -> tuple[Decimal | None, int, list[dict], list[float]]:
    cursor = date(today.year, today.month, 1)
    for _ in range(5):
        cursor = (cursor - timedelta(days=1)).replace(day=1)
    months = []
    while cursor <= date(today.year, today.month, 1):
        months.append(cursor)
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    built = []
    pnl = []
    premium_total = Decimal(0)
    trades = len(flows)
    for month in months:
        credit = Decimal(0)
        net = Decimal(0)
        count = 0
        for created, signed in flows:
            local = created.astimezone(HKT)
            if local.year == month.year and local.month == month.month:
                count += 1
                net += signed
                if signed > 0:
                    credit += signed
        built.append(
            {
                "name": month.strftime("%b"),
                "premium": float(credit),
                "trades": count,
                "win": None if count == 0 else net > 0,
                "net": float(net),
            }
        )
        pnl.append(float(net))
        premium_total += credit
    if trades == 0:
        return None, 0, built, pnl
    return premium_total, trades, built, pnl


def _premium_meta(trades: int, months: list[dict]) -> str:
    if not trades:
        return "no fills in six months"
    scored = [item for item in months if item["win"] is not None]
    wins = sum(1 for item in scored if item["win"])
    return f"{trades} trades\n{wins} of {len(scored)} months up"


def _cash(
    conn,
    account: str,
    nav: Decimal | None,
    currency: str,
    source: str,
    rates: dict,
) -> tuple[str, str, str]:
    """Total cash in the display currency, cash over NLV, and maintenance margin over NLV."""
    row = conn.execute(
        """
        SELECT cash, margin FROM account_marks
        WHERE account_id = %s AND cash IS NOT NULL
        ORDER BY day DESC
        LIMIT 1
        """,
        (account,),
    ).fetchone()
    if row is None:
        return DASH, "", ""
    cash_raw = Decimal(row[0])
    shown = convert(cash_raw, source, currency, source, rates)
    cash_text = marked(shown, currency) if shown is not None else DASH
    pct = ""
    margin = ""
    base = None if nav is None else Decimal(nav)
    if base is not None and base != 0:
        pct = f"{cash_raw / base * Decimal(100):.1f}%"
        if row[1] is not None:
            margin = f"margin {Decimal(row[1]) / base * Decimal(100):.1f}%"
    return cash_text, pct, margin


def _pending(conn, account: str, now: datetime) -> tuple[int, str]:
    rows = conn.execute(
        """
        SELECT created_at FROM drafts
        WHERE account_id = %s AND status = 'pending'
        """,
        (account,),
    ).fetchall()
    if not rows:
        return 0, ""
    ttl = load_active(conn, account).notify.draft_ttl_minutes
    oldest = min(row[0] for row in rows)
    elapsed = (now.astimezone(oldest.tzinfo or HKT) - oldest).total_seconds() / 60
    left = max(0, int(ttl - elapsed))
    return len(rows), f"{left}m"


def _positions(conn, account: str, symbol: str) -> list[tuple]:
    found = conn.execute(
        """
        SELECT quantity, sec_type, expiry, contract_right, strike, snapshot_at
        FROM positions
        WHERE account_id = %s AND symbol = %s
        ORDER BY snapshot_at DESC
        """,
        (account, symbol),
    ).fetchall()
    kept: dict[tuple, tuple] = {}
    for row in found:
        key = (row[1], row[3], row[4])
        if key not in kept:
            kept[key] = row
    return list(kept.values())


def _watch_row(symbol, phase, price, change, tone, samples, rank, positions, plan, has_draft, today: date) -> dict:
    phase_name = "" if phase is None else phase[0]
    tags = [] if phase is None else [part for part in (phase[1] or "").split(",") if part]
    side = []
    strikes = []
    expiries = []
    long_shares = False
    short_call = False
    for quantity, sec_type, expiry, right, strike, _stamp in positions:
        qty = Decimal(quantity)
        if qty == 0:
            continue
        if sec_type == "STK" and qty > 0:
            long_shares = True
            side.append({"t": f"{int(qty)} SH", "cls": "green"})
        elif sec_type == "OPT" and qty < 0 and right == "P":
            side.append({"t": "SHORT PUT", "cls": "amber"})
        elif sec_type == "OPT" and qty < 0 and right == "C":
            short_call = True
            side.append({"t": "SHORT CALL", "cls": "teal"})
        if strike is not None and sec_type == "OPT":
            strikes.append(_whole(strike))
        if expiry is not None:
            expiries.append(expiry)
    if long_shares and short_call:
        side.append({"t": "CC", "cls": "teal"})
    if not side:
        side = [{"t": "FLAT", "cls": ""}]
    if not strikes and plan is not None:
        strikes = [_whole(plan[1]), _whole(plan[2])]
        expiries.append(plan[0])
    dte = ""
    future = [item for item in expiries if item is not None]
    if future:
        dte = str(min((item - today).days for item in future))
    status = []
    if has_draft:
        status.append({"t": "DRAFT", "cls": "blue"})
    elif long_shares and short_call:
        status.append({"t": "COVERED", "cls": "green"})
    return {
        "symbol": symbol,
        "price": price,
        "change": change,
        "tone": tone,
        "iv": [float(item) for item in samples],
        "rank": DASH if rank is None else str(rank),
        "phase": phase_name or DASH,
        "phase_class": "blue" if phase_name.startswith("P2") else "",
        "tags": [{"t": tag, "cls": _TAG_CLASS.get(tag, "")} for tag in tags],
        "side": side,
        "strikes": strikes,
        "dte": dte,
        "status": status,
    }


def _weekly(rows: list[tuple]) -> list[tuple[date, float]]:
    buckets: dict[tuple[int, int], tuple[date, float]] = {}
    for day, nav in rows:
        buckets[day.isocalendar()[:2]] = (day, float(nav))
    return [buckets[key] for key in sorted(buckets)]


def _pad_weeks(weekly: list[tuple[date, float]], today: date) -> list[tuple[date, float]]:
    """26 weeks ending this week. A week with no mark is zero."""
    known = {day.isocalendar()[:2]: value for day, value in weekly}
    start = today - timedelta(days=today.weekday())
    points = []
    for offset in range(25, -1, -1):
        day = start - timedelta(weeks=offset)
        points.append((day, known.get(day.isocalendar()[:2], 0.0)))
    return points


def _change(weekly: list[tuple[date, float]]) -> str:
    if len(weekly) < 2 or weekly[0][1] == 0:
        return "no nav mark" if not weekly else "one mark"
    first = weekly[0][1]
    last = weekly[-1][1]
    pct = (last - first) / abs(first) * 100
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.1f}% / stored"


def _month_labels(weekly: list[tuple[date, float]]) -> list[str]:
    seen = []
    for day, _nav in weekly:
        label = day.strftime("%b")
        if label not in seen:
            seen.append(label)
    return seen


def _slot_row(start: clock, end: clock, owner: str, now: clock, weekend: bool) -> dict:
    label = {"Athena": "athena", "Mobile": "mobile", "Idle": "idle"}.get(owner, owner.lower())
    if weekend:
        status, tag = "idle", ""
    elif end <= now:
        status, tag = ("idle", "") if owner == "Idle" else ("done", "green")
    elif start <= now < end:
        if owner == "Mobile":
            status, tag = "reserved", "amber"
        elif owner == "Idle":
            status, tag = "idle", ""
        else:
            status, tag = "active now", "blue"
    else:
        status, tag = ("idle", "") if owner == "Idle" else ("later", "")
    return {
        "window": f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}",
        "owner": label,
        "status": status,
        "tag": tag,
    }


def _overlap(athena_slots: str, mobile_slots: str) -> str:
    windows = [(start, end, "Athena") for start, end in _parse_slots(athena_slots)]
    windows += [(start, end, "Mobile") for start, end in _parse_slots(mobile_slots)]
    windows.sort()
    for index in range(1, len(windows)):
        start, _end, owner = windows[index]
        prev_end = windows[index - 1][1]
        prev_owner = windows[index - 1][2]
        if start < prev_end:
            return f"{prev_owner} and {owner} overlap. Windows may meet, not cross."
    return ""


def _parse_slots(spec: str) -> list[tuple[clock, clock]]:
    text = spec.replace("HKT", " ").strip()
    found = []
    for part in text.split(","):
        piece = part.strip()
        if "-" not in piece:
            continue
        start_s, end_s = piece.split("-", 1)
        try:
            start = clock.fromisoformat(start_s.strip())
            end = clock.fromisoformat(end_s.strip())
        except ValueError:
            continue
        if start < end:
            found.append((start, end))
    return found


def _orchestrator_check() -> dict:
    delta = datetime.now(ZoneInfo("UTC")) - runtime.started_at
    hours, remainder = divmod(delta.seconds, 3600)
    minutes = remainder // 60
    uptime = f"{delta.days}D {hours:02d}H" if delta.days else f"{hours}H {minutes:02d}M"
    loop = runtime.cycle_view()["loop"]
    return {
        "name": "Orchestrator",
        "ok": True,
        "text": f"running · cycle #{runtime.cycle} · up {uptime} · loop {loop}s",
    }


def _itick_check(conn) -> dict:
    from athena_settings.resolve import itick_key, live_settings

    settings = live_settings(conn)
    ready = bool(itick_key(settings)) if settings is not None else bool(os.environ.get("ITICK_API_KEY", "").strip())
    return {"name": "iTick", "ok": ready, "text": "configured" if ready else "not configured"}


def _store_check(conn) -> dict:
    started = time.perf_counter()
    conn.execute("SELECT 1").fetchone()
    lag = int((time.perf_counter() - started) * 1000)
    return {"name": "Store", "ok": True, "text": f"Postgres · lag {lag}ms"}


def _backup_check() -> dict:
    directory = Path(os.environ.get("ATHENA_LOCAL_BACKUP_DIR", "var/backups"))
    files = sorted(directory.glob("athena-*.json"), key=lambda path: path.stat().st_mtime) if directory.is_dir() else []
    sync = os.environ.get("ATHENA_S3_SYNC_ENABLED", "").lower() in {"1", "true", "yes"}
    sync_text = "sync on" if sync else "sync off"
    if not files:
        return {"name": "MinIO", "ok": False, "text": f"no backup yet · {sync_text}"}
    stamp = datetime.fromtimestamp(files[-1].stat().st_mtime, HKT).strftime("%H:%M")
    return {"name": "MinIO", "ok": True, "text": f"last backup {stamp} · {sync_text}"}


def _telegram_check(conn) -> dict:
    from athena_bots.telegram import polling
    from athena_settings.resolve import allowlist_ids, bot_token, live_settings

    settings = live_settings(conn)
    if settings is None:
        token = bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip())
        allow = bool(os.environ.get("TELEGRAM_ALLOWLIST_USER_IDS", "").strip())
    else:
        token = bool(bot_token(settings))
        allow = bool(allowlist_ids(settings))
    if token and polling:
        return {"name": "Telegram", "ok": True, "text": "polling"}
    if token and allow:
        return {"name": "Telegram", "ok": False, "text": "token set · polling off"}
    return {"name": "Telegram", "ok": False, "text": "not configured"}


def _money(value) -> str:
    if value is None:
        return DASH
    amount = Decimal(value)
    return f"{amount:,.2f}"


def _whole(value) -> str:
    amount = Decimal(value)
    if amount == amount.to_integral():
        return str(int(amount))
    return f"{amount:.2f}"


def screen_bar() -> str:
    width = int(runtime.cycle_view()["progress"] * 100)
    return f'<div class="screen-bar"><i data-cycle="bar" style="width:{width}%"></i></div>'


def footer() -> str:
    view = runtime.cycle_view()
    host = view["host"].replace("<", "")
    line = f"cycle #{view['cycle']}"
    remaining = int(view["remaining"])
    if view["state"] == "running":
        clock = ""
        clock_class = "cycle-clock busy"
    else:
        clock = f"{remaining // 60:02d}:{remaining % 60:02d}"
        clock_class = "cycle-clock"
    return (
        f'<span class="chip" data-cycle="version">v{__version__}</span>'
        f'<span class="chip" data-cycle="host"><span class="dot"></span>host {host}</span>'
        f'<div class="cycle-row"><span class="cycle-mark" data-cycle="mark"></span>'
        f'<span data-cycle="line">{line}</span></div>'
        f'<div class="{clock_class}" data-cycle="clock">{clock}</div>'
        f'<div data-cycle="up">uptime {view["uptime"]}</div>'
    )
