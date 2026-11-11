from collections import Counter

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from tenders.extraction.budget import CallMeter, StopRun
from tenders.extraction.providers import AnthropicExtractor, FakeExtractor
from tenders.extraction.schema import GATES
from tenders.extraction.service import extract_tender
from tenders.models import Tender


def gate_summary(data: dict | None) -> str:
    if not data:
        return "-"
    e = data["eligibility"]
    parts = []
    for gate in GATES:
        g = e[gate]
        if gate == "clearance":
            parts.append(f"clearance={g['corporate']}/{g['personnel']}")
        elif gate == "work_location":
            parts.append(f"on_site={g['on_site']}")
        else:
            parts.append(f"{gate.split('_')[0]}={g['value']}")
    return " ".join(parts)


class Command(BaseCommand):
    help = "Extract structured fields from stored tenders with an LLM (cached per text/model/prompt)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, help="Process at most N tenders.")
        parser.add_argument("--status", help="Only tenders with this tender_status, e.g. Open.")
        parser.add_argument("--reference", help="Only this reference number.")
        parser.add_argument("--fake", action="store_true",
                            help="Use saved JSON fixtures instead of the API.")

    def handle(self, *args, limit, status, reference, fake, **options):
        qs = Tender.objects.order_by("closing_date", "reference_number")
        if status:
            qs = qs.filter(tender_status__iexact=status)
        if reference:
            qs = qs.filter(reference_number=reference)
        if limit is not None:
            if limit < 1:
                raise CommandError("--limit must be >= 1")
            qs = qs[:limit]
        tenders = list(qs)
        if not tenders:
            raise CommandError("No tenders match.")

        extractor = FakeExtractor() if fake else AnthropicExtractor()
        meter = CallMeter(max_calls=settings.LLM_MAX_CALLS_PER_RUN,
                          daily_budget_usd=settings.LLM_DAILY_BUDGET_USD)
        out = self.stdout.write
        out(f"model {extractor.model_id}, {len(tenders)} tender(s)")
        counts: Counter = Counter()
        for t in tenders:
            try:
                o = extract_tender(t, extractor, meter)
            except StopRun as e:  # caps, bad key/model, 400 on the first call
                out(self.style.WARNING(f"stopped: {e}"))
                break
            counts[o.status] += 1
            x = o.extraction
            out(f"{o.reference}  {o.status}  {gate_summary(x.data)}")
            out(f"    flags: {', '.join(x.flags) or '-'}"
                + (f"  error: {x.error.splitlines()[0]}" if x.error else ""))

        out("== totals ==")
        out(f"calls {meter.calls}  cache hits {counts['cache_hit']}  ok {counts['ok']}  "
            f"failed {counts['failed']}")
        out(f"tokens in {meter.input_tokens}  out {meter.output_tokens}  "
            f"cost ${meter.cost:.6f}  (worst-case estimates included for failed connections)")
