"""Conservative token tariffs and integer micro-unit cost arithmetic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any, Final
from urllib.parse import urlsplit

from .models import TokenUsage

MICROS_PER_UNIT: Final = Decimal(1_000_000)
MICROS_QUANTUM: Final = Decimal("0.000001")


def _as_decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a decimal number")
    if isinstance(value, float):
        raise ValueError(f"{name} must be configured as a decimal string, not a float")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{name} must be a decimal number") from None
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{name} must be a finite non-negative decimal")
    return parsed


def to_micros(amount: Decimal) -> int:
    """Round a currency amount up to whole micro-units (never under-reserve)."""

    if amount < 0:
        raise ValueError("amount must be non-negative")
    quantized = amount.quantize(MICROS_QUANTUM, rounding=ROUND_CEILING)
    return int(quantized * MICROS_PER_UNIT)


def micros_to_string(micros: int) -> str:
    if not isinstance(micros, int) or isinstance(micros, bool) or micros < 0:
        raise ValueError("micros must be a non-negative integer")
    return f"{Decimal(micros) / MICROS_PER_UNIT:.6f}"


@dataclass(frozen=True, slots=True)
class TokenRates:
    """Dated per-1M-token rates.  Configure the conservative (peak) rate set."""

    currency: str
    input_cache_hit_per_million: Decimal
    input_cache_miss_per_million: Decimal
    output_per_million: Decimal
    tariff_date: date
    source_url: str
    model: str

    def __post_init__(self) -> None:
        if not isinstance(self.currency, str) or len(self.currency) != 3:
            raise ValueError("currency must be a three-letter ISO code")
        currency = self.currency.upper()
        if not currency.isalpha():
            raise ValueError("currency must contain letters only")
        object.__setattr__(self, "currency", currency)
        for name in (
            "input_cache_hit_per_million",
            "input_cache_miss_per_million",
            "output_per_million",
        ):
            object.__setattr__(self, name, _as_decimal(getattr(self, name), name))
        if self.input_cache_miss_per_million <= 0 or self.output_per_million <= 0:
            raise ValueError("cache-miss input and output rates must be positive")
        if self.input_cache_hit_per_million > self.input_cache_miss_per_million:
            raise ValueError("cache-hit rate cannot exceed the cache-miss rate")
        if not isinstance(self.tariff_date, date):
            raise ValueError("tariff_date must be a date")
        if not isinstance(self.source_url, str) or not self.source_url.startswith("https://"):
            raise ValueError("tariff source must be an https URL")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("tariff model must not be blank")

    def reserve_micros(self, prompt_tokens: int, max_output_tokens: int) -> int:
        """Reserve worst-case cache-miss input plus maximum output."""

        for name, value in (
            ("prompt_tokens", prompt_tokens),
            ("max_output_tokens", max_output_tokens),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        amount = (
            Decimal(prompt_tokens) * self.input_cache_miss_per_million
            + Decimal(max_output_tokens) * self.output_per_million
        ) / MICROS_PER_UNIT
        return to_micros(amount)

    def actual_micros(self, usage: TokenUsage) -> int:
        """Reconcile reported usage once; unknown cache detail is charged as a miss."""

        if not isinstance(usage, TokenUsage):
            raise TypeError("usage must be a TokenUsage")
        prompt = usage.prompt_tokens
        hit = usage.cache_hit_tokens
        miss = usage.cache_miss_tokens
        if hit is None and miss is None:
            hit = 0
            miss = prompt
        elif hit is None:
            assert miss is not None
            hit = max(prompt - miss, 0)
            if hit + miss != prompt:
                miss = prompt
                hit = 0
        elif miss is None:
            miss = max(prompt - hit, 0)
            if hit + miss != prompt:
                miss = prompt
                hit = 0
        amount = (
            Decimal(hit) * self.input_cache_hit_per_million
            + Decimal(miss) * self.input_cache_miss_per_million
            + Decimal(usage.completion_tokens) * self.output_per_million
        ) / MICROS_PER_UNIT
        return to_micros(amount)

    def public_dict(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "model": self.model,
            "input_cache_hit_per_million": str(self.input_cache_hit_per_million),
            "input_cache_miss_per_million": str(self.input_cache_miss_per_million),
            "output_per_million": str(self.output_per_million),
            "tariff_date": self.tariff_date.isoformat(),
            "source_url": self.source_url,
        }

    def validate_source_url(self) -> None:
        parsed = urlsplit(self.source_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("tariff source must be a valid https URL")


def estimate_request_tokens(*, utf8_bytes: int, items: int) -> int:
    """Return a deliberately conservative byte-level prompt-token allowance.

    DeepSeek's published character ratios are estimates, not hard bounds.  The
    V4/V4.1 tokenizer is byte-level, so charging one token per serialized UTF-8
    byte is a safe upper bound for request content.  The fixed and per-item
    margins cover chat-template/control tokens that are not present in the
    JSON projection used for the byte count.
    """

    if utf8_bytes < 0 or items < 0:
        raise ValueError("byte and item counts must be non-negative")
    protocol_margin = 256 + items * 32
    return max(utf8_bytes + protocol_margin, 1)
