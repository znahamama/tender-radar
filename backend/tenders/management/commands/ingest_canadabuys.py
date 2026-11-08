from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from tenders.ingest import fetch, filters
from tenders.ingest.pipeline import run
from tenders.ingest.redact import BLOCK, EMAIL, NAME, PHONE

DEFAULT_DOWNLOAD_DIR = settings.BASE_DIR.parent / "data" / "raw"


class Command(BaseCommand):
    help = (
        "Parse CanadaBuys tender CSVs, redact contacts, keep NL-delivery notices and "
        "national/NL IT notices, print a summary. Dry-run unless --save."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--file", dest="files", action="extend", nargs="+", type=Path, default=[],
            metavar="PATH", help="Local CanadaBuys CSV(s) to parse.",
        )
        parser.add_argument(
            "--url-defaults", action="store_true",
            help="Download the open-notices and 2026-2027 CSVs first, then parse them.",
        )
        parser.add_argument(
            "--download-dir", type=Path, default=DEFAULT_DOWNLOAD_DIR,
            help=f"Where --url-defaults saves files (default: {DEFAULT_DOWNLOAD_DIR}).",
        )
        parser.add_argument(
            "--save", action="store_true", help="Upsert kept notices into the database.",
        )

    def handle(self, *args, files, url_defaults, download_dir, save, **options):
        paths = list(files)
        if url_defaults:
            for url in fetch.DEFAULT_URLS:
                self.stdout.write(f"downloading {url}")
                paths.append(fetch.download(url, download_dir))
        if not paths:
            raise CommandError("Give --file PATH ... and/or --url-defaults.")
        for p in paths:
            if not p.is_file():
                raise CommandError(f"Not a file: {p}")

        now = datetime.now(UTC)
        result = run(paths)
        s = result.stats
        nl, it, nat, kept_refs = result.nl, result.it, result.national, result.kept_refs
        kept = result.kept
        out = self.stdout.write

        out("== CanadaBuys ingest summary ==")
        for name, n in s.rows_read.items():
            out(f"rows read            {n:6d}  {name}")
        out(f"rows read total      {sum(s.rows_read.values()):6d}")
        out(f"parse failures       {s.parse_failures:6d}")
        for sample in s.failure_samples:
            out(f"  - {sample}")
        out(f"duplicates removed   {s.duplicates_removed:6d}")
        if s.duplicate_refs:
            out(f"  e.g. {', '.join(s.duplicate_refs[:5])}")
        out(f"unique notices       {len(result.notices):6d}")
        out(f"NL delivery          {len(nl):6d}")
        out(f"IT (UNSPSC or GSIN)  {len(it):6d}")
        out(f"national delivery    {len(nat):6d}")
        out(f"NL and IT overlap    {len(nl & it):6d}")
        out(f"dropped regional IT  {len(result.dropped_regional_it):6d}  (IT delivered only to other regions)")
        out(f"kept                 {len(kept_refs):6d}  (NL, or IT that is national/NL)")
        self._counter("  by status       ", Counter(n.tender_status or "<none>" for n in kept))
        self._counter("  by region_scope ", Counter(
            filters.region_scope(n.regions_of_delivery) for n in kept))
        self._counter("  by notice_kind  ", Counter(filters.notice_kind(n) for n in kept))
        self._counter("  data_quality    ", Counter(f for n in kept for f in n.data_quality))

        order = (NAME, EMAIL, PHONE, BLOCK)
        for label, subset in (("all rows", None), ("kept    ", kept)):
            red = result.redaction_totals(subset)
            left, left_refs = result.leftovers(subset)
            out(f"redactions {label}  " + ", ".join(f"{k} {red[k]}" for k in order)
                + f"  | leftovers {sum(left.values())} (email {left[EMAIL]}, phone {left[PHONE]})")
            if left_refs:
                out(f"  leftover refs: {', '.join(left_refs[:5])}")

        out("sample kept titles:")
        for n in kept[:3]:
            scope = filters.region_scope(n.regions_of_delivery)
            tag = f"{scope}{'+IT' if filters.notice_is_it(n) else ''}"
            out(f"  [{tag}] {n.reference_number}: {n.title}")

        if not save:
            out("dry run: nothing written (use --save)")
            return
        # Imported here so a dry run never loads the storage layer.
        from tenders.ingest.storage import upsert

        r = upsert(kept, now=now)
        out(f"saved: created {r.created}, updated {r.updated}, unchanged {r.unchanged}")

    def _counter(self, label: str, c: Counter) -> None:
        self.stdout.write(label + (", ".join(f"{k} {v}" for k, v in c.most_common()) or "-"))
