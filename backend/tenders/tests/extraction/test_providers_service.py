import json
from datetime import timedelta
from decimal import Decimal
from io import StringIO

import anthropic
import httpx2
import pytest
from django.core.management import call_command
from django.utils import timezone

from tenders.extraction.budget import CallMeter, CapReached, cost_usd
from tenders.extraction.prompt import PROMPT_VERSION, SYSTEM_PROMPT, build_notice
from tenders.extraction.providers import AnthropicExtractor, FakeExtractor
from tenders.extraction.service import extract_tender
from tenders.models import Extraction, LlmCall

from .conftest import StubClient, response, valid_data

pytestmark = pytest.mark.django_db
REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def api_error(cls, code):
    return cls("boom", response=httpx2.Response(code, request=REQ), body=None)


# --- AnthropicExtractor with a stubbed client ---------------------------------------------

def test_valid_output(make_tender, meter):
    t = make_tender()
    client = StubClient(response(valid_data(), input_tokens=1000, output_tokens=200))
    r = AnthropicExtractor(client).extract(build_notice(t), meter, tender=t)
    assert r.status == "ok" and r.flags == []
    assert r.data.eligibility.supply_arrangement_required.name == "TBIPS"

    (kw,) = client.calls
    assert kw["model"] == "claude-haiku-5-5" and kw["max_tokens"] == 2000
    assert kw["system"] == SYSTEM_PROMPT
    assert kw["messages"] == [{"role": "user", "content": build_notice(t).text}]
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["output_config"]["effort"] == "low"
    assert "temperature" not in kw  # rejected by Haiku 5.5 and absent from SDK 2.x

    (call,) = LlmCall.objects.all()
    assert (call.status, call.input_tokens, call.output_tokens) == ("ok", 1000, 200)
    assert call.cost_usd == Decimal("0.000200")  # 1000*0.10/1M + 200*0.50/1M
    assert call.request_id == "req_test" and call.tender == t
    assert call.prompt_version == PROMPT_VERSION


def test_invalid_output_then_retry_succeeds(make_tender, meter):
    t = make_tender()
    client = StubClient(response(valid_data(work_type="plumbing")), response(valid_data()))
    r = AnthropicExtractor(client).extract(build_notice(t), meter, tender=t)
    assert r.status == "ok" and r.flags == ["validation_retry"]
    retry = client.calls[1]["messages"]
    assert [m["role"] for m in retry] == ["user", "assistant", "user"]
    assert "work_type" in retry[2]["content"] and "plumbing" not in retry[2]["content"]
    assert list(LlmCall.objects.order_by("id").values_list("status", flat=True)) == \
        ["invalid", "ok"]


def test_invalid_twice_fails(make_tender, meter):
    t = make_tender()
    client = StubClient(response("not json"), response("{}", stop_reason="max_tokens"))
    r = AnthropicExtractor(client).extract(build_notice(t), meter, tender=t)
    assert r.status == "failed" and r.flags == ["schema_invalid"]
    assert "max_tokens" in r.error
    assert len(client.calls) == 2 and meter.calls == 2


def test_api_error_fails_notice_and_is_logged(make_tender, meter):
    t = make_tender()
    client = StubClient(api_error(anthropic.InternalServerError, 529))
    r = AnthropicExtractor(client).extract(build_notice(t), meter, tender=t)
    assert r.status == "failed" and r.error == "api_error: InternalServerError (529)"
    assert LlmCall.objects.get().status == "error"


def test_connection_error_fails_notice(make_tender, meter):
    t = make_tender()
    client = StubClient(anthropic.APIConnectionError(request=REQ))
    assert AnthropicExtractor(client).extract(build_notice(t), meter).status == "failed"


def test_auth_error_stops_the_run(make_tender, meter):
    t = make_tender()
    client = StubClient(api_error(anthropic.AuthenticationError, 401))
    with pytest.raises(anthropic.AuthenticationError):
        AnthropicExtractor(client).extract(build_notice(t), meter)


def test_refusal(make_tender, meter):
    t = make_tender()
    client = StubClient(response("", stop_reason="refusal"))
    r = AnthropicExtractor(client).extract(build_notice(t), meter)
    assert r.status == "failed" and r.flags == ["refusal"]


def test_unpriced_model_rejected():
    from django.core.exceptions import ImproperlyConfigured
    with pytest.raises(ImproperlyConfigured):
        AnthropicExtractor(StubClient(), model_id="claude-opus-5-5")


def test_cost_long_prompt_rate():
    assert cost_usd("claude-haiku-5-5", 200_000, 1000) == Decimal("0.1025")


# --- service: cache, post-validation, storage ----------------------------------------------

