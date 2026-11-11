"""Prices, per-run and per-day caps, run stopping, and LlmCall bookkeeping."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time
from decimal import ROUND_CEILING, Decimal

from django.db.models import Sum
from django.utils import timezone

from tenders.models import LlmCall

MILLION = Decimal(1_000_000)
MICRODOLLAR = Decimal("0.000001")  # LlmCall.cost_usd precision
CHARS_PER_TOKEN = 3.5  # deliberately pessimistic, for worst-case estimates
LONG_PROMPT_TOKENS = 100_000
# USD per million tokens: (input, output) for prompts up to 100k tokens, then above that.
PRICES = {
    "claude-haiku-5-5": ((Decimal("0.10"), Decimal("0.50")), (Decimal("0.50"), Decimal("2.50"))),
}


def is_priced(model_id: str) -> bool:
    return model_id in PRICES


def cost_usd(model_id: str, input_tokens: int, output_tokens: int) -> Decimal:
    short, long = PRICES[model_id]
    rate_in, rate_out = long if input_tokens > LONG_PROMPT_TOKENS else short
    cost = (input_tokens * rate_in + output_tokens * rate_out) / MILLION
    return cost.quantize(MICRODOLLAR, rounding=ROUND_CEILING)  # never undercount the cap


def estimate_tokens(chars: int) -> int:
    return math.ceil(chars / CHARS_PER_TOKEN)


class StopRun(Exception):
    """Stop the whole run cleanly (the command prints the reason and the totals)."""


class CapReached(StopRun):
    """Raised before an API call that would exceed a cap."""


def spent_today(now: datetime | None = None) -> Decimal:
    now = now or timezone.now()
    local_day = timezone.localtime(now).date()
    start = timezone.make_aware(datetime.combine(local_day, time.min))
    total = LlmCall.objects.filter(created_at__gte=start).aggregate(s=Sum("cost_usd"))["s"]
    return total or Decimal(0)


@dataclass
class CallMeter:
    """Checked before every call (including validation retries); records every call."""

    max_calls: int
    daily_budget_usd: Decimal
    calls: int = 0  # counts toward max_calls: every API attempt, plus fake extractions
    api_calls: int = 0  # real API attempts only
    input_tokens: int = 0
    output_tokens: int = 0
    cost: Decimal = Decimal(0)

    def check(self) -> None:
        if self.calls >= self.max_calls:
            raise CapReached(f"per-run call cap reached ({self.max_calls} calls)")
        spent = spent_today()
        if spent >= self.daily_budget_usd:
            raise CapReached(f"daily cost cap reached (${spent:.4f} >= ${self.daily_budget_usd})")

    def record(self, *, tender=None, model_id: str, prompt_version: str, input_tokens: int = 0,
               output_tokens: int = 0, latency_ms: int = 0, status: str,
               request_id: str = "", estimated: bool = False) -> LlmCall:
        cost = cost_usd(model_id, input_tokens, output_tokens)
        self.calls += 1
        self.api_calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cost += cost
        return LlmCall.objects.create(
            purpose="extract", tender=tender, model_id=model_id, prompt_version=prompt_version,
            input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost,
            latency_ms=latency_ms, status=status, request_id=request_id or "",
            estimated=estimated,
        )

    def count_fake(self) -> None:
        """FakeExtractor: counts toward the per-run cap, writes no LlmCall row."""
        self.calls += 1
