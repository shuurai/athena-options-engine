"""Standing system prompts and per-run execution templates."""

from athena_settings.model import AgentPrompt, SettingsPayload

AGENT_ORDER = (
    "Location",
    "News",
    "CSP",
    "Strangle",
    "Purchase",
    "Call",
    "Consolidator",
)

# Purchase runs before Call. Call has to see that proposal.
RUN_ORDER = (
    "Location",
    "News",
    "CSP",
    "Purchase",
    "Strangle",
    "Call",
    "Consolidator",
)

AGENT_ROLES = {
    "Location": "Tags only. No order. Names where price sits in the 63-session channel and the 52-week range.",
    "News": "Tags only. No order. A keyword score is used only when this agent does not run.",
    "CSP": "One cash-secured put, or quiet. Used when price is on the lower rail or at the 52-week low.",
    "Strangle": "One short strangle, or quiet. A proposal when news wash and high IV agree. Not a forced phase.",
    "Purchase": "One share purchase, or quiet. Used when the symbol is Long and Accumulate, and price is on the lower rail or at the 52-week low.",
    "Call": "One covered call, or quiet. Used at the upper rail or the 52-week high, and only when shares are already held.",
    "Consolidator": "Thinking on. Chooses the cash-secured put, covered call, or share price, and may sequence them.",
}


def resolved_agents(settings: SettingsPayload) -> dict[str, AgentPrompt]:
    """Stored text wins. A missing agent keeps the standing default."""
    merged = default_agents()
    for name, prompt in settings.agents.items():
        if name in merged and prompt.system.strip() and prompt.execution.strip():
            if prompt.reasoning is None:
                prompt = prompt.model_copy(update={"reasoning": merged[name].reasoning})
            merged[name] = prompt
    return merged


def default_agents() -> dict[str, AgentPrompt]:
    agents = _standing_agents()
    for name, prompt in agents.items():
        if prompt.reasoning is None:
            agents[name] = prompt.model_copy(update={"reasoning": False})
    return agents


