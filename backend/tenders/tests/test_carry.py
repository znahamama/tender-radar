"""Review decisions across re-extractions of the same notice (tenders.review.carry_forward)."""
# ruff: noqa: F811  (pytest fixtures imported from other test modules)

import pytest
from django.urls import reverse

from tenders import review
from tenders.eligibility.service import assess
from tenders.extraction.providers import AnthropicExtractor
from tenders.extraction.service import extract_tender
from tenders.models import AppendOnlyError, CarriedReview, ReviewDecision, Tender
from tenders.tests.extraction.conftest import (  # noqa: F401  (fixtures)
    DESCRIPTION,
    StubClient,
    make_tender,
    meter,
    no_real_api,
    pin_llm_settings,
    response,
    valid_data,
)
from tenders.tests.test_review import (  # noqa: F401  (fixtures and helpers)
    NO_SA,
    QUEUE,
    correct,
    decide,
    extraction,
    page,
    reviewer,
    sa_only_blocker,
    status,
)

pytestmark = pytest.mark.django_db

Reason = CarriedReview.Reason
AMENDED = f"{DESCRIPTION}\n{NO_SA}\nAmendment 1: the closing date is extended by one week."
QUOTE_GONE = f"{DESCRIPTION}\nAmendment 2: the requirement is unchanged otherwise."


@pytest.fixture
def reextract(meter):
    """A newer extraction of the same notice: new text, or (no description) the same text
    under a different extractor config, so the input hash stays the same."""
    def make(x, data=None, *, description=None):
        t = x.tender
        if description is not None:
            Tender.objects.filter(pk=t.pk).update(description=description)
            t.refresh_from_db()
        llm = AnthropicExtractor(StubClient(response(data or sa_only_blocker())),
                                 retry_wait_s=0, max_tokens=2100 if description is None
                                 else None)
        new = extract_tender(t, llm, meter).extraction
        assert new.pk != x.pk
        assert (new.input_hash == x.input_hash) is (description is None)
        assess(new)
        return new
    return make


def carried(x, gate):
    return review.reviews_for(x).carried.get(gate)


def detail(client, x):
    return client.get(reverse("tender-detail", args=[x.tender.reference_number]))


def test_same_input_carries_every_decision(client, reviewer, extraction, reextract):
    x = extraction()
    client.force_login(reviewer)
    correct(client, x, "supply_arrangement_required", value="no", evidence=NO_SA)
    decide(client, x, "clearance", "accept")
    decide(client, x, "documents_behind_login", "reject")
    assert status(x) == "no_blockers_stated"
    originals = {d.gate: d for d in ReviewDecision.objects.all()}

    new = reextract(x)  # same text, the model again says TBIPS is required
    assert status(new) == "no_blockers_stated"  # recomputed with the carried correction
    applied = review.reviews_for(new).applied
    assert applied == originals  # the person's own decisions, not copies
    assert {d.reviewer for d in applied.values()} == {reviewer}
    assert ReviewDecision.objects.count() == 3
    rows = CarriedReview.objects.filter(extraction=new)
    assert {(c.gate, c.applied, c.reason) for c in rows} == {
        (g, True, Reason.SAME_INPUT) for g in originals}
    assert "Reviewed by a person" in detail(client, new).content.decode()


def test_changed_text_with_the_quote_still_there_carries(client, reviewer, extraction,
                                                         reextract):
    x = extraction()
    client.force_login(reviewer)
    correct(client, x, "supply_arrangement_required", value="no", evidence=NO_SA)
    new = reextract(x, description=AMENDED)
    c = carried(new, "supply_arrangement_required")
    assert (c.applied, c.reason, c.source.extraction_id) == (True, Reason.QUOTE_FOUND, x.pk)
    assert status(new) == "no_blockers_stated"


def test_quote_gone_needs_rereview(client, reviewer, extraction, reextract):
    x = extraction()
    client.force_login(reviewer)
    correct(client, x, "supply_arrangement_required", value="no", evidence=NO_SA)
    new = reextract(x, description=QUOTE_GONE)
    c = carried(new, "supply_arrangement_required")
    assert (c.applied, c.reason) == (False, Reason.QUOTE_GONE)
    assert review.reviews_for(new).needs_rereview == ["supply_arrangement_required"]
    # Not applied: the model's "yes" counts again, and the status is recomputed.
    assert status(new) == "blocked"
    assert "Reviewed by a person" not in detail(client, new).content.decode()

    # The queue points at the earlier review; the review page says what to re-check.
    r = client.get(QUEUE)
    row = next(t for t in r.context["cl"].result_list if t.pk == x.tender_id)
    cell = r.context["cl"].model_admin.reviewed(row)
    assert "1 needs re-review (notice text changed)" in cell
    assert "reviewed on an earlier version" in cell and page(x) in cell
    html = client.get(page(new)).content.decode()
    assert "Needs re-review (notice text changed)" in html
    assert f'href="{page(x)}#gate-supply_arrangement_required"' in html


