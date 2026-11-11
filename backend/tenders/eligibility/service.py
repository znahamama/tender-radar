"""Compute and store Assessments. No API calls: everything here re-runs on stored data."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from django.utils import timezone

from tenders.extraction.service import refresh
from tenders.models import Assessment, Extraction, SupplierProfile

from .rules import RULES_VERSION, Profile, TenderFacts, evaluate


def default_profile() -> SupplierProfile:
    profile = SupplierProfile.objects.order_by("id").first()
    if profile is None:
        raise SupplierProfile.DoesNotExist("No SupplierProfile: run migrations (0006 seeds one).")
    return profile


def assess(extraction: Extraction, profile: SupplierProfile | None = None) -> Assessment:
    """(Re)compute the assessment of one extraction for one profile.

    Re-validates the extraction first (grounding rules may have changed), then applies the
    rules. Writes only when the result or its inputs changed, so ``computed_at`` is the time
    the answer last changed. Call it whenever the extraction, the validation rules or the
    profile change; reassess() does it in bulk.
    """
    profile = profile or default_profile()
    tender = extraction.tender
    refresh(extraction, tender=tender)
    facts = TenderFacts(tender.sa_required_from_metadata, tender.notice_type or "")
    prof = Profile.from_model(profile)
    data = extraction.data if extraction.status == Extraction.Status.OK else None
    decision = evaluate(data, list(extraction.flags), facts, prof, extraction.error)

    inputs = {"data": data, "flags": extraction.flags, "error": extraction.error,
              "facts": [facts.sa_required_from_metadata, facts.notice_type],
              "profile": [prof.supply_arrangements, prof.corporate_clearance,
                          prof.personnel_clearance], "rules": RULES_VERSION}
    fields = {**decision.as_fields(), "rules_version": RULES_VERSION,
              "inputs_hash": hashlib.sha256(
                  json.dumps(inputs, sort_keys=True, default=str).encode()).hexdigest()}

    existing = Assessment.objects.filter(extraction=extraction, profile=profile).first()
    if existing and all(getattr(existing, k) == v for k, v in fields.items()):
        return existing
    assessment, _ = Assessment.objects.update_or_create(
        extraction=extraction, profile=profile,
        defaults={"tender": tender, "computed_at": timezone.now(), **fields},
    )
    return assessment


def latest_extractions(model_ids: Iterable[str] | None = None,
                       tender_status: str | None = None) -> list[Extraction]:
    """The newest extraction per (tender, model_id)."""
    qs = Extraction.objects.select_related("tender").order_by(
        "tender_id", "model_id", "-created_at", "-id")
    if model_ids:
        qs = qs.filter(model_id__in=list(model_ids))
    if tender_status:
        qs = qs.filter(tender__tender_status__iexact=tender_status)
    seen, latest = set(), []
    for x in qs:
        if (x.tender_id, x.model_id) not in seen:
            seen.add((x.tender_id, x.model_id))
            latest.append(x)
    return latest


def reassess(profile: SupplierProfile | None = None, model_ids: Iterable[str] | None = None,
             tender_status: str | None = None) -> list[Assessment]:
    """Recompute assessments for the latest extractions (after a profile or rule change)."""
    profile = profile or default_profile()
    return [assess(x, profile) for x in latest_extractions(model_ids, tender_status)]
