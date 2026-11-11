import pytest

from tenders.extraction.keyword import KeywordExtractor
from tenders.extraction.prompt import build_notice
from tenders.extraction.schema import ExtractionData
from tenders.extraction.validate import postvalidate
from tenders.models import Tender

from .conftest import DESCRIPTION, tender_fields

KW = KeywordExtractor()


def notice(**fields):
    return build_notice(Tender(**tender_fields(**fields)))


def gates(**fields) -> dict:
    """Run the baseline, then the same post-validation the LLM gets; assert it is grounded."""
    n = notice(**fields)
    raw = ExtractionData.model_validate(KW.gates(n))
    checked, flags = postvalidate(raw, n, None)
    assert not [f for f in flags if f.startswith("ungrounded")], flags
    return checked.model_dump(mode="json")["eligibility"]


def test_shared_description():
    g = gates()
    assert g["supply_arrangement_required"]["value"] == "yes"
    assert g["supply_arrangement_required"]["name"] == "TBIPS"
    assert (g["clearance"]["corporate"], g["clearance"]["personnel"]) == ("reliability", "unknown")
    assert g["work_location"] == {"place": "St. John's", "on_site": "yes",
                                  "evidence": "The work will be performed on-site at   "
                                              "St. John's, Newfoundland and Labrador."}
    assert g["documents_behind_login"]["value"] == "unknown"  # CanadaBuys is public


@pytest.mark.parametrize(("notice_type", "value"), [
    ("RFP against Supply Arrangement", "yes"),
    ("Request for Supply Arrangement", "no"),
    ("Request for Standing Offer", "no"),
])
def test_sa_from_notice_type(notice_type, value):
    g = gates(notice_type=notice_type, description="Software services are required.")
    assert g["supply_arrangement_required"]["value"] == value
    assert g["supply_arrangement_required"]["evidence"] == notice_type


@pytest.mark.parametrize("text", [
    "Only Supply Arrangement holders may submit a proposal.",
    "This RFQ is issued pursuant to the Software Licensing Supply Arrangement for licences.",
    "Suppliers must be pre-qualified under the ProServices method.",
])
def test_sa_from_text(text):
    assert gates(description=text)["supply_arrangement_required"]["value"] == "yes"


def test_sa_unknown_when_silent():
    g = gates(description="Software services are required for one year.")
    assert g["supply_arrangement_required"]["value"] == "unknown"


def test_origin():
    g = gates(description="This requirement is solely limited to Canadian goods and/or services.")
    assert g["supplier_origin_restriction"]["value"] == "canadian_only"
    g = gates(description="Proposals may be limited to Canadian Suppliers and Suppliers of an "
                          "Applicable Trading Partner.")
    assert g["supplier_origin_restriction"]["value"] == "canadian_and_trading_partners"
    g = gates(description="Nothing about origin here at all.",
              trade_agreements=["Canadian Free Trade Agreement (CFTA)",
                                "World Trade Organization Agreement on Government Procurement"])
    assert g["supplier_origin_restriction"] == {
        "value": "canadian_and_trading_partners",
        "evidence": "World Trade Organization Agreement on Government Procurement"}
    g = gates(description="Nothing here.", trade_agreements=["Canadian Free Trade Agreement (CFTA)"])
    assert g["supplier_origin_restriction"]["value"] == "unknown"


@pytest.mark.parametrize(("text", "corporate", "personnel"), [
    ("Security Requirements: There is no mandatory security requirement for this.", "none", "none"),
    ("Planned contracts: 1 Security Requirement None Contracting Authority", "none", "none"),
    ("The Contractor must hold a valid Facility Security Clearance at the level of SECRET.",
     "secret", "unknown"),
    ("The Contractor must hold a valid Designated Organization Screening (DOS).",
     "reliability", "unknown"),
    ("Personnel must each hold a valid Reliability Status.", "unknown", "reliability"),
    ("Resources must hold a Top Secret clearance.", "unknown", "top_secret"),
    ("The tablets run DOS-free firmware.", "unknown", "unknown"),  # not the acronym
    ("Bidders must hold a valid FSC at the Secret level.", "secret", "unknown"),
    ("There is a Security Requirement associated with this contract.", "unknown", "unknown"),
])
def test_clearance(text, corporate, personnel):
    c = gates(description=text)["clearance"]
    assert (c["corporate"], c["personnel"]) == (corporate, personnel)


def test_work_location():
    g = gates(description="Services must be performed on site at the Gander office.")
    assert (g["work_location"]["on_site"], g["work_location"]["place"]) == ("yes", "Gander")
    g = gates(description="Work may be performed remotely from the contractor's office.")
    assert g["work_location"]["on_site"] == "no"
    g = gates(description="Supply of 40 rugged tablets.", procurement_categories=["GD"])
    assert g["work_location"] == {"place": None, "on_site": "no", "evidence": "GD"}
    g = gates(description="Supply and install 4 UPS units.", procurement_categories=["GD"])
    assert g["work_location"]["on_site"] == "unknown"
    g = gates(description="Remote observation payloads for a satellite.")
    assert g["work_location"]["on_site"] == "unknown"  # "remote" alone is not remote work


def test_sentences_keep_abbreviations():
    from tenders.extraction.keyword import _sentences
    assert _sentences("Work at St. John's, N.L. daily. Then Mt. Pearl.") == [
        "Work at St. John's, N.L. daily.", "Then Mt. Pearl."]


def test_long_sentence_quote_is_a_window():
    text = ("word " * 120) + "Security Requirement None " + ("word " * 120)
    c = gates(description=text)["clearance"]
    assert c["corporate"] == "none" and len(c["evidence"]) < 260


@pytest.mark.parametrize(("text", "value"), [
    ("This procurement is subject to the Comprehensive Land Claims Agreements in Canada.", "yes"),
    ("Comprehensive Land Claim Agreement: Yes Nature of Requirements: services", "yes"),
    ("Comprehensive Land Claim Agreement: No", "no"),
    ("No mention of that topic.", "unknown"),
])
def test_land_claims(text, value):
    assert gates(description=text)["land_claims_applies"]["value"] == value


def test_land_claims_from_trade_agreements():
    g = gates(description="Nothing.", trade_agreements=["Comprehensive Land Claim Agreement (CLCA)"])
    assert g["land_claims_applies"] == {"value": "yes",
                                        "evidence": "Comprehensive Land Claim Agreement (CLCA)"}


def test_documents():
    g = gates(description="Bid documents can be accessed and downloaded when logged into your "
                          "SAP Ariba account.")
    assert g["documents_behind_login"]["value"] == "yes"
    assert g["documents_behind_login"]["portal"] == "SAP Ariba"
    g = gates(description="Solicitation documents require an account on the buyer website.")
    assert (g["documents_behind_login"]["value"], g["documents_behind_login"]["portal"]) == \
        ("yes", None)


def test_only_gates_filled_and_no_meter_use(meter):
    r = KW.extract(notice(), meter)
    assert r.status == "ok" and meter.calls == 0 and meter.api_calls == 0
    d = r.data
    assert (d.summary, d.work_type, d.criteria_location) == ("", "other", "see_solicitation")
    assert d.deliverables == d.deadlines == d.mandatory_requirements == d.certifications == []
    assert d.injection_suspected is False


def test_fingerprint_stable_and_identity():
    assert KeywordExtractor().fingerprint == KW.fingerprint
    assert (KW.model_id, KW.prompt_version) == ("keyword-baseline", "rules-v1")


def test_description_fixture_is_unchanged():
    assert DESCRIPTION.startswith("Public Works requires")
