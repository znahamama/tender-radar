"""Extract one tender: cache lookup, provider call, storage, and re-validation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError

from tenders.models import Extraction, Tender

from .budget import CallMeter
from .prompt import Notice, build_input
from .providers import Extractor
from .schema import SCHEMA_VERSION, SCHEMAS, ExtractionData
from .validate import postvalidate


@dataclass
class Outcome:
    reference: str
    status: Literal["cache_hit", "ok", "failed"]
    extraction: Extraction


@dataclass
class Revalidation:
    data: dict | None
    flags: list[str]


def cache_hash(notice: Notice, extractor: Extractor) -> str:
    """Text + extractor config (model, system prompt, schema, effort, max_tokens)."""
    return hashlib.sha256(f"{extractor.fingerprint}:{notice.content_hash}".encode()).hexdigest()


def revalidate(extraction: Extraction, notice: Notice | None = None,
               tender: Tender | None = None) -> Revalidation:
    """Re-run post-validation on the stored raw output with today's rules. No API call.

    tenders.eligibility.service.assess() calls this (via refresh) before every decision.
    """
    if extraction.status != Extraction.Status.OK or extraction.raw_output is None:
        return Revalidation(extraction.data, list(extraction.flags))
    tender = tender or extraction.tender
    # The input this extraction's prompt version reads: v4 rebuilds it with the documents.
    notice = notice or build_input(tender, extraction.prompt_version)
    flags = list(extraction.call_flags)
    if extraction.input_hash and notice.content_hash != extraction.input_hash:
        flags.append("stale_input")  # tender text changed since this extraction
    try:
        schema = SCHEMAS.get(extraction.schema_version, ExtractionData)
        raw = schema.model_validate(extraction.raw_output)
    except ValidationError:
        return Revalidation(None, [*flags, "raw_output_invalid"])
    checked, post_flags = postvalidate(raw, notice, tender.sa_required_from_metadata)
    return Revalidation(checked.model_dump(mode="json"), flags + post_flags)


def refresh(extraction: Extraction, notice: Notice | None = None,
            tender: Tender | None = None) -> Extraction:
    """Re-validate and persist ``data``/``flags`` if they changed."""
    a = revalidate(extraction, notice, tender)
    if (a.data, a.flags) != (extraction.data, extraction.flags):
        extraction.data, extraction.flags = a.data, a.flags
        extraction.save(update_fields=["data", "flags", "updated_at"])
    return extraction


def extract_tender(tender: Tender, extractor: Extractor, meter: CallMeter) -> Outcome:
    """Raises budget.StopRun (incl. CapReached) when the run must stop.

    The input is the one ``extractor.prompt_version`` reads (build_input); the caller picks
    the extractor per tender (extract_tenders: v4 for a tender with ok documents).
    """
    notice = build_input(tender, extractor.prompt_version)
    key = {"tender": tender, "content_hash": cache_hash(notice, extractor),
           "model_id": extractor.model_id, "prompt_version": extractor.prompt_version}
    cached = Extraction.objects.filter(**key, status=Extraction.Status.OK).first()
    if cached:
        return Outcome(tender.reference_number, "cache_hit", refresh(cached, notice, tender))

    result = extractor.extract(notice, meter, tender=tender)
    call_flags = ((["truncated"] if notice.truncated else [])
                  + (["documents_cut"] if notice.documents_cut else []) + result.flags)
    # A failed row is retried on the next run (only status=ok counts as a cache hit).
    extraction, _ = Extraction.objects.update_or_create(
        **key,
        defaults={
            "schema_version": getattr(extractor, "schema_version", SCHEMA_VERSION),
            "status": result.status, "input_hash": notice.content_hash,
            "documents": notice.documents_record(),
            "raw_output": result.data.model_dump(mode="json") if result.data else None,
            "call_flags": call_flags, "data": None, "flags": call_flags, "error": result.error,
        },
    )
    return Outcome(tender.reference_number, result.status, refresh(extraction, notice, tender))
