from django.contrib import admin
from django.urls import path
from tenders import pages, views

urlpatterns = [
    path("", pages.tender_list, name="tender-list"),
    path("tenders/<str:reference>/", pages.tender_detail, name="tender-detail"),
    path("admin/", admin.site.urls),
    path("api/health", views.health, name="health"),
    path("api/health/db", views.health_db, name="health-db"),
]
