"""Stream CSV rows -> TenderNotice, dedupe across files, classify. No DB, no network."""

from __future__ import annotations

import csv
import logging
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from .filters import is_it, is_national, is_nl
from .schema import TenderNotice

log = logging.getLogger(__name__)

REF_HEADER = TenderNotice.model_fields["reference_number"].alias
MAX_FAILURE_SAMPLES = 3
# Longest real description is ~50k chars; the stdlib default (128 KiB) is too tight for comfort.
csv.field_size_limit(min(sys.maxsize, 8 * 1024 * 1024))


@dataclass
class IngestStats:
    rows_read: dict[str, int] = field(default_factory=dict)
    parse_failures: int = 0
    failure_samples: list[str] = field(default_factory=list)
    duplicates_removed: int = 0
    duplicate_refs: list[str] = field(default_factory=list)

    def record_failure(self, ref: str | None, reason: str) -> None:
        self.parse_failures += 1
        if len(self.failure_samples) < MAX_FAILURE_SAMPLES:
            msg = f"ref={ref or '<missing>'}: {reason}"
            self.failure_samples.append(msg)
            log.warning("parse failure %s", msg)


def _reason(exc: ValidationError) -> str:
    # include_input=False: never echo cell values (which could hold contact data).
    errs = exc.errors(include_url=False, include_input=False, include_context=False)
    return "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in errs)


def parse_file(path: Path, stats: IngestStats) -> Iterator[TenderNotice]:
    """Yield valid notices from one CanadaBuys CSV; count (not raise on) bad rows."""
    read = 0
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if header is None:
            stats.rows_read[path.name] = 0
            return
        ref_idx = header.index(REF_HEADER) if REF_HEADER in header else None
        while True:
            try:
                row = next(reader)
            except StopIteration:
                break
            except csv.Error as exc:
                read += 1
                stats.record_failure(None, f"csv error near line {reader.line_num}: {exc}")
                continue
            read += 1
            ref = row[ref_idx].strip() if ref_idx is not None and ref_idx < len(row) else None
            if len(row) != len(header):
                stats.record_failure(ref, f"expected {len(header)} columns, got {len(row)}")
                continue
            try:
                yield TenderNotice.model_validate(dict(zip(header, row, strict=True)))
            except ValidationError as exc:
                stats.record_failure(ref, _reason(exc))
    stats.rows_read[path.name] = read


def dedupe(notices: Iterable[TenderNotice], stats: IngestStats) -> dict[str, TenderNotice]:
    """Keep one notice per reference number: the highest ``recency_key``; first seen wins ties."""
    best: dict[str, TenderNotice] = {}
    for n in notices:
        cur = best.get(n.reference_number)
        if cur is None:
            best[n.reference_number] = n
            continue
        stats.duplicates_removed += 1
        if len(stats.duplicate_refs) < 10:
            stats.duplicate_refs.append(n.reference_number)
        if n.recency_key > cur.recency_key:
            best[n.reference_number] = n
    return best


@dataclass
class IngestResult:
    stats: IngestStats
    notices: dict[str, TenderNotice]
    nl: set[str]
    it: set[str]
    national: set[str]

    @property
    def kept(self) -> list[TenderNotice]:
        refs = self.nl | self.it
        return [n for ref, n in self.notices.items() if ref in refs]


def run(paths: Iterable[Path]) -> IngestResult:
    stats = IngestStats()

    def all_rows() -> Iterator[TenderNotice]:
        for p in paths:
            yield from parse_file(Path(p), stats)

    notices = dedupe(all_rows(), stats)
    nl = {r for r, n in notices.items() if is_nl(n.regions_of_delivery)}
    it = {r for r, n in notices.items() if is_it(n.unspsc_codes)}
    national = {r for r, n in notices.items() if is_national(n.regions_of_delivery)}
    return IngestResult(stats=stats, notices=notices, nl=nl, it=it, national=national)
