"""Per-gate combine of a v4 extraction (notice and documents) with the tender's v3 one
(notice only): the pure rules, then through assess(), the detail page, the review queue
and the review page. No API calls (stub client). All notice and document text is invented.
"""

import copy

import pytest
from django.utils import timezone

from tenders.admin import NeedsReviewAdmin, queue_flags, review_queue
from tenders.eligibility.combine import DOCS, combine
from tenders.eligibility.service import assess, default_profile
from tenders.extraction.providers import AnthropicExtractor
from tenders.extraction.service import extract_tender
from tenders.ingest.redact import REDACT_VERSION
from tenders.models import Assessment, ReviewDecision, TenderDocument
from tenders.tests.extraction.conftest import StubClient, response, valid_data

pytestmark = pytest.mark.django_db

ATT = "https://canadabuys.canada.ca/documents/pub/att/2026/11/01/abc123/"
MAIN = ATT + "PW-1.EBSU000.PDF"
MAIN_NAME = "PW-1.EBSU000.PDF"
MAIN_TEXT = """Part 1 - General Information
The Contractor must hold a valid Facility Security Clearance at the level of SECRET.
Bidders from Canada and from trade agreement partner countries may submit a bid."""
DOS = "valid Designated Organization Screening (DOS)"  # in the test notice (conftest)
FSC = "must hold a valid Facility Security Clearance at the level of SECRET"  # in MAIN
CFTA = "This procurement is subject to the Canadian Free Trade Agreement."  # in the notice
PARTNERS = "Bidders from Canada and from trade agreement partner countries may submit a bid."


def neutral(**gates) -> dict:
    """No blocker anywhere (so the queue priority comes from flags), plus ``gates``."""
    d = valid_data(criteria_location="partly_stated")
    e = d["eligibility"]
    e["supply_arrangement_required"] = {"value": "unknown", "name": None, "evidence": None}
    e["clearance"] = {"corporate": "unknown", "personnel": "unknown", "evidence": None}
    e["work_location"] = {"place": None, "on_site": "unknown", "evidence": None}
    e.update(copy.deepcopy(gates))
    return d


def as_v4(data: dict, sources: dict | None = None) -> dict:
    """Schema-2 output: a source on each gate ("notice" unless given)."""
    d = copy.deepcopy(data)
    for gate, g in d["eligibility"].items():
        g["source"] = (sources or {}).get(gate, "notice")
    return d


def stub(*items, version="v3"):
    return AnthropicExtractor(StubClient(*items), prompt_version=version, retry_wait_s=0)


def v3_then_v4(make_tender, meter, v3_data, v4_data):
    """A v3 extraction of the notice alone, then documents arrive and v4 reads them."""
    t = make_tender(attachment_urls=[MAIN])
    v3 = extract_tender(t, stub(response(v3_data)), meter).extraction
    TenderDocument.objects.create(
        tender=t, url=MAIN, status="ok", text=MAIN_TEXT, pages=1, sha256="0" * 64,
        injection_patterns=[], redact_version=REDACT_VERSION, fetched_at=timezone.now())
    v4 = extract_tender(t, stub(response(v4_data), version="v4"), meter).extraction
    assert (v3.prompt_version, v4.prompt_version) == ("v3", "v4")
    return t, v3, v4


# --- the pure rules ------------------------------------------------------------------------

def _gate(value, evidence, source=None, **extra):
    g = {"value": value, "evidence": evidence, **extra}
    return g if source is None else {**g, "source": source}


def _data(**gates):
    return {"criteria_location": "partly_stated", "eligibility": {
        "land_claims_applies": _gate("unknown", None, "notice"), **gates}}


def test_v4_grounded_is_used():
    c = combine(_data(land_claims_applies=_gate("yes", "q4", MAIN_NAME)),
                _data(land_claims_applies=_gate("yes", "q3")))
    assert c.data["eligibility"]["land_claims_applies"] == _gate("yes", "q4", MAIN_NAME)
    assert c.versions["land_claims_applies"] == DOCS and c.flags == []


