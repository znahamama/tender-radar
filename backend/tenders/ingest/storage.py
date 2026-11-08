"""Upsert kept notices into ``Tender`` with a constant number of queries per batch."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from django.db import transaction

from tenders.models import Tender

from . import filters
from .schema import TenderNotice

BATCH = 500
# Fields compared via content_hash (everything except bookkeeping timestamps and the hash).
CONTENT_FIELDS = [
    f.name for f in Tender._meta.concrete_fields
    if f.name not in {"id", "content_hash", "first_seen_at", "last_seen_at", "updated_at"}
]


@dataclass
class UpsertResult:
    created: int = 0
    updated: int = 0
    unchanged: int = 0


def tender_fields(n: TenderNotice) -> dict:
    """Map a (redacted) notice to Tender column values. None -> "" for text columns."""
    data = n.model_dump(exclude={"description"})
    data["description"] = n.description or ""  # already redacted by the pipeline
    for k in ("solicitation_number", "tender_status", "notice_type",
              "limited_tendering_reason", "buyer_org_name"):
        data[k] = data[k] or ""
    data["is_nl"] = filters.is_nl(n.regions_of_delivery)
    data["is_it"] = filters.notice_is_it(n)
    data["region_scope"] = filters.region_scope(n.regions_of_delivery) or ""
    data["notice_kind"] = filters.notice_kind(n)
    data["sa_required_from_metadata"] = filters.sa_required_from_metadata(n)
    if mismatch := set(CONTENT_FIELDS) ^ set(data):
        raise RuntimeError(f"Tender/TenderNotice field mismatch: {sorted(mismatch)}")
    return data


def content_hash(fields: dict) -> str:
    canon = json.dumps(fields, sort_keys=True, default=lambda v: v.isoformat(), ensure_ascii=False)
    return hashlib.sha256(canon.encode()).hexdigest()


def _chunks(seq: list, size: int = BATCH):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def upsert(notices: Iterable[TenderNotice], now: datetime | None = None) -> UpsertResult:
    now = now or datetime.now(UTC)
    incoming: dict[str, tuple[dict, str]] = {}
    for n in notices:
        f = tender_fields(n)
        incoming[n.reference_number] = (f, content_hash(f))

    refs = list(incoming)
    existing: dict[str, tuple[int, str]] = {}
    for chunk in _chunks(refs):
        for ref, pk, h in Tender.objects.filter(reference_number__in=chunk).values_list(
            "reference_number", "id", "content_hash"
        ):
            existing[ref] = (pk, h)

    to_create, to_update, unchanged_ids = [], [], []
    for ref, (f, h) in incoming.items():
        if ref not in existing:
            to_create.append(Tender(**f, content_hash=h, first_seen_at=now, last_seen_at=now,
                                    updated_at=now))
        elif existing[ref][1] != h:
            to_update.append(Tender(id=existing[ref][0], **f, content_hash=h, last_seen_at=now,
                                    updated_at=now))
        else:
            unchanged_ids.append(existing[ref][0])

    with transaction.atomic():
        Tender.objects.bulk_create(to_create, batch_size=BATCH)
        Tender.objects.bulk_update(
            to_update, [*CONTENT_FIELDS, "content_hash", "last_seen_at", "updated_at"],
            batch_size=BATCH,
        )
        for chunk in _chunks(unchanged_ids):
            Tender.objects.filter(id__in=chunk).update(last_seen_at=now)

    return UpsertResult(len(to_create), len(to_update), len(unchanged_ids))
