"""One registry for every tag Engine A and Engine B can emit."""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("athena.strategy")

RECOMPUTED_OWNERS = frozenset({"news", "iv", "price", "indicators", "regime"})


@dataclass(frozen=True)
class TagSpec:
    name: str
    owner: str
    direction: str
    requires: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    status: str = "active"
    deprecated: bool = False
    color: str = ""


def _spec(
    name: str,
    owner: str,
    direction: str,
    *,
    requires: tuple[str, ...] = (),
    aliases: tuple[str, ...] = (),
    status: str = "active",
    deprecated: bool = False,
    color: str = "",
) -> TagSpec:
    return TagSpec(name, owner, direction, requires, aliases, status, deprecated, color)


OVERBOUGHT = "OVERBOUGHT"
OVERSOLD = "OVERSOLD"
SUPPORT_TOUCH = "SUPPORT_TOUCH"
RESISTANCE_TOUCH = "RESISTANCE_TOUCH"
PRICE_WEAK = "PRICE_WEAK"
VOLUME_SPIKE = "VOLUME_SPIKE"
NEWS_WASH = "NEWS_WASH"
FED_EVENT = "FED_EVENT"
EARNINGS_NEAR = "EARNINGS_NEAR"
CHANNEL_RISING = "CHANNEL_RISING"
UPPER_RAIL = "UPPER_RAIL"
LOWER_RAIL = "LOWER_RAIL"
HIGH_52W = "HIGH_52W"
LOW_52W = "LOW_52W"
HIGH_RANGE = "HIGH_RANGE"
LOW_RANGE = "LOW_RANGE"
PULLBACK = "PULLBACK"
TREND_STRONG = "TREND_STRONG"
TREND_WEAK = "TREND_WEAK"
DIP_BOUGHT = "DIP_BOUGHT"
DRAWDOWN_OK = "DRAWDOWN_OK"
VOLUME_DRY = "VOLUME_DRY"
ATR_WIDE = "ATR_WIDE"
RV_HIGH = "RV_HIGH"
RV_LOW = "RV_LOW"
REGIME_RISK_OFF = "REGIME_RISK_OFF"
RECLAIM = "RECLAIM"
HIGH_IV = "HIGH_IV"
LOW_IV = "LOW_IV"

TAG_REGISTRY: dict[str, TagSpec] = {
    spec.name: spec
    for spec in (
        _spec(OVERBOUGHT, "price", "bear", color="amber"),
        _spec(OVERSOLD, "price", "bull"),
        _spec(SUPPORT_TOUCH, "price", "bull"),
        _spec(RESISTANCE_TOUCH, "price", "bear"),
        _spec(PRICE_WEAK, "price", "bear", color="red"),
        _spec(VOLUME_SPIKE, "price", "neutral", color="amber"),
        _spec(NEWS_WASH, "news", "bear", color="amber"),
        _spec(FED_EVENT, "news", "neutral", color="blue"),
        _spec(EARNINGS_NEAR, "news", "neutral", color="amber"),
        _spec(CHANNEL_RISING, "location", "bull"),
        _spec(UPPER_RAIL, "location", "bear"),
        _spec(LOWER_RAIL, "location", "bull"),
        _spec(HIGH_52W, "location", "bear"),
        _spec(LOW_52W, "location", "bull"),
        _spec(HIGH_RANGE, "location", "bear"),
        _spec(LOW_RANGE, "location", "bull"),
        _spec(PULLBACK, "indicators", "bull", requires=(CHANNEL_RISING,)),
        _spec(TREND_STRONG, "indicators", "bull"),
        _spec(TREND_WEAK, "indicators", "bear"),
        _spec(DIP_BOUGHT, "indicators", "bull", requires=(TREND_STRONG,)),
        _spec(DRAWDOWN_OK, "indicators", "bull"),
        _spec(VOLUME_DRY, "indicators", "bull"),
        _spec(ATR_WIDE, "indicators", "neutral"),
        _spec(RV_HIGH, "indicators", "neutral"),
        _spec(RV_LOW, "indicators", "neutral"),
        _spec(REGIME_RISK_OFF, "regime", "bear"),
        _spec(RECLAIM, "indicators", "bull"),
        _spec(
            HIGH_IV,
            "iv",
            "neutral",
            aliases=(HIGH_IV,),
            status="legacy_price_proxy",
            deprecated=True,
            color="teal",
        ),
        _spec(
            LOW_IV,
            "iv",
            "neutral",
            aliases=(LOW_IV,),
            status="legacy_price_proxy",
            deprecated=True,
            color="green",
        ),
    )
}


def names_owned_by(*owners: str) -> frozenset[str]:
    wanted = set(owners)
    return frozenset(spec.name for spec in TAG_REGISTRY.values() if spec.owner in wanted)


def tag_color(name: str) -> str:
    spec = TAG_REGISTRY.get(name)
    return "" if spec is None else spec.color


def kept_stored(stored: set[str], *, drop_owners: frozenset[str] = RECOMPUTED_OWNERS) -> set[str]:
    """Tags this pass does not recompute. Unknown and deprecated names are dropped."""
    kept: set[str] = set()
    for tag in stored:
        spec = TAG_REGISTRY.get(tag)
        if spec is None:
            log.warning("unknown tag %s", tag)
            continue
        if spec.deprecated or spec.owner in drop_owners:
            continue
        kept.add(tag)
    return kept


def unknown_names(tags: list[str]) -> list[str]:
    found = []
    for tag in tags:
        if tag and tag not in TAG_REGISTRY:
            log.warning("unknown tag %s", tag)
            found.append(tag)
    return found
