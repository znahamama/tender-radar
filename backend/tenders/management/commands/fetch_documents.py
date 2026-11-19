from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from tenders.documents.service import fetch_documents


class Command(BaseCommand):
    help = ("Fetch CanadaBuys attachment PDFs of open, kept tenders whose attachment URL set "
            "changed or that have a transient failure to retry, and store their redacted "
            "text (tenders.documents). No API calls.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--csv-dir", required=True, type=Path,
            help="Directory with the CanadaBuys CSVs ingest just read (*.csv). Their contact "
                 "columns are the redaction hints; they are not stored.")

    def handle(self, *args, csv_dir: Path, **options):
        paths = sorted(csv_dir.glob("*.csv"))
        if not paths:
            raise CommandError(f"No *.csv in {csv_dir}: run ingest_canadabuys first.")
        s = fetch_documents(paths)
        out = self.stdout.write
        out(f"tenders: {s.tenders_checked} checked, {s.unchanged} unchanged, "
            f"{s.no_csv_row} not in the CSVs")
        out(f"downloads: {s.downloads} ({s.retries} retries); deleted {s.deleted} row(s) for "
            "URLs no longer listed")
        out("documents: " + (", ".join(f"{k} {v}" for k, v in sorted(s.statuses.items()))
                             or "none"))
        if s.stopped:
            out(self.style.WARNING(f"stopped early: {s.stopped} (the rest go next run)"))
