"""Guardian R2: /guard panel, alert store, and reconciliation (booklet 14 §11).

The grading core lives in :mod:`athena_risk.guardian` (R1, pure, zero I/O).
This module is the adapter: it reads the store, assembles the caller-owned
snapshot, reconciles live alerts against the latest grade, and renders HTML.

Layering
--------
* pure:      ``desired``, ``reconcile``, ``assemble``, ``render_board``
* thin SQL:  ``load_alerts``, ``apply_plan``, ``resolve``, ``skip``, ``sync``

``pages.py`` passes in ``_position_rows`` / ``_pending_drafts`` / settings, so
this module never imports ``pages`` (no cycle). R3 will reuse ``sync()`` from
its scheduled runs; R5 will insert ``source='trigger'`` rows into the same
``guardian_alerts`` table.

Honest gaps (documented in booklet 14 §11.3, not invented):
* no persisted option delta  -> ``GuardianPosition.delta`` stays None, so
  ``assignment_risk`` / ``projected_el_negative`` are dormant in R2.
* no persisted IB margin-call flag -> ``AccountMarks.margin_call`` is False;
  the black tier still fires from EL<5% / MaintMargin>90% (D58).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html import escape
from typing import Mapping, Sequence

from athena_risk import (
    AccountMarks,
    GuardianPosition,
    GuardianReport,
    GuardianRules,
    PositionGrade,
    Tier,
    grade,
)
from athena_risk.guardian import BLOWUP_FLAGS, BREACH_FLAGS

__all__ = [
    "ACCOUNT_SCOPE",
    "SKIP_HOURS",
    "AlertRow",
    "ProposedAlert",
    "ReconcilePlan",
    "Snapshot",
    "desired",
    "reconcile",
    "assemble",
    "build_snapshot",
    "load_alerts",
    "apply_plan",
    "resolve",
    "skip",
    "sync",
    "render_board",
    "open_alert_count",
]

# A symbol value that can never be a real ticker: account-level breaches.
ACCOUNT_SCOPE = "*ACCOUNT*"
SKIP_HOURS = 24

# Only hard breaches open alerts. Proximity/healthy/profit never nag (D61).
_HARD_FLAGS = frozenset(BREACH_FLAGS | BLOWUP_FLAGS)
_LIVE_STATES = frozenset({"open", "skipped"})

_TIER_META: dict[Tier, tuple[str, str, str]] = {
    Tier.BLOWUP: ("Blowup / 暴雷", "black", "⬛"),
    Tier.DANGER: ("Danger / 危险", "red", "🟥"),
    Tier.BALANCED: ("Balanced / 平衡", "amber", "🟨"),
    Tier.HEALTHY: ("Healthy / 健康", "green", "🟩"),
    Tier.PROFIT: ("Profit / 盈利", "blue", "🟦"),
}
_TIER_ORDER = (Tier.BLOWUP, Tier.DANGER, Tier.BALANCED, Tier.HEALTHY, Tier.PROFIT)

# Display-only hints. The flag CODE remains the stable key and is always shown.
_FLAG_HINT: dict[str, str] = {
    "margin_call": "IB margin call",
    "maint_margin_critical": "MaintMargin / NetLiq > 90%",
    "el_critical": "ExcessLiquidity / NetLiq < 5%",
    "el_thin": "ExcessLiquidity / NetLiq < 10%",
    "advisor_close_pending": "Advisor CLOSE / TAKE_PROFIT decision staged",
    "uncovered_call": "Short call not covered by held shares",
    "symbol_over_cap": "Position over the per-symbol cap",
    "bucket_over_cap": "Bucket over the cap",
    "projected_el_negative": "Projected excess liquidity negative after assignment",
    "el_watch": "ExcessLiquidity / NetLiq 10-20%",
    "unknown_margin": "NAV or excess liquidity missing",
    "advisor_trim_pending": "Advisor TRIM decision staged",
    "prox_bucket": "Bucket within 5 pts of the cap",
    "prox_dte": "Short option DTE near the floor",
    "assignment_risk": "Short put delta past the assignment threshold",
    "unmanaged": "No universe row (D63): accidental position",
    "missing_market_value": "Concentration check skipped: no market value",
}

_TACTIC_ACTION = {"CLOSE": "close", "TRIM": "trim"}


# ---------------------------------------------------------------------------
# Pure: desired breach set + reconciliation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProposedAlert:
    symbol: str
    breach: str
    tier: str


@dataclass(frozen=True)
class AlertRow:
    id: int
    symbol: str
    breach: str
    tier: str
    state: str
    skip_until: datetime | None = None


@dataclass(frozen=True)
class ReconcilePlan:
    open_new: tuple[ProposedAlert, ...] = ()
    auto_clear: tuple[int, ...] = ()
    reopen: tuple[tuple[int, str], ...] = ()   # (id, tier)
    retier: tuple[tuple[int, str], ...] = ()   # (id, tier)

    def empty(self) -> bool:
        return not (self.open_new or self.auto_clear or self.reopen or self.retier)


def desired(report: GuardianReport) -> dict[tuple[str, str], str]:
    """Hard breaches present right now -> {(symbol, breach): tier}.

    Account-level flags use ``ACCOUNT_SCOPE``. Position flags keep their symbol.
    Proximity / info flags are deliberately absent: they never open alerts (D61).
    """
    out: dict[tuple[str, str], str] = {}
    for flag in report.account_flags:
        if flag in _HARD_FLAGS:
            tier = Tier.BLOWUP.value if flag in BLOWUP_FLAGS else Tier.DANGER.value
            out[(ACCOUNT_SCOPE, flag)] = tier
    for item in report.positions:
        if item.tier not in (Tier.DANGER, Tier.BLOWUP):
            continue
        for flag in item.flags:
            if flag in _HARD_FLAGS:
                tier = Tier.BLOWUP.value if flag in BLOWUP_FLAGS else Tier.DANGER.value
                out[(item.symbol, flag)] = tier
    return out


def reconcile(
    report: GuardianReport,
    alerts: Sequence[AlertRow],
    now: datetime,
) -> ReconcilePlan:
    """Pure state machine. ``alerts`` must be newest-first per key.

    One "current occurrence" per (symbol, breach): the first row seen for that
    key wins. Terminal rows are history and are never deleted (D62).
    """
    wanted = desired(report)
    current: dict[tuple[str, str], AlertRow] = {}
    for row in alerts:
        current.setdefault((row.symbol, row.breach), row)

    open_new: list[ProposedAlert] = []
    auto_clear: list[int] = []
    reopen: list[tuple[int, str]] = []
    retier: list[tuple[int, str]] = []

    for key, row in current.items():
        tier = wanted.get(key)
        if tier is None:
            # Breach gone. Any non-terminal state closes as auto_cleared.
            if row.state in ("open", "skipped", "resolved"):
                auto_clear.append(row.id)
            continue
        if row.state == "auto_cleared":
            # It really disappeared, then came back -> re-arm with a new row.
            open_new.append(ProposedAlert(key[0], key[1], tier))
        elif row.state == "open":
            if row.tier != tier:
                retier.append((row.id, tier))
        elif row.state == "skipped":
            expired = row.skip_until is None or row.skip_until <= now
            if expired:
                reopen.append((row.id, tier))   # SQL also refreshes tier
        # resolved: keep. No nag, no re-open on the very next run.

    live_keys = set(current)
    for key, tier in wanted.items():
        if key not in live_keys:
            open_new.append(ProposedAlert(key[0], key[1], tier))

    return ReconcilePlan(
        open_new=tuple(open_new),
        auto_clear=tuple(auto_clear),
        reopen=tuple(reopen),
        retier=tuple(retier),
    )


# ---------------------------------------------------------------------------
# Pure: snapshot assembly
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    positions: tuple[GuardianPosition, ...]
    marks: AccountMarks
    rules: GuardianRules
    report: GuardianReport
    base_currency: str


def _dec_opt(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _int_opt(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value))
    except (ValueError, TypeError):
        return None


def assemble(
    rows: Sequence[Mapping],
    drafts: Sequence[Mapping],
    settings,
    signals: Mapping[str, Mapping],
    facts: Mapping[str, Sequence[str]],
    nav: Decimal | None,
    margin: Decimal | None,
    excess: Decimal | None,
    base: str,
    rates: Mapping[str, Decimal],
) -> Snapshot:
    """Pure. Build the R1 inputs from caller-owned, already-fetched data.

    ``pending_action`` is gated by an actually-pending draft (D66 §10): when the
    draft is approved/rejected it stops being pending, the breach disappears,
    and the next ``sync()`` auto-clears the alert. No second ack path, no hook
    into ``drafts.decide()``.
    """
    from athena_orchestrator.money import convert

    base = base or "HKD"
    watchlist = {str(s).upper() for s in (getattr(settings, "watchlist", None) or ())}
    limits = getattr(settings, "risk", None)

    pending_by_symbol: dict[str, list[Mapping]] = {}
    for draft in drafts:
        symbol = str(draft.get("symbol") or "").upper()
        if symbol:
            pending_by_symbol.setdefault(symbol, []).append(draft)

    def to_base(value, currency: str) -> Decimal | None:
        amount = _dec_opt(value)
        if amount is None:
            return None
        src = (currency or base).upper()
        if src == base.upper():
            return amount
        converted = convert(amount, src, base, base, dict(rates))
        return converted  # None -> missing_market_value, never fabricated

    positions: list[GuardianPosition] = []
    for row in rows:
        symbol = str(row.get("symbol") or "").upper()
        qty = _dec_opt(row.get("qty"))
        if not symbol or qty is None or qty == 0:
            continue
        sec = str(row.get("sec_type") or "STK").upper()
        currency = str(row.get("currency") or "")
        classifiers = tuple(str(c).strip().upper() for c in (facts.get(symbol) or ()) if str(c).strip())

        pending_action = None
        if symbol in pending_by_symbol:
            signal = signals.get(symbol) or {}
            action = str(signal.get("action") or "").strip().lower()
            if action not in {"close", "take_profit", "trim"}:
                tactic = str(pending_by_symbol[symbol][0].get("tactic") or "").upper()
                action = _TACTIC_ACTION.get(tactic, "")
            pending_action = action or None

        positions.append(GuardianPosition(
            symbol=symbol,
            quantity=qty,
            sec_type=sec,
            right=str(row.get("right") or "").upper(),
            strike=_dec_opt(row.get("strike")),
            multiplier=_dec_opt(row.get("multiplier")) if sec == "OPT" else None,
            market_value=to_base(row.get("market"), currency),
            unrealized_pnl=to_base(row.get("pnl"), currency),
            day_pnl=None,
            dte=_int_opt(row.get("dte")),
            delta=None,                      # §11.3: no persisted delta in R2
            classifiers=classifiers,
            managed=symbol in watchlist,
            pending_action=pending_action,
        ))

    marks = AccountMarks(
        net_liquidation=nav,
        excess_liquidity=excess,
        maint_margin_req=margin,
        margin_call=False,                  # §11.3: no persisted IB flag in R2
        base_currency=base,
    )
    rules = GuardianRules.from_limits(limits) if limits is not None else GuardianRules()
    report = grade(positions, marks, rules)
    return Snapshot(tuple(positions), marks, rules, report, base)


# ---------------------------------------------------------------------------
# Thin SQL
# ---------------------------------------------------------------------------

_ALERT_COLUMNS = "id, symbol, breach, tier, state, skip_until"


def load_alerts(conn, account: str) -> list[AlertRow]:
    """All rows for the account, newest first (per-key occurrence = first seen)."""
    if not account:
        return []
    found = conn.execute(
        f"""
        SELECT {_ALERT_COLUMNS}
        FROM guardian_alerts
        WHERE account_id = %s
        ORDER BY id DESC
        """,
        (account,),
    ).fetchall()
    return [
        AlertRow(int(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4]), r[5])
        for r in found
    ]


def open_alert_count(conn, account: str) -> int:
    if not account:
        return 0
    row = conn.execute(
        "SELECT count(*) FROM guardian_alerts WHERE account_id = %s AND state = 'open'",
        (account,),
    ).fetchone()
    return 0 if row is None else int(row[0])


def apply_plan(conn, account: str, plan: ReconcilePlan, now: datetime) -> None:
    """Execute a ReconcilePlan. Every statement is parameterised and account-scoped."""
    for item in plan.open_new:
        conn.execute(
            """
            INSERT INTO guardian_alerts
                (account_id, symbol, breach, tier, state, source, created_at, updated_at)
            VALUES (%s, %s, %s, %s, 'open', 'grade', %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (account, item.symbol, item.breach, item.tier, now, now),
        )
    for alert_id in plan.auto_clear:
        conn.execute(
            """
            UPDATE guardian_alerts
               SET state = 'auto_cleared', resolved_at = %s, updated_at = %s
             WHERE id = %s AND account_id = %s AND state <> 'auto_cleared'
            """,
            (now, now, alert_id, account),
        )
    for alert_id, tier in plan.reopen:
        conn.execute(
            """
            UPDATE guardian_alerts
               SET state = 'open', tier = %s, skip_until = NULL, updated_at = %s
             WHERE id = %s AND account_id = %s AND state = 'skipped'
            """,
            (tier, now, alert_id, account),
        )
    for alert_id, tier in plan.retier:
        conn.execute(
            """
            UPDATE guardian_alerts
               SET tier = %s, updated_at = %s
             WHERE id = %s AND account_id = %s AND state = 'open'
            """,
            (tier, now, alert_id, account),
        )


