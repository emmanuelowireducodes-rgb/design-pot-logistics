from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class ClientMapsMigrationTests(TransactionTestCase):
    """Exercise the 0005 -> 0006 history-trigger transition on an isolated DB."""

    reset_sequences = True

    def test_0005_to_0006_restores_all_immutable_history_triggers(self):
        executor = MigrationExecutor(connection)
        latest_targets = executor.loader.graph.leaf_nodes()
        executor.migrate([("operations", "0005_phase25_foundations")])

        try:
            executor = MigrationExecutor(connection)
            executor.migrate([("operations", "0006_client_maps_links")])
            self.assert_immutable_history_triggers()
        finally:
            MigrationExecutor(connection).migrate(latest_targets)

    def assert_immutable_history_triggers(self):
        tables = (
            "location",
            "pricingversion",
            "quote",
            "quoteconfirmation",
            "assignment",
            "statusevent",
            "proofofdelivery",
            "ledgerentry",
            "domainevent",
            "fixedrate",
        )
        with connection.cursor() as cursor:
            if connection.vendor == "postgresql":
                cursor.execute(
                    """
                    SELECT trigger.tgname, relation.relname
                    FROM pg_trigger AS trigger
                    JOIN pg_class AS relation ON relation.oid = trigger.tgrelid
                    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                    WHERE NOT trigger.tgisinternal
                      AND namespace.nspname = current_schema()
                      AND trigger.tgname LIKE 'immutable_%'
                    """
                )
                found = {(name, table.removeprefix("operations_")) for name, table in cursor}
                for table in tables:
                    self.assertIn((f"immutable_{table}", table), found)
                cursor.execute(
                    "SELECT 1 FROM pg_proc WHERE proname = %s",
                    ["dpot_reject_history_mutation"],
                )
                self.assertIsNotNone(cursor.fetchone())
            else:
                cursor.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'immutable_%'"
                )
                found = {name for (name,) in cursor}
                for table in tables:
                    for action in ("update", "delete"):
                        self.assertIn(f"immutable_{table}_{action}", found)
