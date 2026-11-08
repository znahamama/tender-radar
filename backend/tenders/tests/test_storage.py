import re
from datetime import UTC, datetime, timedelta
from io import StringIO

import pytest
from django.core.management import call_command

from tenders.ingest.pipeline import run
from tenders.ingest.storage import upsert
from tenders.models import Tender

from .ingest.conftest import FY_FAKE, OPEN_FAKE

NOW = datetime(2026, 11, 7, 12, tzinfo=UTC)
CONTACT_FIELD = re.compile(r"contact|email|phone|fax|address|authority|hint", re.IGNORECASE)
FAKE_CONTACTS = ("Test Person", "Person, Test", "test@example.com", "Test.Person", "555-0100",
                 "555 0100", "555.0100", "1 Fake Street", "A1A 1A1", "helpdesk.fake")


def _kept(now=NOW):
    return run([OPEN_FAKE, FY_FAKE], now=now).kept


def test_tender_model_has_no_contact_columns_or_attributes():
    names = [f.name for f in Tender._meta.get_fields()] + [
        f.column for f in Tender._meta.concrete_fields
    ]
    assert [n for n in names if CONTACT_FIELD.search(n)] == []
    assert not any(CONTACT_FIELD.search(a) for a in vars(Tender) if not a.startswith("_"))


@pytest.mark.django_db
def test_stored_rows_hold_no_contact_hints_anywhere():
    upsert(_kept(), now=NOW)
    assert Tender.objects.count() == 6
    for t in Tender.objects.all():
        assert not [a for a in vars(t) if CONTACT_FIELD.search(a)]
        blob = repr({f.attname: getattr(t, f.attname) for f in Tender._meta.concrete_fields})
        for fake in FAKE_CONTACTS:
            assert fake not in blob, (t.reference_number, fake)
    # Known, deliberate gap: URLs are never altered, so digits inside a URL survive.
    it2 = Tender.objects.get(reference_number="FAKE-IT-2").description
    assert "https://example.com/tender/7095550100?contact=1" in it2


@pytest.mark.django_db
def test_upsert_is_idempotent_and_bulk(django_assert_max_num_queries):
    with django_assert_max_num_queries(6):  # select + insert + (empty) updates, not per-row
        first = upsert(_kept(), now=NOW)
    assert (first.created, first.updated, first.unchanged) == (6, 0, 0)

    later = NOW + timedelta(hours=1)
    with django_assert_max_num_queries(6):
        second = upsert(_kept(), now=later)
    assert (second.created, second.updated, second.unchanged) == (0, 0, 6)
    t = Tender.objects.get(reference_number="FAKE-NL-1")
    assert t.first_seen_at == NOW and t.updated_at == NOW and t.last_seen_at == later


@pytest.mark.django_db
def test_upsert_updates_only_changed_rows():
    upsert(_kept(), now=NOW)
    notices = _kept()
    changed = next(n for n in notices if n.reference_number == "FAKE-CITY-1")
    changed.title = "City-only St. John's delivery (amended)"
    later = NOW + timedelta(days=1)
    r = upsert(notices, now=later)
    assert (r.created, r.updated, r.unchanged) == (0, 1, 5)
    t = Tender.objects.get(reference_number="FAKE-CITY-1")
    assert t.title.endswith("(amended)") and t.updated_at == later and t.first_seen_at == NOW


@pytest.mark.django_db
def test_stored_values():
    upsert(_kept(), now=NOW)
    gsin = Tender.objects.get(reference_number="FAKE-GSIN-1")
    assert gsin.is_it and gsin.is_nl
    assert gsin.region_scope == "nl+national"
    assert gsin.notice_kind == "standing_or_ongoing"
    assert gsin.data_quality == ["placeholder_closing_date"]
    assert gsin.notice_url_derived
    assert gsin.notice_url.endswith("/tender-notice/FAKE-GSIN-1")
    assert gsin.redaction_counts == {"name": 0, "email": 1, "phone": 1, "contact_block": 0}
    nl = Tender.objects.get(reference_number="FAKE-NL-1")
    assert "[CONTACT_BLOCK]" in nl.description and "[PHONE]" in nl.description
    assert nl.closing_date == datetime(2026, 12, 1, 19, tzinfo=UTC)
    assert Tender.objects.get(reference_number="FAKE-SO-1").data_quality == ["closes_before_published"]


@pytest.mark.django_db
def test_command_save_twice_is_idempotent():
    args = ("ingest_canadabuys", "--file", str(OPEN_FAKE), str(FY_FAKE), "--save")
    out1, out2 = StringIO(), StringIO()
    call_command(*args, stdout=out1)
    call_command(*args, stdout=out2)
    assert "saved: created 6, updated 0, unchanged 0" in out1.getvalue()
    assert "saved: created 0, updated 0, unchanged 6" in out2.getvalue()


@pytest.mark.django_db
def test_admin_is_read_only(admin_client):
    upsert(_kept(), now=NOW)
    t = Tender.objects.get(reference_number="FAKE-NL-1")
    assert admin_client.get("/admin/tenders/tender/").status_code == 200
    assert admin_client.get("/admin/tenders/tender/?q=FAKE-NL").status_code == 200
    assert admin_client.get(f"/admin/tenders/tender/{t.pk}/change/").status_code == 200  # view
    assert admin_client.get("/admin/tenders/tender/add/").status_code == 403
    assert admin_client.get(f"/admin/tenders/tender/{t.pk}/delete/").status_code == 403
    resp = admin_client.post(f"/admin/tenders/tender/{t.pk}/change/", {"title": "hacked"})
    assert resp.status_code == 403
    t.refresh_from_db()
    assert t.title != "hacked"
