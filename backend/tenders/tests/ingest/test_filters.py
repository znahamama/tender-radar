import pytest

from tenders.ingest.filters import is_it, is_national, is_nl


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
def test_is_it(codes, expected):
    assert is_it(codes) is expected


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
