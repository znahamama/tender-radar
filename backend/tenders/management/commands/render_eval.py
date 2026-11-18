from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from tenders.evals import report


class Command(BaseCommand):
    help = ("Re-render an eval report from its stored JSON (no API call, no extraction): "
            "re-scores the stored runs with the current report code and rewrites the .md and "
            ".json in place. --compare adds a side-by-side view with another report's JSON "
            "(e.g. dev next to test).")

    def add_arguments(self, parser):
        parser.add_argument("json_path", type=Path)
        parser.add_argument("--compare", type=Path, default=None)

    def handle(self, *args, json_path, compare, **options):
        for p in (json_path, compare):
            if p is not None and not p.is_file():
                raise CommandError(f"no such report: {p}")
        md, js = report.rerender(json_path, compare)
        self.stdout.write(f"rewrote {md} and {js}")
