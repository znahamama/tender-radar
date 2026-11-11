from django.db import models


class Tender(models.Model):
    """A kept CanadaBuys notice. Holds no contact data: the description is stored redacted,
    and contactInfo* columns never reach this model (see tenders.ingest.redact)."""

    class RegionScope(models.TextChoices):
        NL = "nl", "NL"
        NATIONAL = "national", "National"
        NL_NATIONAL = "nl+national", "NL + national"

    class NoticeKind(models.TextChoices):
        REGULAR = "regular", "Regular"
        SA_OR_SO_REQUEST = "sa_or_so_request", "Request for SA / standing offer"
        AGAINST_SA = "against_sa", "Competition against an SA"

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
    # True: metadata says only SA holders may bid. None: unknown (decided later, not False).
    sa_required_from_metadata = models.BooleanField(null=True, blank=True)
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


class Extraction(models.Model):
    """LLM-extracted fields for one version of a tender's text under one extractor config.

    The unique key makes re-runs cache hits: same text and config means no API call.
    ``raw_output`` is the source of truth; ``data`` and ``flags`` are the latest assessment
    (post-validation re-run on read, see tenders.extraction.service.assess).
    """

    class Status(models.TextChoices):
        OK = "ok", "OK"
        FAILED = "failed", "Failed"

    tender = models.ForeignKey(Tender, on_delete=models.CASCADE, related_name="extractions")
    content_hash = models.CharField(
        max_length=64,
        help_text="Cache key: sha256 of the extractor config fingerprint and the prompt input.")
    input_hash = models.CharField(max_length=64, blank=True,
                                  help_text="sha256 of the prompt input alone.")
    model_id = models.CharField(max_length=100)
    prompt_version = models.CharField(max_length=20)
    schema_version = models.CharField(max_length=20)
    status = models.CharField(max_length=10, choices=Status.choices)
    raw_output = models.JSONField(null=True, blank=True,
                                  help_text="Schema-valid model output, before post-validation.")
    call_flags = models.JSONField(default=list, blank=True,
                                  help_text="Flags from the call itself (retries, truncation).")
    data = models.JSONField(null=True, blank=True, help_text="Latest post-validated output.")
    flags = models.JSONField(default=list, blank=True, help_text="call_flags + latest checks.")
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(
                fields=["tender", "content_hash", "model_id", "prompt_version"],
                name="extraction_cache_key",
            ),
        )

    def __str__(self) -> str:
        return f"{self.tender_id} {self.model_id} {self.prompt_version} {self.status}"


class LlmCall(models.Model):
    """One API attempt (the SDK does no hidden retries). Drives the daily cost cap."""

    class Status(models.TextChoices):
        OK = "ok", "OK"
        INVALID = "invalid", "Schema-invalid output"
        REFUSAL = "refusal", "Refusal"
        ERROR = "error", "API error"

    purpose = models.CharField(max_length=20, default="extract")
    tender = models.ForeignKey(Tender, null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="llm_calls")
    model_id = models.CharField(max_length=100)
    prompt_version = models.CharField(max_length=20)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    latency_ms = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=10, choices=Status.choices)
    request_id = models.CharField(max_length=100, blank=True)
    estimated = models.BooleanField(
        default=False, help_text="Tokens and cost are a worst-case estimate (timeout or "
                                 "connection error), so the daily cap still counts the call.")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    def __str__(self) -> str:
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.purpose} {self.model_id} {self.status}"
