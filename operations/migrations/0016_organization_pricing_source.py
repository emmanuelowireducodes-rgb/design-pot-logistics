from django.db import migrations, models


def preserve_existing_client_cards(apps, schema_editor):
    Organization = apps.get_model("operations", "Organization")
    PriceCard = apps.get_model("operations", "PriceCard")
    database = schema_editor.connection.alias
    existing_card_orgs = PriceCard.objects.using(database).filter(
        organization__isnull=False
    ).values_list("organization_id", flat=True)
    Organization.objects.using(database).filter(pk__in=existing_card_orgs).update(
        use_custom_pricing=True
    )


class Migration(migrations.Migration):
    dependencies = [("operations", "0015_area_directory")]

    operations = [
        migrations.AddField(
            model_name="organization",
            name="use_custom_pricing",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(preserve_existing_client_cards, migrations.RunPython.noop),
    ]
