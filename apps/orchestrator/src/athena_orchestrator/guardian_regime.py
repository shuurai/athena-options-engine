"""R7 Run C midday regime observer (booklet 14, §13.4/§13.9/§24; D67–D108).

Layering (same split as R3's guardian_runner):

* pure:      ``athena_strategy.regime`` (matrix) and ``athena_marketdata.vixfeed``
             (VIX state derivation) — both zero I/O
* schedule:  ``guardian_schedule`` — A/B/C are already derived there
* I/O here:  read ``market_bars`` + ``news_events``, assemble ``Axes``, write one
             ``regime_labels`` row, render the read-only card, run the daemon

Run C is executed **here only**. ``EXECUTED_RUNS`` stays ``("A", "B")`` so R3's
scheduler never fires C and R7's daemon never fires A/B (D71). Two threads, two
scopes, one shared pure schedule module.

What this batch deliberately cannot measure (D105/D106, §24.6) is recorded as
``unknown`` **with an in-place reason**, never guessed and never defaulted to
zero: 10Y has no feed at all, single-name IV is unmeasured, and intraday volume
is structurally unmeasurable because ``upsert_delayed()`` stores ``volume=0``.
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timezone

from psycopg.types.json import Jsonb

from athena_marketdata import vixfeed
from athena_orchestrator.guardian_schedule import RunSlot, hk_clock, next_run
from athena_strategy.regime import Axes, Regime, evaluate

__all__ = [
    "REFERENCE",
    "GOLD",
    "build_axes",
    "observe",
    "write_label",
    "latest_label",
    "prompt_line",
    "render_card",
    "run_once",
    "start_regime_scheduler",
    "stop_regime_scheduler",
]

log = logging.getLogger("athena")

# §13.4 speaks of "股" and "金" without naming an index. SPY and GLD are the two
# broad proxies already in UNIVERSE_SEED, so they need no new subscription.
REFERENCE = "SPY"
GOLD = "GLD"

# 待校準: below 5 bp a delayed print is indistinguishable from unchanged, so the
# direction is reported as ``flat`` rather than invented as up/down.
DIRECTION_EPS = 0.0005

RUN = "C"

_STOP = threading.Event()
_THREAD: threading.Thread | None = None

# D69: append the observation, but always keep the newest reading. This differs
# from guardian_runs' DO NOTHING on purpose — a grade is an audit record and must
# freeze, whereas a regime is a read-only observation of "now" and the operator
# may re-sync VIX after 12:30 and re-observe. created_at keeps the first sighting.
_WRITE_SQL = """
INSERT INTO regime_labels
    (account_id, trade_day, run, label, close_bias, evidence, inputs)
