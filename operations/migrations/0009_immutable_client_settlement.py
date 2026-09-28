from django.db import migrations


def install(apps, schema_editor):
    if schema_editor.connection.vendor == "sqlite":
        for action in ("UPDATE", "DELETE"):
            schema_editor.execute(
                f"CREATE TRIGGER immutable_clientsettlement_{action.lower()} "
                f"BEFORE {action} ON operations_clientsettlement "
                "BEGIN SELECT RAISE(ABORT, 'Historical records are immutable'); END;"
            )
    elif schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(
            "CREATE TRIGGER immutable_clientsettlement BEFORE UPDATE OR DELETE "
            "ON operations_clientsettlement FOR EACH ROW "
            "EXECUTE FUNCTION dpot_reject_history_mutation();"
        )


def uninstall(apps, schema_editor):
    if schema_editor.connection.vendor == "sqlite":
        for action in ("update", "delete"):
            schema_editor.execute(f"DROP TRIGGER IF EXISTS immutable_clientsettlement_{action};")
    elif schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(
            "DROP TRIGGER IF EXISTS immutable_clientsettlement ON operations_clientsettlement;"
        )


class Migration(migrations.Migration):
    dependencies = [("operations", "0008_phase4_client_settlements")]
    operations = [migrations.RunPython(install, uninstall)]
