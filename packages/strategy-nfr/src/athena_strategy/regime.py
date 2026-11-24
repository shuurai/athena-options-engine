"""R7 midday regime matrix: pure, zero IO (booklet 14, §13.4/§13.9; D67–D73, D102–D108).

The output is a **read-only observation** of a joint state, never a prediction
and never a gate (D67). Three ratified deviations shape this module:

* **D105** — the ``量能`` (volume) axis is gone. ``upsert_delayed()`` stores
  ``volume=0`` and today's ``ib-daily`` bar is written only after the close, so
  intraday volume is structurally unmeasurable. Keeping it would make every row
  unreachable; it is recorded as a permanent gap instead of a fabricated number.
* **D106** — ``insufficient_data`` is decided **per candidate row**, not
  globally. §13.4 marks 10Y/gold/VIX as "—" for ``distribution_into_strength``,
  so that row may fire on stock reversal + short-report news alone.
* **D68 / D73** — a missing input is ``unknown`` with a fixed, in-place reason.
  Never a guess, never a silent blank.

``regime_shift`` stays in the vocabulary but is unreachable in this batch: it
needs Fed-speech text + SEC/regulatory scraping + a bond-market move, and
``score_title()`` only produces five coarse codes (§24.2 #13). That is an honest
gap, not a bug to paper over.
"""

from __future__ import annotations

from dataclasses import dataclass, field

LABELS = (
    "liquidity_squeeze",
    "short_cover_bounce",
    "distribution_into_strength",
    "normal_risk_off",
    "regime_shift",
    "dead_cat",
    "insufficient_data",
)

CLOSE_BIAS = (
    "risk_off_into_close",
    "bounce_likely_fades",
    "neutral",
    "insufficient_data",
)

# D73: one fixed reason per axis, reused verbatim in the push and the panel card.
REASONS = {
    "stock": "stock n/a (no intraday print for the regime index)",
    "rates": "10Y n/a (no treasury-yield feed in store)",
    "gold": "gold n/a (no intraday print for GLD)",
    "vix_state": "VIX n/a (no VIX daily-close feed in store)",
    "vix_slope": "VIX n/a (no VIX daily-close feed in store)",
    "vix_term": "VIX9D/VIX3M/VIX1Y n/a (no VIX-term feed in store)",
    "single_name_iv": "single_name_iv n/a (no measured IV)",
    "news": "news n/a (no scored event today)",
    "volume": "volume n/a (no intraday volume; axis removed by D105)",
}

# Required axes per row. "—" columns in §13.4 simply do not appear here (D106).
ROWS = (
    ("distribution_into_strength", "bounce_likely_fades",
     {"stock_reversed": (True,), "news": ("narrative_only",)}),
    ("liquidity_squeeze", "risk_off_into_close",
     {"stock": ("down",), "rates": ("up",), "gold": ("down",),
      "vix_state": ("high_rising",)}),
    ("short_cover_bounce", "bounce_likely_fades",
     {"stock": ("up",), "rates": ("down",), "gold": ("down",),
      "vix_state": ("early_high_fell_above_30",)}),
    ("normal_risk_off", "neutral",
     {"stock": ("down",), "rates": ("down",), "gold": ("up",),
      "vix_state": ("falling",)}),
    ("dead_cat", "bounce_likely_fades",
     {"stock": ("up",), "news": ("narrative_only",), "vix_state": ("falling",)}),
)

VIX_DIMENSIONS = ("vix_slope", "vix_term", "single_name_iv", "rates_divergence")


