"""Label templates and check_labels. All notice text here is invented."""

import json
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command

from tenders.evals.labels import BODY, check_text, parse_header, render
from tenders.extraction.validate import normalize

META = {
    "reference": "cb-0000-00000042",
    "title": "Help desk support for the Corner Brook regional office",
    "buyer": "Department of Invented Services",
    "notice_type": "RFP against Supply Arrangement",
    "procurement_categories": "SRV",
    "regions_of_delivery": "Newfoundland and Labrador; Ontario (except NCR)",
    "regions_of_opportunity": "",
    "trade_agreements": "Canadian Free Trade Agreement (CFTA)",
    "closing_date": "2026-12-01T17:30:00+00:00",
    "url": "https://example.com/" + "x" * 120,
}
DESCRIPTION = """Two analysts must work on-site at the Corner Brook regional office, Monday to \
Friday, for the term of the contract; the work is well-defined and long-running enough that \
this sentence has to wrap over several lines.

Bidders must hold a valid TBIPS supply arrangement. Personnel must hold valid Reliability Status.
   Indented line with ‹angle› brackets and “curly quotes”.

Documents are available only through SAP Ariba after registration."""

FILLED = """\
criteria_location: partly_stated
gates:
  supply_arrangement_required: {value: yes, name: TBIPS, evidence: "RFP against Supply Arrangement"}
  supplier_origin_restriction: {value: unknown, evidence: null}
  clearance: {corporate: unknown, personnel: reliability, evidence: "personnel MUST hold  valid reliability status"}
  work_location: {on_site: yes, place: "Corner Brook regional office", evidence: "Two analysts must work on-site at the Corner Brook regional office"}
  land_claims_applies: {value: unknown, evidence: null}
  documents_behind_login: {value: yes, portal: SAP Ariba, evidence: "available only through SAP Ariba after registration"}
unsure: false
notes: ""
"""


def notice_block() -> str:
    return render("dev-01", META, DESCRIPTION).split("\n\n" + BODY)[0] + "\n\n"


def check(body: str):
    return check_text(Path("dev-01.yaml"), notice_block() + body)


def test_render_round_trips_and_wraps():
    text = render("dev-01", META, DESCRIPTION)
    h = parse_header(text)
    assert h.meta["reference"] == META["reference"]
    for key, value in META.items():
        assert normalize(h.meta[key]) == normalize(value)
    assert normalize(h.description) == normalize(DESCRIPTION)
    # Wrapped at 100 except an unbreakable URL; never split inside "on-site".
    long = [line for line in text.splitlines() if len(line) > 100]
    assert len(long) == 1 and long[0].startswith("# url: ")
    assert "on-site" in h.description


def test_template_is_only_todos():
    r = check_text(Path("dev-01.yaml"), render("dev-01", META, DESCRIPTION))
    assert r.todo == 8
    assert r.problems == []
    assert not r.complete


def test_filled_file_is_complete():
    r = check(FILLED)
    assert r.problems == []
    assert r.todo == 0
    assert r.complete


@pytest.mark.parametrize("old, new, problem", [
    ("criteria_location: partly_stated", "criteria_location: maybe",
     "criteria_location: 'maybe' is not one of"),
    ("corporate: unknown", "corporate: no", "clearance.corporate: False is not one of"),
    ('evidence: "RFP against Supply Arrangement"', 'evidence: "must hold a TBIPS arrangement"',
     "supply_arrangement_required.evidence: not an exact copy"),
    ('evidence: "RFP against Supply Arrangement"', 'evidence: "against Supply"',
     "supply_arrangement_required.evidence: not an exact copy"),
    ("supplier_origin_restriction: {value: unknown, evidence: null}",
     'supplier_origin_restriction: {value: unknown, evidence: "Bidders must hold a valid"}',
     "supplier_origin_restriction.evidence: value is unknown"),
    ("land_claims_applies: {value: unknown, evidence: null}",
     "land_claims_applies: {value: no, evidence: null}",
     "land_claims_applies.evidence: a value other than unknown needs evidence"),
    ('place: "Corner Brook regional office"', "place: Ontario",
     "work_location.place: 'Ontario' does not appear in the notice (not counting"),
    ("supplier_origin_restriction: {value: unknown, evidence: null}",
     "supplier_origin_restriction: {value: unknown, evidence: null, extra: 1}",
     "supplier_origin_restriction: unexpected extra"),
    ("unsure: false", "unsure: true", "unsure: true needs a one-line note"),
    ("name: TBIPS, evidence", "name: ProServices, evidence",
     "supply_arrangement_required.name: 'ProServices' does not appear"),
])
def test_problems(old, new, problem):
    assert old in FILLED
    r = check(FILLED.replace(old, new))
    assert any(p.startswith(problem) for p in r.problems), r.problems
    assert not r.complete


def test_detail_only_with_yes():
    body = FILLED.replace("{value: yes, portal: SAP Ariba,", "{value: no, portal: SAP Ariba,")
    assert "documents_behind_login.portal: only filled when the value is yes" in check(body).problems


def test_unsure_with_note_is_fine():
    body = FILLED.replace("unsure: false", "unsure: true").replace(
        'notes: ""', 'notes: "Unclear whether the analysts are the bidder\'s staff."')
    assert check(body).complete


def test_yaml_error_and_damaged_header():
    r = check(FILLED.replace("{value: unknown, evidence: null}\n  clearance", "{value: unknown\n  clearance", 1))
    assert any(p.startswith("YAML does not parse") for p in r.problems)
    r = check_text(Path("x.yaml"), FILLED)
    assert "notice comment block missing or damaged" in r.problems


def test_command_reports_per_file(tmp_path):
    (tmp_path / "dev").mkdir()
    cases = [{"case": f"dev-0{i}", "split": "dev", "file": f"dev/dev-0{i}.yaml",
              "reference": META["reference"]} for i in (1, 2, 3)]
    (tmp_path / "manifest.json").write_text(json.dumps({"cases": cases}))
    (tmp_path / "dev" / "dev-01.yaml").write_text(notice_block() + FILLED)
    (tmp_path / "dev" / "dev-02.yaml").write_text(render("dev-02", META, DESCRIPTION))
    out = StringIO()
    call_command("check_labels", dir=tmp_path, stdout=out)
    lines = out.getvalue().splitlines()
    assert lines == ["dev/dev-01.yaml  complete", "dev/dev-02.yaml  incomplete",
                     "  - 8 TODO left", "dev/dev-03.yaml  missing", "1 of 3 complete"]


def test_command_flags_wrong_notice(tmp_path):
    (tmp_path / "dev").mkdir()
    cases = [{"case": "dev-01", "split": "dev", "file": "dev/dev-01.yaml",
              "reference": "cb-0000-00000099"}]
    (tmp_path / "manifest.json").write_text(json.dumps({"cases": cases}))
    (tmp_path / "dev" / "dev-01.yaml").write_text(notice_block() + FILLED)
    out = StringIO()
    call_command("check_labels", dir=tmp_path, stdout=out)
    assert "manifest says cb-0000-00000099" in out.getvalue()
    assert "0 of 1 complete" in out.getvalue()
