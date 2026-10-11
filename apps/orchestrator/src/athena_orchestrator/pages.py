"""Console pages that follow the design mockups. The cockpit stays in console.py."""

from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import secrets
import threading
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from html import escape
from pathlib import Path
from urllib.parse import quote

from athena_bots.public_url import control_page_url
from athena_exec.accounts import active_accounts, trading_mode
from athena_exec.slots import HKT
from . import runtime
from .cockpit import DASH, sleeve
from .money import CURRENCIES, cash_rows, convert, ledger, marked, report_currency, show
from .control import _amend_fields, _amend_from_form
from athena_settings import load_active
from athena_settings.model import (
    CREDENTIAL_ENV,
    CREDENTIAL_FIELD,
    MODELS,
    AccountEntry,
    AccountSwitches,
    RiskLimits,
    SettingsPayload,
    TacticFlags,
    normalize_provider,
)
from athena_settings.prompts import AGENT_ORDER, AGENT_ROLES, resolved_agents
from athena_settings.repository import save_settings
from athena_settings.resolve import (
    allowlist_ids,
    bot_token,
    bot_username,
    control_flag,
    deepseek_token,
    itick_key,
    scoring_model,
)
from athena_strategy.classifiers import CLASSIFIER_BADGE, CLASSIFIER_MEANING, CLASSIFIERS, clean_classifiers
from athena_strategy.drafts import AmendError, amend_view, apply_amend, decide
from athena_strategy.iv import PREMIUM_METHOD, iv_rank
from athena_strategy.tags import tag_color
from athena_strategy.news import dismiss_event, ingest_url
from athena_strategy.pool import clean_symbol
from athena_strategy.types import NEWS_TYPE_BADGE, NEWS_TYPE_MEANING
from athena_strategy.phases import calendar_offset

PROFILE_LIMITS = (
    "max_symbol_pct_nav",
    "cash_reserve_pct",
    "daily_loss_halt_pct",
    "max_contracts_per_symbol",
    "min_rv_pctl_new_short",
    "min_dte_new",
    "max_position_per_symbol",
    "max_orders_per_day",
    "max_short_opt_margin_pct",
    "earnings_blackout_days",
)
FORM_LIMITS = (
    "max_symbol", "cash", "halt", "contracts", "min_rv", "min_dte",
    "position", "max_orders", "margin", "blackout",
)
PROFILES = {
    "defensive": (10, 40, 3, 1, 60, 21, 0, 10, 50, 7),
    "standard": (20, 30, 5, 3, 50, 7, 0, 10, 50, 7),
    "income": (25, 25, 5, 2, 45, 14, 0, 10, 50, 7),
    "aggressive": (35, 15, 8, 4, 30, 7, 0, 10, 50, 7),
    "paper": (25, 20, 8, 5, 40, 21, 20000, 6, 35, 7),
}
PREVIEW_FIELDS = ("rv_high_pctl", "rv_low_pctl", "rv_window", "ivr_min_samples")
PROFILE_COPY = {
    "defensive": "Tighter name size, more cash, one contract.",
    "standard": "The book Athena starts with.",
    "income": "More premium, a shorter DTE floor.",
    "aggressive": "Larger name size and more contracts.",
    "paper": "Acceptance limits. Approve still applies.",
}
PHASES = (
    ("P1", "Warning", "Watchlist and IV"),
    ("P2", "Panic", "Strangle bias"),
    ("P3", "Event", "Hold"),
    ("P4", "Repair", "Close or rotate"),
    ("P5", "Euphoria", "Covered-call bias"),
)
HISTORY_FILTERS = (
    ("all", "All"),
    ("csp", "CSP"),
    ("cc", "CC"),
    ("strangle", "Strangle"),
    ("roll", "Roll"),
    ("rejected", "Rejected"),
)
PRIVILEGES = {
    "all_orders": "Every open, roll, and close needs Approve. This is the active privilege.",
    "opens_only": "Opens would need Approve. Rolls and closes could run after the risk checks. This choice stays off. Privilege remains all_orders.",
    "auto_within_risk": "Orders could go out when the risk checks pass. This choice stays off. Privilege remains all_orders.",
    "paper_only": "Paper already sends a real order on the paper account. This is not a separate switch. Privilege remains all_orders.",
}


def build(conn, path: str, query: dict) -> dict | None:
    account = _account()
    pages = {
        "/simulation": _simulation,
        "/drafts": _drafts,
        "/phase": _phase,
        "/orchestration": _strategy,
        "/strategy": _strategy,
        "/volatility": _volatility_page,
        "/audit": _audit,
        "/news": _news,
        "/universe": _universe,
        "/history": _history,
        "/history.csv": _history_csv,
        "/positions": _positions_page,
        "/book": _book_page,
        "/accounts": _accounts,
        "/risk": _risk,
        "/settings": _settings,
        "/skill": _skill,
    }
    page = pages.get(path)
    if page is None:
        return None
    return page(conn, account, query)


def submit(conn, path: str, form: dict, actor: str, allowlist: set[str]) -> dict | None:
    account = _account()
    if path == "/drafts":
        return _post_draft(conn, account, form, actor, allowlist)
    if path == "/news":
        return _post_news(conn, account, form)
    if path in {"/orchestration", "/strategy"}:
        return _post_strategy(conn, account, form, actor)
    if path == "/universe":
        return _post_universe(conn, account, form, actor)
    if path == "/risk":
        return _post_risk(conn, account, form, actor)
    if path == "/settings":
        return _post_settings(conn, account, form, actor)
    if path == "/accounts":
        return _post_accounts(conn, account, form, actor)
    return None


def _drafts(conn, account: str, query: dict, note: str = "") -> dict:
    pending = []
    recent = []
    if account:
        pending = conn.execute(
            """
            SELECT id, symbol, tactic, phase, reason, created_at, contracts,
                   expiry, put_strike, call_strike, put_limit, call_limit, limit_price, multiplier,
                   simulated
            FROM drafts
            WHERE account_id = %s AND status = 'pending'
            ORDER BY created_at
            """,
            (account,),
        ).fetchall()
        recent = conn.execute(
            """
            SELECT a.created_at, a.draft_id, d.symbol, d.tactic, a.decision, a.actor, a.reason
            FROM approvals a
            JOIN drafts d ON d.id = a.draft_id
            WHERE a.account_id = %s
            ORDER BY a.created_at DESC
            LIMIT 40
            """,
            (account,),
        ).fetchall()
    cards = "".join(_draft_card(conn, account, row, index == 0) for index, row in enumerate(pending))
    if not cards:
        cards = f'<div class="card draft-empty"><div class="sim-idle">{_SIM_IDLE}<p>No draft is waiting.</p></div></div>'
    rows = []
    for created, draft_id, symbol, tactic, decision, actor, reason in recent:
        label = {"approve": "APPROVED", "reject": "REJECTED"}.get(decision, decision.upper())
        tag = "green" if decision == "approve" else "red"
        short = tactic[5:] if tactic.startswith("OPEN_") else tactic
        rows.append(
            "<tr>"
            f"<td class=\"mono\">{escape(_stamp(created))}</td>"
            f"<td class=\"mono\">{escape(draft_id)} · {escape(symbol)} {escape(short)}</td>"
            f"<td><span class=\"tag {tag}\">{escape(label)}</span></td>"
            f"<td>{escape(actor)}</td>"
            "</tr>"
        )
    body_rows = "".join(rows) or "<tr><td colspan=\"4\">No decision is stored yet.</td></tr>"
    note_html = f'<p class="note">{escape(note)}</p>' if note else ""
    banner = _draft_banner(conn, account)
    body = (
        f"{note_html}{banner}"
        '<div class="draft-cols">'
        f'<div class="draft-stack">{cards}</div>'
        '<div class="card fill">'
        '<h2>Recent decisions <a class="more" href="/history">full history →</a></h2>'
        '<div class="scroll-y"><table class="data"><thead><tr>'
        "<th>When</th><th>Draft</th><th>Action Status</th><th>By</th></tr></thead>"
        f"<tbody>{body_rows}</tbody></table></div></div></div>"
    )
    chips = [("info", f"{len(pending)} AWAITING")]
    if account:
        chips.append(("", f"TTL {load_active(conn, account).notify.draft_ttl_minutes}M"))
    return _html(
        "Draft queue",
        "Human gate. Athena proposes; you Approve, Amend, or Reject. Same cards push to Telegram when connected.",
        body,
        chips,
    )


def _draft_card(conn, account: str, row, hot: bool) -> str:
    draft_id, symbol, tactic, phase, reason, created, contracts, expiry, put_strike, call_strike, put_limit, call_limit, limit_price, multiplier, simulated = row
    settings = load_active(conn, account)
    left = _minutes_left(created, settings.notify.draft_ttl_minutes)
    tags = _symbol_tags(conn, account, symbol)
    badges = _tactic_tag(tactic)
    if simulated:
        badges += '<span class="tag amber">SIM</span>'
    if tactic in {"ROLL", "CLOSE"}:
        badges += '<span class="tag green">MGMT</span>'
    badges += _phase_badge(phase)
    badges += "".join(_driver_tag(item) for item in tags)
    link = _live_link(conn, draft_id)
    link_html = f'<a class="btn primary" href="{escape(link, quote=True)}">Open control link</a>' if link else ""
    view = amend_view(conn, draft_id)
    amend = _amend_fields(view) if view else ""
    klass = "card draft-hot" if hot else "card"
    hidden = (
        f'<div class="draft-amend" hidden><form method="post" action="/drafts">{amend}'
        f'<input type="hidden" name="draft_id" value="{escape(draft_id, quote=True)}">'
        '<input type="hidden" name="decision" value="amend">'
        '<button class="btn ghost" type="submit">Save amendment</button></form></div>'
        f'<div class="draft-reject" hidden><form method="post" action="/drafts">'
        f'<input type="hidden" name="draft_id" value="{escape(draft_id, quote=True)}">'
        '<input type="hidden" name="decision" value="reject">'
        '<label>Reject reason<input name="reason" required placeholder="Why this draft stops"></label>'
        '<button class="btn danger" type="submit">Reject</button></form></div>'
    )
    return (
        f'<div class="{klass}">'
        f'<div class="badge-row" style="margin-bottom:10px">{badges}'
        f'<span class="mono draft-id">{escape(draft_id)} · expires {left}m</span></div>'
        f'<div class="draft-title">{_draft_title(symbol, tactic, put_strike, call_strike, expiry)}</div>'
        f'<div class="draft-sub">{_draft_sub(conn, symbol, contracts, expiry, put_limit, call_limit, limit_price, settings)}</div>'
        f'<div class="field"><div class="k">Reason</div><div class="v">{escape(reason) or "—"}</div></div>'
        f'<div class="field"><div class="k">Risk</div><div class="v">{_draft_risk(conn, account, symbol, int(contracts), expiry, tactic, settings, multiplier or "100")}</div></div>'
        '<div class="row-actions" style="margin-top:14px">'
        f'<form method="post" action="/drafts" data-confirm="Approve {escape(symbol, quote=True)} {escape(tactic, quote=True)}?">'
        f'<input type="hidden" name="draft_id" value="{escape(draft_id, quote=True)}">'
        '<input type="hidden" name="decision" value="approve">'
        '<button class="btn ok" type="submit">Approve</button></form>'
        f"{link_html}"
        '<button class="btn ghost" type="button" data-cmd="draft-amend">Amend</button>'
        '<button class="btn danger" type="button" data-cmd="draft-reject">Reject</button>'
        "</div>"
        f"{hidden}</div>"
    )


def _driver_tag(name: str) -> str:
    color = tag_color(name)
    klass = f"tag {color}".strip()
    return f'<span class="{klass}">{escape(name)}</span>'


def _draft_title(symbol: str, tactic: str, put_strike, call_strike, expiry) -> str:
    parts = [escape(symbol)]
    put = _strike_text(put_strike)
    call = _strike_text(call_strike)
    if tactic == "OPEN_STRANGLE" and put and call:
        parts.append(f"sell {escape(put)}P / {escape(call)}C")
    elif tactic == "OPEN_CSP" and put:
        parts.append(f"sell {escape(put)}P")
    elif tactic == "OPEN_CC" and call:
        parts.append(f"sell {escape(call)}C")
    elif tactic == "ROLL":
        parts.append("roll")
    elif tactic == "CLOSE":
        parts.append("close")
    else:
        parts.append(escape(tactic))
    if isinstance(expiry, date):
        parts.append(escape(f"{expiry.day} {expiry.strftime('%b %Y')}"))
    return " · ".join(parts)


def _draft_sub(conn, symbol: str, contracts, expiry, put_limit, call_limit, limit_price, settings) -> str:
    parts = []
    spot, _change, _tone = _closes(conn, symbol)
    if spot != DASH:
        parts.append(f"Spot {escape(spot)}")
    rank = _rank_text(conn, symbol, settings.strategy.ivr_min_samples)
    if rank != DASH:
        parts.append(f"IVR {escape(rank)}")
    if isinstance(expiry, date):
        parts.append(f"DTE {(expiry - datetime.now(HKT).date()).days}")
    parts.append(f"qty {int(contracts)}")
    credit = _draft_limit(put_limit, call_limit, limit_price)
    if credit:
        parts.append(f'limit <b class="mono">{escape(credit)}</b>')
    return " · ".join(parts)


def _draft_limit(put_limit, call_limit, limit_price) -> str:
    if put_limit is not None and call_limit is not None:
        return f"${Decimal(put_limit) + Decimal(call_limit):.2f}"
    for value in (put_limit, call_limit, limit_price):
        if value is not None:
            return f"${Decimal(value):.2f}"
    return ""


def _draft_risk(conn, account: str, symbol: str, contracts: int, expiry, tactic: str, settings, multiplier: str = "100") -> str:
    opening = tactic.startswith("OPEN_")
    limits = settings.risk
    badges = []
    if opening:
        nav = conn.execute(
            "SELECT last_nav FROM nav_marks WHERE account_id = %s ORDER BY day DESC LIMIT 1",
            (account,),
        ).fetchone()
        mark = conn.execute(
            "SELECT cash FROM account_marks WHERE account_id = %s ORDER BY day DESC LIMIT 1",
            (account,),
        ).fetchone()
        nav_value = None if nav is None or nav[0] is None else Decimal(nav[0])
        cash = None if mark is None or mark[0] is None else Decimal(mark[0])
        if nav_value and nav_value > 0 and cash is not None and tactic in {"OPEN_CSP", "OPEN_STRANGLE"}:
            ok = cash / nav_value * Decimal(100) >= limits.cash_reserve_pct
            badges.append(("CASH OK" if ok else "CASH LOW", "green" if ok else "amber"))
        stored = conn.execute(
            "SELECT indicators FROM symbol_phases WHERE account_id = %s AND symbol = %s",
            (account, symbol),
        ).fetchone()
        rank = None if stored is None or not stored[0] else stored[0].get("rv_pctl")
        if rank is not None:
            ok = int(rank) >= limits.min_rv_pctl_new_short
            badges.append((f"RV ≥ {limits.min_rv_pctl_new_short}", "green" if ok else "amber"))
        if isinstance(expiry, date):
            dte = (expiry - datetime.now(HKT).date()).days
            ok = dte >= limits.min_dte_new
            badges.append(("DTE BAND" if ok else f"DTE {dte}", "green" if ok else "amber"))
        if nav_value and nav_value > 0:
            held = conn.execute(
                """
                SELECT quantity, avg_price FROM positions
                WHERE account_id = %s AND symbol = %s
                ORDER BY snapshot_at DESC, id DESC
                LIMIT 1
                """,
                (account, symbol),
            ).fetchone()
            if held is not None:
                share = abs(Decimal(held[0]) * Decimal(held[1])) / nav_value * Decimal(100)
                ok = share <= limits.max_symbol_pct_nav
                badges.append((f"NAME {share:.0f}% NAV", "green" if ok else "amber"))
    if contracts > limits.max_contracts_per_symbol:
        badges.append(("SIZE", "red"))
    elif opening and limits.max_position_per_symbol > 0:
        from athena_risk import allowed_open
        from athena_strategy.drafts import _exposure, last_tradeable_price

        price = last_tradeable_price(conn, symbol)
        if price is not None:
            mult = Decimal(multiplier or "100")
            if mult <= 0:
                mult = Decimal(100)
            allowed = allowed_open(limits, price, mult, _exposure(conn, account, symbol, price))
            if contracts > allowed:
                badges.append(("POSITION", "red"))
    if not badges:
        return "—"
    return "".join(f'<span class="tag {color}">{escape(text)}</span>' for text, color in badges)


def _draft_banner(conn, account: str) -> str:
    settings = load_active(conn, account) if account else None
    if settings is None:
        return ""
    parts = []
    offset = _nearest_offset(conn, list(settings.watchlist), settings)
    if offset is not None:
        parts.append(f"Nearest event T{offset:+d}")
    below = settings.news.wash_sentiment_below
    start = datetime.now(UTC) - timedelta(days=settings.news.wash_window_days)
    densest = ("", 0)
    for symbol in settings.watchlist:
        count = conn.execute(
            """
            SELECT count(*) FROM news_events
            WHERE symbol = %s AND sentiment < %s AND ts >= %s AND dismissed = false
            """,
            (symbol, below, start),
        ).fetchone()[0]
        if count > densest[1]:
            densest = (symbol, count)
    if densest[1]:
        parts.append(f"{densest[0]} {densest[1]} neg / {settings.news.wash_window_days}d")
    if not parts:
        return ""
    return f'<div class="banner">{escape(" · ".join(parts))}</div>'


