import django.utils.timezone
from django.db import migrations, models
from django.db.models import F


def backfill(apps, schema_editor):
    """Until now the current extraction was the newest created one: keep that order."""
    Extraction = apps.get_model("tenders", "Extraction")
    Extraction.objects.update(last_used_at=F("created_at"))


class Migration(migrations.Migration):
    dependencies = [
        ("tenders", "0019_tenderdocument_upload"),
    ]

    operations = [
        migrations.AddField(
            model_name="extraction",
            name="last_used_at",
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
