"""Compute and store Assessments. No API calls: everything here re-runs on stored data.

Human review overrides the model here, not in the frozen rules: the latest review decision
per gate (tenders.review) is applied to the post-validated extraction before evaluate().
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from django.utils import timezone

from tenders.extraction.service import refresh
from tenders.models import Assessment, Extraction, SupplierProfile
from tenders.review import apply_reviews, latest_decisions

from .rules import RULES_VERSION, Profile, TenderFacts, evaluate


def default_profile() -> SupplierProfile:
    profile = SupplierProfile.objects.order_by("id").first()
    if profile is None:
        raise SupplierProfile.DoesNotExist("No SupplierProfile: run migrations (0006 seeds one).")
    return profile


def assess(extraction: Extraction, profile: SupplierProfile | None = None) -> Assessment:
    """(Re)compute the assessment of one extraction for one profile.

    Re-validates the extraction first (grounding rules may have changed), applies the latest
    human review decisions (a correction replaces the gate's value, a rejection makes it
    unknown), then applies the rules. Writes only when the result or its inputs changed, so
    ``computed_at`` is the time the answer last changed. Call it whenever the extraction, a
    review decision, the validation rules or the profile change; reassess() does it in bulk.
    """
    profile = profile or default_profile()
    tender = extraction.tender
    refresh(extraction, tender=tender)
    facts = TenderFacts(tender.sa_required_from_metadata, tender.notice_type or "",
                        tender.notice_kind or "")
    prof = Profile.from_model(profile)
    data = extraction.data if extraction.status == Extraction.Status.OK else None
    reviews = latest_decisions(extraction)
    data = apply_reviews(data, reviews)
    decision = evaluate(data, list(extraction.flags), facts, prof, extraction.error)

    inputs = {"data": data, "flags": extraction.flags, "error": extraction.error,
              "facts": [facts.sa_required_from_metadata, facts.notice_type,
                        facts.notice_kind],
              "profile": [prof.supply_arrangements, prof.corporate_clearance,
                          prof.personnel_clearance], "rules": RULES_VERSION}
    if reviews:  # only when there are any, so unreviewed assessments keep their hash
        inputs["reviews"] = {g: [d.action, d.pk] for g, d in sorted(reviews.items())}
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
    qs = Extraction.objects.select_related("tender").prefetch_related(
        "review_decisions").order_by(
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
