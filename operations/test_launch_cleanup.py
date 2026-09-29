from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch
from uuid import uuid4

from allauth.socialaccount.models import SocialAccount
from django.contrib.sessions.backends.db import SessionStore
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from .management.commands.prepare_production_launch import (
    SCOPE_CONFIRMATION,
    Command,
    _verify_immutable_guards,
)
from .models import (
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


class ProductionLaunchCleanupTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            "launch-admin", "admin@example.test", "unused", role=User.Role.ADMIN
        )
        self.organization = Organization.objects.create(name="Launch Client")
        self.client_user = User.objects.create_user(
            "launch-client",
            email="client@example.test",
            role=User.Role.CLIENT,
            organization=self.organization,
        )
        self.rider = User.objects.create_user(
            "launch-rider", email="rider@example.test", role=User.Role.RIDER, is_active=False
        )
        self.driver = User.objects.create_user(
            "launch-driver", email="driver@example.test", role=User.Role.DRIVER
        )
        self.rider_resource = Resource.objects.create(
            user=self.rider,
            vehicle_type="bike",
            vehicle_name="Bike 1",
            registration="CLEAN-B1",
            status="off_duty",
        )
        self.driver_resource = Resource.objects.create(
            user=self.driver,
            vehicle_type="car",
            vehicle_name="Car 1",
            registration="CLEAN-C1",
            status="available",
        )
        self.zone = OperationalZone.objects.create(name="Launch Zone")
        self.origin = Location.objects.create(
            display_address="Pickup retained", area="Osu", zone=self.zone
        )
        self.destination = Location.objects.create(
            display_address="Destination retained", area="Labone", zone=self.zone
        )
        self.price_card = PriceCard.objects.create(name="Launch Card", scope_key="launch-general")
        self.pricing_version = PricingVersion.objects.create(label="Launch pricing", rules={})
        self.rate = FixedRate.objects.create(
            card=self.price_card,
            origin=self.zone,
            destination=self.zone,
            vehicle="bike",
            service="standard",
            amount=Decimal("25.00"),
            actor=self.admin,
            reason="Retained launch configuration",
        )
        self.pricing_configuration = PricingConfiguration.objects.create(
            current_version=self.pricing_version
        )

        self.bulk = BulkBooking.objects.create(
            organization=self.organization,
            created_by=self.client_user,
            pickup=self.origin,
            request_key=uuid4(),
        )
        self.job = Job.objects.create(
            organization=self.organization,
            created_by=self.client_user,
            is_bulk=True,
            bulk_booking=self.bulk,
        )
        self.delivery = Delivery.objects.create(
            job=self.job,
            pickup=self.origin,
            destination=self.destination,
            recipient_name="Test Recipient",
            recipient_phone="000",
            package_description="Test goods",
            vehicle_type="bike",
            payment_method="cod",
            cod_amount=Decimal("80.00"),
        )
        self.quote = Quote.objects.create(
            job=self.job,
            pricing_version=self.pricing_version,
            amount=Decimal("25.00"),
            expires_at=timezone.now() + timedelta(days=1),
        )
        self.job.current_quote = self.quote
        self.job.save(update_fields=["current_quote"])
        self.confirmation = QuoteConfirmation.objects.create(
            job=self.job, quote=self.quote, actor=self.client_user
        )
        self.assignment = Assignment.objects.create(
            delivery=self.delivery,
            resource=self.rider_resource,
            actor=self.admin,
            resource_snapshot={"vehicle": "Bike 1"},
        )
        self.status_event = StatusEvent.objects.create(
            delivery=self.delivery, actor=self.admin, status=Delivery.Status.REQUESTED
        )
        self.proof = ProofOfDelivery.objects.create(
            delivery=self.delivery, actor=self.admin, recipient_name="Test Recipient"
        )
        self.delivery_run = DeliveryRun.objects.create(
            reference="DPT-RUN-CLEANUP",
            resource=self.rider_resource,
            operational_date=timezone.localdate(),
            created_by=self.admin,
        )
        self.run_stop = RunStop.objects.create(
            run=self.delivery_run, delivery=self.delivery, sequence=1
        )
        self.booking_block = Availability.objects.create(
            resource=self.rider_resource,
            delivery=self.delivery,
            kind="booking",
            starts_at=timezone.now(),
            ends_at=timezone.now() + timedelta(hours=1),
        )
        self.run_block = Availability.objects.create(
            resource=self.rider_resource,
            kind="booking",
            note=f"run:{self.delivery_run.pk}",
            starts_at=timezone.now(),
            ends_at=timezone.now() + timedelta(hours=2),
        )
        self.manual_booking_block = Availability.objects.create(
            resource=self.driver_resource,
            kind="booking",
            note="manual review",
            starts_at=timezone.now(),
            ends_at=timezone.now() + timedelta(hours=1),
        )
        self.off_duty_block = Availability.objects.create(
            resource=self.rider_resource,
            kind="off_duty",
            starts_at=timezone.now(),
            ends_at=timezone.now() + timedelta(days=1),
        )
        self.location_update = LocationUpdate.objects.create(
            resource=self.rider_resource,
            latitude=Decimal("5.5"),
            longitude=Decimal("-0.2"),
            accuracy_metres=10,
            recorded_at=timezone.now(),
        )
        self.route = RoutePlan.objects.create(
            resource=self.rider_resource,
            origin=self.origin,
            provider="local",
            cache_key="c" * 64,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        self.route_stop = RouteStop.objects.create(
            route=self.route,
            delivery=self.delivery,
            position=1,
            road_metres=100,
            travel_seconds=60,
        )

        self.collected = LedgerEntry.objects.create(
            delivery=self.delivery,
            actor=self.rider,
            kind="cod_collected",
            amount=Decimal("80.00"),
            currency="GHS",
            method="cash",
        )
        self.reconciled = LedgerEntry.objects.create(
            delivery=self.delivery,
            actor=self.admin,
            kind="cod_reconciled",
            amount=Decimal("30.00"),
            currency="GHS",
            method="cash",
        )
        self.reconciled_second = LedgerEntry.objects.create(
            delivery=self.delivery,
            actor=self.admin,
            kind="cod_reconciled",
            amount=Decimal("50.00"),
            currency="GHS",
            method="cash",
        )
        self.settlement = ClientSettlement.objects.create(
            organization=self.organization,
            recorded_by=self.admin,
            reference="DPT-SET-CLEANUP",
            amount=Decimal("30.00"),
            currency="GHS",
        )
        self.settled = LedgerEntry.objects.create(
            delivery=self.delivery,
            actor=self.admin,
            kind="cod_settled",
            amount=Decimal("30.00"),
            currency="GHS",
            settlement=self.settlement,
            reconciled_entry=self.reconciled,
        )
        self.fee = LedgerEntry.objects.create(
            delivery=self.delivery, actor=self.admin, kind="delivery_fee", amount=Decimal("25.00")
        )

        self.delivery_event = DomainEvent.objects.create(
            actor=self.admin, kind="delivery.created", delivery=self.delivery
        )
        self.run_event = DomainEvent.objects.create(
            actor=self.admin,
            kind="run.created",
            payload={"run_id": str(self.delivery_run.pk)},
        )
        self.booking_event = DomainEvent.objects.create(
            actor=self.driver,
            kind="availability.created",
            payload={"block_id": str(self.booking_block.pk), "kind": "booking"},
        )
        self.schedule_event = DomainEvent.objects.create(
            actor=self.driver,
            kind="availability.created",
            payload={"block_id": str(self.off_duty_block.pk), "kind": "off_duty"},
        )
        self.security_event = DomainEvent.objects.create(
            actor=self.admin,
            kind="USER_PASSWORD_RESET",
            payload={"target_user_id": self.client_user.pk},
        )
        self.config_event = DomainEvent.objects.create(
            actor=self.admin,
            kind="pricing.card_saved",
            payload={"card_id": str(self.price_card.pk)},
        )
        self.operational_notification = Notification.objects.create(
            event=self.delivery_event,
            recipient=self.client_user,
            title="Test notice",
            message="Test transaction.",
        )
        self.outbox = PushOutbox.objects.create(notification=self.operational_notification)
        self.preserved_notification = Notification.objects.create(
            event=self.security_event,
            recipient=self.admin,
            title="Security notice",
            message="Account event.",
        )
        self.subscription = PushSubscription.objects.create(
            user=self.client_user,
            endpoint="https://push.example.test/device/1",
            p256dh="public-key-placeholder",
            auth="auth-placeholder",
        )
        SocialAccount.objects.create(user=self.client_user, provider="google", uid="launch-google")
        self.session = SessionStore()
        self.session["_auth_user_id"] = str(self.admin.pk)
        self.session.save()
        session_key = self.session.session_key
        assert session_key is not None
        self.session_key: str = session_key

    def cleanup(self, **extra):
        options = {
            "execute": True,
            "dry_run": False,
            "backup_confirmed": True,
            "confirm_scope": SCOPE_CONFIRMATION,
            "confirm_plan": Command()._build_plan()["fingerprint"],
            "confirm_production": False,
            **extra,
        }
        return call_command("prepare_production_launch", stdout=StringIO(), **options)

    def test_default_dry_run_reports_scope_and_changes_nothing(self):
        before = (
            Delivery.objects.count(),
            LedgerEntry.objects.count(),
            DomainEvent.objects.count(),
        )
        output = StringIO()
        call_command("prepare_production_launch", dry_run=True, stdout=output)
        self.assertEqual(
            (Delivery.objects.count(), LedgerEntry.objects.count(), DomainEvent.objects.count()),
            before,
        )
        self.assertIn("DRY RUN ONLY", output.getvalue())
        self.assertIn("Deliveries: 1", output.getvalue())
        self.assertIn("Bulk bookings: 1", output.getvalue())
        self.assertIn("COD collected entries: 1", output.getvalue())
        self.assertIn("COD reconciled entries: 2", output.getvalue())
        self.assertIn("COD settled entries: 1", output.getvalue())
        self.assertIn("FixedRate versions: 1", output.getvalue())
        self.assertIn("Client: active 1, disabled 0", output.getvalue())

    def test_dry_run_is_the_default_and_preserves_every_row(self):
        before = self._counts()
        call_command("prepare_production_launch", stdout=StringIO())
        self.assertEqual(self._counts(), before)

    def test_execute_requires_explicit_scope_and_backup_acknowledgement(self):
        with self.assertRaises(CommandError):
            call_command("prepare_production_launch", execute=True, stdout=StringIO())
        with self.assertRaises(CommandError):
            call_command(
                "prepare_production_launch",
                execute=True,
                backup_confirmed=True,
                confirm_scope="wrong",
                stdout=StringIO(),
            )
        with self.assertRaises(CommandError):
            call_command(
                "prepare_production_launch",
                execute=True,
                backup_confirmed=True,
                confirm_scope=SCOPE_CONFIRMATION,
                confirm_plan="",
                stdout=StringIO(),
            )
        self.assertTrue(Delivery.objects.filter(pk=self.delivery.pk).exists())

    @override_settings(PRODUCTION=True)
    def test_production_execute_requires_separate_production_acknowledgement(self):
        plan = Command()._build_plan()["fingerprint"]
        with self.assertRaises(CommandError):
            call_command(
                "prepare_production_launch",
                execute=True,
                backup_confirmed=True,
                confirm_scope=SCOPE_CONFIRMATION,
                confirm_plan=plan,
                confirm_production=False,
                stdout=StringIO(),
            )
        self.assertTrue(Delivery.objects.filter(pk=self.delivery.pk).exists())

    def test_execution_refuses_to_start_without_an_active_admin(self):
        User.objects.filter(pk=self.admin.pk).update(is_active=False)
        with self.assertRaises(CommandError):
            self.cleanup()
        self.assertTrue(Delivery.objects.filter(pk=self.delivery.pk).exists())

    def test_execute_rejects_stale_or_unreviewed_scope_fingerprint(self):
        with self.assertRaises(CommandError):
            self.cleanup(confirm_plan="0" * 64)
        self.assertTrue(Delivery.objects.filter(pk=self.delivery.pk).exists())

    def test_execute_removes_transaction_graph_and_preserves_accounts_config_and_devices(self):
        preserved_ids = {
            "users": set(User.objects.values_list("pk", flat=True)),
            "resources": set(Resource.objects.values_list("pk", flat=True)),
            "locations": set(Location.objects.values_list("pk", flat=True)),
            "zones": set(OperationalZone.objects.values_list("pk", flat=True)),
            "cards": set(PriceCard.objects.values_list("pk", flat=True)),
            "rates": set(FixedRate.objects.values_list("pk", flat=True)),
            "versions": set(PricingVersion.objects.values_list("pk", flat=True)),
            "subscriptions": set(PushSubscription.objects.values_list("pk", flat=True)),
            "identities": set(SocialAccount.objects.values_list("pk", flat=True)),
        }
        self.cleanup()
        for model in (
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
        ):
            self.assertFalse(model.objects.exists(), model.__name__)
        self.assertFalse(
            DomainEvent.objects.filter(
                pk__in=[self.delivery_event.pk, self.run_event.pk, self.booking_event.pk]
            ).exists()
        )
        self.assertFalse(Notification.objects.filter(pk=self.operational_notification.pk).exists())
        self.assertFalse(PushOutbox.objects.filter(pk=self.outbox.pk).exists())
        self.assertTrue(Notification.objects.filter(pk=self.preserved_notification.pk).exists())
        self.assertTrue(DomainEvent.objects.filter(pk=self.security_event.pk).exists())
        self.assertTrue(DomainEvent.objects.filter(pk=self.config_event.pk).exists())
        self.assertTrue(DomainEvent.objects.filter(pk=self.schedule_event.pk).exists())
        self.assertTrue(Availability.objects.filter(pk=self.off_duty_block.pk).exists())
        self.assertTrue(Availability.objects.filter(pk=self.manual_booking_block.pk).exists())
        self.assertEqual(set(User.objects.values_list("pk", flat=True)), preserved_ids["users"])
        self.assertEqual(
            set(Resource.objects.values_list("pk", flat=True)), preserved_ids["resources"]
        )
        self.assertEqual(
            set(Location.objects.values_list("pk", flat=True)), preserved_ids["locations"]
        )
        self.assertEqual(
            set(OperationalZone.objects.values_list("pk", flat=True)), preserved_ids["zones"]
        )
        self.assertEqual(
            set(PriceCard.objects.values_list("pk", flat=True)), preserved_ids["cards"]
        )
        self.assertEqual(
            set(FixedRate.objects.values_list("pk", flat=True)), preserved_ids["rates"]
        )
        self.assertEqual(
            set(PricingVersion.objects.values_list("pk", flat=True)), preserved_ids["versions"]
        )
        self.assertEqual(
            set(PushSubscription.objects.values_list("pk", flat=True)),
            preserved_ids["subscriptions"],
        )
        self.assertEqual(
            set(SocialAccount.objects.values_list("pk", flat=True)), preserved_ids["identities"]
        )
        self.assertTrue(SessionStore(self.session_key).exists(self.session_key))
        self.assertEqual(Resource.objects.get(pk=self.rider_resource.pk).status, "off_duty")
        self.assertEqual(Resource.objects.get(pk=self.driver_resource.pk).status, "available")
        _verify_immutable_guards()
        with self.assertRaises(DatabaseError), transaction.atomic():
            FixedRate.objects.filter(pk=self.rate.pk).update(active=False)

    def test_execute_is_idempotent(self):
        self.cleanup()
        self.cleanup()
        self.assertEqual(Delivery.objects.count(), 0)
        self.assertEqual(User.objects.count(), 4)

    def test_failure_after_deletions_rolls_back_rows_and_restores_triggers(self):
        delete = Command._delete_transactional_data

        def delete_then_fail():
            delete()
            raise RuntimeError("injected cleanup failure")

        with patch.object(Command, "_delete_transactional_data", side_effect=delete_then_fail):
            with self.assertRaises(RuntimeError):
                self.cleanup()
        self.assertTrue(Delivery.objects.filter(pk=self.delivery.pk).exists())
        self.assertTrue(LedgerEntry.objects.filter(pk=self.collected.pk).exists())
        self.assertTrue(ClientSettlement.objects.filter(pk=self.settlement.pk).exists())
        self.assertTrue(DomainEvent.objects.filter(pk=self.delivery_event.pk).exists())
        _verify_immutable_guards()

    def test_unclassified_delivery_audit_event_blocks_execute_without_deleting(self):
        unexpected = DomainEvent.objects.create(
            actor=self.admin, kind="unclassified.audit", delivery=self.delivery
        )
        with self.assertRaises(CommandError):
            self.cleanup()
        self.assertTrue(Delivery.objects.filter(pk=self.delivery.pk).exists())
        self.assertTrue(DomainEvent.objects.filter(pk=unexpected.pk).exists())

    def test_broken_cod_graph_blocks_execute(self):
        LedgerEntry.objects.create(
            delivery=self.delivery,
            actor=self.admin,
            kind="cod_reconciled",
            amount=Decimal("79.00"),
            currency="GHS",
            method="cash",
        )
        with self.assertRaises(CommandError):
            self.cleanup()
        self.assertTrue(Delivery.objects.filter(pk=self.delivery.pk).exists())
        self.assertTrue(LedgerEntry.objects.filter(pk=self.settled.pk).exists())

    def test_immutable_triggers_are_present_and_enabled_after_cleanup(self):
        _verify_immutable_guards()
        self.cleanup()
        _verify_immutable_guards()

    def test_no_admin_can_be_removed_and_last_active_admin_stays(self):
        self.cleanup()
        self.assertEqual(User.objects.filter(role=User.Role.ADMIN, is_active=True).count(), 1)
        self.assertTrue(User.objects.filter(pk=self.admin.pk).exists())

    def test_cleanup_preserves_google_identity_sessions_and_push_subscription(self):
        self.cleanup()
        self.assertTrue(SocialAccount.objects.filter(user=self.client_user).exists())
        self.assertTrue(PushSubscription.objects.filter(pk=self.subscription.pk).exists())
        self.assertEqual(SessionStore(self.session_key).get("_auth_user_id"), str(self.admin.pk))

    def test_cleanup_preserves_zone_cards_fixed_rate_history_and_price_configuration(self):
        self.cleanup()
        self.assertTrue(OperationalZone.objects.filter(pk=self.zone.pk).exists())
        self.assertTrue(PriceCard.objects.filter(pk=self.price_card.pk).exists())
        self.assertTrue(FixedRate.objects.filter(pk=self.rate.pk).exists())
        self.assertTrue(PricingVersion.objects.filter(pk=self.pricing_version.pk).exists())
        self.assertTrue(
            PricingConfiguration.objects.filter(pk=self.pricing_configuration.pk).exists()
        )

    def test_unlinked_delivery_availability_blocks_are_reported_and_execution_stops(self):
        block = Availability.objects.create(
            resource=self.driver_resource,
            delivery=self.delivery,
            kind="off_duty",
            starts_at=timezone.now(),
            ends_at=timezone.now() + timedelta(hours=1),
        )
        with self.assertRaises(CommandError):
            self.cleanup()
        self.assertTrue(Availability.objects.filter(pk=block.pk).exists())

    def test_normal_client_booking_workflow_works_after_local_cleanup(self):
        self.cleanup()
        from . import workflow

        with patch("operations.maps.RoadDistance.road_kilometres", return_value=Decimal("0")):
            delivery = workflow.save_booking(
                actor=self.client_user,
                data={
                    "booking_key": str(uuid4()),
                    "revision": 0,
                    "pickup_zone": str(self.zone.pk),
                    "destination_zone": str(self.zone.pk),
                    "pickup_address": "Fresh launch pickup",
                    "pickup_area": "Osu",
                    "destination_address": "Fresh launch destination",
                    "destination_area": "Labone",
                    "recipient_name": "New Recipient",
                    "recipient_phone": "+233200000001",
                    "package_description": "Launch parcel",
                    "instructions": "",
                    "vehicle_type": "bike",
                    "service": "standard",
                    "payment_method": "paid",
                    "cod_amount": "0.00",
                },
            )
        self.assertTrue(Delivery.objects.filter(pk=delivery.pk).exists())
        self.assertTrue(Quote.objects.filter(job=delivery.job).exists())
        self.assertTrue(
            DomainEvent.objects.filter(delivery=delivery, kind="delivery.created").exists()
        )

    def _counts(self):
        models = (
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
            DomainEvent,
            Notification,
            PushOutbox,
            LocationUpdate,
            RoutePlan,
            RouteStop,
            Availability,
            User,
            Resource,
            Organization,
            OperationalZone,
            PriceCard,
            FixedRate,
            PricingVersion,
            PricingConfiguration,
            Location,
            PushSubscription,
        )
        return {model.__name__: model.objects.count() for model in models}
