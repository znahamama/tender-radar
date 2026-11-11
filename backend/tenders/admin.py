from django.contrib import admin

from .models import Assessment, Extraction, LlmCall, SupplierProfile, Tender


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
                    "computed_at")
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


@admin.register(SupplierProfile)
class SupplierProfileAdmin(admin.ModelAdmin):
    """Editable. Saving re-assesses the latest extractions for this profile (no API calls)."""

    list_display = ("name", "corporate_clearance", "personnel_clearance", "updated_at")

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        from .eligibility.service import reassess

        reassess(profile=obj)