def _strike_text(value) -> str:
    if value is None:
        return ""
    text = format(Decimal(value), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _phase(conn, account: str, query: dict) -> dict:
    settings = load_active(conn, account) if account else None
    symbols = list(settings.watchlist) if settings else []
    selected = (query.get("symbol") or [""])[0].upper()
    if selected not in symbols and symbols:
        selected = symbols[0]
    offset = _nearest_offset(conn, symbols, settings) if settings else None
    active = _phase_name(offset, settings.strategy) if settings else ""
    rail = []
    for name, title, detail in PHASES:
        mark = " active" if name == active else ""
        window = _phase_window(name, settings.strategy) if settings else ""
        rail.append(
            f'<div class="phase{mark}"><div class="n">{name}</div><div class="t">{title}</div>'
            f'<div class="d">{escape(window)}<br>{detail}</div></div>'
        )
    series, rank = _iv_series(conn, selected, settings.strategy.ivr_min_samples if settings else 20)
    rank_text = DASH if rank is None else str(rank)
    ivr_high = settings.strategy.ivr_high if settings else 80
    counts = _phase_counts(conn, account)
    bars = json.dumps([counts.get(name, 0) for name, _, _ in PHASES])
    table = _phase_table(conn, account, symbols, settings, selected)
    anchor = "No event is inside the cycle." if offset is None else f"Nearest stored offset {offset:+d} days."
    body = (
        f'<div class="card"><h2>Cycle phases</h2>'
        f'<p class="slot-note">{escape(anchor)}</p><div class="phase-rail">{"".join(rail)}</div></div>'
        '<div class="grid cols-2 phase-charts">'
        f'<div class="card"><h2>{escape(selected or "IV")} IV rank</h2>'
        f'<div class="chart-box chart-h-md"><div class="chart-plot ivr-spark" data-iv="{escape(json.dumps(series), quote=True)}" data-threshold="{ivr_high}"></div></div>'
        f'<div class="chart-legend"><span><i style="background:#22d3ee"></i>IVR</span>'
        f'<span><i style="background:#f7931a"></i>threshold {ivr_high}</span>'
        f'<span>Rank {escape(rank_text)}</span></div></div>'
        '<div class="card"><h2>Phase counts</h2>'
        f'<div class="chart-box chart-h-md"><div class="chart-plot" data-bars="{escape(bars, quote=True)}"></div></div>'
        '<div class="month-row"><span>P1</span><span>P2</span><span>P3</span><span>P4</span><span>P5</span></div></div>'
        "</div>"
        '<div class="card fill"><h2>Universe under NFR <a class="more" href="/news">news stream →</a></h2>'
        f'<div class="scroll-y">{table}</div></div>'
    )
    return _html("News Front-Running board", "One strategy. The active phase follows the nearest stored Fed or earnings date.", body, [])


def _news(conn, account: str, query: dict, note: str = "") -> dict:
    settings = load_active(conn, account) if account else None
    symbols = list(settings.watchlist) if settings else []
    below = settings.news.wash_sentiment_below if settings else -0.3
    window = settings.news.wash_window_days if settings else 3
    start = datetime.now(UTC) - timedelta(days=window)
    densest = ("", 0)
    for symbol in symbols:
        count = conn.execute(
            """
            SELECT count(*) FROM news_events
            WHERE symbol = %s AND sentiment < %s AND ts >= %s AND dismissed = false
            """,
            (symbol, below, start),
        ).fetchone()[0]
        if count >= densest[1]:
            densest = (symbol, count)
    average = conn.execute(
        "SELECT avg(sentiment) FROM news_events WHERE dismissed = false"
    ).fetchone()[0]
    wash_names = 0
    if account:
        wash_names = conn.execute(
            """
            SELECT count(*) FROM symbol_phases
            WHERE account_id = %s AND tags LIKE %s
            """,
            (account, "%NEWS_WASH%"),
        ).fetchone()[0]
    weeks = _weekly_negatives(conn, below)
    sources = _source_counts(conn)
    stream, more, nxt = _news_rows(conn, _offset(query))
    if _wants_rows(query):
        return _fragment(stream, more, nxt)
    avg_class, avg_text = _sentiment_parts(average)
    name = densest[0] or "—"
    dense_color = ' style="color:var(--ath-halt)"' if densest[1] else ""
    note_html = f'<p class="note">{escape(note)}</p>' if note else ""
    body = (
        f"{note_html}"
        '<div class="grid cols-3">'
        f'<div class="card stat"><div class="label">Most negative</div><div class="value"{dense_color}>{escape(name)}</div>'
        f'<div class="meta">{densest[1]} headlines in {window} days</div></div>'
        f'<div class="card stat"><div class="label">Average sentiment</div><div class="value {avg_class}">{escape(avg_text)}</div>'
        '<div class="meta">Stored scores, false alarms excluded</div></div>'
        f'<div class="card stat"><div class="label">Names in NEWS_WASH</div><div class="value">{wash_names}</div>'
        '<div class="meta">This account</div></div></div>'
        '<div class="grid cols-2">'
        '<div class="card"><h2>Negative headlines · 12 weeks</h2>'
        f'<div class="chart-box chart-h-md"><div class="chart-plot" data-bars="{escape(json.dumps(weeks), quote=True)}"></div></div></div>'
        '<div class="card"><h2>Headlines by source</h2>'
        f'<div class="chart-box chart-h-md"><div class="chart-plot" data-bars="{escape(json.dumps([item[1] for item in sources]), quote=True)}"></div></div>'
        f'<div class="month-row">{"".join(f"<span>{escape(item[0])}</span>" for item in sources)}</div></div></div>'
        '<div class="card fill"><h2>Stream'
        '<button class="more" data-cmd="manual-ingest" type="button">manual ingest →</button></h2>'
        f"{_feed('/news', nxt, more, _news_table(stream))}</div>"
    )
    chip = ("warn", f"{name} NEWS_WASH") if densest[1] else ("", "NO WASH")
    return _html(
        "News intake",
        "Headline density. Manual ingest pulls a page and DSH writes the event. A false alarm stays stored and drops out of the wash count.",
        body,
        [chip],
        _REFRESH,
    )


def _sort_th(label: str, key: str) -> str:
    return (
        f'<th><button class="sort" type="button" data-sort="{key}" aria-label="Sort by {escape(label, quote=True)}">'
        f"{escape(label)}"
        '<span class="sort-ico" aria-hidden="true"><svg viewBox="0 0 8 10" width="8" height="10">'
        '<path class="up" d="M4 .6 7.2 4.2H.8Z"/><path class="down" d="M.8 5.8h6.4L4 9.4Z"/>'
        "</svg></span></button></th>"
    )


def _year_band(conn, symbol: str) -> tuple[str, str, str]:
    from athena_marketdata.pricing import year_view

    return year_view(conn, symbol)


def _band_cell(band: str, high_pct: str, low_pct: str) -> str:
    if not high_pct:
        return f'<td class="mono band"><span class="px">{escape(band)}</span></td>'
    if not low_pct:
        gap = escape(high_pct)
    else:
        gap = (
            f'<span class="{_pct_tone(high_pct)}">{escape(high_pct)}</span>/'
            f'<span class="{_pct_tone(low_pct)}">{escape(low_pct)}</span>'
        )
    return (
        '<td class="mono band">'
        f'<span class="px">{escape(band)}</span> '
        f'<span class="px-chg">{gap}</span></td>'
    )


def _pct_tone(text: str) -> str:
    if text.startswith("+"):
        return "chg-up"
    if text.startswith("-"):
        return "chg-down"
    return ""


def _universe(conn, account: str, query: dict, note: str = "") -> dict:
    settings = load_active(conn, account) if account else None
    symbols = list(settings.watchlist) if settings else []
    offset = _offset(query)
    page, more, nxt = _window(symbols, offset)
    if (query.get("partial") or [""])[0] == "symbol":
        wanted = (query.get("symbol") or [""])[0].strip().upper()
        if settings is None or wanted not in {item.upper() for item in symbols}:
            return _pool_patch(False, "Symbol is not in the pool.", status=404)
        return _pool_patch(True, "", html=_pool_row(conn, account, settings, wanted), count=len(symbols))
    rows = [_pool_row(conn, account, settings, symbol) for symbol in page]
    body_rows = "".join(rows)
    if not body_rows and offset == 0:
        body_rows = '<tr><td colspan="15">The watchlist is empty.</td></tr>'
    if offset == 0:
        body_rows += '<tr id="pool-empty" hidden><td colspan="15">No symbols match.</td></tr>'
    if _wants_rows(query):
        return _fragment(body_rows, more, nxt)
    classifier_options = "".join(
        f'<option value="{escape(code, quote=True)}">{escape(code)}</option>'
        for code, _badge, _meaning in CLASSIFIERS
    )
    note_html = f'<p class="note">{escape(note)}</p>' if note else ""
    profile = settings.risk.profile if settings else "standard"
    tone = {
        "defensive": "tone-defensive",
        "standard": "tone-standard",
        "income": "tone-income",
        "aggressive": "tone-aggressive",
        "test": "tone-test",
        "paper": "tone-paper",
        "custom": "tone-custom",
    }.get(profile, "")
    ivr_floor = settings.risk.min_rv_pctl_new_short if settings else 50
    pending = 0
    if account:
        pending = conn.execute(
            "SELECT count(*) FROM drafts WHERE account_id = %s AND status = 'pending'",
            (account,),
        ).fetchone()[0]
    draft_value = f'<div class="value" style="color:var(--site-primary)">{pending}</div>' if pending else f'<div class="value">{pending}</div>'
    body = (
        f"{note_html}"
        '<div class="grid cols-3">'
        '<div class="card stat"><div class="label">Screening profile</div>'
        f'<div class="value {tone}">{escape(profile)}</div>'
        f'<div class="meta">IVR≥{ivr_floor}</div></div>'
        '<div class="card stat"><div class="label">Last scan</div>'
        f'<div class="value">{DASH}</div>'
        f'<div class="meta" data-pool-count>{len(symbols)} symbols</div></div>'
        '<div class="card stat"><div class="label">Drafts from scan</div>'
        f"{draft_value}"
        '<div class="meta"><a href="/drafts" style="color:var(--site-primary)">open queue →</a></div></div>'
        "</div>"
        '<div class="card fill"><h2>Symbol pool '
        '<button class="more" type="button" data-cmd="add-symbol">+ add symbol</button></h2>'
        '<div class="filters pool-filters">'
        '<input class="input" id="pool-keyword" type="search" placeholder="Keyword" autocomplete="off" spellcheck="false">'
        f'<select class="input" id="pool-classifier"><option value="">All classifiers</option>{classifier_options}</select>'
        "</div>"
        '<div class="scroll-y" data-feed="/universe" data-offset="'
        f'{nxt}" data-more="{"1" if more else "0"}"><table class="data" id="pool-table"><thead><tr>'
        f"{_sort_th('Symbol', 'symbol')}{_sort_th('Price', 'price')}<th>1D %</th><th>52w</th>{_sort_th('Mkt cap', 'cap')}"
        "<th>Classifiers</th><th>IVR</th><th>Phase</th>"
        "<th>Intent</th><th>Long</th><th>May buy</th><th>Enabled</th><th>CSP</th><th>CC</th><th>Strangle</th><th></th></tr></thead>"
        f"<tbody>{body_rows}</tbody></table></div></div>"
    )
    return _html(
        "Universe",
        "The pool Athena screens every day for NFR tactics. Only enabled symbols generate drafts. Toggles apply immediately (audited).",
        body,
        [("ok", f"{len(symbols)} IN POOL")],
        _REFRESH,
    )


def _history(conn, account: str, query: dict) -> dict:
    kind = _history_kind(query)
    everything = _history_rows(conn, account)
    rows = [item for item in everything if _history_visible(item, kind)]
    offset = _offset(query)
    page, more, nxt = _window(rows, offset)
    facts = sleeve(conn, account) if account else None
    premium_class, premium_text, premium_meta = _premium_stat(facts)
    best, worst = _month_stats(facts)
    currency = (facts or {}).get("currency") or "HKD"
    badge = f' <span class="tag">{escape(currency)}</span>'
    filters = "".join(
        f'<a class="{"on" if key == kind else ""}" href="/history?kind={key}">{label}</a>'
        for key, label in HISTORY_FILTERS
    )
    table_rows = [_history_tr(item) for item in page]
    body_rows = "".join(table_rows)
    if not body_rows and offset == 0:
        body_rows = '<tr><td colspan="6">No rows for this filter.</td></tr>'
    if _wants_rows(query):
        return _fragment(body_rows, more, nxt)
    body = (
        '<div class="grid cols-3">'
        f'<div class="card stat"><div class="label">Total premium{badge}</div>'
        f'<div class="value {premium_class}">{escape(premium_text)}</div>'
        f'<div class="meta">{escape(premium_meta)}</div></div>'
        f'<div class="card stat"><div class="label">Best month{badge}</div>'
        f'<div class="value">{escape(best[0])}</div><div class="meta">{escape(best[1])}</div></div>'
        f'<div class="card stat"><div class="label">Worst month{badge}</div>'
        f'<div class="value {worst[2]}">{escape(worst[0])}</div><div class="meta">{escape(worst[1])}</div></div>'
        "</div>"
        '<div class="card fill"><h2>Fills &amp; decisions '
        f'<a class="more" href="/history.csv?kind={quote(kind)}">export csv →</a></h2>'
        f'<div class="filters">{filters}</div>'
        f'{_feed(f"/history?kind={quote(kind)}", nxt, more, _history_table(body_rows))}</div></div>'
    )
    return _html(
        "History",
        "Six months of fills, rolls, rejects, and phase context. Every row was Approve-gated at the time.",
        body,
        _history_chips(everything),
        _REFRESH,
    )


def _history_table(rows: str) -> str:
    return (
        '<table class="data"><thead><tr>'
        "<th>Date</th><th>Symbol</th><th>Tactic</th><th>Phase</th><th>P&amp;L</th><th>Status</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>"
    )


def _kept_int(form: dict, key: str, default: int) -> int:
    raw = form.get(key)
    if not raw or str(raw[0]).strip() == "":
        return default
    return int(raw[0])


def _kept_float(form: dict, key: str, default: float) -> float:
    raw = form.get(key)
    if not raw or str(raw[0]).strip() == "":
        return float(default)
    return float(raw[0])


def _book_page(conn, account: str, query: dict) -> dict:
    """Read-only concentration. It does not choose a trade."""
    del query
    from athena_risk import panel_enabled
    from athena_strategy.indicators import compute, load_bars

    if not account:
        return _html("Book", "No account is configured.", '<div class="card"><p>No account is configured.</p></div>', [])
    settings = load_active(conn, account)
    load_bars(conn, settings.strategy.regime_index, settings.strategy.regime_lookback)
    if not panel_enabled():
        return _html("Book", "Charts are off.", '<div class="card"><p>Charts are off.</p></div>', [("info", "panel off")])
    points, book_point = _book_points(conn, settings, account, compute, load_bars)
    buckets = _book_buckets(conn, account, settings)
    assignment = _book_assignment(conn, account, settings)
    intents = {symbol.upper(): flags.intent for symbol, flags in settings.tactic_flags.items()}
    heatmap = _position_heatmap(_position_rows(conn, account), intents)
    body = (
        '<div class="book-stack">'
        '<div class="grid cols-3">'
        f'<div class="card"><h2>Universe vs book</h2>{_scatter(points, book_point)}</div>'
        f'<div class="card"><h2>Buckets</h2>{_bucket_chart(buckets, settings.risk.max_bucket_pct_nav)}</div>'
        f'<div class="card"><h2>Assignment now</h2>{_assignment_chart(assignment, settings.risk)}</div>'
        "</div>"
        f'<div class="card"><h2>Positions</h2>{heatmap}</div>'
        "</div>"
    )
    return _html(
        "Book",
        "Realized risk and signed premium.",
        body,
        [("info", "read only")],
    )


def _book_points(conn, settings, account: str, compute, load_bars) -> tuple[list, tuple | None]:
    rows = conn.execute(
        """
        SELECT DISTINCT ON (symbol, expiry, contract_right, strike)
            symbol, quantity, avg_price, expiry, contract_right, strike, multiplier
        FROM positions
        WHERE account_id = %s AND simulated = false AND quantity < 0 AND sec_type = 'OPT'
        ORDER BY symbol, expiry, contract_right, strike, snapshot_at DESC
        """,
        (account,),
    ).fetchall()
    today = datetime.now(HKT).date()
    points = []
    weighted_x = Decimal(0)
    weighted_y = Decimal(0)
    weight = Decimal(0)
    for symbol, quantity, avg_price, expiry, _right, strike, multiplier in rows:
        if avg_price is None or strike is None or expiry is None:
            continue
        dte = (expiry - today).days
        if dte <= 0 or Decimal(strike) <= 0:
            continue
        bars = load_bars(conn, symbol, settings.strategy.rv_window + 1)
        rv20 = compute(bars, settings.strategy).rv20
        if rv20 is None:
            continue
        capital = abs(Decimal(quantity)) * Decimal(strike) * Decimal(multiplier or 100)
        premium = abs(Decimal(quantity)) * Decimal(avg_price) * Decimal(multiplier or 100)
        yld = premium / capital * Decimal(365) / Decimal(dte) * Decimal(100)
        names = _symbol_classifiers(conn, account, symbol)
        points.append((float(rv20), float(yld), float(capital), _bucket_color(names[:1]), symbol))
        weighted_x += Decimal(rv20) * capital
        weighted_y += yld * capital
        weight += capital
    book_point = None
    if weight > 0:
        book_point = (float(weighted_x / weight), float(weighted_y / weight))
    return points, book_point


def _symbol_classifiers(conn, account: str, symbol: str) -> list[str]:
    row = conn.execute(
        "SELECT classifiers FROM symbol_phases WHERE account_id = %s AND symbol = %s",
        (account, symbol.upper()),
    ).fetchone()
    if row is None or not row[0]:
        return []
    return [part.strip() for part in str(row[0]).split(",") if part.strip()]


def _book_buckets(conn, account: str, settings) -> list[tuple[str, float, str]]:
    from athena_strategy.budget import _baseline

    book = _baseline(conn, settings, account)
    nav = book.nav or Decimal(0)
    found = []
    for name, amount in sorted(book.buckets.items()):
        pct = float(amount / nav * 100) if nav > 0 else 0.0
        count = _bucket_count(conn, account, name)
        rho = Decimal("0.7")
        bets = Decimal(count) / (1 + (Decimal(count) - 1) * rho) if count else Decimal(0)
        found.append((name, pct, f"N_eff {bets:.1f} at ρ 0.7"))
    return found


def _bucket_count(conn, account: str, name: str) -> int:
    rows = conn.execute(
        "SELECT classifiers FROM symbol_phases WHERE account_id = %s AND classifiers <> ''",
        (account,),
    ).fetchall()
    return sum(1 for (text,) in rows if name in [part.strip() for part in str(text).split(",")])


def _book_assignment(conn, account: str, settings) -> dict:
    from athena_strategy.budget import _baseline

    book = _baseline(conn, settings, account)
    nav = book.nav or Decimal(0)
    cash = book.cash if book.cash is not None else Decimal(0)
    assign = book.assignment
    return {
        "assign_pct": float(assign / nav * 100) if nav > 0 else 0.0,
        "cash_after_pct": float((cash - assign) / nav * 100) if nav > 0 else 0.0,
    }


def _bucket_color(names: list[str]) -> str:
    palette = {
        "AI_NARRATIVE": "#6b96f2",
        "SEMIS": "#22d3ee",
        "MEGA_CAP": "#a78bfa",
        "HIGH_BETA": "#f7931a",
        "DEFENSIVE": "#16c784",
        "MAG7": "#f472b6",
    }
    if not names:
        return "#8b97a8"
    return palette.get(names[0], "#8b97a8")


def _scatter(points: list, book_point: tuple | None) -> str:
    width, height, pad = 640, 220, 28
    xs = [0.0] + [point[0] for point in points]
    ys = [0.0] + [point[1] for point in points]
    if book_point:
        xs.append(book_point[0])
        ys.append(book_point[1])
    max_x = max(xs) or 1
    max_y = max(ys) or 1

    def px(value: float) -> float:
        return pad + (value / max_x) * (width - 2 * pad)

    def py(value: float) -> float:
        return height - pad - (value / max_y) * (height - 2 * pad)

    parts = [
        f'<line x1="{px(0)}" y1="{py(0)}" x2="{px(max_x)}" y2="{py(0)}" stroke="#3a4350"/>',
        f'<line x1="{px(0)}" y1="{py(0)}" x2="{px(0)}" y2="{py(max_y)}" stroke="#3a4350"/>',
        f'<text x="{pad}" y="{height - 8}" fill="#8b97a8" font-size="11">RV20</text>',
        f'<text x="{pad + 6}" y="16" fill="#8b97a8" font-size="11">premium yield</text>',
    ]
    if book_point and book_point[0] > 0:
        y_end = min(max_y, book_point[1] / book_point[0] * max_x)
        parts.append(
            f'<line x1="{px(0)}" y1="{py(0)}" x2="{px(max_x)}" y2="{py(y_end)}" stroke="#6b96f2"/>'
        )
    for rv, yld, capital, color, label in points:
        radius = 4 + min(10, capital / 5000)
        parts.append(
            f'<circle cx="{px(rv):.1f}" cy="{py(yld):.1f}" r="{radius:.1f}" fill="{color}">'
            f'<title>{escape(label)}</title></circle>'
        )
    if book_point:
        parts.append(
            f'<circle cx="{px(book_point[0]):.1f}" cy="{py(book_point[1]):.1f}" r="8" fill="none" stroke="#e7ecf3" stroke-width="2"/>'
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" style="width:100%;height:auto">'
        + "".join(parts)
        + "</svg>"
    )


def _bucket_chart(rows: list[tuple[str, float, str]], cap: int) -> str:
    if not rows:
        return '<p class="note">No bucket capital yet.</p>'
    width, height = 640, max(80, 28 * len(rows) + 16)
    cap_x = 16 + (min(cap, 100) / 100) * (width - 32)
    parts = [f'<line x1="{cap_x:.1f}" y1="8" x2="{cap_x:.1f}" y2="{height - 8}" stroke="#f7931a"/>']
    for index, (name, pct, note) in enumerate(rows):
        y = 16 + index * 28
        bar = 16 + (min(pct, 100) / 100) * (width - 32)
        parts.append(f'<text x="16" y="{y + 12}" fill="#8b97a8" font-size="11">{escape(name)} {pct:.0f}% {escape(note)}</text>')
        parts.append(f'<rect x="16" y="{y + 16}" width="{max(bar - 16, 0):.1f}" height="6" fill="#6b96f2"/>')
    return f'<svg viewBox="0 0 {width} {height}" role="img" style="width:100%;height:auto">{"".join(parts)}</svg>'


def _assignment_chart(facts: dict, risk) -> str:
    width, height = 640, 90
    assign = min(facts["assign_pct"], 100)
    cash_after = facts["cash_after_pct"]
    parts = [
        f'<text x="16" y="24" fill="#8b97a8" font-size="11">assignment {assign:.0f}% / cap {risk.max_assign_pct_nav}%</text>',
        f'<rect x="16" y="30" width="{16 + assign / 100 * (width - 48):.1f}" height="8" fill="#f7931a"/>',
        f'<text x="16" y="64" fill="#8b97a8" font-size="11">cash after {cash_after:.0f}% / floor {risk.min_cash_after_assign_pct}%</text>',
        f'<rect x="16" y="70" width="{16 + max(min(cash_after, 100), 0) / 100 * (width - 48):.1f}" height="8" fill="#16c784"/>',
    ]
    return f'<svg viewBox="0 0 {width} {height}" role="img" style="width:100%;height:auto">{"".join(parts)}</svg>'


_HEAT_GREY = "#3a4350"
_HEAT_CAP = 25.0
_INTENT_LABELS = (("income", "Income"), ("both", "Both"), ("accumulate", "Accumulate"))


def _worst_ratio(row: list[float], side: float) -> float:
    if not row or side <= 0:
        return math.inf
    total = sum(row)
    if total <= 0:
        return math.inf
    high = max(row)
    low = min(row)
    side2 = side * side
    return max((side2 * high) / (total * total), (total * total) / (side2 * low))


def _squarify(
    items: list[tuple[float, object]], x: float, y: float, w: float, h: float
) -> list[tuple[float, float, float, float, object]]:
    """Squarified treemap. Larger values are placed first. Rects do not overlap."""
    if w <= 0 or h <= 0 or not items:
        return []
    ranked = sorted(items, key=lambda item: item[0], reverse=True)
    total = sum(value for value, _ref in ranked) or 1.0
    areas = [(value / total) * (w * h) for value, _ref in ranked]
    refs = [ref for _value, ref in ranked]
    out: list[tuple[float, float, float, float, object]] = []
    left, top, width, height = x, y, w, h
    while areas:
        row_areas: list[float] = []
        row_refs: list[object] = []
        index = 0
        while index < len(areas):
            candidate = row_areas + [areas[index]]
            side = min(width, height)
            current = _worst_ratio(row_areas, side) if row_areas else math.inf
            if not row_areas or _worst_ratio(candidate, side) <= current:
                row_areas.append(areas[index])
                row_refs.append(refs[index])
                index += 1
            else:
                break
        row_sum = sum(row_areas) or 1.0
        if width >= height:
            column = row_sum / height if height else 0.0
            cursor = top
            for area, ref in zip(row_areas, row_refs):
                tile_h = (area / row_sum) * height
                out.append((left, cursor, column, tile_h, ref))
                cursor += tile_h
            left += column
            width -= column
        else:
            row_h = row_sum / width if width else 0.0
            cursor = left
            for area, ref in zip(row_areas, row_refs):
                tile_w = (area / row_sum) * width
                out.append((cursor, top, tile_w, row_h, ref))
                cursor += tile_w
            top += row_h
            height -= row_h
        del areas[: len(row_areas)]
        del refs[: len(row_areas)]
    return out


def _lerp_hex(start: str, end: str, amount: float) -> str:
    def channels(color: str) -> list[int]:
        raw = color.lstrip("#")
        return [int(raw[index:index + 2], 16) for index in (0, 2, 4)]

    left, right = channels(start), channels(end)
    mixed = [round(left[index] + (right[index] - left[index]) * amount) for index in range(3)]
    return "#" + "".join(f"{part:02x}" for part in mixed)


def _heat_color(pnl: float | None) -> tuple[str, str]:
    """Grey at no change. Green deepens with profit, red with loss, capped at ±25%."""
    if pnl is None or abs(pnl) < 0.05:
        return _HEAT_GREY, "#c5ced9"
    amount = max(-1.0, min(1.0, pnl / _HEAT_CAP))
    if amount > 0:
        return _lerp_hex("#1e3a2a", "#16c784", amount), "#e7ecf3"
    return _lerp_hex("#3a1c1c", "#f87171", -amount), "#e7ecf3"


def _heat_tile(row: dict) -> dict:
    qty = abs(Decimal(row["qty"]))
    average = Decimal(row["avg"])
    raw_mult = str(row.get("multiplier") or "").strip()
    multiplier = Decimal(raw_mult) if raw_mult else Decimal(1)
    cost = qty * average * multiplier
    market = row.get("market")
    area = abs(Decimal(market)) if market not in (None, 0, Decimal(0)) else cost
    if area <= 0:
        area = Decimal(1)
    pnl = row.get("pnl")
    pct = None
    if pnl is not None and cost > 0:
        pct = float(Decimal(pnl) / cost * Decimal(100))
    name = str(row["symbol"])
    if row.get("sec_type") == "OPT":
        bits = [name, str(row.get("right") or ""), str(row.get("strike") or "")]
        name = " ".join(bit for bit in bits if bit)
    return {
        "name": name,
        "mark": str(row.get("mark") or ""),
        "area": float(area),
        "cost": float(cost),
        "pnl": None if pnl is None else float(pnl),
        "pct": pct,
    }


def _heat_groups(rows: list[dict], intents: dict[str, str]) -> list[dict]:
    buckets: dict[str, list[dict]] = {key: [] for key, _label in _INTENT_LABELS}
    for row in rows:
        tile = _heat_tile(row)
        intent = intents.get(str(row["symbol"]).upper(), "income")
        if intent not in buckets:
            intent = "income"
        buckets[intent].append(tile)
    groups = []
    for key, label in _INTENT_LABELS:
        tiles = buckets[key]
        if not tiles:
            continue
        cost = sum(tile["cost"] for tile in tiles)
        pnl = sum(tile["pnl"] or 0 for tile in tiles)
        groups.append({
            "label": label,
            "tiles": tiles,
            "area": sum(tile["area"] for tile in tiles) or 1.0,
            "pct": (pnl / cost * 100) if cost else 0.0,
        })
    groups.sort(key=lambda group: group["area"], reverse=True)
    return groups


def _fit_label(text: str, width: float, size: int) -> str:
    room = max(1, int(width / (size * 0.62)))
    if len(text) <= room:
        return text
    return text[: max(1, room - 1)] + "…"


def _heat_tip(tile: dict) -> str:
    lines = [tile["name"]]
    if tile["mark"]:
        lines.append(f"Mark {tile['mark']}")
    if tile["pct"] is None:
        lines.append("P&L —")
    else:
        sign = "+" if tile["pct"] > 0 else ""
        lines.append(f"P&L {sign}{tile['pct']:.1f}%")
    return "\n".join(lines)


def _heat_pack(groups: list[dict]) -> str:
    """Tile facts for the browser. It lays these out in the card's real pixel box."""
    packed = []
    for group in groups:
        tiles = []
        for tile in group["tiles"]:
            fill, ink = _heat_color(tile["pct"])
            tiles.append({
                "name": tile["name"],
                "mark": tile["mark"],
                "area": tile["area"],
                "pct": tile["pct"],
                "fill": fill,
                "ink": ink,
            })
        ink = "#8b97a8"
        if group["pct"] > 0.05:
            ink = "#16c784"
        elif group["pct"] < -0.05:
            ink = "#f87171"
        packed.append({
            "label": group["label"],
            "area": group["area"],
            "pct": group["pct"],
            "ink": ink,
            "tiles": tiles,
        })
    return json.dumps(packed, separators=(",", ":")).replace("<", "\\u003c")


def _position_heatmap(rows: list[dict], intents: dict[str, str]) -> str:
    """Nested treemap. Outer blocks are intent. Inner tiles are open positions."""
    groups = _heat_groups(rows, intents)
    if not groups:
        return '<p class="note">No open position.</p>'
    width, height = 960, 420
    parts: list[str] = []
    font = "ui-monospace, monospace"
    for left, top, block_w, block_h, group in _squarify(
        [(group["area"], group) for group in groups], 0, 0, width, height
    ):
        parts.append(
            f'<rect x="{left:.1f}" y="{top:.1f}" width="{block_w:.1f}" height="{block_h:.1f}" '
            f'fill="#10141a" stroke="#3a4350"/>'
        )
        header = 22 if block_h > 70 and block_w > 90 else 0
        if header:
            ink = "#8b97a8"
            if group["pct"] > 0.05:
                ink = "#16c784"
            elif group["pct"] < -0.05:
                ink = "#f87171"
            sign = "+" if group["pct"] > 0 else ""
            parts.append(
                f'<text x="{left + 8:.1f}" y="{top + 15:.1f}" fill="#e7ecf3" font-size="11" '
                f'font-family="{font}">{escape(group["label"])}</text>'
            )
            parts.append(
                f'<text x="{left + block_w - 8:.1f}" y="{top + 15:.1f}" fill="{ink}" font-size="11" '
                f'text-anchor="end" font-family="{font}">{sign}{group["pct"]:.1f}%</text>'
            )
        inner_w = block_w
        inner_h = block_h - header
        if inner_w <= 6 or inner_h <= 6:
            continue
        for tile_x, tile_y, tile_w, tile_h, tile in _squarify(
            [(item["area"], item) for item in group["tiles"]], left, top + header, inner_w, inner_h
        ):
            bg, fg = _heat_color(tile["pct"])
            gap_x, gap_y = tile_x + 2, tile_y + 2
            gap_w, gap_h = max(0.0, tile_w - 4), max(0.0, tile_h - 4)
            parts.append(
                f'<rect x="{gap_x:.1f}" y="{gap_y:.1f}" width="{gap_w:.1f}" height="{gap_h:.1f}" fill="{bg}" '
                f'data-name="{escape(tile["name"], quote=True)}"><title>{escape(_heat_tip(tile))}</title></rect>'
            )
            if gap_w <= 32 or gap_h <= 22:
                continue
            size = min(13, max(9, int(gap_w / 7)))
            parts.append(
                f'<text x="{gap_x + 6:.1f}" y="{gap_y + size + 4:.1f}" fill="{fg}" font-size="{size}" '
                f'font-family="{font}">{escape(_fit_label(tile["name"], gap_w - 8, size))}</text>'
            )
            if gap_w <= 60 or gap_h <= 44:
                continue
            cursor = gap_y + size + 4 + size + 2
            if tile["mark"]:
                parts.append(
                    f'<text x="{gap_x + 6:.1f}" y="{cursor:.1f}" fill="{fg}" font-size="{size}" '
                    f'font-family="{font}">{escape(tile["mark"])}</text>'
                )
                cursor += size + 2
            if tile["pct"] is not None:
                sign = "+" if tile["pct"] > 0 else ""
                parts.append(
                    f'<text x="{gap_x + 6:.1f}" y="{cursor:.1f}" fill="{fg}" font-size="{size}" '
                    f'font-family="{font}">Δ {sign}{tile["pct"]:.1f}%</text>'
                )
    return (
        f'<script type="application/json" id="book-heat-data">{_heat_pack(groups)}</script>'
        f'<svg class="book-heat" viewBox="0 0 {width} {height}" role="img">'
        + "".join(parts)
        + "</svg>"
    )


def _positions_page(conn, account: str, query: dict) -> dict:
    kind = (query.get("kind") or ["all"])[0]
    if kind not in {"all", "stock", "option"}:
        kind = "all"
    rows = _position_rows(conn, account)
    visible = [row for row in rows if _position_visible(row, kind)]
    offset = _offset(query)
    page, more, nxt = _window(visible, offset)
    filters = "".join(
        f'<a class="{"on" if key == kind else ""}" href="/positions?kind={key}">{label}</a>'
        for key, label in (("all", "All"), ("stock", "Stock"), ("option", "Option"))
    )
    body_rows = "".join(_position_tr(row) for row in page)
    if not body_rows and offset == 0:
        body_rows = '<tr><td colspan="10">No open position.</td></tr>'
    if _wants_rows(query):
        return _fragment(body_rows, more, nxt)
    synced = _position_clock(rows)
    body = (
        '<div class="grid cols-5">'
        f"{_position_nlv_stat(rows)}"
        f"{_position_stat('Open', str(len(rows)), 'on this account')}"
        f"{_position_stat('Stock', str(sum(1 for row in rows if row['sec_type'] == 'STK')), 'shares')}"
        f"{_position_stat('Option', str(sum(1 for row in rows if row['sec_type'] == 'OPT')), 'contracts')}"
        f"{_position_pnl_stat(rows)}"
        "</div>"
        '<div class="card fill"><h2>Open positions '
        '<button class="more" data-cmd="sync-positions" type="button">Sync from IB</button></h2>'
        f'<div class="filters">{filters}</div>'
        f'{_feed(f"/positions?kind={quote(kind)}", nxt, more, _positions_table(body_rows))}</div></div>'
    )
    chips = [("info", f"{len(rows)} open" if rows else "flat")]
    if synced:
        chips.append(("", f"synced {synced}"))
    return _html(
        "Positions",
        "The latest IBKR book for this account. A stock and an option each get their own row. A flat contract is left off.",
        body,
        chips,
        _REFRESH,
    )


def _positions_table(rows: str) -> str:
    return (
        '<table class="data"><thead><tr>'
        "<th>Symbol</th><th>Type</th><th>Side</th><th>Qty</th><th>Avg</th><th>Mark</th>"
        "<th>Value</th><th>Open P&amp;L</th><th>Expiry</th><th>Strike</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>"
    )


def _position_rows(conn, account: str) -> list[dict]:
    if not account:
        return []
    found = conn.execute(
        """
        SELECT DISTINCT ON (symbol, sec_type, expiry, contract_right, strike)
               symbol, quantity, avg_price, sec_type, expiry, contract_right, strike,
               market_price, currency, multiplier, market_value, unrealized_pnl,
               realized_pnl, snapshot_at, simulated
        FROM positions
        WHERE account_id = %s AND (simulated = false OR %s)
        ORDER BY symbol, sec_type, expiry, contract_right, strike, snapshot_at DESC, id DESC
        """,
        (account, _replaying(conn, account)),
    ).fetchall()
    today = datetime.now(HKT).date()
    rows = []
    for symbol, quantity, avg_price, sec_type, expiry, right, strike, mark, currency, multiplier, value, unrealized, realized, snapshot_at, simulated in found:
        qty = Decimal(quantity)
        if qty == 0:
            continue
        mult = Decimal(multiplier or 1)
        price = None if mark is None else Decimal(mark)
        average = Decimal(avg_price)
        market_value = None if value is None else Decimal(value)
        if market_value is None and price is not None:
            market_value = qty * price * mult
        pnl = None if unrealized is None else Decimal(unrealized)
        if pnl is None and price is not None:
            pnl = (price - average) * qty * mult
        dte = "" if expiry is None else str((expiry - today).days)
        rows.append({
            "symbol": symbol,
            "qty": qty,
            "avg": average,
            "sec_type": sec_type or "STK",
            "right": right or "",
            "expiry": "" if expiry is None else expiry.isoformat(),
            "dte": dte,
            "strike": "" if strike is None else _plain_number(strike),
            "mark": "" if price is None else f"{price:,.2f}",
            "market": market_value,
            "value": "" if market_value is None else f"{market_value:,.2f}",
            "pnl": pnl,
            "currency": currency or "",
            "multiplier": "" if sec_type != "OPT" else _plain_number(mult),
            "realized": "" if realized is None else f"{Decimal(realized):,.2f}",
            "synced": "" if snapshot_at is None else snapshot_at.astimezone(HKT).strftime("%Y-%m-%d %H:%M"),
            "simulated": bool(simulated),
        })
    rows.sort(key=lambda row: (row["symbol"], 0 if row["sec_type"] == "STK" else 1, row["expiry"], row["strike"]))
    return rows


def _position_visible(row: dict, kind: str) -> bool:
    if kind == "stock":
        return row["sec_type"] == "STK"
    if kind == "option":
        return row["sec_type"] == "OPT"
    return True


def _position_stat(label: str, value: str, meta: str) -> str:
    return (
        f'<div class="card stat"><div class="label">{label}</div>'
        f'<div class="value">{escape(value)}</div><div class="meta">{escape(meta)}</div></div>'
    )


def _position_nlv_stat(rows: list[dict]) -> str:
    known = [row for row in rows if row["market"] is not None]
    if not rows:
        return _position_stat("NLV", DASH, "flat")
    if not known:
        return _position_stat("NLV", DASH, "no mark yet")
    currencies = {row["currency"] or "USD" for row in known}
    missing = len(rows) - len(known)
    gap = "" if missing == 0 else f" · {missing} unmarked"
    if len(currencies) == 1:
        currency = next(iter(currencies))
        total = sum((row["market"] for row in known), Decimal(0))
        return (
            '<div class="card stat"><div class="label">NLV</div>'
            f'<div class="value">{escape(marked(total, currency))}</div>'
            f'<div class="meta">{escape(currency + gap)}</div></div>'
        )
    parts = []
    for currency in sorted(currencies):
        total = sum(
            (row["market"] for row in known if (row["currency"] or "USD") == currency),
            Decimal(0),
        )
        parts.append(f"{currency} {total:,.2f}")
    return _position_stat("NLV", "mixed", "  ".join(parts) + gap)


def _position_pnl_stat(rows: list[dict]) -> str:
    known = [row for row in rows if row["pnl"] is not None]
    if not known:
        return _position_stat("Open P&L", DASH, "no mark yet")
    currencies = {row["currency"] or "USD" for row in known}
    if len(currencies) == 1:
        total = sum((row["pnl"] for row in known), Decimal(0))
        tone = "up" if total > 0 else "down" if total < 0 else ""
        return (
            '<div class="card stat"><div class="label">Open P&amp;L</div>'
            f'<div class="value {tone}">{escape(_signed(total))}</div>'
            f'<div class="meta">{escape(next(iter(currencies)))}</div></div>'
        )
    parts = []
    for currency in sorted(currencies):
        total = sum((row["pnl"] for row in known if (row["currency"] or "USD") == currency), Decimal(0))
        parts.append(f"{currency} {_signed(total)}")
    return _position_stat("Open P&L", "mixed", "  ".join(parts))


def _position_clock(rows: list[dict]) -> str:
    stamps = [row["synced"] for row in rows if row["synced"]]
    return max(stamps) if stamps else ""


def _position_tr(row: dict) -> str:
    kind = _position_kind(row)
    row["label"] = kind
    side = "long" if row["qty"] > 0 else "short"
    side_tag = "green" if side == "long" else "amber"
    type_tag = {"stock": "", "put": "amber", "call": "teal"}.get(kind, "")
    pnl = row["pnl"]
    pnl_class = "" if pnl is None else "chg-up" if pnl > 0 else "chg-down" if pnl < 0 else ""
    pnl_text = DASH if pnl is None else _signed(pnl)
    attrs = " ".join(
        f'{name}="{escape(str(row[key]), quote=True)}"'
        for name, key in (
            ("data-symbol", "symbol"),
            ("data-kind", "label"),
            ("data-qty", "qty"),
            ("data-avg", "avg"),
            ("data-mark", "mark"),
            ("data-value", "value"),
            ("data-pnl", "pnl"),
            ("data-expiry", "expiry"),
            ("data-dte", "dte"),
            ("data-strike", "strike"),
            ("data-ccy", "currency"),
            ("data-mult", "multiplier"),
            ("data-realized", "realized"),
            ("data-synced", "synced"),
        )
    )
    qty = _plain_number(abs(row["qty"])) if row["sec_type"] == "OPT" else _plain_number(row["qty"])
    if row["sec_type"] == "OPT" and row["qty"] < 0:
        qty = f"-{qty}"
    average = f'{row["avg"]:,.2f}'
    return (
        f'<tr data-cmd="position-row" {attrs} style="cursor:pointer">'
        f'<td><b>{escape(row["symbol"])}</b>'
        + (' <span class="tag amber">SIM</span>' if row.get("simulated") else "")
        + (f'<div class="sym-name">{escape(row["currency"])}</div>' if row["currency"] else "")
        + "</td>"
        f'<td><span class="tag {type_tag}">{escape(kind)}</span></td>'
        f'<td><span class="tag {side_tag}">{side}</span></td>'
        f'<td class="mono">{escape(qty)}</td>'
        f'<td class="mono">{escape(average)}</td>'
        f'<td class="mono">{escape(row["mark"] or DASH)}</td>'
        f'<td class="mono">{escape(row["value"] or DASH)}</td>'
        f'<td class="mono {pnl_class}">{escape(pnl_text)}</td>'
        f'<td class="mono">{escape(row["expiry"] or DASH)}'
        + (f'<div class="sym-name">{escape(row["dte"])}d</div>' if row["dte"] else "")
        + "</td>"
        f'<td class="mono">{escape(row["strike"] or DASH)}</td>'
        "</tr>"
    )


def _position_kind(row: dict) -> str:
    if row["sec_type"] == "OPT" and row["right"] == "P":
        return "put"
    if row["sec_type"] == "OPT" and row["right"] == "C":
        return "call"
    if row["sec_type"] == "OPT":
        return "option"
    return "stock"


def _signed(number: Decimal) -> str:
    text = f"{number:,.2f}"
    if number > 0:
        return f"+{text}"
    return text


def _history_csv(conn, account: str, query: dict) -> dict:
    kind = _history_kind(query)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["date", "symbol", "tactic", "phase", "pnl", "status"])
    for item in _history_rows(conn, account):
        if not _history_visible(item, kind):
            continue
        writer.writerow([item["when"], item["symbol"], item["tactic"], item["phase"], item["pnl"], item["status"]])
    return {"status": 200, "kind": "csv", "body": buffer.getvalue(), "title": "", "lead": "", "chips": []}


