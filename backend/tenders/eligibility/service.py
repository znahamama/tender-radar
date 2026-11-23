"""Compute and store Assessments. No API calls: everything here re-runs on stored data.

Human review overrides the model here, not in the frozen rules: the latest review decision
per gate (tenders.review), including decisions carried from earlier extractions of the same
notice that still hold, is applied to the post-validated extraction before evaluate().
A documents extraction (v4, v5) is first combined per gate with the tender's v3 one
(eligibility.combine), so review decisions still override either version.

Rules v4 (eligibility.unread): after the v3 rules, stored document text the model did not
see is searched for clearance and on-site terms when those gates are not stated.

Scope (tenders.scope: is this IT work, from the category codes) is stored on the same row but
is never an input to the status: a scope change rewrites only ``scope``, not ``computed_at``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from django.utils import timezone

from tenders.extraction.service import refresh
from tenders.models import Assessment, Extraction, SupplierProfile
from tenders.review import apply_reviews, carry_forward, reviews_for
from tenders.scope import notice_scope, record

from . import unread
from .combine import combine, notice_only_partner
from .rules import Profile, TenderFacts, evaluate
from .unread import RULES_VERSION, UnreadText


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
    partner = notice_only_partner(extraction) if data is not None else None
    if partner is not None:
        refresh(partner, tender=tender)
    combined = combine(data, partner.data if partner else None, extraction.prompt_version)
    data = combined.data
    carry_forward(extraction)  # decisions on earlier extractions of this notice, re-checked
    reviews = reviews_for(extraction).applied
    data = apply_reviews(data, reviews)
    decision = evaluate(data, list(extraction.flags), facts, prof, extraction.error)
    hits = unread.find_hits(unread_texts(extraction)) if unread.needed(decision) else []
    decision = unread.apply(decision, hits)

    inputs = {"data": data, "flags": extraction.flags, "error": extraction.error,
              "facts": [facts.sa_required_from_metadata, facts.notice_type,
                        facts.notice_kind],
              "profile": [prof.supply_arrangements, prof.corporate_clearance,
                          prof.personnel_clearance], "rules": RULES_VERSION}
    if partner is not None:  # only then, so assessments of other extractions keep their hash
        inputs["combined"] = [partner.pk, partner.data, partner.flags]
    if hits:  # only when there are any
        inputs["unread"] = [[h.gate, h.term, h.document, h.page] for h in hits]
    if reviews:  # only when there are any, so unreviewed assessments keep their hash
        inputs["reviews"] = {g: [d.action, d.pk, d.extraction_id != extraction.pk]
                             for g, d in sorted(reviews.items())}
    fields = {**decision.as_fields(), "rules_version": RULES_VERSION,
              "combined": combined.record(partner),
              "inputs_hash": hashlib.sha256(
                  json.dumps(inputs, sort_keys=True, default=str).encode()).hexdigest()}

    scope = record(notice_scope(tender))
    existing = Assessment.objects.filter(extraction=extraction, profile=profile).first()
    if existing and all(getattr(existing, k) == v for k, v in fields.items()):
        if existing.scope != scope:
            existing.scope = scope
            existing.save(update_fields=["scope"])
        return existing
    assessment, _ = Assessment.objects.update_or_create(
        extraction=extraction, profile=profile,
        defaults={"tender": tender, "computed_at": timezone.now(), "scope": scope, **fields},
    )
    return assessment


def unread_texts(extraction: Extraction) -> list[UnreadText]:
    """The stored text of each ok document of the tender that this extraction's model did
    not see: past the cut of a partial one, all of one left out at the cap, and all of
    every ok document the input didn't list (a notice-only extraction, or a document fetched
    after it). Input order first, then the unlisted ones in id order."""
    from tenders.documents.service import _filename
    from tenders.extraction.prompt import FULL, PARTIAL

    given = {d.get("url"): (i, d) for i, d in enumerate(extraction.documents or [])}
    docs = (extraction.tender.documents.filter(status="ok").exclude(text="")
            .only("url", "text", "pages", "pages_read").order_by("id"))
    out = []
    for doc in docs:
        pos, rec = given.get(doc.url, (len(given), None))
        status = rec.get("status") if rec else None
        if status == FULL:
            continue
        start = int(rec.get("chars") or 0) if status == PARTIAL else 0
        name = rec.get("name") if rec else _filename(doc.url)
        out.append((pos, UnreadText(name, doc.text, start, doc.pages_read or doc.pages)))
    return [u for _, u in sorted(out, key=lambda x: x[0])]


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
