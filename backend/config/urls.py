from django.contrib import admin
from django.urls import path
from tenders import views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/health", views.health, name="health"),
    path("api/health/db", views.health_db, name="health-db"),
]