_COPY_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
    '<rect x="9" y="9" width="12" height="12" rx="1"/>'
    '<path d="M5 15V5a1 1 0 0 1 1-1h10"/></svg>'
)


def _accounts(conn, account: str, query: dict, note: str = "") -> dict:
    paper = os.environ.get("ATHENA_IB_PAPER_ACCOUNT_ID", "").strip()
    live = [part.strip() for part in os.environ.get("ATHENA_IB_ACCOUNT_IDS", "").split(",") if part.strip()]
    labels = _account_labels(conn, account)
    settings = load_active(conn, account) if account else None
    session_ids = set(active_accounts())
    books = [(labels.get(paper, "Paper"), "PAPER", paper)] if paper else []
    books += [(labels.get(item, "Live"), "LIVE", item) for item in live]
    known = {book_id for _name, _mode, book_id in books}
    if account:
        current = load_active(conn, account)
        for entry in current.extra_accounts:
            if entry.account_id in known:
                continue
            books.append((entry.label or entry.account_id, "PAPER", entry.account_id))
            known.add(entry.account_id)
    cards = []
    rows = []
    panels = []
    for index, (name, mode, book_id) in enumerate(books):
        facts = _book_facts(conn, book_id)
        active = book_id == account
        panel_id = f"book-panel-{index}"
        sync_on = control_flag(settings, book_id, "sync", book_id in session_ids) if settings else book_id in session_ids
        trading_on = control_flag(settings, book_id, "trading", book_id in session_ids) if settings else book_id in session_ids
        cards.append(_account_card(name, mode, book_id, active, facts, panel_id))
        rows.append(_account_control_row(name, mode, book_id, facts, sync_on, trading_on))
        panels.append(_book_panel(book_id, mode, facts, panel_id, conn))
    grid = "".join(cards) or '<div class="card"><p>No account id is configured.</p></div>'
    table = "".join(rows) or "<tr><td colspan=\"5\">No account id is configured.</td></tr>"
    note_html = f'<p class="note">{escape(note)}</p>' if note else ""
    body = (
        f"{note_html}"
        f'<div class="grid cols-3" id="acctGrid">{grid}</div>'
        '<div class="card fill">'
        '<h2>Account controls <button class="more" type="button" data-cmd="add-account">+ add account</button></h2>'
        '<div class="scroll-y"><table class="data"><thead><tr>'
        "<th>Account</th><th>Mode</th><th>Sync</th><th>Trading</th><th>Last mark</th>"
        f"</tr></thead><tbody>{table}</tbody></table></div></div>"
        f"{''.join(panels)}"
    )
    chips = [("info", f"{len(books)} CONFIGURED")]
    if account:
        chips.append(("ok", "ACTIVE"))
    return _html(
        "Accounts",
        "Configured IB accounts. The active book is the one this Gateway session trades.",
        body,
        chips,
    )


def _account_labels(conn, account: str) -> dict[str, str]:
    if not account:
        return {}
    current = load_active(conn, account)
    return {entry.account_id: entry.label for entry in current.extra_accounts if entry.label}


def _post_accounts(conn, account: str, form: dict, actor: str) -> dict:
    if not account:
        return _accounts(conn, account, {}, "No account is configured.") | {"status": 400}
    if (form.get("action") or [""])[0] == "toggle":
        return _toggle_account(conn, account, form, actor)
    account_id = (form.get("account_id") or [""])[0].strip()
    label = (form.get("label") or [""])[0].strip()
    if not account_id or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for char in account_id):
        return _accounts(conn, account, {}, "Use letters, numbers, or a dash.") | {"status": 400}
    if len(account_id) > 32 or len(label) > 40:
        return _accounts(conn, account, {}, "That id or label is too long.") | {"status": 400}
    current = load_active(conn, account)
    payload = SettingsPayload.model_validate(current.model_dump())
    label = label or account_id
    updated = []
    replaced = False
    for entry in payload.extra_accounts:
        if entry.account_id.upper() == account_id.upper():
            updated.append(AccountEntry(account_id=account_id, label=label))
            replaced = True
        else:
            updated.append(entry)
    if not replaced:
        updated.append(AccountEntry(account_id=account_id, label=label))
    payload.extra_accounts = updated
    payload.privilege = current.privilege
    try:
        save_settings(conn, account, payload, f"console:{actor}", f"add account {account_id}")
    except ValueError as exc:
        return _accounts(conn, account, {}, str(exc)) | {"status": 400}
    paper = os.environ.get("ATHENA_IB_PAPER_ACCOUNT_ID", "").strip()
    live = [part.strip() for part in os.environ.get("ATHENA_IB_ACCOUNT_IDS", "").split(",") if part.strip()]
    if account_id in {paper, *live, account}:
        note = f"Saved the label {label}."
    else:
        note = f"Added {label}. Paper mode, sync off."
    return _accounts(conn, account, {}, note)


def _volatility_card(risk, strategy) -> str:
    """One card for the volatility gate and the strike band. The RV floor still follows the profile."""
    method = _field("Method", "The mark written today. This page cannot change it.", _tag("premium_ratio", "blue"))
    waiting = _field("chain_bs", "Waits for measured chain marks. It cannot be selected.", _tag("chain_bs"))
    return (
        '<div class="card risk-span-2" id="volatility-reading"><h2>Volatility reading</h2>'
        '<div class="form-grid">'
        f"{method}{waiting}"
        f"{_limit('Min samples for rank', 'ivr_min_samples', strategy.ivr_min_samples, 'How many samples a rank needs. Fewer than 20 leaves the rank off.', preview=True)}"
        f"{_limit('RV window', 'rv_window', strategy.rv_window, 'Sessions in the realized-vol window. This is the volatility gate.', preview=True)}"
        f"{_limit('RV high at pctl', 'rv_high_pctl', strategy.rv_high_pctl, 'RV20 percentile at or above this is RV_HIGH.', preview=True)}"
        f"{_limit('RV low at pctl', 'rv_low_pctl', strategy.rv_low_pctl, 'RV20 percentile at or below this is RV_LOW.', preview=True)}"
        f"{_limit('Min RV percentile new short', 'min_rv', risk.min_rv_pctl_new_short, 'This follows the current profile. A new short is blocked when the symbol RV20 percentile is below this. Fewer than 20 samples does not pass.')}"
        f"{_limit('Chain band %', 'chain_band_pct', strategy.chain_band_pct, 'How wide a strike menu to fetch. This is discovery, not the strike that gets sold. The sold strike follows the delta target.')}"
        f"{_limit('Strangle delta target', 'strangle_delta_target', strategy.strangle_delta_target, 'Strangle legs prefer this delta. Changing it moves the distance for every symbol.', step='0.01')}"
        f"{_limit('OTM floor %', 'otm_floor_pct', strategy.otm_floor_pct, 'The narrowest distance the volatility width may use.')}"
        f"{_limit('OTM ceiling %', 'otm_ceiling_pct', strategy.otm_ceiling_pct, 'The widest distance the volatility width may use.')}"
        f"{_limit('RV multiple high', 'iv_rv_multiple_hi', strategy.iv_rv_multiple_hi, 'Assumption, not a measurement. Used only to pick a strike when realized vol is 45% or higher.', step='0.01')}"
        f"{_limit('RV multiple mid', 'iv_rv_multiple_mid', strategy.iv_rv_multiple_mid, 'Assumption, not a measurement. Used only to pick a strike when realized vol is 25% to 45%.', step='0.01')}"
        f"{_limit('RV multiple low', 'iv_rv_multiple_lo', strategy.iv_rv_multiple_lo, 'Assumption, not a measurement. Used only to pick a strike when realized vol is below 25%.', step='0.01')}"
        "</div></div>"
    )


def method_switch_note(symbols: int) -> str:
    """Wording for a future chain_bs switch. The disabled control does not call this."""
    return "\n".join((
        "method: premium_ratio → chain_bs",
        f"symbols: {symbols}",
        "quiet sessions: 20",
        "sanity: expected ok",
    ))


def listed_otm_pct(spot: Decimal | None, choices: list[tuple[str, Decimal]]) -> Decimal | None:
    """Shallowest positive distance in a stored strike list. None when nothing is listed out of the money."""
    if spot is None or spot <= 0:
        return None
    found: list[Decimal] = []
    for right, strike in choices:
        if strike <= 0:
            continue
        if right == "P" and strike < spot:
            found.append((spot - strike) / spot * Decimal(100))
        elif right == "C" and strike > spot:
            found.append((strike - spot) / spot * Decimal(100))
    if not found:
        return None
    return min(found)


def band_gap(listed: Decimal | None, target: Decimal | None) -> Decimal | None:
    """Listed distance minus the target distance. Positive means the menu is farther out."""
    if listed is None or target is None:
        return None
    return listed - target


def vol_tag(tags: set[str]) -> str:
    from athena_strategy.tags import RV_HIGH, RV_LOW

    if RV_HIGH in tags:
        return "RV_HIGH"
    if RV_LOW in tags:
        return "RV_LOW"
    return "—"


def reading_shift(bars, samples, strategy, field: str, new_value: int) -> tuple[str, str]:
    """Tags before and after one threshold change. The second call is the saved reading."""
    from athena_strategy.indicators import compute

    before = vol_tag(compute(bars, strategy, rv_samples=samples).tags)
    after = vol_tag(compute(bars, strategy.model_copy(update={field: new_value}), rv_samples=samples).tags)
    return before, after


