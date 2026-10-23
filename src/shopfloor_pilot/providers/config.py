"""Explicit paid-run configuration and environment-file loading.

A key being present is never treated as permission to spend.  Every paid run
also requires a currency, workflow and run monetary caps, dated rates, a tariff
source, a model, and an endpoint.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

from ..budgets import ProviderLimits
from .costs import TokenRates, to_micros
from .errors import ConfigurationError
from .retry import RetryPolicy

DEFAULT_LEDGER_PATH: Final = Path(".runtime/provider_ledger.sqlite3")
DEFAULT_MAX_OUTPUT_TOKENS: Final = 1_024
ENV_FILE_PREFIXES: Final = ("DEEPSEEK_", "SHOPFLOOR_PROVIDER_")
REASONING_EFFORTS: Final = frozenset({"low", "high", "max"})

_REQUIRED_NAMES: Final = (
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL",
    "SHOPFLOOR_PROVIDER_CURRENCY",
    "SHOPFLOOR_PROVIDER_WORKFLOW_BUDGET",
    "SHOPFLOOR_PROVIDER_RUN_BUDGET",
    "SHOPFLOOR_PROVIDER_INPUT_CACHE_HIT_RATE",
    "SHOPFLOOR_PROVIDER_INPUT_CACHE_MISS_RATE",
    "SHOPFLOOR_PROVIDER_OUTPUT_RATE",
    "SHOPFLOOR_PROVIDER_TARIFF_DATE",
    "SHOPFLOOR_PROVIDER_TARIFF_SOURCE",
)


@dataclass(frozen=True, slots=True)
class DeepSeekSettings:
    """Server-side connection settings; the API key is excluded from repr."""

    api_key: str = field(repr=False)
    base_url: str
    model: str
    thinking: bool
    reasoning_effort: str | None
    limits: ProviderLimits

    def __post_init__(self) -> None:
        if not isinstance(self.api_key, str) or len(self.api_key) < 8:
            raise ValueError("api_key is missing or implausibly short")
        if any(character.isspace() for character in self.api_key):
            raise ValueError("api_key must not contain whitespace")
        _validate_base_url(self.base_url)
        _validate_model(self.model)
        if self.reasoning_effort is not None and self.reasoning_effort not in REASONING_EFFORTS:
            raise ValueError("reasoning_effort must be low, high, or max")

    @property
    def secrets(self) -> tuple[str, ...]:
        return (self.api_key,)

    def public_dict(self) -> dict[str, Any]:
        return {
            "provider": "deepseek",
            "base_url": self.base_url,
            "model": self.model,
            "thinking": self.thinking,
            "reasoning_effort": self.reasoning_effort,
            "limits": self.limits.public_dict(),
        }


@dataclass(frozen=True, slots=True)
class PaidRunSettings:
    """Everything required before a billable request may be attempted."""

    connection: DeepSeekSettings
    pricing: TokenRates
    workflow_cap_micros: int
    run_cap_micros: int
    ledger_path: Path
    retry_policy: RetryPolicy
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS

    def __post_init__(self) -> None:
        if self.pricing.currency != self.pricing.currency.upper():
            raise ValueError("currency must be upper case")
        if not 1 <= self.max_output_tokens <= 32_768:
            raise ValueError("max_output_tokens must be between 1 and 32768")
        if self.workflow_cap_micros <= 0 or self.run_cap_micros <= 0:
            raise ValueError("workflow and run caps must be positive")
        if self.workflow_cap_micros > self.run_cap_micros:
            raise ValueError("workflow cap cannot exceed the parent run cap")

    @property
    def api_key(self) -> str:
        return self.connection.api_key

    @property
    def model(self) -> str:
        return self.connection.model

    @property
    def currency(self) -> str:
        return self.pricing.currency

    @property
    def secrets(self) -> tuple[str, ...]:
        return self.connection.secrets

    def public_dict(self) -> dict[str, Any]:
        return {
            "connection": self.connection.public_dict(),
            "pricing": self.pricing.public_dict(),
            "workflow_cap_micros": self.workflow_cap_micros,
            "run_cap_micros": self.run_cap_micros,
            "ledger_path": str(self.ledger_path),
            "retry_policy": {
                "max_retries_per_request": self.retry_policy.max_retries_per_request,
                "initial_backoff_seconds": self.retry_policy.initial_backoff_seconds,
                "multiplier": self.retry_policy.multiplier,
                "max_backoff_seconds": self.retry_policy.max_backoff_seconds,
            },
            "max_output_tokens": self.max_output_tokens,
        }


def load_env_file(path: str | Path) -> dict[str, str]:
    """Read only provider-prefixed settings from a simple KEY=VALUE file."""

    env_path = Path(path)
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        raise ConfigurationError("could not read the requested provider env file") from None
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key:
            raise ConfigurationError(
                f"provider env file line {line_number} is not a KEY=VALUE assignment"
            )
        if not key.startswith(ENV_FILE_PREFIXES):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def merged_environment(
    environ: Mapping[str, str] | None = None,
    *,
    env_file: str | Path | None = None,
) -> dict[str, str]:
    """Merge an optional provider env file under the process environment."""

    base = dict(os.environ if environ is None else environ)
    if env_file is None:
        return base
    file_values = load_env_file(env_file)
    return {**file_values, **base}


def load_paid_run_settings(environ: Mapping[str, str]) -> PaidRunSettings:
    """Build paid-run settings or fail before any network request."""

    missing: list[str] = []
    api_key = _read_api_key(environ, missing)
    base_url = _required(environ, "DEEPSEEK_BASE_URL", missing)
    model = _required(environ, "DEEPSEEK_MODEL", missing)
    currency = _required(environ, "SHOPFLOOR_PROVIDER_CURRENCY", missing)
    workflow_budget = _required(environ, "SHOPFLOOR_PROVIDER_WORKFLOW_BUDGET", missing)
    run_budget = _required(environ, "SHOPFLOOR_PROVIDER_RUN_BUDGET", missing)
    hit_rate = _required(environ, "SHOPFLOOR_PROVIDER_INPUT_CACHE_HIT_RATE", missing)
    miss_rate = _required(environ, "SHOPFLOOR_PROVIDER_INPUT_CACHE_MISS_RATE", missing)
    output_rate = _required(environ, "SHOPFLOOR_PROVIDER_OUTPUT_RATE", missing)
    tariff_date = _required(environ, "SHOPFLOOR_PROVIDER_TARIFF_DATE", missing)
    tariff_source = _required(environ, "SHOPFLOOR_PROVIDER_TARIFF_SOURCE", missing)
    if missing:
        raise ConfigurationError(
            "missing required paid-run settings: " + ", ".join(sorted(missing)),
            details={"missing": sorted(missing)},
        )
    assert api_key is not None
    _validate_base_url(base_url)
    _validate_model(model)
    currency = _parse_currency(currency)
    workflow_micros = to_micros(_parse_money(workflow_budget, "workflow budget"))
    run_micros = to_micros(_parse_money(run_budget, "run budget"))
    if workflow_micros > run_micros:
        raise ConfigurationError("workflow budget cannot exceed the parent run budget")
    try:
        rates = TokenRates(
            currency=currency,
            input_cache_hit_per_million=_parse_decimal(
                hit_rate, "SHOPFLOOR_PROVIDER_INPUT_CACHE_HIT_RATE"
            ),
            input_cache_miss_per_million=_parse_decimal(
                miss_rate, "SHOPFLOOR_PROVIDER_INPUT_CACHE_MISS_RATE"
            ),
            output_per_million=_parse_decimal(output_rate, "SHOPFLOOR_PROVIDER_OUTPUT_RATE"),
            tariff_date=_parse_date(tariff_date),
            source_url=tariff_source,
            model=model,
        )
        rates.validate_source_url()
        thinking = _parse_thinking(environ.get("DEEPSEEK_THINKING", "disabled"))
        effort = environ.get("DEEPSEEK_REASONING_EFFORT", "").strip() or None
        if effort is not None and effort not in REASONING_EFFORTS:
            raise ConfigurationError("DEEPSEEK_REASONING_EFFORT must be low, high, or max")
        limits = ProviderLimits(
            max_provider_attempts=_int_setting(
                environ, "SHOPFLOOR_PROVIDER_MAX_ATTEMPTS", 8, minimum=1
            ),
            max_tool_executions=_int_setting(
                environ, "SHOPFLOOR_PROVIDER_MAX_TOOL_EXECUTIONS", 16, minimum=0
            ),
            max_retries_per_request=_int_setting(
                environ, "SHOPFLOOR_PROVIDER_MAX_RETRIES_PER_REQUEST", 2, minimum=0
            ),
            attempt_timeout_seconds=_float_setting(
                environ, "SHOPFLOOR_PROVIDER_ATTEMPT_TIMEOUT_SECONDS", 30.0
            ),
            max_active_seconds=_float_setting(environ, "SHOPFLOOR_PROVIDER_ACTIVE_SECONDS", 120.0),
            max_tokens=_int_setting(environ, "SHOPFLOOR_PROVIDER_MAX_TOKENS", 32_000, minimum=1),
        )
        retry_policy = RetryPolicy(
            max_retries_per_request=limits.max_retries_per_request,
            initial_backoff_seconds=_float_setting(
                environ, "SHOPFLOOR_PROVIDER_RETRY_BACKOFF_SECONDS", 1.0
            ),
            max_backoff_seconds=_float_setting(
                environ, "SHOPFLOOR_PROVIDER_RETRY_MAX_BACKOFF_SECONDS", 4.0
            ),
        )
        max_output_tokens = _int_setting(
            environ,
            "SHOPFLOOR_PROVIDER_MAX_OUTPUT_TOKENS",
            DEFAULT_MAX_OUTPUT_TOKENS,
            minimum=1,
        )
        if max_output_tokens > 32_768:
            raise ConfigurationError("SHOPFLOOR_PROVIDER_MAX_OUTPUT_TOKENS must be at most 32768")
        ledger_path = Path(
            environ.get("SHOPFLOOR_PROVIDER_LEDGER_PATH", "").strip() or DEFAULT_LEDGER_PATH
        )
        connection = DeepSeekSettings(
            api_key=api_key,
            base_url=_normalize_base_url(base_url),
            model=model.strip(),
            thinking=thinking,
            reasoning_effort=effort,
            limits=limits,
        )
        return PaidRunSettings(
            connection=connection,
            pricing=rates,
            workflow_cap_micros=workflow_micros,
            run_cap_micros=run_micros,
            ledger_path=ledger_path,
            retry_policy=retry_policy,
            max_output_tokens=max_output_tokens,
        )
    except ConfigurationError:
        raise
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"invalid paid-run configuration: {exc}") from None


def _read_api_key(environ: Mapping[str, str], missing: list[str]) -> str | None:
    value = environ.get("DEEPSEEK_API_KEY", "").strip()
    if value:
        if any(character.isspace() for character in value):
            raise ConfigurationError("DEEPSEEK_API_KEY contains whitespace")
        return value
    key_file = environ.get("DEEPSEEK_API_KEY_FILE", "").strip()
    if not key_file:
        missing.append("DEEPSEEK_API_KEY or DEEPSEEK_API_KEY_FILE")
        return None
    try:
        file_value = Path(key_file).read_text(encoding="utf-8").strip()
    except OSError:
        raise ConfigurationError("could not read DEEPSEEK_API_KEY_FILE") from None
    if not file_value:
        raise ConfigurationError("DEEPSEEK_API_KEY_FILE is empty")
    if any(character.isspace() for character in file_value):
        raise ConfigurationError("DEEPSEEK_API_KEY_FILE contains whitespace")
    return file_value


def _required(environ: Mapping[str, str], name: str, missing: list[str]) -> str:
    value = environ.get(name, "").strip()
    if not value:
        missing.append(name)
    return value


def _parse_decimal(value: str, name: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ConfigurationError(f"{name} must be a decimal number") from None
    if not parsed.is_finite() or parsed < 0:
        raise ConfigurationError(f"{name} must be a finite non-negative decimal")
    return parsed


def _parse_money(value: str, label: str) -> Decimal:
    parsed = _parse_decimal(value, label)
    if parsed <= 0:
        raise ConfigurationError(f"{label} must be positive")
    return parsed


def _parse_currency(value: str) -> str:
    currency = value.strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        raise ConfigurationError("SHOPFLOOR_PROVIDER_CURRENCY must be a three-letter code")
    return currency


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise ConfigurationError("SHOPFLOOR_PROVIDER_TARIFF_DATE must be YYYY-MM-DD") from None


def _parse_thinking(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"enabled", "true", "1"}:
        return True
    if normalized in {"disabled", "false", "0", ""}:
        return False
    raise ConfigurationError("DEEPSEEK_THINKING must be enabled or disabled")


def _int_setting(
    environ: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
) -> int:
    raw = environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigurationError(f"{name} must be an integer") from None
    if value < minimum:
        raise ConfigurationError(f"{name} must be at least {minimum}")
    return value


def _float_setting(environ: Mapping[str, str], name: str, default: float) -> float:
    raw = environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigurationError(f"{name} must be a number") from None
    if not math.isfinite(value) or value < 0:
        raise ConfigurationError(f"{name} must be a finite non-negative number")
    return value


def _validate_base_url(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError("DEEPSEEK_BASE_URL is required")
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or not parsed.netloc:
        raise ConfigurationError("DEEPSEEK_BASE_URL must be an https URL")
    if parsed.username or parsed.password:
        raise ConfigurationError("DEEPSEEK_BASE_URL must not embed credentials")
    if parsed.query or parsed.fragment:
        raise ConfigurationError("DEEPSEEK_BASE_URL must not contain a query or fragment")
    if parsed.hostname != "api.deepseek.com" or parsed.port is not None:
        raise ConfigurationError("DEEPSEEK_BASE_URL must use the official api.deepseek.com host")
    if parsed.path not in {"", "/"}:
        raise ConfigurationError("DEEPSEEK_BASE_URL must use the standard non-beta endpoint")


def _normalize_base_url(value: str) -> str:
    return value.strip().rstrip("/")


def _validate_model(value: str | None) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError("DEEPSEEK_MODEL is required")
    model = value.strip()
    if len(model) > 128 or any(character.isspace() for character in model):
        raise ConfigurationError("DEEPSEEK_MODEL is not a valid model identifier")
