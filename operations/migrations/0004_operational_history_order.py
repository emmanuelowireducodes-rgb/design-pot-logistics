from importlib import import_module

from django.db import migrations, models

history = import_module("operations.migrations.0002_immutable_history")


def sequence_legacy_history(apps, schema_editor):
    events = apps.get_model("operations", "StatusEvent")
    current_delivery = None
    position = 0
    batch = []
    for event in events.objects.order_by("delivery_id", "created_at", "id").iterator(chunk_size=500):
        if current_delivery != event.delivery_id:
            current_delivery, position = event.delivery_id, 0
        position += 1
        event.sequence = position
        batch.append(event)
        if len(batch) == 500:
            events.objects.bulk_update(batch, ["sequence"])
            batch = []
    if batch:
        events.objects.bulk_update(batch, ["sequence"])


class Migration(migrations.Migration):
    dependencies = [("operations", "0003_phase2_workflow")]
    operations = [
        migrations.RunPython(history.uninstall, history.install),
        migrations.AddField(model_name="availability", name="release_reason", field=models.CharField(blank=True, max_length=16)),
        migrations.AddField(model_name="statusevent", name="sequence", field=models.PositiveIntegerField(default=0)),
        migrations.RunPython(sequence_legacy_history, migrations.RunPython.noop),
        migrations.AddIndex(model_name="statusevent", index=models.Index(fields=["delivery", "-sequence"], name="status_delivery_sequence_idx")),
        migrations.AddConstraint(model_name="statusevent", constraint=models.UniqueConstraint(fields=["delivery", "sequence"], condition=models.Q(sequence__gt=0), name="unique_positive_status_sequence")),
        migrations.RunPython(history.install, history.uninstall),
    ]
