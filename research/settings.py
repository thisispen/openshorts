"""Research configuration, read from the environment on every call (tests patch it)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_decimal(name: str, default: str) -> Decimal:
    raw = (os.getenv(name) or "").strip() or default
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    return int(raw) if raw else default


@dataclass(frozen=True)
class ResearchSettings:
    database_url: str
    # The research API fails closed: without a token every route answers 503.
    api_token: str | None
    max_jobs_per_hour: int
    daily_budget_usd: Decimal
    jev_api_key: str | None
    jev_enabled: bool
    jev_model: str
    jev_base_url: str
    # TypeSafe bills input tokens but publishes no price in its API spec, so the
    # operator sets the quoted rate. Used to reserve budget before each call.
    jev_cost_per_1k_input_usd: Decimal
    jev_max_input_tokens: int

    @property
    def jev_active(self) -> bool:
        return bool(self.jev_enabled and self.jev_api_key)


def load_settings() -> ResearchSettings:
    return ResearchSettings(
        database_url=(os.getenv("RESEARCH_DATABASE_URL") or "sqlite:///research.db").strip(),
        api_token=(os.getenv("RESEARCH_API_TOKEN") or "").strip() or None,
        max_jobs_per_hour=_env_int("RESEARCH_MAX_JOBS_PER_HOUR", 20),
        daily_budget_usd=_env_decimal("RESEARCH_DAILY_BUDGET_USD", "0"),
        jev_api_key=(os.getenv("JEV_API_KEY") or "").strip() or None,
        jev_enabled=_env_bool("JEV_ENABLED", False),
        jev_model=(os.getenv("JEV_MODEL") or "jev-latest").strip(),
        jev_base_url=(os.getenv("JEV_BASE_URL") or "https://api.typesafe.ai").rstrip("/"),
        jev_cost_per_1k_input_usd=_env_decimal("JEV_COST_PER_1K_INPUT_USD", "0.01"),
        jev_max_input_tokens=_env_int("JEV_MAX_INPUT_TOKENS", 4000),
    )
