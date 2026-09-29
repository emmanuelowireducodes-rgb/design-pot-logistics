from django.db import migrations


def ensure_immutable_fixed_rates(apps, schema_editor):
    connection = schema_editor.connection
    if connection.vendor == "sqlite":
        for action in ("update", "delete"):
            schema_editor.execute(
                f"DROP TRIGGER IF EXISTS immutable_fixedrate_{action};"
            )
            schema_editor.execute(
                f"CREATE TRIGGER immutable_fixedrate_{action} "
                f"BEFORE {action.upper()} ON operations_fixedrate "
                "BEGIN SELECT RAISE(ABORT, 'Historical records are immutable'); END;"
            )
    elif connection.vendor == "postgresql":
        schema_editor.execute(
            "DROP TRIGGER IF EXISTS immutable_fixedrate ON operations_fixedrate;"
        )
        schema_editor.execute(
            "CREATE TRIGGER immutable_fixedrate BEFORE UPDATE OR DELETE "
            "ON operations_fixedrate FOR EACH ROW "
            "EXECUTE FUNCTION dpot_reject_history_mutation();"
        )


class Migration(migrations.Migration):
    dependencies = [("operations", "0012_bulk_booking_group")]
    operations = [migrations.RunPython(ensure_immutable_fixed_rates, migrations.RunPython.noop)]