def test_v4_not_grounded_v3_grounded_uses_v3_from_the_notice():
    c = combine(_data(), _data(land_claims_applies=_gate("no", "q3")))
    assert c.data["eligibility"]["land_claims_applies"] == _gate("no", "q3", "notice")
    assert c.versions["land_claims_applies"] == "v3"
    assert c.flags == ["from_v3:eligibility.land_claims_applies"]


def test_both_grounded_and_different_keeps_v4_and_flags():
    c = combine(_data(land_claims_applies=_gate("yes", "q4", MAIN_NAME)),
                _data(land_claims_applies=_gate("no", "q3")))
    assert c.data["eligibility"]["land_claims_applies"]["value"] == "yes"
    assert c.versions["land_claims_applies"] == DOCS
    assert c.flags == ["versions_disagree:eligibility.land_claims_applies"]


def test_same_answer_from_different_quotes_or_details_is_no_disagreement():
    """Only the values a status depends on count: not the quote, source or a place."""
    v4 = {"place": "Halifax data centre", "on_site": "yes", "evidence": "q4", "source": "n"}
    v3 = {"place": "Halifax", "on_site": "yes", "evidence": "q3"}
    c = combine(_data(work_location=v4), _data(work_location=v3))
    assert c.flags == [] and c.data["eligibility"]["work_location"] == v4


def test_neither_grounded_stays_v4():
    c = combine(_data(), _data(land_claims_applies=_gate("unknown", None)))
    assert c.data == _data() and c.versions["land_claims_applies"] == DOCS and c.flags == []


def test_nothing_to_combine_without_both():
    assert combine(_data(), None).versions == {} and combine(None, _data()).data is None
    assert combine(_data(), None).record(None) == {}


def test_combine_never_changes_its_inputs():
    v4, v3 = _data(), _data(land_claims_applies=_gate("no", "q3"))
    before = copy.deepcopy((v4, v3))
    combine(v4, v3)
    assert (v4, v3) == before


# --- through assess() ----------------------------------------------------------------------

def test_assess_uses_v3_where_v4_has_no_quote_that_held(make_tender, meter):
    """v4's clearance quote is in no source (dropped); v3's DOS quote holds: v3's answer
    decides, so the corporate Reliability requirement blocks as it did under v3."""
    v3_data = neutral(clearance={"corporate": "reliability", "personnel": "unknown",
                                 "evidence": DOS})
    v4_data = as_v4(neutral(clearance={"corporate": "secret", "personnel": "unknown",
                                       "evidence": "an invented quote in no source"}))
    _, v3, v4 = v3_then_v4(make_tender, meter, v3_data, v4_data)
    assert "ungrounded:eligibility.clearance" in v4.flags
    a = assess(v4)
    assert a.status == "blocked" and [b["gate"] for b in a.blockers] == ["clearance"]
    assert a.blockers[0]["evidence"] == DOS
    assert a.combined["v3_extraction"] == v3.pk
    assert a.combined["gates"]["clearance"] == "v3"
    assert a.combined["flags"] == ["from_v3:eligibility.clearance"]
    assert {g for g, v in a.combined["gates"].items() if v == "v4"} == {
        "supply_arrangement_required", "supplier_origin_restriction", "work_location",
        "land_claims_applies", "documents_behind_login"}


def test_assess_keeps_v4_when_both_hold_and_disagree(make_tender, meter):
    v3_data = neutral(clearance={"corporate": "reliability", "personnel": "unknown",
                                 "evidence": DOS})
    v4_data = as_v4(neutral(clearance={"corporate": "secret", "personnel": "unknown",
                                       "evidence": FSC}), {"clearance": MAIN_NAME})
    _, _, v4 = v3_then_v4(make_tender, meter, v3_data, v4_data)
    a = assess(v4)
    assert a.blockers[0]["evidence"] == FSC  # v4's answer decides the status
    assert a.combined["gates"]["clearance"] == "v4"
    assert a.combined["flags"] == ["versions_disagree:eligibility.clearance"]


