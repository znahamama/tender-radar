import pytest

from tenders.eligibility.places import Where, classify, find_place


@pytest.mark.parametrize(("place", "where"), [
    ("St. John's", Where.ST_JOHNS_AREA),
    ("st johns", Where.ST_JOHNS_AREA),
    ("St. John's, NL", Where.ST_JOHNS_AREA),
    ("Mount Pearl", Where.ST_JOHNS_AREA),
    ("Paradise, NL", Where.ST_JOHNS_AREA),
    ("CBS", Where.ST_JOHNS_AREA),
    ("Conception Bay South", Where.ST_JOHNS_AREA),
    ("Torbay", Where.ST_JOHNS_AREA),
    ("Portugal Cove-St. Philip's", Where.ST_JOHNS_AREA),
    ("Gander, NL", Where.NL_OTHER),
    ("Happy Valley-Goose Bay", Where.NL_OTHER),
    ("Labrador City", Where.NL_OTHER),
    ("somewhere in Newfoundland", Where.NL_OTHER),
    ("Ottawa", Where.OUTSIDE_NL),
    ("Gatineau, QC", Where.OUTSIDE_NL),
    ("Kanata, ON", Where.OUTSIDE_NL),
    ("Saint John, NB", Where.OUTSIDE_NL),  # not St. John's
    ("Halifax, N.S.", Where.OUTSIDE_NL),
    ("National Capital Region", Where.OUTSIDE_NL),
    ("St. John's or Ottawa", Where.MIXED),
    ("Office on Water Street", Where.UNKNOWN),  # "on" is not Ontario
    ("Building 12", Where.UNKNOWN),
])
def test_classify(place, where):
    assert classify(place) is where


def test_find_place_keeps_original_spelling():
    assert find_place("Staff must work at the St. John's office, then Gander.") == "St. John's"
    assert find_place("Delivery to MOUNT PEARL depot") == "MOUNT PEARL"
    assert find_place("Delivered across Ontario") is None  # provinces are not places
    assert find_place("no place here") is None