def threshold_preview(field: str, old: int, new: int, before: list[str], after: list[str]) -> str:
    high_before = before.count("RV_HIGH")
    high_after = after.count("RV_HIGH")
    low_before = before.count("RV_LOW")
    low_after = after.count("RV_LOW")
    changed = sum(1 for left, right in zip(before, after) if left != right)
    return "\n".join((
        f"{field}: {old} → {new}",
        f"RV_HIGH: {high_before} → {high_after}",
        f"RV_LOW: {low_before} → {low_after}",
        f"Symbols changed: {changed}",
    ))


def settings_note(profile: str, before, after) -> str:
    """Name only the blocks that differ. A profile click that also moves a strategy knob lists both."""
    parts = []
    if before.risk.model_dump() != after.risk.model_dump():
        parts.append(f"risk {profile}")
    names = [
        name for name in type(after.strategy).model_fields
        if getattr(before.strategy, name) != getattr(after.strategy, name)
    ]
    if names:
        parts.append("strategy " + " ".join(names))
    return " + ".join(parts) if parts else f"risk {profile}"


def _rv_need(strategy) -> int:
    return max(strategy.rv_window + 1, strategy.volume_avg_sessions + 1, 30)


def _preview_thresholds(conn, current, form: dict) -> dict:
    field = (form.get("preview_field") or [""])[0]
    if field not in PREVIEW_FIELDS:
        return {"status": 400, "kind": "json", "body": json.dumps({"ok": False, "text": "That field does not preview."})}
    try:
        new_value = int((form.get(field) or [""])[0])
    except ValueError:
        return {"status": 400, "kind": "json", "body": json.dumps({"ok": False, "text": "Use a whole number."})}
    from athena_strategy.indicators import load_bars, rv_samples

    before: list[str] = []
    after: list[str] = []
    for symbol in current.watchlist:
        bars = load_bars(conn, symbol, _rv_need(current.strategy))
        samples = rv_samples(conn, symbol)
        left, right = reading_shift(bars, samples, current.strategy, field, new_value)
        before.append(left)
        after.append(right)
    text = threshold_preview(field, getattr(current.strategy, field), new_value, before, after)
    return {"status": 200, "kind": "json", "body": json.dumps({"ok": True, "text": text})}