def test_review_decisions_still_override_either_version(make_tender, meter, django_user_model):
    v3_data = neutral(clearance={"corporate": "reliability", "personnel": "unknown",
                                 "evidence": DOS})
    v4_data = as_v4(neutral(clearance={"corporate": "unknown", "personnel": "unknown",
                                       "evidence": None}))
    _, _, v4 = v3_then_v4(make_tender, meter, v3_data, v4_data)
    assert assess(v4).status == "blocked"  # v3's Reliability, filled in
    reviewer = django_user_model.objects.create_user("r", is_staff=True)
    ReviewDecision.objects.create(extraction=v4, gate="clearance", action="reject",
                                  reviewer=reviewer)
    a = assess(v4)
    assert a.status == "no_blockers_stated" and "clearance" in a.not_stated
    ReviewDecision.objects.create(
        extraction=v4, gate="clearance", action="correct", reviewer=reviewer,
        corrected_value={"corporate": "secret", "personnel": "none"},
        evidence="The Contractor must hold a valid Designated Organization Screening")
    assert assess(v4).blockers[0]["gate"] == "clearance"


def test_a_v3_only_tender_is_not_combined_and_keeps_its_hash(make_tender, meter):
    t = make_tender()
    v3 = extract_tender(t, stub(response(valid_data())), meter).extraction
    a = assess(v3)
    assert a.combined == {}
    before = a.inputs_hash
    assert assess(v3).inputs_hash == before


def test_a_changed_v3_extraction_reassesses_the_v4_one(make_tender, meter):
    v3_data = neutral(clearance={"corporate": "reliability", "personnel": "unknown",
                                 "evidence": DOS})
    _, v3, v4 = v3_then_v4(make_tender, meter, v3_data, as_v4(neutral()))
    first = assess(v4).inputs_hash
    v3.raw_output["eligibility"]["clearance"]["corporate"] = "secret"
    v3.save(update_fields=["raw_output"])
    a = assess(v4)  # assess refreshes the v3 extraction from its raw output
    assert a.inputs_hash != first and "Secret" in a.blockers[0]["label"]


# --- detail page, review queue, review page ------------------------------------------------

def _page(client, t) -> str:
    r = client.get(f"/tenders/{t.reference_number}/")
    assert r.status_code == 200
    return r.content.decode()


def test_detail_page_shows_which_extraction_each_gate_came_from(make_tender, meter, client):
    v3_data = neutral(clearance={"corporate": "reliability", "personnel": "unknown",
                                 "evidence": DOS},
                      supplier_origin_restriction={"value": "canadian_only",
                                                   "evidence": CFTA})
    v4_data = as_v4(neutral(
        clearance={"corporate": "secret", "personnel": "unknown", "evidence": "invented x y"},
        supplier_origin_restriction={"value": "canadian_and_trading_partners",
                                     "evidence": PARTNERS}),
        {"supplier_origin_restriction": MAIN_NAME})
    t, _, v4 = v3_then_v4(make_tender, meter, v3_data, v4_data)
    assess(v4)
    html = _page(client, t)
    assert '<th scope="col">From</th>' in html
    assert html.count('<td data-label="From">v3 (notice only)</td>') == 1
    assert html.count('<td data-label="From">v4 (notice and documents)</td>') == 5
    assert "v4: quote not found in its source: treated as unknown" in html
    assert ("v3 (notice only) said Canadian only: versions disagree, in the review queue"
            in html)
    assert "1 gate from the prompt v3 extraction of the notice alone" in html
    assert DOS in html  # v3's quote, shown as from the notice (no document line on it)
    assert html.count("From the solicitation documents:") >= 1  # v4's origin quote


def test_detail_page_without_a_combine_has_no_from_column(make_tender, meter, client):
    t = make_tender()
    assess(extract_tender(t, stub(response(valid_data())), meter).extraction)
    html = _page(client, t)
    assert 'data-label="From"' not in html and "prompt v3 extraction of" not in html


def test_source_corrected_flag_is_shown_in_words(make_tender, meter, client):
    """v4 names the notice for a quote that is in the document: kept, source corrected."""
    v4_data = as_v4(neutral(supplier_origin_restriction={
        "value": "canadian_and_trading_partners", "evidence": PARTNERS}))
    t, _, v4 = v3_then_v4(make_tender, meter, neutral(), v4_data)
    assert "source_corrected:eligibility.supplier_origin_restriction" in v4.flags
    assert v4.data["eligibility"]["supplier_origin_restriction"]["source"] == MAIN_NAME
    assess(v4)
    html = _page(client, t)
    assert "quote found in a different source than the model named: source corrected" in html
    assert f"From the solicitation documents: <a href=\"{MAIN}\"" in html


