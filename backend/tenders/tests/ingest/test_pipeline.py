from datetime import datetime

from tenders.ingest.pipeline import IngestStats, dedupe, run
from tenders.ingest.schema import SOURCE_TZ, TenderNotice

from .conftest import FY_FAKE, OPEN_FAKE


def test_run_counts_and_filters():
    r = run([OPEN_FAKE, FY_FAKE])
    s = r.stats
    assert s.rows_read == {OPEN_FAKE.name: 9, FY_FAKE.name: 3}
    assert s.parse_failures == 2  # bad date + short row
    assert s.duplicates_removed == 1
    assert r.nl == {"FAKE-NL-1", "FAKE-CITY-1", "FAKE-DUP-1"}
    assert r.it == {"FAKE-IT-1", "FAKE-IT-2", "FAKE-MULTI-1", "FAKE-DUP-1"}
    assert r.national == {"FAKE-IT-2"}
    assert {n.reference_number for n in r.kept} == r.nl | r.it
    assert "FAKE-NOT-1" not in r.nl | r.it
    assert "FAKE-EMPTY-1" not in r.it  # "*4321" is not an IT code


def test_bad_rows_are_counted_not_raised_and_reasons_hide_raw_values():
    s = run([OPEN_FAKE, FY_FAKE]).stats
    assert len(s.failure_samples) == 2
    bad_date, short = s.failure_samples
    assert bad_date.startswith("ref=FAKE-BAD-1: tenderClosingDate")
    assert "not-a-date" not in bad_date
    assert short == "ref=FAKE-SHORT-1: expected 67 columns, got 4"
    joined = " ".join(s.failure_samples)
    assert "Test Person" not in joined and "example.com" not in joined


def test_failure_samples_capped_at_three(tmp_path):
    lines = OPEN_FAKE.read_text(encoding="utf-8-sig").splitlines()
    header, bad = lines[0], next(line for line in lines if "FAKE-BAD-1" in line)
    p = tmp_path / "bad.csv"
    p.write_text("\n".join([header] + [bad] * 5) + "\n", encoding="utf-8-sig")
    s = run([p]).stats
    assert s.parse_failures == 5
    assert len(s.failure_samples) == 3


def test_dedupe_keeps_latest_amendment_across_files():
    r = run([OPEN_FAKE, FY_FAKE])
    n = r.notices["FAKE-DUP-1"]
    assert n.amendment_number == 3
    assert n.title == "Duplicate, amendment 3 (latest)"
    # Order of files must not matter.
    assert run([FY_FAKE, OPEN_FAKE]).notices["FAKE-DUP-1"].amendment_number == 3


def _n(ref, amend, pub):
    return TenderNotice(
        reference_number=ref, title=f"{ref}-{amend}-{pub}", amendment_number=amend,
        publication_date=datetime(2026, 1, pub, tzinfo=SOURCE_TZ),
    )


def test_dedupe_uses_publication_date_when_amendments_tie():
    s = IngestStats()
    best = dedupe([_n("R", 2, 10), _n("R", 2, 20), _n("R", 1, 30)], s)
    assert best["R"].title == "R-2-20"
    assert s.duplicates_removed == 2


def test_empty_file(tmp_path):
    p = tmp_path / "empty.csv"
    p.write_text("", encoding="utf-8")
    r = run([p])
    assert r.stats.rows_read == {"empty.csv": 0}
    assert r.notices == {}
