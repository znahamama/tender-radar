from django.contrib import admin

from .models import Tender


@admin.register(Tender)
class TenderAdmin(admin.ModelAdmin):
    """Read-only: tenders are written by the ingest command only."""

    list_display = ("title", "tender_status", "region_scope", "is_it", "notice_kind", "closing_date")
    list_filter = ("tender_status", "region_scope", "is_it", "notice_kind", "closing_date")
    search_fields = ("title", "reference_number")
    date_hierarchy = "closing_date"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
