from tenders.ingest.audit import ContactIndex, audit
from tenders.ingest.pipeline import run

from .conftest import FY_FAKE, OPEN_FAKE

FAKE_VALUES = ("Alex Buyer", "Buyer, Alex", "Test Person", "test@example.com", "7095550100",
               "709", "555")


def _audit(items=None):
    index = ContactIndex.from_csvs([OPEN_FAKE, FY_FAKE])
    if items is None:
        items = [(n.reference_number, n.description, n.title)
                 for n in run([OPEN_FAKE, FY_FAKE]).kept]
    return audit(items, index), index


def test_contact_name_from_another_row_is_flagged():
    # FAKE-SO-1's contact is "Buyer, Alex (SPAC/PSPC)"; FAKE-CITY-1's description mentions
    # "Alex Buyer". Row-level redaction can't know that name, so only the audit catches it.
    report, _ = _audit()
    assert report.refs[("description", "known_name")] == ["FAKE-CITY-1"]


def test_known_phone_inside_url_is_flagged():
    # Documented redactor gap: URLs are never altered. The audit looks inside them anyway.
    report, _ = _audit()
    assert report.refs[("description", "known_phone")] == ["FAKE-IT-2"]


def test_fixture_has_no_other_hits():
    report, _ = _audit()
    assert set(report.refs) == {("description", "known_name"), ("description", "known_phone")}
    assert report.scanned == 6


def test_index_repr_hides_values():
    _, index = _audit()
    assert index.names and index.emails and index.phones
    text = repr(index) + str(index)
    assert not any(v in text for v in FAKE_VALUES)


def test_name_hint_skipping_rule_is_shared_with_redactor():
    index = ContactIndex.from_csvs([OPEN_FAKE, FY_FAKE])
    assert "alex buyer" in index.names and "buyer, alex" in index.names
    assert all(" " in n or "," in n for n in index.names)  # no single tokens


def _hits(text, index):
    return {k: v for k, v in index.detect(text).items() if v}


def test_independent_detectors():
    index = ContactIndex.from_csvs([OPEN_FAKE, FY_FAKE])
    assert _hits("write to jane@example.org", index) == {"at_sign": 1}
    assert _hits("write to jane @ example.org", index) == {"at_sign": 1}
    assert _hits("bids due June 3 @ 2:00PM", index) == {}
    assert _hits("call 613 555 0123 today", index) == {"long_digits": 1}
    assert _hits("call 16135550123 today", index) == {"long_digits": 1}
    assert _hits("Contracting Authority: Jane Doe", index) == {"name_label": 1}
    assert _hits("DND POC: Jane Doe", index) == {"name_label": 1}
    assert _hits("for deliveryContracting Authority: Jane DoeEmail: [EMAIL]", index) == {"name_label": 1}
    assert _hits("the Codename: Blue Sky", index) == {}  # "name" inside a word is not a label
    assert _hits("Organization Name: Royal Canadian Mounted Police", index) == {}
    assert _hits("Name: [CONTACT_BLOCK]", index) == {}
    for noise in ("File Number: 21120-27-5363626", "RFQ 01044-270173", "NSN: 5985-01-689-7983",
                  "Event ID: 0000009090", "closing of 2026-05-19 02:00 PM", "$1,500,000,000",
                  "https://example.com/6135550123", "solicitation 1000536720/B",
                  "SOL-FAKE-NL-1"):
        assert _hits(noise, index) == {}, noise
