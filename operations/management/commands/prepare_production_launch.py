"""Report or, with explicit safeguards, remove transactional pre-launch data."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from contextlib import contextmanager
from decimal import Decimal
from typing import Iterator

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connections, transaction
from django.db.models import Count, Model, Q

from operations.models import (
    Assignment,
    Availability,
    BulkBooking,
    ClientSettlement,
    Delivery,
    DeliveryRun,
    DomainEvent,
    FixedRate,
    Job,
    LedgerEntry,
    Location,
    LocationUpdate,
    Notification,
    OperationalZone,
    Organization,
    PriceCard,
    PricingConfiguration,
    PricingVersion,
    ProofOfDelivery,
    PushOutbox,
    PushSubscription,
    Quote,
    QuoteConfirmation,
    Resource,
    RoutePlan,
    RouteStop,
    RunStop,
    StatusEvent,
    User,
)

SCOPE_CONFIRMATION = "DELETE-ALL-TRANSACTIONAL-DATA"

# Audit event names are deliberately scoped by transaction family. Events such
# as USER_*, session.*, pricing.*, zone.updated, and resource duty changes stay.
_EVENT_PREFIXES = ("delivery.", "quote.", "cod.", "bulk.", "run.", "sample.")
_EVENT_EXACT = ("location.zone_corrected",)

_IMMUTABLE_MODELS = (
    Location,
    PricingVersion,
    Quote,
    QuoteConfirmation,
    Assignment,
    StatusEvent,
    ProofOfDelivery,
    LedgerEntry,
    DomainEvent,
    FixedRate,
    ClientSettlement,
)
_DELETE_IMMUTABLE_MODELS = (
    Quote,
    QuoteConfirmation,
    Assignment,
    StatusEvent,
    ProofOfDelivery,
    LedgerEntry,
    DomainEvent,
    ClientSettlement,
)
# SQLite rebuilds tables when certain existing migrations alter these models,
# which drops their triggers. PostgreSQL retains them. These two local-test
# gaps are documented and never tolerated on PostgreSQL.
_SQLITE_REBUILD_TRIGGER_GAPS = {"operations_proofofdelivery", "operations_ledgerentry"}


def _booking_availability():
    """Only assignment- and run-linked booking blocks are in cleanup scope."""
    return Availability.objects.filter(kind="booking").filter(
        Q(delivery__isnull=False) | Q(note__startswith="run:")
    )


def _transaction_event_filter():
    event_filter = Q()
    for prefix in _EVENT_PREFIXES:
        event_filter |= Q(kind__startswith=prefix)
    return event_filter | Q(kind__in=_EVENT_EXACT)


def _transaction_events():
    event_filter = _transaction_event_filter()
    block_ids = [str(pk) for pk in _booking_availability().values_list("pk", flat=True)]
    events = DomainEvent.objects.filter(event_filter)
    if block_ids:
        events = events | DomainEvent.objects.filter(
            kind="availability.created", payload__block_id__in=block_ids
        )
    return events.distinct()


def _unclassified_linked_events():
    return DomainEvent.objects.filter(delivery__isnull=False).exclude(_transaction_event_filter())


def _trigger_name(model) -> str:
    table = model._meta.db_table
    prefix = "operations_"
    if not table.startswith(prefix):
        raise CommandError(f"Unexpected immutable model table: {table}")
    return f"immutable_{table[len(prefix) :]}"


def _sqlite_triggers(model) -> dict[str, str]:
    connection = connections["default"]
    trigger_prefix = _trigger_name(model)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = %s",
            [model._meta.db_table],
        )
        rows = cursor.fetchall()
    expected = {f"{trigger_prefix}_update", f"{trigger_prefix}_delete"}
    triggers = {name: sql for name, sql in rows if name in expected and sql}
    if set(triggers) != expected:
        if model._meta.db_table in _SQLITE_REBUILD_TRIGGER_GAPS and not triggers:
            return {}
        found_names = ", ".join(sorted(name for name, _sql in rows)) or "none"
        raise CommandError(
            f"Immutable SQLite triggers are missing for {model.__name__}; found: {found_names}. Cleanup is blocked."
        )
    return triggers


def _postgres_trigger(model) -> tuple[str, str]:
    connection = connections["default"]
    name = _trigger_name(model)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tgname, tgenabled FROM pg_trigger "
            "WHERE tgrelid = to_regclass(%s) AND tgname = %s AND NOT tgisinternal",
            [model._meta.db_table, name],
        )
        row = cursor.fetchone()
    if row is None or row[1] not in ("O", "A"):
        raise CommandError(
            f"The PostgreSQL immutable trigger for {model.__name__} is missing or disabled; cleanup is blocked."
        )
    return row[0], row[1]


def _verify_immutable_guards() -> None:
    connection = connections["default"]
    if connection.vendor == "sqlite":
        for model in _IMMUTABLE_MODELS:
            _sqlite_triggers(model)
        return
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regprocedure('dpot_reject_history_mutation()')")
            if cursor.fetchone()[0] is None:
                raise CommandError(
                    "The PostgreSQL history-protection function is missing; cleanup is blocked."
                )
        for model in _IMMUTABLE_MODELS:
            _postgres_trigger(model)
        return
    raise CommandError("Launch cleanup supports only SQLite local tests or PostgreSQL.")


@contextmanager
def _temporarily_suspend_target_guards() -> Iterator[None]:
    """Suspend only targeted history-table guards inside the cleanup transaction.

    DDL is transactional on supported backends. The finally block restores the
    same named triggers before commit; an error rolls back both data and DDL.
    """
    connection = connections["default"]
    suspended: list[tuple[type[Model], tuple[str, str]]] = []
    try:
        if connection.vendor == "sqlite":
            for target_model in _DELETE_IMMUTABLE_MODELS:
                for name, sql in _sqlite_triggers(target_model).items():
                    with connection.cursor() as cursor:
                        cursor.execute(f"DROP TRIGGER {connection.ops.quote_name(name)}")
                    suspended.append((target_model, (name, sql)))
            yield
        elif connection.vendor == "postgresql":
            for target_model in _DELETE_IMMUTABLE_MODELS:
                name, state = _postgres_trigger(target_model)
                table = connection.ops.quote_name(target_model._meta.db_table)
                quoted_trigger = connection.ops.quote_name(name)
                with connection.cursor() as cursor:
                    cursor.execute(f"ALTER TABLE {table} DISABLE TRIGGER {quoted_trigger}")
                suspended.append((target_model, (name, state)))
            yield
        else:
            raise CommandError("Launch cleanup supports only SQLite local tests or PostgreSQL.")
    finally:
        for suspended_model, descriptor in reversed(suspended):
            if connection.vendor == "sqlite":
                _name, sql = descriptor
                with connection.cursor() as cursor:
                    cursor.execute(sql)
            else:
                name, state = descriptor
                table = connection.ops.quote_name(suspended_model._meta.db_table)
                quoted_trigger = connection.ops.quote_name(name)
                command = "ENABLE ALWAYS" if state == "A" else "ENABLE"
                with connection.cursor() as cursor:
                    cursor.execute(f"ALTER TABLE {table} {command} TRIGGER {quoted_trigger}")


def _financial_integrity_issues() -> int:
    """Count broken collection/reconciliation/settlement chains without values."""
    entries = list(
        LedgerEntry.objects.select_related(
            "delivery__job",
            "settlement",
            "reconciled_entry__delivery__job",
        )
    )
    by_delivery: dict[object, list[LedgerEntry]] = defaultdict(list)
    settled_by_settlement: dict[object, list[LedgerEntry]] = defaultdict(list)
    issues = 0
    for entry in entries:
        by_delivery[entry.delivery_id].append(entry)
        if entry.kind == "cod_settled":
            if entry.settlement_id is not None:
                settled_by_settlement[entry.settlement_id].append(entry)
            reconciliation = entry.reconciled_entry
            settlement = entry.settlement
            if (
                entry.settlement_id is None
                or reconciliation is None
                or settlement is None
                or reconciliation.kind != "cod_reconciled"
                or reconciliation.delivery_id != entry.delivery_id
                or reconciliation.amount != entry.amount
                or reconciliation.currency != entry.currency
                or settlement.organization_id != entry.delivery.job.organization_id
                or settlement.currency != entry.currency
            ):
                issues += 1

    for delivery_id, delivery_entries in by_delivery.items():
        collections = [row for row in delivery_entries if row.kind == "cod_collected"]
        reconciliations = [row for row in delivery_entries if row.kind == "cod_reconciled"]
        if len(collections) > 1:
            issues += 1
        if collections:
            collection = collections[0]
            if (
                collection.delivery.payment_method != "cod"
                or collection.amount != collection.delivery.cod_amount
                or collection.currency != collection.delivery.currency
            ):
                issues += 1
        for reconciliation in reconciliations:
            if len(collections) != 1 or reconciliation.currency != collections[0].currency:
                issues += 1
        if (
            collections
            and sum((row.amount for row in reconciliations), Decimal("0")) > collections[0].amount
        ):
            issues += 1

    for settlement in ClientSettlement.objects.all():
        rows = settled_by_settlement.get(settlement.pk, [])
        if (
            not rows
            or sum((row.amount for row in rows), Decimal("0")) != settlement.amount
            or any(row.currency != settlement.currency for row in rows)
        ):
            issues += 1
    if set(settled_by_settlement) - set(ClientSettlement.objects.values_list("pk", flat=True)):
        issues += 1
    return issues


class Command(BaseCommand):
    help = "Safely report or explicitly remove transactional pre-launch data."

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--dry-run", action="store_true", help="Report scope only (the default).")
        mode.add_argument("--execute", action="store_true", help="Delete scoped transactions.")
        parser.add_argument(
            "--backup-confirmed",
            action="store_true",
            help="A current production backup and recovery point were confirmed manually.",
        )
        parser.add_argument(
            "--confirm-scope",
            default="",
            help=f"Required execute acknowledgement: {SCOPE_CONFIRMATION}",
        )
        parser.add_argument(
            "--confirm-plan",
            default="",
            help="Required execution fingerprint copied from a previously reviewed dry run.",
        )
        parser.add_argument(
            "--confirm-production",
            action="store_true",
            help="Additional acknowledgement required for production execution.",
        )

    def handle(self, *args, **options):
        execute = bool(options["execute"])
        if execute:
            if not options["backup_confirmed"]:
                raise CommandError(
                    "Execution requires --backup-confirmed after manual backup verification."
                )
            if options["confirm_scope"] != SCOPE_CONFIRMATION:
                raise CommandError(f"Execution requires --confirm-scope={SCOPE_CONFIRMATION}.")
            if not options["confirm_plan"]:
                raise CommandError(
                    "Execution requires --confirm-plan copied from a reviewed dry run."
                )
            if settings.DEMO_MODE:
                raise CommandError("Execution is blocked in demo mode.")
            if settings.PRODUCTION and not options["confirm_production"]:
                raise CommandError("Production execution also requires --confirm-production.")
            if connections["default"].vendor == "postgresql" and not options["confirm_production"]:
                raise CommandError(
                    "PostgreSQL execution also requires --confirm-production, including local commands using production PostgreSQL."
                )
        elif options["confirm_production"]:
            raise CommandError("--confirm-production is valid only together with --execute.")

        _verify_immutable_guards()
        plan = self._build_plan()
        self._write_report(plan)

        if plan["unclassified_events"] or plan["unclassified_availability"]:
            self.stdout.write(
                self.style.WARNING(
                    "Execution blocked: unclassified delivery-linked audit or availability rows need review."
                )
            )
            for kind, count in sorted(plan["unclassified_event_kinds"].items()):
                self.stdout.write(f"  {kind}: {count}")
            if execute:
                raise CommandError(
                    "Review unclassified delivery-linked audit or availability rows before cleanup."
                )
        if plan["financial_integrity_issues"]:
            self.stdout.write(
                self.style.WARNING(
                    f"Execution blocked: {plan['financial_integrity_issues']} COD graph integrity issue(s) need review."
                )
            )
            if execute:
                raise CommandError(
                    "Review the COD collection/reconciliation/settlement graph before cleanup."
                )
        if execute and User.objects.filter(role=User.Role.ADMIN, is_active=True).count() < 1:
            raise CommandError(
                "Execution is blocked because there is no active Admin account to preserve."
            )

        if not execute:
            self.stdout.write(self.style.SUCCESS("DRY RUN ONLY — no records were changed."))
            return

        # Re-plan only after locking on PostgreSQL so a live request cannot add
        # transactions between the displayed scope and the deletion pass.
        with transaction.atomic():
            self._lock_cleanup_tables()
            _verify_immutable_guards()
            locked_plan = self._build_plan()
            if (
                locked_plan["unclassified_events"]
                or locked_plan["unclassified_availability"]
                or locked_plan["financial_integrity_issues"]
            ):
                raise CommandError(
                    "Cleanup scope changed or failed integrity checks while acquiring locks."
                )
            if (
                locked_plan["fingerprint"] != plan["fingerprint"]
                or locked_plan["preserved"] != plan["preserved"]
            ):
                raise CommandError("Cleanup scope changed after the report; rerun the dry run.")
            if options["confirm_plan"] != locked_plan["fingerprint"]:
                raise CommandError(
                    "The reviewed dry-run fingerprint does not match the locked cleanup scope."
                )
            if User.objects.filter(role=User.Role.ADMIN, is_active=True).count() < 1:
                raise CommandError(
                    "Execution is blocked because there is no active Admin account to preserve."
                )
            preserved_before = self._preserved_snapshot()
            with _temporarily_suspend_target_guards():
                # The savepoint makes the transaction usable again if a
                # protected relation or database constraint rejects a delete;
                # guards can then be restored before the outer rollback.
                with transaction.atomic():
                    self._delete_transactional_data()
                    self._assert_cleanup_complete()
            _verify_immutable_guards()
            if self._preserved_snapshot() != preserved_before:
                raise CommandError(
                    "A protected account/configuration count changed; transaction rolled back."
                )
        self.stdout.write(
            self.style.SUCCESS("Transactional operational data cleanup completed atomically.")
        )

    def _build_plan(self) -> dict:
        events = _transaction_events()
        notification_qs = Notification.objects.filter(event__in=events.values("pk"))
        booking_qs = _booking_availability()
        unknown = _unclassified_linked_events()
        unknown_kinds = {
            item["kind"]: item["count"]
            for item in unknown.values("kind").order_by("kind").annotate(count=Count("pk"))
        }
        unclassified_availability = (
            Availability.objects.filter(delivery__isnull=False)
            .exclude(pk__in=booking_qs.values("pk"))
            .count()
        )
        querysets = {
            "Deliveries": Delivery.objects.all(),
            "Jobs": Job.objects.all(),
            "Bulk bookings": BulkBooking.objects.all(),
            "Quotes": Quote.objects.all(),
            "Quote confirmations": QuoteConfirmation.objects.all(),
            "Assignments": Assignment.objects.all(),
            "Delivery status history": StatusEvent.objects.all(),
            "Proofs of delivery": ProofOfDelivery.objects.all(),
            "Delivery runs": DeliveryRun.objects.all(),
            "Run stops": RunStop.objects.all(),
            "Assignment/run booking blocks": booking_qs,
            "COD collected entries": LedgerEntry.objects.filter(kind="cod_collected"),
            "COD reconciled entries": LedgerEntry.objects.filter(kind="cod_reconciled"),
            "COD settled entries": LedgerEntry.objects.filter(kind="cod_settled"),
            "Delivery fee ledger entries": LedgerEntry.objects.filter(kind="delivery_fee"),
            "Client settlements": ClientSettlement.objects.all(),
            "Operational notifications": notification_qs,
            "Related push outbox rows": PushOutbox.objects.filter(
                notification__event__in=events.values("pk")
            ),
            "Transactional DomainEvents": events,
            "Location updates": LocationUpdate.objects.all(),
            "Route plans": RoutePlan.objects.all(),
            "Route stops": RouteStop.objects.all(),
        }
        counts = {label: queryset.count() for label, queryset in querysets.items()}
        candidate_ids = {
            label: [str(pk) for pk in queryset.order_by("pk").values_list("pk", flat=True)]
            for label, queryset in querysets.items()
        }
        fingerprint_body = json.dumps(candidate_ids, sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256(fingerprint_body.encode("utf-8")).hexdigest()
        return {
            "counts": counts,
            "fingerprint": fingerprint,
            "unclassified_events": unknown.count(),
            "unclassified_availability": unclassified_availability,
            "unclassified_event_kinds": unknown_kinds,
            "financial_integrity_issues": _financial_integrity_issues(),
            "preserved": self._preserved_report(),
        }

    def _preserved_report(self) -> dict:
        roles = {}
        for role, label in User.Role.choices:
            qs = User.objects.filter(role=role)
            roles[label] = {
                "active": qs.filter(is_active=True).count(),
                "disabled": qs.filter(is_active=False).count(),
            }
        availability = {
            kind: Availability.objects.filter(kind=kind).count()
            for kind in ("off_duty", "break", "maintenance", "personal")
        }
        from allauth.account.models import EmailAddress
        from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
        from django.contrib.sessions.models import Session
        from django.db.migrations.recorder import MigrationRecorder

        return {
            "Users": roles,
            "Organizations": Organization.objects.count(),
            "Resources/fleet": {
                status: Resource.objects.filter(status=status).count()
                for status in ("available", "booked", "off_duty")
            },
            "Operational zones": OperationalZone.objects.count(),
            "Price Cards": PriceCard.objects.count(),
            "FixedRate versions": FixedRate.objects.count(),
            "Pricing versions": PricingVersion.objects.count(),
            "Pricing configurations": PricingConfiguration.objects.count(),
            "Locations retained for manual review": Location.objects.count(),
            "Non-booking availability blocks": availability,
            "Unlinked booking blocks retained for manual review": Availability.objects.filter(
                kind="booking"
            )
            .exclude(pk__in=_booking_availability().values("pk"))
            .count(),
            "Push subscriptions": PushSubscription.objects.count(),
            "Google/social identities": SocialAccount.objects.count(),
            "Google/social app configuration": SocialApp.objects.count(),
            "Social tokens": SocialToken.objects.count(),
            "Email identities": EmailAddress.objects.count(),
            "Sessions": Session.objects.count(),
            "Django migration records": MigrationRecorder.Migration.objects.count(),
            "Daily reference sequences": self._count_if_table_exists("operations_dailysequence"),
            "Login attempt throttles": self._count_if_table_exists("operations_loginattempt"),
            "Preserved non-transaction DomainEvents": DomainEvent.objects.exclude(
                pk__in=_transaction_events().values("pk")
            ).count(),
        }

    @staticmethod
    def _count_if_table_exists(table_name: str) -> int:
        connection = connections["default"]
        if table_name not in connection.introspection.table_names():
            return 0
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) FROM {connection.ops.quote_name(table_name)}")
            return cursor.fetchone()[0]

    def _preserved_snapshot(self) -> dict:
        return self._preserved_report()

    def _write_report(self, plan: dict) -> None:
        self.stdout.write("Operational data proposed for removal")
        self.stdout.write("-------------------------------------")
        for label, count in plan["counts"].items():
            self.stdout.write(f"{label}: {count}")
        self.stdout.write(f"Cleanup scope fingerprint: {plan['fingerprint']}")
        self.stdout.write("\nPreserved configuration and security data")
        self.stdout.write("-----------------------------------------")
        for label, value in plan["preserved"].items():
            if label == "Users":
                for role, counts in value.items():
                    self.stdout.write(
                        f"{role}: active {counts['active']}, disabled {counts['disabled']}"
                    )
            elif isinstance(value, dict):
                for sublabel, count in value.items():
                    self.stdout.write(f"{label} — {sublabel}: {count}")
            else:
                self.stdout.write(f"{label}: {value}")
        self.stdout.write(
            "\nCleanup removes all transactional operational rows in the listed scope. "
            "It does not infer which Client, field, fleet, pricing, or location records are test data."
        )
        self.stdout.write(f"COD graph integrity issues: {plan['financial_integrity_issues']}")
        self.stdout.write(
            f"Unclassified delivery-linked DomainEvents (execution blocker): {plan['unclassified_events']}"
        )
        self.stdout.write(
            f"Unclassified delivery-linked availability rows (execution blocker): {plan['unclassified_availability']}"
        )

    def _lock_cleanup_tables(self) -> None:
        connection = connections["default"]
        if connection.vendor != "postgresql":
            return
        from allauth.account.models import EmailAddress
        from allauth.socialaccount.models import SocialAccount, SocialApp, SocialToken
        from django.contrib.sessions.models import Session

        models = (
            Assignment,
            Availability,
            BulkBooking,
            ClientSettlement,
            Delivery,
            DeliveryRun,
            DomainEvent,
            Job,
            LedgerEntry,
            Location,
            LocationUpdate,
            Notification,
            OperationalZone,
            Organization,
            PriceCard,
            PricingConfiguration,
            PricingVersion,
            ProofOfDelivery,
            PushOutbox,
            PushSubscription,
            Quote,
            QuoteConfirmation,
            Resource,
            RoutePlan,
            RouteStop,
            RunStop,
            StatusEvent,
            User,
            FixedRate,
            EmailAddress,
            SocialAccount,
            SocialApp,
            SocialToken,
            Session,
        )
        table_names = sorted({model._meta.db_table for model in models})
        quoted = ", ".join(connection.ops.quote_name(name) for name in table_names)
        with connection.cursor() as cursor:
            cursor.execute(f"LOCK TABLE {quoted} IN ACCESS EXCLUSIVE MODE")

    @staticmethod
    def _delete_transactional_data() -> None:
        events = _transaction_events()
        notifications = Notification.objects.filter(event__in=events.values("pk"))

        # Clear dependent operational messages and event history before deleting
        # the objects they describe. PushOutbox cascades from Notification too.
        PushOutbox.objects.filter(notification__in=notifications).delete()
        notifications.delete()
        events.delete()

        RunStop.objects.all().delete()
        RouteStop.objects.all().delete()
        Availability.objects.filter(pk__in=_booking_availability().values("pk")).delete()
        Assignment.objects.all().delete()
        ProofOfDelivery.objects.all().delete()
        StatusEvent.objects.all().delete()
        LedgerEntry.objects.filter(kind="cod_settled").delete()
        LedgerEntry.objects.exclude(kind="cod_settled").delete()
        ClientSettlement.objects.all().delete()
        QuoteConfirmation.objects.all().delete()
        Job.objects.filter(current_quote__isnull=False).update(current_quote=None)
        Quote.objects.all().delete()
        DeliveryRun.objects.all().delete()
        RoutePlan.objects.all().delete()
        LocationUpdate.objects.all().delete()
        Delivery.objects.all().delete()
        Job.objects.all().delete()
        BulkBooking.objects.all().delete()

    @staticmethod
    def _assert_cleanup_complete() -> None:
        checks = (
            Delivery,
            Job,
            BulkBooking,
            Quote,
            QuoteConfirmation,
            Assignment,
            StatusEvent,
            ProofOfDelivery,
            DeliveryRun,
            RunStop,
            LedgerEntry,
            ClientSettlement,
            LocationUpdate,
            RoutePlan,
            RouteStop,
        )
        remaining = {
            model.__name__: model.objects.count() for model in checks if model.objects.exists()
        }
        if remaining:
            raise CommandError(f"Cleanup postcondition failed for: {', '.join(sorted(remaining))}.")
        if _booking_availability().exists():
            raise CommandError(
                "Cleanup postcondition failed for linked booking availability blocks."
            )
        if _transaction_events().exists():
            raise CommandError("Cleanup postcondition failed for transactional DomainEvents.")
        if Notification.objects.filter(event__in=_transaction_events().values("pk")).exists():
            raise CommandError("Cleanup postcondition failed for operational notifications.")
        if PushOutbox.objects.filter(
            notification__event__in=_transaction_events().values("pk")
        ).exists():
            raise CommandError("Cleanup postcondition failed for related push outbox rows.")
