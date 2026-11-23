"""Rules v4: the unread-text tripwire (tenders.eligibility.unread), pure and through assess().
Shapes follow the notices of 2026-11-22 (cb-435, cb-181, cb-157, cb-831); all text invented.
"""

import copy

import pytest
from django.utils import timezone

from tenders.eligibility import unread
from tenders.eligibility.rules import Profile, TenderFacts, evaluate
from tenders.eligibility.service import assess, unread_texts
from tenders.eligibility.unread import UnreadText, find_hits
from tenders.extraction.prompt import FULL, NOT_READ, PARTIAL
from tenders.extraction.providers import AnthropicExtractor
from tenders.extraction.service import extract_tender
from tenders.ingest.redact import REDACT_VERSION
from tenders.models import TenderDocument
from tenders.tests.extraction.conftest import StubClient, response, valid_data

PROFILE = Profile("Test IT firm", (), "none", "none")
FACTS = TenderFacts(None, "RFP", "regular")
SEEN = "Part 1 - General Information. The work is described in Annex A. " * 20


def gates(**changes) -> dict:
    """No blocker and no warning: every gate unknown except a supplier-origin note."""
    d = valid_data(criteria_location="partly_stated")
    e = d["eligibility"]
    e["supply_arrangement_required"] = {"value": "unknown", "name": None, "evidence": None}
    e["supplier_origin_restriction"] = {
        "value": "canadian_and_trading_partners",
        "evidence": "This procurement is subject to the Canadian Free Trade Agreement."}
    e["clearance"] = {"corporate": "unknown", "personnel": "unknown", "evidence": None}
    e["work_location"] = {"place": None, "on_site": "unknown", "evidence": None}
    e["documents_behind_login"] = {"value": "unknown", "portal": None, "evidence": None}
    e.update(copy.deepcopy(changes))
    return d


def decide(data, texts):
    return unread.apply(evaluate(data, [], FACTS, PROFILE), find_hits(texts))


# --- the terms ---------------------------------------------------------------------------

def test_each_term_case_insensitive_across_line_breaks():
    text = ("TOP SECRET. secret. Reliability\nStatus. Designated Organisation Screening. "
            "FACILITY SECURITY CLEARANCE. This document contains a security requirement. "
            "There is a security requirement associated with this requirement. "
            "Work on-site, on site, on‑site, sur place.")
    assert [(h.gate, h.term) for h in find_hits([UnreadText("d.pdf", text, 0)])] == [
        ("clearance", "Top Secret"), ("clearance", "Secret"),
        ("clearance", "Reliability Status"), ("clearance", "Designated Organization Screening"),
        ("clearance", "Facility Security Clearance"),
        ("clearance", "contains a security requirement"),
        ("clearance", "security requirements associated"),
        ("work_location", "on-site"), ("work_location", "on site"),
        ("work_location", "on-site"), ("work_location", "sur place")]


@pytest.mark.parametrize("text", [
    "There are no security requirements associated with this requirement.",  # cb-831's amd.
    "There is no security requirement associated with this solicitation.",
    "This document contains no security requirement.",
    "The Secretary of the Treasury Board; secretariat; secrets.",
    "Reliability of the system; onsite; on-sites; placement sur placeur.",
])
def test_not_a_hit(text):
    assert find_hits([UnreadText("d.pdf", text, 0)]) == []


def test_only_text_past_the_start_counts_and_the_page_is_by_position():
    text = "Reliability Status. " + "x" * 9_980 + " Reliability Status near the end."
    hits = find_hits([UnreadText("rfp.pdf", text, start=50, pages=10)])
    assert [(h.term, h.document, h.page) for h in hits] == [
        ("Reliability Status", "rfp.pdf", 10)]
    assert find_hits([UnreadText("rfp.pdf", text, start=50)])[0].page is None


# --- the rule ------------------------------------------------------------------------------

