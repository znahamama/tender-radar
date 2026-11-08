import re
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from tenders.ingest.pipeline import IngestStats, parse_file
from tenders.ingest.schema import (
    SOURCE_TZ,
    TenderNotice,
    normalize_text,
    quality_flags,
)

from .conftest import FY_FAKE, OPEN_FAKE

FORBIDDEN_FIELD = re.compile(r"contact|email|phone|address|authority|fax|hint", re.IGNORECASE)
# Header fragments that identify contact columns in the real CanadaBuys files.
FORBIDDEN_ALIAS = re.compile(
    r"contact|courriel|email|phone|telephone|fax|address|adresse|postal", re.IGNORECASE
)
NOW = datetime(2026, 11, 7, tzinfo=UTC)


def _notices(path=OPEN_FAKE):
    return {n.reference_number: n for n in parse_file(path, IngestStats(), NOW)}


def _minimal(**extra):
    return TenderNotice.model_validate(
        {"referenceNumber-numeroReference": "R-1", "title-titre-eng": "T", **extra},
        context={"now": NOW},
    )


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
    for fake in ("Test Person", "Person, Test", "test@example.com", "709-555-0100", "(709) 555-0100",
                 "709.555.0100", "1 Fake Street", "A1A 1A1", "2 Example Road", "3 Sample Ave",
                 "Testville", "helpdesk.fake@example.gc.ca", "1-800-555-0123"):
        assert fake not in dumped, fake
    assert "(fr)" not in dumped  # French duplicates are dropped too


def test_dates_are_eastern_converted_to_utc():
    n = _notices()["FAKE-NL-1"]
    assert n.closing_date == datetime(2026, 12, 1, 14, 0, tzinfo=SOURCE_TZ)
    assert n.closing_date.tzinfo is UTC
    assert n.closing_date.hour == 19  # 14:00 EST == 19:00 UTC
    assert n.publication_date == datetime(2026, 10, 1, tzinfo=SOURCE_TZ)
    assert n.publication_date.tzinfo is UTC
    assert n.publication_date.hour == 4  # midnight EDT == 04:00 UTC


def test_explicit_offset_is_respected():
    n = _minimal(**{"tenderClosingDate-appelOffresDateCloture": "2026-12-01T14:00:00-03:30"})
    assert n.closing_date == datetime(2026, 12, 1, 17, 30, tzinfo=UTC)


def test_empty_strings_become_none():
    n = _notices()["FAKE-EMPTY-1"]
    assert n.description is None
    assert n.notice_type is None
    assert n.buyer_org_name is None
    assert n.publication_date is None
    assert n.regions_of_delivery == []
    assert n.amendment_date is None


def test_none_literals_become_none_or_empty_list():
    n = _notices()["FAKE-IT-2"]
    assert n.limited_tendering_reason is None
    assert n.trade_agreements == []
    n = _minimal(**{"tradeAgreements-accordsCommerciaux-eng": "*None\n*Canadian Free Trade Agreement (CFTA)"})
    assert n.trade_agreements == ["Canadian Free Trade Agreement (CFTA)"]


def test_normalize_text_unescapes_entities_and_newlines():
    assert normalize_text("a &amp; b&nbsp;c&#64;d\r\ne\rf") == "a & b c@d\ne\nf"
    assert normalize_text(None) == ""


def test_description_is_normalized_in_pipeline():
    d = _notices()["FAKE-IT-2"].description
    assert "&amp;" not in d and "&nbsp;" not in d and "\r" not in d
    assert "services & support" in d


def test_notice_url_falls_back_to_canadabuys_page():
    n = _notices()["FAKE-EMPTY-1"]  # whitespace-only URL in the CSV
    assert n.notice_url == "https://canadabuys.canada.ca/en/tender-opportunities/tender-notice/FAKE-EMPTY-1"
    assert n.notice_url_derived is True
    kept = _notices()["FAKE-NL-1"]
    assert kept.notice_url == "https://example.com/notice/FAKE-NL-1"
    assert kept.notice_url_derived is False


@pytest.mark.parametrize(
    "pub, close, expected",
    [
        ((2026, 10, 1), (2026, 12, 1), []),
        (None, (2026, 12, 1), ["no_publication_date"]),
        ((2026, 10, 10), (2026, 10, 1), ["closes_before_published"]),
        ((2026, 10, 1), (2099, 12, 31), ["placeholder_closing_date"]),
        ((2026, 10, 1), (2030, 1, 1), ["placeholder_closing_date"]),  # > 3 years out
        ((2026, 10, 1), (2029, 6, 1), []),  # < 3 years out
        ((2026, 10, 1), None, ["no_closing_date"]),
    ],
)
def test_quality_flags(pub, close, expected):
    def dt(t):
        return datetime(*t, tzinfo=UTC) if t else None

    assert quality_flags(dt(pub), dt(close), NOW) == expected


def test_quality_flags_from_fixture():
    assert _notices()["FAKE-GSIN-1"].data_quality == ["placeholder_closing_date"]
    assert _notices(FY_FAKE)["FAKE-SO-1"].data_quality == ["closes_before_published"]


def test_multi_value_cells_split_on_newline_and_strip_star():
    n = _notices()["FAKE-MULTI-1"]
    assert n.unspsc_codes == ["30000000", "43233200"]
    assert n.procurement_categories == ["SRV"]
    assert _notices()["FAKE-GSIN-1"].gsin_codes == ["D302AAP"]


def test_amendment_number_parses_zero_padded():
    assert _notices()["FAKE-DUP-1"].amendment_number == 1
    assert _notices()["FAKE-NL-1"].amendment_number == 0


@pytest.mark.parametrize("missing", ["referenceNumber-numeroReference", "title-titre-eng"])
def test_required_fields(missing):
    data = {"referenceNumber-numeroReference": "R", "title-titre-eng": "T", missing: "  "}
    with pytest.raises(ValidationError):
        TenderNotice.model_validate(data)
