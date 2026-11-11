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


# --- whole metadata values (a) -----------------------------------------------------------------

def test_whole_metadata_value_is_grounded_even_if_short():
    n = notice(notice_type="RFP against Supply Arrangement")
    srcs = [normalize(t) for _, t in n.sources]
    whole = frozenset(normalize(t) for k, t in n.sources if k.startswith("meta:"))
    closing = dict(n.sources)["meta:closing_date"]
    assert closing == "2026-12-01T17:30:00+00:00"
    assert is_grounded(closing, srcs, whole)  # 1 word: only grounded as a whole value
    assert not is_grounded(closing, srcs)  # without the exemption it is too short
    assert is_grounded("RFP  against supply arrangement", srcs, whole)
    assert not is_grounded("2026-12-01", srcs, whole)  # part of a value, too short
    assert not is_grounded("Supply Arrangement", srcs, whole)
    assert not is_grounded("notice_type: RFP against Supply Arrangement", srcs, whole)


def test_closing_deadline_from_metadata_is_kept():
    d = valid_data(deadlines=[{"type": "closing", "datetime": "2026-12-01T17:30:00Z",
                               "evidence": "2026-12-01T17:30:00+00:00"}])
    data, flags = check(d)
    assert flags == [] and len(data.deadlines) == 1


def test_sa_gate_grounded_by_notice_type_value():
    n = notice(notice_type="RFP against Supply Arrangement")
    d = valid_data()
    d["eligibility"]["supply_arrangement_required"] = {
        "value": "yes", "name": None, "evidence": "RFP against Supply Arrangement"}
    data, flags = check(d, sa_meta=True, n=n)
    assert data.eligibility.supply_arrangement_required.value == "yes"
    assert flags == []



def test_rendered_label_value_line_is_grounded():
    # The notice renders metadata as "label: value"; the model often quotes the whole line.
    n = notice(notice_type="RFP against Supply Arrangement")
    d = valid_data(deadlines=[{"type": "closing", "datetime": "2026-12-01T17:30:00Z",
                               "evidence": "closing_date: 2026-12-01T17:30:00+00:00"}])
    d["eligibility"]["supply_arrangement_required"] = {
        "value": "yes", "name": None,
        "evidence": "notice_type: RFP against Supply Arrangement"}
    data, flags = check(d, sa_meta=True, n=n)
    assert flags == [] and len(data.deadlines) == 1
    assert data.eligibility.supply_arrangement_required.value == "yes"


@pytest.mark.parametrize("evidence", [
    "closing_date 2026-12-01T17:30:00+00:00",  # no colon: not the rendered line
    "notice_type: 2026-12-01T17:30:00+00:00",  # another field's value
    "closing_date: 2026-12-01",  # part of the value
])
def test_label_value_line_must_be_exact(evidence):
    d = valid_data(deadlines=[{"type": "closing", "datetime": "2026-12-01T17:30:00Z",
                               "evidence": evidence}])
    data, flags = check(d)
    assert flags == ["ungrounded:deadlines[0]"] and data.deadlines == []

# --- punctuation-insensitive details (b) -------------------------------------------------------

SLSA = notice(description="This Request for Quote is issued pursuant to Software Licensing "
                          "Supply Arrangement (SLSA) # EN578-232335/065/SMS for licences.")


def test_detail_ignores_punctuation_but_quote_stays_strict():
    d = valid_data(deliverables=[], deadlines=[], mandatory_requirements=[])
    d["eligibility"]["work_location"] = {"place": None, "on_site": "unknown", "evidence": None}
    d["eligibility"]["documents_behind_login"] = {"value": "unknown", "portal": None,
                                                  "evidence": None}
    d["eligibility"]["clearance"] = {"corporate": "unknown", "personnel": "unknown",
                                     "evidence": None}
    d["eligibility"]["supply_arrangement_required"] = {
        "value": "yes", "name": "(SLSA) EN578",
        "evidence": "issued pursuant to Software Licensing Supply Arrangement (SLSA) #"}
    data, flags = check(d, n=SLSA)
    assert data.eligibility.supply_arrangement_required.name == "(SLSA) EN578"
    assert flags == []

    # The same text without the "#" is not a verbatim quote: punctuation counts there.
    d["eligibility"]["supply_arrangement_required"]["evidence"] = \
        "Software Licensing Supply Arrangement (SLSA) EN578-232335"
    data, flags = check(d, n=SLSA)
    assert data.eligibility.supply_arrangement_required.value == "unknown"
    assert flags == ["ungrounded:eligibility.supply_arrangement_required"]


def test_detail_punctuation_does_not_merge_words():
    d = valid_data()
    d["eligibility"]["work_location"]["place"] = "St.John's"  # no space: still matches
    assert check(d)[1] == []
    d["eligibility"]["work_location"]["place"] = "StJohns"  # letters merged: no match
    assert check(d)[1] == ["ungrounded_detail:eligibility.work_location.place"]
