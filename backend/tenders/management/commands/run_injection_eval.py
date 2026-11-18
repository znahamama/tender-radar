from django.core.management.base import BaseCommand, CommandError

from tenders.evals import injection


class Command(BaseCommand):
    help = ("Prompt-injection eval: run the site's prompt (frozen model settings) on the 5 "
            "invented attack notices in eval/injection/ and their clean twins, count the "
            "pattern detector's false positives on real stored notices, and write "
            "eval/reports/injection-<date>.md and .json. --rescore re-scores and re-renders a "
            "stored run with no API call.")

    def add_arguments(self, parser):
        parser.add_argument("--repeats", type=int, default=3,
                            help="Runs per notice beyond the first (default 3: 4 runs each).")
        parser.add_argument("--rescore", metavar="JSON",
                            help="Re-score and re-render a stored run's JSON; no API call.")

    def handle(self, *args, repeats, rescore, **options):
        if repeats < 0:
            raise CommandError("--repeats must be >= 0")
        try:
            if rescore:
                import json
                from pathlib import Path
                data = json.loads(Path(rescore).read_text(encoding="utf-8"))
                data = {k: v for k, v in data.items() if k != "scores"}
                scored = injection.score(data, injection.load_cases())
            else:
                scored = injection.score(injection.run(repeats, echo=self.stdout.write))
            md_path = injection.REPORTS_DIR / f"injection-{scored['date']}.md"
            md, js = injection.write(scored, body=injection.kept_body(md_path))
        except injection.InjectionEvalError as e:
            raise CommandError(str(e)) from e
        self.stdout.write(injection.results_table(scored))
        fp = scored["false_positives"]
        self.stdout.write(f"detector false positives: {fp['sample']['flagged']}/"
                          f"{fp['sample']['n']} sampled, {fp['all_stored']['flagged']}/"
                          f"{fp['all_stored']['n']} all stored")
        c = scored["scores"]["calls"]
        self.stdout.write(f"{c['n']} calls, ${c['cost_usd']}")
        if scored["stopped"]:
            self.stdout.write(self.style.WARNING(f"stopped early: {scored['stopped']}"))
        self.stdout.write(f"wrote {md} and {js}")
