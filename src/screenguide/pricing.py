"""Estimated cost of a model call. Prices live only in `~/.config/screenguide/pricing.toml`."""

from dataclasses import dataclass
from pathlib import Path

from screenguide.config import ConfigError, config_dir, load_toml

LOCAL_PROVIDERS = frozenset({"ollama"})
TOKENS_PER_PRICE_UNIT = 1_000_000


@dataclass(frozen=True)
class ModelPrice:
    input_usd_per_mtok: float
    output_usd_per_mtok: float


class PricingTable:
    def __init__(self, prices: dict[tuple[str, str], ModelPrice]) -> None:
        self._prices = prices

    def cost(
        self, provider: str, model: str, input_tokens: int | None, output_tokens: int | None
    ) -> float | None:
        """Return the cost in USD, or `None` if the model has no price or a count is missing."""
        if provider in LOCAL_PROVIDERS:
            return 0.0
        price = self._prices.get((provider, model))
        if price is None or input_tokens is None or output_tokens is None:
            return None
        return (
            input_tokens * price.input_usd_per_mtok + output_tokens * price.output_usd_per_mtok
        ) / TOKENS_PER_PRICE_UNIT


def load_pricing(path: Path | None = None) -> PricingTable:
    """Read the table `[provider."model"]`; a missing file is an empty table."""
    path = path or config_dir() / "pricing.toml"
    prices = {}
    for provider, models in load_toml(path).items():
        if not isinstance(models, dict):
            raise ConfigError(f"{path}: {provider} must be a table of models")
        for model, entry in models.items():
            prices[(provider, model)] = _parse_price(path, f"{provider}.{model}", entry)
    return PricingTable(prices)


def _parse_price(path: Path, name: str, entry: object) -> ModelPrice:
    if not isinstance(entry, dict):
        raise ConfigError(f"{path}: {name} must be a table")
    values = []
    for field in ("input_usd_per_mtok", "output_usd_per_mtok"):
        value = entry.get(field)
        if type(value) not in (int, float) or value < 0:
            raise ConfigError(f"{path}: {name}.{field} must be a non-negative number")
        values.append(float(value))
    return ModelPrice(*values)