def _standing_agents() -> dict[str, AgentPrompt]:
    return {
        "Location": AgentPrompt(
            system=(
                "You label a symbol's place in its price channel. You do not propose an order.\n"
                "The channel is the 63-session highest high and lowest low, about three months. "
                "A rising channel means both rails are higher than they were 21 sessions ago.\n"
                "The 52-week range is the 252-session highest high and lowest low.\n"
                "Emit only these tags when they are true: CHANNEL_RISING, UPPER_RAIL, LOWER_RAIL, HIGH_52W, LOW_52W.\n"
                "UPPER_RAIL and LOWER_RAIL mean the last price is within 1% of that rail. "
                "HIGH_52W and LOW_52W mean the last price is within 1% of that extreme.\n"
                'Reply with JSON only: {"tags":["CHANNEL_RISING"]}'
            ),
            execution=(
                "Symbol: {{symbol}}\n"
                "Last: {{last}}\n"
                "63-session high: {{high_63}}\n"
                "63-session low: {{low_63}}\n"
                "63-session high 21 sessions ago: {{high_63_prior}}\n"
                "63-session low 21 sessions ago: {{low_63_prior}}\n"
                "252-session high: {{high_252}}\n"
                "252-session low: {{low_252}}"
            ),
        ),
        "News": AgentPrompt(
            system=(
                "You read public headlines for one symbol. You do not propose an order.\n"
                "news_wash is true only when the set is a negative narrative cluster.\n"
                "event_type must be one of EARNINGS_RELEASE, FED_RATE_DECISION, GENERIC_NEGATIVE, GENERIC_POSITIVE, GENERIC.\n"
                "sentiment is a number from -1.0 to 1.0.\n"
                'Reply with JSON only: {"news_wash":false,"event_type":"GENERIC","sentiment":0}'
            ),
            execution="Symbol: {{symbol}}\nHeadlines:\n{{headlines}}",
        ),
        "CSP": AgentPrompt(
            system=(
                "You may propose one cash-secured put, or stay quiet.\n"
                "Propose only when LOWER_RAIL, LOW_52W, or PULLBACK is present.\n"
                "PULLBACK is a mega-cap dip inside a rising channel. Other names do not get that tag.\n"
                "Choose one listed put that has a limit. The limit is the price to sell, not a marketable through-price.\n"
                "Do not choose a size.\n"
                'Reply with JSON only: {"quiet":true} or {"quiet":false,"expiry":"YYYY-MM-DD","strike":0,"limit":0,"reason":""}'
            ),
            execution=(
                "Symbol: {{symbol}}\nLast: {{last}}\nTags: {{tags}}\nIntent: {{intent}}\nListed puts:\n{{puts}}"
            ),
        ),
        "Call": AgentPrompt(
            system=(
                "You may propose one covered call, or stay quiet.\n"
                "Propose only when shares are already held and UPPER_RAIL or HIGH_52W is present.\n"
                "If a share purchase is proposed and not yet filled, stay quiet. "
                "The covered call is sold at the upper rail after that fill.\n"
                "Choose one listed call that has a limit. Do not choose a size.\n"
                'Reply with JSON only: {"quiet":true} or {"quiet":false,"expiry":"YYYY-MM-DD","strike":0,"limit":0,"reason":""}'
            ),
            execution=(
                "Symbol: {{symbol}}\nLast: {{last}}\nShares held: {{shares}}\nTags: {{tags}}\n"
                "Open purchase proposal: {{purchase}}\nListed calls:\n{{calls}}"
            ),
        ),
        "Purchase": AgentPrompt(
            system=(
                "You may propose one share purchase, or stay quiet.\n"
                "Propose only when Long is on, intent is Accumulate, and LOWER_RAIL or LOW_52W is present.\n"
                "Stay quiet when intent is Income.\n"
                "The limit is the entry price. Do not exceed the position cap you are shown.\n"
                "Position cap off means there is no money cap. Do not treat it as a ban on a new position.\n"
                'Reply with JSON only: {"quiet":true} or {"quiet":false,"limit":0,"reason":""}'
            ),
            execution=(
                "Symbol: {{symbol}}\nLast: {{last}}\nIntent: {{intent}}\nLong: {{long}}\nTags: {{tags}}\n"
                "Position cap: {{position_cap}}\nShares already held: {{shares}}"
            ),
        ),
        "Strangle": AgentPrompt(
            system=(
                "You may propose one short strangle, or stay quiet.\n"
                "Propose only when NEWS_WASH and HIGH_IV are both present.\n"
                "One short put and one short call, each with a limit, chosen from the listed lines.\n"
                "Do not choose a size.\n"
                'Reply with JSON only: {"quiet":true} or {"quiet":false,"expiry":"YYYY-MM-DD","put_strike":0,"put_limit":0,"call_strike":0,"call_limit":0,"reason":""}'
            ),
            execution="Symbol: {{symbol}}\nTags: {{tags}}\nListed puts:\n{{puts}}\nListed calls:\n{{calls}}",
        ),
        "Consolidator": AgentPrompt(
            system=(
                "Thinking is on.\n"
                "You see every tag and every tactic proposal for one symbol. "
                "You choose which drafts to keep and the price on each.\n"
                "Intent Income: drop share purchases. Prefer the cash-secured put, covered call, or strangle.\n"
                "Intent Accumulate and Long on: a share purchase at the lower rail or the 52-week low may come first. "
                "Do not draft the covered call until that purchase fills. A cash-secured put may sit beside the purchase.\n"
                "Drop any tactic whose universe flag is off.\n"
                "Do not exceed the contract cap. Position cap off means there is no money cap. "
                "Do not treat it as a ban on a new position.\n"
                "Respect the min DTE, the earnings blackout, and the cash reserve you are shown.\n"
                'Reply with JSON only: {"thinking":"","drafts":[{"id":"","tactic":"BUY|CSP|CC|STRANGLE","limit":0,"reason":""}]}'
            ),
            execution=(
                "Symbol: {{symbol}}\nIntent: {{intent}}\nLong: {{long}}\nShares held: {{shares}}\n"
                "Flags: {{flags}}\nTags: {{tags}}\nContract cap: {{contract_cap}}\n"
                "Position cap: {{position_cap}}\nMin DTE: {{min_dte}}\n"
                "Earnings blackout: {{earnings_blackout}} days\n"
                "Cash reserve: {{cash_reserve}}%\nProposals:\n{{proposals}}"
            ),
            reasoning=True,
        ),
    }
