from django.db import migrations

TABLES = [
    "location", "pricingversion", "quote", "quoteconfirmation", "assignment",
    "statusevent", "proofofdelivery", "ledgerentry", "domainevent",
]


def install(apps, schema_editor):
    if schema_editor.connection.vendor == "sqlite":
        for table in TABLES:
            for action in ("UPDATE", "DELETE"):
                schema_editor.execute(
                    f"CREATE TRIGGER immutable_{table}_{action.lower()} BEFORE {action} ON operations_{table} "
                    "BEGIN SELECT RAISE(ABORT, 'Historical records are immutable'); END;"
                )
    elif schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("""
            CREATE FUNCTION dpot_reject_history_mutation() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION 'Historical records are immutable'; END;
            $$ LANGUAGE plpgsql;
        """)
        for table in TABLES:
            schema_editor.execute(
                f"CREATE TRIGGER immutable_{table} BEFORE UPDATE OR DELETE ON operations_{table} "
                "FOR EACH ROW EXECUTE FUNCTION dpot_reject_history_mutation();"
            )


def uninstall(apps, schema_editor):
    for table in TABLES:
        if schema_editor.connection.vendor == "sqlite":
            for action in ("update", "delete"):
                schema_editor.execute(f"DROP TRIGGER IF EXISTS immutable_{table}_{action};")
        elif schema_editor.connection.vendor == "postgresql":
            schema_editor.execute(f"DROP TRIGGER IF EXISTS immutable_{table} ON operations_{table};")
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute("DROP FUNCTION IF EXISTS dpot_reject_history_mutation();")


class Migration(migrations.Migration):
    dependencies = [("operations", "0001_initial")]
    operations = [migrations.RunPython(install, uninstall)]