@dataclass(frozen=True)
class Axes:
    """Observed axes. ``None`` means unmeasurable, never "flat".

    A measured value that no row describes is still **measured**: ``flat`` for a
    sub-deadband direction and ``quiet`` for a low, non-falling VIX. Those block
    the dependent rows exactly like a mismatch, but they are not ``unknown`` and
    therefore carry no gap reason (D73).
    """

    stock: str | None = None            # up / down / flat
    stock_reversed: bool | None = None  # early red -> midday green
    rates: str | None = None            # up / down, always the YIELD direction
    gold: str | None = None             # up / down / flat
    vix_state: str | None = None        # high_rising / early_high_fell_above_30 /
                                        # falling / rising_not_high / quiet
    vix_slope: str | None = None        # rising_from_low / flat / falling
    vix_term: str | None = None         # front_end_hot / contango / flat / inversion
    single_name_iv: str | None = None   # elevated / normal
    rates_divergence: str | None = None  # rates_tightening / aligned
    news: str | None = None             # regime_shift / narrative_only / none
    numbers: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Regime:
    label: str
    close_bias: str
    inputs: dict
    evidence: dict

    @property
    def is_real(self) -> bool:
        """D107: only a non-degenerate label is worth injecting into prompts."""
        return self.label != "insufficient_data"

    def line(self) -> str:
        """One compact line for the panel card and the agent prompt (D69/D107)."""
        def number(key, suffix=""):
            value = self.inputs.get(key)
            try:
                return f"{float(value):.2f}{suffix}"
            except (TypeError, ValueError):
                return None

        pieces = []
        vix = number("vix_level")
        if vix:
            pieces.append(f"VIX {vix}")
        rates = number("rates_10y", "%")
        if rates:
            pieces.append(f"10Y {rates}")
        gold = self.inputs.get("gold_dir")
        if gold in {"up", "down"}:
            pieces.append("金升" if gold == "up" else "金跌")
        detail = "、".join(pieces) if pieces else "no live inputs"
        return f"午間 Regime：{detail} →【{self.label}】。close_bias: {self.close_bias}。"


def _hit_count(axes: Axes) -> int:
    """D77: 0–4 display-only count of the four registered VIX dimensions."""
    values = (axes.vix_slope, axes.vix_term, axes.single_name_iv, axes.rates_divergence)
    return sum(1 for value in values if value is not None)


def _reasons(axes: Axes) -> list[str]:
    """D73: one fixed reason per unmeasurable axis, in a stable order.

    Deduplicated: ``vix_state`` and ``vix_slope`` share the same missing feed, so
    repeating the sentence twice would read as two separate gaps.
    """
    out = []
    for key in ("stock", "rates", "gold", "vix_state", "vix_slope", "vix_term",
                "single_name_iv", "news"):
        if getattr(axes, key) is None and REASONS[key] not in out:
            out.append(REASONS[key])
    return out


def evaluate(axes: Axes) -> Regime:
    """Pure. Returns the first matching row, else ``insufficient_data``.

    A row fires only when **every axis it requires** is known and matches; an
    unknown required axis blocks that row (it can neither confirm nor deny).
    This is the D106 per-row reading of §13.4.
    """
    if not isinstance(axes, Axes):
        raise TypeError("evaluate needs an Axes instance")

    observed = {
        "stock": axes.stock,
        "stock_reversed": axes.stock_reversed,
        "rates": axes.rates,
        "gold": axes.gold,
        "vix_state": axes.vix_state,
        "news": axes.news,
    }

    tried = []
    for label, bias, required in ROWS:
        unknown = [key for key in required if observed.get(key) is None]
        matched = not unknown and all(
            observed[key] in allowed for key, allowed in required.items()
        )
        tried.append({
            "label": label,
            "matched": matched,
            "blocked_by": unknown,
        })
        if matched:
            return Regime(
                label=label,
                close_bias=bias,
                inputs=_inputs(axes),
                evidence={"matched_rule": label, "rows": tried},
            )

    return Regime(
        label="insufficient_data",
        close_bias="insufficient_data",
        inputs=_inputs(axes),
        evidence={
            "matched_rule": "",
            "rows": tried,
            "reasons": _reasons(axes),
        },
    )


def _inputs(axes: Axes) -> dict:
    numbers = dict(axes.numbers or {})
    return {
        "stock": axes.stock,
        "stock_reversed": axes.stock_reversed,
        "rates_10y": numbers.get("rates_10y"),
        "gold_dir": axes.gold,
        "vix_level": numbers.get("vix_level"),
        "vix_early_high": numbers.get("vix_early_high"),
        "vix_state": axes.vix_state,
        "vix_slope": axes.vix_slope,
        "vix_term": axes.vix_term,
        "single_name_iv": axes.single_name_iv,
        "rates_divergence": axes.rates_divergence,
        "news": axes.news,
        "volume": None,  # D105: structurally unmeasurable at 12:30
        "vix_hits": _hit_count(axes),
        "thresholds_unratified": True,
    }
