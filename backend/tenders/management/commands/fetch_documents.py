from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from tenders.documents.service import fetch_documents


class Command(BaseCommand):
    help = ("Fetch CanadaBuys attachment PDFs of open, kept tenders whose attachment URL set "
            "changed, a failure to retry, or a document that now fits the per-tender "
            "limit, and store their redacted text (tenders.documents). Then re-clean any stored "
            "document, on any tender, cleaned by an older redact(). No API calls.")

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
        out(f"downloads: {s.downloads} ({s.retries} retries, {s.reevaluated} previously over "
            f"the per-tender limit, {s.recleaned} re-cleaned); deleted {s.deleted} row(s) for "
            "URLs no longer listed")
        out("documents: " + (", ".join(f"{k} {v}" for k, v in sorted(s.statuses.items()))
                             or "none"))
        out(f"stored ok: {s.ok_tenders} tender(s) with >= 1 ok document, {s.ok_documents} "
            f"document(s), {s.ok_chars:,} characters; {s.ok_stale} still cleaned by an "
            f"older redact() ({s.stale_no_csv_row} left: tender not in the CSVs)")
        if s.stopped:
            out(self.style.WARNING(f"stopped early: {s.stopped} (the rest go next run)"))
