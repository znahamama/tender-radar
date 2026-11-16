from functools import cache

from django.db.models import Max

from tenders.models import Tender


def site(request):
    """``last_ingest``: when the newest ingest last saw a notice.

    A callable, so the query runs only for templates that print it (the public footer),
    never for the admin, and at most once per request.
    """

    @cache
    def last_ingest():
        return Tender.objects.aggregate(m=Max("last_seen_at"))["m"]

    return {"last_ingest": last_ingest}
