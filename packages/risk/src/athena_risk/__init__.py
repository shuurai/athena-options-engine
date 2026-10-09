"""Draft size checks. Does not send orders."""

from athena_settings.model import RiskLimits


def block_reason(contracts: int, limits: RiskLimits) -> str | None:
    if contracts < 1:
        return "contracts must be at least 1"
    if contracts > limits.max_contracts_per_symbol:
        return "contracts exceed max_contracts_per_symbol"
    return None
