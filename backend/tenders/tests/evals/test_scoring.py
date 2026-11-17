"""Eval scoring: error types, Wilson interval, status derivation, consistency. Pure, no DB."""

import pytest

from tenders.eligibility.rules import Profile, TenderFacts
from tenders.evals import scoring
from tenders.evals.scoring import FIELDS, Label, classify, wilson

PROFILE = Profile("Invented IT Ltd", (), "none", "none")
REGULAR = TenderFacts(None, "RFP", "regular")

UNKNOWN_GATES = {
    "supply_arrangement_required": {"value": "unknown", "name": None, "evidence": None},
    "supplier_origin_restriction": {"value": "unknown", "evidence": None},
    "clearance": {"corporate": "unknown", "personnel": "unknown", "evidence": None},
    "work_location": {"on_site": "unknown", "place": None, "evidence": None},
    "land_claims_applies": {"value": "unknown", "evidence": None},
    "documents_behind_login": {"value": "unknown", "portal": None, "evidence": None},
}


def label(criteria="stated_in_notice", unsure=False, **gate_changes) -> Label:
    gates = {g: {**v, **gate_changes.get(g, {})} for g, v in UNKNOWN_GATES.items()}
    return Label(criteria, gates, unsure, "note" if unsure else "")


def fields(**changes) -> dict:
    base = {f: "unknown" for f in FIELDS}
    base["criteria_location"] = "stated_in_notice"
    return {**base, **changes}


def case(name, label_fields, llm_fields, baseline_fields=None, *, expected="no_blockers_stated",
         llm_status="no_blockers_stated", baseline_status="check_solicitation", unsure=False,
         repeats=()):
    def system(f, status):
        return {"ok": f is not None, "fields": f if f is not None else dict.fromkeys(FIELDS),
                "evidence": dict.fromkeys(FIELDS), "status": status, "flags": []}
    return {"case": name, "unsure": unsure,
            "label": {"fields": label_fields, "evidence": dict.fromkeys(FIELDS),
                      "status": expected},
            "systems": {"llm": system(llm_fields, llm_status),
                        "baseline": system(baseline_fields or fields(), baseline_status)},
            "repeats": list(repeats)}


# --- error types -------------------------------------------------------------------------

@pytest.mark.parametrize("lab, got, kind", [
    ("yes", "yes", "correct"),
    ("unknown", "unknown", "correct"),       # matching "unknown" counts as correct
    ("secret", "reliability", "wrong"),      # both stated, different
    ("no", "yes", "wrong"),
    ("yes", "unknown", "missed"),            # label known, system silent
    ("none", None, "missed"),                # a failed extraction is silent
    ("unknown", "no", "overclaimed"),        # system states what the notice doesn't
    ("unknown", "top_secret", "overclaimed"),
])
def test_classify_four_error_types(lab, got, kind):
    assert classify(lab, got) == kind


def test_tally_counts_each_type_and_unknown_rate():
    lab = fields(**{"clearance.personnel": "reliability"})
    cases = [
        case("c1", lab, fields(**{"clearance.personnel": "reliability"})),  # correct
        case("c2", lab, fields(**{"clearance.personnel": "secret"})),       # wrong
        case("c3", lab, fields()),                                          # missed
        case("c4", fields(), fields(**{"clearance.personnel": "secret"})),  # overclaimed
        case("c5", fields(), None),                                         # correct (failed)
    ]
    t = scoring.tally(cases, "llm", "clearance.personnel")
    assert (t["correct"], t["wrong"], t["missed"], t["overclaimed"]) == (2, 1, 1, 1)
    assert t["n"] == 5 and t["accuracy"] == pytest.approx(0.4)
    assert t["system_unknown"] == 2  # c3 said unknown, c5 failed
    lo, hi = t["ci95"]
    assert lo < 0.4 < hi


def test_failed_extraction_misses_criteria_location():
    t = scoring.tally([case("c1", fields(), None)], "llm", "criteria_location")
    assert t["missed"] == 1


# --- Wilson interval ---------------------------------------------------------------------

@pytest.mark.parametrize("k, n, lo, hi", [
    (7, 10, 0.3968, 0.8922),
    (0, 10, 0.0, 0.2775),
    (10, 10, 0.7225, 1.0),
    (5, 10, 0.2366, 0.7634),
    (1, 1, 0.2065, 1.0),
])
def test_wilson_matches_reference_values(k, n, lo, hi):
    got = wilson(k, n)
    assert got == pytest.approx((lo, hi), abs=1e-4)


def test_wilson_empty_and_bounds():
    assert wilson(0, 0) is None
    for k in range(11):
        lo, hi = wilson(k, 10)
        assert 0 <= lo <= k / 10 <= hi <= 1


# --- labels ------------------------------------------------------------------------------

LABEL_YAML = """\
# a notice comment block would be here
criteria_location: partly_stated
gates:
  supply_arrangement_required: {value: yes, name: TBIPS, evidence: "must hold a valid TBIPS"}
  supplier_origin_restriction: {value: canadian_only, evidence: "limited to Canadian goods"}
  clearance: {corporate: none, personnel: reliability, evidence: "valid Reliability Status"}
  work_location: {on_site: no, place: null, evidence: "GD"}
  land_claims_applies: {value: unknown, evidence: null}
  documents_behind_login: {value: yes, portal: SAP Ariba, evidence: "only through SAP Ariba"}
unsure: true
notes: "Hard to tell."
"""


