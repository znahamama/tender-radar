"""Record which redact() version cleaned each stored document's text.

Every row stored before this migration was cleaned by version 1, so they get 1; the field
has no default afterwards (the fetcher always sets it). The fetcher re-cleans ok rows older
than tenders.ingest.redact.REDACT_VERSION by downloading and parsing them again.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tenders", "0013_attachment_urls_tender_documents"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenderdocument",
            name="redact_version",
            field=models.PositiveSmallIntegerField(default=1),
            preserve_default=False,
        ),
    ]
