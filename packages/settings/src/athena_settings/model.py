"""Settings payload. Secrets stay in the environment, not in this document."""

from typing import Literal

from pydantic import BaseModel, Field

Privilege = Literal["all_orders", "opens_only", "auto_within_risk", "paper_only"]
RiskProfile = Literal["defensive", "standard", "income", "aggressive", "custom"]


class RiskLimits(BaseModel):
    profile: RiskProfile
    max_symbol_pct_nav: int
    cash_reserve_pct: int
    daily_loss_halt_pct: int
    max_contracts_per_symbol: int
    min_iv_rank_new_short: int
    min_dte_new: int
    max_short_opt_margin_pct: int
    earnings_blackout_days: int


class IBSlots(BaseModel):
    """Hong Kong windows. The clock applies them Monday through Friday."""

    athena_slots: str = "HKT 09:30-12:00, 13:00-16:00, 21:30-23:55"
    mobile_slots: str = "HKT 19:00-21:30"
    gateway_host: str = "127.0.0.1"
    gateway_port: int = 4001
    client_id: int = 1


class NewsSettings(BaseModel):
    yahoo: bool = True
    finviz: bool = True
    fed_calendar: bool = True
    msn: bool = False
    manual_forward: bool = True
    poll_seconds: int = 60
    wash_min_negative: int = 3
    wash_window_days: int = 3
    wash_sentiment_below: float = -0.3
    fed_window_days: int = 7
    earnings_near_days: int = 7


class AISettings(BaseModel):
    provider: str = "DSH"
    model: str = "sentiment-v1"
    base_url: str = "http://127.0.0.1:8787"


class ITickSettings(BaseModel):
    base_url: str = "https://api.itick.org"


class NotifySettings(BaseModel):
    """How long an unapproved draft stays open. The control link uses the same window."""

    draft_ttl_minutes: int = 60


class StrategySettings(BaseModel):
    profit_take_pct: int = 50
    roll_dte: int = 21
    ivr_high: int = 80
    ivr_low: int = 20
    ivr_min_samples: int = 20
    rsi_period: int = 14
    rsi_overbought: int = 70
    rsi_oversold: int = 30
    level_lookback: int = 20
    touch_pct: int = 1
    p1_from: int = -7
    p1_to: int = -3
    p2_from: int = -3
    p2_to: int = -1
    p4_from: int = 1
    p4_to: int = 3
    p5_from: int = 5
    p5_to: int = 10


class SettingsPayload(BaseModel):
    privilege: Privilege = "all_orders"
    risk: RiskLimits
    ib: IBSlots = Field(default_factory=IBSlots)
    news: NewsSettings = Field(default_factory=NewsSettings)
    ai: AISettings = Field(default_factory=AISettings)
    itick: ITickSettings = Field(default_factory=ITickSettings)
    notify: NotifySettings = Field(default_factory=NotifySettings)
    strategy: StrategySettings = Field(default_factory=StrategySettings)
    watchlist: list[str] = Field(default_factory=lambda: ["NVDA", "QCOM", "T"])


def standard_payload() -> SettingsPayload:
    """Standard profile from Risk control. Privilege stays all_orders."""
    return SettingsPayload(
        privilege="all_orders",
        risk=RiskLimits(
            profile="standard",
            max_symbol_pct_nav=20,
            cash_reserve_pct=30,
            daily_loss_halt_pct=5,
            max_contracts_per_symbol=3,
            min_iv_rank_new_short=50,
            min_dte_new=7,
            max_short_opt_margin_pct=50,
            earnings_blackout_days=7,
        ),
    )
