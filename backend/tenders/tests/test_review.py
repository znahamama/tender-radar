"""Review queue: permissions, correction checks, human overrides before the rules,
append-only decisions, and the candidate label export."""

import json
from datetime import timedelta
from io import StringIO

import pytest
import yaml
from django.contrib.auth.models import Permission, User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import DatabaseError, connection, transaction
from django.urls import reverse
from django.utils import timezone

from tenders import review
from tenders.eligibility.service import assess
from tenders.evals.labels import check_text
from tenders.extraction.providers import AnthropicExtractor
from tenders.extraction.service import extract_tender
from tenders.models import AppendOnlyError, Assessment, Extraction, ReviewDecision
from tenders.tests.extraction.conftest import (  # noqa: F401  (autouse fixtures)
    DESCRIPTION,
    StubClient,
    make_tender,
    meter,
    no_real_api,
    pin_llm_settings,
    response,
    valid_data,
)

pytestmark = pytest.mark.django_db

# Invented, like the fixture notice it extends.
NO_SA = "No supply arrangement is required to bid on this requirement."
SA_EVIDENCE = "Bidders must hold a valid TBIPS supply arrangement."
QUEUE = "/admin/tenders/needsreview/"


def sa_only_blocker() -> dict:
    """The model says TBIPS is required (the profile holds none), and nothing else blocks."""
    d = valid_data()
    d["eligibility"]["clearance"] = {"corporate": "unknown", "personnel": "unknown",
                                     "evidence": None}
    return d


@pytest.fixture
def extraction(make_tender, meter):  # noqa: F811 (the imported fixtures)
    def make(data=None, **tender_fields):
        tender_fields.setdefault("closing_date", timezone.now() + timedelta(days=10))
        tender_fields.setdefault("description", f"{DESCRIPTION}\n{NO_SA}")
        t = make_tender(**tender_fields)
        llm = AnthropicExtractor(StubClient(response(data or sa_only_blocker())),
                                 retry_wait_s=0)
        x = extract_tender(t, llm, meter).extraction
        assess(x)
        return x
    return make


@pytest.fixture
def reviewer(db):
    return User.objects.create_superuser("reviewer", password="unused-in-tests-1234")


def page(x):
    return reverse("admin:tenders_review_extraction", args=[x.pk])


def status(x):
    return Assessment.objects.get(extraction=x).status


def correct(client, x, gate, evidence="", note="", **fields):
    data = {"gate": gate, "form": "correct", f"{gate}-evidence": evidence,
            f"{gate}-note": note, **{f"{gate}-{k}": v for k, v in fields.items()}}
    return client.post(page(x), data)


def decide(client, x, gate, action, note=""):
    return client.post(page(x), {"gate": gate, "form": "decide", "action": action,
                                 "note": note})


# --- permissions ----------------------------------------------------------------------

def test_anonymous_is_sent_to_login(client, extraction):
    x = extraction()
    for url in (QUEUE, page(x)):
        r = client.get(url)
        assert r.status_code == 302 and r["Location"].startswith("/admin/login/")
    r = decide(client, x, "clearance", "accept")
    assert r.status_code == 302 and not ReviewDecision.objects.exists()


def test_non_staff_is_sent_to_login(client, extraction):
    x = extraction()
    client.force_login(User.objects.create_user("visitor"))
    for url in (QUEUE, page(x)):
        r = client.get(url)
        assert r.status_code == 302 and r["Location"].startswith("/admin/login/")
    decide(client, x, "clearance", "reject")
    assert not ReviewDecision.objects.exists()


def test_staff_without_the_review_permission_is_refused(client, extraction):
    x = extraction()
    client.force_login(User.objects.create_user("clerk", is_staff=True))
    assert client.get(QUEUE).status_code == 403
    assert client.get(page(x)).status_code == 403
    assert decide(client, x, "clearance", "reject").status_code == 403
    assert not ReviewDecision.objects.exists()


