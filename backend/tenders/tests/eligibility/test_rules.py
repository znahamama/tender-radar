import copy

import pytest

from tenders.eligibility.rules import (
    RULES_VERSION,
    Decision,
    Profile,
    TenderFacts,
    evaluate,
    holds_arrangement,
)
from tenders.tests.extraction.conftest import VALID

NONE_PROFILE = Profile("Harbourline Software (fictional)", (), "none", "none")
FACTS = TenderFacts(sa_required_from_metadata=None, notice_type="Request for Proposal")


def data(criteria="stated_in_notice", **gates) -> dict:
    """Valid post-validated data with everything settled and permissive, then overrides."""
    d = copy.deepcopy(VALID)
    d["criteria_location"] = criteria
    e = d["eligibility"]
    e["supply_arrangement_required"] = {"value": "no", "name": None, "evidence": "q"}
    e["clearance"] = {"corporate": "none", "personnel": "none", "evidence": "q"}
    e["work_location"] = {"place": None, "on_site": "no", "evidence": "q"}
    e["documents_behind_login"] = {"value": "no", "portal": None, "evidence": "q"}
    for gate, values in gates.items():
        e[gate] = {**e[gate], **values}
    return d


def run(d, facts=FACTS, profile=NONE_PROFILE, flags=(), error="") -> Decision:
    return evaluate(d, list(flags), facts, profile, error)


def reasons(findings):
    return [f.reason for f in findings]


def test_all_clear_is_no_blockers_stated():
    dec = run(data())
    assert dec.status == "no_blockers_stated"
    assert dec.blockers == [] and dec.warnings == []
    # data() leaves these two unknown (from VALID)
    assert dec.not_stated == ["supplier_origin_restriction", "land_claims_applies"]


def test_rules_version():
    assert RULES_VERSION == "2"


def test_not_stated_lists_every_unknown_gate_in_schema_order():
    d = data(supply_arrangement_required={"value": "unknown", "evidence": None},
             clearance={"corporate": "unknown", "personnel": "unknown", "evidence": None},
             work_location={"on_site": "unknown", "evidence": None},
             documents_behind_login={"value": "unknown", "evidence": None})
    dec = run(d)
    assert dec.status == "no_blockers_stated"  # unknowns no longer change the status
    assert dec.not_stated == ["supply_arrangement_required", "supplier_origin_restriction",
                              "clearance", "work_location", "land_claims_applies",
                              "documents_behind_login"]


def test_nothing_not_stated():
    d = data(supplier_origin_restriction={"value": "none", "evidence": "q"},
             land_claims_applies={"value": "no", "evidence": "q"})
    assert run(d).not_stated == []


# --- supply arrangement ----------------------------------------------------------------------

def test_sa_named_and_not_held_blocks():
    dec = run(data(supply_arrangement_required={"value": "yes", "name": "TBIPS"}))
    assert dec.status == "blocked"
    assert reasons(dec.blockers) == [
        "Bidding is limited to holders of the TBIPS supply arrangement; the profile holds none of it."]


def test_sa_unnamed_blocks_profile_with_none():
    dec = run(data(supply_arrangement_required={"value": "yes", "name": None}))
    assert "not named" in dec.blockers[0].reason


def test_sa_held_is_a_note():
    profile = Profile("p", ("TBIPS Tier 1",), "none", "none")
    dec = run(data(supply_arrangement_required={"value": "yes", "name": "TBIPS"}),
              profile=profile)
    assert dec.status == "no_blockers_stated" and "holds it" in dec.notes[0].reason


def test_sa_unnamed_with_some_held_is_a_warning():
    profile = Profile("p", ("ProServices",), "none", "none")
    dec = run(data(supply_arrangement_required={"value": "yes", "name": None}), profile=profile)
    assert dec.blockers == [] and "check whether" in dec.warnings[0].reason


def test_metadata_precedence_overrides_llm_no():
    facts = TenderFacts(sa_required_from_metadata=True,
                        notice_type="RFP against Supply Arrangement")
    dec = run(data(supply_arrangement_required={"value": "no", "evidence": None}), facts=facts)
    assert dec.status == "blocked"
    assert dec.blockers[0].evidence == "RFP against Supply Arrangement"


def test_metadata_precedence_keeps_llm_evidence_when_it_agrees():
    facts = TenderFacts(True, "RFP against Supply Arrangement")
    d = data(supply_arrangement_required={"value": "yes", "name": "TSPS", "evidence": "x y z"})
    dec = run(d, facts=facts)
    assert dec.blockers[0].evidence == "x y z" and "TSPS" in dec.blockers[0].reason


def test_holds_arrangement_loose():
    assert holds_arrangement("TBIPS", ("TBIPS Tier 1",))
    assert holds_arrangement("Task-Based Informatics (TBIPS)", ("tbips",))
    assert not holds_arrangement("TSPS", ("TBIPS",))
    assert not holds_arrangement("TSPS", ())


# --- clearance ---------------------------------------------------------------------------------

@pytest.mark.parametrize("level", ["reliability", "secret", "top_secret"])
def test_corporate_clearance_blocks(level):
    dec = run(data(clearance={"corporate": level, "personnel": "none"}))
    assert dec.status == "blocked" and dec.blockers[0].gate == "clearance"


