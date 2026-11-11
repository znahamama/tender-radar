from django.contrib import admin

from .models import Extraction, LlmCall, Tender


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
