import typing

from django.conf import settings
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
    notice_url = models.TextField()  # the CSV's own link, or "" (never a guessed URL)
    # The CSV's attachment URLs, any host. tenders.documents fetches only CanadaBuys ones.
    attachment_urls = models.JSONField(default=list, blank=True)
    notice_url_derived = models.BooleanField(default=False)  # legacy, always False: see 0012
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


class TenderDocument(models.Model):
    """One attachment URL of a tender, and what fetching it gave (tenders.documents).

    Holds only the redacted text, never the PDF. Not shown on any public page and not in
    the prompt yet. One row per (tender, url) once it was looked at, including skipped
    and failed ones. Only transient failures are tried again, on later runs, up to 3
    attempts (tenders.documents.service.RETRYABLE, MAX_ATTEMPTS).
    """

    class Status(models.TextChoices):
        OK = "ok", "OK"
        SKIPPED_HOST = "skipped_host", "Skipped: not a CanadaBuys attachment URL"
        OVER_TENDER_LIMIT = "over_tender_limit", "Skipped: over the per-tender limit"
        TOO_BIG = "too_big", "Too big"
        TOO_MANY_PAGES = "too_many_pages", "Too many pages"
        NOT_PDF = "not_pdf", "Not a PDF"
        TIMEOUT = "timeout", "Download timed out"
        REDIRECTED = "redirected", "Redirected (not followed)"
        HTTP_ERROR = "http_error", "HTTP error"
        NETWORK_ERROR = "network_error", "Network error"
        PARSE_TIMEOUT = "parse_timeout", "PDF parsing timed out"
        PARSE_ERROR = "parse_error", "PDF parsing failed"

    tender = models.ForeignKey(Tender, on_delete=models.CASCADE, related_name="documents")
    url = models.TextField()
    status = models.CharField(max_length=24, choices=Status.choices)
    text = models.TextField(blank=True, help_text="Redacted. Never served publicly.")
    pages = models.PositiveIntegerField(null=True, blank=True)
    sha256 = models.CharField(max_length=64, blank=True)  # of the downloaded bytes
    # Rule names from tenders.ingest.injection.scan_text, never the matched text.
    injection_patterns = models.JSONField(default=list, blank=True)
    attempts = models.PositiveSmallIntegerField(default=1)  # downloads tried, retries included
    fetched_at = models.DateTimeField()  # of the latest attempt

    class Meta:
        constraints = (models.UniqueConstraint(fields=["tender", "url"],
                                               name="uniq_tender_document_url"),)

    def __str__(self) -> str:
        return f"{self.tender_id}: {self.status} {self.url.rsplit('/', 1)[-1]}"


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


class EvalExtraction(models.Model):
    """One extra LLM run on an eval case (run_eval --repeats), to measure run-to-run
    consistency. Bypasses the Extraction cache and never replaces a cached Extraction, so
    the site never shows it. ``raw_output`` is stored as returned; scoring re-runs
    post-validation on it, as the site does for an Extraction."""

    run_tag = models.CharField(max_length=64, db_index=True)
    tender = models.ForeignKey(Tender, on_delete=models.CASCADE, related_name="eval_extractions")
    repeat = models.PositiveSmallIntegerField(help_text="1..N within the run.")
    model_id = models.CharField(max_length=100)
    prompt_version = models.CharField(max_length=20)
    schema_version = models.CharField(max_length=20)
    config_fingerprint = models.CharField(max_length=64)
    input_hash = models.CharField(max_length=64)
    status = models.CharField(max_length=10, choices=Extraction.Status.choices)
    raw_output = models.JSONField(null=True, blank=True)
    call_flags = models.JSONField(default=list, blank=True)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=["run_tag", "tender", "repeat"],
                                    name="eval_extraction_per_run_repeat"),
        )

    def __str__(self) -> str:
        return f"{self.run_tag} {self.tender_id} #{self.repeat} {self.status}"


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


CLEARANCE_CHOICES = [
    ("none", "None"), ("reliability", "Reliability"), ("secret", "Secret"),
    ("top_secret", "Top Secret"),
]


class SupplierProfile(models.Model):
    """The (fictional) firm whose eligibility we assess. The lowest id is the default."""

    name = models.CharField(max_length=200, unique=True)
    capabilities = models.JSONField(default=list, blank=True)
    supply_arrangements = models.JSONField(
        default=list, blank=True, help_text="Names held, e.g. TBIPS, ProServices.")
    corporate_clearance = models.CharField(max_length=12, choices=CLEARANCE_CHOICES,
                                           default="none")
    personnel_clearance = models.CharField(max_length=12, choices=CLEARANCE_CHOICES,
                                           default="none")
    work_locations = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.name


class Assessment(models.Model):
    """Eligibility of one extraction for one profile. Recomputed by
    tenders.eligibility.service.assess(); ``inputs_hash`` covers everything it depends on."""

    class Status(models.TextChoices):
        BLOCKED = "blocked", "Blocked"
        CHECK_SOLICITATION = "check_solicitation", "Check solicitation"
        NO_BLOCKERS_STATED = "no_blockers_stated", "No blockers stated"

    tender = models.ForeignKey(Tender, on_delete=models.CASCADE, related_name="assessments")
    extraction = models.ForeignKey(Extraction, on_delete=models.CASCADE,
                                   related_name="assessments")
    profile = models.ForeignKey(SupplierProfile, on_delete=models.CASCADE,
                                related_name="assessments")
    status = models.CharField(max_length=20, choices=Status.choices)
    blockers = models.JSONField(default=list, blank=True)  # [{gate, reason, evidence}]
    warnings = models.JSONField(default=list, blank=True)
    notes = models.JSONField(default=list, blank=True)
    not_stated = models.JSONField(default=list, blank=True,
                                  help_text="Gates whose value is unknown.")
    rules_version = models.CharField(max_length=20)
    inputs_hash = models.CharField(max_length=64)
    computed_at = models.DateTimeField()

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=["extraction", "profile"],
                                    name="assessment_per_extraction_profile"),
        )

    def __str__(self) -> str:
        return f"{self.tender_id} {self.status}"


