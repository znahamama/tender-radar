from io import StringIO

import pytest
from django.core.management import call_command

from tenders.admin import AssessmentAdmin
from tenders.eligibility import rules
from tenders.eligibility.service import (
    assess,
    default_profile,
    latest_extractions,
    reassess,
)
from tenders.extraction import validate
from tenders.extraction.keyword import KeywordExtractor
from tenders.extraction.providers import AnthropicExtractor
from tenders.extraction.service import extract_tender
from tenders.models import Assessment, Extraction, LlmCall, SupplierProfile
from tenders.tests.extraction.conftest import StubClient, response, valid_data

pytestmark = pytest.mark.django_db


def llm(*items):
    return AnthropicExtractor(StubClient(*items), retry_wait_s=0)


def test_seeded_profile():
    p = default_profile()
    assert p.name == "Harbourline Software (fictional)"
    assert p.capabilities == ["custom software development", "web and mobile apps",
                              "data engineering", "IT consulting", "cloud migration"]
    assert (p.supply_arrangements, p.corporate_clearance, p.personnel_clearance) == ([], "none",
                                                                                        "none")
    assert p.work_locations == ["St. John's, NL", "remote"]


def test_assess_stores_decision(make_tender, meter):
    t = make_tender()  # VALID: TBIPS required, DOS (corporate reliability), on-site St. John's
    x = extract_tender(t, llm(response(valid_data())), meter).extraction
    a = assess(x)
    assert a.status == "blocked" and a.tender == t and a.profile == default_profile()
    assert {b["gate"] for b in a.blockers} == {"supply_arrangement_required", "clearance"}
    assert a.notes[0]["reason"] == "On-site work at St. John's (St. John's area)."
    assert a.rules_version == rules.RULES_VERSION and len(a.inputs_hash) == 64


def test_assess_is_idempotent(make_tender, meter):
    x = extract_tender(make_tender(), llm(response(valid_data())), meter).extraction
    first = assess(x)
    again = assess(x)
    assert again.pk == first.pk and again.computed_at == first.computed_at
    assert Assessment.objects.count() == 1


def test_profile_change_recomputes(make_tender, meter):
    x = extract_tender(make_tender(), llm(response(valid_data())), meter).extraction
    assert assess(x).status == "blocked"
    p = default_profile()
    p.supply_arrangements = ["TBIPS"]
    p.corporate_clearance = "reliability"
    p.save()
    (a,) = reassess(profile=p)
    assert a.blockers == []
    assert a.status == "check_solicitation"  # VALID leaves personnel clearance unknown


def test_validation_rule_change_recomputes(make_tender, meter, monkeypatch):
    x = extract_tender(make_tender(), llm(response(valid_data())), meter).extraction
    assert assess(x).status == "blocked"
    monkeypatch.setattr(validate, "MIN_EVIDENCE_WORDS", 50)  # every quote now ungrounded
    a = assess(x)
    assert a.status == "check_solicitation" and a.blockers == []
    assert LlmCall.objects.count() == 1  # no new API call


def test_metadata_precedence_with_llm_no(make_tender, meter):
    t = make_tender(sa_required_from_metadata=True, notice_type="RFP against Supply Arrangement")
    d = valid_data()
    d["eligibility"]["supply_arrangement_required"] = {"value": "no", "name": None,
                                                       "evidence": "RFP against Supply Arrangement"}
    x = extract_tender(t, llm(response(d)), meter).extraction
    assert "sa_metadata_disagrees" in x.flags  # the LLM answer stays visible for the eval
    assert x.data["eligibility"]["supply_arrangement_required"]["value"] == "no"
    a = assess(x)
    assert a.status == "blocked"
    assert any(b["gate"] == "supply_arrangement_required" for b in a.blockers)


def test_failed_extraction_is_assessed(make_tender, meter):
    import anthropic
    import httpx2
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    meter.api_calls = 1
    err = anthropic.UnprocessableEntityError("x", response=httpx2.Response(422, request=req),
                                             body=None)
    x = extract_tender(make_tender(), llm(err), meter).extraction
    a = assess(x)
    assert x.status == "failed" and a.status == "check_solicitation"
    assert a.warnings[0]["reason"].startswith("Extraction failed")


def test_latest_extraction_per_tender_and_model(make_tender, meter):
    t = make_tender()
    ex = llm(response(valid_data()), response(valid_data()))
    old = extract_tender(t, ex, meter).extraction
    t.description += " Amended."
    t.save()
    new = extract_tender(t, ex, meter).extraction
    base = extract_tender(t, KeywordExtractor(), meter).extraction
    latest = latest_extractions()
    assert {x.pk for x in latest} == {new.pk, base.pk} and old.pk not in {x.pk for x in latest}
    assert [x.pk for x in latest_extractions(model_ids=["keyword-baseline"])] == [base.pk]


def test_assess_tenders_command(make_tender, meter):
    t = make_tender()
    extract_tender(t, llm(response(valid_data())), meter)
    extract_tender(t, KeywordExtractor(), meter)
    out = StringIO()
    call_command("assess_tenders", "--status", "open", stdout=out)
    text = out.getvalue()
    assert "claude-haiku-5-5: blocked 1" in text and "assessed 2" in text
    assert Assessment.objects.count() == 2


def test_extract_command_assesses_each_notice(make_tender):
    make_tender()
    out = StringIO()
    call_command("extract_tenders", "--baseline", stdout=out)
    text = out.getvalue()
    assert "TEST-1  ok  blocked" in text  # baseline sees TBIPS and the DOS
    assert "assessments blocked 1" in text and "calls 0" in text
    x = Extraction.objects.get()
    assert x.model_id == "keyword-baseline" and x.prompt_version == "rules-v1"
    assert LlmCall.objects.count() == 0


def test_assessment_admin_is_read_only(rf):
    admin = AssessmentAdmin(Assessment, None)
    req = rf.get("/")
    assert not admin.has_add_permission(req)
    assert not admin.has_change_permission(req)
    assert not admin.has_delete_permission(req)
    assert "status" in admin.list_filter


def test_profile_admin_save_reassesses(make_tender, meter, rf):
    from tenders.admin import SupplierProfileAdmin

    x = extract_tender(make_tender(), llm(response(valid_data())), meter).extraction
    assert assess(x).status == "blocked"
    p = default_profile()
    p.supply_arrangements, p.corporate_clearance = ["TBIPS"], "reliability"
    SupplierProfileAdmin(SupplierProfile, None).save_model(rf.post("/"), p, None, True)
    assert Assessment.objects.get().blockers == []