def test_staff_with_the_permission_can_review(client, extraction):
    x = extraction()
    u = User.objects.create_user("checker", is_staff=True)
    u.user_permissions.add(Permission.objects.get(codename="add_reviewdecision"))
    client.force_login(u)
    assert client.get(QUEUE).status_code == 200
    r = client.get(page(x))
    assert r.status_code == 200 and b"Save correction" in r.content
    # The admin CSP allows same-origin files only: no inline styles or scripts.
    assert "script-src 'self'" in r["Content-Security-Policy"]
    assert b' style="' not in r.content and b"<script>" not in r.content
    assert decide(client, x, "clearance", "accept").status_code == 302
    assert ReviewDecision.objects.get().reviewer == u


# --- the queue ------------------------------------------------------------------------

def test_queue_order_blocked_then_flagged_then_rest(client, reviewer, extraction):
    clean = valid_data(criteria_location="partly_stated")
    clean["eligibility"]["supply_arrangement_required"] = {
        "value": "no", "name": None, "evidence": NO_SA}
    clean["eligibility"]["clearance"] = {"corporate": "unknown", "personnel": "unknown",
                                         "evidence": None}
    flagged = json.loads(json.dumps(clean))
    flagged["eligibility"]["land_claims_applies"] = {"value": "yes",
                                                     "evidence": "not in this notice at all"}
    soon = timezone.now() + timedelta(days=3)
    rest = extraction(clean, title="Rest", closing_date=soon)
    flag = extraction(flagged, title="Flagged", closing_date=soon + timedelta(days=1))
    blocked = extraction(title="Blocked", closing_date=soon + timedelta(days=2))
    injected = extraction(clean, title="Injected", data_quality=["injection_pattern"],
                          closing_date=soon + timedelta(days=3))
    extraction(clean, title="Closed", tender_status="Expired")  # not open: not listed
    assert [status(x) for x in (rest, flag, blocked)] == [
        "no_blockers_stated", "no_blockers_stated", "blocked"]

    client.force_login(reviewer)
    r = client.get(QUEUE)
    rows = list(r.context["cl"].result_list)
    assert [t.title for t in rows] == ["Blocked", "Flagged", "Injected", "Rest"]
    admin = r.context["cl"].model_admin
    assert admin.flags(rows[1]) == "ungrounded"
    assert admin.flags(rows[2]) == "injection_pattern"
    assert admin.flags(rows[3]) == "-"
    assert admin.status(rows[0]) == "Blocked"
    assert admin.reviewed(rows[0]) == "no"
    # Each notice links through its review link, which opens the current extraction.
    for x in (blocked, injected):
        current = reverse("admin:tenders_review_current",
                          args=[x.tender.reference_number])
        assert f'href="{current}"'.encode() in r.content
        assert client.get(current)["Location"] == page(x)

    decide(client, blocked, "clearance", "accept")
    decide(client, blocked, "clearance", "reject")  # the same gate twice counts once
    rows = list(client.get(QUEUE).context["cl"].result_list)
    assert admin.reviewed(rows[0]) == "partly (1 of 7)"


# --- correction checks ----------------------------------------------------------------

def test_correction_value_must_be_allowed_by_the_schema(extraction):
    x = extraction()
    with pytest.raises(ValidationError) as e:
        review.validate_correction(x, "supply_arrangement_required", {"value": "maybe"}, NO_SA)
    assert "value" in e.value.message_dict
    with pytest.raises(ValidationError) as e:
        review.validate_correction(x, "clearance", {"corporate": "secret",
                                                    "personnel": "classified"}, NO_SA)
    assert "personnel" in e.value.message_dict
    with pytest.raises(ValidationError):
        review.validate_correction(x, "criteria_location", {"value": "somewhere"})
    with pytest.raises(ValidationError):
        review.validate_correction(x, "not_a_gate", {"value": "yes"})


