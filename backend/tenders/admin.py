import json

from django import forms
from django.conf import settings
from django.contrib import admin, messages
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
from django.http import HttpResponseBadRequest, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html

from . import review
from .ingest.injection import INJECTION_PATTERN
from .models import (
    Assessment,
    Extraction,
    LlmCall,
    NeedsReview,
    ReviewDecision,
    SupplierProfile,
    Tender,
)
from .pages import GATE_LABELS, gate_flags, gate_value, is_open, primary_assessments


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


def queue_flags(flags: list[str], data_quality: list[str]) -> list[str]:
    out = []
    if any(f.startswith(("ungrounded:eligibility.", "ungrounded_detail:eligibility."))
           for f in flags):
        out.append("ungrounded")
    out += [f for f in ("sa_metadata_disagrees", "injection_suspected", "truncated")
            if f in flags]
    if INJECTION_PATTERN in (data_quality or []):
        out.append(INJECTION_PATTERN)
    return out


def review_queue():
    """Open notices, newest LLM extraction each (the one the site shows): blocked first,
    then flagged, then the rest; by closing date within each."""
    newest = (Extraction.objects.filter(tender=OuterRef("pk"), model_id=settings.LLM_MODEL)
              .order_by("-created_at", "-id"))
    status = primary_assessments().filter(tender=OuterRef("pk")).values("status")[:1]
    gates_reviewed = (ReviewDecision.objects.filter(extraction=OuterRef("review_extraction_id"))
                      .values("extraction").annotate(n=Count("gate", distinct=True))
                      .values("n")[:1])
    # jsonb as text ('["ungrounded:eligibility.clearance", ...]'): one LIKE per flag.
    flag_text = Subquery(newest.annotate(t=Cast("flags", TextField())).values("t")[:1],
                         output_field=TextField())
    qs = NeedsReview.objects.filter(is_open()).annotate(
        review_extraction_id=Subquery(newest.values("id")[:1]),
        review_extraction_status=Subquery(newest.values("status")[:1]),
        review_flags=flag_text,
        review_status=Subquery(status),
    ).annotate(
        gates_reviewed=Coalesce(Subquery(gates_reviewed, output_field=IntegerField()), 0),
        # The newest earlier extraction of this notice that a person reviewed.
        earlier_reviewed_id=Subquery(
            Extraction.objects.filter(tender=OuterRef("pk"),
                                      model_id=settings.LLM_MODEL,
                                      review_decisions__isnull=False)
            .exclude(pk=OuterRef("review_extraction_id"))
            .order_by("-created_at", "-id").values("id")[:1]),
    )
    flagged = Q(data_quality__contains=[INJECTION_PATTERN])
    for f in QUEUE_FLAGS:
        flagged |= Q(review_flags__contains=f'"{f}')
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
    checks as model output (tenders.review.validate_correction)."""

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
                help_text="An exact quote from the notice, as for the model.")
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
        url = reverse("admin:tenders_review_extraction", args=[obj.review_extraction_id])
        return format_html('<a href="{}">{}</a>', url, obj.title)

    @admin.display(description="status")
    def status(self, obj):
        if obj.review_extraction_status == Extraction.Status.FAILED:
            return "extraction failed"
        return dict(Assessment.Status.choices).get(obj.review_status, "not assessed")

    @admin.display(description="flags")
    def flags(self, obj):
        flags = json.loads(obj.review_flags) if obj.review_flags else []
        return ", ".join(queue_flags(flags, obj.data_quality)) or "-"

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
        return [path("extraction/<int:pk>/", self.admin_site.admin_view(self.review_view),
                     name="tenders_review_extraction"), *super().get_urls()]

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
            "newest_id": (Extraction.objects.filter(tender=extraction.tender,
                                                    model_id=extraction.model_id)
                          .order_by("-created_at", "-id").values_list("id", flat=True).first()),
            "assessment": (extraction.assessments.filter(profile_id=_default_profile_id())
                           .first()),
            "gates": self._gate_context(extraction, bound) if reviewable else [],
        })

    @staticmethod
    def _gate_context(extraction: Extraction, bound: dict) -> list[dict]:
        history = list(extraction.review_decisions.select_related("reviewer"))
        reviews = review.reviews_for(extraction)
        latest = review.latest_decisions(extraction)
        effective = review.apply_reviews(extraction.data, reviews.applied)
        labels = {**GATE_LABELS, **dict(ReviewDecision.GATE_CHOICES)}
        rows = []
        for gate in review.REVIEW_GATES:
            if gate == review.CRITERIA:
                model, now = extraction.data[gate], effective[gate]
                model_text, evidence, flags = model, None, []
            else:
                model = extraction.data["eligibility"][gate]
                now = effective["eligibility"][gate]
                model_text, evidence = gate_value(gate, model), model.get("evidence")
                flags = gate_flags(gate, extraction.flags)
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
            # A decision on an older extraction may carry to the newest one the site shows.
            newest = (Extraction.objects.select_related("tender")
                      .filter(tender_id=extraction.tender_id, model_id=extraction.model_id)
                      .order_by("-created_at", "-id").first())
            if newest.pk != extraction.pk:
                for p in {default_profile(), *SupplierProfile.objects.filter(
                        assessments__extraction=newest)}:
                    assess(newest, p)
        messages.success(request, f"{ReviewDecision.Action(action).label}: "
                                  f"{dict(ReviewDecision.GATE_CHOICES)[gate]}. "
                                  f"Status now: {', '.join(sorted(set(statuses)))}.")
        url = reverse("admin:tenders_review_extraction", args=[extraction.pk])
        return HttpResponseRedirect(f"{url}#gate-{gate}")


def _default_profile_id():
    return SupplierProfile.objects.order_by("id").values("id")[:1]


@admin.register(ReviewDecision)
class ReviewDecisionAdmin(ReadOnlyAdmin):
    """The append-only log of every decision. Decisions are made on the review page."""

    list_display = ("created_at", "extraction", "gate", "action", "reviewer", "note")
    list_filter = ("action", "gate")
    search_fields = ("extraction__tender__reference_number",)
    list_select_related = ("extraction", "reviewer")
