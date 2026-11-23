import json

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.models import DELETION, LogEntry
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import (
    Case,
    Count,
    IntegerField,
    OuterRef,
    Q,
    Subquery,
    TextField,
    When,
)
from django.db.models.functions import Cast, Coalesce
from django.http import (
    HttpResponseBadRequest,
    HttpResponseNotAllowed,
    HttpResponseRedirect,
)
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html

from . import review
from .documents import upload
from .eligibility.combine import V3, VERSIONS_DISAGREE, combined_for
from .ingest.injection import INJECTION_PATTERN
from .models import (
    Assessment,
    Extraction,
    LlmCall,
    NeedsReview,
    ReviewDecision,
    SupplierProfile,
    Tender,
    TenderDocument,
)
from .pages import (
    GATE_LABELS,
    current_extraction,
    current_extractions,
    gate_flags,
    gate_value,
    is_open,
    primary_assessments,
    reference_or_404,
)


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Tender)
class TenderAdmin(ReadOnlyAdmin):
    """Read-only: tenders are written by the ingest command only."""

    list_display = ("title", "tender_status", "region_scope", "is_it", "notice_kind", "closing_date")
    list_filter = ("tender_status", "region_scope", "is_it", "notice_kind", "closing_date")
    search_fields = ("title", "reference_number")
    date_hierarchy = "closing_date"


@admin.register(Extraction)
class ExtractionAdmin(ReadOnlyAdmin):
    """Read-only: written by the extract_tenders command only."""

    list_display = ("tender", "status", "model_id", "prompt_version", "updated_at")
    list_filter = ("status", "model_id", "prompt_version")
    search_fields = ("tender__reference_number",)


@admin.register(LlmCall)
class LlmCallAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "purpose", "model_id", "status", "input_tokens",
                    "output_tokens", "cost_usd", "latency_ms")
    list_filter = ("purpose", "model_id", "status")
    date_hierarchy = "created_at"


@admin.register(Assessment)
class AssessmentAdmin(ReadOnlyAdmin):
    """Read-only: computed by tenders.eligibility.service.assess()."""

    list_display = ("tender", "model_id", "status", "blocker_reasons", "warning_reasons",
                    "not_stated_gates", "computed_at")
    list_filter = ("status", "extraction__model_id", "profile")
    search_fields = ("tender__reference_number", "tender__title")
    list_select_related = ("tender", "extraction")

    @admin.display(description="model", ordering="extraction__model_id")
    def model_id(self, obj):
        return obj.extraction.model_id

    @admin.display(description="blockers")
    def blocker_reasons(self, obj):
        return "; ".join(b["reason"] for b in obj.blockers) or "-"

    @admin.display(description="warnings")
    def warning_reasons(self, obj):
        return "; ".join(w["reason"] for w in obj.warnings) or "-"

    @admin.display(description="not stated")
    def not_stated_gates(self, obj):
        return ", ".join(obj.not_stated) or "-"


@admin.register(SupplierProfile)
class SupplierProfileAdmin(admin.ModelAdmin):
    """Editable. Saving re-assesses the latest extractions for this profile (no API calls)."""

    list_display = ("name", "corporate_clearance", "personnel_clearance", "updated_at")

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        from .eligibility.service import reassess

        reassess(profile=obj)


# --- review queue ----------------------------------------------------------------------

def can_review(user) -> bool:
    """Staff with the add-review permission (superusers have it). The admin site itself
    already turns away anonymous and non-staff users."""
    return user.is_active and user.is_staff and user.has_perm("tenders.add_reviewdecision")


# Extraction flags that put a notice above the unflagged ones. Gate-level only: a dropped
# deliverable doesn't change eligibility.
QUEUE_FLAGS = ("ungrounded:eligibility.", "ungrounded_detail:eligibility.",
               "sa_metadata_disagrees", "injection_suspected", "truncated")


def queue_flags(flags: list[str], data_quality: list[str],
                combined_flags: list[str] | None = None) -> list[str]:
    out = []
    if any(f.startswith(("ungrounded:eligibility.", "ungrounded_detail:eligibility."))
           for f in flags):
        out.append("ungrounded")
    out += [f for f in ("sa_metadata_disagrees", "injection_suspected", "truncated")
            if f in flags]
    if INJECTION_PATTERN in (data_quality or []):
        out.append(INJECTION_PATTERN)
    disagree = [f.removeprefix(f"{VERSIONS_DISAGREE}:eligibility.")
                for f in combined_flags or [] if f.startswith(f"{VERSIONS_DISAGREE}:")]
    if disagree:  # v3 and v4 both quoted an answer for these gates, and the answers differ
        out.append(f"{VERSIONS_DISAGREE} ({', '.join(disagree)})")
    return out


