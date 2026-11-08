from collections import Counter
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from tenders.ingest import fetch
from tenders.ingest.filters import is_it, is_nl
from tenders.ingest.pipeline import run

DEFAULT_DOWNLOAD_DIR = settings.BASE_DIR.parent / "data" / "raw"


class Command(BaseCommand):
    help = "Parse CanadaBuys tender CSVs, filter to NL-delivery or IT notices, print a summary."

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

    def handle(self, *args, files, url_defaults, download_dir, **options):
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

        result = run(paths)
        s = result.stats
        nl, it = result.nl, result.it
        kept_refs = nl | it
        nat = result.national
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
        out(f"IT (UNSPSC)          {len(it):6d}")
        out(f"NL and IT overlap    {len(nl & it):6d}")
        out(f"kept (NL or IT)      {len(kept_refs):6d}")
        out(f"national delivery    {len(nat):6d}  (counted, not kept on its own)")
        out(f"  national and IT    {len(nat & it):6d}  (already kept via IT)")
        out(f"  national, not kept {len(nat - kept_refs):6d}")
        out(f"  IT, regional non-NL{len(it - nl - nat):6d}  (IT delivered only to other regions)")
        by_status = Counter(n.tender_status or "<none>" for n in result.kept)
        out("kept by status       " + ", ".join(f"{k} {v}" for k, v in by_status.most_common()))

        out("sample kept titles:")
        for n in result.kept[:3]:
            tags = "+".join(t for t, ok in (
                ("NL", is_nl(n.regions_of_delivery)), ("IT", is_it(n.unspsc_codes))) if ok)
            out(f"  [{tags}] {n.reference_number}: {n.title}")