def _points(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return f"{value.quantize(Decimal('0.01'))}"


def _vol_table(conn, settings) -> str:
    """Read-only evidence. It does not record a new RV mark."""
    from athena_exec.options import rv_multiple, target_otm_pct
    from athena_marketdata.pricing import last_tradeable_price
    from athena_strategy.indicators import compute, load_bars, rv_samples
    from athena_strategy.iv import PREMIUM_METHOD, RANK_FLOOR, iv_rank, sanity_label
    from athena_strategy.screener import load_choices

    rows = []
    floor = max(settings.strategy.ivr_min_samples, RANK_FLOOR)
    for symbol in settings.watchlist:
        bars = load_bars(conn, symbol, _rv_need(settings.strategy))
        samples = rv_samples(conn, symbol)
        reading = compute(bars, settings.strategy, rv_samples=samples)
        target = None
        sigma = None
        if reading.rv20 is not None and reading.rv20 > 0:
            target, _clamp = target_otm_pct(reading.rv20, settings.strategy, settings.strategy.dte_target)
            sigma = rv_multiple(reading.rv20, settings.strategy) * reading.rv20
        listed = listed_otm_pct(
            last_tradeable_price(conn, symbol),
            [(choice.right, choice.strike) for choice in load_choices(conn, symbol)],
        )
        gap = band_gap(listed, target)
        rank_text = f"pending ({len(samples)}/{floor})" if reading.rv_pctl is None else str(reading.rv_pctl)
        method, count, proxy, underlying, pairs = _iv_window(conn, symbol)
        iv_rank_text = "—"
        if proxy is not None:
            ranked = iv_rank([item[0] for item in pairs], proxy, settings.strategy.ivr_min_samples)
            iv_rank_text = f"pending ({count}/{floor})" if ranked is None else str(ranked)
        verdict = sanity_label(pairs)
        tone = {"INVERTED?": "red", "pending": "amber", "ok": "green"}.get(verdict, "")
        rows.append(
            "<tr>"
            f"<td>{escape(symbol)}</td>"
            f"<td>{_points(reading.rv20)}</td>"
            f"<td>{escape(rank_text)}</td>"
            f"<td>{escape(vol_tag(reading.tags))}</td>"
            f"<td>{_points(sigma)}</td>"
            f"<td>{_points(target)}</td>"
            f"<td>{_points(listed)}</td>"
            f"<td>{_points(gap)}</td>"
            f"<td>{escape(method or PREMIUM_METHOD)}</td>"
            f"<td>{count}</td>"
            f"<td>{_points(proxy)}</td>"
            f"<td>{_points(underlying)}</td>"
            f"<td>{escape(iv_rank_text)}</td>"
            f"<td>{_tag(verdict, tone)}</td>"
            "</tr>"
        )
    body = "".join(rows) or '<tr><td colspan="14">No symbol is on the watchlist.</td></tr>'
    return (
        '<div class="card" id="vol-evidence"><h2>Volatility</h2>'
        '<p class="card-lead">A full red sanity column is the known state. The stored premium ratio moves opposite the price. It turns ok when measured chain marks exist.</p>'
        '<table class="data"><thead>'
        '<tr><th colspan="8">Volatility</th><th colspan="6">Legacy premium ratio</th></tr>'
        "<tr><th>Symbol</th><th>rv20</th><th>rv pctl</th><th>tag</th>"
        f"<th>{_q('sigma ref', 'Assumption, not a measurement. The RV multiple times RV20. Not implied vol.')}</th>"
        "<th>target otm %</th><th>listed otm %</th>"
        f"<th>{_q('band gap', 'Listed distance minus the target distance. Positive means the nearest listed strike is farther out than the target.')}</th>"
        "<th>method</th><th>samples</th><th>iv proxy</th><th>underlying</th><th>rank</th><th>sanity</th>"
        "</tr></thead>"
        f"<tbody>{body}</tbody></table></div>"
    )


def _volatility_page(conn, account: str, query: dict, note: str = "") -> dict:
    """Realized vol, strike distance, and the premium-ratio check. Read only."""
    del query
    if not account:
        return _html("Volatility", "No account is configured.", '<div class="card"><p>No account is configured.</p></div>', [])
    current = load_active(conn, account)
    note_html = f'<p class="note">{escape(note)}</p>' if note else ""
    body = f'{note_html}<div class="strategy-scroll">{_vol_table(conn, current)}</div>'
    return _html(
        "Volatility",
        "Realized vol for each watched symbol, the strike distance, and the premium-ratio check.",
        body,
        [],
    )


def _iv_window(conn, symbol: str) -> tuple[str, int, Decimal | None, Decimal | None, list[tuple[Decimal, Decimal]]]:
    from athena_strategy.iv import PREMIUM_METHOD

    rows = conn.execute(
        """
        SELECT method, iv_proxy, underlying
        FROM iv_marks
        WHERE symbol = %s
        ORDER BY day
        """,
        (symbol.upper(),),
    ).fetchall()
    if not rows:
        return PREMIUM_METHOD, 0, None, None, []
    method = rows[-1][0] or PREMIUM_METHOD
    same = [row for row in rows if (row[0] or PREMIUM_METHOD) == method]
    pairs = [(row[1], row[2]) for row in same if row[2] is not None and row[1] is not None]
    last = same[-1]
    proxy = None if last[1] is None else Decimal(last[1])
    underlying = None if last[2] is None else Decimal(last[2])
    return method, len(same), proxy, underlying, [(Decimal(proxy), Decimal(underlying)) for proxy, underlying in pairs]


def _risk(conn, account: str, query: dict, note: str = "") -> dict:
    if not account:
        return _html("Risk & Strategy", "No account is configured.", '<div class="card"><p>No account is configured.</p></div>', [])
    current = load_active(conn, account)
    risk = current.risk
    strategy = current.strategy
    cards = []
    for name, values in PROFILES.items():
        on = " on" if risk.profile == name else ""
        cards.append(
            f'<button class="profile{on}" type="button" data-limits="{",".join(str(item) for item in values)}">'
            f'<div class="name">{name}</div><div class="desc">{PROFILE_COPY[name]}</div></button>'
        )
    priv = []
    for name, copy in PRIVILEGES.items():
        on = " on" if name == "all_orders" else ""
        title = "Raise automation privilege?" if name in {"opens_only", "auto_within_risk"} else name
        priv.append(
            f'<button class="{on}" type="button" data-cmd="privilege" data-title="{escape(title, quote=True)}" '
            f'data-help="{escape(copy, quote=True)}">{name}</button>'
        )
    versions = _version_rows(conn, account)
    note_html = f'<p class="note">{escape(note)}</p>' if note else ""
    body = (
        f"{note_html}"
        '<div class="strategy-scroll">'
        f'<div class="profiles">{"".join(cards)}</div>'
        '<form id="risk-form" method="post" action="/risk" class="risk-cols">'
        '<div class="card risk-span-5" id="strategy-card"><h2>Strategy</h2>'
        '<p class="card-lead">NFR tactic shape. Size and halt limits are above. Volatility knobs are on the reading card.</p>'
        '<div class="form-grid three">'
        f"{_limit('IVR high', 'ivr_high', strategy.ivr_high, 'A line on the phase chart. It does not tag a symbol.')}"
        f"{_limit('Screener top N', 'screener_top_n', strategy.screener_top_n, 'How many ranked strikes each table shows.')}"
        f"{_limit('CSP delta low', 'csp_delta_lo', strategy.csp_delta_lo, 'Puts quieter than this stay off the cash-secured put table.', step='0.01')}"
        f"{_limit('CSP delta target', 'csp_delta_target', strategy.csp_delta_target, 'The cash-secured put table prefers this delta.', step='0.01')}"
        f"{_limit('CSP delta high', 'csp_delta_hi', strategy.csp_delta_hi, 'Puts busier than this stay off the cash-secured put table.', step='0.01')}"
        f"{_limit('CC delta target', 'cc_delta_target', strategy.cc_delta_target, 'Covered calls are ordered by strike, then by distance to this delta.', step='0.01')}"
        f"{_limit('CC rail position', 'cc_rail_pos', strategy.cc_rail_pos, 'Sell a covered call when price is at least this far up the 63-session channel. Location may also tag the upper rail.', step='0.01')}"
        f"{_limit('DTE min', 'dte_min', strategy.dte_min, 'Shortest expiry the chain menu will offer.')}"
        f"{_limit('DTE target', 'dte_target', strategy.dte_target, 'Expiry the chain menu prefers.')}"
        f"{_limit('DTE max', 'dte_max', strategy.dte_max, 'Longest expiry the chain menu will offer.')}"
        f"{_limit('Profit take %', 'profit_take_pct', strategy.profit_take_pct, 'Close a short when this percent of the credit is captured.')}"
        f"{_limit('Roll DTE', 'roll_dte', strategy.roll_dte, 'Roll when days to expiry are at or below this.')}"
        "</div></div>"
        f"{_volatility_card(risk, strategy)}"
        '<div class="card risk-span-2"><h2>Limits</h2><div class="form-grid">'
        f"{_limit('Max symbol % NAV', 'max_symbol', risk.max_symbol_pct_nav, 'Share of net liquidation one underlying may use. A new open above this is blocked.')}"
        f"{_limit('Cash reserve %', 'cash', risk.cash_reserve_pct, 'Cash that must stay free, as a percent of net liquidation. A cash-secured open below this is blocked.')}"
        f"{_limit('Daily loss halt %', 'halt', risk.daily_loss_halt_pct, 'Trading pauses when today drops this percent from the first mark. Zero turns this off.')}"
        f"{_limit('Max contracts / symbol', 'contracts', risk.max_contracts_per_symbol, 'Most contracts one symbol may use. A draft above this is blocked.')}"
        f"{_limit('Max position / symbol', 'position', risk.max_position_per_symbol, 'Ticker currency. One contract is the latest price times the multiplier. A total within 10% still fits. When one contract is already above this, the contract limit is used. Zero turns this off.')}"
        f"{_limit('Max orders / day', 'max_orders', risk.max_orders_per_day, 'New orders stop for the day after this many. A profile that differs here is custom.')}"
        f"{_limit('Min DTE new', 'min_dte', risk.min_dte_new, 'A new open is blocked when days to expiry are below this.')}"
        f"{_limit('Seller margin budget %', 'margin', risk.max_short_opt_margin_pct, 'Short-option margin may not exceed this percent of net liquidation.')}"
        f"{_limit('Earnings blackout days', 'blackout', risk.earnings_blackout_days, 'No new open within this many days of earnings.')}"
        f"{_limit('Max bucket % NAV', 'max_bucket', risk.max_bucket_pct_nav, 'Capital in one classifier bucket may not exceed this share of net liquidation. A name sits in every bucket it carries.')}"
        f"{_limit('Max assignment % NAV', 'max_assign', risk.max_assign_pct_nav, 'Short puts that are likely to be assigned may not exceed this share of net liquidation.')}"
        f"{_limit('Min cash after assignment %', 'min_cash_after', risk.min_cash_after_assign_pct, 'Cash left after those likely assignments, as a percent of net liquidation.')}"
        f"{_limit('Assignment delta', 'assign_delta', risk.assign_delta, 'A short put counts as a likely assignment when its absolute delta is at least this. Delta is a proxy, not a probability.', step='0.01')}"
        "</div></div>"
        '<div class="risk-stack risk-span-1">'
        '<div class="card"><h2>Approval privilege'
        '<span>'
        '<button class="help-q" type="button" data-cmd="help-q" aria-label="About privilege" '
        'data-help="Privilege stays all_orders. The other choices are visible and do not write.">?</button>'
        '<button class="more" type="button" data-cmd="view-book" data-panel="risk-versions" data-title="Risk versions" data-wide>version history →</button>'
        '</span></h2>'
        f'<div class="seg stack">{"".join(priv)}</div>'
        f'<template id="risk-versions">{versions}</template></div>'
        f"{_conflict_card(risk.strict_conflict)}"
        "</div></form>"
        "</div>"
    )
    return _html(
        "Risk & Strategy",
        "Pick a profile to fill the limits, then save. Strategy knobs are on this page. Privilege does not change.",
        body,
        [("info", risk.profile.upper()), ("", f"V{current.id}")],
    )


def _settings(conn, account: str, query: dict, note: str = "") -> dict:
    if not account:
        return _html("Settings", "No account is configured.", '<div class="card"><p>No account is configured.</p></div>', [])
    current = load_active(conn, account)
    news = current.news
    mode = trading_mode().upper()
    key_ready = bool(itick_key(current))
    dsh_ready = bool(os.environ.get("DSH_BIN", "").strip())
    token_ready = bool(bot_token(current))
    note_html = f'<p class="note">{escape(note)}</p>' if note else ""
    body = (
        f"{note_html}"
        '<form id="settings-form" method="post" action="/settings" class="fill">'
        '<button type="submit" hidden></button>'
        '<div class="settings-cols"><div class="settings-col">'
        f"{_book_card()}"
        '<div class="card"><h2>Global currency</h2>'
        f"{_field('Currency', 'Display currency for net liquidation, premium, and cash. Ticker prices stay in the market currency.', f'<select class=\"input\" name=\"global_currency\">{_currency_options(current.global_currency)}</select>')}"
        "</div>"
        '<div class="card"><h2>IB</h2><div class="scroll-y">'
        f"{_row('Gateway host', _tag(_env('IB_GATEWAY_HOST', '127.0.0.1')), 'Address of the logged-in Gateway. It stays in the environment.')}"
        f"{_row('Gateway port', _tag(_env('IB_GATEWAY_PORT', '4002')), '4002 is paper. 4001 is live. Athena does not switch the port.')}"
        f"{_row('Client id', _tag(_env('IB_CLIENT_ID', '1')), 'The Athena session uses this id. Probes and history use another id.')}"
        f"{_row('Trading mode', _tag(mode, 'amber'), 'Paper or live, from the environment. The other book stays stored and hidden.')}"
        f"{_limit('Cycle minutes', 'cycle_minutes', current.cycle_minutes, 'Minutes between cycles. A wait already running keeps its clock. The next wait uses this.')}"
        '<p class="slot-note">Host, port, and client id stay on the logged-in Gateway. Slot windows are on the cockpit.</p></div></div>'
        '<div class="card fill"><h2>AI</h2><div class="scroll-y">'
        '<p class="card-lead">Scores news sentiment. Does not place orders.</p>'
        f"{_field('Provider', 'DSH runs the local binary. The others call that provider.', f'<select class=\"input\" name=\"ai_provider\">{_ai_provider_options(current.ai.provider)}</select>')}"
        f"{_field('Model', 'The model sent with each AI call. The list follows the provider.', f'<select class=\"input\" name=\"model\">{_model_options(current)}</select>')}"
        f'<script type="application/json" id="ai-models">{json.dumps({key: list(names) for key, names in MODELS.items()})}</script>'
        f"{_row('DSH binary', _tag('SET', 'green') if dsh_ready else _tag('EMPTY'), 'Whether DSH_BIN is set on this machine.')}"
        f'<div id="credential-block"{"" if current.ai.provider != "DSH" else " hidden"}>'
        f"{_field('Credential token', 'The middle stays hidden. Clear the field to remove the token.', _credential_input(current))}"
        f'<p class="slot-note" id="credential-help">{escape(_credential_help(current.ai.provider))}</p>'
        f'<script type="application/json" id="credential-masks">{json.dumps(_credential_masks(current))}</script>'
        "</div></div></div></div>"
        '<div class="settings-col">'
        f"{_sim_card(conn, current)}"
        '<div class="card"><h2>Market data</h2><div class="scroll-y">'
        '<p class="card-lead">iTick for live quotes. IB is used for history and orders inside Athena slots.</p>'
        f"{_field('iTick URL', 'Base URL for live quotes.', f'<input class=\"input\" name=\"itick_url\" required value=\"{escape(current.itick.base_url)}\">')}"
        f"{_row('API token', _tag('SET', 'green') if key_ready else _tag('EMPTY'), 'Whether an iTick token is stored.')}"
        f"{_field('Token', 'Leave blank to keep the current iTick token. Clear token removes it.', '<input class=\"input\" name=\"itick_key\" type=\"password\" autocomplete=\"off\" placeholder=\"Leave blank to keep\">')}"
        '<button class="btn" type="submit" name="action" value="clear-itick" data-explicit>Clear token</button>'
        f"{_row('Watchlist', _tag(str(len(current.watchlist))), 'How many symbols the universe is watching.')}</div></div>"
        '<div class="card fill"><h2>Notify</h2><div class="scroll-y">'
        f"{_limit('Draft TTL minutes', 'draft_ttl', current.notify.draft_ttl_minutes, 'How long a pending draft and its control link stay open.')}"
        f"{_telegram_block(current, token_ready)}"
        f"{_row('Bot token', _tag('SET', 'green') if token_ready else _tag('EMPTY'), 'Whether a Telegram bot token is stored or inherited.')}"
        f"{_field('Bot username', 'Telegram bot name without the @. Used to build the link.', f'<input class=\"input\" name=\"bot_username\" value=\"{escape(bot_username(current))}\" placeholder=\"athena_hk_bot\">')}"
        f"{_field('Bot token', 'Leave blank to keep the current bot token. Clear bot token removes it.', '<input class=\"input\" name=\"bot_token\" type=\"password\" autocomplete=\"off\" placeholder=\"Leave blank to keep\">')}"
        '<button class="btn" type="submit" name="action" value="clear-bot" data-explicit>Clear bot token</button></div></div></div>'
        '<div class="settings-col">'
        f"{_developer_card(current)}"
        '<div class="card fill"><h2>News</h2><div class="scroll-y">'
        f"{_switch('Yahoo', 'yahoo', news.yahoo, 'Pull Yahoo headlines for the watchlist.')}"
        f"{_switch('Finviz', 'finviz', news.finviz, 'Pull Finviz headlines for the watchlist.')}"
        f"{_switch('Fed calendar', 'fed_calendar', news.fed_calendar, 'Watch the Fed calendar for nearby events.')}"
        f"{_switch('MSN', 'msn', news.msn, 'Pull MSN headlines. Off unless you turn it on.')}"
        f"{_switch('Manual forward', 'manual_forward', news.manual_forward, 'Accept a page you paste in.')}"
        f"{_limit('Poll seconds', 'poll_seconds', news.poll_seconds, 'How often the news sources are checked.')}"
        f"{_limit('Wash count', 'wash_min_negative', news.wash_min_negative, 'How many negative headlines count as a wash.')}"
        f"{_limit('Wash window days', 'wash_window_days', news.wash_window_days, 'Those headlines must fall inside this many days.')}</div></div>"
        "</div></div>"
        "</form>"
    )
    return _html(
        "Settings",
        "Changes save on their own. A blank secret keeps the current one.",
        body,
        [],
    )


def _post_draft(conn, account: str, form: dict, actor: str, allowlist: set[str]) -> dict:
    draft_id = (form.get("draft_id") or [""])[0]
    decision = (form.get("decision") or [""])[0]
    try:
        if decision == "amend":
            apply_amend(conn, draft_id, _amend_from_form(form))
            conn.commit()
            note = f"Amended {draft_id}"
        elif decision == "approve":
            decide(conn, draft_id, "approve", actor, allowlist)
            note = f"Approved {draft_id}"
        elif decision == "reject":
            decide(conn, draft_id, "reject", actor, allowlist, (form.get("reason") or [""])[0])
            note = f"Rejected {draft_id}"
        else:
            note = "Unknown action"
            return _drafts(conn, account, {}, note) | {"status": 400}
    except (PermissionError, ValueError, AmendError) as exc:
        failed = _drafts(conn, account, {}, str(exc))
        failed["status"] = 400
        return failed
    return _drafts(conn, account, {}, note)


def _post_news(conn, account: str, form: dict) -> dict:
    action = (form.get("action") or [""])[0]
    if action == "dismiss":
        fingerprint = (form.get("fingerprint") or [""])[0]
        ok = dismiss_event(conn, fingerprint)
        return _news(conn, account, {}, "Marked false alarm." if ok else "That headline was already excluded.")
    if action == "ingest_url":
        settings = load_active(conn, account) if account else None
        symbols = list(settings.watchlist) if settings else []
        model = scoring_model(settings) if settings else "deepseek-flash"
        try:
            note = ingest_url(
                conn,
                (form.get("url") or [""])[0],
                symbols,
                model,
                settings.ai.provider if settings else "DSH",
                deepseek_token(settings) if settings else "",
            )
        except ValueError as exc:
            return _news(conn, account, {}, str(exc)) | {"status": 400}
        return _news(conn, account, {}, note)
    failed = _news(conn, account, {}, "Unknown action")
    failed["status"] = 400
    return failed


def _pool_patch(ok: bool, message: str, html: str = "", removed: bool = False, count: int = 0, status: int = 200) -> dict:
    return {
        "status": status,
        "kind": "json",
        "body": json.dumps({"ok": ok, "message": message, "html": html, "removed": removed, "count": count}),
        "title": "",
        "lead": "",
        "chips": [],
    }


def _pool_row(conn, account: str, settings, symbol: str) -> str:
    last, change, tone = _closes(conn, symbol)
    band, high_pct, low_pct = _year_band(conn, symbol)
    phase, _tags, _tactic = _phase_row(conn, account, symbol)
    name, cap, classifiers, thesis = _pool_profile(conn, account, symbol)
    rank = _rank_text(conn, symbol, settings.strategy.ivr_min_samples)
    enabled = symbol.upper() not in {item.upper() for item in settings.blocked_symbols}
    flags = settings.tactic_flags.get(symbol.upper(), TacticFlags())
    faded = "" if enabled else ' style="opacity:.45"'
    phase_html = f'<span class="tag">{escape(phase)}</span>' if phase else DASH
    price_sort = "" if last == DASH else last
    cap_sort = "" if cap is None else format(Decimal(cap), "f")
    return (
        f"<tr{faded}"
        f' data-symbol="{escape(symbol, quote=True)}"'
        f' data-name="{escape(name, quote=True)}"'
        f' data-price="{escape(price_sort, quote=True)}"'
        f' data-cap="{escape(cap_sort, quote=True)}"'
        f' data-classifiers="{escape(classifiers, quote=True)}">'
        f"<td><b>{escape(symbol)}</b><div class=\"sym-name\">{escape(name) if name else DASH}</div></td>"
        f"<td class=\"mono\" style=\"font-weight:600\">{escape(last)}</td>"
        f"<td class=\"mono {tone}\">{escape(change)}</td>"
        f"{_band_cell(band, high_pct, low_pct)}"
        f"<td class=\"mono\" style=\"color:var(--site-text-3)\">{escape(_cap_text(cap))}</td>"
        f"<td>{_classifier_badges(classifiers)}</td>"
        f"<td class=\"mono\">{escape(rank)}</td>"
        f"<td>{phase_html}</td>"
        f"<td>{_intent_select(symbol, flags.intent)}</td>"
        f"<td>{_toggle(symbol, 'long', flags.long)}</td>"
        f"<td>{_toggle(symbol, 'may_buy', flags.may_buy is True or (flags.may_buy is None and flags.long and flags.intent != 'income'))}</td>"
        f"<td>{_toggle(symbol, 'enabled', enabled, green=True)}</td>"
        f"<td>{_toggle(symbol, 'csp', flags.csp)}</td>"
        f"<td>{_toggle(symbol, 'cc', flags.cc)}</td>"
        f"<td>{_toggle(symbol, 'strangle', flags.strangle)}</td>"
        f"<td class=\"pool-actions\">{_edit_symbol(symbol, name, cap, classifiers, thesis, last, change, rank, phase)}{_sync_symbol(symbol)}{_remove_symbol(symbol)}</td>"
        "</tr>"
    )


def _post_universe(conn, account: str, form: dict, actor: str) -> dict:
    partial = (form.get("partial") or [""])[0] == "row"

    def fail(message: str) -> dict:
        if partial:
            return _pool_patch(False, message, status=400)
        return _universe(conn, account, {}, message) | {"status": 400}

    def done(message: str, removed: bool = False) -> dict:
        if not partial:
            return _universe(conn, account, {}, message)
        current = load_active(conn, account)
        html = "" if removed else _pool_row(conn, account, current, symbol)
        return _pool_patch(True, message, html=html, removed=removed, count=len(current.watchlist))

    if not account:
        return fail("No account is configured.")
    current = load_active(conn, account)
    payload = SettingsPayload.model_validate(current.model_dump())
    symbol = (form.get("symbol") or [""])[0].strip().upper()
    action = (form.get("action") or [""])[0]
    field = (form.get("field") or [""])[0]
    if not symbol:
        return fail("Symbol is required.")
    if action == "facts":
        return _save_pool_facts(conn, account, symbol, form)
    if action == "add":
        try:
            symbol = clean_symbol(symbol)
        except ValueError as exc:
            return fail(str(exc))
        if symbol in {item.upper() for item in payload.watchlist}:
            return fail(f"{symbol} is already in the pool.")
        payload.watchlist.append(symbol)
        try:
            save_settings(conn, account, payload, f"console:{actor}", f"universe add {symbol}")
        except ValueError as exc:
            return fail(str(exc))
        return done(f"Added {symbol}.")
    elif action == "remove":
        payload.watchlist = [item for item in payload.watchlist if item.upper() != symbol]
        payload.blocked_symbols = [item for item in payload.blocked_symbols if item.upper() != symbol]
        payload.tactic_flags.pop(symbol, None)
        removed = True
    elif action == "toggle" and field == "enabled":
        blocked = {item.upper() for item in payload.blocked_symbols}
        if symbol in blocked:
            payload.blocked_symbols = [item for item in payload.blocked_symbols if item.upper() != symbol]
        else:
            payload.blocked_symbols.append(symbol)
        removed = False
    elif action == "intent":
        intent = (form.get("intent") or [""])[0]
        if intent not in {"income", "both", "accumulate"}:
            return fail("Intent must be Income, Both, or Accumulate.")
        flags = payload.tactic_flags.get(symbol, TacticFlags())
        payload.tactic_flags[symbol] = flags.model_copy(update={"intent": intent})
        removed = False
    elif action == "toggle" and field == "may_buy":
        flags = payload.tactic_flags.get(symbol, TacticFlags())
        allowed = flags.may_buy is True or (flags.may_buy is None and flags.long and flags.intent != "income")
        payload.tactic_flags[symbol] = flags.model_copy(update={"may_buy": not allowed})
        removed = False
    elif action == "toggle" and field in {"csp", "cc", "strangle", "long"}:
        flags = payload.tactic_flags.get(symbol, TacticFlags())
        payload.tactic_flags[symbol] = flags.model_copy(update={field: not getattr(flags, field)})
        removed = False
    else:
        return fail("Unknown action")
    try:
        save_settings(conn, account, payload, f"console:{actor}", f"universe {action} {symbol}")
    except ValueError as exc:
        return fail(str(exc))
    return done(f"Saved {action} {symbol}.", removed)


def _post_risk(conn, account: str, form: dict, actor: str) -> dict:
    if not account:
        return _risk(conn, account, {}, "No account is configured.") | {"status": 400}
    current = load_active(conn, account)
    if (form.get("action") or [""])[0] == "preview":
        return _preview_thresholds(conn, current, form)
    try:
        values = (
            int((form.get("max_symbol") or ["0"])[0]),
            int((form.get("cash") or ["0"])[0]),
            int((form.get("halt") or ["0"])[0]),
            int((form.get("contracts") or ["0"])[0]),
            int((form.get("min_rv") or ["0"])[0]),
            int((form.get("min_dte") or ["0"])[0]),
        )
        margin = int((form.get("margin") or ["0"])[0])
        blackout = int((form.get("blackout") or ["0"])[0])
        position = int((form.get("position") or ["0"])[0])
        orders = int((form.get("max_orders") or ["10"])[0])
    except ValueError:
        return _risk(conn, account, {}, "Limits must be whole numbers.") | {"status": 400}
    if position < 0:
        return _risk(conn, account, {}, "Max position must be zero or more.") | {"status": 400}
    try:
        bucket_cap = _kept_int(form, "max_bucket", current.risk.max_bucket_pct_nav)
        assign_cap = _kept_int(form, "max_assign", current.risk.max_assign_pct_nav)
        cash_after = _kept_int(form, "min_cash_after", current.risk.min_cash_after_assign_pct)
        assign_delta = _kept_float(form, "assign_delta", current.risk.assign_delta)
    except ValueError:
        return _risk(conn, account, {}, "Budget limits must be numbers.") | {"status": 400}
    if not 0 <= bucket_cap <= 100 or not 0 <= assign_cap <= 100 or not 0 <= cash_after <= 100:
        return _risk(conn, account, {}, "Budget percents must be 0 to 100.") | {"status": 400}
    if not 0.01 <= assign_delta <= 0.9:
        return _risk(conn, account, {}, "Assignment delta must be 0.01 to 0.9.") | {"status": 400}
    symbol, cash, halt, contracts, rv_floor, dte = values
    profile = next(
        (name for name, preset in PROFILES.items() if preset == (symbol, cash, halt, contracts, rv_floor, dte, position, orders, margin, blackout)),
        "custom",
    )
    payload = SettingsPayload.model_validate(current.model_dump())
    payload.risk = RiskLimits(
        profile=profile,  # type: ignore[arg-type]
        max_symbol_pct_nav=symbol,
        cash_reserve_pct=cash,
        daily_loss_halt_pct=halt,
        max_contracts_per_symbol=contracts,
        max_position_per_symbol=position,
        min_rv_pctl_new_short=rv_floor,
        min_dte_new=dte,
        max_short_opt_margin_pct=margin,
        earnings_blackout_days=blackout,
        max_orders_per_day=orders,
        margin_rate=current.risk.margin_rate,
        strict_conflict=_checked(form, "strict_conflict"),
        max_bucket_pct_nav=bucket_cap,
        max_assign_pct_nav=assign_cap,
        min_cash_after_assign_pct=cash_after,
        assign_delta=assign_delta,
    )
    payload.privilege = current.privilege
    strategy_ranges = {
        "roll_dte": (1, 60),
        "profit_take_pct": (1, 100),
        "ivr_high": (1, 100),
        "chain_band_pct": (1, 80),
        "otm_floor_pct": (1, 30),
        "otm_ceiling_pct": (1, 50),
        "screener_top_n": (1, 10),
        "dte_min": (1, 90),
        "dte_target": (1, 120),
        "dte_max": (1, 180),
    }
    try:
        strategy_numbers = {key: int((form.get(key) or [""])[0]) for key in strategy_ranges}
    except ValueError:
        return _risk(conn, account, {}, "Strategy knobs must be whole numbers.") | {"status": 400}
    for key, (low, high) in strategy_ranges.items():
        if not low <= strategy_numbers[key] <= high:
            return _risk(conn, account, {}, f"{key} must be {low} to {high}.") | {"status": 400}
    if not strategy_numbers["dte_min"] <= strategy_numbers["dte_target"] <= strategy_numbers["dte_max"]:
        return _risk(conn, account, {}, "DTE min, target, and max must stay in order.") | {"status": 400}
    if strategy_numbers["otm_floor_pct"] >= strategy_numbers["otm_ceiling_pct"]:
        return _risk(conn, account, {}, "OTM floor must be below the ceiling.") | {"status": 400}
    float_ranges = {
        "csp_delta_lo": (0.01, 0.9),
        "csp_delta_target": (0.01, 0.9),
        "csp_delta_hi": (0.01, 0.9),
        "cc_delta_target": (0.01, 0.9),
        "cc_rail_pos": (0.5, 0.99),
        "strangle_delta_target": (0.01, 0.9),
        "iv_rv_multiple_hi": (0.5, 5),
        "iv_rv_multiple_mid": (0.5, 5),
        "iv_rv_multiple_lo": (0.5, 5),
    }
    try:
        strategy_floats = {key: float((form.get(key) or [""])[0]) for key in float_ranges}
    except ValueError:
        return _risk(conn, account, {}, "Delta and multiple knobs must be numbers.") | {"status": 400}
    for key, (low, high) in float_ranges.items():
        if not low <= strategy_floats[key] <= high:
            return _risk(conn, account, {}, f"{key} must be {low} to {high}.") | {"status": 400}
    if not strategy_floats["csp_delta_lo"] <= strategy_floats["csp_delta_target"] <= strategy_floats["csp_delta_hi"]:
        return _risk(conn, account, {}, "CSP delta low, target, and high must stay in order.") | {"status": 400}
    try:
        ivr_min_samples = _kept_int(form, "ivr_min_samples", payload.strategy.ivr_min_samples)
        rv_window = _kept_int(form, "rv_window", payload.strategy.rv_window)
        rv_high_pctl = _kept_int(form, "rv_high_pctl", payload.strategy.rv_high_pctl)
        rv_low_pctl = _kept_int(form, "rv_low_pctl", payload.strategy.rv_low_pctl)
    except ValueError:
        return _risk(conn, account, {}, "Volatility knobs must be whole numbers.") | {"status": 400}
    if not 1 <= ivr_min_samples <= 252:
        return _risk(conn, account, {}, "Min samples must be 1 to 252.") | {"status": 400}
    if not 2 <= rv_window <= 60:
        return _risk(conn, account, {}, "RV window must be 2 to 60.") | {"status": 400}
    if not 1 <= rv_low_pctl < rv_high_pctl <= 100:
        return _risk(conn, account, {}, "RV low must be below RV high, and both must be 1 to 100.") | {"status": 400}
    payload.strategy = payload.strategy.model_copy(
        update={
            "ivr_high": strategy_numbers["ivr_high"],
            "chain_band_pct": strategy_numbers["chain_band_pct"],
            "otm_floor_pct": strategy_numbers["otm_floor_pct"],
            "otm_ceiling_pct": strategy_numbers["otm_ceiling_pct"],
            "screener_top_n": strategy_numbers["screener_top_n"],
            "roll_dte": strategy_numbers["roll_dte"],
            "profit_take_pct": strategy_numbers["profit_take_pct"],
            "dte_min": strategy_numbers["dte_min"],
            "dte_target": strategy_numbers["dte_target"],
            "dte_max": strategy_numbers["dte_max"],
            "ivr_min_samples": ivr_min_samples,
            "rv_window": rv_window,
            "rv_high_pctl": rv_high_pctl,
            "rv_low_pctl": rv_low_pctl,
            **strategy_floats,
        }
    )
    payload.privilege = current.privilege
    try:
        new_id, _changes = save_settings(conn, account, payload, f"console:{actor}", settings_note(profile, current, payload))
    except ValueError as exc:
        return _risk(conn, account, {}, str(exc)) | {"status": 400}
    return _risk(conn, account, {}, f"Saved version {new_id}. Privilege stays {payload.privilege}.")


def _post_settings(conn, account: str, form: dict, actor: str) -> dict:
    if not account:
        return _settings(conn, account, {}, "No account is configured.") | {"status": 400}
    current = load_active(conn, account)
    payload = SettingsPayload.model_validate(current.model_dump())
    action = (form.get("action") or [""])[0]
    if action == "link":
        from athena_bots.telegram import issue_link_code

        code, expires = issue_link_code()
        runtime.link_code = code
        runtime.link_expires_at = expires
        return _settings(conn, account, {}, "Open the Telegram link within 10 minutes.")
    if action == "unlink":
        payload.notify = payload.notify.model_copy(update={"telegram_user_ids": ""})
        return _save_live(conn, account, payload, actor, "unlinked telegram")
    if action in {"clear-deepseek", "clear-credential"}:
        field = CREDENTIAL_FIELD.get(normalize_provider((form.get("ai_provider") or [payload.ai.provider])[0]))
        if field:
            payload.ai = payload.ai.model_copy(update={field: ""})
        return _save_live(conn, account, payload, actor, "cleared credential token")
    if action == "clear-itick":
        payload.itick = payload.itick.model_copy(update={"api_key": ""})
        return _save_live(conn, account, payload, actor, "cleared itick token")
    if action == "clear-bot":
        payload.notify = payload.notify.model_copy(update={"bot_token": ""})
        return _save_live(conn, account, payload, actor, "cleared bot token")
    if action == "generate-api-key":
        payload.developer = payload.developer.model_copy(update={"api_key": secrets.token_urlsafe(24)})
        return _save_live(conn, account, payload, actor, "generated api key")
    if action == "clear-api-key":
        payload.developer = payload.developer.model_copy(update={"api_key": ""})
        return _save_live(conn, account, payload, actor, "cleared api key")
    ranges = {
        "wash_min_negative": (1, 20),
        "wash_window_days": (1, 14),
        "poll_seconds": (15, 600),
        "draft_ttl": (15, 240),
        "cycle_minutes": (1, 60),
    }
    try:
        numbers = {key: int((form.get(key) or [""])[0]) for key in ranges}
    except ValueError:
        return _settings(conn, account, {}, "Knobs must be whole numbers.") | {"status": 400}
    for key, (low, high) in ranges.items():
        if not low <= numbers[key] <= high:
            return _settings(conn, account, {}, f"{key} must be {low} to {high}.") | {"status": 400}
    posted_currency = (form.get("global_currency") or [""])[0].strip().upper()
    if posted_currency not in CURRENCIES:
        return _settings(conn, account, {}, "Choose a currency from the list.") | {"status": 400}
    url = (form.get("itick_url") or [""])[0].strip()
    model = (form.get("model") or [""])[0].strip()
    if not url.startswith("http"):
        return _settings(conn, account, {}, "iTick URL must start with http.") | {"status": 400}
    if not model or any(char in model for char in "\n\r "):
        return _settings(conn, account, {}, "Model name is required.") | {"status": 400}
    itick_update = {"base_url": url}
    posted_key = (form.get("itick_key") or [""])[0].strip()
    if posted_key:
        itick_update["api_key"] = posted_key
    notify_update = {
        "draft_ttl_minutes": numbers["draft_ttl"],
        "bot_username": (form.get("bot_username") or [""])[0].strip().lstrip("@"),
    }
    posted_bot = (form.get("bot_token") or [""])[0].strip()
    if posted_bot:
        notify_update["bot_token"] = posted_bot
    provider = normalize_provider((form.get("ai_provider") or ["DSH"])[0])
    if provider not in MODELS:
        return _settings(conn, account, {}, "Choose a provider from the list.") | {"status": 400}
    if model not in MODELS[provider]:
        model = MODELS[provider][0]
    payload.itick = payload.itick.model_copy(update=itick_update)
    ai_update = {"model": model, "provider": provider}
    field = CREDENTIAL_FIELD.get(provider)
    if field and "credential_token" in form:
        posted_token = (form.get("credential_token") or [""])[0].strip()
        if "•" not in posted_token:
            ai_update[field] = posted_token
    payload.ai = payload.ai.model_copy(update=ai_update)
    payload.news = payload.news.model_copy(
        update={
            "yahoo": _checked(form, "yahoo"),
            "finviz": _checked(form, "finviz"),
            "fed_calendar": _checked(form, "fed_calendar"),
            "msn": _checked(form, "msn"),
            "manual_forward": _checked(form, "manual_forward"),
            "poll_seconds": numbers["poll_seconds"],
            "wash_min_negative": numbers["wash_min_negative"],
            "wash_window_days": numbers["wash_window_days"],
        }
    )
    payload.notify = payload.notify.model_copy(update=notify_update)
    payload.global_currency = posted_currency
    payload.cycle_minutes = numbers["cycle_minutes"]
    payload.privilege = current.privilege
    payload.sim_symbols = sim_targets(
        form.get("sim_symbol", []),
        current.watchlist,
        current.sim_symbols,
    )
    return _save_live(conn, account, payload, actor, "saved settings")


_CYCLE_HELP = (
    "They are not one chain. Location and News run together and only emit tags. "
    "CSP, Strangle, Purchase, and Call each read that same tag set. They do not call one another. "
    "Call is the exception: it also sees the Purchase proposal, and stays quiet until a share purchase fills. "
    "The four proposals then join. The consolidator, with thinking on, is the only agent that chooses the drafts. "
    "Universe flags, risk caps, and Approve still apply."
)


def _chain_svg() -> str:
    candidate = Path(__file__).resolve().parents[4] / "design" / "14-strategy" / "chain.svg"
    if candidate.is_file():
        return candidate.read_text()
    return ""


def _strategy(conn, account: str, query: dict, note: str = "") -> dict:
    settings = load_active(conn, account) if account else None
    prompts = resolved_agents(settings) if settings else resolved_agents(SettingsPayload(risk=RiskLimits(
        profile="standard", max_symbol_pct_nav=20, cash_reserve_pct=30, daily_loss_halt_pct=5,
        max_contracts_per_symbol=3, min_rv_pctl_new_short=50, min_dte_new=7,
        max_short_opt_margin_pct=50, earnings_blackout_days=7,
    )))
    cards = []
    side = ""
    for name in AGENT_ORDER:
        prompt = prompts[name]
        open_card = name == "Consolidator"
        fold = "" if open_card else (
            '<button class="fold" type="button" data-cmd="fold-agent" aria-expanded="false" '
            f'aria-label="Expand {escape(name, quote=True)}">'
            '<svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">'
            '<path d="M2 4.5 L6 8.5 L10 4.5" fill="none" stroke="currentColor" stroke-width="1.4"/>'
            "</svg></button>"
        )
        card = (
            f'<form class="card agent{" collapsed" if not open_card else ""}" method="post" action="/orchestration">'
            f"<h2><span>{escape(name)}</span>{fold}</h2>"
            '<div class="body">'
            f'<p class="role">{escape(AGENT_ROLES[name])}</p>'
            f'<input type="hidden" name="agent" value="{escape(name, quote=True)}">'
            '<label class="prompt">System prompt'
            f'<textarea class="prompt" name="system">{escape(prompt.system)}</textarea></label>'
            '<label class="prompt">Execution prompt'
            f'<textarea class="prompt" name="execution">{escape(prompt.execution)}</textarea></label>'
            '<div class="toggle-row"><div>Reasoning</div><div class="toggle-end">'
            '<input type="hidden" name="reasoning" value="0">'
            f'<input type="checkbox" name="reasoning" value="1"{" checked" if prompt.reasoning else ""}>'
            "</div></div>"
            '<p class="slot-note">When on, the provider uses its medium reasoning setting.</p>'
            '<button class="btn primary" type="submit">Save prompts</button>'
            "</div></form>"
        )
        if name == "Consolidator":
            side = card
        else:
            cards.append(card)
    diagram = _chain_svg()
    picture = f'<div class="flow-diagram">{diagram}</div>' if diagram else ""
    note_html = f'<p class="note">{escape(note)}</p>' if note else ""
    body = (
        f"{note_html}"
        '<div class="scroll-y strategy-scroll">'
        '<div class="cycle-split">'
        '<div class="card flow"><h2><span>How a cycle runs '
        '<button class="help-q" type="button" data-cmd="help-q" aria-label="How a cycle runs" '
        f'data-help="{escape(_CYCLE_HELP, quote=True)}">?</button></span></h2>'
        f"{picture}</div>"
        f"{side}"
        "</div>"
        f'<div class="agent-grid">{"".join(cards)}</div>'
        "</div>"
    )
    return _html(
        "Orchestration",
        "The orchestration blueprint. Edit the system prompt and the execution prompt for each agent. Income or Accumulate, and Long, are set per symbol on Universe.",
        body,
        [("info", "7 AGENTS")],
    )


def _post_strategy(conn, account: str, form: dict, actor: str) -> dict:
    if not account:
        return _strategy(conn, account, {}, "No account is configured.") | {"status": 400}
    name = (form.get("agent") or [""])[0]
    if name not in AGENT_ORDER:
        return _strategy(conn, account, {}, "Unknown agent.") | {"status": 400}
    system = (form.get("system") or [""])[0].strip()
    execution = (form.get("execution") or [""])[0].strip()
    if not system or not execution:
        return _strategy(conn, account, {}, "Both prompts are required.") | {"status": 400}
    current = load_active(conn, account)
    payload = SettingsPayload.model_validate(current.model_dump())
    prompts = resolved_agents(payload)
    prompts[name] = prompts[name].model_copy(
        update={"system": system, "execution": execution, "reasoning": _checked(form, "reasoning")}
    )
    payload.agents = prompts
    try:
        save_settings(conn, account, payload, f"console:{actor}", f"strategy {name}")
    except ValueError as exc:
        if str(exc) == "no change":
            return _strategy(conn, account, {})
        return _strategy(conn, account, {}, str(exc)) | {"status": 400}
    return _strategy(conn, account, {}, f"Saved {name} prompts.")


def _audit(conn, account: str, query: dict) -> dict:
    offset = _offset(query)
    rows, more, nxt = _audit_page(conn, account, offset)
    rendered = "".join(_audit_tr(row) for row in rows)
    if not rendered and offset == 0:
        rendered = '<tr><td colspan="9">No audit rows yet.</td></tr>'
    if _wants_rows(query):
        return _fragment(rendered, more, nxt)
    calls, decisions, failed = _audit_counts(conn, account)
    filters = ['<button class="on" type="button" data-agent="all">All</button>']
    filters.append('<button type="button" data-agent="Decision">Decision</button>')
    filters.append('<button type="button" data-agent="Universe">Universe</button>')
    for name in AGENT_ORDER:
        filters.append(f'<button type="button" data-agent="{escape(name, quote=True)}">{escape(name)}</button>')
    chips = [("info", f"{calls} CALLS")]
    if decisions:
        chips.append(("", f"{decisions} DECISIONS"))
    if failed:
        chips.append(("warn", f"{failed} FAILED"))
    table = (
        '<table class="data" id="audit-table"><thead><tr>'
        "<th>Time</th><th>Agent</th><th>Symbol</th><th>Status</th><th>Thinking</th>"
        "<th>Model</th><th>Latency</th><th>Tokens</th><th>Result</th>"
        f"</tr></thead><tbody>{rendered}</tbody></table>"
    )
    body = (
        '<div class="card fill"><h2>Calls and decisions</h2>'
        '<div class="filters" id="audit-filters">'
        '<input class="input" id="audit-keyword" type="search" placeholder="Keyword" autocomplete="off" spellcheck="false">'
        f'{"".join(filters)}</div>'
        f"{_feed('/audit', nxt, more, table)}</div>"
    )
    return _html(
        "Audit",
        "Model calls and the decision trail for this account. Open a row for the reply or the reasons. This page does not approve orders.",
        body,
        chips,
        _REFRESH,
    )


def _audit_tr(row: dict) -> str:
    blob = escape(json.dumps(row), quote=True)
    model = escape(row["model"]) if row.get("model") else DASH
    return (
        f'<tr class="hit" data-cmd="audit-row" data-json="{blob}">'
        f'<td class="mono">{escape(row["time"])}</td>'
        f'<td>{_audit_agent_tag(row["agent"])}</td>'
        f'<td><b>{escape(row["symbol"])}</b>'
        + (' <span class="tag amber">SIM</span>' if row.get("simulated") else "")
        + "</td>"
        f'<td>{_audit_status_tag(row["status"])}</td>'
        f'<td>{_audit_thinking_tag(row.get("thinking") or "", row.get("thinking_text") or "")}</td>'
        f'<td class="mono">{model}</td>'
        f'<td class="mono">{escape(row["latency"])}</td>'
        f'<td class="mono">{escape(row["tokens"])}</td>'
        f'<td>{_audit_result_tag(row["result"], row["status"])}</td>'
        "</tr>"
    )


def _audit_shown(text: str) -> str:
    """Quiet is a status word. The table and the stored line use the same spelling."""
    if (text or "").strip().lower() == "quiet":
        return "QUIET"
    return text or ""


_AUDIT_AGENTS = {
    "Location": "blue",
    "News": "teal",
    "CSP": "green",
    "Call": "amber",
    "Purchase": "violet",
    "Strangle": "red",
    "Consolidator": "blue",
    "Universe": "teal",
    "Decision": "amber",
}
_AUDIT_STATUS = {
    "ok": ("OK", "green"),
    "error": ("ERROR", "red"),
    "quiet": ("QUIET", ""),
    "drafted": ("DRAFTED", "green"),
    "dropped": ("DROPPED", "red"),
}


def _audit_agent_tag(agent: str) -> str:
    return _tag(agent, _AUDIT_AGENTS.get(agent, ""))


def _audit_status_label(status: str) -> str:
    label, _kind = _AUDIT_STATUS.get((status or "").lower(), ((status or "").upper(), ""))
    return label


def _audit_status_tag(status: str) -> str:
    label, kind = _AUDIT_STATUS.get((status or "").lower(), ((status or "").upper(), ""))
    return _tag(label, kind)


def _audit_thinking_tag(thinking: str, note: str) -> str:
    if thinking == "on" or note:
        return _tag("ON", "violet")
    if thinking == "off":
        return _tag("OFF")
    return DASH


def _audit_result_tag(result: str, status: str) -> str:
    shown = _audit_shown(result)
    state = (status or "").lower()
    key = shown.strip().lower()
    if key == "quiet":
        kind = ""
    elif state in {"error", "dropped"}:
        kind = "red"
    elif "wash" in key:
        kind = "amber"
    elif state in {"ok", "drafted"}:
        kind = "green"
    else:
        kind = "blue"
    return _tag(shown, kind)


def _audit_counts(conn, account: str) -> tuple[int, int, int]:
    if not account:
        return 0, 0, 0
    calls = conn.execute("SELECT count(*) FROM ai_calls WHERE account_id = %s", (account,)).fetchone()[0]
    decisions = conn.execute(
        "SELECT count(*) FROM decision_trails WHERE account_id = %s", (account,)
    ).fetchone()[0]
    failed = conn.execute(
        "SELECT count(*) FROM ai_calls WHERE account_id = %s AND status <> 'ok'", (account,)
    ).fetchone()[0]
    return int(calls), int(decisions), int(failed)


def _audit_page(conn, account: str, offset: int) -> tuple[list[dict], bool, int]:
    if not account:
        return [], False, 0
    need = offset + _PAGE + 1
    rows = _audit_rows(conn, account, need)
    chunk, more, nxt = _window(rows, offset)
    return chunk, more, nxt


def _audit_rows(conn, account: str, limit: int) -> list[dict]:
    if not account or limit <= 0:
        return []
    rows = []
    stored = conn.execute(
        """
        SELECT called_at, agent, symbol, model, provider, thinking,
               latency_ms, tokens, status, result, response, request, simulated
        FROM ai_calls
        WHERE account_id = %s
        ORDER BY called_at DESC
        LIMIT %s
        """,
        (account, limit),
    ).fetchall()
    for called, agent, symbol, model, provider, thinking, latency_ms, tokens, status, result, response, request, simulated in stored:
        when = called.astimezone(HKT) if called.tzinfo else called
        payload = response if isinstance(response, dict) else {}
        rows.append({
            "_at": when,
            "kind": "model",
            "time": when.strftime("%m-%d %H:%M:%S"),
            "agent": agent,
            "symbol": symbol,
            "model": model,
            "provider": provider,
            "thinking": thinking,
            "latency": f"{(latency_ms or 0) / 1000:.1f}s",
            "tokens": f"{tokens or 0:,}",
            "status": _audit_status_label(status),
            "result": _audit_shown(result),
            "json": payload,
            "prompt": request if isinstance(request, dict) else {},
            "simulated": bool(simulated),
        })
    decisions = conn.execute(
        """
        SELECT logged_at, symbol, source, outcome, summary, steps, simulated
        FROM decision_trails
        WHERE account_id = %s
        ORDER BY logged_at DESC
        LIMIT %s
        """,
        (account, limit),
    ).fetchall()
    for logged, symbol, source, outcome, summary, steps, simulated in decisions:
        when = logged.astimezone(HKT) if logged.tzinfo else logged
        payload = steps if isinstance(steps, dict) else {}
        items = payload.get("items") if isinstance(payload.get("items"), list) else []
        rows.append({
            "_at": when,
            "kind": "decision",
            "time": when.strftime("%m-%d %H:%M:%S"),
            "agent": "Decision",
            "symbol": symbol,
            "source": source,
            "model": "",
            "thinking": "on" if (payload.get("thinking") or "") else "",
            "latency": "—",
            "tokens": "—",
            "status": _audit_status_label(outcome),
            "result": _audit_shown(summary),
            "phase": payload.get("phase") or "",
            "intent": payload.get("intent") or "",
            "long": payload.get("long") or "",
            "flags": payload.get("flags") or "",
            "tags": payload.get("tags") or "",
            "thinking_text": payload.get("thinking") or "",
            "steps": items,
            "simulated": bool(simulated),
        })
    rows.sort(key=lambda item: item["_at"], reverse=True)
    for item in rows:
        item.pop("_at", None)
    return rows


def _intent_select(symbol: str, intent: str) -> str:
    options = []
    for value, label in (("income", "Income"), ("both", "Both"), ("accumulate", "Accumulate")):
        selected = " selected" if intent == value else ""
        options.append(f'<option value="{value}"{selected}>{label}</option>')
    return (
        '<form method="post" action="/universe">'
        f'<input type="hidden" name="symbol" value="{escape(symbol, quote=True)}">'
        '<input type="hidden" name="action" value="intent">'
        f'<select class="input intent" name="intent" aria-label="Intent for {escape(symbol, quote=True)}">'
        f'{"".join(options)}</select></form>'
    )


_PAGE = 50
_REFRESH = (
    '<button class="btn ghost icon-btn" type="button" data-cmd="refresh-list" aria-label="Refresh">'
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">'
    '<path d="M21 12a9 9 0 1 1-2.6-6.3"/><path d="M21 3v6h-6"/></svg></button>'
)


def _offset(query: dict) -> int:
    raw = (query.get("offset") or ["0"])[0]
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _wants_rows(query: dict) -> bool:
    return (query.get("partial") or [""])[0] == "rows"


def _window(items: list, offset: int) -> tuple[list, bool, int]:
    chunk = items[offset:offset + _PAGE]
    return chunk, offset + _PAGE < len(items), offset + len(chunk)


def _fragment(html: str, more: bool, nxt: int) -> dict:
    return {
        "status": 200,
        "kind": "json",
        "body": json.dumps({"html": html, "more": more, "next": nxt}),
        "title": "",
        "lead": "",
        "chips": [],
    }


def _feed(path: str, nxt: int, more: bool, inner: str) -> str:
    flag = "1" if more else "0"
    return (
        f'<div class="scroll-y" data-feed="{escape(path, quote=True)}" '
        f'data-offset="{nxt}" data-more="{flag}">{inner}</div>'
    )


def _html(title: str, lead: str, body: str, chips: list[tuple[str, str]], actions: str = "") -> dict:
    return {"status": 200, "kind": "html", "title": title, "lead": lead, "body": body, "chips": chips, "actions": actions}


def _account() -> str:
    accounts = active_accounts()
    return accounts[0] if accounts else ""


def _contract_line(conn, draft_id: str) -> dict:
    from athena_strategy.drafts import draft_facts

    lines = draft_facts(conn, draft_id)
    kept = [line for line in lines if line.startswith(("Expiry ", "Put ", "Call ", "Limit ", "Class ", "Cancels"))]
    return {"contract": ", ".join(kept) if kept else "No contract line yet", "lines": lines}


def _minutes_left(created: datetime, ttl: int) -> int:
    elapsed = (datetime.now(created.tzinfo or UTC) - created).total_seconds() / 60
    return max(0, int(ttl - elapsed))


def _symbol_tags(conn, account: str, symbol: str) -> list[str]:
    row = conn.execute(
        "SELECT tags FROM symbol_phases WHERE account_id = %s AND symbol = %s",
        (account, symbol),
    ).fetchone()
    if row is None or not row[0]:
        return []
    return [part for part in row[0].split(",") if part and part not in {"P1", "P2", "P3", "P4", "P5"}]


def _live_link(conn, draft_id: str) -> str:
    row = conn.execute(
        """
        SELECT token FROM control_links
        WHERE draft_id = %s AND used_at IS NULL AND expires_at > now()
        ORDER BY expires_at DESC
        LIMIT 1
        """,
        (draft_id,),
    ).fetchone()
    if row is None:
        return ""
    return control_page_url(row[0])


def _nearest_offset(conn, symbols: list[str], settings: SettingsPayload) -> int | None:
    today = datetime.now(HKT).date()
    found = []
    for symbol in symbols:
        offset = calendar_offset(conn, symbol, today, settings)
        if offset is not None:
            found.append(offset)
    if not found:
        return None
    return min(found, key=lambda days: (abs(days), days))


def _phase_name(offset: int | None, strategy) -> str:
    if offset is None:
        return ""
    if offset == 0:
        return "P3"
    if strategy.p2_from <= offset <= strategy.p2_to:
        return "P2"
    if strategy.p1_from <= offset <= strategy.p1_to:
        return "P1"
    if strategy.p4_from <= offset <= strategy.p4_to:
        return "P4"
    if strategy.p5_from <= offset <= strategy.p5_to:
        return "P5"
    return ""


def _phase_window(name: str, strategy) -> str:
    windows = {
        "P1": (strategy.p1_from, strategy.p1_to),
        "P2": (strategy.p2_from, strategy.p2_to),
        "P3": (0, 0),
        "P4": (strategy.p4_from, strategy.p4_to),
        "P5": (strategy.p5_from, strategy.p5_to),
    }
    start, end = windows[name]
    if start == end:
        return "T day"
    return f"T{start:+d}…T{end:+d}"


def _iv_series(conn, symbol: str, minimum: int) -> tuple[list[float], int | None]:
    if not symbol:
        return [], None
    rows = conn.execute(
        """
        SELECT iv_proxy FROM iv_marks
        WHERE symbol = %s AND method = %s
        ORDER BY day DESC LIMIT 26
        """,
        (symbol, PREMIUM_METHOD),
    ).fetchall()
    samples = [Decimal(row[0]) for row in reversed(rows)]
    series = _rank_points(samples)
    if len(series) == 1:
        series = series + series
    rank = iv_rank(samples, samples[-1], minimum) if samples else None
    return series, rank


def _phase_counts(conn, account: str) -> dict[str, int]:
    if not account:
        return {}
    rows = conn.execute(
        "SELECT phase, count(*) FROM symbol_phases WHERE account_id = %s GROUP BY phase",
        (account,),
    ).fetchall()
    return {phase: count for phase, count in rows}


def _phase_table(conn, account: str, symbols: list[str], settings, selected: str = "") -> str:
    rows = []
    window_days = settings.news.wash_window_days if settings else 3
    below = settings.news.wash_sentiment_below if settings else -0.3
    start = datetime.now(UTC) - timedelta(days=window_days)
    minimum = settings.strategy.ivr_min_samples if settings else 20
    for symbol in symbols:
        phase, tags, tactic = _phase_row(conn, account, symbol)
        negatives = conn.execute(
            """
            SELECT count(*) FROM news_events
            WHERE symbol = %s AND sentiment < %s AND ts >= %s AND dismissed = false
            """,
            (symbol, below, start),
        ).fetchone()[0]
        draft = conn.execute(
            """
            SELECT id FROM drafts
            WHERE account_id = %s AND symbol = %s AND status = 'pending'
            ORDER BY created_at DESC LIMIT 1
            """,
            (account, symbol),
        ).fetchone()
        link = (
            f'<a class="draft-link" href="/drafts">{escape(draft[0])}</a>'
            if draft
            else DASH
        )
        chosen = " selected" if symbol == selected else ""
        rows.append(
            f'<tr class="phase-pick{chosen}" data-href="/phase?symbol={quote(symbol)}"'
            f' tabindex="0" aria-selected="{"true" if symbol == selected else "false"}">'
            f"<td><b>{escape(symbol)}</b></td>"
            f"<td>{_phase_badge(phase)}</td>"
            f"<td class=\"mono\">{escape(_rank_text(conn, symbol, minimum))}</td>"
            f"<td>{negatives} neg / {window_days}d</td>"
            f"<td>{_tactic_tag(tactic) if tactic else DASH}</td>"
            f"<td>{link}</td></tr>"
        )
    body = "".join(rows) or "<tr><td colspan=\"6\">The watchlist is empty.</td></tr>"
    return (
        "<table class=\"data\"><thead><tr><th>Symbol</th><th>Phase</th><th>IV rank</th>"
        f"<th>Negative headlines</th><th>Tactic</th><th>Draft</th></tr></thead><tbody>{body}</tbody></table>"
    )


def _phase_row(conn, account: str, symbol: str) -> tuple[str, str, str]:
    if not account:
        return "", "", ""
    row = conn.execute(
        "SELECT phase, tags, tactic FROM symbol_phases WHERE account_id = %s AND symbol = %s",
        (account, symbol),
    ).fetchone()
    if row is None:
        return "", "", ""
    return row[0], row[1] or "", row[2] or ""


def _rank_text(conn, symbol: str, minimum: int) -> str:
    _series, rank = _iv_series(conn, symbol, minimum)
    return DASH if rank is None else str(rank)


def _weekly_negatives(conn, below: float) -> list[int]:
    rows = conn.execute(
        """
        SELECT date_trunc('week', ts) AS week, count(*)
        FROM news_events
        WHERE sentiment < %s AND dismissed = false AND ts >= now() - interval '12 weeks'
        GROUP BY week
        ORDER BY week
        """,
        (below,),
    ).fetchall()
    counts = {row[0].date(): row[1] for row in rows}
    today = datetime.now(UTC).date()
    monday = today - timedelta(days=today.weekday())
    weeks = []
    for index in range(11, -1, -1):
        week = monday - timedelta(weeks=index)
        weeks.append(int(counts.get(week, 0)))
    return weeks


def _source_counts(conn) -> list[tuple[str, int]]:
    rows = conn.execute(
        """
        SELECT source, count(*) FROM news_events
        WHERE dismissed = false
        GROUP BY source
        ORDER BY count(*) DESC, source
        """
    ).fetchall()
    if not rows:
        return [("none", 0)]
    return [(row[0], row[1]) for row in rows]


def _sentiment_parts(value) -> tuple[str, str]:
    if value is None:
        return "", DASH
    score = Decimal(value)
    if score < 0:
        return "chg-down", f"−{abs(score):.2f}"
    if score > 0:
        return "chg-up", f"+{score:.2f}"
    return "", "0.00"


def _type_badge(event_type: str) -> str:
    code = (event_type or "GENERIC").upper()
    badge = NEWS_TYPE_BADGE.get(code, "")
    meaning = NEWS_TYPE_MEANING.get(code, "Outside the news type glossary.")
    klass = f"tag {badge}".strip()
    return f'<span class="{klass}" title="{escape(meaning, quote=True)}">{escape(code)}</span>'


def _news_table(rows: str) -> str:
    return (
        "<table class=\"data\"><thead><tr>"
        "<th>When</th><th>Symbol</th><th>Source</th><th>Type</th><th>Sentiment</th><th>Headline</th><th></th>"
        f"</tr></thead><tbody>{rows}</tbody></table>"
    )


def _news_rows(conn, offset: int) -> tuple[str, bool, int]:
    rows = conn.execute(
        """
        SELECT fingerprint, ts, symbol, source, event_type, sentiment, title, dismissed
        FROM news_events
        ORDER BY ts DESC
        LIMIT %s OFFSET %s
        """,
        (_PAGE + 1, offset),
    ).fetchall()
    more = len(rows) > _PAGE
    shown = rows[:_PAGE]
    body = []
    for fingerprint, ts, symbol, source, event_type, sentiment, title, dismissed in shown:
        action = "Excluded" if dismissed else (
            '<form method="post" action="/news">'
            f'<input type="hidden" name="fingerprint" value="{escape(fingerprint, quote=True)}">'
            '<input type="hidden" name="action" value="dismiss">'
            '<button class="btn ghost" type="submit">False alarm</button></form>'
        )
        tone, score = _sentiment_parts(sentiment)
        body.append(
            "<tr>"
            f"<td class=\"mono news-when\">{escape(_stamp(ts))}</td>"
            f"<td>{escape(symbol)}</td>"
            f"<td><span class=\"tag\">{escape(source)}</span></td>"
            f"<td>{_type_badge(event_type)}</td>"
            f"<td class=\"mono {tone}\">{escape(score)}</td>"
            f"<td class=\"news-title\">{escape(title)}</td>"
            f"<td>{action}</td></tr>"
        )
    inner = "".join(body)
    if not inner and offset == 0:
        inner = "<tr><td colspan=\"7\">No headline is stored.</td></tr>"
    return inner, more, offset + len(shown)


_SIM_IDLE = """<svg viewBox="0 0 96 96" fill="none" aria-hidden="true">
<path d="M58 18a26 26 0 1 0 6 51 18 18 0 1 1-6-51z" fill="#8eb0f5"/>
<circle cx="24" cy="28" r="1.5" fill="#6b96f2"/>
<circle cx="18" cy="40" r="1" fill="#6b96f2" opacity=".65"/>
<path d="M72 26h8M76 22v8" stroke="#e7eefc" stroke-width="1.4" stroke-linecap="round"/>
<path d="M28 58h9l-9 8h9" stroke="#9aa6b2" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
<path d="M42 48h7l-7 6h7" stroke="#b7c2d0" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>
<path d="M54 40h5l-5 5h5" stroke="#d5dee8" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/>
<path d="M16 80h64" stroke="#2a3340" stroke-width="1.5" stroke-linecap="round"/>
<path d="M46 72v16M41 76h10M41 84h10" stroke="#7d8794" stroke-width="1.4" stroke-linecap="round"/>
</svg>"""


def _simulation(conn, account: str, _query: dict) -> dict:
    from athena_orchestrator.sim import progress

    if trading_mode() != "paper":
        body = '<div class="card"><p>Simulation runs in paper mode.</p></div>'
        return _html("Simulation", "", body, [])
    info = progress(conn, account, series=True)
    playing = info["status"] in {"downloading", "active", "halted"}
    day = escape(info["cursor_label"]) if info["cursor_label"] else DASH
    window = f'{escape(info["start_label"])} – {escape(info["end_label"])}' if info["start_label"] else DASH
    step = f'Day {info["day"]} of {info["total"]}' if info["run_id"] else DASH
    left = f'{info["left"]} cycles left' if info["run_id"] else DASH
    status = escape(info["label"]) if info["run_id"] else "Idle"
    spinner = '<span class="sim-spin" aria-hidden="true"></span>' if info["status"] == "active" else ""
    note = f'<p class="sim-note" id="sim-note">{escape(info["note"])}</p>' if info["note"] else ""
    buttons = []
    if info["status"] in {"downloading", "active"}:
        buttons.append('<button class="btn danger" type="button" data-cmd="sim-halt">Halt</button>')
    elif info["status"] == "halted":
        disabled = "" if info["resume"] else " disabled"
        buttons.append(f'<button class="btn primary" type="button" data-cmd="sim-resume"{disabled}>Resume</button>')
    else:
        buttons.append('<button class="btn primary" type="button" data-cmd="simulate">Simulate</button>')
    if info["run_id"]:
        buttons.append('<button class="btn danger" type="button" data-cmd="sim-reset">Reset</button>')
    if playing:
        rows = []
        for symbol in info["symbols"]:
            rows.append(
                '<div class="card sim-row">'
                f'<h2><span>{escape(symbol)}</span><span class="sim-window"><span class="tag">Daily</span><span class="tag">60 days</span></span></h2>'
                f'<div class="chart-box sim-box"><div class="sim-plot" data-symbol="{escape(symbol, quote=True)}"></div></div>'
                "</div>"
            )
        stage = f'<div class="sim-charts scroll-y">{"".join(rows)}</div>'
        lead = "Played days are green or red. Days not played yet are grey."
    else:
        stage = f'<div class="sim-idle">{_SIM_IDLE}<p>Nothing is playing.</p></div>'
        lead = "Start a paper replay from here."
    blob = json.dumps(info).replace("<", "\\u003c")
    body = (
        '<div class="sim-page">'
        '<div class="card sim-progress">'
        '<div class="sim-facts">'
        f'<div><div class="k">Status</div><div class="v" id="sim-status">{spinner}{status}</div></div>'
        f'<div><div class="k">Day</div><div class="v" id="sim-day">{day}</div></div>'
        f'<div><div class="k">Window</div><div class="v" id="sim-window">{window}</div></div>'
        f'<div><div class="k">Progress</div><div class="v" id="sim-step">{step}</div></div>'
        f'<div><div class="k">Left</div><div class="v" id="sim-left">{left}</div></div>'
        "</div>"
        f"{note}"
        f'<div class="row-actions">{"".join(buttons)}</div></div>'
        f"{stage}"
        "</div>"
        f'<script type="application/json" id="sim-data">{blob}</script>'
    )
    return _html("Simulation", lead, body, [])


def _replaying(conn, account: str) -> bool:
    if not account:
        return False
    found = conn.execute(
        """
        SELECT 1 FROM sim_runs
        WHERE account_id = %s AND status IN ('downloading', 'active', 'halted')
        LIMIT 1
        """,
        (account,),
    ).fetchone()
    return found is not None


def _closes(conn, symbol: str) -> tuple[str, str, str]:
    from athena_strategy.simbars import in_replay, load_daily

    if in_replay(conn, symbol):
        daily = load_daily(conn, symbol)
        if not daily:
            return DASH, DASH, ""
        last = Decimal(daily[-1][2])
        previous = None
        for row in reversed(daily[:-1]):
            close = Decimal(row[2])
            if close == 0 or close == last:
                continue
            previous = close
            break
        if previous is None or previous == 0:
            return f"{last:.2f}", DASH, ""
        change = (last - previous) / previous * Decimal(100)
        tone = "chg-up" if change > 0 else "chg-down" if change < 0 else ""
        return f"{last:.2f}", f"{change:+.2f}%", tone
    from athena_marketdata.pricing import quote_pair

    last, previous = quote_pair(conn, symbol)
    if last is None:
        return DASH, DASH, ""
    if previous is None or previous == 0:
        return f"{last:.2f}", DASH, ""
    change = (last - previous) / previous * Decimal(100)
    tone = "chg-up" if change > 0 else "chg-down" if change < 0 else ""
    return f"{last:.2f}", f"{change:+.2f}%", tone


def _pool_profile(conn, account: str, symbol: str) -> tuple[str, Decimal | None, str, str]:
    fact = conn.execute(
        "SELECT name, market_cap FROM symbol_facts WHERE symbol = %s",
        (symbol,),
    ).fetchone()
    name = "" if fact is None else fact[0] or ""
    cap = None if fact is None else fact[1]
    book = conn.execute(
        "SELECT classifiers, thesis FROM symbol_phases WHERE account_id = %s AND symbol = %s",
        (account, symbol),
    ).fetchone() if account else None
    classifiers = "" if book is None else book[0] or ""
    thesis = "" if book is None else book[1] or ""
    return name, cap, classifiers, thesis


def _save_pool_facts(conn, account: str, symbol: str, form: dict) -> dict:
    partial = (form.get("partial") or [""])[0] == "row"
    name = " ".join((form.get("name") or [""])[0].split())[:80]
    thesis = " ".join((form.get("thesis") or [""])[0].split())[:240]
    classifiers = clean_classifiers((form.get("classifiers") or [""])[0])
    try:
        cap = _parse_cap((form.get("market_cap") or [""])[0])
    except ValueError:
        message = "Market cap needs a number, such as 5.31T or 188.2B."
        if partial:
            return _pool_patch(False, message, status=400)
        return _universe(conn, account, {}, message) | {"status": 400}
    conn.execute(
        """
        INSERT INTO symbol_facts (symbol, name, market_cap)
        VALUES (%s, %s, %s)
        ON CONFLICT (symbol) DO UPDATE
        SET name = EXCLUDED.name, market_cap = EXCLUDED.market_cap, updated_at = now()
        """,
        (symbol, name, cap),
    )
    conn.execute(
        """
        INSERT INTO symbol_phases (account_id, symbol, phase, tags, tactic, classifiers, thesis)
        VALUES (%s, %s, 'P1', '', 'HOLD', %s, %s)
        ON CONFLICT (account_id, symbol) DO UPDATE
        SET classifiers = EXCLUDED.classifiers, thesis = EXCLUDED.thesis, updated_at = now()
        """,
        (account, symbol, classifiers, thesis),
    )
    conn.commit()
    message = f"Saved {symbol}."
    if partial:
        current = load_active(conn, account)
        return _pool_patch(
            True,
            message,
            html=_pool_row(conn, account, current, symbol),
            count=len(current.watchlist),
        )
    return _universe(conn, account, {}, message)


def _sync_symbol(symbol: str) -> str:
    return (
        '<button class="btn ghost" type="button" data-cmd="sync-symbol" '
        f'data-symbol="{escape(symbol, quote=True)}" style="padding:4px 10px;font-size:11px">Sync</button>'
    )


def _remove_symbol(symbol: str) -> str:
    return (
        '<form method="post" action="/universe" style="display:inline"'
        f' data-confirm="Remove {escape(symbol, quote=True)} from the pool?">'
        f'<input type="hidden" name="symbol" value="{escape(symbol, quote=True)}">'
        '<input type="hidden" name="action" value="remove">'
        '<button class="btn ghost" type="submit" style="padding:4px 10px;font-size:11px">Remove</button>'
        "</form>"
    )


def _classifier_badges(raw: str) -> str:
    parts = [part for part in (raw or "").split(",") if part]
    if not parts:
        return DASH
    badges = []
    for part in parts:
        tone = CLASSIFIER_BADGE.get(part, "")
        meaning = CLASSIFIER_MEANING.get(part, "")
        title = f' title="{escape(meaning, quote=True)}"' if meaning else ""
        badges.append(f'<span class="tag {tone}"{title}>{escape(part)}</span>')
    return "".join(badges)


def _parse_cap(raw: str) -> Decimal | None:
    text = raw.strip().replace(",", "").replace("$", "")
    if not text:
        return None
    scales = {"K": Decimal("1e3"), "M": Decimal("1e6"), "B": Decimal("1e9"), "T": Decimal("1e12")}
    suffix = text[-1].upper()
    if suffix in scales:
        return Decimal(text[:-1]) * scales[suffix]
    return Decimal(text)


def _cap_text(value) -> str:
    if value is None:
        return DASH
    amount = Decimal(value)
    for unit, scale in (("T", Decimal("1e12")), ("B", Decimal("1e9")), ("M", Decimal("1e6"))):
        if abs(amount) >= scale:
            shown = f"{amount / scale:.2f}".rstrip("0").rstrip(".")
            return f"{shown}{unit}"
    return f"{amount:,.0f}"


def _edit_symbol(symbol: str, name: str, cap, classifiers: str, thesis: str, price: str, change: str, rank: str, phase: str) -> str:
    cap_text = "" if cap is None else _cap_text(cap)
    return (
        '<button class="btn ghost" type="button" data-cmd="edit-symbol" style="padding:4px 10px;font-size:11px"'
        f' data-symbol="{escape(symbol, quote=True)}"'
        f' data-name="{escape(name, quote=True)}"'
        f' data-cap="{escape(cap_text, quote=True)}"'
        f' data-classifiers="{escape(classifiers, quote=True)}"'
        f' data-thesis="{escape(thesis, quote=True)}"'
        f' data-price="{escape(price, quote=True)}"'
        f' data-change="{escape(change, quote=True)}"'
        f' data-ivr="{escape(rank, quote=True)}"'
        f' data-phase="{escape(phase or DASH, quote=True)}">Edit</button>'
    )


def _toggle(symbol: str, field: str, on: bool, green: bool = False) -> str:
    state = " on" if on else ""
    tone = " green" if green else ""
    label = {"enabled": "Enabled", "long": "Long", "may_buy": "May buy"}.get(field, field.upper())
    return (
        '<form method="post" action="/universe" style="display:inline">'
        f'<input type="hidden" name="symbol" value="{escape(symbol, quote=True)}">'
        '<input type="hidden" name="action" value="toggle">'
        f'<input type="hidden" name="field" value="{field}">'
        f'<button class="pool-tgl{tone}{state}" type="submit" aria-label="{escape(label, quote=True)}"></button>'
        "</form>"
    )


def _history_kind(query: dict) -> str:
    kind = (query.get("kind") or ["all"])[0]
    return kind if kind in {key for key, _label in HISTORY_FILTERS} else "all"


def _history_rows(conn, account: str) -> list[dict]:
    if not account:
        return []
    fills = conn.execute(
        """
        SELECT f.created_at, f.symbol, f.tactic, COALESCE(d.phase, ''), f.draft_id,
               f.price, f.contracts, o.action, o.multiplier,
               COALESCE(a.actor, ''), COALESCE(a.reason, ''), COALESCE(d.reason, '')
        FROM fills f
        LEFT JOIN drafts d ON d.id = f.draft_id
        LEFT JOIN LATERAL (
            SELECT action, multiplier FROM orders
            WHERE draft_id = f.draft_id AND account_id = f.account_id
            ORDER BY id
            LIMIT 1
        ) o ON true
        LEFT JOIN LATERAL (
            SELECT actor, reason FROM approvals
            WHERE draft_id = f.draft_id AND account_id = f.account_id AND decision = 'approve'
            ORDER BY id DESC
            LIMIT 1
        ) a ON true
        WHERE f.account_id = %s AND (f.simulated = false OR %s)
        """,
        (account, _replaying(conn, account)),
    ).fetchall()
    filled_drafts = set()
    rows = []
    for created, symbol, tactic, phase, draft_id, price, contracts, action, multiplier, actor, approval_reason, draft_reason in fills:
        if draft_id:
            filled_drafts.add(draft_id)
        rows.append(
            _history_item(
                created,
                symbol,
                tactic,
                phase,
                "filled",
                _fill_cash(price, contracts, action, multiplier),
                actor,
                approval_reason or draft_reason,
            )
        )
    approvals = conn.execute(
        """
        SELECT a.created_at, d.symbol, d.tactic, d.phase, a.draft_id, a.decision, a.actor, a.reason, d.reason
        FROM approvals a
        JOIN drafts d ON d.id = a.draft_id
        WHERE a.account_id = %s
        """,
        (account,),
    ).fetchall()
    for created, symbol, tactic, phase, draft_id, decision, actor, reason, draft_reason in approvals:
        if decision == "approve" and draft_id in filled_drafts:
            continue
        if decision not in {"approve", "reject"}:
            continue
        rows.append(
            _history_item(
                created,
                symbol,
                tactic,
                phase,
                "rejected" if decision == "reject" else "approved",
                None,
                actor,
                reason or draft_reason,
            )
        )
    rows.sort(key=lambda item: item["sort"], reverse=True)
    return rows


def _history_item(created, symbol, tactic, phase, status, pnl, actor, reason) -> dict:
    local = created.astimezone(HKT)
    return {
        "when": local.strftime("%Y-%m-%d"),
        "clock": local.strftime("%m-%d %H:%M"),
        "symbol": symbol,
        "tactic": tactic,
        "phase": phase or "",
        "status": status,
        "pnl": pnl,
        "actor": actor or "",
        "reason": reason or "",
        "sort": created,
    }


def _history_visible(item: dict, kind: str) -> bool:
    if kind == "rejected":
        return item["status"] == "rejected"
    if kind == "all":
        return True
    return _tactic_matches(item["tactic"], kind)


def _history_tr(item: dict) -> str:
    status = {"filled": "FILLED", "rejected": "REJECTED", "approved": "APPROVED"}[item["status"]]
    tag = {"filled": "tag green", "rejected": "tag red", "approved": "tag"}[item["status"]]
    pnl = "—" if item["pnl"] is None else _pnl_text(item["pnl"])
    pnl_class = "mono" if item["pnl"] is None else ("pnl-pos" if item["pnl"] > 0 else "pnl-neg" if item["pnl"] < 0 else "mono")
    by = f"{item['actor']} {item['clock']} HKT" if item["actor"] else "—"
    title = f"{item['symbol']} {item['tactic']} {item['when']}"
    return (
        '<tr data-cmd="history-row"'
        f' data-title="{escape(title, quote=True)}"'
        f' data-phase="{escape(item["phase"] or "—", quote=True)}"'
        f' data-status="{status}"'
        f' data-pnl="{escape(pnl, quote=True)}"'
        f' data-by="{escape(by, quote=True)}"'
        f' data-reason="{escape(item["reason"] or "—", quote=True)}">'
        f'<td class="mono">{escape(item["when"])}</td>'
        f"<td><b>{escape(item['symbol'])}</b></td>"
        f"<td>{_tactic_tag(item['tactic'])}</td>"
        f"<td>{_phase_tag(item['phase'])}</td>"
        f'<td class="{pnl_class}">{escape(pnl)}</td>'
        f'<td><span class="{tag}">{status}</span></td></tr>'
    )


def _tactic_tag(tactic: str) -> str:
    text = tactic.upper()
    if "STRANGLE" in text:
        color = "violet"
    elif "CSP" in text:
        color = "green"
    elif text.endswith("_CC") or text == "CC" or "OPEN_CC" in text:
        color = "blue"
    else:
        color = ""
    klass = f"tag {color}".strip()
    return f'<span class="{klass}">{escape(tactic)}</span>'


def _rank_points(samples: list[Decimal]) -> list[float]:
    if not samples:
        return []
    low = min(samples)
    high = max(samples)
    if high == low:
        return [0.0 for _item in samples]
    return [float((item - low) / (high - low) * Decimal(100)) for item in samples]


def _phase_badge(phase: str) -> str:
    if not phase:
        return DASH
    titles = {name: title for name, title, _detail in PHASES}
    label = f"{phase} {titles[phase].upper()}" if phase in titles else phase
    color = "blue" if phase == "P2" else ""
    klass = f"tag {color}".strip()
    return f'<span class="{klass}">{escape(label)}</span>'


def _phase_tag(phase: str) -> str:
    if not phase:
        return "—"
    return f'<span class="tag">{escape(phase)}</span>'


def _fill_cash(price, contracts, action, multiplier) -> Decimal | None:
    if action not in {"SELL", "BUY"}:
        return None
    signed = Decimal(price) * Decimal(contracts) * Decimal(multiplier or "100")
    if action == "BUY":
        return -signed
    return signed


def _pnl_text(amount: Decimal) -> str:
    shown = _cash_text(abs(amount))
    if amount > 0:
        return f"+${shown}"
    if amount < 0:
        return f"−${shown}"
    return "$0"


def _cash_text(amount: Decimal) -> str:
    if amount == amount.to_integral():
        return f"{int(amount):,}"
    return f"{amount:,.2f}"


_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _history_chips(rows: list[dict]) -> list[tuple[str, str]]:
    filled = sum(1 for item in rows if item["status"] == "filled")
    rejected = sum(1 for item in rows if item["status"] == "rejected")
    chips = [("ok", f"{filled} FILLED"), ("", f"{rejected} REJECTED")]
    if not rows:
        return chips
    start = min(item["sort"] for item in rows).astimezone(HKT)
    end = max(item["sort"] for item in rows).astimezone(HKT)
    if start.year == end.year:
        span = f"{_MONTHS[start.month - 1]} → {_MONTHS[end.month - 1]} {end.year}"
    else:
        span = f"{_MONTHS[start.month - 1]} {start.year} → {_MONTHS[end.month - 1]} {end.year}"
    chips.append(("info", span.upper()))
    return chips


def _premium_stat(facts: dict | None) -> tuple[str, str, str]:
    if not facts or facts["premium"] == DASH:
        return "", DASH, "No fills in six months"
    return "up", facts["premium"], facts["premium_meta"]


def _month_stats(facts: dict | None) -> tuple[tuple[str, str], tuple[str, str, str]]:
    empty = (DASH, "No fills in six months")
    if not facts:
        return empty, (*empty, "")
    scored = [item for item in facts["months"] if item["trades"]]
    if not scored:
        return empty, (*empty, "")
    best = max(scored, key=lambda item: item["net"])
    worst = min(scored, key=lambda item: item["net"])
    worst_class = "down" if worst["net"] < 0 else ""
    currency = facts.get("currency") or "HKD"
    return (_month_value(best, currency), _month_meta(best)), (_month_value(worst, currency), _month_meta(worst), worst_class)


def _month_value(month: dict, currency: str) -> str:
    net = Decimal(str(month["net"]))
    return f"{month['name']} {marked(net, currency)}"


def _month_meta(month: dict) -> str:
    trades = month["trades"]
    word = "trade" if trades == 1 else "trades"
    return f"{trades} {word}"


def _tactic_matches(tactic: str, kind: str) -> bool:
    text = tactic.upper()
    if kind == "all":
        return True
    if kind == "csp":
        return "CSP" in text
    if kind == "cc":
        return text.endswith("_CC") or text == "CC" or "OPEN_CC" in text
    if kind == "strangle":
        return "STRANGLE" in text
    if kind == "roll":
        return "ROLL" in text
    return True


def _account_card(name: str, mode: str, book_id: str, active: bool, facts: dict, panel_id: str) -> str:
    badge = '<span class="tag blue">ACTIVE</span>' if active else ""
    current = '<button class="btn ghost" type="button" disabled>Current</button>' if active else ""
    title = f"{name} sleeve"
    return (
        f'<div class="acct{" active" if active else ""}"><div class="a-head"><div>'
        f'<div class="a-name">{escape(name)}</div>'
        f'<div class="a-id">{escape(book_id)}'
        f'<button class="copy-plain" type="button" data-copy="{escape(book_id, quote=True)}" title="Copy">{_COPY_SVG}</button>'
        f"</div></div>{badge}</div>"
        '<div class="a-stats">'
        f'<div class="a-stat"><div class="l">NLV</div><div class="v">{escape(facts["nlv"])}</div></div>'
        f'<div class="a-stat"><div class="l">Cash</div><div class="v">{escape(facts["cash"])}</div></div>'
        f'<div class="a-stat"><div class="l">Positions</div><div class="v">{facts["positions"]}</div></div>'
        f'<div class="a-stat"><div class="l">Mode</div><div class="v" style="font-size:12px">{escape(mode)}</div></div>'
        "</div>"
        '<div class="row-actions">'
        f"{current}"
        f'<button class="btn ghost" type="button" data-cmd="view-book" data-panel="{panel_id}" data-title="{escape(title, quote=True)}">View</button>'
        "</div></div>"
    )


def _account_control_row(name: str, mode: str, book_id: str, facts: dict, sync_on: bool, trading_on: bool) -> str:
    return (
        "<tr>"
        f"<td><b>{escape(name)}</b> <span class=\"mono\" style=\"font-size:10.5px;color:var(--site-text-4)\">{escape(book_id)}</span></td>"
        f"<td><span class=\"tag\">{escape(mode)}</span></td>"
        f"<td>{_switch_button(book_id, 'sync', sync_on, 'Sync')}</td>"
        f"<td>{_switch_button(book_id, 'trading', trading_on, 'Trading')}</td>"
        f"<td class=\"mono\">{escape(facts['last'])}</td>"
        "</tr>"
    )


def _switch_button(book_id: str, field: str, on: bool, label: str) -> str:
    state = " on" if on else ""
    return (
        f'<button class="status-tgl{state}" type="button" data-cmd="account-toggle" '
        f'data-account="{escape(book_id, quote=True)}" data-field="{field}" aria-pressed="{str(on).lower()}" '
        f'aria-label="{label}"></button>'
    )


def _toggle_account(conn, account: str, form: dict, actor: str) -> dict:
    account_id = (form.get("account_id") or [""])[0].strip()
    field = (form.get("field") or [""])[0]
    if field not in {"sync", "trading"} or not account_id:
        return {"status": 400, "kind": "json", "body": '{"error":"unknown toggle"}'}
    current = load_active(conn, account)
    session = account_id in set(active_accounts())
    previous = control_flag(current, account_id, field, session)
    updated = not previous
    payload = SettingsPayload.model_validate(current.model_dump())
    entry = payload.account_controls.get(account_id) or AccountSwitches()
    payload.account_controls[account_id] = entry.model_copy(update={field: updated})
    payload.privilege = current.privilege
    try:
        save_settings(conn, account, payload, f"console:{actor}", f"{field} {account_id}")
    except ValueError as exc:
        return {"status": 400, "kind": "json", "body": json.dumps({"error": str(exc)})}
    if field == "sync":
        detail = "Marks refresh on the next cycle." if updated else "Stored marks stay as they are."
    elif updated and session:
        detail = "Approve is still required before an order."
    elif updated:
        detail = f"Saved. This Gateway session trades {account}."
    else:
        detail = "New drafts stay parked. Queued orders stay parked."
    return {
        "status": 200,
        "kind": "json",
        "body": json.dumps({"account_id": account_id, "field": field, "on": updated, "detail": detail}),
    }


def _book_facts(conn, book_id: str) -> dict:
    nav = conn.execute(
        "SELECT day, last_nav FROM nav_marks WHERE account_id = %s ORDER BY day DESC LIMIT 1",
        (book_id,),
    ).fetchone()
    cash = conn.execute(
        """
        SELECT day, cash FROM account_marks
        WHERE account_id = %s AND cash IS NOT NULL
        ORDER BY day DESC LIMIT 1
        """,
        (book_id,),
    ).fetchone()
    positions = conn.execute(
        """
        SELECT count(*) FROM (
            SELECT DISTINCT ON (symbol) quantity
            FROM positions
            WHERE account_id = %s
            ORDER BY symbol, snapshot_at DESC, id DESC
        ) latest
        WHERE quantity <> 0
        """,
        (book_id,),
    ).fetchone()[0]
    currency = report_currency(conn, _account())
    base, balances, rates = ledger(conn, book_id)
    source = base or "HKD"
    nlv_amount = None if nav is None else convert(Decimal(nav[1]), source, currency, source, rates)
    cash_amount = None if cash is None else convert(Decimal(cash[1]), source, currency, source, rates)
    marks = [row[0] for row in (nav, cash) if row is not None]
    return {
        "nlv": show(nlv_amount, currency) if nlv_amount is not None else DASH,
        "cash": show(cash_amount, currency) if cash_amount is not None else DASH,
        "cash_rows": cash_rows(balances),
        "positions": positions,
        "synced": bool(marks),
        "last": max(marks).isoformat() if marks else DASH,
    }


def _book_panel(book_id: str, mode: str, facts: dict, panel_id: str, conn) -> str:
    drafts = conn.execute(
        """
        SELECT id, symbol, tactic, status FROM drafts
        WHERE account_id = %s
        ORDER BY created_at DESC LIMIT 12
        """,
        (book_id,),
    ).fetchall()
    positions = conn.execute(
        """
        SELECT symbol, quantity, sec_type FROM (
            SELECT DISTINCT ON (symbol, sec_type) symbol, quantity, sec_type
            FROM positions
            WHERE account_id = %s
            ORDER BY symbol, sec_type, snapshot_at DESC, id DESC
        ) latest
        WHERE quantity <> 0
        """,
        (book_id,),
    ).fetchall()
    halt = conn.execute(
        """
        SELECT reason, active FROM halts
        WHERE account_id = %s
        ORDER BY created_at DESC LIMIT 1
        """,
        (book_id,),
    ).fetchone()
    draft_rows = "".join(
        f"<tr><td class=\"mono\">{escape(item[0])}</td><td>{escape(item[1])}</td><td>{escape(item[2])}</td><td>{escape(item[3])}</td></tr>"
        for item in drafts
    ) or "<tr><td colspan=\"4\">No drafts.</td></tr>"
    position_rows = "".join(
        f"<tr><td>{escape(item[0])}</td><td class=\"mono\">{escape(str(item[1]))}</td><td>{escape(item[2] or '')}</td></tr>"
        for item in positions
    ) or "<tr><td colspan=\"3\">No positions.</td></tr>"
    halt_text = "None" if halt is None or not halt[1] else halt[0]
    return (
        f'<template id="{panel_id}">'
        f'<div class="field"><div class="k">Account</div><div class="v mono">{escape(book_id)}'
        f'<button class="copy-plain" type="button" data-copy="{escape(book_id, quote=True)}" title="Copy">{_COPY_SVG}</button></div></div>'
        f'<div class="field"><div class="k">NLV</div><div class="v mono">{escape(facts["nlv"])}</div></div>'
        f"{_cash_fields(facts)}"
        f'<div class="field"><div class="k">Positions</div><div class="v">{facts["positions"]}</div></div>'
        f'<div class="field"><div class="k">Mode</div><div class="v">{escape(mode)}</div></div>'
        f'<div class="field"><div class="k">Halt</div><div class="v">{escape(halt_text)}</div></div>'
        "<h2>Drafts</h2><table class=\"data\"><thead><tr><th>Id</th><th>Symbol</th><th>Tactic</th><th>Status</th></tr></thead>"
        f"<tbody>{draft_rows}</tbody></table>"
        "<h2>Positions</h2><table class=\"data\"><thead><tr><th>Symbol</th><th>Qty</th><th>Type</th></tr></thead>"
        f"<tbody>{position_rows}</tbody></table></template>"
    )


def _cash_fields(facts: dict) -> str:
    rows = facts.get("cash_rows") or []
    if not rows:
        return f'<div class="field"><div class="k">Cash</div><div class="v mono">{escape(facts["cash"])}</div></div>'
    return "".join(
        f'<div class="field"><div class="k">Cash {escape(code)}</div><div class="v mono">{escape(text)}</div></div>'
        for code, text in rows
    )


def _ai_provider_options(current: str) -> str:
    labels = (
        ("DSH", "DSH"),
        ("DEEPSEEK", "DEEPSEEK"),
        ("OPENROUTER", "OPENROUTER"),
        ("CLAUDE", "CLAUDE"),
        ("KIMI", "KIMI API"),
    )
    return "".join(
        f'<option value="{name}"{" selected" if name == current else ""}>{label}</option>'
        for name, label in labels
    )


def _model_options(current) -> str:
    names = MODELS.get(current.ai.provider, MODELS["DSH"])
    selected = scoring_model(current)
    if selected not in names:
        selected = names[0]
    return "".join(
        f'<option value="{escape(name, quote=True)}"{" selected" if name == selected else ""}>{escape(name)}</option>'
        for name in names
    )


def _credential_help(provider: str) -> str:
    env = CREDENTIAL_ENV.get(provider, "")
    if not env:
        return ""
    return f"Clear the field to remove the token. With nothing saved, Athena uses {env}."


def _credential_masks(current) -> dict[str, str]:
    masks = {}
    for provider in CREDENTIAL_FIELD:
        probe = current.model_copy(update={"ai": current.ai.model_copy(update={"provider": provider})})
        token = deepseek_token(probe)
        masks[provider] = _mask_center(token) if token else ""
    return masks


def _credential_input(current) -> str:
    shown = ""
    if current.ai.provider != "DSH":
        token = deepseek_token(current)
        shown = _mask_center(token) if token else ""
    value = escape(shown, quote=True)
    return (
        f'<input class="input" name="credential_token" type="text" autocomplete="off" '
        f'spellcheck="false" value="{value}">'
    )


def _currency_options(current: str) -> str:
    return "".join(
        f'<option value="{code}"{" selected" if code == current else ""}>{code}</option>'
        for code in CURRENCIES
    )


def _plain_number(value) -> str:
    number = Decimal(value)
    if number == number.to_integral():
        return f"{int(number):,}"
    text = f"{number:,.2f}"
    return text.rstrip("0").rstrip(".")


def _version_rows(conn, account: str) -> str:
    rows = conn.execute(
        """
        SELECT v.id, v.note, v.author, a.changes
        FROM settings_versions v
        LEFT JOIN settings_audits a ON a.settings_id = v.id AND a.account_id = v.account_id
        WHERE v.account_id = %s
        ORDER BY v.id DESC
        LIMIT 8
        """,
        (account,),
    ).fetchall()
    body = []
    for version_id, note, author, changes in rows:
        body.append(
            "<tr>"
            f"<td class=\"mono\">v{version_id}</td>"
            f"<td>{escape(note)}</td>"
            f"<td>{escape(author)}</td>"
            f"<td class=\"mono diff\">{escape(changes or '')}</td>"
            "</tr>"
        )
    inner = "".join(body) or "<tr><td colspan=\"4\">No version is stored.</td></tr>"
    return (
        "<table class=\"data\"><thead><tr><th>Version</th><th>Note</th><th>Author</th><th>Diff</th></tr></thead>"
        f"<tbody>{inner}</tbody></table>"
    )


def _q_btn(label: str, note: str) -> str:
    return (
        '<button class="help-q" type="button" data-cmd="help-q" '
        f'aria-label="About {escape(label, quote=True)}" '
        f'data-help="{escape(note, quote=True)}">?</button>'
    )


def _q(label: str, note: str) -> str:
    return f"{escape(label)}{_q_btn(label, note)}"


def _field(label: str, note: str, control: str) -> str:
    return f'<div class="settings-field"><label>{_q(label, note)}</label>{control}</div>'


def _limit(label: str, name: str, value: int | float, note: str = "", step: str = "1", preview: bool = False) -> str:
    title = _q(label, note) if note else escape(label)
    shown = value
    if isinstance(value, float):
        shown = f"{value:.4f}".rstrip("0").rstrip(".")
    marker = ' data-preview="threshold"' if preview else ""
    return (
        f'<div class="settings-field"><label>{title}</label>'
        f'<input class="input" name="{name}" type="number" min="0" step="{step}" required value="{shown}"{marker}>'
        "</div>"
    )


def _secret(name: str) -> str:
    return "set" if os.environ.get(name, "").strip() else "empty"


def _tag(text: str, kind: str = "") -> str:
    classes = "tag"
    if kind in {"green", "amber", "blue", "teal", "red", "violet"}:
        classes = f"tag {kind}"
    return f'<span class="{classes}">{escape(text)}</span>'


def _row(label: str, value: str, note: str = "") -> str:
    title = _q(label, note) if note else escape(label)
    return f'<div class="toggle-row"><div>{title}</div>{value}</div>'


_EYE_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
    '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/>'
    '<circle cx="12" cy="12" r="3"/></svg>'
)