@pytest.mark.parametrize("evidence", [
    "Any supplier may bid on this requirement.",   # not in the notice
    "No supply",                                    # in it, but under 3 words
    "is required to bid. Bidders must hold",        # joined across two sentences
    "",                                             # missing
])
def test_correction_evidence_must_be_grounded(extraction, evidence):
    x = extraction()
    with pytest.raises(ValidationError) as e:
        review.validate_correction(x, "supply_arrangement_required", {"value": "no"}, evidence)
    assert list(e.value.message_dict) == ["evidence"]


def test_correction_detail_must_appear_in_the_notice(extraction):
    x = extraction()
    with pytest.raises(ValidationError) as e:
        review.validate_correction(x, "supply_arrangement_required",
                                   {"value": "yes", "name": "ProServices"}, SA_EVIDENCE)
    assert list(e.value.message_dict) == ["name"]
    with pytest.raises(ValidationError) as e:  # a detail only goes with "yes"
        review.validate_correction(x, "supply_arrangement_required",
                                   {"value": "no", "name": "TBIPS"}, NO_SA)
    assert list(e.value.message_dict) == ["name"]


def test_correction_to_unknown_must_be_a_reject(extraction):
    x = extraction()
    with pytest.raises(ValidationError, match="use Reject"):
        review.validate_correction(x, "land_claims_applies", {"value": "unknown"}, "")


def test_valid_correction_is_stored_like_model_output(extraction):
    x = extraction()
    value, evidence = review.validate_correction(
        x, "supply_arrangement_required", {"value": "yes", "name": " tbips "},
        "bidders  must hold a valid TBIPS supply arrangement.")  # case/space differ: ok
    assert value == {"value": "yes", "name": "tbips"}
    assert evidence == "bidders must hold a valid TBIPS supply arrangement."
    assert review.validate_correction(x, "criteria_location",
                                      {"value": "see_solicitation"}) == (
        {"value": "see_solicitation"}, "")


def test_bad_correction_over_http_shows_errors_and_saves_nothing(client, reviewer, extraction):
    x = extraction()
    client.force_login(reviewer)
    r = correct(client, x, "supply_arrangement_required", value="no",
                evidence="Any supplier may bid on this requirement.")
    assert r.status_code == 200 and b"not an exact quote of the notice" in r.content
    r = correct(client, x, "supply_arrangement_required", value="maybe", evidence=NO_SA)
    assert r.status_code == 200 and b"Select a valid choice" in r.content
    assert not ReviewDecision.objects.exists()
    assert status(x) == "blocked"


# --- human overrides model ------------------------------------------------------------

def test_correcting_sa_yes_to_no_unblocks(client, reviewer, extraction):
    x = extraction()
    a = Assessment.objects.get(extraction=x)
    assert a.status == "blocked"
    assert [b["gate"] for b in a.blockers] == ["supply_arrangement_required"]

    client.force_login(reviewer)
    r = correct(client, x, "supply_arrangement_required", value="no", evidence=NO_SA,
                note="The notice says no SA is needed.")
    assert r.status_code == 302 and r["Location"].endswith("#gate-supply_arrangement_required")
    d = ReviewDecision.objects.get()
    assert (d.action, d.corrected_value, d.evidence, d.reviewer) == (
        "correct", {"value": "no", "name": None}, NO_SA, reviewer)
    a = Assessment.objects.get(extraction=x)
    assert a.status == "no_blockers_stated" and a.blockers == []
    # The stored extraction (model output) is untouched.
    x.refresh_from_db()
    assert x.data["eligibility"]["supply_arrangement_required"]["value"] == "yes"
    assert x.raw_output["eligibility"]["supply_arrangement_required"]["value"] == "yes"

    # The public page shows the reviewed value.
    r = client.get(reverse("tender-detail", args=[x.tender.reference_number]))
    html = r.content.decode()
    assert "Reviewed by a person" in html and "model said: yes (TBIPS)" in html
    assert NO_SA in html and "A person reviewed 1 of the model" in html