def review_queue():
    """Open notices, current LLM extraction each (the one the site shows: latest
    last_used_at): blocked first, then flagged, then the rest; by closing date within each."""
    newest = current_extractions(OuterRef("pk"))
    status = primary_assessments().filter(tender=OuterRef("pk")).values("status")[:1]
    gates_reviewed = (ReviewDecision.objects.filter(extraction=OuterRef("review_extraction_id"))
                      .values("extraction").annotate(n=Count("gate", distinct=True))
                      .values("n")[:1])
    # jsonb as text ('["ungrounded:eligibility.clearance", ...]'): one LIKE per flag.
    flag_text = Subquery(newest.annotate(t=Cast("flags", TextField())).values("t")[:1],
                         output_field=TextField())
    combined_text = Subquery(primary_assessments().filter(tender=OuterRef("pk"))
                             .annotate(t=Cast("combined", TextField())).values("t")[:1],
                             output_field=TextField())
    qs = NeedsReview.objects.filter(is_open()).annotate(
        review_extraction_id=Subquery(newest.values("id")[:1]),
        review_extraction_status=Subquery(newest.values("status")[:1]),
        review_flags=flag_text,
        review_combined=combined_text,
        review_status=Subquery(status),
    ).annotate(
        gates_reviewed=Coalesce(Subquery(gates_reviewed, output_field=IntegerField()), 0),
        # The most recently current other extraction of this notice that a person reviewed.
        earlier_reviewed_id=Subquery(
            current_extractions(OuterRef("pk")).filter(review_decisions__isnull=False)
            .exclude(pk=OuterRef("review_extraction_id")).values("id")[:1]),
    )
    flagged = Q(data_quality__contains=[INJECTION_PATTERN])
    for f in QUEUE_FLAGS:
        flagged |= Q(review_flags__contains=f'"{f}')
    flagged |= Q(review_combined__contains=f'"{VERSIONS_DISAGREE}:')
    return qs.annotate(review_priority=Case(
        When(review_status=Assessment.Status.BLOCKED, then=0), When(flagged, then=1),
        default=2, output_field=IntegerField()))


class _DecisionForm(forms.Form):
    """Accept or reject one gate's model answer."""

    action = forms.ChoiceField(choices=[(a.value, a.label) for a in (
        ReviewDecision.Action.ACCEPT, ReviewDecision.Action.REJECT)])
    note = forms.CharField(required=False, max_length=1000)


class CorrectionForm(forms.Form):
    """One gate's corrected value, built from the frozen schema. clean() runs the same
    checks as model output, against the same input the extraction read
    (tenders.review.validate_correction)."""

    def __init__(self, *args, gate: str, extraction: Extraction, initial_value=None, **kwargs):
        super().__init__(*args, prefix=gate, **kwargs)
        self.gate, self.extraction, self.correction = gate, extraction, None
        current = initial_value if isinstance(initial_value, dict) else {
            "value": initial_value}
        for f, allowed in review.value_fields(gate).items():
            self.fields[f] = forms.ChoiceField(choices=[(v, v) for v in allowed],
                                               initial=current.get(f))
        for f in review.detail_fields(gate):
            self.fields[f] = forms.CharField(required=False, max_length=300,
                                             initial=current.get(f))
        if gate != review.CRITERIA:
            self.fields["evidence"] = forms.CharField(
                required=False, max_length=2000, widget=forms.Textarea(attrs={"rows": 2}),
                help_text="An exact quote from the notice or from a document this "
                          "extraction was given, as for the model.")
        self.fields["note"] = forms.CharField(required=False, max_length=1000)

    def clean(self):
        cleaned = super().clean()
        if self.errors:
            return cleaned
        fields = {f: cleaned.get(f) for f in (*review.value_fields(self.gate),
                                              *review.detail_fields(self.gate))}
        try:
            self.correction = review.validate_correction(self.extraction, self.gate, fields,
                                                         cleaned.get("evidence", ""))
        except ValidationError as e:
            self.add_error(None, e)
        return cleaned


