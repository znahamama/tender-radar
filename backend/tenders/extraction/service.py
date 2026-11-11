"""Extract one tender: cache lookup, provider call, post-validation, storage."""

from __future__ import annotations

from dataclasses import dataclass

from tenders.models import Extraction

from .budget import CallMeter
from .prompt import PROMPT_VERSION, build_notice
from .providers import Extractor
from .schema import SCHEMA_VERSION
from .validate import postvalidate


@dataclass
class Outcome:
    reference: str
    status: str  # "cache_hit" | "ok" | "failed"
    extraction: Extraction


def extract_tender(tender, extractor: Extractor, meter: CallMeter) -> Outcome:
    """Raises budget.CapReached before any call that would break a cap."""
    notice = build_notice(tender)
    key = {"tender": tender, "content_hash": notice.content_hash,
           "model_id": extractor.model_id, "prompt_version": PROMPT_VERSION}
    cached = Extraction.objects.filter(**key, status=Extraction.Status.OK).first()
    if cached:
        return Outcome(tender.reference_number, "cache_hit", cached)

    result = extractor.extract(notice, meter, tender=tender)
    flags = (["truncated"] if notice.truncated else []) + result.flags
    data = None
    if result.status == "ok":
        checked, post_flags = postvalidate(result.data, notice.text,
                                           tender.sa_required_from_metadata)
        flags += post_flags
        data = checked.model_dump(mode="json")

    # A failed row is retried on the next run (only status=ok counts as a cache hit).
    extraction, _ = Extraction.objects.update_or_create(
        **key,
        defaults={"schema_version": SCHEMA_VERSION, "status": result.status, "data": data,
                  "flags": flags, "error": result.error},
    )
    return Outcome(tender.reference_number, result.status, extraction)
