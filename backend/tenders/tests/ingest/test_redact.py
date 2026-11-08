import logging

import pytest

from tenders.ingest import pipeline
from tenders.ingest.redact import ContactHints, leftover_check, redact

from .conftest import OPEN_FAKE

HINTS = ContactHints(
    name="Person, Test (SPAC/PSPC) (elle-la / she-her)",
    email="Test.Person@Example.com",
    phone="(709) 555-0100",
    fax="709-555-0199",
)


def r(text, hints=None):
    return redact(text, hints)


# --- hints ------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    ["Ask Test Person today", "Ask TEST  PERSON today", "Ask Person, Test today", "Ask Person,Test today"],
)
def test_hint_name_variants_case_insensitive(text):
    clean, counts = r(text, HINTS)
    assert clean == "Ask [NAME] today"
    assert counts["name"] == 1


def test_hint_name_needs_word_boundaries():
    clean, _ = r("Test Personnel office", ContactHints(name="Test Person"))
    assert clean == "Test Personnel office"


@pytest.mark.parametrize(
    "name",
    ["Contract Administrator", "Department of National Defence", "Innovative Solutions Canada",
     "PWGSC IDEaS Team / L'équipe IDEeS de TPSGC", "Jane"],
)
def test_generic_or_single_token_name_hints_are_not_scrubbed(name):
    text = f"Bids go to the {name}."
    assert r(text, ContactHints(name=name))[0] == text


def test_hint_email_case_insensitive():
    clean, counts = r("mail TEST.PERSON@EXAMPLE.COM now", HINTS)
    assert clean == "mail [EMAIL] now"
    assert counts["email"] == 1


@pytest.mark.parametrize("text", ["7095550100", "709 5550100", "+1 (709) 555-0100", "1-709-555-0199"])
def test_hint_phone_and_fax_any_format(text):
    clean, counts = r(f"call {text} today", HINTS)
    assert clean == "call [PHONE] today"
    assert counts["phone"] == 1


def test_hints_never_appear_in_repr_or_logs(caplog):
    assert "Test" not in repr(HINTS) and "709" not in str(HINTS)
    with caplog.at_level(logging.DEBUG):
        list(pipeline.parse_file(OPEN_FAKE, pipeline.IngestStats()))
    assert "Test Person" not in caplog.text and "example.com" not in caplog.text


# --- regex ------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        "jane.doe@example.gc.ca",
        "Jane_Doe+bids@sub.example.org",
        "jane.doe [at] example [dot] ca",
        "jane.doe(at)example.ca",
    ],
)
def test_email_regex(text):
    clean, counts = r(f"Write to {text}.")
    assert clean == "Write to [EMAIL]."
    assert counts["email"] == 1


@pytest.mark.parametrize(
    "text",
    [
        "613-555-0100",
        "613.555.0100",
        "613 555 0100",
        "(613) 555-0100",
        "(613)555-0100",
        "+1 613 555 0100",
        "+1-613-555-0100",
        "1-800-555-0100",
        "613-555-0100 ext. 1234",
        "613-555-0100, ext 12",
        "613-555-0100 x22",
        "613-555-0100 poste 45",
        "613555-0100",
    ],
)
def test_phone_regex(text):
    clean, counts = r(f"Tel {text} now")
    assert clean == "Tel [PHONE] now"
    assert counts["phone"] == 1


def test_phone_glued_to_following_word():
    assert r("Telephone 343-555-0176Inquiries")[0] == "Telephone [PHONE]Inquiries"


@pytest.mark.parametrize(
    "text",
    [
        "W8486-123456",
        "$1,500,000",
        "$1,500,000.00",
        "2026-12-02",
        "2026-12-02T14:00:00",
        "cb-515-53436821",
        "PW-23-01027079",
        "1000536720/B",
        "SAP Ariba #WS3430950409",
        "EN578-170003/B",
        "24062-230012/A",
        "580-6000MHz",
        "K1A 0S5",
        "January 23 2026 @ 2:00PM EST",
        "Lot 3: 1 500 000 litres",
        "GSIN N7030 / UNSPSC 43211500",
        "NAICS 541510",
    ],
)
def test_no_false_positives(text):
    clean, counts = r(f"See {text} here.")
    assert clean == f"See {text} here."
    assert sum(counts.values()) == 0


@pytest.mark.parametrize(
    "url",
    [
        "https://canadabuys.canada.ca/en/tender-opportunities/tender-notice/cb-515-53436821",
        "https://portal.us.bn.cloud.ariba.com/dashboard/public/appext/x#/RfxEvent/preview/1110009819?anId=ANONYMOUS",
        "https://example.com/contact?email=jane@example.com&tel=613-555-0100",
        "www.example.com/7095550100",
    ],
)
def test_urls_are_untouched(url):
    text = f"Docs: {url}\nContact Person"
    clean, _ = r(text, HINTS)
    assert url in clean