class AppendOnlyError(Exception):
    """A ReviewDecision is never edited or deleted: record a newer decision instead."""


class ReviewDecisionQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise AppendOnlyError("review decisions are append-only")

    def delete(self):
        raise AppendOnlyError("review decisions are append-only")


class ReviewDecision(models.Model):
    """A person's verdict on one gate (or criteria_location) of one extraction.

    Append-only (enforced here, on the queryset and by a database trigger, migration 0009):
    the newest decision for (extraction, gate) is the one that counts. tenders.review applies
    it before the eligibility rules run; tenders.eligibility.service.assess() uses it.
    - accept: the model's (post-validated) value is right.
    - correct: ``corrected_value`` replaces it, with ``evidence`` quoted from the notice
      (checked like model evidence). Gate value fields and detail fields, no evidence key;
      for criteria_location, {"value": ...}.
    - reject: the model's value is wrong; the gate is treated as unknown.
    """

    class Action(models.TextChoices):
        ACCEPT = "accept", "Accept"
        CORRECT = "correct", "Correct"
        REJECT = "reject", "Reject"

    GATE_CHOICES: typing.ClassVar = [
        ("supply_arrangement_required", "Supply arrangement required"),
        ("supplier_origin_restriction", "Supplier origin restriction"),
        ("clearance", "Security clearance"),
        ("work_location", "Work location"),
        ("land_claims_applies", "Land claims agreement"),
        ("documents_behind_login", "Documents behind a login"),
        ("criteria_location", "Where the evaluation criteria are"),
    ]

    extraction = models.ForeignKey(Extraction, on_delete=models.PROTECT,
                                   related_name="review_decisions")
    gate = models.CharField(max_length=32, choices=GATE_CHOICES)
    action = models.CharField(max_length=10, choices=Action.choices)
    corrected_value = models.JSONField(null=True, blank=True)
    evidence = models.TextField(blank=True)
    reviewer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
                                 related_name="review_decisions")
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = ReviewDecisionQuerySet.as_manager()

    class Meta:
        ordering = ("extraction_id", "gate", "-created_at", "-id")
        constraints = (
            models.CheckConstraint(
                condition=models.Q(action="correct", corrected_value__isnull=False)
                | (~models.Q(action="correct") & models.Q(corrected_value__isnull=True,
                                                          evidence="")),
                name="review_corrected_value_only_on_correct",
            ),
        )

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AppendOnlyError("review decisions are append-only")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError("review decisions are append-only")

    def __str__(self) -> str:
        return f"{self.extraction_id} {self.gate} {self.action}"


class CarriedReview(models.Model):
    """A review decision considered for a newer extraction of the same notice (same model).

    Not a decision and not a copy: ``source`` is the person's original ReviewDecision, so
    the reviewer and time stay theirs. Written by tenders.review.carry_forward() when the
    newer extraction is assessed; never edited (the newest row per gate counts).
    - Same LLM input as the extraction before: the decision carries as it stood.
    - Changed input: a correction carries if its quote (and details) still pass the
      grounding check on the new text; a reject or accept carries if the model's new
      answer equals the one that was judged. Otherwise ``applied`` is False and the gate
      needs re-review.
    """

    class Reason(models.TextChoices):
        SAME_INPUT = "same_input", "same notice text"
        QUOTE_FOUND = "quote_found", "notice text changed; the correction's quote is still in it"
        QUOTE_GONE = "quote_gone", "notice text changed; the correction's quote is no longer in it"
        NO_QUOTE = "no_quote", "notice text changed; a criteria-location correction has no quote to re-check"
        SAME_ANSWER = "same_answer", "notice text changed; the model gave the same answer"
        ANSWER_CHANGED = "answer_changed", "notice text changed; the model's answer changed"

    extraction = models.ForeignKey(Extraction, on_delete=models.CASCADE,
                                   related_name="carried_reviews")
    gate = models.CharField(max_length=32, choices=ReviewDecision.GATE_CHOICES)
    source = models.ForeignKey(ReviewDecision, on_delete=models.PROTECT, related_name="carried")
    applied = models.BooleanField()
    reason = models.CharField(max_length=20, choices=Reason.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = (
            models.UniqueConstraint(fields=["extraction", "gate", "source"],
                                    name="carried_review_once_per_source"),
        )

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AppendOnlyError("carried reviews are never edited; a newer one supersedes")
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.extraction_id} {self.gate} from {self.source_id} applied={self.applied}"


class NeedsReview(Tender):
    """Proxy for the admin's "Needs review" queue (tenders.admin.NeedsReviewAdmin)."""

    class Meta:
        proxy = True
        verbose_name = "notice needing review"
        verbose_name_plural = "needs review"