def test_reject_treats_the_gate_as_unknown(client, reviewer, extraction):
    x = extraction()
    client.force_login(reviewer)
    decide(client, x, "supply_arrangement_required", "reject")
    a = Assessment.objects.get(extraction=x)
    assert a.status == "no_blockers_stated"
    assert "supply_arrangement_required" in a.not_stated
    r = client.get(reverse("tender-detail", args=[x.tender.reference_number]))
    assert b"the model&#x27;s answer was marked wrong" in r.content


def test_newer_decision_supersedes_older(client, reviewer, extraction):
    x = extraction()
    client.force_login(reviewer)
    correct(client, x, "supply_arrangement_required", value="no", evidence=NO_SA)
    assert status(x) == "no_blockers_stated"
    decide(client, x, "supply_arrangement_required", "accept")  # back to the model's "yes"
    assert status(x) == "blocked"
    assert ReviewDecision.objects.count() == 2  # the correction stays in the log
    assert review.latest_decisions(x)["supply_arrangement_required"].action == "accept"


def test_criteria_location_correct_and_reject(client, reviewer, extraction):
    clean = valid_data()
    clean["eligibility"]["supply_arrangement_required"] = {
        "value": "no", "name": None, "evidence": NO_SA}
    clean["eligibility"]["clearance"] = {"corporate": "unknown", "personnel": "unknown",
                                         "evidence": None}
    x = extraction(clean)
    assert status(x) == "no_blockers_stated"  # criteria partly_stated
    client.force_login(reviewer)
    correct(client, x, "criteria_location", value="see_solicitation")
    assert status(x) == "check_solicitation"
    correct(client, x, "criteria_location", value="stated_in_notice")
    assert status(x) == "no_blockers_stated"
    decide(client, x, "criteria_location", "reject")  # no "unknown": check the solicitation
    assert status(x) == "check_solicitation"


def test_metadata_still_requires_the_arrangement(client, reviewer, extraction):
    """The metadata override is a frozen rule, not model output: a person's "no" can't
    lift it."""
    x = extraction(sa_required_from_metadata=True, notice_type="RFP against Supply Arrangement",
                   notice_kind="against_sa")
    client.force_login(reviewer)
    correct(client, x, "supply_arrangement_required", value="no", evidence=NO_SA)
    assert status(x) == "blocked"


def test_failed_extraction_has_nothing_to_review(client, reviewer, extraction):
    x = extraction()
    Extraction.objects.filter(pk=x.pk).update(status="failed", data=None, raw_output=None,
                                              error="boom")
    client.force_login(reviewer)
    r = client.get(page(x))
    assert r.status_code == 200 and b"no model answer to review" in r.content
    assert decide(client, x, "clearance", "reject").status_code == 400
    assert not ReviewDecision.objects.exists()


def test_unreviewed_assessment_hash_is_unchanged(extraction):
    """Adding reviews must not rewrite every existing assessment on the next run."""
    x = extraction()
    a = Assessment.objects.get(extraction=x)
    again = assess(x)
    assert again.inputs_hash == a.inputs_hash and again.computed_at == a.computed_at


# --- append-only ----------------------------------------------------------------------

def test_decisions_are_append_only(reviewer, extraction):
    x = extraction()
    d = ReviewDecision.objects.create(extraction=x, gate="clearance", action="accept",
                                      reviewer=reviewer)
    d.note = "edited"
    with pytest.raises(AppendOnlyError):
        d.save()
    with pytest.raises(AppendOnlyError):
        d.delete()
    with pytest.raises(AppendOnlyError):
        ReviewDecision.objects.filter(pk=d.pk).update(note="edited")
    with pytest.raises(AppendOnlyError):
        ReviewDecision.objects.all().delete()
    # And in the database, whatever the code path.
    for sql in ("UPDATE tenders_reviewdecision SET note = 'x'",
                "DELETE FROM tenders_reviewdecision"):
        with (pytest.raises(DatabaseError, match="append-only"), transaction.atomic(),
              connection.cursor() as cur):
            cur.execute(sql)
    # An extraction with decisions can't be deleted out from under them.
    with pytest.raises(DatabaseError), transaction.atomic():
        x.delete()
    assert ReviewDecision.objects.get().note == ""


