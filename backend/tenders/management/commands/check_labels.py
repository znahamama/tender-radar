import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from tenders.evals.labels import check_file

DEFAULT_DIR = settings.BASE_DIR.parent / "eval" / "cases"


class Command(BaseCommand):
    help = ("Check the hand-labelled eval files listed in eval/cases/manifest.json: allowed "
            "values, evidence copied exactly from the file's notice text (grounding "
            "normalization), and no TODO left. Reads no model output.")

    def add_arguments(self, parser):
        parser.add_argument("--dir", type=Path, default=DEFAULT_DIR,
                            help="Folder holding manifest.json (default: eval/cases).")

    def handle(self, *args, dir: Path, **options):
        manifest_path = dir / "manifest.json"
        if not manifest_path.is_file():
            raise CommandError(f"No manifest at {manifest_path}. Run eval/select_cases.py.")
        cases = json.loads(manifest_path.read_text(encoding="utf-8"))["cases"]

        complete = 0
        for case in cases:
            path = dir / case["file"]
            if not path.is_file():
                self.stdout.write(f"{case['file']}  missing")
                continue
            report = check_file(path)
            if report.reference and report.reference != case["reference"]:
                report.problems.insert(0, f"notice is {report.reference}, manifest says "
                                          f"{case['reference']}")
            if report.complete:
                complete += 1
                self.stdout.write(f"{case['file']}  complete")
                continue
            self.stdout.write(f"{case['file']}  incomplete")
            if report.todo:
                self.stdout.write(f"  - {report.todo} TODO left")
            for problem in report.problems:
                self.stdout.write(f"  - {problem}")

        known = {c["file"] for c in cases}
        for path in sorted(dir.glob("*/*.yaml")):
            rel = path.relative_to(dir).as_posix()
            if rel not in known:
                self.stdout.write(f"{rel}  not in the manifest (ignored)")
        self.stdout.write(f"{complete} of {len(cases)} complete")
