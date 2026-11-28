"""Read-only Iris architecture snapshot (V2-P2 + V2-P4 shadow additions).

This module stays a pure read model: no animation code, no polling, no event
stream, no domain writer, no migration and no trading logic. It renders the
graph from existing tables and reuses the one existing draft queue. The
event-driven connector pulse lives in the console client script and is fed
only by structured ``strategy_events`` frames; with no event there is no
animation.

V2-P4 additions are read-only recomputations: the daily gap audit (Q7) and
the Strategy 3 weekly shadow summary (Q9 signal subset) run on every
snapshot from ``market_bars`` / ``calendar_marks`` and never persist
anything. Live ``ib-daily`` bars carry no adjusted-close proof, so the
shadow runs with ``adjustment_basis=None`` and every week honestly reports
``no_data`` + ``adjustment_unavailable`` (§28.3 R3) until an adjusted
producer exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo

from athena_strategy.gap_audit import STATE_COMPLETE, STATE_PARTIAL, STATE_UNKNOWN, audit_daily
from athena_store import news_read

GRAPH_VERSION = "iris-p2-events-v1"

_ET = ZoneInfo("America/New_York")

# SPX is read under its own symbol; SPY is an ETF and is deliberately not
# accepted as an SPX proxy without a ruling (§28.3 R4).


@dataclass(frozen=True)
class Node:
    node_id: str
    label: str
    x: int
    y: int
    width: int = 220
    height: int = 58
    column: str = "input"


@dataclass(frozen=True)
class Edge:
    edge_id: str
    source: str
    target: str
    flow: str


NODES: tuple[Node, ...] = (
    Node("news", "News", 30, 70, column="input"),
    Node("calendar", "Calendar", 30, 150, column="input"),
    Node("daily_bars", "Daily bars · 52W", 30, 230, column="input"),
    Node("price_ticks", "Delayed price prints", 30, 310, column="input"),
    Node("vix", "VIX / VIX9D family", 30, 390, column="input"),
    Node("option_plans", "Option plans", 30, 470, column="input"),
    Node("positions", "Held positions", 30, 550, column="input"),
    Node("news_sentiment", "Sentiment / NEWS_WASH", 320, 70, column="factor"),
    Node("event_tags", "Event / earnings tags", 320, 150, column="factor"),
    Node("price_location", "52W position / rails", 320, 230, column="factor"),
    Node("realized_vol", "RV20 / RV percentile", 320, 310, column="factor"),
    Node("regime_context", "Regime context", 320, 390, column="factor"),
    Node("premium_ratio", "Premium ratio rank", 320, 470, column="factor"),
    Node("advisor_score", "Advisor score", 320, 550, column="factor"),
    Node("strategy_1", "V2-S1 · NFR Wheel", 610, 190, column="strategy"),
    Node("strategy_2", "V2-S2 · Advisor / Manage", 610, 370, column="strategy"),
    Node("drafts", "Existing drafts", 900, 190, column="action"),
    Node("approval", "Human approval", 900, 330, column="action"),
    Node("orders", "Orders / fills", 900, 470, column="action"),
    Node("operator_direct", "Operator direct paths", 900, 610, column="direct"),
)

EDGES: tuple[Edge, ...] = (
    Edge("news-sentiment", "news", "news_sentiment", "data"),
    Edge("news-strategy", "news_sentiment", "strategy_1", "factor"),
    Edge("calendar-tags", "calendar", "event_tags", "data"),
    Edge("tags-strategy", "event_tags", "strategy_1", "factor"),
    Edge("daily-location", "daily_bars", "price_location", "data"),
    Edge("ticks-location", "price_ticks", "price_location", "data"),
    Edge("location-strategy", "price_location", "strategy_1", "factor"),
    Edge("daily-rv", "daily_bars", "realized_vol", "data"),
    Edge("rv-strategy", "realized_vol", "strategy_1", "factor"),
    Edge("vix-regime", "vix", "regime_context", "data"),
    Edge("regime-strategy", "regime_context", "strategy_1", "factor"),
    Edge("options-premium", "option_plans", "premium_ratio", "data"),
    Edge("premium-advisor", "premium_ratio", "strategy_2", "factor"),
    Edge("positions-score", "positions", "advisor_score", "data"),
    Edge("score-advisor", "advisor_score", "strategy_2", "factor"),
    Edge("strategy1-drafts", "strategy_1", "drafts", "action"),
    Edge("strategy2-drafts", "strategy_2", "drafts", "action"),
    Edge("drafts-approval", "drafts", "approval", "approval"),
    Edge("approval-orders", "approval", "orders", "execution"),
    Edge("direct-orders", "operator_direct", "orders", "direct"),
)


def _scalar(conn, sql: str, params=(), default=0):
    row = conn.execute(sql, params).fetchone()
    if not row or row[0] is None:
        return default
    return row[0]


def _stamp(value) -> str:
    if value is None:
        return "unknown"
    return str(value)[:19]


def _et_today():
    return datetime.now(_ET).date()


def _p4_recompute(conn, settings) -> dict:
    """Read-only daily gap audit (S0a: Strategy 3 shadow removed).

    Reads ``market_bars`` only, writes nothing. S0b+ will rebuild this into
    the SMA case engine; the P4 gap-audit plumbing is deliberately kept.
    """
    empty = {
        "daily_gap_state": STATE_UNKNOWN,
        "daily_gap_count": 0,
        "daily_gap_symbols": 0,
    }
    rows = conn.execute(
        "SELECT symbol, ts::date, open, high, low, close, volume "
        "FROM market_bars WHERE source = 'ib-daily' ORDER BY symbol, ts"
    ).fetchall()
    if not rows:
        return empty
    by_symbol: dict[str, list[tuple]] = {}
    for symbol, day, o, high, low, close, volume in rows:
        by_symbol.setdefault(str(symbol).upper(), []).append(
            (day, o, high, low, close, volume)
        )
    watchlist = [s.upper() for s in (getattr(settings, "watchlist", []) or [])]
    symbols = sorted(
        set(watchlist) & set(by_symbol) if watchlist else sorted(by_symbol)
    )
    today = _et_today()
    audits = []
    for symbol in symbols:
        daily = by_symbol[symbol]
        audit = audit_daily(symbol, [(d, v) for d, *_rest, v in daily], as_of=today)
        audits.append(audit)
    states = {a.state for a in audits} - {STATE_UNKNOWN}
    if not states:
        return empty
    return {
        "daily_gap_state": STATE_PARTIAL if STATE_PARTIAL in states else STATE_COMPLETE,
        "daily_gap_count": sum(len(a.gaps) for a in audits),
        "daily_gap_symbols": len(audits),
    }


def snapshot(conn, account: str, settings) -> dict:
    """Read existing tables only. Never writes, commits or calls a strategy."""
    daily_bars = int(
        _scalar(
            conn,
            "SELECT count(DISTINCT ts::date) FROM market_bars WHERE source = 'ib-daily'",
        )
    )
    daily_first = _scalar(
        conn,
        "SELECT min(ts::date) FROM market_bars WHERE source = 'ib-daily'",
        default=None,
    )
    daily_last = _scalar(
        conn,
        "SELECT max(ts::date) FROM market_bars WHERE source = 'ib-daily'",
        default=None,
    )
    delayed_prints = int(
        _scalar(
            conn,
            "SELECT count(*) FROM market_bars WHERE source = 'ib-delayed'",
        )
    )
    vix_bars = int(
        _scalar(
            conn,
            # psycopg parses % as a placeholder even with empty params, so the
            # LIKE wildcard must be doubled. A bare 'VIX%' crashes on real PG.
            "SELECT count(*) FROM market_bars WHERE symbol LIKE 'VIX%%' "
            "AND source IN ('ib-daily', 'ib-vix')",
        )
    )
    news_rows = int(
        _scalar(conn, news_read.render("SELECT count(*) FROM news_events WHERE NOT dismissed"))
    )
    news_last = _scalar(
        conn,
        news_read.render("SELECT max(ts) FROM news_events WHERE NOT dismissed"),
        default=None,
    )
    calendar_marks = int(_scalar(conn, "SELECT count(*) FROM calendar_marks"))
    iv_marks = int(_scalar(conn, "SELECT count(*) FROM iv_marks"))
    phase_rows = int(
        _scalar(
            conn,
            "SELECT count(*) FROM symbol_phases WHERE account_id = %s",
            (account,),
        )
    )
    advisor_rows = int(
        _scalar(
            conn,
            "SELECT count(*) FROM advisor_signals WHERE account_id = %s AND day = CURRENT_DATE",
            (account,),
        )
    )
    regime_rows = int(
        _scalar(
            conn,
            "SELECT count(*) FROM regime_labels WHERE account_id = %s",
            (account,),
        )
    )
    pending = int(
        _scalar(
            conn,
            "SELECT count(*) FROM drafts WHERE account_id = %s AND status = 'pending'",
            (account,),
        )
    )
    approvals = int(
        _scalar(
            conn,
            "SELECT count(*) FROM approvals WHERE account_id = %s",
            (account,),
        )
    )
    queued = int(
        _scalar(
            conn,
            "SELECT count(*) FROM orders WHERE account_id = %s AND status = 'queued'",
            (account,),
        )
    )
    submitted = int(
        _scalar(
            conn,
            "SELECT count(*) FROM orders WHERE account_id = %s AND status = 'submitted'",
            (account,),
        )
    )
    fills = int(
        _scalar(
            conn,
            "SELECT count(*) FROM fills WHERE account_id = %s",
            (account,),
        )
    )
    open_alerts = int(
        _scalar(
            conn,
            "SELECT count(*) FROM guardian_alerts WHERE account_id = %s AND state = 'open'",
            (account,),
        )
    )
    positions = int(
        _scalar(
            conn,
            "SELECT count(DISTINCT symbol) FROM positions WHERE account_id = %s AND quantity <> 0",
            (account,),
        )
    )
    return {
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "account": account,
        "watchlist_count": len(getattr(settings, "watchlist", []) or []),
        "daily_bars": daily_bars,
        "daily_first": _stamp(daily_first),
        "daily_last": _stamp(daily_last),
        **_p4_recompute(conn, settings),
        "delayed_prints": delayed_prints,
        "vix_bars": vix_bars,
        "news_rows": news_rows,
        "news_last": _stamp(news_last),
        "calendar_marks": calendar_marks,
        "iv_marks": iv_marks,
        "phase_rows": phase_rows,
        "advisor_rows": advisor_rows,
        "regime_rows": regime_rows,
        "pending": pending,
        "approvals": approvals,
        "queued": queued,
        "submitted": submitted,
        "fills": fills,
        "open_alerts": open_alerts,
        "positions": positions,
        "graph_version": GRAPH_VERSION,
        "run_evidence": "unavailable",
    }


def _state(value: int, full: int | None = None) -> str:
    if not value:
        return "no_data"
    if full and value < full:
        return "partial"
    return "ready"


def _node_states(snap: dict) -> dict[str, tuple[str, str]]:
    return {
        "news": (_state(snap["news_rows"]), f"{snap['news_rows']} stored rows"),
        "calendar": (
            _state(snap["calendar_marks"]),
            f"{snap['calendar_marks']} marks",
        ),
        "daily_bars": (
            _state(snap["daily_bars"], 252),
            f"{snap['daily_bars']} ib-daily sessions; gaps "
            f"{snap['daily_gap_state']} ({snap['daily_gap_count']})",
        ),
        "price_ticks": (
            _state(snap["delayed_prints"]),
            f"{snap['delayed_prints']} delayed prints",
        ),
        "vix": (_state(snap["vix_bars"]), f"{snap['vix_bars']} VIX-family bars"),
        "option_plans": (
            _state(snap["iv_marks"]),
            f"{snap['iv_marks']} premium-ratio marks",
        ),
        "positions": (
            _state(snap["positions"]),
            f"{snap['positions']} held symbols",
        ),
        "news_sentiment": ("unknown", "run evidence unavailable"),
        "event_tags": ("unknown", "run evidence unavailable"),
        "price_location": ("unknown", "run evidence unavailable"),
        "realized_vol": ("unknown", "run evidence unavailable"),
        "regime_context": (
            "ready" if snap["regime_rows"] else "unknown",
            f"{snap['regime_rows']} regime labels",
        ),
        "premium_ratio": ("unknown", "not measured IV"),
        "advisor_score": (
            "ready" if snap["advisor_rows"] else "unknown",
            f"{snap['advisor_rows']} today signals",
        ),
        "strategy_1": ("configured", "current opening path"),
        "strategy_2": ("configured", "current management path"),
        "drafts": (
            "ready" if snap["pending"] else "idle",
            f"{snap['pending']} pending",
        ),
        "approval": (
            "ready" if snap["approvals"] else "idle",
            f"{snap['approvals']} decisions",
        ),
        "orders": (
            "ready" if snap["queued"] or snap["submitted"] else "idle",
            f"{snap['queued']} queued / {snap['submitted']} submitted",
        ),
        "operator_direct": ("operator", "manual / liquidate / external"),
    }


def render_header(snap: dict) -> str:
    stats = (
        ("Account", snap["account"]),
        ("As of", snap["as_of"]),
        ("Watchlist", str(snap["watchlist_count"])),
        ("52W daily sessions", str(snap["daily_bars"])),
        ("First / last bar", f"{snap['daily_first']} / {snap['daily_last']}"),
        ("News rows", str(snap["news_rows"])),
        ("Pending drafts", str(snap["pending"])),
    )
    cells = "".join(
        f'<div><span class="k">{escape(str(label))}</span>'
        f'<span class="v mono">{escape(str(value))}</span></div>'
        for label, value in stats
    )
    if snap.get("daily_gap_state", "unknown") == "unknown":
        gap_note = (
            "The 52-week count is stored daily sessions; internal gaps are unknown."
        )
    else:
        gap_note = (
            f"The 52-week gap audit found {snap['daily_gap_count']} gap(s) "
            f"across {snap['daily_gap_symbols']} symbol(s): "
            f"{snap['daily_gap_state']}."
        )
    return (
        '<div class="card iris-header" data-iris-static="true" '
        f'data-graph-version="{escape(snap["graph_version"], quote=True)}">'
        '<h2>Iris snapshot</h2>'
        f'<div class="iris-stats">{cells}</div>'
        '<p class="slot-note">Read-only graph. Connectors pulse only on a structured run event; '
        f'no event means no animation. {gap_note}</p>'
        "</div>"
    )


def render_guardian_band(snap: dict) -> str:
    return (
        '<div class="card iris-guardian"><h2>Guardian side band</h2>'
        '<p class="iris-band-line">Portfolio conformance and alerts. Guardian observes; it is not a strategy and creates no drafts.</p>'
        f'<p class="slot-note"><span class="mono">{snap["open_alerts"]}</span> open alerts · '
        f'<span class="mono">{snap["regime_rows"]}</span> Run C labels · read-only</p>'
        "</div>"
    )


def _edge_path(source: Node, target: Node) -> str:
    y1 = source.y + source.height / 2
    y2 = target.y + target.height / 2
    if source.x >= target.x - 50:
        x1 = source.x + source.width
        x2 = target.x + target.width
        return (
            f"M {x1:.1f},{y1:.1f} C {x1 + 42:.1f},{y1:.1f} "
            f"{x2 + 42:.1f},{y2:.1f} {x2:.1f},{y2:.1f}"
        )
    x1 = source.x + source.width
    x2 = target.x
    return (
        f"M {x1:.1f},{y1:.1f} C {x1 + 48:.1f},{y1:.1f} "
        f"{x2 - 48:.1f},{y2:.1f} {x2:.1f},{y2:.1f}"
    )


def render_graph(snap: dict) -> str:
    nodes = {node.node_id: node for node in NODES}
    states = _node_states(snap)
    parts = [
        f'<svg class="iris-graph" viewBox="0 0 1160 710" role="img" '
        f'aria-label="Iris static architecture graph" data-graph-version="{escape(snap["graph_version"], quote=True)}">',
        '<defs><marker id="iris-arrow" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#4b5768"/></marker></defs>',
    ]
    for edge in EDGES:
        source = nodes[edge.source]
        target = nodes[edge.target]
        dash = ' stroke-dasharray="6 5"' if edge.flow in {"shadow", "direct"} else ""
        parts.append(
            f'<path class="iris-edge" id="edge-{escape(edge.edge_id, quote=True)}" '
            f'data-graph-role="edge" data-edge-id="{escape(edge.edge_id, quote=True)}" '
            f'data-source="{escape(edge.source, quote=True)}" '
            f'data-target="{escape(edge.target, quote=True)}" '
            f'data-flow="{escape(edge.flow, quote=True)}" '
            f'd="{_edge_path(source, target)}" marker-end="url(#iris-arrow)"{dash}/>'
        )
    for node in NODES:
        status, detail = states[node.node_id]
        label = escape(node.label)
        short = escape(detail[:42])
        parts.append(
            f'<g class="iris-node" id="node-{escape(node.node_id, quote=True)}" '
            f'data-graph-role="node" data-node-id="{escape(node.node_id, quote=True)}" '
            f'data-column="{escape(node.column, quote=True)}" '
            f'data-status="{escape(status, quote=True)}" tabindex="0" '
            f'aria-label="{label}: {escape(status, quote=True)}; {short}">'
            f'<rect x="{node.x}" y="{node.y}" width="{node.width}" height="{node.height}" rx="8"/>'
            f'<text class="iris-node-label" x="{node.x + 12}" y="{node.y + 23}">{label}</text>'
            f'<text class="iris-node-detail" x="{node.x + 12}" y="{node.y + 43}">{short}</text>'
            f"<title>{label} · {escape(detail)}</title></g>"
        )
    parts.append("</svg>")
    return (
        '<div class="card iris-graph-card"><h2>Architecture · static</h2>'
        '<div class="iris-graph-wrap">' + "".join(parts) + "</div>"
        '<p class="slot-note">Lines describe current architecture and planned shadow edges. '
        'A connector moves only when a matching structured run event arrives.</p>'
        '<p class="slot-note iris-live-status" data-iris-live="idle" role="status">'
        'Event stream idle. No structured run events yet.</p>'
        "</div>"
    )


def render_draft_rail(drafts: list[dict]) -> str:
    if not drafts:
        rows = (
            '<tr><td colspan="5"><p class="guard-empty">No draft is waiting.</p></td></tr>'
        )
    else:
        rows = "".join(
            "<tr>"
            f'<td><a href="/drafts">{escape(str(item["id"]))}</a></td>'
            f'<td>{escape(str(item["symbol"]))}</td>'
            f'<td>{escape(str(item["tactic"]))}</td>'
            f'<td><span class="mono">insufficient_provenance</span></td>'
            f'<td class="advisor-reason">{escape(str(item.get("reason") or ""))}</td>'
            "</tr>"
            for item in drafts
        )
    return (
        '<div class="card fill iris-draft-rail"><h2>Action rail · existing drafts</h2>'
        '<div class="scroll-y"><table class="data"><thead><tr>'
        "<th>Draft</th><th>Symbol</th><th>Tactic</th><th>Lineage</th><th>Reason</th>"
        f"</tr></thead><tbody>{rows}</tbody></table></div>"
        '<p class="slot-note">One action system. Approve, amend or reject on <a href="/drafts">/drafts</a>. '
        'Legacy drafts have no persisted strategy / factor / run identity, so lineage is never guessed.</p>'
        "</div>"
    )
