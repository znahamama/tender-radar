from tenders.extraction.schema import ExtractionData
from tenders.extraction.validate import normalize, postvalidate

from .conftest import DESCRIPTION, valid_data

SOURCE = f"<notice>\n<title>T</title>\n<metadata>\nclosing_date: 2026-12-01T17:30:00+00:00\n" \
         f"</metadata>\n<description>\n{DESCRIPTION}\n</description>\n</notice>"


def check(d, sa_meta=None):
    return postvalidate(ExtractionData.model_validate(d), SOURCE, sa_meta)


def test_grounded_output_passes_unchanged():
    data, flags = check(valid_data())
    assert flags == []
    assert data == ExtractionData.model_validate(valid_data())


def test_normalize_whitespace_case_and_quotes():
    assert normalize("  On-Site\n at  St. John’s ") == "on-site at st. john's"


def test_ungrounded_gate_reset_to_unknown_and_flagged():
    d = valid_data()
    d["eligibility"]["clearance"]["evidence"] = "Personnel must hold Secret clearance."
    d["eligibility"]["clearance"]["personnel"] = "secret"
    d["eligibility"]["work_location"]["evidence"] = "work is remote"
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


def test_ungrounded_list_items_dropped():
    d = valid_data(deliverables=[
        {"text": "Plan", "evidence": "Deliver a migration plan within 30 days"},
        {"text": "Invented", "evidence": "Provide 24/7 support for five years"},
    ])
    data, flags = check(d)
    assert [x.text for x in data.deliverables] == ["Plan"]
    assert flags == ["ungrounded:deliverables[1]"]


def test_sa_metadata_disagreement_flag():
    d = valid_data()
    d["eligibility"]["supply_arrangement_required"] = {"value": "no", "name": None,
                                                       "evidence": None}
    assert "sa_metadata_disagrees" in check(d, sa_meta=True)[1]
    assert "sa_metadata_disagrees" not in check(d, sa_meta=None)[1]
    assert "sa_metadata_disagrees" not in check(valid_data(), sa_meta=True)[1]


def test_sa_flag_after_grounding_reset():
    d = valid_data()
    d["eligibility"]["supply_arrangement_required"]["evidence"] = "only SA holders"  # not in text
    _, flags = check(d, sa_meta=True)
    assert flags == ["ungrounded:eligibility.supply_arrangement_required",
                     "sa_metadata_disagrees"]


def test_injection_flag():
    assert check(valid_data(injection_suspected=True))[1] == ["injection_suspected"]
