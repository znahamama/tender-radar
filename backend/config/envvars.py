"""Typed, range-checked environment settings, so a bad value fails at startup, not mid-run.

Only for non-secret settings: error messages echo the offending value.
"""

from __future__ import annotations

import math
import os
from collections.abc import Collection
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ImproperlyConfigured


def env_str(name: str, default: str, *, choices: Collection[str] | None = None) -> str:
    value = os.environ.get(name, default).strip()
    if not value:
        raise ImproperlyConfigured(f"{name} must not be empty")
    if choices is not None and value not in choices:
        raise ImproperlyConfigured(f"{name}={value!r}: expected one of {sorted(choices)}")
    return value


def _in_range(name: str, value, lo, hi):
    if not lo <= value <= hi:
        raise ImproperlyConfigured(f"{name}={value}: must be between {lo} and {hi}")
    return value


def env_int(name: str, default: int, *, lo: int, hi: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        raise ImproperlyConfigured(f"{name}={raw!r} is not an integer") from None
    return _in_range(name, value, lo, hi)


def env_float(name: str, default: float, *, lo: float, hi: float) -> float:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError:
        raise ImproperlyConfigured(f"{name}={raw!r} is not a number") from None
    if math.isnan(value):
        raise ImproperlyConfigured(f"{name}={raw!r} is not a number")
    return _in_range(name, value, lo, hi)


def env_decimal(name: str, default: str, *, lo: Decimal, hi: Decimal) -> Decimal:
    raw = os.environ.get(name, default).strip()
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise ImproperlyConfigured(f"{name}={raw!r} is not a decimal number") from None
    if not value.is_finite():
        raise ImproperlyConfigured(f"{name}={raw!r} is not a finite number")
    return _in_range(name, value, lo, hi)