@admin.register(NeedsReview)
class NeedsReviewAdmin(admin.ModelAdmin):
    """Open notices to check, worst first. Each row opens the review page of the extraction
    the site shows. Read-only list: decisions are made on the review page."""

    list_display = ("notice", "status", "flags", "reviewed")
    list_display_links = None
    list_per_page = 50
    search_fields = ("title", "reference_number")
    sortable_by = ()
    actions = None

    def has_module_permission(self, request):
        return can_review(request.user)

    def has_view_permission(self, request, obj=None):
        return can_review(request.user)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_queryset(self, request):
        return review_queue()

    def get_ordering(self, request):
        return ("review_priority", "closing_date", "id")

    @admin.display(description="notice")
    def notice(self, obj):
        if obj.review_extraction_id is None:
            return format_html("{} <span class=\"quiet\">(no LLM extraction)</span>", obj.title)
        return format_html('<a href="{}">{}</a>', review_current_url(obj), obj.title)

    @admin.display(description="status")
    def status(self, obj):
        if obj.review_extraction_status == Extraction.Status.FAILED:
            return "extraction failed"
        return dict(Assessment.Status.choices).get(obj.review_status, "not assessed")

    @admin.display(description="flags")
    def flags(self, obj):
        flags = json.loads(obj.review_flags) if obj.review_flags else []
        combined = json.loads(obj.review_combined) if obj.review_combined else {}
        return ", ".join(queue_flags(flags, obj.data_quality, combined.get("flags"))) or "-"

    @admin.display(description="reviewed")
    def reviewed(self, obj):
        """Gates with a decision in effect (own, or carried and still holding), gates whose
        earlier decision needs re-review, and a link to an earlier reviewed version."""
        total = len(review.REVIEW_GATES)
        carried = ({} if obj.review_extraction_id is None else {
            g: c for g, c in review.latest_carried(obj.review_extraction_id).items()})
        own = set(ReviewDecision.objects.filter(extraction_id=obj.review_extraction_id)
                  .values_list("gate", flat=True)) if obj.review_extraction_id else set()
        carried = {g: c for g, c in carried.items() if g not in own}
        n = len(own) + sum(c.applied for c in carried.values())
        text = "no" if n == 0 else "yes" if n >= total else f"partly ({n} of {total})"
        extra = []
        if k := sum(c.applied for c in carried.values()):
            extra.append(f"{k} carried")
        if m := sum(not c.applied for c in carried.values()):
            extra.append(f"{m} {review.NEEDS_REREVIEW}")
        if extra:
            text += f" ({'; '.join(extra)})"
        if obj.earlier_reviewed_id is None:
            return text
        url = reverse("admin:tenders_review_extraction", args=[obj.earlier_reviewed_id])
        return format_html('{}<br><a href="{}">reviewed on an earlier version</a>', text, url)

    def get_urls(self):
        view = self.admin_site.admin_view  # staff login (with the lockout) and CSRF
        return [path("extraction/<int:pk>/", view(self.review_view),
                     name="tenders_review_extraction"),
                path("extraction/<int:pk>/documents/", view(self.upload_view),
                     name="tenders_upload_documents"),
                path("extraction/<int:pk>/documents/<int:doc_pk>/delete/",
                     view(self.delete_document_view), name="tenders_delete_document"),
                path("tender/<str:reference>/review/", view(self.review_current_view),
                     name="tenders_review_current"),
                *super().get_urls()]

    # -- the review page ----------------------------------------------------------------

    def review_view(self, request, pk: int):
        if not can_review(request.user):
            raise PermissionDenied
        extraction = get_object_or_404(Extraction.objects.select_related("tender"), pk=pk)
        reviewable = extraction.status == Extraction.Status.OK and extraction.data is not None
        bound: dict[str, forms.Form] = {}
        if request.method == "POST":
            if not reviewable:
                return HttpResponseBadRequest("This extraction failed: nothing to review.")
            gate = request.POST.get("gate")
            if gate not in review.REVIEW_GATES:
                return HttpResponseBadRequest("Unknown gate.")
            if request.POST.get("form") == "correct":
                form = CorrectionForm(request.POST, gate=gate, extraction=extraction)
                if form.is_valid():
                    value, evidence = form.correction
                    return self._decide(request, extraction, gate, ReviewDecision.Action.CORRECT,
                                        form.cleaned_data["note"], value, evidence)
                bound[gate] = form
            else:
                form = _DecisionForm(request.POST)
                if not form.is_valid():
                    return HttpResponseBadRequest("Invalid decision.")
                return self._decide(request, extraction, gate, form.cleaned_data["action"],
                                    form.cleaned_data["note"])
        return TemplateResponse(request, "admin/tenders/review_extraction.html", {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": f"Review: {extraction.tender.title}",
            "extraction": extraction,
            "tender": extraction.tender,
            "reviewable": reviewable,
            "newest_id": (current_extractions(extraction.tender, extraction.model_id)
                          .values_list("id", flat=True).first()),
            "assessment": (extraction.assessments.filter(profile_id=_default_profile_id())
                           .first()),
            "gates": self._gate_context(extraction, bound) if reviewable else [],
            **self._documents_context(extraction.tender),
        })

    def review_current_view(self, request, reference: str):
        """The review page of the extraction the site shows for this notice now. Links that
        should follow the notice (the queue, after an upload or delete) point here; a
        #fragment on the link is kept by the browser through the redirect."""
        if not can_review(request.user):
            raise PermissionDenied
        reference = reference_or_404(reference)  # NUL-free, or 404
        tender = get_object_or_404(Tender, reference_number=reference)
        current = current_extraction(tender)
        if current is None:
            messages.warning(request, f"{reference} has no LLM extraction to review yet.")
            return HttpResponseRedirect(reverse("admin:tenders_needsreview_changelist"))
        return HttpResponseRedirect(reverse("admin:tenders_review_extraction",
                                            args=[current.pk]))

    # -- reviewer document upload (tenders.documents.upload) ------------------------------

    @staticmethod
    def _documents_context(tender: Tender) -> dict:
        docs = list(upload.uploaded(tender).select_related("uploaded_by").order_by("id"))
        return {
            "uploads": [{"doc": d, "reading": upload.reading(d)} for d in docs],
            "fetched_ok": tender.documents.filter(source=TenderDocument.Source.FETCHED,
                                                  status=TenderDocument.Status.OK).count(),
            "upload_limits": {"mb": upload.MAX_FILE_BYTES // (1024 * 1024),
                              "per_tender": upload.MAX_PER_TENDER,
                              "per_day": upload.MAX_PER_USER_PER_DAY},
        }

    def _documents_target(self, request, pk: int):
        """The extraction (and so the tender) of an upload or delete POST."""
        if not can_review(request.user):
            raise PermissionDenied
        return get_object_or_404(Extraction.objects.select_related("tender"), pk=pk)

    @staticmethod
    def _back(extraction: Extraction) -> HttpResponseRedirect:
        """To the notice's current extraction: an upload or delete is about the notice, and
        the extraction it was sent from may no longer be the one the site shows."""
        return HttpResponseRedirect(f"{review_current_url(extraction.tender)}#documents")

    @staticmethod
    def _reread(request, tender: Tender, done: str) -> None:
        """Ask the worker to re-read the tender, and say what happens next."""
        why_not = upload.start_extract_one(tender.reference_number)
        if why_not:
            messages.warning(request, f"{done} Not re-read yet: {why_not}. The daily run "
                                      "reads it otherwise.")
        else:
            # The browser stays on the extraction it was redirected to, so a reload would
            # not reach the one the re-read makes: link the notice's current one instead.
            messages.success(request, format_html(
                '{} The notice is being read again; <a href="{}#documents">open the current '
                'extraction</a> in a minute or two.', done, review_current_url(tender)))

    def upload_view(self, request, pk: int):
        extraction = self._documents_target(request, pk)
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        tender = extraction.tender
        files = request.FILES.getlist("files")
        try:
            upload.check_request(tender, request.user, len(files))
        except upload.Rejected as e:
            messages.error(request, str(e))
            return self._back(extraction)
        result = upload.handle_files(tender, request.user, files)
        for name, why in result.rejected:
            messages.error(request, f"Not uploaded: {name}: {why}.")
        if result.stored:
            names = ", ".join(d.original_filename for d in result.stored)
            self._reread(request, tender, f"Uploaded: {names}.")
        return self._back(extraction)

    def delete_document_view(self, request, pk: int, doc_pk: int):
        extraction = self._documents_target(request, pk)
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        doc = get_object_or_404(upload.uploaded(extraction.tender), pk=doc_pk)
        name = doc.original_filename
        with transaction.atomic():
            LogEntry.objects.log_actions(
                request.user.pk, [doc], DELETION, single_object=True,
                change_message=f"Deleted the upload {name} of "
                               f"{extraction.tender.reference_number}")
            doc.delete()
        self._reread(request, extraction.tender, f"Deleted: {name}.")
        return self._back(extraction)

    @staticmethod
    def _gate_context(extraction: Extraction, bound: dict) -> list[dict]:
        history = list(extraction.review_decisions.select_related("reviewer"))
        reviews = review.reviews_for(extraction)
        latest = review.latest_decisions(extraction)
        effective = review.apply_reviews(extraction.data, reviews.applied)
        labels = {**GATE_LABELS, **dict(ReviewDecision.GATE_CHOICES)}
        assessment = extraction.assessments.filter(profile_id=_default_profile_id()).first()
        combined, partner = combined_for(extraction, assessment.combined if assessment else None)
        v3_data = ((partner.data if partner else None) or {}).get("eligibility") or {}
        rows = []
        for gate in review.REVIEW_GATES:
            if gate == review.CRITERIA:
                model, now = extraction.data[gate], effective[gate]
                model_text, evidence, flags = model, None, []
            else:
                model = extraction.data["eligibility"][gate]
                now = effective["eligibility"][gate]
                model_text, evidence = gate_value(gate, model), model.get("evidence")
                flags = gate_flags(gate, extraction.flags, bool(extraction.documents))
                v3_said = gate_value(gate, v3_data.get(gate))
                if combined.versions.get(gate) == V3:
                    flags.append(f"the site shows the v3 answer (notice only): {v3_said}")
                if f"{VERSIONS_DISAGREE}:eligibility.{gate}" in combined.flags:
                    flags.append(f"v3 (notice only) said {v3_said}: versions disagree")
            decision = latest.get(gate)
            in_effect = reviews.applied.get(gate)
            rows.append({
                "gate": gate, "label": labels[gate], "model_value": model_text,
                "evidence": evidence, "flags": flags, "decision": decision,
                "carried": reviews.carried.get(gate),
                "effective": (now if gate == review.CRITERIA else gate_value(gate, now))
                if in_effect and in_effect.action != ReviewDecision.Action.ACCEPT else "",
                "effective_evidence": None if gate == review.CRITERIA else now.get("evidence"),
                "history": [d for d in history if d.gate == gate],
                "correction_form": bound.get(gate) or CorrectionForm(
                    gate=gate, extraction=extraction, initial_value=now),
            })
        return rows

    def _decide(self, request, extraction, gate, action, note, value=None, evidence=""):
        """Record one decision, then recompute this extraction's assessments (no API call)."""
        from .eligibility.service import assess, default_profile

        with transaction.atomic():
            ReviewDecision.objects.create(
                extraction=extraction, gate=gate, action=action, corrected_value=value,
                evidence=evidence, reviewer=request.user, note=note)
            extraction = Extraction.objects.select_related("tender").get(pk=extraction.pk)
            profiles = {default_profile(), *SupplierProfile.objects.filter(
                assessments__extraction=extraction)}
            statuses = [assess(extraction, p).get_status_display() for p in profiles]
            # A decision on another extraction may carry to the current one the site shows.
            newest = (current_extractions(extraction.tender_id, extraction.model_id)
                      .select_related("tender").first())
            if newest.pk != extraction.pk:
                for p in {default_profile(), *SupplierProfile.objects.filter(
                        assessments__extraction=newest)}:
                    assess(newest, p)
        messages.success(request, f"{ReviewDecision.Action(action).label}: "
                                  f"{dict(ReviewDecision.GATE_CHOICES)[gate]}. "
                                  f"Status now: {', '.join(sorted(set(statuses)))}.")
        url = reverse("admin:tenders_review_extraction", args=[extraction.pk])
        return HttpResponseRedirect(f"{url}#gate-{gate}")


def review_current_url(tender) -> str:
    """The link that always opens the notice's current extraction (review_current_view)."""
    return reverse("admin:tenders_review_current", args=[tender.reference_number])


def _default_profile_id():
    return SupplierProfile.objects.order_by("id").values("id")[:1]


@admin.register(ReviewDecision)
class ReviewDecisionAdmin(ReadOnlyAdmin):
    """The append-only log of every decision. Decisions are made on the review page."""

    list_display = ("created_at", "extraction", "gate", "action", "reviewer", "note")
    list_filter = ("action", "gate")
    search_fields = ("extraction__tender__reference_number",)
    list_select_related = ("extraction", "reviewer")