def test_reject_carries_only_if_the_model_answer_is_the_same(client, reviewer, extraction,
                                                            reextract):
    # valid_data: corporate Reliability (DOS) blocks; the person rejects that answer.
    x = extraction(valid_data())
    client.force_login(reviewer)
    decide(client, x, "clearance", "reject")
    same = reextract(x, valid_data(), description=AMENDED)
    c = carried(same, "clearance")
    assert (c.applied, c.reason) == (True, Reason.SAME_ANSWER)
    assert "clearance" not in {b["gate"] for b in
                               same.assessments.get().blockers}  # still treated as unknown

    changed = valid_data()
    changed["eligibility"]["clearance"] = {
        "corporate": "secret", "personnel": "unknown",
        "evidence": "valid Designated Organization Screening (DOS)"}
    other = reextract(same, changed, description=QUOTE_GONE)
    c = carried(other, "clearance")
    assert (c.applied, c.reason, c.source.extraction_id) == (False, Reason.ANSWER_CHANGED, x.pk)
    assert "clearance" in {b["gate"] for b in other.assessments.get().blockers}


def test_accept_follows_the_same_answer_rule(client, reviewer, extraction, reextract):
    x = extraction()
    client.force_login(reviewer)
    decide(client, x, "supply_arrangement_required", "accept")
    no_sa = sa_only_blocker()
    no_sa["eligibility"]["supply_arrangement_required"] = {
        "value": "no", "name": None, "evidence": NO_SA}
    new = reextract(x, no_sa, description=AMENDED)
    assert (carried(new, "supply_arrangement_required").reason) == Reason.ANSWER_CHANGED


def test_criteria_correction_needs_rereview_when_text_changes(client, reviewer, extraction,
                                                              reextract):
    x = extraction()
    client.force_login(reviewer)
    correct(client, x, "criteria_location", value="see_solicitation")
    assert carried(reextract(x), "criteria_location").applied  # same text: carries
    new = reextract(x, description=AMENDED)
    c = carried(new, "criteria_location")
    assert (c.applied, c.reason) == (False, Reason.NO_QUOTE)


def test_chain_links_to_the_original_decision(client, reviewer, extraction, reextract):
    x1 = extraction()
    client.force_login(reviewer)
    correct(client, x1, "supply_arrangement_required", value="no", evidence=NO_SA)
    original = ReviewDecision.objects.get()
    x2 = reextract(x1, description=AMENDED)  # changed text, quote still there
    x3 = reextract(x2)                        # same text as x2
    c3 = carried(x3, "supply_arrangement_required")
    assert c3.source == original and c3.applied
    assert c3.reason == Reason.QUOTE_FOUND  # inherited from x2's judgement, not re-labelled
    assert status(x3) == "no_blockers_stated"


def test_decision_on_an_older_extraction_reaches_the_newest(client, reviewer, extraction,
                                                            reextract):
    x = extraction()
    new = reextract(x)  # nothing to carry yet
    assert status(new) == "blocked" and not CarriedReview.objects.exists()
    client.force_login(reviewer)
    correct(client, x, "supply_arrangement_required", value="no", evidence=NO_SA)
    assert status(new) == "no_blockers_stated"  # the newest was reassessed too
    # A newer decision on the old extraction supersedes the carried one.
    decide(client, x, "supply_arrangement_required", "accept")
    assert status(new) == "blocked"
    assert CarriedReview.objects.filter(extraction=new).count() == 2


def test_own_decision_on_the_new_version_wins(client, reviewer, extraction, reextract):
    x = extraction()
    client.force_login(reviewer)
    correct(client, x, "supply_arrangement_required", value="no", evidence=NO_SA)
    new = reextract(x, description=QUOTE_GONE)
    assert status(new) == "blocked"
    decide(client, new, "supply_arrangement_required", "reject")
    assert status(new) == "no_blockers_stated"
    reviews = review.reviews_for(new)
    assert reviews.applied["supply_arrangement_required"].extraction_id == new.pk
    assert reviews.needs_rereview == []


def test_carry_is_idempotent_and_rows_are_immutable(client, reviewer, extraction, reextract):
    x = extraction()
    client.force_login(reviewer)
    correct(client, x, "supply_arrangement_required", value="no", evidence=NO_SA)
    new = reextract(x)
    first = assess(new)
    again = assess(new)
    assert CarriedReview.objects.count() == 1
    assert again.computed_at == first.computed_at
    c = CarriedReview.objects.get()
    c.applied = False
    with pytest.raises(AppendOnlyError):
        c.save()


def test_unreviewed_notice_costs_one_query(django_assert_max_num_queries, extraction):
    x = extraction()
    with django_assert_max_num_queries(1):
        review.carry_forward(x)
