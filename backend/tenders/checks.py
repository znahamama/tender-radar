"""System checks: run before every management command, so bad LLM config fails at startup."""

from django.conf import settings
from django.core.checks import Error, register


@register()
def llm_model_is_priced(app_configs, **kwargs):
    from tenders.extraction.budget import is_priced

    if is_priced(settings.LLM_MODEL):
        return []
    return [Error(f"LLM_MODEL={settings.LLM_MODEL!r} has no price in "
                  "tenders.extraction.budget.PRICES; the daily cost cap can't work without one.",
                  id="tenders.E001")]