def test_cb435_like_flips_back_to_check():
    """Clearance and work location unknown; the cut RFP says Reliability Status and
    on-site work, and an amendment the model never saw says there is a requirement."""
    data = gates()
    rfp = UnreadText("security-system-services-rfp.pdf",
                     SEEN + "Each technician must hold a valid security clearance at the "
                     "'Reliability Status' level. Services are performed on-site at each office.",
                     start=len(SEEN), pages=10)
    amendment = UnreadText("amendment-005.pdf", "This document contains a security "
                           "requirement.", 0, pages=2)
    assert evaluate(data, [], FACTS, PROFILE).status == "no_blockers_stated"
    d = decide(data, [rfp, amendment])
    assert d.status == "check_solicitation" and d.blockers == []
    added = {w.gate: w for w in d.warnings}
    assert set(added) == {"clearance", "work_location"}
    assert added["clearance"].reason == (
        "Clearance is not stated in what the model read, but text it did not read says "
        "“Reliability Status” (security-system-services-rfp.pdf, about page 10) "
        "(1 more mention(s) unread). Check the solicitation.")
    assert added["clearance"].label == "Unread text: Reliability Status"
    assert added["work_location"].reason.startswith(
        "The work location is not stated in what the model read, but text it did not read "
        "says “on-site” (security-system-services-rfp.pdf, about page 10).")
    assert d.not_stated == evaluate(data, [], FACTS, PROFILE).not_stated


def test_cb181_like_flips_back_to_check():
    """Clearance unknown (the model's quote didn't hold), a land-claims warning, and an
    amendment left out at the cap that changes the security requirements."""
    data = gates(land_claims_applies={
        "value": "yes", "evidence": "This procurement is subject to the Canadian Free Trade "
                                    "Agreement."})
    before = evaluate(data, [], FACTS, PROFILE)
    assert before.status == "no_blockers_stated" and len(before.warnings) == 1
    amd = UnreadText("rfsa-amendment-005.pdf",
                     "This AMENDMENT is raised to change security requirements.\nThere are "
                     "security requirements associated with this requirement.", 0, pages=3)
    d = decide(data, [amd])
    assert d.status == "check_solicitation" and d.blockers == []
    assert [w.gate for w in d.warnings] == ["land_claims_applies", "clearance"]
    assert "“security requirements associated” (rfsa-amendment-005.pdf, about page " \
           "2)" in d.warnings[1].reason


def test_cb157_like_answered_clearance_and_data_security_text_stay_no_blockers():
    """Clearance answered "none" from the document, no on-site wording past the cut: the
    data-security clauses there are not terms, and an answered gate is never re-opened."""
    data = gates(clearance={"corporate": "none", "personnel": "none",
                            "evidence": "This procurement is subject to the Canadian Free "
                                        "Trade Agreement."})
    cut = UnreadText("solicitation-and-contract.pdf",
                     SEEN + "Data must be protected per the CCCS Medium cloud profile. The "
                     "data security requirements of Annex D apply. Data reliability is "
                     "measured monthly. Secret information is not involved.",
                     start=len(SEEN), pages=107)
    d = decide(data, [cut])
    assert d == evaluate(data, [], FACTS, PROFILE)
    assert d.status == "no_blockers_stated"


def test_a_blocked_notice_is_left_alone_and_no_blocker_is_ever_added():
    blocked = gates(supply_arrangement_required={
        "value": "yes", "name": "TBIPS",
        "evidence": "Bidders must hold a valid TBIPS supply arrangement."})
    text = [UnreadText("rfp.pdf", "Top Secret, on-site.", 0)]
    assert decide(blocked, text) == evaluate(blocked, [], FACTS, PROFILE)
    check = gates()
    check["criteria_location"] = "see_solicitation"
    d = decide(check, text)
    assert d.status == "check_solicitation" and d.blockers == []
    assert len(d.warnings) == 2


def test_nothing_unread_changes_nothing():
    data = gates()
    assert decide(data, []) == evaluate(data, [], FACTS, PROFILE)
    assert not unread.needed(evaluate(gates(
        clearance={"corporate": "none", "personnel": "none", "evidence": "x y z"},
        work_location={"place": None, "on_site": "no", "evidence": "x y z"}),
        [], FACTS, PROFILE))


# --- through assess() ------------------------------------------------------------------------

