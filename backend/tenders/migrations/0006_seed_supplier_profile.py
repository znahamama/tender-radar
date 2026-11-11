from django.db import migrations

NAME = "Harbourline Software (fictional)"
PROFILE = {
    "capabilities": ["custom software development", "web and mobile apps", "data engineering",
                     "IT consulting", "cloud migration"],
    "supply_arrangements": [],
    "corporate_clearance": "none",
    "personnel_clearance": "none",
    "work_locations": ["St. John's, NL", "remote"],
}


def seed(apps, schema_editor):
    SupplierProfile = apps.get_model("tenders", "SupplierProfile")
    SupplierProfile.objects.update_or_create(name=NAME, defaults=PROFILE)


def unseed(apps, schema_editor):
    apps.get_model("tenders", "SupplierProfile").objects.filter(name=NAME).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("tenders", "0005_supplierprofile_assessment"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
