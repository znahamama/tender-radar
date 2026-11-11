import pytest

from tenders.extraction.prompt import build_notice
from tenders.extraction.schema import ExtractionData
from tenders.extraction.validate import is_grounded, normalize, postvalidate
from tenders.models import Tender

from .conftest import tender_fields, valid_data


def notice(**fields):
    return build_notice(Tender(**tender_fields(**fields)))  # unsaved: no DB needed


NOTICE = notice(regions_of_delivery=["Ontario (except NCR)", "National Capital Region (NCR)"],
                buyer_org_name="Department of Records")


def check(d, sa_meta=None, n=NOTICE):
    return postvalidate(ExtractionData.model_validate(d), n, sa_meta)


def sources(n=NOTICE):
    return [normalize(t) for _, t in n.sources]


def test_grounded_output_passes_unchanged():
    data, flags = check(valid_data())
    assert flags == []
    assert data == ExtractionData.model_validate(valid_data())


def test_normalize_whitespace_case_quotes_and_brackets():
    assert normalize("  On-Site\n at  St. John’s ") == "on-site at st. john's"
    assert normalize("a <b> c") == normalize("a ‹b› c")


@pytest.mark.parametrize("evidence", [
    "the",                                     # trivial word
    "regions_of_delivery:",                    # label text
    "regions_of_delivery: Ontario (except NCR)",  # label + value
    "buyer: Department of Records",            # label + value
    '<description truncated="false">',         # tag text
    "</metadata>\n<description",               # tag text spanning lines
    "hold a valid",                            # 3 words but under 15 chars
    "valid TBIPS",                             # 2 words
])
def test_short_label_and_tag_evidence_is_not_grounded(evidence):
    assert not is_grounded(evidence, sources())


def test_minimum_length_boundary():
    assert is_grounded("valid TBIPS supply", sources())  # 3 words, 18 chars: enough


def test_metadata_value_and_title_are_valid_sources():
    assert is_grounded("Ontario (except NCR); National Capital Region (NCR)", sources())
    assert is_grounded("Records system modernization", sources())


def test_trivial_evidence_cannot_ground_a_claim():
    d = valid_data()
    d["eligibility"]["clearance"] = {"corporate": "top_secret", "personnel": "top_secret",
                                     "evidence": "the"}
    data, flags = check(d)
    assert (data.eligibility.clearance.corporate, data.eligibility.clearance.evidence) == \
        ("unknown", None)
    assert flags == ["ungrounded:eligibility.clearance"]


def test_label_evidence_cannot_ground_a_claim():
    d = valid_data()
    d["eligibility"]["work_location"] = {"place": None, "on_site": "yes",
                                         "evidence": "regions_of_delivery:"}
    data, flags = check(d)
    assert data.eligibility.work_location.on_site == "unknown"
    assert flags == ["ungrounded:eligibility.work_location"]


def test_ungrounded_gate_reset_to_unknown_and_flagged():
    d = valid_data()
    d["eligibility"]["clearance"]["evidence"] = "Personnel must hold Secret clearance."
    d["eligibility"]["clearance"]["personnel"] = "secret"
    d["eligibility"]["work_location"]["evidence"] = "work is performed remotely"
    data, flags = check(d)
    c, w = data.eligibility.clearance, data.eligibility.work_location
    assert (c.corporate, c.personnel, c.evidence) == ("unknown", "unknown", None)
    assert (w.on_site, w.place, w.evidence) == ("unknown", None, None)
    assert flags == ["ungrounded:eligibility.clearance", "ungrounded:eligibility.work_location"]


def test_claim_without_evidence_is_ungrounded():
    d = valid_data()
    d["eligibility"]["land_claims_applies"] = {"value": "no", "evidence": None}
    data, flags = check(d)
    assert data.eligibility.land_claims_applies.value == "unknown"
    assert flags == ["ungrounded:eligibility.land_claims_applies"]


def test_empty_evidence_is_ungrounded():
    d = valid_data()
    d["eligibility"]["documents_behind_login"]["evidence"] = "  "
    data, flags = check(d)
    g = data.eligibility.documents_behind_login
    assert (g.value, g.portal, g.evidence) == ("unknown", None, None)
    assert flags == ["ungrounded:eligibility.documents_behind_login"]