def test_admin_log_is_read_only(client, reviewer, extraction):
    x = extraction()
    d = ReviewDecision.objects.create(extraction=x, gate="clearance", action="accept",
                                      reviewer=reviewer)
    client.force_login(reviewer)
    assert client.get("/admin/tenders/reviewdecision/").status_code == 200
    assert client.get("/admin/tenders/reviewdecision/add/").status_code == 403
    assert client.post(f"/admin/tenders/reviewdecision/{d.pk}/delete/",
                       {"post": "yes"}).status_code == 403
    assert ReviewDecision.objects.filter(pk=d.pk).exists()


def test_correct_needs_a_value_and_only_correct_has_one(reviewer, extraction):
    x = extraction()
    for bad in ({"action": "correct"}, {"action": "accept", "corrected_value": {"value": "no"}},
                {"action": "reject", "evidence": "a quote"}):
        with pytest.raises(DatabaseError), transaction.atomic():
            ReviewDecision.objects.create(extraction=x, gate="clearance", reviewer=reviewer,
                                          **bad)


# --- export ---------------------------------------------------------------------------

def export(tmp_path, eval_refs=()):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"cases": [{"reference": r} for r in eval_refs]}))
    out = StringIO()
    call_command("export_review_labels", "--dir", str(tmp_path / "reviewed"),
                 "--manifest", str(manifest), stdout=out)
    return out.getvalue()


def test_export_writes_corrections_as_candidate_labels(client, reviewer, extraction, tmp_path):
    x = extraction(reference_number="cb-0000-00000042")
    other = extraction()  # accepted only: not exported
    client.force_login(reviewer)
    correct(client, x, "supply_arrangement_required", value="no", evidence=NO_SA)
    correct(client, x, "work_location", on_site="yes", place="St. John's",
            evidence="The work will be performed on-site at St. John's")
    correct(client, x, "criteria_location", value="see_solicitation")
    decide(client, x, "clearance", "accept")
    decide(client, x, "land_claims_applies", "reject")
    decide(client, other, "clearance", "accept")

    out = export(tmp_path)
    path = tmp_path / "reviewed" / "cb-0000-00000042.yaml"
    assert sorted(p.name for p in (tmp_path / "reviewed").iterdir()) == [path.name]
    assert "1 written, 0 skipped" in out

    text = path.read_text()
    assert text.startswith("# =====") and "# Eval case reviewed-cb-0000-00000042." in text
    assert "# reference: cb-0000-00000042" in text
    labels = yaml.safe_load(text)
    assert labels["criteria_location"] == "see_solicitation"
    g = labels["gates"]
    assert g["supply_arrangement_required"] == {"value": False, "name": None,
                                                "evidence": NO_SA}  # YAML 1.1: bare no
    assert g["work_location"] == {"on_site": True, "place": "St. John's",
                                  "evidence": "The work will be performed on-site at St. John's"}
    # No model output: accepted, rejected and unreviewed gates are left to a labeller.
    assert g["clearance"] == {"corporate": "TODO", "personnel": "TODO", "evidence": None}
    assert g["land_claims_applies"] == {"value": "TODO", "evidence": None}
    assert g["documents_behind_login"]["value"] == "TODO"
    assert "model answer accepted (review decision" in text
    assert "model answer rejected (review decision" in text
    # Readable by the dev/test label checker: only the TODOs are left.
    report = check_text(path, text)
    assert report.problems == [] and report.todo == 5
    assert "written, 5 TODO left" in out

    # Never overwritten.
    path.write_text(text + "# labeller's edits\n")
    assert "exists, not overwritten" in export(tmp_path)
    assert path.read_text().endswith("# labeller's edits\n")


def test_export_never_includes_a_dev_or_test_notice(client, reviewer, extraction, tmp_path):
    x = extraction(reference_number="cb-1111-00000001")
    client.force_login(reviewer)
    correct(client, x, "supply_arrangement_required", value="no", evidence=NO_SA)
    out = export(tmp_path, eval_refs=["cb-1111-00000001"])
    assert "a dev/test case, skipped" in out and not (tmp_path / "reviewed").exists()


