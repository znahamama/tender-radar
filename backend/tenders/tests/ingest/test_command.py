from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from .conftest import FY_FAKE, OPEN_FAKE


def test_command_prints_summary():
    out = StringIO()
    call_command("ingest_canadabuys", "--file", str(OPEN_FAKE), str(FY_FAKE), stdout=out)
    text = out.getvalue()
    for expected in (
        "rows read total          12",
        "parse failures            2",
        "duplicates removed        1",
        "NL delivery               3",
        "IT (UNSPSC)               4",
        "NL and IT overlap         1",
        "kept (NL or IT)           6",
        "national delivery         1",
        "sample kept titles:",
    ):
        assert expected in text
    assert "Test Person" not in text and "test@example.com" not in text


def test_command_requires_input():
    with pytest.raises(CommandError):
        call_command("ingest_canadabuys", stdout=StringIO())