def _developer_card(current) -> str:
    key = (current.developer.api_key or "").strip()
    if key:
        shown = (
            '<div class="key-row">'
            f'<input class="input" name="developer_api_key" type="password" readonly autocomplete="off" value="{escape(key, quote=True)}">'
            f'<button class="key-plain" type="button" data-cmd="reveal-key" aria-label="Reveal key">{_EYE_SVG}</button>'
            f'<button class="key-plain" type="button" data-copy="{escape(key, quote=True)}" title="Copy">{_COPY_SVG}</button>'
            "</div>"
            '<button class="btn" type="submit" name="action" value="generate-api-key" data-explicit>Regenerate key</button>'
            '<button class="btn ghost" type="submit" name="action" value="clear-api-key" data-explicit>Clear key</button>'
        )
    else:
        shown = '<p class="slot-note">No key yet.</p>'
    button = "" if key else '<button class="btn" type="submit" name="action" value="generate-api-key" data-explicit>Generate key</button>'
    return (
        '<div class="card"><h2>Developer</h2><div class="scroll-y">'
        '<p class="card-lead">A key lets another app on this machine read prices. It does not place orders. '
        '<a class="more" href="/skill">How to call it</a></p>'
        f"{_field('API key', 'Secret for the local price API. Reveal shows it. Copy puts it on the clipboard.', shown)}"
        f"{button}"
        "</div></div>"
    )


