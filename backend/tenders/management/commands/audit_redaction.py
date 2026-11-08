from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from tenders.ingest.audit import DETECTORS, FIELDS, ContactIndex, audit
from tenders.ingest.fetch import DEFAULT_URLS
from tenders.models import Tender

DEFAULT_FILES = [settings.BASE_DIR.parent / "data" / "raw" / u.rsplit("/", 1)[-1] for u in DEFAULT_URLS]


class Command(BaseCommand):
    help = (
        "Scan stored Tender descriptions and titles for contact data the redactor missed, "
        "using every contact in the given CSVs plus independent detectors. Prints counts and "
        "reference numbers only, never matched text."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--file", dest="files", action="extend", nargs="+", type=Path, default=[],
            metavar="PATH", help="CanadaBuys CSV(s) to take contacts from (default: data/raw).",
        )

    def handle(self, *args, files, **options):
        paths = files or [p for p in DEFAULT_FILES if p.is_file()]
        if not paths:
            raise CommandError("Give --file PATH ... (no default CSVs found in data/raw).")
        for p in paths:
            if not p.is_file():
                raise CommandError(f"Not a file: {p}")

        index = ContactIndex.from_csvs(paths)
        rows = Tender.objects.values_list("reference_number", "description", "title")
        report = audit(rows.iterator(), index)
        sizes = (index.rows, len(index.names), len(index.emails), len(index.phones))
        del index  # contacts never outlive the audit

        out = self.stdout.write
        out("== Redaction audit ==")
        out("contact index         {} CSV rows -> {} name variants, {} emails, {} phones "
            "(memory only)".format(*sizes))
        out(f"tenders scanned       {report.scanned}")
        out(f"{'field':<12} {'detector':<12} {'hits':>5} {'tenders':>8}")
        for fld in FIELDS:
            for det in DETECTORS:
                out(f"{fld:<12} {det:<12} {report.hits.get((fld, det), 0):>5} "
                    f"{len(report.refs.get((fld, det), [])):>8}")
        out(f"total hits            {report.total}")
        for (fld, det), refs in sorted(report.refs.items()):
            out(f"  {fld}/{det}: {', '.join(refs)}")
