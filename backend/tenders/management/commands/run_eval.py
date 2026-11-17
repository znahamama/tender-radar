from django.core.management.base import BaseCommand, CommandError

from tenders.evals import report
from tenders.evals.harness import CASES_DIR, REPORTS_DIR, EvalError, run_eval


class Command(BaseCommand):
    help = ("Score the frozen LLM (claude-haiku-5-5, prompt v2) and the keyword baseline "
            "against the hand labels of one split, and write eval/reports/<split>-<date>.md "
            "and .json. Extracts missing cases through the normal cache and caps; --repeats N "
            "adds N uncached LLM runs per case (stored as EvalExtraction under a run tag) to "
            "measure consistency. See docs/eval-protocol.md.")

    def add_arguments(self, parser):
        parser.add_argument("--split", choices=("dev", "test"), default="dev")
        parser.add_argument("--repeats", type=int, default=0,
                            help="Extra uncached LLM runs per case (default 0).")

    def handle(self, *args, split, repeats, **options):
        if repeats < 0:
            raise CommandError("--repeats must be >= 0")
        try:
            run = run_eval(split, repeats, cases_dir=CASES_DIR, echo=self.stdout.write)
        except EvalError as e:
            raise CommandError(str(e)) from e
        scored = report.score(run)
        md, js = report.write(scored, REPORTS_DIR)
        s = scored["scores"]
        for sys in ("llm", "baseline"):
            c = s["status"][sys]
            self.stdout.write(f"{sys}: status agrees {c['agree']}/{c['n']}, false blocks "
                              f"{len(c['false_blocks'])}, missed blocks {len(c['missed_blocks'])}")
        for purpose, c in s["calls"].items():
            self.stdout.write(f"{purpose}: {c['calls']} calls, ${c['cost_usd']}")
        if run["stopped"]:
            self.stdout.write(self.style.WARNING(f"repeats stopped early: {run['stopped']}"))
        self.stdout.write(f"wrote {md.relative_to(CASES_DIR.parent.parent)} and "
                          f"{js.relative_to(CASES_DIR.parent.parent)}")
