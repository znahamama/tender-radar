from django.apps import AppConfig


class TendersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "tenders"

    def ready(self):
        from . import checks  # noqa: F401  (registers system checks)
