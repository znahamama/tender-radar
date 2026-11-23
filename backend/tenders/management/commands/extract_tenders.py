from collections import Counter

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from tenders.eligibility.service import assess, default_profile
from tenders.extraction.budget import CallMeter, CapReached, StopRun
from tenders.extraction.keyword import KeywordExtractor
from tenders.extraction.prompt import (
    DOCUMENTS_PROMPT_VERSION,
    PROMPT_VERSION,
    prompt_version_for,
)
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
    help = ("Extract structured fields from stored tenders (LLM, --baseline keywords, or --fake "
            "fixtures), cached per text and config, then assess eligibility.")

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, help="Process at most N tenders.")
        parser.add_argument("--status", help="Only tenders with this tender_status, e.g. Open.")
        parser.add_argument("--reference", help="Only this reference number.")
        source = parser.add_mutually_exclusive_group()
        source.add_argument("--fake", action="store_true",
                            help="Use saved JSON fixtures instead of the API.")
        source.add_argument("--baseline", action="store_true",
                            help="Use the keyword baseline (regexes only, no API, no cost).")

    def handle(self, *args, limit, status, reference, fake, baseline, **options):
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

        by_version: dict = {}
        if fake:
            extractor = FakeExtractor()
        elif baseline:
            extractor = KeywordExtractor()
        else:
            extractor = AnthropicExtractor()
            # A tender with ok solicitation documents is read with them (prompt v5); every
            # other tender stays on v3 and keeps its cache. One client, one meter: v4 calls
            # count against the same per-run and daily caps.
            with_documents = AnthropicExtractor(extractor.client,
                                                prompt_version=DOCUMENTS_PROMPT_VERSION)
            by_version = {PROMPT_VERSION: extractor, DOCUMENTS_PROMPT_VERSION: with_documents}
        profile = default_profile()
        meter = CallMeter(max_calls=settings.LLM_MAX_CALLS_PER_RUN,
                          daily_budget_usd=settings.LLM_DAILY_BUDGET_USD)
        out = self.stdout.write
        out(f"model {extractor.model_id}, {len(tenders)} tender(s)")
        counts: Counter = Counter()
        verdicts: Counter = Counter()
        stopped: StopRun | None = None
        v4_failed = 0
        for i, t in enumerate(tenders):
            chosen = by_version[prompt_version_for(t)] if by_version else extractor
            with_docs = chosen is by_version.get(DOCUMENTS_PROMPT_VERSION)
            try:
                o = extract_tender(t, chosen, meter)
            except CapReached as e:  # the budget working as designed, v3 or v4
                out(self.style.WARNING(f"stopped: {e}"))
                stopped = e
                if e.daily:  # this tender and the rest wait for the next run
                    Tender.objects.filter(pk__in=[x.pk for x in tenders[i:]]).update(
                        llm_capped_at=timezone.now())
                break
            except StopRun as e:  # bad key/model, 400 on the first call
                if with_docs:
                    v4_failed += 1
                    self._v4_failed(t, str(e))
                    continue
                out(self.style.WARNING(f"stopped: {e}"))
                stopped = e
                break
            if with_docs and o.status == "failed":
                v4_failed += 1
                self._v4_failed(t, o.extraction.error.splitlines()[0] if o.extraction.error
                                else "failed")
                # Removed so it never becomes the tender's current extraction: the v3 one
                # and its assessment stay what the site and assess_tenders use. The next
                # run tries v4 again (only an ok row is a cache hit).
                o.extraction.delete()
                continue
            counts[o.status] += 1
            x = o.extraction
            a = assess(x, profile)
            verdicts[a.status] += 1
            read = sum(d["status"] != "not_read" for d in x.documents)
            docs = f"  [{x.prompt_version}: {read} of {len(x.documents)} document(s) read]" \
                if x.documents else ""
            out(f"{o.reference}  {o.status}  {a.status}  {gate_summary(x.data)}{docs}")
            out(f"    flags: {', '.join(x.flags) or '-'}"
                + (f"  error: {x.error.splitlines()[0]}" if x.error else ""))

        out("== totals ==")
        out("assessments " + ", ".join(f"{k} {v}" for k, v in sorted(verdicts.items())))
        out(f"calls {meter.calls}  cache hits {counts['cache_hit']}  ok {counts['ok']}  "
            f"failed {counts['failed']}")
        out(f"tokens in {meter.input_tokens}  out {meter.output_tokens}  "
            f"cost ${meter.cost:.6f}  (worst-case estimates included for failed connections)")
        if v4_failed:
            out(self.style.WARNING(f"{DOCUMENTS_PROMPT_VERSION} failed {v4_failed}: kept their "
                                   "v3 extraction and assessment (see the lines above)"))

        # A cap is the budget working as designed: a clean exit. Any other stop (bad key or
        # model, 400 on the first call) is a broken run; failing here fails the worker
        # invocation, which is what the CloudWatch Errors alarm watches.
        # A v4 failure is isolated: logged, the tender keeps its v3 answer, the run goes on.
        if stopped is not None and not isinstance(stopped, CapReached):
            raise CommandError(f"stopped: {stopped}") from stopped

    def _v4_failed(self, tender: Tender, reason: str) -> None:
        self.stdout.write(self.style.WARNING(
            f"{tender.reference_number}  {DOCUMENTS_PROMPT_VERSION} failed, kept v3: {reason}"))