def _mask_center(value: str) -> str:
    count = len(value)
    if count < 2:
        return value
    hidden = count // 2
    front = (count - hidden) // 2
    back = count - hidden - front
    return value[:front] + ("•" * hidden) + value[-back:]


def _skill(conn, account: str, query: dict, note: str = "") -> dict:
    key = ""
    if account:
        try:
            key = (load_active(conn, account).developer.api_key or "").strip()
        except RuntimeError:
            key = ""
    if key:
        secret = (
            '<div class="key-row">'
            f'<span class="mono" data-secret="{escape(key, quote=True)}" data-mask="{escape(_mask_center(key), quote=True)}" data-hidden="1">{escape(_mask_center(key))}</span>'
            f'<button class="key-plain" type="button" data-cmd="reveal-key" aria-label="Reveal key">{_EYE_SVG}</button>'
            f'<button class="key-plain" type="button" data-copy="{escape(key, quote=True)}" title="Copy">{_COPY_SVG}</button>'
            "</div>"
        )
    else:
        secret = '<p>No key yet. Generate one on <a href="/settings">Settings</a>.</p>'
    side = (
        '<div class="card skill-doc">'
        "<h2>Developer API</h2>"
        "<p>This page is the contract for another program, such as fanit-cli, running on this machine. "
        "The console is local. Send the Developer key. A call returns a task id. Poll that id until it is done or failed. "
        "These calls read IB. They do not place orders.</p>"
        f"{_row('Key', secret, 'The Developer key for this account. Generate it on Settings.')}"
        f"{_row('Base', _tag('http://127.0.0.1:8788'), 'The local console address.')}"
        f"{_row('Auth', _tag('Authorization: Bearer <key>'), 'Send this header on every call.')}"
        + "</div>"
        '<div class="card skill-doc"><h2>Rules</h2><ul>'
        "<li>bar is 1 min, 5 mins, 15 mins, 30 mins, 1 hour, or 1 day. duration is an IB span such as 30 D or 1 Y.</li>"
        "<li>Up to 20 symbols. Gateway down makes the whole task failed.</li>"
        "<li>A symbol IB cannot find is status failed inside a done task, with error set.</li>"
        "</ul></div>"
        + _skill_try()
    )
    body = f'<div class="skill-cols"><div class="skill-side">{side}</div><div class="skill-requests">{_skill_rest()}</div></div>'
    chips = [("ok", "KEY SET")] if key else [("warn", "NO KEY")]
    return _html("Skill", "How another app reads prices from Athena.", body, chips)