# --- detail fields (#6) ---------------------------------------------------------------------

def test_detail_not_in_notice_is_nulled_but_gate_kept():
    d = valid_data()
    d["eligibility"]["supply_arrangement_required"]["name"] = "ProServices"
    d["eligibility"]["documents_behind_login"]["portal"] = "SAP Ariba"
    data, flags = check(d)
    sa, docs = data.eligibility.supply_arrangement_required, data.eligibility.documents_behind_login
    assert (sa.value, sa.name) == ("yes", None)
    assert (docs.value, docs.portal) == ("no", None)
    assert flags == ["ungrounded_detail:eligibility.supply_arrangement_required.name",
                     "ungrounded_detail:eligibility.documents_behind_login.portal"]


def test_detail_matches_case_and_whitespace_insensitively():
    d = valid_data()
    d["eligibility"]["supply_arrangement_required"]["name"] = "tbips"
    d["eligibility"]["work_location"]["place"] = "St.  John's,\nNewfoundland"
    assert check(d)[1] == []


def test_place_from_regions_metadata_only_is_rejected():
    n = notice(regions_of_delivery=["Ottawa"])
    d = valid_data()
    d["eligibility"]["work_location"]["place"] = "Ottawa"
    data, flags = check(d, n=n)
    assert data.eligibility.work_location.place is None
    assert data.eligibility.work_location.on_site == "yes"  # evidence itself is grounded
    assert flags == ["ungrounded_detail:eligibility.work_location.place"]


def test_place_in_regions_and_description_is_allowed():
    n = notice(regions_of_delivery=["St. John's"])
    assert check(valid_data(), n=n)[1] == []


def test_place_quoted_from_a_regions_value_is_rejected():
    n = notice(regions_of_delivery=["Ontario (except NCR)", "Ottawa"])
    d = valid_data()
    d["eligibility"]["work_location"] = {"place": "Ottawa", "on_site": "unknown",
                                         "evidence": "Ontario (except NCR); Ottawa"}
    data, flags = check(d, n=n)
    assert data.eligibility.work_location.evidence is not None  # quote is a real value
    assert data.eligibility.work_location.place is None
    assert flags == ["ungrounded_detail:eligibility.work_location.place"]


def test_detail_checked_even_without_a_claim():
    d = valid_data()
    d["eligibility"]["supply_arrangement_required"] = {"value": "unknown", "name": "SBIPS",
                                                       "evidence": None}
    data, flags = check(d)
    assert data.eligibility.supply_arrangement_required.name is None
    assert flags == ["ungrounded_detail:eligibility.supply_arrangement_required.name"]


# --- lists and cross-checks --------------------------------------------------------------------

def test_ungrounded_list_items_dropped():
    d = valid_data(deliverables=[
        {"text": "Plan", "evidence": "Deliver a migration plan within 30 days"},
        {"text": "Invented", "evidence": "Provide 24/7 support for five years"},
        {"text": "Label", "evidence": "closing_date: 2026-12-01"},
    ])
    data, flags = check(d)
    assert [x.text for x in data.deliverables] == ["Plan"]
    assert flags == ["ungrounded:deliverables[1]", "ungrounded:deliverables[2]"]


def test_sa_metadata_disagreement_flag():
    d = valid_data()
    d["eligibility"]["supply_arrangement_required"] = {"value": "no", "name": None,
                                                       "evidence": None}
    assert "sa_metadata_disagrees" in check(d, sa_meta=True)[1]
    assert "sa_metadata_disagrees" not in check(d, sa_meta=None)[1]
    assert "sa_metadata_disagrees" not in check(valid_data(), sa_meta=True)[1]


def test_sa_flag_after_grounding_reset():
    d = valid_data()
    d["eligibility"]["supply_arrangement_required"]["evidence"] = "only SA holders may bid"
    _, flags = check(d, sa_meta=True)
    assert flags == ["ungrounded:eligibility.supply_arrangement_required",
                     "sa_metadata_disagrees"]


def test_injection_flag():
    assert check(valid_data(injection_suspected=True))[1] == ["injection_suspected"]
