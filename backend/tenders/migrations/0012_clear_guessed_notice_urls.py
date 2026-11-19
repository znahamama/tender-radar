"""Clear the notice URLs that ingest used to guess, and rehash those rows.

Before this, a notice with no URL in the CSV got
https://canadabuys.canada.ca/en/tender-opportunities/tender-notice/<reference> and
notice_url_derived=True. That path is not how CanadaBuys builds its page slugs, so many of
those links were 404s. Ingest no longer builds a URL (tenders.ingest.schema); this makes
the stored rows match: notice_url "" and notice_url_derived False.

content_hash is recomputed for the changed rows, so the next ingest finds them unchanged
instead of counting them all as updated. The hash rule is copied from
tenders.ingest.storage as it is today, as 0011 did, so this migration keeps meaning what it
meant when it ran. Reversing it is a no-op: the guessed URLs are not rebuilt.
"""

import hashlib
import json

from django.db import migrations

EXCLUDED = {"id", "content_hash", "first_seen_at", "last_seen_at", "updated_at"}
UNORDERED_FIELDS = (
    "trade_agreements", "regions_of_delivery", "regions_of_opportunity",
    "procurement_categories", "unspsc_codes", "gsin_codes",
)


def _hash(fields: dict) -> str:
    canon_fields = {**fields, **{f: sorted(fields[f]) for f in UNORDERED_FIELDS if f in fields}}
    canon = json.dumps(canon_fields, sort_keys=True, default=lambda v: v.isoformat(),
                       ensure_ascii=False)
    return hashlib.sha256(canon.encode()).hexdigest()


def forwards(apps, schema_editor):
    Tender = apps.get_model("tenders", "Tender")
    names = [f.name for f in Tender._meta.concrete_fields if f.name not in EXCLUDED]
    changed = []
    for t in Tender.objects.filter(notice_url_derived=True).iterator(chunk_size=500):
        t.notice_url = ""
        t.notice_url_derived = False
        t.content_hash = _hash({n: getattr(t, n) for n in names})
        changed.append(t)
    Tender.objects.bulk_update(changed, ["notice_url", "notice_url_derived", "content_hash"],
                               batch_size=500)


class Migration(migrations.Migration):
    dependencies = [("tenders", "0011_order_insensitive_content_hash")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
