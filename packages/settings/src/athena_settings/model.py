"""Settings payload. Secrets stay in the environment, not in this document."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Privilege = Literal["all_orders", "opens_only", "auto_within_risk", "paper_only"]
GlobalCurrency = Literal["HKD", "USD", "AUD", "CNY", "JPY", "CAD"]
RiskProfile = Literal["defensive", "standard", "income", "aggressive", "test", "paper", "custom"]


class RiskLimits(BaseModel):
    profile: RiskProfile
    max_symbol_pct_nav: int
    cash_reserve_pct: int
    daily_loss_halt_pct: int
    max_contracts_per_symbol: int
    max_position_per_symbol: int = 0
    min_rv_pctl_new_short: int
    min_dte_new: int
    max_short_opt_margin_pct: int
    earnings_blackout_days: int
    max_orders_per_day: int = 10
    margin_rate: int = 20
    strict_conflict: bool = False
    max_bucket_pct_nav: int = 35
    max_assign_pct_nav: int = 40
    min_cash_after_assign_pct: int = 10
    assign_delta: float = 0.30


class IBSlots(BaseModel):
    """Hong Kong clock. Athena holds the HK cash sessions and the US cash session.

    US regular hours are 21:30–04:00 while the US is on daylight time. The
    morning half belongs to the previous evening, including Saturday.
    """

    athena_slots: str = "HKT 09:30-12:00, 13:00-16:00, 21:30-04:00"
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


ProviderName = Literal["DSH", "DEEPSEEK", "OPENROUTER", "CLAUDE", "KIMI"]

# OpenRouter ids for the ten most-used text models in the week ending 9 Oct 2026.
OPENROUTER_MODELS = (
    "deepseek/deepseek-v4.1-flash",
    "stealth/space-bunny-alpha",
    "z-ai/glm-5.3-flash",
    "xiaomi/mimo-v2.6-flash",
    "tencent/hy4-preview",
    "openai/gpt-6-luna",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "deepseek/deepseek-v4-flash-0731",
    "anthropic/claude-opus-5.5",
    "z-ai/glm-5.3",
)
DEEPSEEK_MODELS = ("deepseek-flash", "deepseek-v4-pro")
CLAUDE_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")
KIMI_MODELS = ("kimi-k2.6", "kimi-k3", "kimi-k2.7-code")
MODELS: dict[str, tuple[str, ...]] = {
    "DSH": DEEPSEEK_MODELS,
    "DEEPSEEK": DEEPSEEK_MODELS,
    "OPENROUTER": OPENROUTER_MODELS,
    "CLAUDE": CLAUDE_MODELS,
    "KIMI": KIMI_MODELS,
}
CREDENTIAL_ENV = {
    "DEEPSEEK": "DEEPSEEK_TOKEN",
    "OPENROUTER": "OPENROUTER_API_KEY",
    "CLAUDE": "ANTHROPIC_API_KEY",
    "KIMI": "MOONSHOT_API_KEY",
}
CREDENTIAL_FIELD = {
    "DEEPSEEK": "deepseek_token",
    "OPENROUTER": "openrouter_token",
    "CLAUDE": "claude_token",
    "KIMI": "kimi_token",
}


def normalize_provider(value: object) -> str:
    """DIRECT is the old DeepSeek name. KIMI API is the label shown in Settings."""
    text = str(value or "DSH").strip().upper()
    if text == "DIRECT":
        return "DEEPSEEK"
    if text in {"KIMI API", "KIMI"}:
        return "KIMI"
    return text


class AISettings(BaseModel):
    provider: ProviderName = "DSH"
    model: str = "deepseek-flash"
    base_url: str = "http://127.0.0.1:8787"
    deepseek_token: str | None = None
    openrouter_token: str | None = None
    claude_token: str | None = None
    kimi_token: str | None = None

    @field_validator("provider", mode="before")
    @classmethod
    def _provider(cls, value: object) -> str:
        return normalize_provider(value)


class ITickSettings(BaseModel):
    base_url: str = "https://api.itick.org"
    api_key: str | None = None


class AccountEntry(BaseModel):
    """A configured id. It is not the Gateway session until the environment lists it."""

    account_id: str
    label: str = ""


class AccountSwitches(BaseModel):
    """None inherits the default: on for the session account, off for any other id."""

    sync: bool | None = None
    trading: bool | None = None


class TacticFlags(BaseModel):
    """Per-symbol tactic switches. Missing symbols stay on.

    Intent and Long are per symbol. Income does not buy shares.
    Both prefers income and may also buy shares. Accumulate may buy shares.
    """

    csp: bool = True
    cc: bool = True
    strangle: bool = True
    intent: Literal["income", "both", "accumulate"] = "income"
    long: bool = False
    may_buy: bool | None = None

    def options_on(self) -> bool:
        """A chain is useful only when one option tactic is still on."""
        return self.csp or self.cc or self.strangle


class AgentPrompt(BaseModel):
    """Standing instruction plus the per-run template.

    reasoning None means the standing default: on for the consolidator, off for the rest.
    """

    system: str
    execution: str
    reasoning: bool | None = None


class DeveloperSettings(BaseModel):
    """None means no key. The console can reveal the stored value."""

    api_key: str | None = None


class NotifySettings(BaseModel):
    """How long an unapproved draft stays open. The control link uses the same window.

    A None secret inherits the environment. An empty string overrides it.
    """

    draft_ttl_minutes: int = 60
    bot_token: str | None = None
    bot_username: str | None = None
    telegram_user_ids: str | None = None


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
    volume_spike_multiple: int = 2
    p1_from: int = -7
    p1_to: int = -3
    p2_from: int = -3
    p2_to: int = -1
    p4_from: int = 1
    p4_to: int = 3
    p5_from: int = 5
    p5_to: int = 10
    chain_band_pct: int = 25
    screener_top_n: int = 3
    csp_delta_lo: float = 0.10
    csp_delta_target: float = 0.25
    csp_delta_hi: float = 0.40
    cc_delta_target: float = 0.30
    strangle_delta_target: float = 0.28
    otm_floor_pct: int = 3
    otm_ceiling_pct: int = 20
    iv_rv_multiple_hi: float = 1.05
    iv_rv_multiple_mid: float = 1.35
    iv_rv_multiple_lo: float = 1.80
    dte_min: int = 21
    dte_target: int = 35
    dte_max: int = 60
    trend_slope_min_pct: float = 2
    dip_window_sessions: int = 5
    dip_deep_pos: float = 0.35
    dip_reclaim_pos: float = 0.45
    cc_rail_pos: float = 0.80
    drawdown_band_high_pct: int = 15
    drawdown_band_low_pct: int = 5
    volume_dry_multiple: float = 0.7
    atr_window: int = 14
    atr_wide_ratio_pct: int = 4
    rv_window: int = 20
    rv_high_pctl: int = 80
    rv_low_pctl: int = 20
    regime_index: str = "SPY"
    regime_lookback: int = 63
    reclaim_window: int = 5
    rail_lookback: int = 63
    trend_lag_sessions: int = 21
    volume_avg_sessions: int = 20
    range_min_sessions: int = 252
    trading_days_per_year: int = 252
    observe_slots: Literal["always", "athena", "athena+mobile"] = "always"
    draft_slots: Literal["athena", "always"] = "athena"


SIM_SYMBOLS = ["NVDA", "MSFT", "NOK", "AAPL", "IGV", "NIO", "AMZN", "AVGO"]


class SettingsPayload(BaseModel):
    privilege: Privilege = "all_orders"
    global_currency: GlobalCurrency = "HKD"
    risk: RiskLimits
    ib: IBSlots = Field(default_factory=IBSlots)
    news: NewsSettings = Field(default_factory=NewsSettings)
    ai: AISettings = Field(default_factory=AISettings)
    itick: ITickSettings = Field(default_factory=ITickSettings)
    notify: NotifySettings = Field(default_factory=NotifySettings)
    developer: DeveloperSettings = Field(default_factory=DeveloperSettings)
    strategy: StrategySettings = Field(default_factory=StrategySettings)
    watchlist: list[str] = Field(default_factory=lambda: ["NVDA", "QCOM", "T"])
    blocked_symbols: list[str] = Field(default_factory=list)
    tactic_flags: dict[str, TacticFlags] = Field(default_factory=dict)
    agents: dict[str, AgentPrompt] = Field(default_factory=dict)
    extra_accounts: list[AccountEntry] = Field(default_factory=list)
    account_controls: dict[str, AccountSwitches] = Field(default_factory=dict)
    retain_local_days: int = 14
    cycle_minutes: int = 5
    sim_symbols: list[str] = Field(default_factory=lambda: list(SIM_SYMBOLS))


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
            min_rv_pctl_new_short=50,
            min_dte_new=7,
            max_short_opt_margin_pct=50,
            earnings_blackout_days=7,
        ),
    )


def paper_payload() -> SettingsPayload:
    """Acceptance profile. It does not replace the live book until someone selects it."""
    base = standard_payload()
    return base.model_copy(
        update={
            "risk": RiskLimits(
                profile="paper",
                max_symbol_pct_nav=25,
                cash_reserve_pct=20,
                daily_loss_halt_pct=8,
                max_contracts_per_symbol=5,
                max_position_per_symbol=20000,
                min_rv_pctl_new_short=40,
                min_dte_new=21,
                max_short_opt_margin_pct=35,
                earnings_blackout_days=7,
                max_orders_per_day=6,
            )
        }
    )
