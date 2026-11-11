import copy
import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import anthropic
import pytest

from tenders.extraction.budget import CallMeter
from tenders.models import Tender

NOW = datetime(2026, 11, 11, 12, tzinfo=UTC)

# Invented notice text; not copied from any real CanadaBuys notice.
DESCRIPTION = """Public Works requires an IT service provider to modernize a records system.
The work will be performed on-site at   St. John's, Newfoundland and Labrador.
Bidders must hold a valid TBIPS supply arrangement.
The Contractor must hold a valid Designated Organization Screening (DOS).
Deliver a migration plan within 30 days of contract award.
Bids close on December 1, 2026 at 2:00 PM NST.
This procurement is subject to the Canadian Free Trade Agreement.
Bid documents are available on CanadaBuys."""

VALID = {
    "summary": "Modernize a records system for Public Works in St. John's.",
    "work_type": "it_services",
    "criteria_location": "partly_stated",
    "deliverables": [
        {"text": "Migration plan", "evidence": "Deliver a migration plan within 30 days"},
    ],
    "procurement_method": None,
    "deadlines": [
        {"type": "closing", "datetime": "2026-12-01T14:00:00-03:30",
         "evidence": "Bids close on December 1, 2026 at 2:00 PM NST."},
    ],
    "mandatory_requirements": [
        {"text": "TBIPS supply arrangement", "category": "other",
         "evidence": "Bidders must hold a valid TBIPS supply arrangement."},
    ],
    "certifications": [],
    "eligibility": {
        "supply_arrangement_required": {
            "value": "yes", "name": "TBIPS",
            "evidence": "Bidders must hold a valid TBIPS supply arrangement."},
        "supplier_origin_restriction": {"value": "unknown", "evidence": None},
        "clearance": {"corporate": "reliability", "personnel": "unknown",
                      "evidence": "valid Designated Organization Screening (DOS)"},
        # Different case and whitespace from the source on purpose.
        "work_location": {"place": "St. John's", "on_site": "yes",
                          "evidence": "performed ON-SITE at St. John's"},
        "land_claims_applies": {"value": "unknown", "evidence": None},
        "documents_behind_login": {"value": "no", "portal": "CanadaBuys",
                                   "evidence": "Bid documents are available on CanadaBuys."},
    },
    "injection_suspected": False,
}

PINNED_LLM_SETTINGS = {
    "LLM_MODEL": "claude-haiku-5-5",
    "LLM_EFFORT": "low",
    "LLM_MAX_TOKENS": 2000,
    "LLM_TIMEOUT_S": 60.0,
    "LLM_MAX_CALLS_PER_RUN": 50,
    "LLM_DAILY_BUDGET_USD": Decimal("0.50"),
    "LLM_MAX_DESCRIPTION_CHARS": 30_000,
}


def valid_data(**changes) -> dict:
    d = copy.deepcopy(VALID)
    d.update(changes)
    return d


def tender_fields(**fields) -> dict:
    return {
        "reference_number": "TEST-0", "title": "Records system modernization",
        "description": DESCRIPTION, "notice_url": "https://example.com/notice",
        "tender_status": "Open", "region_scope": "nl", "notice_kind": "regular",
        "closing_date": datetime(2026, 12, 1, 17, 30, tzinfo=UTC),
        "content_hash": "x", "first_seen_at": NOW, "last_seen_at": NOW, "updated_at": NOW,
        **fields,
    }


@pytest.fixture(autouse=True)
def pin_llm_settings(settings):
    """A local .env must not change test results."""
    for name, value in PINNED_LLM_SETTINGS.items():
        setattr(settings, name, value)


@pytest.fixture(autouse=True)
def no_real_api(monkeypatch):
    """No test may build a real client (however it is imported) or see a real credential."""
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)

    def boom(self, *args, **kwargs):
        raise RuntimeError("real Anthropic client constructed in a test")

    monkeypatch.setattr(anthropic.Anthropic, "__init__", boom)
    monkeypatch.setattr(anthropic.AsyncAnthropic, "__init__", boom)


@pytest.fixture
def make_tender(db):
    counter = {"n": 0}

    def make(**fields):
        counter["n"] += 1
        fields.setdefault("reference_number", f"TEST-{counter['n']}")
        return Tender.objects.create(**tender_fields(**fields))

    return make


@pytest.fixture
def meter():
    return CallMeter(max_calls=50, daily_budget_usd=Decimal("0.50"))


def response(payload, *, stop_reason="end_turn", input_tokens=1000, output_tokens=200,
             request_id="req_test", text_blocks=True):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    content = [SimpleNamespace(type="thinking", thinking="", signature="sig")]
    if text_blocks:
        content.append(SimpleNamespace(type="text", text=text))
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
        _request_id=request_id,
    )


class StubClient:
    """Stands in for anthropic.Anthropic: returns (or raises) queued items in order."""

    def __init__(self, *items):
        self.items = list(items)
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