def _live_state(conn, account: str, alert_id: int) -> str:
    row = conn.execute(
        "SELECT state FROM guardian_alerts WHERE id = %s AND account_id = %s",
        (alert_id, account),
    ).fetchone()
    if row is None:
        raise ValueError("Unknown alert.")
    if str(row[0]) not in _LIVE_STATES:
        raise ValueError("That alert is already closed.")
    return str(row[0])


def resolve(conn, account: str, alert_id: int, actor: str, note: str, now: datetime) -> None:
    """D62 close path 1: operator clicked Resolve (note optional)."""
    _live_state(conn, account, alert_id)
    conn.execute(
        """
        UPDATE guardian_alerts
           SET state = 'resolved', resolved_at = %s, ack_actor = %s,
               ack_note = %s, skip_until = NULL, updated_at = %s
         WHERE id = %s AND account_id = %s
        """,
        (now, actor, (note or "").strip(), now, alert_id, account),
    )


def skip(conn, account: str, alert_id: int, actor: str, reason: str, now: datetime) -> None:
    """D62 close path 2: Skip + reason, sleeps 24h then reopens if still real."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("A reason is required to skip.")
    _live_state(conn, account, alert_id)
    conn.execute(
        """
        UPDATE guardian_alerts
           SET state = 'skipped', skip_until = %s, ack_actor = %s,
               ack_note = %s, resolved_at = NULL, updated_at = %s
         WHERE id = %s AND account_id = %s
        """,
        (now + timedelta(hours=SKIP_HOURS), actor, reason, now, alert_id, account),
    )


def build_snapshot(conn, account: str, rows, drafts, settings) -> Snapshot:
    """Read the store and assemble the R1 snapshot. R3 reuses this verbatim."""
    from athena_exec.slots import HKT
    from athena_orchestrator.money import ledger
    from athena_strategy.advisor import today_signals

    if not account:
        return assemble([], [], settings, {}, {}, None, None, None, "HKD", {})

    day = datetime.now(HKT).date()
    signals = today_signals(conn, account, day)

    facts: dict[str, tuple[str, ...]] = {}
    for symbol, raw in conn.execute(
        "SELECT symbol, classifiers FROM symbol_facts"
    ).fetchall():
        facts[str(symbol).upper()] = tuple(
            part.strip().upper() for part in str(raw or "").split(",") if part.strip()
        )

    nav_row = conn.execute(
        "SELECT last_nav FROM nav_marks WHERE account_id = %s ORDER BY day DESC LIMIT 1",
        (account,),
    ).fetchone()
    mark_row = conn.execute(
        """
        SELECT margin, excess_liquidity
        FROM account_marks WHERE account_id = %s ORDER BY day DESC LIMIT 1
        """,
        (account,),
    ).fetchone()
    base, _balances, rates = ledger(conn, account)

    nav = _dec_opt(nav_row[0]) if nav_row else None
    margin = _dec_opt(mark_row[0]) if mark_row else None
    excess = _dec_opt(mark_row[1]) if mark_row else None
    return assemble(rows, drafts, settings, signals, facts, nav, margin, excess, base, rates)


def sync(
    conn, account: str, rows, drafts, settings, now: datetime | None = None,
) -> tuple[Snapshot, list[AlertRow]]:
    """One guardian run: grade, reconcile, apply, reload. Returns (snapshot, alerts)."""
    moment = now or datetime.now(timezone.utc)
    snapshot = build_snapshot(conn, account, rows, drafts, settings)
    existing = load_alerts(conn, account)
    plan = reconcile(snapshot.report, existing, moment)
    if not plan.empty():
        apply_plan(conn, account, plan, moment)
        conn.commit()
        existing = load_alerts(conn, account)
    return snapshot, existing


# ---------------------------------------------------------------------------
# Pure: HTML rendering
# ---------------------------------------------------------------------------


def _flag_chip(code: str) -> str:
    hint = _FLAG_HINT.get(code, "")
    title = f' title="{escape(hint, quote=True)}"' if hint else ""
    return f'<code class="guard-flag"{title}>{escape(code)}</code>'


def _draft_links(symbol: str, drafts_by_symbol: Mapping[str, list[Mapping]]) -> str:
    items = drafts_by_symbol.get(symbol.upper(), [])
    if not items:
        return ""
    links = " ".join(
        f'<a href="/drafts">{escape(str(d.get("id") or ""))}</a>' for d in items
    )
    return f'<span class="guard-drafts">pending draft {links}</span>'


def _ack_forms(alert: AlertRow) -> str:
    ident = escape(str(alert.id), quote=True)
    return (
        '<div class="guard-ack">'
        f'<form method="post" action="/guard">'
        f'<input type="hidden" name="alert_id" value="{ident}">'
        '<input type="hidden" name="decision" value="resolve">'
        '<input class="input" type="text" name="note" placeholder="note (optional)">'
        '<button class="btn ok" type="submit">Resolve</button></form>'
        f'<form method="post" action="/guard">'
        f'<input type="hidden" name="alert_id" value="{ident}">'
        '<input type="hidden" name="decision" value="skip">'
        '<input class="input" type="text" name="reason" placeholder="reason (required)" required>'
        '<button class="btn ghost" type="submit">Skip 24h</button></form>'
        "</div>"
    )


def _position_line(item: PositionGrade, drafts_by_symbol) -> str:
    flags = " ".join(_flag_chip(f) for f in item.flags) or '<span class="tag">clean</span>'
    extra = ""
    if item.unmanaged:
        extra = '<span class="tag amber">unmanaged</span>'
    links = _draft_links(item.symbol, drafts_by_symbol)
    return (
        '<div class="guard-row">'
        f'<span class="guard-symbol">{escape(item.symbol)}</span>'
        f'<span class="guard-flags">{flags}{extra}</span>'
        f"{links}</div>"
    )


def render_board(
    report: GuardianReport,
    alerts: Sequence[AlertRow],
    drafts: Sequence[Mapping],
    note: str = "",
    store_error: str = "",
    base_currency: str = "",
) -> str:
    """Pure. Five-colour board + per-alert ack forms. All dynamic text escaped."""
    drafts_by_symbol: dict[str, list[Mapping]] = {}
    for draft in drafts:
        symbol = str(draft.get("symbol") or "").upper()
        if symbol:
            drafts_by_symbol.setdefault(symbol, []).append(draft)

    label, tone, glyph = _TIER_META[report.portfolio_tier]
    el = "n/a" if report.el_nl_pct is None else f"{report.el_nl_pct:.1f}%"
    mm = "n/a" if report.maint_nl_pct is None else f"{report.maint_nl_pct:.1f}%"
    account_flags = " ".join(_flag_chip(f) for f in report.account_flags) or '<span class="tag green">ok</span>'

    parts: list[str] = []
    if store_error:
        parts.append(
            '<div class="card guard-error"><h2>Alert store unavailable</h2>'
            f'<p>Tiers are computed live. Run migrations to enable alerts.</p>'
            f'<pre>{escape(store_error)}</pre></div>'
        )

    open_alerts = [a for a in alerts if a.state in _LIVE_STATES]
    parts.append(
        '<div class="card guard-summary">'
        f'<h2>Portfolio <span class="tag {tone}">{glyph} {escape(label)}</span></h2>'
        '<div class="guard-stats">'
        f'<div><span class="k">EL / NetLiq</span><span class="v">{escape(el)}</span></div>'
        f'<div><span class="k">Maint / NetLiq</span><span class="v">{escape(mm)}</span></div>'
        f'<div><span class="k">Hard breaches</span><span class="v">{report.breach_count}</span></div>'
        f'<div><span class="k">Proximity</span><span class="v">{report.proximity_count}</span></div>'
        f'<div><span class="k">Unmanaged</span><span class="v">{report.unmanaged_count}</span></div>'
        f'<div><span class="k">Open alerts</span><span class="v">{len(open_alerts)}</span></div>'
        "</div>"
        f'<div class="guard-account"><span class="k">Account</span> {account_flags}'
        f'{" <span class=k>base</span> " + escape(base_currency) if base_currency else ""}</div>'
        "</div>"
    )

    for tier in _TIER_ORDER:
        tlabel, ttone, tglyph = _TIER_META[tier]
        members = [g for g in report.positions if g.tier == tier]
        head = (
            f'<h2><span class="tag {ttone}">{tglyph} {escape(tlabel)}</span>'
            f' <span class="guard-count">{len(members)}</span></h2>'
        )
        if not members:
            parts.append(f'<div class="card guard-tier">{head}<p class="guard-empty">— none</p></div>')
            continue
        lines: list[str] = []
        for item in members:
            lines.append(_position_line(item, drafts_by_symbol))
            # Danger/blowup rows carry the ack forms for their live alerts.
            if tier in (Tier.DANGER, Tier.BLOWUP):
                for alert in [a for a in open_alerts if a.symbol == item.symbol]:
                    lines.append(
                        '<div class="guard-alert">'
                        f'<span class="tag {ttone}">{escape(alert.breach)}</span>'
                        f'<span class="guard-state">{escape(alert.state)}</span>'
                        f"{_ack_forms(alert)}</div>"
                    )
        parts.append(f'<div class="card guard-tier">{head}{"".join(lines)}</div>')

    # Account-level alerts have no position row; render them separately.
    account_rows = [a for a in open_alerts if a.symbol == ACCOUNT_SCOPE]
    if account_rows:
        lines = []
        for alert in account_rows:
            lines.append(
                '<div class="guard-alert">'
                f'<span class="tag black">{escape(alert.breach)}</span>'
                f'<span class="guard-state">{escape(alert.state)}</span>'
                f"{_ack_forms(alert)}</div>"
            )
        parts.append(
            '<div class="card guard-tier"><h2>Account-level breaches</h2>'
            + "".join(lines) + "</div>"
        )

    closed = [a for a in alerts if a.state not in _LIVE_STATES]
    if closed:
        rows_html = "".join(
            "<tr>"
            f"<td>{escape(a.symbol)}</td><td>{escape(a.breach)}</td>"
            f"<td><span class=\"tag\">{escape(a.state)}</span></td>"
            f"<td>{escape(a.tier)}</td></tr>"
            for a in closed[:20]
        )
        parts.append(
            '<div class="card"><h2>Recently closed (kept, never deleted)</h2>'
            '<table class="guard-history"><thead><tr><th>Symbol</th><th>Breach</th>'
            f'<th>State</th><th>Tier</th></tr></thead><tbody>{rows_html}</tbody></table></div>'
        )

    # Flash/toast convention: the note must be the FIRST child of body so
    # console._pull_flash() lifts it out of the page.
    prefix = f'<p class="note">{escape(note)}</p>' if note else ""
    return prefix + '<div class="guard-stack">' + "".join(parts) + "</div>"
