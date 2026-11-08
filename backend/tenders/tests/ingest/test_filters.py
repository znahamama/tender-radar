import pytest

from tenders.ingest.filters import (
    is_dropped_regional_it,
    is_it,
    is_kept,
    is_national,
    is_nl,
    notice_kind,
    region_scope,
)
from tenders.ingest.schema import TenderNotice


def _n(regions=(), unspsc=(), gsin=(), **kw):
    return TenderNotice(
        reference_number="R", title=kw.pop("title", "T"), regions_of_delivery=list(regions),
        unspsc_codes=list(unspsc), gsin_codes=list(gsin), **kw,
    )


@pytest.mark.parametrize(
    "codes, expected",
    [
        (["43211500"], True),  # computer equipment
        (["4321"], False),  # truncated code
        (["81111500"], True),  # software/hardware engineering
        (["81121500"], False),  # economic analysis, not 8111
        (["43"], False),
        (["4321150099"], False),  # too long
        (["*43211500"], True),  # raw star prefix tolerated
        (["30000000", "43233200"], True),  # multi-code, IT second
        (["30000000", "81121500"], False),
        ([], False),
    ],
)
def test_is_it_unspsc(codes, expected):
    assert is_it(codes) is expected


@pytest.mark.parametrize(
    "gsin, expected",
    [
        (["D302AAP"], True),  # informatics professional services
        (["D302AAR"], True),
        (["N7030"], True),  # ADP software
        (["N7025405"], True),  # LAN systems
        (["JX7030"], True),  # ADP software maintenance
        (["WL7025AJ"], True),  # ADP printer lease
        (["D304A"], False),  # data transmission (telecom)
        (["D316B"], False),  # telecom network services
        (["D399B"], False),  # voice telecom
        (["D317E"], False),  # information products
        (["N7110"], False),  # office furniture
        (["JX5805B"], False),  # telecom switching maintenance
        (["N5820D", "d302aap"], True),  # multi-code, lowercase
    ],
)
def test_is_it_gsin(gsin, expected):
    assert is_it([], gsin) is expected


@pytest.mark.parametrize(
    "regions, expected",
    [
        (["Newfoundland and Labrador"], True),
        (["Ontario (except NCR)", "Newfoundland and Labrador"], True),
        (["St. John's"], True),
        (["St John’s"], True),  # curly apostrophe, no period
        (["Labrador City"], True),
        (["Happy Valley-Goose Bay"], True),
        (["Charlottetown (Labrador)"], True),
        (["Saint John"], False),  # New Brunswick
        (["Charlottetown"], False),  # PEI
        (["Canada"], False),  # national is counted separately, not as NL
        ([], False),
    ],
)
def test_is_nl(regions, expected):
    assert is_nl(regions) is expected


@pytest.mark.parametrize(
    "regions, expected",
    [
        (["Canada"], True),
        (["Canada", "Nova Scotia"], True),
        (["National Capital Region (NCR)"], False),
        (["World"], False),
        ([], False),
    ],
)
def test_is_national(regions, expected):
    assert is_national(regions) is expected


@pytest.mark.parametrize(
    "regions, expected",
    [
        (["Newfoundland and Labrador"], "nl"),
        (["St. John's", "Nova Scotia"], "nl"),
        (["Canada"], "national"),
        (["Canada", "Newfoundland and Labrador"], "nl+national"),
        (["Ontario (except NCR)"], None),
        ([], None),
    ],
)
def test_region_scope(regions, expected):
    assert region_scope(regions) == expected


@pytest.mark.parametrize(
    "regions, unspsc, gsin, kept, dropped_it",
    [
        (["Newfoundland and Labrador"], ["72101500"], [], True, False),  # NL, any category
        (["Canada"], ["43211500"], [], True, False),  # national IT
        (["Canada", "Gander"], [], ["D302AAP"], True, False),  # NL+national IT via GSIN
        (["St. John's"], ["81111500"], [], True, False),  # NL IT
        (["Ontario (except NCR)"], ["43211500"], [], False, True),  # regional IT: dropped
        (["Saint John"], [], ["N7030"], False, True),  # NB only: dropped
        (["Canada"], ["72101500"], [], False, False),  # national non-IT: not kept
        (["Canada"], [], ["D399B"], False, False),  # national telecom: not IT
        ([], ["43211500"], [], False, True),  # IT with no delivery region: dropped
    ],
)
def test_is_kept(regions, unspsc, gsin, kept, dropped_it):
    n = _n(regions, unspsc, gsin)
    assert is_kept(n) is kept
    assert is_dropped_regional_it(n) is dropped_it


@pytest.mark.parametrize(
    "kw, expected",
    [
        ({"notice_type": "Request for Standing Offer"}, "standing_or_ongoing"),
        ({"notice_type": "RFP against Supply Arrangement"}, "standing_or_ongoing"),
        ({"title": "Ongoing qualification for IT pros"}, "standing_or_ongoing"),
        ({"description": "Open to pre-qualified suppliers only."}, "standing_or_ongoing"),
        ({"description": "Only prequalified firms may bid."}, "standing_or_ongoing"),
        ({"data_quality": ["placeholder_closing_date"]}, "standing_or_ongoing"),
        ({"notice_type": "Request for Proposal", "title": "Laptop refresh"}, "regular"),
        ({"description": "Supply of ten arrangements of flowers"}, "regular"),
    ],
)
def test_notice_kind(kw, expected):
    n = _n()
    for k, v in kw.items():
        setattr(n, k, v)  # set after validation: data_quality is derived on validate
    assert notice_kind(n) == expected