def test_cache_hit_makes_no_call(make_tender, meter):
    t = make_tender(sa_required_from_metadata=True)
    client = StubClient(response(valid_data()))
    ex = AnthropicExtractor(client)
    first = extract_tender(t, ex, meter)
    assert first.status == "ok" and first.extraction.flags == []
    second = extract_tender(t, ex, meter)  # StubClient would IndexError on a 2nd call
    assert second.status == "cache_hit" and second.extraction.pk == first.extraction.pk
    assert len(client.calls) == 1 and LlmCall.objects.count() == 1


def test_changed_text_is_a_cache_miss(make_tender, meter):
    t = make_tender()
    ex = AnthropicExtractor(StubClient(response(valid_data()), response(valid_data())))
    extract_tender(t, ex, meter)
    t.description += "\nAmendment 1: closing date extended."
    assert extract_tender(t, ex, meter).status == "ok"
    assert Extraction.objects.filter(tender=t).count() == 2


def test_failed_extraction_is_retried_next_run(make_tender, meter):
    t = make_tender()
    ex = AnthropicExtractor(StubClient(api_error(anthropic.InternalServerError, 500),
                                       response(valid_data())))
    assert extract_tender(t, ex, meter).status == "failed"
    o = extract_tender(t, ex, meter)
    assert o.status == "ok" and Extraction.objects.get().status == "ok"


def test_postvalidation_applied_before_storage(make_tender, meter):
    t = make_tender(sa_required_from_metadata=True)
    d = valid_data()
    d["eligibility"]["supply_arrangement_required"] = {
        "value": "no", "name": None, "evidence": "open to all suppliers"}
    extract_tender(t, AnthropicExtractor(StubClient(response(d))), meter)
    x = Extraction.objects.get()
    assert x.data["eligibility"]["supply_arrangement_required"]["value"] == "unknown"
    assert x.flags == ["ungrounded:eligibility.supply_arrangement_required",
                       "sa_metadata_disagrees"]
    assert (x.model_id, x.prompt_version, x.schema_version) == ("claude-haiku-5-5", "v1", "1")


def test_fake_extractor_reads_fixtures(make_tender, meter, tmp_path):
    t = make_tender(reference_number="REF/1")
    (tmp_path / "REF_1.json").write_text(json.dumps(valid_data()))
    (tmp_path / "default.json").write_text("{}")
    o = extract_tender(t, FakeExtractor(tmp_path), meter)
    assert o.status == "ok" and o.extraction.model_id == "fake"
    assert o.extraction.data["work_type"] == "it_services"
    assert LlmCall.objects.count() == 0 and meter.calls == 1
    other = make_tender()
    assert extract_tender(other, FakeExtractor(tmp_path), meter).status == "failed"


# --- caps ------------------------------------------------------------------------------------

def test_per_run_call_cap(make_tender):
    meter = CallMeter(max_calls=1, daily_budget_usd=Decimal("0.50"))
    a, b = make_tender(), make_tender()
    ex = AnthropicExtractor(StubClient(response(valid_data())))
    extract_tender(a, ex, meter)
    with pytest.raises(CapReached, match="per-run"):
        extract_tender(b, ex, meter)


def test_retry_counts_toward_call_cap(make_tender):
    meter = CallMeter(max_calls=1, daily_budget_usd=Decimal("0.50"))
    t = make_tender()
    ex = AnthropicExtractor(StubClient(response("bad"), response(valid_data())))
    with pytest.raises(CapReached):
        extract_tender(t, ex, meter)
    assert LlmCall.objects.get().status == "invalid"


def test_daily_cost_cap_uses_todays_rows(make_tender):
    LlmCall.objects.create(model_id="claude-haiku-5-5", prompt_version="v1", status="ok",
                           cost_usd=Decimal("0.50"))
    meter = CallMeter(max_calls=50, daily_budget_usd=Decimal("0.50"))
    client = StubClient(response(valid_data()))
    with pytest.raises(CapReached, match="daily"):
        extract_tender(make_tender(), AnthropicExtractor(client), meter)
    assert client.calls == []

    LlmCall.objects.update(created_at=timezone.now() - timedelta(days=2))
    assert extract_tender(make_tender(), AnthropicExtractor(client), meter).status == "ok"


def test_command_stops_cleanly_at_cap(make_tender, settings):
    settings.LLM_MAX_CALLS_PER_RUN = 2
    for _ in range(3):
        make_tender()
    out = StringIO()
    call_command("extract_tenders", "--fake", "--status", "open", stdout=out)
    text = out.getvalue()
    assert "stopped: per-run call cap reached (2 calls)" in text
    assert "calls 2  cache hits 0  ok 2  failed 0" in text
    assert "supply=unknown" in text

    out = StringIO()
    call_command("extract_tenders", "--fake", "--limit", "2", stdout=out)
    assert "calls 0  cache hits 2" in out.getvalue()
