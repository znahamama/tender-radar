from datetime import UTC, datetime

from tenders.ingest.pipeline import IngestStats, dedupe, run
from tenders.ingest.schema import TenderNotice

from .conftest import FY_FAKE, OPEN_FAKE

NOW = datetime(2026, 11, 7, tzinfo=UTC)


def _run(*paths):
    return run(paths or [OPEN_FAKE, FY_FAKE], now=NOW)


def test_run_counts_and_filters():
    r = _run()
    s = r.stats
    assert s.rows_read == {OPEN_FAKE.name: 11, FY_FAKE.name: 4}
    assert s.parse_failures == 2  # bad date + short row
    assert s.duplicates_removed == 1
    assert r.nl == {"FAKE-NL-1", "FAKE-CITY-1", "FAKE-DUP-1", "FAKE-GSIN-1", "FAKE-SO-1"}
    assert r.it == {"FAKE-IT-1", "FAKE-IT-2", "FAKE-MULTI-1", "FAKE-DUP-1", "FAKE-GSIN-1"}
    assert r.national == {"FAKE-IT-2", "FAKE-GSIN-1", "FAKE-TEL-1"}
    assert r.dropped_regional_it == {"FAKE-IT-1", "FAKE-MULTI-1"}
    assert r.kept_refs == {"FAKE-NL-1", "FAKE-CITY-1", "FAKE-DUP-1", "FAKE-GSIN-1", "FAKE-SO-1",
                           "FAKE-IT-2"}
    assert {n.reference_number for n in r.kept} == r.kept_refs


def test_bad_rows_are_counted_not_raised_and_reasons_hide_raw_values():
    s = _run().stats
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
    s = _run(p).stats
    assert s.parse_failures == 5
    assert len(s.failure_samples) == 3


def test_dedupe_keeps_latest_amendment_across_files():
    n = _run().notices["FAKE-DUP-1"]
    assert n.amendment_number == 3
    assert n.title == "Duplicate, amendment 3 (latest)"
    assert _run(FY_FAKE, OPEN_FAKE).notices["FAKE-DUP-1"].amendment_number == 3


def _n(ref, amend, pub):
    return TenderNotice(
        reference_number=ref, title=f"{ref}-{amend}-{pub}", amendment_number=amend,
        publication_date=datetime(2026, 1, pub, tzinfo=UTC),
    )


def test_dedupe_uses_publication_date_when_amendments_tie():
    s = IngestStats()
    best = dedupe([_n("R", 2, 10), _n("R", 2, 20), _n("R", 1, 30)], s)
    assert best["R"].title == "R-2-20"
    assert s.duplicates_removed == 2


def test_redaction_happens_in_pipeline_with_counts_and_no_leftovers():
    r = _run()
    nl = r.notices["FAKE-NL-1"]
    assert nl.redaction_counts == {"name": 1, "email": 0, "phone": 1, "contact_block": 1}
    assert "Contracting Authority: [CONTACT_BLOCK]" in nl.description
    assert "W8486-123456" in nl.description and "$1,500,000" in nl.description
    assert "2026-12-02" in nl.description
    totals = r.redaction_totals()
    assert totals == {"name": 3, "email": 2, "phone": 4, "contact_block": 1}
    left, refs = r.leftovers()
    assert sum(left.values()) == 0 and refs == []


def test_empty_file(tmp_path):
    p = tmp_path / "empty.csv"
    p.write_text("", encoding="utf-8")
    r = _run(p)
    assert r.stats.rows_read == {"empty.csv": 0}
    assert r.notices == {}
