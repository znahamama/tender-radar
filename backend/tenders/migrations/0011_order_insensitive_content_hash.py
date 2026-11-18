"""Recompute Tender.content_hash with list order ignored (tenders.ingest.storage).

Stored values are not changed. Without this, the first ingest after the hash change would
count every notice with a multi-item list as "updated" and save today's order, changing
the LLM input and causing re-extractions. The hash rule is copied here on purpose, so this
migration keeps meaning what it meant when it ran.
"""

import hashlib
import json

from django.db import migrations

EXCLUDED = {"id", "content_hash", "first_seen_at", "last_seen_at", "updated_at"}
UNORDERED_FIELDS = (
    "trade_agreements", "regions_of_delivery", "regions_of_opportunity",
    "procurement_categories", "unspsc_codes", "gsin_codes",
)


def _hash(fields: dict, unordered: tuple) -> str:
    canon_fields = {**fields, **{f: sorted(fields[f]) for f in unordered if f in fields}}
    canon = json.dumps(canon_fields, sort_keys=True, default=lambda v: v.isoformat(),
                       ensure_ascii=False)
    return hashlib.sha256(canon.encode()).hexdigest()


def _rehash(apps, unordered: tuple) -> None:
    Tender = apps.get_model("tenders", "Tender")
    names = [f.name for f in Tender._meta.concrete_fields if f.name not in EXCLUDED]
    changed = []
    for t in Tender.objects.all().iterator(chunk_size=500):
        h = _hash({n: getattr(t, n) for n in names}, unordered)
        if h != t.content_hash:
            t.content_hash = h
            changed.append(t)
    Tender.objects.bulk_update(changed, ["content_hash"], batch_size=500)


def forwards(apps, schema_editor):
    _rehash(apps, UNORDERED_FIELDS)


def backwards(apps, schema_editor):
    _rehash(apps, ())  # the order-sensitive hash


class Migration(migrations.Migration):
    dependencies = [("tenders", "0010_carried_review")]
    operations = [migrations.RunPython(forwards, backwards)]
