from collections import Counter

from django.core.management.base import BaseCommand

from tenders.eligibility.service import reassess


class Command(BaseCommand):
    help = ("Recompute eligibility assessments for the latest extraction of each tender and "
            "model, using today's validation rules and the default profile. No API calls.")

    def add_arguments(self, parser):
        parser.add_argument("--model-id", action="append", dest="model_ids",
                            help="Only this extractor model_id (repeatable).")
        parser.add_argument("--status", help="Only tenders with this tender_status, e.g. Open.")

    def handle(self, *args, model_ids, status, **options):
        assessments = reassess(model_ids=model_ids, tender_status=status)
        by_model: dict[str, Counter] = {}
        for a in assessments:
            by_model.setdefault(a.extraction.model_id, Counter())[a.status] += 1
        for model_id, c in sorted(by_model.items()):
            self.stdout.write(f"{model_id}: " + ", ".join(f"{k} {v}" for k, v in sorted(c.items())))
        self.stdout.write(f"assessed {len(assessments)}")