def test_superseded_correction_is_not_exported(client, reviewer, extraction, tmp_path):
    x = extraction()
    client.force_login(reviewer)
    correct(client, x, "supply_arrangement_required", value="no", evidence=NO_SA)
    decide(client, x, "supply_arrangement_required", "accept")
    assert "0 written" in export(tmp_path)


# --- detail page display --------------------------------------------------------------

def gate_row(client, x, label):
    r = client.get(reverse("tender-detail", args=[x.tender.reference_number]))
    rows = {row.label: row for row in r.context["rows"]}
    return r, rows[label]


def test_corrected_row_shows_model_said_and_flags_as_the_models(client, reviewer, extraction):
    d = sa_only_blocker()
    # The model's answer is right, but its quote isn't in the notice: grounding resets it.
    d["eligibility"]["documents_behind_login"] = {
        "value": "yes", "portal": "CanadaBuys",
        "evidence": "documents are only on a login portal"}
    x = extraction(d)
    client.force_login(reviewer)
    _, before = gate_row(client, x, "Documents behind a login")
    assert before.flags == ["quote not found in the notice: treated as unknown"]

    correct(client, x, "documents_behind_login", value="yes", portal="CanadaBuys",
            evidence="Bid documents are available on CanadaBuys.")
    r, row = gate_row(client, x, "Documents behind a login")
    # The person's value matches the model's words, and "model said" is still shown.
    assert row.value == row.raw_value == "yes (CanadaBuys)"
    assert row.flags == ["model's answer: quote not found in the notice: treated as unknown"]
    assert row.reviewed == "Reviewed by a person"
    html = r.content.decode()
    assert "model said: yes (CanadaBuys)" in html
    assert "model&#x27;s answer: quote not found in the notice" in html
    # An unreviewed gate keeps its flags as they are.
    _, sa = gate_row(client, x, "Supply arrangement required")
    assert sa.reviewed == "" and not any(f.startswith("model's") for f in sa.flags)


def test_rejected_row_also_marks_flags_as_the_models(client, reviewer, extraction):
    d = sa_only_blocker()
    d["eligibility"]["land_claims_applies"] = {"value": "yes",
                                               "evidence": "not in this notice at all"}
    x = extraction(d)
    client.force_login(reviewer)
    decide(client, x, "land_claims_applies", "reject")
    _, row = gate_row(client, x, "Land claims agreement")
    assert row.value == "unknown" and row.raw_value == "yes"
    assert row.flags == ["model's answer: quote not found in the notice: treated as unknown"]


def test_accepted_row_is_shown_as_the_model_answered(client, reviewer, extraction):
    x = extraction()
    client.force_login(reviewer)
    decide(client, x, "supply_arrangement_required", "accept")
    _, row = gate_row(client, x, "Supply arrangement required")
    assert row.reviewed == "" and row.raw_value == "" and row.value == "yes (TBIPS)"


def not_stated(client, x):
    r = client.get(reverse("tender-detail", args=[x.tender.reference_number]))
    return r.context["not_stated"]


def test_not_stated_names_the_unknown_clearance_side(client, reviewer, extraction):
    x = extraction(valid_data())  # corporate reliability (DOS), personnel unknown
    assert "clearance" in Assessment.objects.get(extraction=x).not_stated
    assert "Security clearance (personnel)" in not_stated(client, x)

    both = extraction()  # sa_only_blocker: corporate and personnel unknown
    labels = not_stated(client, both)
    assert "Security clearance" in labels
    assert not any(label.startswith("Security clearance (") for label in labels)

    # Display follows the reviewed values: a person states personnel, corporate unknown.
    client.force_login(reviewer)
    correct(client, both, "clearance", corporate="unknown", personnel="reliability",
            evidence="valid Designated Organization Screening (DOS)")
    assert "Security clearance (corporate)" in not_stated(client, both)