# --- label blocks -----------------------------------------------------------------------

def test_contact_block_scrubs_value_and_following_lines_until_blank():
    text = (
        "Intro paragraph.\n\n"
        "Contracting Authority: Jane Doe\n"
        "Public Services and Procurement Canada\n"
        "11 Laurier Street\n"
        "Gatineau, QC K1A 0S5\n"
        "\n"
        "Closing remarks."
    )
    clean, counts = r(text)
    assert clean == "Intro paragraph.\n\nContracting Authority: [CONTACT_BLOCK]\n\nClosing remarks."
    assert counts["contact_block"] == 1


def test_block_stops_after_four_lines():
    text = "Contact:\nl1\nl2\nl3\nl4\nl5 stays"
    assert r(text)[0] == "Contact: [CONTACT_BLOCK]\nl5 stays"


def test_block_stops_at_other_label_and_prose():
    text = (
        "Name: Jane Doe\n"
        "Closing date: 2026-12-02\n"
        "Inquiries:\n"
        "Inquiries regarding this requirement must be submitted in writing before the deadline\n"
    )
    clean, counts = r(text)
    assert clean == (
        "Name: [CONTACT_BLOCK]\n"
        "Closing date: 2026-12-02\n"
        "Inquiries:\n"
        "Inquiries regarding this requirement must be submitted in writing before the deadline\n"
    )
    assert counts["contact_block"] == 1


@pytest.mark.parametrize(
    "line",
    ["Courriel : jane", "E-mail: jane", "Telephone No. - jane", "Tel.: jane", "Phone Number:\tjane",
     "• Contact Person: Jane Doe", "Autorité contractante : Jane Doe", "Fax: jane", "Contacts: Jane"],
)
def test_label_variants(line):
    clean, counts = r(line)
    assert clean.endswith("[CONTACT_BLOCK]")
    assert "jane" not in clean.lower()
    assert counts["contact_block"] == 1


@pytest.mark.parametrize(
    "line",
    [
        "Contact the Contracting Authority for details.",
        "Email bids to the address in the RFP.",
        "Telephone interviews may be held.",
        "Name of project: Harbour repairs",
        "Inquiries regarding this RFP must be in writing.",
        "Contractor: must hold a Reliability clearance",
    ],
)
def test_prose_starting_with_label_words_is_kept(line):
    assert r(line) == (line, {"name": 0, "email": 0, "phone": 0, "contact_block": 0})


@pytest.mark.parametrize(
    "line, expected",
    [
        (
            "Name:" + " " * 30 + "Jane DoeTitle:" + " " * 30 + "Senior Procurement OfficerDepartment: Fake",
            "Name: [CONTACT_BLOCK] Title:" + " " * 30 + "Senior Procurement OfficerDepartment: Fake",
        ),
        (
            "Contracting Authority:" + " " * 40 + "Jane Doe" + " " * 40 + "Telephone: [PHONE]",
            "Contracting Authority: [CONTACT_BLOCK] Telephone: [PHONE]",
        ),
    ],
)
def test_contact_block_glued_onto_one_long_line(line, expected):
    clean, counts = r(line)
    assert clean == expected
    assert "Jane" not in clean and counts["contact_block"] == 1


def test_long_prose_after_label_without_glued_label_is_kept():
    line = "Contact: " + "all questions must go through the portal and will be answered in writing " * 2
    assert r(line)[0] == line


def test_glued_label_needs_a_boundary():
    line = "Contact: Stay at the Grand Hotel: rooms are booked " + "for bidders and staff " * 4
    assert r(line)[0] == line  # "Hotel:" is not "tel:"; no cut, so the long value is prose


def test_block_of_only_placeholders_is_left_as_is():
    clean, counts = r("Email: jane@example.com\nTelephone: 613-555-0100")
    assert clean == "Email: [EMAIL]\nTelephone: [PHONE]"
    assert counts == {"name": 0, "email": 1, "phone": 1, "contact_block": 0}


# --- leftovers --------------------------------------------------------------------------

def test_leftover_check():
    assert leftover_check("mail a@b.ca or 613-555-0100") == {"email": 1, "phone": 1}
    clean, _ = r("mail a@b.ca or 613-555-0100")
    assert leftover_check(clean) == {"email": 0, "phone": 0}
    assert leftover_check(None) == {"email": 0, "phone": 0}


def test_empty_text():
    assert r("") == ("", {"name": 0, "email": 0, "phone": 0, "contact_block": 0})
    assert r(None)[0] == ""
