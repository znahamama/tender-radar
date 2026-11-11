# Reuse the extraction fixtures: pinned LLM settings, no real API client, tender factory.
from tenders.tests.extraction.conftest import (  # noqa: F401
    make_tender,
    meter,
    no_real_api,
    pin_llm_settings,
)
