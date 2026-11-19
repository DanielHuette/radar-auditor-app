from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from radarauditor.errors import ConfigurationError
from radarauditor.feedback.routes import load_measurements

OPENAI_SERVICE = "openai_chat_completions"
ASSISTANTS_MEASUREMENT = "assistants"
CHATGPT_VARIANT = "chatgpt"
OPENAI_SUPPLIER = "openai_api"
PRICES_KEY = "usd_per_million_tokens"
PRICE_SOURCE_KEY = "price_source"
PRICE_READ_ON_KEY = "price_read_on"
INPUT_KEY = "input"
OUTPUT_KEY = "output"
TOKENS_PER_PRICE_UNIT = Decimal(1_000_000)
MAX_OUTPUT_TOKENS = 1500
MESSAGE_OVERHEAD_TOKENS = 16


@dataclass(frozen=True)
class TokenPrice:
    input_usd_per_million: Decimal
    output_usd_per_million: Decimal


def _amount(path: Path, model: str, key: str, value: object) -> Decimal:
    try:
        amount = Decimal(str(value))
    except InvalidOperation as error:
        raise ConfigurationError(
            f"{path}: OpenAI price {key} of model {model} is not a number: {value!r}"
        ) from error
    if not amount.is_finite() or amount <= 0:
        raise ConfigurationError(
            f"{path}: OpenAI price {key} of model {model} must be above zero, got {amount}"
        )
    return amount


def load_token_price(path: Path, model: str) -> TokenPrice | None:
    measurement = load_measurements(path).get(ASSISTANTS_MEASUREMENT)
    routes = measurement.variants.get(CHATGPT_VARIANT, []) if measurement else []
    route = next((item for item in routes if item.supplier == OPENAI_SUPPLIER), None)
    if route is None:
        return None
    extra = route.model_extra or {}
    if not extra.get(PRICE_SOURCE_KEY) or not extra.get(PRICE_READ_ON_KEY):
        return None
    prices = extra.get(PRICES_KEY)
    entry = prices.get(model) if isinstance(prices, dict) else None
    if entry is None:
        return None
    if not isinstance(entry, dict) or not {INPUT_KEY, OUTPUT_KEY} <= entry.keys():
        raise ConfigurationError(
            f"{path}: OpenAI price of model {model} needs {INPUT_KEY} and {OUTPUT_KEY}"
        )
    return TokenPrice(
        _amount(path, model, INPUT_KEY, entry[INPUT_KEY]),
        _amount(path, model, OUTPUT_KEY, entry[OUTPUT_KEY]),
    )


def call_cost_usd(price: TokenPrice, tokens_in: int, tokens_out: int) -> Decimal:
    total = price.input_usd_per_million * tokens_in + price.output_usd_per_million * tokens_out
    return total / TOKENS_PER_PRICE_UNIT


def max_call_cost_usd(price: TokenPrice, question_bytes: int) -> Decimal:
    return call_cost_usd(price, question_bytes + MESSAGE_OVERHEAD_TOKENS, MAX_OUTPUT_TOKENS)
