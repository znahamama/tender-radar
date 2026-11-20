"""Scope rules v1: in scope for an IT firm, from category codes only (tenders.scope)."""

import pytest

from tenders.ingest.filters import is_it
from tenders.scope import IN, OUT, UNKNOWN, classify
from tenders.tests.extraction.conftest import (  # noqa: F401  (fixtures; no real API)
    no_real_api,
    pin_llm_settings,
)


@pytest.mark.parametrize(("unspsc", "gsin", "status"), [
    # IT families, as the CSV writes codes (leading "*", stray spaces).
    (["*43230000"], [], IN),  # software
    (["43211500"], [], IN),  # computers
    (["43222500"], [], IN),  # network security equipment
    (["81111508"], [], IN),  # application implementation services
    (["81112200"], [], IN),  # software maintenance and support
    (["81162000"], [], IN),  # cloud-based software as a service
    (["81161700"], [], IN),  # telecommunication services
    (["83112200"], [], IN),  # enhanced telecommunications services
    (["45111902"], [], IN),  # videoconferencing systems
    ([], [" *D302A"], IN),  # informatics professional services
    ([], ["N7030"], IN),  # ADP software
    ([], ["JX7025EA"], IN),  # LAN maintenance
    ([], ["WL7025AJ"], IN),  # printer lease
    ([], ["D304A"], IN),  # data transmission
    ([], ["d399bb"], IN),  # satellite telecom (case-insensitive)
    # Exclusions inside an IT family, and families left out on purpose.
    (["81162309"], [], OUT),  # payroll process as a service
    (["83111800"], [], OUT),  # television services
    (["83111900"], [], OUT),  # radio services
    (["46171610"], [], OUT),  # security cameras
    (["92121700"], [], OUT),  # security systems services
    (["81151605"], [], OUT),  # satellite imaging
    (["55111500"], [], OUT),  # electronic publications
    (["83120000"], [], OUT),  # information services
    ([], ["D309A"], OUT),  # radio and TV towers
    ([], ["D317E"], OUT),  # information products
    ([], ["N5820D"], OUT),  # CCTV camera
    (["72151100"], ["N2310"], OUT),  # plumbing, vehicles
    # No codes at all.
    ([], [], UNKNOWN),
    (["", " "], ["*"], UNKNOWN),
])
def test_classify(unspsc, gsin, status):
    assert classify(unspsc, gsin).status == status


def test_one_it_code_among_others_is_enough_and_is_named():
    s = classify(["10000000", "43230000", "81162309"], ["N7110"])
    assert s.status == IN
    assert s.matches == (("unspsc", "43230000", "IT and telecom equipment and software"),)


def test_scope_v1_against_the_ingest_it_filter():
    """Everything the ingest filter calls IT is in scope except 811119; telecom is added."""
    for unspsc, gsin in ((["43230000"], []), (["81110000"], []), ([], ["D302A"]),
                         ([], ["N7030"]), ([], ["JX7030"]), ([], ["WL7025AJ"])):
        assert is_it(unspsc, gsin) and classify(unspsc, gsin).status == IN
    assert is_it(["81111902"], []) and classify(["81111902"], []).status == OUT
    assert not is_it(["83112200"], ["D316B"])
    assert classify(["83112200"], ["D316B"]).status == IN


@pytest.mark.parametrize("code", ["81111900", "81111901", "81111902"])
def test_information_retrieval_is_excluded(code):
    """Data purchases (imagery, SSA data, subscriptions) on the 2026-11-19 data."""
    assert classify([code]).status == OUT
    assert classify([code, "81111508"]).status == IN  # other computer services still count


def test_pages_use_the_same_in_value():
    from tenders.pages import IN_SCOPE

    assert IN_SCOPE == IN


# --- ingest: code titles, outside the content hash --------------------------------------

def _notice(**cells):
    from tenders.ingest.schema import TenderNotice

    return TenderNotice.model_validate({
        "referenceNumber-numeroReference": "PW-1", "title-titre-eng": "A notice",
        "unspsc": "*43230000\n*72151100", "unspscDescription-eng": "*Software\n*Plumbing",
        "gsin-nibs": "*n7030", "gsinDescription-nibsDescription-eng": "*ADP Software",
        **cells})


def test_code_titles_are_parsed_but_not_tender_fields_or_hashed():
    from tenders.ingest.storage import content_hash, tender_fields

    n = _notice()
    assert (n.unspsc_descriptions, n.gsin_descriptions) == (["Software", "Plumbing"],
                                                            ["ADP Software"])
    f = tender_fields(n)
    assert "unspsc_descriptions" not in f and "gsin_descriptions" not in f
    bare = _notice(**{"unspscDescription-eng": "", "gsinDescription-nibsDescription-eng": ""})
    assert content_hash(tender_fields(bare)) == content_hash(f)  # no "updated" churn


@pytest.mark.django_db
def test_code_titles_are_upserted_and_misaligned_rows_skipped():
    from tenders.ingest.storage import save_code_descriptions
    from tenders.models import CategoryCode

    misaligned = _notice(**{"referenceNumber-numeroReference": "PW-2",
                            "unspsc": "*81110000", "unspscDescription-eng": "*A\n*B"})
    assert save_code_descriptions([_notice(), misaligned]) == 3
    titles = dict(CategoryCode.objects.values_list("code", "description"))
    assert titles == {"43230000": "Software", "72151100": "Plumbing", "N7030": "ADP Software"}
    save_code_descriptions([_notice(**{"unspscDescription-eng": "*Software (new)\n*Plumbing"})])
    assert CategoryCode.objects.get(code="43230000").description == "Software (new)"


# --- assess: scope stored beside the status, never part of it ---------------------------

@pytest.mark.django_db
def test_assess_stores_scope_without_touching_the_status():
    from decimal import Decimal

    from tenders.eligibility.service import assess
    from tenders.extraction.budget import CallMeter
    from tenders.extraction.providers import AnthropicExtractor
    from tenders.extraction.service import extract_tender
    from tenders.models import Tender
    from tenders.tests.extraction.conftest import (
        StubClient,
        response,
        tender_fields,
        valid_data,
    )

    t = Tender.objects.create(**tender_fields(reference_number="IT-1",
                                              unspsc_codes=["43230000"]))
    x = extract_tender(t, AnthropicExtractor(StubClient(response(valid_data())),
                                             retry_wait_s=0),
                       CallMeter(max_calls=5, daily_budget_usd=Decimal(1))).extraction
    a = assess(x)
    assert a.scope == {"version": "1", "status": "in", "matches": [
        {"scheme": "unspsc", "code": "43230000",
         "family": "IT and telecom equipment and software"}]}
    before = (a.status, a.inputs_hash, a.computed_at)
    Tender.objects.filter(pk=t.pk).update(unspsc_codes=["72151100"])
    x.tender.refresh_from_db()
    b = assess(x)
    assert b.scope["status"] == "out" and b.scope["matches"] == []
    assert (b.status, b.inputs_hash, b.computed_at) == before
