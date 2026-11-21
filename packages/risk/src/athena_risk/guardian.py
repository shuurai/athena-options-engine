"""Guardian conformance grading (booklet 14, iteration R1).

Pure and read-only. No database, no IB, no clock, no environment, no UI, no
alerts. The caller assembles a snapshot and gets back a report.

Every tier is derived from numbers the caller supplies. A missing number is
never invented: the check is skipped and a visible flag records the skip.

Ratified decisions: D58 (thresholds), D62 (per-alert close paths, consumed by
R2), D63 (unmanaged positions), D65 (heartbeat, consumed by R6), D66 (no
invented stop line; mirror the advisor's own pending decision instead).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Iterable, Sequence

__all__ = [
    "Tier",
    "GuardianRules",
    "GuardianPosition",
    "AccountMarks",
    "PositionGrade",
    "GuardianReport",
    "grade",
    "render_report",
    "BLOWUP_FLAGS",
    "BREACH_FLAGS",
    "PROXIMITY_FLAGS",
    "INFO_FLAGS",
]


# ---------------------------------------------------------------------------
# Stable flag codes. R2 keys guardian_alerts.breach off these; do not reword.
# ---------------------------------------------------------------------------

BLOWUP_FLAGS = frozenset({
    "margin_call",              # IB margin-call flag
    "maint_margin_critical",    # MaintMarginReq / NetLiq > 90%
    "el_critical",              # ExcessLiquidity / NetLiq < 5%
})

BREACH_FLAGS = frozenset({
    "el_thin",                  # ExcessLiquidity / NetLiq < 10%
    "advisor_close_pending",    # D66: caller-supplied pending CLOSE / TAKE_PROFIT
    "uncovered_call",           # short call not covered by held shares
    "symbol_over_cap",          # |market_value| / nav > max_symbol_pct_nav
    "bucket_over_cap",          # bucket market_value / nav > max_bucket_pct_nav
    "projected_el_negative",    # excess - short-put assignment notional < 0
})

PROXIMITY_FLAGS = frozenset({
    "el_watch",                 # EL/NL 10-20%
    "unknown_margin",           # NAV or ExcessLiquidity missing
    "advisor_trim_pending",     # D66: caller-supplied pending TRIM
    "prox_bucket",              # bucket % within 5 pts below cap
    "prox_dte",                 # abs(dte - min_dte_new) <= 5
    "assignment_risk",          # |delta| past assign_delta on a short put
})

INFO_FLAGS = frozenset({
    "unmanaged",                # D63: no universe row / no tactic flags
    "missing_market_value",     # concentration checks skipped for this position
})

_ZERO = Decimal(0)
_HUNDRED = Decimal(100)


class Tier(str, Enum):
    """Best to worst. Profit sits ABOVE healthy, not beside it (§2)."""

    PROFIT = "profit"
    HEALTHY = "healthy"
    BALANCED = "balanced"
    DANGER = "danger"
    BLOWUP = "blowup"


_RANK: dict[Tier, int] = {
    Tier.PROFIT: 0,
    Tier.HEALTHY: 1,
    Tier.BALANCED: 2,
    Tier.DANGER: 3,
    Tier.BLOWUP: 4,
}

_TIER_LABEL: dict[Tier, str] = {
    Tier.PROFIT: "盈利",
    Tier.HEALTHY: "健康",
    Tier.BALANCED: "平衡",
    Tier.DANGER: "危险",
    Tier.BLOWUP: "暴雷",
}


@dataclass(frozen=True)
class GuardianRules:
    """D58 constants plus the D32-overlapping limits read from live RiskLimits.

    The five D58 thresholds are fixed here and must not be overridden by a
    settings knob in R1. The four limits that D32 already owns are bound by
    ``from_limits`` so guardian and wheel never grade against two different
    numbers.
    """

    # D58 ratified constants
    el_healthy_pct: Decimal = Decimal("20")
    el_balanced_pct: Decimal = Decimal("10")
    el_blowup_pct: Decimal = Decimal("5")
    maint_blowup_pct: Decimal = Decimal("90")
    bucket_proximity_pts: Decimal = Decimal("5")
    dte_proximity_days: int = 5

    # D32-overlapping values; defaults mirror RiskLimits defaults
    max_bucket_pct_nav: Decimal = Decimal("35")
    max_symbol_pct_nav: Decimal = Decimal("20")
    min_dte_new: int = 7
    assign_delta: Decimal = Decimal("0.30")

    @classmethod
    def from_limits(cls, limits) -> "GuardianRules":
        """Bind the overlapping knobs from a live RiskLimits instance."""
        return cls(
            max_bucket_pct_nav=_dec(limits.max_bucket_pct_nav, Decimal("35")),
            max_symbol_pct_nav=_dec(limits.max_symbol_pct_nav, Decimal("20")),
            min_dte_new=int(limits.min_dte_new),
            assign_delta=_dec(limits.assign_delta, Decimal("0.30")),
        )


@dataclass(frozen=True)
class AccountMarks:
    """IB account summary. None means 'not supplied', never 'zero'."""

    net_liquidation: Decimal | None = None
    excess_liquidity: Decimal | None = None
    maint_margin_req: Decimal | None = None
    margin_call: bool = False
    base_currency: str = ""


@dataclass(frozen=True)
class GuardianPosition:
    """One synced position. Monetary values are in the book currency already.

    The caller converts FX before calling; R1 does no I/O and therefore no FX.
    ``pending_action`` is D66: the caller may supply the advisor's own pending
    decision (``close`` / ``take_profit`` / ``trim``), read from
    ``advisor_signals`` or a pending ``drafts.tactic`` in R2. R1 never invents
    a price line, never does price-crossing arithmetic, and never reads the
    advisor itself — it stays a zero-I/O pure function. Operator price
    tripwires belong to R5 and feed ``guardian_alerts`` directly.
    """

    symbol: str
    quantity: Decimal
    sec_type: str = "STK"                 # STK | OPT
    right: str = ""                       # P | C for OPT
    strike: Decimal | None = None
    multiplier: Decimal | None = None    # None -> OPT 100 / STK 1 (budget.py convention)
    market_value: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    day_pnl: Decimal | None = None
    dte: int | None = None
    delta: Decimal | None = None
    classifiers: tuple[str, ...] = ()
    managed: bool = True
    pending_action: str | None = None     # D66: caller-supplied, not invented here


@dataclass(frozen=True)
class PositionGrade:
    symbol: str
    tier: Tier
    flags: tuple[str, ...]
    unmanaged: bool


@dataclass(frozen=True)
class GuardianReport:
    portfolio_tier: Tier
    account_tier: Tier
    positions: tuple[PositionGrade, ...]
    account_flags: tuple[str, ...]
    el_nl_pct: Decimal | None
    maint_nl_pct: Decimal | None
    breach_count: int
    proximity_count: int
    unmanaged_count: int


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------

def grade(
    positions: Sequence[GuardianPosition],
    marks: AccountMarks,
    rules: GuardianRules | None = None,
) -> GuardianReport:
    """Grade a snapshot. Pure: same input always gives the same report."""
    rules = rules or GuardianRules()
    rows = list(positions or [])
    nav = _pos(marks.net_liquidation)
    excess = marks.excess_liquidity if marks.excess_liquidity is not None else None

    # Account-level margin distance. A missing number is a visible skip, not green.
    el_nl_pct, maint_nl_pct, account_flags = _account_flags(marks, nav, rules)
    account_tier = _tier_from_flags(account_flags)

    # Pre-aggregate what needs a whole-book view before grading each position.
    shares_by_symbol = _shares_by_symbol(rows)
    bucket_pct = _bucket_pct(rows, nav, rules)
    assignment_notional = _assignment_notional(rows, rules)

    grades: list[PositionGrade] = []
    total_pnl = _ZERO
    for row in rows:
        flags: set[str] = set()
        mult = _multiplier(row)
        qty = _dec(row.quantity, _ZERO)
        # D66: no invented stop line. The only "act now" input is the advisor's
        # own pending decision, supplied by the caller. Mapping mirrors
        # manage.py:239 (take_profit/close -> CLOSE, trim -> TRIM) so guardian
        # and wheel never disagree about what a signal means.
        action = (row.pending_action or "").strip().lower()

        # -- hard breaches -------------------------------------------------
        if action in {"close", "take_profit"}:
            flags.add("advisor_close_pending")

        if _is_short_call(row) and shares_by_symbol.get(row.symbol.upper(), _ZERO) < abs(qty) * mult:
            flags.add("uncovered_call")

        has_value = row.market_value is not None
        if has_value and nav is not None and nav > 0:
            if abs(_dec(row.market_value, _ZERO)) / nav * _HUNDRED > rules.max_symbol_pct_nav:
                flags.add("symbol_over_cap")
        else:
            flags.add("missing_market_value")
        for name in row.classifiers or ("UNCLASSIFIED",):
            if name in bucket_pct["over"]:
                flags.add("bucket_over_cap")
            elif name in bucket_pct["near"]:
                flags.add("prox_bucket")

        if excess is not None and assignment_notional > 0 and excess - assignment_notional < 0:
            # Attribute the projected shortfall to the short puts that cause it.
            if _is_short_put(row) and abs(_dec_or_zero(row.delta)) >= rules.assign_delta:
                flags.add("projected_el_negative")

        # -- proximity ------------------------------------------------------
        if action == "trim":
            flags.add("advisor_trim_pending")
        if _is_short_option(row) and row.dte is not None:
            if abs(int(row.dte) - rules.min_dte_new) <= rules.dte_proximity_days:
                flags.add("prox_dte")
        if _is_short_put(row) and abs(_dec_or_zero(row.delta)) >= rules.assign_delta:
            flags.add("assignment_risk")

        # -- tier ------------------------------------------------------------
        tier = _tier_from_flags(flags)
        if not row.managed:
            # D63: unmanaged can never read healthy/profit. A real breach still
            # elevates it -- the cap only applies below BALANCED.
            flags.add("unmanaged")
            if _RANK[tier] < _RANK[Tier.BALANCED]:
                tier = Tier.BALANCED
        elif tier == Tier.HEALTHY:
            pnl = _day_or_unrealized(row)
            if pnl is not None and pnl > 0:
                tier = Tier.PROFIT

        pnl = _day_or_unrealized(row)
        if pnl is not None:
            total_pnl += pnl
        grades.append(PositionGrade(row.symbol, tier, tuple(sorted(flags)), not row.managed))

    portfolio = account_tier
    for item in grades:
        if _RANK[item.tier] > _RANK[portfolio]:
            portfolio = item.tier
    # Blue requires green first (§2). No breach, no proximity, and the book is
    # in the money -> profit. A thin-margin account stays BALANCED even when
    # individual positions are profitable.
    if portfolio == Tier.HEALTHY and total_pnl > 0:
        portfolio = Tier.PROFIT

    breach_count = sum(1 for g in grades if g.tier in (Tier.DANGER, Tier.BLOWUP))
    proximity_count = sum(1 for g in grades if g.tier == Tier.BALANCED)
    unmanaged_count = sum(1 for g in grades if g.unmanaged)

    return GuardianReport(
        portfolio_tier=portfolio,
        account_tier=account_tier,
        positions=tuple(grades),
        account_flags=tuple(sorted(account_flags)),
        el_nl_pct=el_nl_pct,
        maint_nl_pct=maint_nl_pct,
        breach_count=breach_count,
        proximity_count=proximity_count,
        unmanaged_count=unmanaged_count,
    )


def _account_flags(
    marks: AccountMarks, nav: Decimal | None, rules: GuardianRules,
) -> tuple[Decimal | None, Decimal | None, set[str]]:
    """EL/NL and MaintMargin/NL. Missing data -> unknown_margin, never green."""
    flags: set[str] = set()
    el_nl_pct: Decimal | None = None
    maint_nl_pct: Decimal | None = None

    if marks.margin_call:
        flags.add("margin_call")

    if nav is None or nav <= 0:
        flags.add("unknown_margin")
        return el_nl_pct, maint_nl_pct, flags

    if marks.maint_margin_req is not None:
        mmr = _dec(marks.maint_margin_req, _ZERO)
        maint_nl_pct = mmr / nav * _HUNDRED
        if maint_nl_pct > rules.maint_blowup_pct:
            flags.add("maint_margin_critical")

    if marks.excess_liquidity is None:
        flags.add("unknown_margin")
        return el_nl_pct, maint_nl_pct, flags

    el_nl_pct = _dec(marks.excess_liquidity, _ZERO) / nav * _HUNDRED
    if el_nl_pct < rules.el_blowup_pct:
        flags.add("el_critical")
    elif el_nl_pct < rules.el_balanced_pct:
        flags.add("el_thin")
    elif el_nl_pct < rules.el_healthy_pct:
        flags.add("el_watch")
    return el_nl_pct, maint_nl_pct, flags


def _tier_from_flags(flags: Iterable[str]) -> Tier:
    found = set(flags)
    if found & BLOWUP_FLAGS:
        return Tier.BLOWUP
    if found & BREACH_FLAGS:
        return Tier.DANGER
    if found & PROXIMITY_FLAGS:
        return Tier.BALANCED
    return Tier.HEALTHY


# ---------------------------------------------------------------------------
# Whole-book helpers
# ---------------------------------------------------------------------------

def _shares_by_symbol(rows: Sequence[GuardianPosition]) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for row in rows:
        if (row.sec_type or "STK").upper() == "STK":
            key = (row.symbol or "").upper()
            out[key] = out.get(key, _ZERO) + _dec(row.quantity, _ZERO)
    return out


def _bucket_pct(
    rows: Sequence[GuardianPosition], nav: Decimal | None, rules: GuardianRules,
) -> dict[str, set[str]]:
    """Buckets over the cap, and buckets within the proximity window below it."""
    empty = {"over": set(), "near": set()}
    if nav is None or nav <= 0:
        return empty
    totals: dict[str, Decimal] = {}
    for row in rows:
        if row.market_value is None:
            continue
        for name in row.classifiers or ("UNCLASSIFIED",):
            totals[name] = totals.get(name, _ZERO) + _dec(row.market_value, _ZERO)
    over: set[str] = set()
    near: set[str] = set()
    for name, value in totals.items():
        pct = abs(value) / nav * _HUNDRED
        if pct > rules.max_bucket_pct_nav:
            over.add(name)
        elif rules.max_bucket_pct_nav - pct <= rules.bucket_proximity_pts:
            near.add(name)
    return {"over": over, "near": near}


def _assignment_notional(rows: Sequence[GuardianPosition], rules: GuardianRules) -> Decimal:
    """Short puts whose |delta| is past assign_delta (budget.py arithmetic)."""
    total = _ZERO
    for row in rows:
        if not _is_short_put(row) or row.strike is None:
            continue
        if abs(_dec_or_zero(row.delta)) < rules.assign_delta:
            continue
        total += abs(_dec(row.quantity, _ZERO)) * _dec(row.strike, _ZERO) * _multiplier(row)
    return total


# ---------------------------------------------------------------------------
# Small predicates / coercion
# ---------------------------------------------------------------------------

def _multiplier(row: GuardianPosition) -> Decimal:
    if row.multiplier is not None:
        return _dec(row.multiplier, Decimal(1))
    return Decimal(100) if (row.sec_type or "STK").upper() == "OPT" else Decimal(1)


def _is_short_call(row: GuardianPosition) -> bool:
    return (
        (row.sec_type or "STK").upper() == "OPT"
        and (row.right or "").upper() == "C"
        and _dec(row.quantity, _ZERO) < 0
    )


def _is_short_put(row: GuardianPosition) -> bool:
    return (
        (row.sec_type or "STK").upper() == "OPT"
        and (row.right or "").upper() == "P"
        and _dec(row.quantity, _ZERO) < 0
    )


def _is_short_option(row: GuardianPosition) -> bool:
    return (row.sec_type or "STK").upper() == "OPT" and _dec(row.quantity, _ZERO) < 0


def _day_or_unrealized(row: GuardianPosition) -> Decimal | None:
    """Day P&L when known, otherwise unrealized. Neither -> None, not zero."""
    for value in (row.day_pnl, row.unrealized_pnl):
        if value is not None:
            return _dec(value, _ZERO)
    return None


def _dec_or_zero(value) -> Decimal:
    return _dec(value, _ZERO)


def _dec(value, default: Decimal) -> Decimal:
    parsed = _dec_opt(value)
    return default if parsed is None else parsed


def _dec_opt(value) -> Decimal | None:
    """Decimal or None. Invalid input is a skip, never a fabricated zero."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _pos(value: Decimal | None) -> Decimal | None:
    """NAV is only usable when strictly positive."""
    if value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number > 0 else None


# ---------------------------------------------------------------------------
# Log rendering (R1's only output surface; no UI and no Telegram here)
# ---------------------------------------------------------------------------

def render_report(report: GuardianReport) -> str:
    """Deterministic text for a log line or a terminal. R2 owns the panel."""
    el = "n/a" if report.el_nl_pct is None else f"{report.el_nl_pct:.1f}%"
    lines = [
        "PORTFOLIO {}  EL/NL {}  {} breach, {} proximity, {} unmanaged".format(
            _TIER_LABEL[report.portfolio_tier],
            el,
            report.breach_count,
            report.proximity_count,
            report.unmanaged_count,
        ),
        "ACCOUNT   {}  {}".format(
            _TIER_LABEL[report.account_tier],
            ",".join(report.account_flags) or "ok",
        ),
    ]
    for tier in (Tier.BLOWUP, Tier.DANGER, Tier.BALANCED, Tier.HEALTHY, Tier.PROFIT):
        members = [g for g in report.positions if g.tier == tier]
        label = _TIER_LABEL[tier]
        if not members:
            lines.append(f"  {label}  - none")
            continue
        for item in members:
            lines.append(f"  {label}  {item.symbol}  {','.join(item.flags) or 'ok'}")
    return "\n".join(lines)