ATT = "https://canadabuys.canada.ca/documents/pub/att/2026/11/01/abc123/"
MAIN, AMEND1 = ATT + "PW-1.EBSU000.PDF", ATT + "PW-1.EBSU001.PDF"


def add_doc(tender, url, text, pages=1):
    return TenderDocument.objects.create(
        tender=tender, url=url, status="ok", text=text, pages=pages, sha256="0" * 64,
        injection_patterns=[], redact_version=REDACT_VERSION, fetched_at=timezone.now())


def with_sources(data):
    d = copy.deepcopy(data)
    for g in d["eligibility"].values():
        g["source"] = "notice"
    return d


def extract(tender, meter, data, version="v5"):
    llm = AnthropicExtractor(StubClient(response(with_sources(data) if version != "v3"
                                                 else data)),
                             prompt_version=version, retry_wait_s=0)
    return extract_tender(tender, llm, meter).extraction


@pytest.mark.django_db
def test_assess_searches_past_the_cut_of_the_main_document(make_tender, meter):
    t = make_tender(attachment_urls=[MAIN, AMEND1])
    add_doc(t, AMEND1, "Amendment 001: the closing date is extended.")
    add_doc(t, MAIN, "word " * 30_000 + "\nResources must hold a valid Reliability Status.",
            pages=60)
    x = extract(t, meter, gates())
    assert [(d["name"], d["status"]) for d in x.documents] == [
        ("PW-1.EBSU001.PDF", FULL), ("PW-1.EBSU000.PDF", PARTIAL)]
    [u] = unread_texts(x)
    assert (u.document, u.start, u.pages) == ("PW-1.EBSU000.PDF", x.documents[1]["chars"], 60)
    a = assess(x)
    assert a.status == "check_solicitation" and a.rules_version == "4"
    assert a.warnings[-1]["gate"] == "clearance"
    assert "“Reliability Status” (PW-1.EBSU000.PDF, about page 60)" in \
        a.warnings[-1]["reason"]
    assert a.blockers == []


@pytest.mark.django_db
def test_assess_ignores_text_the_model_read(make_tender, meter):
    t = make_tender(attachment_urls=[MAIN])
    add_doc(t, MAIN, "Resources must hold a valid Reliability Status. Work is on-site.")
    x = extract(t, meter, gates())
    assert x.documents[0]["status"] == FULL and unread_texts(x) == []
    assert assess(x).status == "no_blockers_stated"


@pytest.mark.django_db
def test_assess_of_a_notice_only_extraction_searches_every_ok_document(make_tender, meter):
    """A v3 extraction whose tender has documents (fetched after it): none were read."""
    t = make_tender(attachment_urls=[MAIN])
    x = extract(t, meter, gates(), version="v3")
    assert assess(x).status == "no_blockers_stated"
    add_doc(t, MAIN, "The work will be performed on site in Ottawa.", pages=4)
    [u] = unread_texts(x)
    assert (u.document, u.start) == ("PW-1.EBSU000.PDF", 0)
    a = assess(x)
    assert a.status == "check_solicitation"
    assert a.warnings[-1]["gate"] == "work_location" and a.warnings[-1]["label"] == \
        "Unread text: on site"


@pytest.mark.django_db
def test_assess_searches_a_document_left_out_at_the_cap(make_tender, meter):
    t = make_tender(attachment_urls=[MAIN, AMEND1, ATT + "PW-1.EBSU002.PDF"])
    add_doc(t, MAIN, "word " * 10_000)
    add_doc(t, AMEND1, "word " * 14_000)
    add_doc(t, ATT + "PW-1.EBSU002.PDF", "Top Secret. " + "word " * 20_000)
    x = extract(t, meter, gates())
    status = {d["name"]: d["status"] for d in x.documents}
    assert (status["PW-1.EBSU002.PDF"], status["PW-1.EBSU000.PDF"]) == (NOT_READ, PARTIAL)
    a = assess(x)
    assert a.status == "check_solicitation"
    assert "PW-1.EBSU002.PDF" in a.warnings[-1]["reason"]
