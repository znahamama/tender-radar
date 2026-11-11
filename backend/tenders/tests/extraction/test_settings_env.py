from decimal import Decimal

import pytest
from config.envvars import env_decimal, env_float, env_int, env_str
from django.core.exceptions import ImproperlyConfigured


def test_defaults_when_unset(monkeypatch):
    for name in ("X_STR", "X_INT", "X_FLOAT", "X_DEC"):
        monkeypatch.delenv(name, raising=False)
    assert env_str("X_STR", "low", choices=("low", "high")) == "low"
    assert env_int("X_INT", 5, lo=1, hi=10) == 5
    assert env_float("X_FLOAT", 60, lo=5, hi=600) == 60.0
    assert env_decimal("X_DEC", "0.50", lo=Decimal(0), hi=Decimal(10)) == Decimal("0.50")


@pytest.mark.parametrize(("fn", "raw", "kwargs"), [
    (env_str, "hgih", {"choices": ("low", "high")}),
    (env_str, "  ", {}),
    (env_int, "2k", {"lo": 1, "hi": 10}),
    (env_int, "0", {"lo": 1, "hi": 10}),
    (env_float, "nan", {"lo": 5, "hi": 600}),
    (env_float, "1", {"lo": 5, "hi": 600}),
    (env_decimal, "fifty cents", {"lo": Decimal(0), "hi": Decimal(10)}),
    (env_decimal, "Infinity", {"lo": Decimal(0), "hi": Decimal(10)}),
    (env_decimal, "-1", {"lo": Decimal(0), "hi": Decimal(10)}),
    (env_decimal, "50", {"lo": Decimal(0), "hi": Decimal(10)}),
])
def test_bad_values_fail_fast(monkeypatch, fn, raw, kwargs):
    monkeypatch.setenv("X_VAR", raw)
    default = {env_str: "low", env_int: 5, env_float: 60.0, env_decimal: "1"}[fn]
    with pytest.raises(ImproperlyConfigured, match="X_VAR"):
        fn("X_VAR", default, **kwargs)


def test_budget_setting_is_a_decimal():
    from django.conf import settings
    assert isinstance(settings.LLM_DAILY_BUDGET_USD, Decimal)


def test_unpriced_model_fails_system_check(settings):
    from tenders.checks import llm_model_is_priced
    assert llm_model_is_priced(None) == []
    settings.LLM_MODEL = "claude-opus-5-5"
    (err,) = llm_model_is_priced(None)
    assert err.id == "tenders.E001"