@pytest.mark.parametrize("level", ["secret", "top_secret"])
def test_personnel_secret_blocks(level):
    assert run(data(clearance={"corporate": "none", "personnel": level})).status == "blocked"


def test_personnel_reliability_is_a_warning():
    dec = run(data(clearance={"corporate": "none", "personnel": "reliability"}))
    assert dec.status == "no_blockers_stated"
    assert reasons(dec.warnings) == ["Personnel clearance required: Reliability (profile: none)."]


def test_profile_with_clearance_is_not_blocked():
    profile = Profile("p", (), "secret", "secret")
    dec = run(data(clearance={"corporate": "secret", "personnel": "secret"}), profile=profile)
    assert dec.blockers == []


@pytest.mark.parametrize("clearance", [
    {"corporate": "unknown", "personnel": "none"},
    {"corporate": "none", "personnel": "unknown"},
])
def test_unknown_clearance_is_not_stated(clearance):
    dec = run(data(clearance=clearance))
    assert dec.status == "no_blockers_stated" and "clearance" in dec.not_stated


def test_unknown_side_with_a_blocking_side():
    dec = run(data(clearance={"corporate": "secret", "personnel": "unknown"}))
    assert dec.status == "blocked" and "clearance" in dec.not_stated


# --- on-site -----------------------------------------------------------------------------------

@pytest.mark.parametrize(("place", "status", "kind"), [
    ("Ottawa", "blocked", "blockers"),
    ("St. John's", "no_blockers_stated", "notes"),
    ("Mount Pearl", "no_blockers_stated", "notes"),
    ("Gander", "no_blockers_stated", "warnings"),
    ("St. John's and Ottawa", "no_blockers_stated", "warnings"),
    ("Building 12", "no_blockers_stated", "warnings"),
])
def test_on_site_place(place, status, kind):
    dec = run(data(work_location={"on_site": "yes", "place": place}))
    assert dec.status == status
    assert [f.gate for f in getattr(dec, kind)] == ["work_location"]


def test_on_site_place_unknown_is_a_warning():
    dec = run(data(work_location={"on_site": "yes", "place": None}))
    assert reasons(dec.warnings) == ["On-site work, location not stated."]


def test_on_site_unknown_is_not_stated():
    dec = run(data(work_location={"on_site": "unknown"}))
    assert dec.status == "no_blockers_stated" and "work_location" in dec.not_stated


def test_off_site_place_is_ignored():
    dec = run(data(work_location={"on_site": "no", "place": "Ottawa"}))
    assert dec.status == "no_blockers_stated" and dec.warnings == []


# --- notes, warnings, status -------------------------------------------------------------------

def test_origin_land_claims_documents():
    dec = run(data(supplier_origin_restriction={"value": "canadian_only", "evidence": "a b c"},
                   land_claims_applies={"value": "yes", "evidence": "d e f"},
                   documents_behind_login={"value": "yes", "portal": "SAP Ariba"}))
    assert dec.status == "no_blockers_stated"
    assert reasons(dec.warnings) == ["A comprehensive land claims agreement applies."]
    assert reasons(dec.notes) == ["Limited to Canadian suppliers, goods or services.",
                                  "Documents are behind a login: SAP Ariba."]


def test_see_solicitation_means_check_solicitation():
    assert run(data(criteria="see_solicitation")).status == "check_solicitation"
    assert run(data(criteria="partly_stated")).status == "no_blockers_stated"


def test_sa_unknown_is_not_stated():
    d = data(supply_arrangement_required={"value": "unknown", "evidence": None})
    dec = run(d)
    assert dec.status == "no_blockers_stated"
    assert "supply_arrangement_required" in dec.not_stated


def test_sa_from_metadata_is_stated_even_if_llm_unknown():
    d = data(supply_arrangement_required={"value": "unknown", "evidence": None})
    dec = run(d, facts=TenderFacts(True, "RFP against Supply Arrangement"))
    assert dec.status == "blocked" and "supply_arrangement_required" not in dec.not_stated


def test_blocker_beats_everything():
    d = data(criteria="see_solicitation", clearance={"corporate": "secret", "personnel": "unknown"})
    assert run(d).status == "blocked"


def test_failed_extraction():
    dec = run(None, error="schema_invalid:\n- x: bad")
    assert dec.status == "check_solicitation"
    assert reasons(dec.warnings) == ["Extraction failed: schema_invalid:"]
    assert len(dec.not_stated) == 6


def test_failed_extraction_still_blocked_by_metadata():
    dec = run(None, facts=TenderFacts(True, "RFP against Supply Arrangement"))
    assert dec.status == "blocked" and len(dec.not_stated) == 5


def test_injection_flag_warns():
    dec = run(data(), flags=["injection_suspected"])
    assert "instruct the model" in dec.warnings[0].reason


def test_as_fields_is_json_ready():
    f = run(data(clearance={"corporate": "secret", "personnel": "none"})).as_fields()
    assert f["status"] == "blocked"
    assert f["not_stated"] == ["supplier_origin_restriction", "land_claims_applies"]
    assert f["blockers"] == [{"gate": "clearance", "evidence": "q",
                              "reason": "Corporate clearance required: Secret (profile: none)."}]