def test_versions_disagree_puts_the_notice_in_the_review_queue(make_tender, meter):
    v3_data = neutral(supplier_origin_restriction={"value": "canadian_only",
                                                   "evidence": CFTA})
    v4_data = as_v4(neutral(supplier_origin_restriction={
        "value": "canadian_and_trading_partners", "evidence": PARTNERS}),
        {"supplier_origin_restriction": MAIN_NAME})
    t, _, v4 = v3_then_v4(make_tender, meter, v3_data, v4_data)
    calm = make_tender()  # no flag at all: below the flagged notice
    assess(extract_tender(calm, stub(response(neutral())), meter).extraction)
    a = assess(v4)
    assert a.status != Assessment.Status.BLOCKED
    rows = {r.pk: r for r in review_queue()}
    assert (rows[t.pk].review_priority, rows[calm.pk].review_priority) == (1, 2)
    admin = NeedsReviewAdmin(type(rows[t.pk]), None)
    assert admin.flags(rows[t.pk]) == "versions_disagree (supplier_origin_restriction)"
    assert queue_flags([], [], None) == []


def test_review_page_says_when_the_site_shows_the_v3_answer(make_tender, meter):
    v3_data = neutral(clearance={"corporate": "reliability", "personnel": "unknown",
                                 "evidence": DOS},
                      supplier_origin_restriction={"value": "canadian_only",
                                                   "evidence": CFTA})
    v4_data = as_v4(neutral(supplier_origin_restriction={
        "value": "canadian_and_trading_partners", "evidence": PARTNERS}),
        {"supplier_origin_restriction": MAIN_NAME})
    _, _, v4 = v3_then_v4(make_tender, meter, v3_data, v4_data)
    assess(v4, default_profile())
    rows = {r["gate"]: r for r in NeedsReviewAdmin._gate_context(v4, {})}
    assert ("the site shows the v3 answer (notice only): corporate: Reliability; "
            "personnel: unknown") in rows["clearance"]["flags"]
    assert ("v3 (notice only) said Canadian only: versions disagree"
            in rows["supplier_origin_restriction"]["flags"])


@pytest.mark.parametrize(("action", "said"), [
    ("accept", "a person reviewed it and accepted the answer from the documents"),
    ("correct", "a person reviewed it and corrected the answer"),
    ("reject", "a person reviewed it and marked the answer wrong"),
])
def test_a_decided_disagreement_no_longer_says_in_the_review_queue(
        make_tender, meter, client, django_user_model, action, said):
    """Once a decision on the flagged gate is in effect, the page says what it was."""
    v3_data = neutral(supplier_origin_restriction={"value": "canadian_only",
                                                   "evidence": CFTA})
    v4_data = as_v4(neutral(supplier_origin_restriction={
        "value": "canadian_and_trading_partners", "evidence": PARTNERS}),
        {"supplier_origin_restriction": MAIN_NAME})
    t, _, v4 = v3_then_v4(make_tender, meter, v3_data, v4_data)
    assess(v4)
    queued = "v3 (notice only) said Canadian only: versions disagree, in the review queue"
    assert queued in _page(client, t)
    reviewer = django_user_model.objects.create_user("r", is_staff=True)
    extra = ({"corrected_value": {"value": "canadian_only"}, "evidence": CFTA}
             if action == "correct" else {})
    ReviewDecision.objects.create(extraction=v4, gate="supplier_origin_restriction",
                                  action=action, reviewer=reviewer, **extra)
    assess(v4)
    html = _page(client, t)
    assert "in the review queue" not in html
    assert f"v3 (notice only) said Canadian only: versions disagreed; {said}" in html
    # The note is about the decision, not the model's answer: never prefixed.
    assert "model&#x27;s answer: v3 (notice only)" not in html
