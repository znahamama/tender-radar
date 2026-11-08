from django.db import models


class Tender(models.Model):
    """A kept CanadaBuys notice. Holds no contact data: the description is stored redacted,
    and contactInfo* columns never reach this model (see tenders.ingest.redact)."""

    class RegionScope(models.TextChoices):
        NL = "nl", "NL"
        NATIONAL = "national", "National"
        NL_NATIONAL = "nl+national", "NL + national"

    class NoticeKind(models.TextChoices):
        STANDING_OR_ONGOING = "standing_or_ongoing", "Standing / ongoing"
        REGULAR = "regular", "Regular"

    reference_number = models.CharField(max_length=100, unique=True)
    solicitation_number = models.CharField(max_length=200, blank=True)
    amendment_number = models.PositiveIntegerField(null=True, blank=True)
    amendment_date = models.DateTimeField(null=True, blank=True)
    title = models.TextField()
    description = models.TextField(blank=True, help_text="Redacted before storage.")
    publication_date = models.DateTimeField(null=True, blank=True)
    closing_date = models.DateTimeField(null=True, blank=True, db_index=True)
    tender_status = models.CharField(max_length=50, blank=True)
    notice_type = models.CharField(max_length=100, blank=True)
    procurement_categories = models.JSONField(default=list, blank=True)
    unspsc_codes = models.JSONField(default=list, blank=True)
    gsin_codes = models.JSONField(default=list, blank=True)
    regions_of_delivery = models.JSONField(default=list, blank=True)
    regions_of_opportunity = models.JSONField(default=list, blank=True)
    trade_agreements = models.JSONField(default=list, blank=True)
    limited_tendering_reason = models.CharField(max_length=200, blank=True)
    notice_url = models.TextField()
    notice_url_derived = models.BooleanField(default=False)
    buyer_org_name = models.CharField(max_length=300, blank=True)

    is_nl = models.BooleanField(default=False)
    is_it = models.BooleanField(default=False)
    region_scope = models.CharField(max_length=16, choices=RegionScope.choices)
    notice_kind = models.CharField(max_length=24, choices=NoticeKind.choices)
    data_quality = models.JSONField(default=list, blank=True)
    redaction_counts = models.JSONField(default=dict, blank=True)

    content_hash = models.CharField(max_length=64)
    first_seen_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    updated_at = models.DateTimeField()

    class Meta:
        ordering = ("closing_date",)

    def __str__(self) -> str:
        return f"{self.reference_number}: {self.title}"
