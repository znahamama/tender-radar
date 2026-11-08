import re
from datetime import datetime

import pytest
from pydantic import ValidationError

from tenders.ingest.pipeline import IngestStats, parse_file
from tenders.ingest.schema import SOURCE_TZ, TenderNotice

from .conftest import OPEN_FAKE

FORBIDDEN_FIELD = re.compile(r"contact|email|phone|address|authority", re.IGNORECASE)
# Header fragments that identify contact columns in the real CanadaBuys files.
FORBIDDEN_ALIAS = re.compile(r"contact|courriel|email|phone|telephone|fax|address|adresse|postal", re.IGNORECASE)


def _notices():
    return {n.reference_number: n for n in parse_file(OPEN_FAKE, IngestStats())}


def test_model_fields_are_contact_free():
    bad = [name for name in TenderNotice.model_fields if FORBIDDEN_FIELD.search(name)]
    assert bad == []


def test_model_aliases_do_not_map_contact_columns():
    bad = [f.alias for f in TenderNotice.model_fields.values() if FORBIDDEN_ALIAS.search(f.alias or "")]
    assert bad == []


def test_model_ignores_extra_columns():
    assert TenderNotice.model_config["extra"] == "ignore"


def test_allowlist_drops_contact_values_from_parsed_rows():
    raw = OPEN_FAKE.read_text(encoding="utf-8-sig")
    assert "Test Person" in raw and "test@example.com" in raw  # the fixture really has contacts
    notices = _notices()
    assert notices
    dumped = repr([n.model_dump() for n in notices.values()])
    for fake in ("Test Person", "test@example.com", "709-555-0100", "1 Fake Street", "A1A 1A1",
                 "2 Example Road", "3 Sample Ave", "Testville"):
        assert fake not in dumped
    assert "(fr)" not in dumped  # French duplicates are dropped too


def test_dates_parse_to_aware_eastern_datetimes():
    n = _notices()["FAKE-NL-1"]
    assert n.closing_date == datetime(2026, 12, 1, 14, 0, tzinfo=SOURCE_TZ)
    assert n.closing_date.utcoffset().total_seconds() == -5 * 3600  # EST in December
    assert n.publication_date == datetime(2026, 10, 1, tzinfo=SOURCE_TZ)
    assert n.publication_date.tzinfo is not None


def test_explicit_offset_is_respected():
    n = TenderNotice.model_validate({
        "referenceNumber-numeroReference": "R", "title-titre-eng": "T",
        "tenderClosingDate-appelOffresDateCloture": "2026-12-01T14:00:00-03:30",
    })
    assert n.closing_date.utcoffset().total_seconds() == -3.5 * 3600


def test_empty_strings_become_none():
    n = _notices()["FAKE-EMPTY-1"]
    assert n.description is None
    assert n.notice_url is None  # whitespace-only
    assert n.notice_type is None
    assert n.buyer_org_name is None
    assert n.publication_date is None
    assert n.regions_of_delivery == []
    assert n.amendment_date is None


def test_multi_value_cells_split_on_newline_and_strip_star():
    n = _notices()["FAKE-MULTI-1"]
    assert n.unspsc_codes == ["30000000", "43233200"]
    assert n.procurement_categories == ["SRV"]
    assert _notices()["FAKE-IT-1"].gsin_codes == ["N7010"]


def test_amendment_number_parses_zero_padded():
    assert _notices()["FAKE-DUP-1"].amendment_number == 1
    assert _notices()["FAKE-NL-1"].amendment_number == 0


@pytest.mark.parametrize("missing", ["referenceNumber-numeroReference", "title-titre-eng"])
def test_required_fields(missing):
    data = {"referenceNumber-numeroReference": "R", "title-titre-eng": "T", missing: "  "}
    with pytest.raises(ValidationError):
        TenderNotice.model_validate(data)
