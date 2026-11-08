from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from .conftest import FY_FAKE, OPEN_FAKE


def test_dry_run_prints_summary_without_db():
    # No django_db mark: pytest-django would raise if the dry run touched the database.
    out = StringIO()
    call_command("ingest_canadabuys", "--file", str(OPEN_FAKE), str(FY_FAKE), stdout=out)
    text = out.getvalue()
    for expected in (
        "rows read total          15",
        "parse failures            2",
        "duplicates removed        1",
        "NL delivery               5",
        "IT (UNSPSC or GSIN)       5",
        "dropped regional IT       2",
        "kept                      6",
        "by region_scope nl 4, national 1, nl+national 1",
        "by notice_kind  regular 3, sa_or_so_request 2, against_sa 1",
        "mentions_standing_offer 2",
        "redactions kept      name 3, email 2, phone 4, contact_block 1  | leftovers 0",
        "dry run: nothing written",
    ):
        assert expected in text, expected
    assert "Test Person" not in text and "test@example.com" not in text


def test_command_requires_input():
    with pytest.raises(CommandError):
        call_command("ingest_canadabuys", stdout=StringIO())