_JSON_TOKEN = re.compile(
    r'"(?:\\.|[^"\\])*"'
    r"|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?"
    r"|\b(?:true|false|null)\b"
    r"|[{}\[\],:]"
)


def _paint_json(text: str) -> str:
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return escape(text)
    parts = []
    pos = 0
    for match in _JSON_TOKEN.finditer(text):
        parts.append(escape(text[pos : match.start()]))
        token = match.group()
        pos = match.end()
        if token.startswith('"'):
            kind = "jk" if text[pos:].lstrip().startswith(":") else "js"
        elif token in "{}[],:":
            kind = "jp"
        elif token in {"true", "false", "null"}:
            kind = "jb"
        else:
            kind = "jn"
        parts.append(f'<span class="{kind}">{escape(token)}</span>')
    parts.append(escape(text[pos:]))
    return "".join(parts)


_SKILL_PLAY = (
    (
        "bars",
        "Bars",
        "POST",
        "/api/v1/bars",
        """{
  "symbols": ["NVDA", "IGV", "700"],
  "bar": "1 day",
  "duration": "30 D"
}""",
    ),
    (
        "day",
        "Day",
        "POST",
        "/api/v1/day",
        """{
  "symbols": ["NVDA"],
  "day": "2026-10-09"
}""",
    ),
    (
        "option",
        "Option",
        "POST",
        "/api/v1/option",
        """{
  "symbols": [{
    "symbol": "NVDA",
    "expiry": "20261120",
    "strike": 180,
    "right": "P"
  }],
  "bar": "1 day",
  "duration": "5 D"
}""",
    ),
    (
        "company",
        "Company",
        "POST",
        "/api/v1/company",
        """{
  "symbols": ["NVDA", "700"]
}""",
    ),
    ("task", "Task", "GET", "/api/v1/tasks/{task_id}", "ab12cd34ef56"),
)


def _skill_try() -> str:
    options = "".join(
        f'<option value="{escape(key, quote=True)}">{escape(label)}</option>'
        for key, label, _method, _path, _body in _SKILL_PLAY
    )
    samples = {
        key: {"method": method, "path": path, "body": body}
        for key, _label, method, path, body in _SKILL_PLAY
    }
    first = _SKILL_PLAY[0][4]
    first_url = "http://127.0.0.1:8788" + _SKILL_PLAY[0][3]
    curl = escape(
        "curl -s -X POST http://127.0.0.1:8788/api/v1/bars \\\n"
        "  -H 'Authorization: Bearer <key>' \\\n"
        "  -H 'Content-Type: application/json' \\\n"
        "  -d '{\"symbols\":[\"NVDA\"],\"bar\":\"1 day\",\"duration\":\"5 D\"}'\n\n"
        "curl -s -X POST http://127.0.0.1:8788/api/v1/day \\\n"
        "  -H 'Authorization: Bearer <key>' \\\n"
        "  -H 'Content-Type: application/json' \\\n"
        "  -d '{\"symbols\":[\"NVDA\"],\"day\":\"2026-10-09\"}'\n\n"
        "curl -s http://127.0.0.1:8788/api/v1/tasks/<task_id> \\\n"
        "  -H 'Authorization: Bearer <key>'"
    )
    blob = json.dumps(samples).replace("<", "\\u003c")
    return (
        '<div class="card skill-doc">'
        "<h2>Try</h2>"
        '<div class="skill-try">'
        '<div class="seg">'
        '<button type="button" class="on" data-skill-tab="curl">Curl Sample</button>'
        '<button type="button" data-skill-tab="play">Playground</button>'
        "</div>"
        f'<div data-skill-panel="curl"><pre class="json">{curl}</pre></div>'
        '<div data-skill-panel="play" hidden>'
        '<label class="skill-play-label">Request'
        f'<select id="skill-play-kind">{options}</select></label>'
        '<label class="skill-play-label">URL'
        f'<input id="skill-play-url" readonly value="{escape(first_url, quote=True)}"></label>'
        '<label class="skill-play-label">Body'
        f'<textarea id="skill-play-body" spellcheck="false">{escape(first)}</textarea></label>'
        '<button class="btn primary" type="button" id="skill-play-send">Submit</button>'
        '<pre class="json" id="skill-play-out">Response shows here.</pre>'
        "</div>"
        f'<script type="application/json" id="skill-play-samples">{blob}</script>'
        "</div></div>"
    )


def _skill_call(method: str, path: str) -> str:
    kind = "amber" if method == "POST" else "green"
    return f'<div class="skill-call">{_tag(method, kind)}<span class="mono">http://127.0.0.1:8788{path}</span></div>'


def _skill_rest() -> str:
    posts = [
        (
            "Bars",
            "/api/v1/bars",
            "Stock and ETF candles for a window. A string is a US stock or ETF. A numeric symbol such as 700 is a Hong Kong stock. bar and duration are optional.",
            """{
  "symbols": ["NVDA", "IGV", "700"],
  "bar": "1 day",
  "duration": "30 D"
}""",
        ),
        (
            "Day",
            "/api/v1/day",
            "One session for the same symbols as Bars. day is required, YYYY-MM-DD. A missing session is failed on that symbol.",
            """{
  "symbols": ["NVDA"],
  "day": "2026-10-09"
}""",
        ),
        (
            "Option",
            "/api/v1/option",
            "Option candles. Each symbol is an object with expiry YYYYMMDD, strike, and right C or P. US history is the CBOE print. A 1 day request is folded from hourly prints.",
            """{
  "symbols": [{
    "symbol": "NVDA",
    "expiry": "20261120",
    "strike": 180,
    "right": "P"
  }],
  "bar": "1 day",
  "duration": "5 D"
}""",
        ),
        (
            "Company",
            "/api/v1/company",
            "Name, exchange, currency, and market cap when Athena has it. Same symbol strings as Bars.",
            """{
  "symbols": ["NVDA", "700"]
}""",
        ),
    ]
    parts = []
    for title, path, note, sample in posts:
        parts.append(
            '<div class="card skill-doc">'
            f"<h2>{escape(title)}</h2>"
            f"{_skill_call('POST', path)}"
            f"<p>{escape(note)}</p>"
            f"<pre class=\"json\">{_paint_json(sample)}</pre>"
            "</div>"
        )
    parts.append(
        '<div class="card skill-doc"><h2>Task</h2>'
        + _skill_call("GET", "/api/v1/tasks/{task_id}")
        + "<p>Every POST above answers with this at once. It is not the prices.</p>"
        + f"<pre class=\"json\">{_paint_json('''{\n  \"ok\": true,\n  \"task_id\": \"ab12cd34ef56\",\n  \"status\": \"running\"\n}''')}</pre>"
        + "<p>Poll the same URL. Bars, Day, and Option put latest and bars on each symbol. latest is the close of the newest bar.</p>"
        + f"<pre class=\"json\">{_paint_json('''{\n  \"ok\": true,\n  \"task_id\": \"ab12cd34ef56\",\n  \"status\": \"done\",\n  \"result\": {\n    \"symbols\": [{\n      \"symbol\": \"NVDA\",\n      \"sec_type\": \"STK\",\n      \"status\": \"done\",\n      \"latest\": 181.0,\n      \"bars\": [{\"date\": \"2026-10-09\", \"time\": \"2026-10-09\", \"open\": 180.1, \"high\": 182.4, \"low\": 179.2, \"close\": 181.0, \"volume\": 1000}],\n      \"error\": \"\"\n    }]\n  },\n  \"error\": \"\"\n}''')}</pre>"
        + "<p>Company uses the same GET. The symbol object looks like this instead.</p>"
        + f"<pre class=\"json\">{_paint_json('''{\n  \"symbol\": \"NVDA\",\n  \"sec_type\": \"STK\",\n  \"status\": \"done\",\n  \"name\": \"NVIDIA Corp\",\n  \"market_cap\": 5530000000000,\n  \"exchange\": \"SMART\",\n  \"primary_exchange\": \"NASDAQ\",\n  \"currency\": \"USD\",\n  \"error\": \"\"\n}''')}</pre>"
        + "</div>"
    )
    return "".join(parts)


def _book_card() -> str:
    return (
        '<div class="card danger"><h2><span>Book'
        + _q_btn("Book", "Liquidate deletes simulated positions and sends one limit close for each real position. Flush wipes this account after you type I CONFIRM and wait out the countdown.")
        + "</span></h2>"
        '<div class="row-actions">'
        '<button class="btn danger" type="button" data-cmd="liquidate">Liquidate All</button>'
        '<button class="btn danger" type="button" data-cmd="flush">Flush All</button>'
        "</div></div>"
    )


_SIM_SYMBOL = re.compile(r"^[A-Z0-9.]{1,16}$")


def sim_targets(posted: list[str], watchlist: list[str], kept: list[str]) -> list[str]:
    """Replay names from the form. A new name must already be in the universe."""
    allowed = {item.strip().upper() for item in watchlist if item.strip()}
    allowed.update(item.strip().upper() for item in kept if item.strip())
    picked: list[str] = []
    for raw in posted:
        symbol = raw.strip().upper()
        if not symbol or symbol in picked or symbol not in allowed or _SIM_SYMBOL.fullmatch(symbol) is None:
            continue
        picked.append(symbol)
    return picked


def _sim_card(conn, current) -> str:
    watchlist = [item.strip().upper() for item in current.watchlist if item.strip()]
    names = _symbol_names(conn, watchlist)
    universe = json.dumps(
        [{"symbol": symbol, "name": names.get(symbol, "")} for symbol in watchlist],
        ensure_ascii=True,
    )
    picks = []
    for symbol in current.sim_symbols:
        text = symbol.strip().upper()
        if not text:
            continue
        picks.append(
            f'<span class="sim-pick"><b>{escape(text)}</b>'
            f'<button class="sim-x" type="button" aria-label="Remove {escape(text, quote=True)}">'
            '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M3 3l6 6M9 3L3 9" fill="none" stroke="currentColor" stroke-width="1.4"/></svg>'
            "</button>"
            f'<input type="hidden" name="sim_symbol" value="{escape(text, quote=True)}"></span>'
        )
    empty = "" if picks else '<p class="sim-empty">None selected.</p>'
    return (
        '<div class="card"><h2><span>Simulation'
        + _q_btn(
            "Simulation",
            "These names start as the replay defaults. Find a symbol from the universe to add it. The delete icon removes it.",
        )
        + "</span></h2>"
        '<div class="sim-find">'
        '<input class="input" id="sim-find" type="search" placeholder="Find a universe symbol" autocomplete="off" spellcheck="false">'
        '<div class="sim-menu" hidden></div></div>'
        f'<div class="sim-picks" id="sim-picks">{"".join(picks)}{empty}</div>'
        f'<script type="application/json" id="sim-universe">{universe}</script></div>'
    )


def _symbol_names(conn, symbols: list[str]) -> dict[str, str]:
    if not symbols:
        return {}
    rows = conn.execute(
        "SELECT symbol, name FROM symbol_facts WHERE symbol = ANY(%s)",
        (symbols,),
    ).fetchall()
    return {row[0]: row[1] or "" for row in rows}


def _conflict_card(on: bool) -> str:
    note = (
        "On: any short blocks every new open on that symbol. "
        "Off: a short put may sit beside shares and a covered call."
    )
    checked = " checked" if on else ""
    badge = _tag("ON", "green") if on else _tag("OFF")
    return (
        '<div class="card"><h2>Strict conflict'
        f'{_q_btn("Strict conflict", note)}</h2>'
        '<div class="toggle-row"><div class="toggle-end">'
        '<input type="hidden" name="strict_conflict" value="0">'
        f'<input type="checkbox" name="strict_conflict" value="1"{checked}>'
        f"{badge}</div></div></div>"
    )


def _switch(label: str, name: str, on: bool, note: str = "") -> str:
    checked = " checked" if on else ""
    badge = _tag("ON", "green") if on else _tag("OFF")
    title = _q(label, note) if note else escape(label)
    return (
        f'<div class="toggle-row"><div>{title}</div><div class="toggle-end">'
        f'<input type="hidden" name="{name}" value="0">'
        f'<input type="checkbox" name="{name}" value="1"{checked}>'
        f"{badge}</div></div>"
    )


def _checked(form: dict, name: str) -> bool:
    return (form.get(name) or ["0"])[-1] == "1"


def _telegram_block(current, token_ready: bool) -> str:
    from athena_bots.telegram import polling

    users = sorted(allowlist_ids(current))
    name = bot_username(current)
    live = polling
    if users:
        bot = f"@{name}" if name else "bot name not set"
        poll = _tag("ON", "green") if token_ready and live else _tag("OFF")
        return (
            f"{_row('Bot', _tag(bot), 'Telegram bot that receives drafts and fill notices.')}"
            f"{_row('Linked id', _tag(', '.join(users)), 'Telegram user ids allowed to approve.')}"
            f"{_row('Polling', poll, 'Whether the bot is waiting for replies.')}"
            '<button class="btn" type="submit" name="action" value="unlink" data-explicit>Unlink</button>'
        )
    link = ""
    expires = runtime.link_expires_at
    if runtime.link_code and expires is not None and expires > datetime.now(UTC):
        if name:
            href = f"https://t.me/{quote(name)}?start={quote(runtime.link_code)}"
            link = f'<p class="slot-note"><a href="{escape(href)}">{escape(href)}</a></p>'
        else:
            link = '<p class="slot-note">Set the bot username, then use Link again.</p>'
    return f"{_row('Telegram', _tag('NOT LINKED'), 'No Telegram user is linked yet. Link sends a one-time code.')}" + '<button class="btn" type="submit" name="action" value="link" data-explicit>Link</button>' + link


def _save_live(conn, account: str, payload, actor: str, note: str) -> dict:
    try:
        new_id, _changes = save_settings(conn, account, payload, f"console:{actor}", note)
    except ValueError as exc:
        return _settings(conn, account, {}, str(exc)) | {"status": 400}
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if database_url:
        from athena_bots.telegram import ensure_polling

        ensure_polling(database_url)
        threading.Thread(
            target=_quote_display_rates, args=(database_url,), daemon=True, name="fx-rates"
        ).start()
    return _settings(conn, account, {}, f"Saved version {new_id}.")


def _quote_display_rates(database_url: str) -> None:
    from athena_exec.session import refresh_display_rates

    refresh_display_rates(database_url)


def _env(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def _stamp(value: datetime) -> str:
    local = value.astimezone(HKT)
    return local.strftime("%m-%d %H:%M")