VALUES (%s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (account_id, trade_day, run) DO UPDATE SET
    label = EXCLUDED.label,
    close_bias = EXCLUDED.close_bias,
    evidence = EXCLUDED.evidence,
    inputs = EXCLUDED.inputs
"""

_LATEST_SQL = """
SELECT trade_day, run, label, close_bias, evidence, inputs, created_at
FROM regime_labels
WHERE account_id = %s
ORDER BY trade_day DESC, created_at DESC, id DESC
LIMIT 1
"""

# --- thin reads -------------------------------------------------------------

_INTRADAY_SQL = """
SELECT ts, close
FROM market_bars
WHERE symbol = %s
  AND source = 'ib-delayed'
  AND close > 0
  AND (ts AT TIME ZONE 'America/New_York')::date = %s
ORDER BY ts
"""

_PRIOR_CLOSE_SQL = """
SELECT close
FROM market_bars
WHERE symbol = %s
  AND source IN ('ib-daily', 'ib-delayed')
  AND close > 0
  AND (ts AT TIME ZONE 'America/New_York')::date < %s
ORDER BY ts DESC
LIMIT 1
"""

_NEWS_SQL = """
SELECT event_type
FROM news_events
WHERE (ts AT TIME ZONE 'America/New_York')::date = %s
"""


def _intraday(conn, symbol: str, trade_day: date) -> list[float]:
    """Today's delayed prints, oldest first. Empty when nothing was polled."""
    rows = conn.execute(_INTRADAY_SQL, (symbol.upper(), trade_day)).fetchall()
    out = []
    for _ts, close in rows:
        try:
            value = float(close)
        except (TypeError, ValueError):
            continue
        if value > 0:
            out.append(value)
    return out


def _prior_close(conn, symbol: str, trade_day: date) -> float | None:
    row = conn.execute(_PRIOR_CLOSE_SQL, (symbol.upper(), trade_day)).fetchone()
    if row is None:
        return None
    try:
        value = float(row[0])
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _direction(latest: float, reference: float) -> str:
    """up / down / flat. Flat is a measurement, not an absence."""
    if reference <= 0:
        return "flat"
    move = (latest - reference) / reference
    if abs(move) < DIRECTION_EPS:
        return "flat"
    return "up" if move > 0 else "down"


def _news_label(conn, trade_day: date) -> str:
    """``narrative_only`` / ``none``. ``regime_shift`` is unreachable (§24.6).

    §13.4 makes ``regime_shift`` a three-way conjunction (Fed speech + regulatory
    action + a bond-market move). ``score_title()`` emits five coarse codes and
    there is no bond feed, so "缺一不算" leaves it unassertable. Single-name
    earnings without a Fed event is exactly the ``narrative_only`` bucket.
    """
    rows = conn.execute(_NEWS_SQL, (trade_day,)).fetchall()
    kinds = {str(row[0] or "") for row in rows}
    if "FED_RATE_DECISION" in kinds:
        return "none"  # neither narrative-only nor a confirmable regime_shift
    if "EARNINGS_RELEASE" in kinds:
        return "narrative_only"
    return "none"


def build_axes(conn, trade_day: date) -> Axes:
    """Assemble every axis from the store. Missing -> None, never a default."""
    stock_prints = _intraday(conn, REFERENCE, trade_day)
    gold_prints = _intraday(conn, GOLD, trade_day)
    stock_ref = _prior_close(conn, REFERENCE, trade_day)
    gold_ref = _prior_close(conn, GOLD, trade_day)

    stock = None
    stock_reversed = None
    if stock_prints and stock_ref is not None:
        stock = _direction(stock_prints[-1], stock_ref)
        if len(stock_prints) >= 2:
            opened_red = stock_prints[0] < stock_ref
            now_green = stock_prints[-1] > stock_ref
            stock_reversed = bool(opened_red and now_green)

    gold = None
    if gold_prints and gold_ref is not None:
        gold = _direction(gold_prints[-1], gold_ref)

    closes = vixfeed.vix_closes(conn, "VIX", vixfeed.SLOPE_WINDOW)
    levels = vixfeed.latest_levels(conn)

    return Axes(
        stock=stock,
        stock_reversed=stock_reversed,
        rates=None,       # D103: no treasury feed this batch
        gold=gold,
        vix_state=vixfeed.vix_state(closes),
        vix_slope=vixfeed.slope_state(closes),
        vix_term=vixfeed.term_state(levels),
        single_name_iv=None,   # no measured IV (§13.9)
        rates_divergence=None,  # needs 10Y (§13.9)
        news=_news_label(conn, trade_day),
        numbers={
            "vix_level": levels.get("VIX"),
            "vix9d": levels.get("VIX9D"),
            "vix3m": levels.get("VIX3M"),
            "vix1y": levels.get("VIX1Y"),
            "vix_early_high": max((lvl for _d, lvl in closes), default=None),
            "stock_prints": len(stock_prints),
            "gold_prints": len(gold_prints),
        },
    )


def observe(conn, trade_day: date) -> Regime:
    """Pure evaluate() over freshly read axes."""
    return evaluate(build_axes(conn, trade_day))


def write_label(conn, account: str, trade_day: date, regime: Regime) -> None:
    evidence = dict(regime.evidence or {})
    evidence["observed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn.execute(
        _WRITE_SQL,
        (
            account, trade_day, RUN, regime.label, regime.close_bias,
            Jsonb(evidence), Jsonb(dict(regime.inputs or {})),
        ),
    )


def latest_label(conn, account: str) -> dict | None:
    """Newest row for the account, or None when Run C never ran."""
    if not account:
        return None
    row = conn.execute(_LATEST_SQL, (account,)).fetchone()
    if row is None:
        return None
    return {
        "trade_day": row[0],
        "run": str(row[1]),
        "label": str(row[2]),
        "close_bias": str(row[3]),
        "evidence": row[4] or {},
        "inputs": row[5] or {},
        "created_at": row[6],
        "is_real": str(row[2]) != "insufficient_data",
    }


# --- D107: prompt injection -------------------------------------------------


def prompt_line(conn, account: str) -> str | None:
    """One line for the agent prompt, only when the label is real.

    D107: injecting ``insufficient_data`` would add noise to every per-name
    prompt while telling the model nothing. Absence of the line is the signal.
    Never raises — a missing table or store must not break a draft.
    """
    try:
        found = latest_label(conn, account)
    except Exception as exc:  # noqa: BLE001 - prompt assembly must not fail
        log.info("regime prompt line skipped: %s", exc.__class__.__name__)
        return None
    if not found or not found["is_real"]:
        return None
    return Regime(
        label=found["label"],
        close_bias=found["close_bias"],
        inputs=dict(found["inputs"] or {}),
        evidence=dict(found["evidence"] or {}),
    ).line()


# --- read-only card ---------------------------------------------------------

_DIMENSION_LABELS = (
    ("vix_slope", "VIX 斜率"),
    ("vix_term", "期限结构"),
    ("single_name_iv", "单股 IV"),
    ("rates_divergence", "股债背离"),
)


def render_card(conn, account: str) -> str:
    """Pure-ish HTML for /portfolio-analysis. Every dynamic cell escaped."""
    from html import escape

    try:
        found = latest_label(conn, account)
    except Exception as exc:  # noqa: BLE001 - a page must render without R7
        log.info("regime card skipped: %s", exc.__class__.__name__)
        return ""
    if not found:
        return ""

    inputs = dict(found["inputs"] or {})
    label = found["label"]
    bias = found["close_bias"]
    tone = "green" if found["is_real"] else "amber"

    rows = []
    for key, title in _DIMENSION_LABELS:
        value = inputs.get(key)
        if value is None:
            rows.append(
                f'<tr><td>{escape(title)}</td>'
                f'<td><span class="tag amber">unknown</span></td>'
                f'<td>{escape(_reason_for(key, inputs))}</td></tr>'
            )
        else:
            rows.append(
                f'<tr><td>{escape(title)}</td>'
                f'<td><span class="mono">{escape(str(value))}</span></td>'
                f"<td></td></tr>"
            )

    hits = inputs.get("vix_hits")
    day = found["trade_day"]
    headline = Regime(
        label=label, close_bias=bias, inputs=inputs, evidence=found["evidence"],
    ).line()
    return (
        '<div class="card guard-runs regime-card"><h2>午間 Regime · Run C</h2>'
        f'<p><span class="tag {tone}">{escape(label)}</span> '
        f'<span class="mono">close_bias: {escape(bias)}</span> '
        f'<span class="mono">VIX hits {escape(str(hits if hits is not None else 0))}/4</span></p>'
        f"<p>{escape(headline)}</p>"
        '<table class="guard-history"><thead><tr>'
        "<th>Dimension</th><th>State</th><th>Why unknown</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
        f'<p class="guard-empty">Observed for {escape(str(day))} (ET) at 12:30 · '
        "read-only observation, never a gate. VIX is manual: Settings → Sync VIX Data.</p>"
        "</div>"
    )


def _reason_for(key: str, inputs: dict) -> str:
    """D73: the fixed in-place reason, chosen by which axis is missing."""
    from athena_strategy.regime import REASONS

    mapping = {
        "vix_slope": "vix_slope",
        "vix_term": "vix_term",
        "single_name_iv": "single_name_iv",
        "rates_divergence": "rates",
    }
    return REASONS.get(mapping.get(key, key), "n/a")


# --- one run + daemon -------------------------------------------------------


def run_once(conn, account: str, slot: RunSlot, now: datetime | None = None) -> Regime:
    """One Run C observation for one account. Writes exactly one row."""
    regime = observe(conn, slot.trade_day)
    write_label(conn, account, slot.trade_day, regime)
    conn.commit()
    return regime


def _scheduler_loop(database_url: str) -> None:
    from athena_exec.accounts import active_accounts
    from athena_store import connect

    while not _STOP.is_set():
        slot = next_run(datetime.now(timezone.utc), runs=(RUN,))
        if slot is None:
            if _STOP.wait(60):
                break
            continue
        delay = max(0.0, (slot.moment - datetime.now(timezone.utc)).total_seconds())
        if _STOP.wait(delay):
            break
        if _STOP.is_set():
            break
        for account in active_accounts():
            try:
                with connect(database_url) as conn:
                    regime = run_once(conn, account, slot)
                log.info(
                    "regime run ok account=%s day=%s hkt=%s label=%s bias=%s",
                    account, slot.trade_day, hk_clock(slot.moment),
                    regime.label, regime.close_bias,
                )
            except Exception:
                # No durable failure row: regime_labels is an observation log,
                # not a grading audit. The absence of today's row is the signal,
                # and the next fire (or a manual recompute) fills it.
                log.exception(
                    "regime run failed account=%s day=%s", account, slot.trade_day,
                )


def start_regime_scheduler(database_url: str) -> None:
    """Start (or restart) the Run C daemon. Safe to call more than once."""
    global _THREAD
    _STOP.clear()
    if _THREAD is not None and _THREAD.is_alive():
        return
    _THREAD = threading.Thread(
        target=_scheduler_loop, args=(database_url,),
        name="guardian-r7-regime", daemon=True,
    )
    _THREAD.start()


def stop_regime_scheduler() -> None:
    _STOP.set()