def test_parse_label_maps_yaml_booleans_back():
    lab = scoring.parse_label(LABEL_YAML)
    assert lab.unsure and lab.notes == "Hard to tell."
    assert lab.fields == {
        "criteria_location": "partly_stated", "supply_arrangement_required": "yes",
        "supplier_origin_restriction": "canadian_only", "clearance.corporate": "none",
        "clearance.personnel": "reliability", "work_location.on_site": "no",
        "land_claims_applies": "unknown", "documents_behind_login": "yes",
    }
    assert lab.gates["supply_arrangement_required"]["name"] == "TBIPS"
    assert scoring.evidence_by_field(lab.gates)["work_location.on_site"] == "GD"


# --- status derivation -------------------------------------------------------------------

def test_status_no_blockers_when_criteria_stated_and_nothing_blocks():
    assert scoring.expected_status(label(), REGULAR, PROFILE) == "no_blockers_stated"


def test_status_check_solicitation_when_criteria_elsewhere():
    assert scoring.expected_status(label("see_solicitation"), REGULAR,
                                   PROFILE) == "check_solicitation"


@pytest.mark.parametrize("gates", [
    {"clearance": {"personnel": "secret", "evidence": "Secret clearance"}},
    {"clearance": {"corporate": "reliability", "evidence": "valid DOS"}},
    {"supply_arrangement_required": {"value": "yes", "name": "TBIPS", "evidence": "TBIPS"}},
    {"work_location": {"on_site": "yes", "place": "Ottawa", "evidence": "on-site in Ottawa"}},
])
def test_status_blocked_by_label_gates(gates):
    assert scoring.expected_status(label(**gates), REGULAR, PROFILE) == "blocked"


def test_status_personnel_reliability_is_only_a_warning():
    lab = label(clearance={"personnel": "reliability", "evidence": "Reliability Status"})
    assert scoring.expected_status(lab, REGULAR, PROFILE) == "no_blockers_stated"


def test_status_uses_notice_kind_and_sa_metadata():
    sa_yes = label(supply_arrangement_required={"value": "yes", "name": None, "evidence": "x"})
    request = TenderFacts(None, "Request for Supply Arrangement", "sa_or_so_request")
    assert scoring.expected_status(sa_yes, request, PROFILE) == "no_blockers_stated"
    # Metadata says only holders may bid: blocked even though the label says unknown.
    against = TenderFacts(True, "RFP against Supply Arrangement", "against_sa")
    assert scoring.expected_status(label(), against, PROFILE) == "blocked"


def test_confusion_flags_false_and_missed_blocks():
    cases = [
        case("ok", fields(), fields(), expected="blocked", llm_status="blocked"),
        case("fb", fields(), fields(), expected="check_solicitation", llm_status="blocked"),
        case("mb", fields(), fields(), expected="blocked", llm_status="no_blockers_stated"),
        case("x", fields(), fields(), expected="no_blockers_stated",
             llm_status="check_solicitation"),
    ]
    c = scoring.confusion(cases, "llm")
    assert c["table"]["blocked"]["blocked"] == 1
    assert c["table"]["check_solicitation"]["blocked"] == 1
    assert c["table"]["blocked"]["no_blockers_stated"] == 1
    assert c["table"]["no_blockers_stated"]["check_solicitation"] == 1
    assert c["agree"] == 1 and c["n"] == 4
    assert c["false_blocks"] == ["fb"] and c["missed_blocks"] == ["mb"]


# --- consistency and disagreements -------------------------------------------------------

def test_consistency_counts_stable_cases_and_lists_flips():
    same = fields(land_claims_applies="no")
    run = lambda f, status="no_blockers_stated", ok=True: (
        {"ok": ok, "fields": f, "status": status})
    cases = [
        case("stable", fields(), same, repeats=[run(same), run(same)]),
        case("flips", fields(), same,
             repeats=[run(fields(land_claims_applies="unknown")), run(same, "blocked")]),
        case("failed", fields(), same, repeats=[run(dict.fromkeys(FIELDS), ok=False)]),
        case("no_repeats", fields(), same),
    ]
    con = scoring.consistency(cases)
    assert con["cases"] == 3
    assert con["runs_per_case"] == {"stable": 3, "flips": 3, "failed": 2}
    land = con["fields"]["land_claims_applies"]
    assert land["stable"] == 1 and land["n"] == 3
    assert [f["case"] for f in land["flips"]] == ["flips", "failed"]
    assert land["flips"][0]["values"] == ["no", "unknown", "no"]
    assert land["flips"][1]["values"] == ["no", scoring.FAILED]
    status = con["fields"]["status"]
    assert [f["case"] for f in status["flips"]] == ["flips"]


def test_disagreements_lists_only_fields_where_a_system_differs():
    lab = fields(documents_behind_login="yes")
    c = case("c1", lab, fields(documents_behind_login="yes"),
             fields(documents_behind_login="unknown", land_claims_applies="yes"))
    rows = scoring.disagreements(c)
    assert [r["field"] for r in rows] == ["land_claims_applies", "documents_behind_login"]
    assert rows[0]["llm"]["kind"] == "correct" and rows[0]["baseline"]["kind"] == "overclaimed"
    assert rows[1]["baseline"]["kind"] == "missed"
