from django.core.management.base import BaseCommand, CommandError

from tenders.evals import report
from tenders.evals.harness import (
    CASES_DIR,
    PINNED_PROMPTS,
    REPORTS_DIR,
    EvalError,
    run_eval,
)


class Command(BaseCommand):
    help = ("Score the LLM (claude-haiku-5-5) with each pinned prompt version, and the keyword "
            "baseline, against the hand labels of one split, and write "
            "eval/reports/<split>[-<tag>]-<date>.md and .json. The site's prompt uses the "
            "cached extraction (made through the normal cache and caps if missing); other "
            "prompts run eval-only. --repeats N adds N uncached runs per case and prompt "
            "(stored as EvalExtraction under a run tag) to measure consistency. See "
            "docs/eval-protocol.md.")

    def add_arguments(self, parser):
        parser.add_argument("--split", choices=("dev", "test"), default="dev")
        parser.add_argument("--repeats", type=int, default=0,
                            help="Extra uncached LLM runs per case and prompt (default 0).")
        parser.add_argument("--prompts", default=",".join(PINNED_PROMPTS),
                            help="Comma-separated pinned prompt versions to score "
                                 f"(default: all, {','.join(PINNED_PROMPTS)}).")
        parser.add_argument("--report-tag", default="",
                            help="Optional name part: eval/reports/<split>-<tag>-<date>.md.")

    def handle(self, *args, split, repeats, prompts, report_tag, **options):
        if repeats < 0:
            raise CommandError("--repeats must be >= 0")
        versions = tuple(p.strip() for p in prompts.split(",") if p.strip())
        try:
            report.check_tag(report_tag)  # before any API call
            run = run_eval(split, repeats, prompts=versions, cases_dir=CASES_DIR,
                           echo=self.stdout.write)
            scored = report.score(run)
            md, js = report.write(scored, REPORTS_DIR, tag=report_tag)
        except (EvalError, ValueError) as e:
            raise CommandError(str(e)) from e
        s = scored["scores"]
        for sys in run["systems"]:
            c = s["status"][sys]
            self.stdout.write(f"{report.display(sys)}: status agrees {c['agree']}/{c['n']}, "
                              f"false blocks {len(c['false_blocks'])}, missed blocks "
                              f"{len(c['missed_blocks'])}")
        for key, c in s["calls"].items():
            self.stdout.write(f"{key}: {c['calls']} calls, ${c['cost_usd']}")
        if run["stopped"]:
            self.stdout.write(self.style.WARNING(f"repeats stopped early: {run['stopped']}"))
        self.stdout.write(f"wrote {md.relative_to(CASES_DIR.parent.parent)} and "
                          f"{js.relative_to(CASES_DIR.parent.parent)}")
