import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, connection, transaction
from django.test import Client, TestCase, override_settings, skipUnlessDBFeature
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from . import price_cards, workflow
from .booking_forms import BookingForm
from .models import (
    Assignment,
    Availability,
    Delivery,
    DomainEvent,
    LedgerEntry,
    Location,
    Notification,
    OperationalZone,
    Organization,
    PriceCard,
    ProofOfDelivery,
    Quote,
    QuoteConfirmation,
    Resource,
    StatusEvent,
    User,
)


class Phase2Tests(TestCase):
    organization: Organization
    other_organization: Organization
    customer: User
    other_customer: User
    admin: User
    rider: User
    other_rider: User
    driver: User
    bike: Resource
    second_bike: Resource
    car: Resource
    origin: OperationalZone
    destination: OperationalZone
    general_card: PriceCard

    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(name="Example shop")
        cls.other_organization = Organization.objects.create(name="Another shop")
        cls.customer = User.objects.create_user(
            "client2", role="client", organization=cls.organization
        )
        cls.other_customer = User.objects.create_user(
            "other2", role="client", organization=cls.other_organization
        )
        cls.admin = User.objects.create_user("dispatch2", role="admin", first_name="Dispatch")
        cls.origin = OperationalZone.objects.create(name="Osu")
        cls.destination = OperationalZone.objects.create(name="East Legon")
        cls.general_card = price_cards.save_card(actor=cls.admin, name="Phase 2 test card")
        price_cards.save_route_price(
            actor=cls.admin, card_id=cls.general_card.pk,
            origin_id=cls.origin.pk, destination_id=cls.destination.pk,
            bike_price=Decimal("20.00"), car_price=Decimal("50.00"),
            service="standard", bidirectional=True, reason="Test fixture rates",
        )
        price_cards.save_route_price(
            actor=cls.admin, card_id=cls.general_card.pk,
            origin_id=cls.origin.pk, destination_id=cls.destination.pk,
            bike_price=Decimal("30.00"), car_price=Decimal("50.00"),
            service="premium", bidirectional=True, reason="Test fixture Premium rates",
        )
        cls.rider = User.objects.create_user("rider2", role="rider", first_name="Kofi")
        cls.other_rider = User.objects.create_user("rider3", role="rider", first_name="Esi")
        cls.driver = User.objects.create_user("driver2", role="driver")
        cls.bike = Resource.objects.create(
            user=cls.rider,
            vehicle_type="bike",
            vehicle_name="Bike 1",
            registration="BIKE-TEST-1",
            status="available",
        )
        cls.second_bike = Resource.objects.create(
            user=cls.other_rider,
            vehicle_type="bike",
            vehicle_name="Bike 2",
            registration="BIKE-TEST-2",
            status="available",
        )
        cls.car = Resource.objects.create(
            user=cls.driver,
            vehicle_type="car",
            vehicle_name="Car 1",
            registration="CAR-TEST-1",
            status="available",
        )

    def setUp(self):
        road_distance = patch(
            "operations.maps.RoadDistance.road_kilometres", return_value=Decimal("0")
        )
        road_distance.start()
        self.addCleanup(road_distance.stop)

    def booking_data(self, **overrides):
        data = {
            "booking_key": str(uuid.uuid4()),
            "revision": 0,
            "pickup_zone": str(self.origin.pk),
            "destination_zone": str(self.destination.pk),
            "pickup_address": "Shop 14, Oxford Street",
            "pickup_area": "Osu",
            "pickup_instructions": "Blue door",
            "destination_address": "House 2, Boundary Road",
            "destination_area": "East Legon",
            "destination_instructions": "Call at the gate",
            "recipient_name": "Ama Test",
            "recipient_phone": "+233200000000",
            "package_description": "Box of shoes",
            "instructions": "Keep dry",
            "vehicle_type": "bike",
            "service": "standard",
            "payment_method": "cod",
            "cod_amount": "180.00",
        }
        data.update(overrides)
        return data

    def draft(self, **overrides):
        return workflow.save_booking(actor=self.customer, data=self.booking_data(**overrides))

    def confirmed(self, **overrides):
        delivery = self.draft(**overrides)
        self.confirm_without_auto_assignment(delivery)
        delivery.refresh_from_db()
        return delivery

    def confirm_without_auto_assignment(self, delivery):
        # These tests exercise manual dispatch; preserve duty state while suppressing auto-assign.
        statuses = dict(Resource.objects.values_list("pk", "status"))
        Resource.objects.filter(status="available").update(status="off_duty")
        try:
            workflow.confirm_quote(actor=self.customer, quote_id=delivery.job.current_quote_id)
        finally:
            for resource_id, status in statuses.items():
                Resource.objects.filter(pk=resource_id).update(status=status)

    def auto_confirmed(self, **overrides):
        delivery = self.draft(**overrides)
        workflow.confirm_quote(actor=self.customer, quote_id=delivery.job.current_quote_id)
        delivery.refresh_from_db()
        return delivery

    def assign(self, delivery, **overrides):
        start = timezone.now() + timedelta(minutes=1)
        data = {
            "actor": self.admin,
            "delivery_id": delivery.pk,
            "resource_id": self.bike.pk,
            "starts_at": start,
            "ends_at": start + timedelta(hours=1),
            "request_key": uuid.uuid4(),
        }
        data.update(overrides)
        return workflow.assign_delivery(**data)

    def underway(self, **overrides):
        delivery = self.confirmed(**overrides)
        self.assign(delivery)
        for before, after in [
            ("assigned", "to_pickup"),
            ("to_pickup", "picked_up"),
            ("picked_up", "in_transit"),
        ]:
            workflow.transition_delivery(
                actor=self.rider,
                delivery_id=delivery.pk,
                target=after,
                expected_status=before,
                request_key=uuid.uuid4(),
            )
        delivery.refresh_from_db()
        return delivery

    def collect(self, delivery, **overrides):
        data = {
            "actor": self.rider,
            "delivery_id": delivery.pk,
            "request_key": uuid.uuid4(),
            "amount": Decimal("180.00"),
            "method": "cash",
        }
        data.update(overrides)
        return workflow.collect_cod(**data)

    def test_booking_persists_structured_ungeocoded_locations(self):
        with patch("operations.maps.RoadDistance.road_kilometres", return_value=None):
            delivery = self.draft(pickup_zone="", destination_zone="")
        self.assertIsNone(delivery.reference)
        self.assertIsNone(delivery.pickup.latitude)
        self.assertIsNone(delivery.destination.longitude)
        self.assertEqual(delivery.pickup.area, "Unclassified")
        self.assertEqual(delivery.cod_amount, Decimal("180.00"))
        self.assertEqual(delivery.status, "requested")
        self.assertIsNone(delivery.job.current_quote)
        self.assertTrue(delivery.job.quote_requested)
        self.assertEqual(StatusEvent.objects.get(delivery=delivery).previous_status, "")

    def test_duplicate_booking_creates_one_delivery(self):
        data = self.booking_data()
        first = workflow.save_booking(actor=self.customer, data=data)
        second = workflow.save_booking(actor=self.customer, data=data)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Delivery.objects.count(), 1)
        self.assertEqual(Quote.objects.count(), 1)
        with self.assertRaises(PermissionDenied):
            workflow.save_booking(actor=self.other_customer, data=data)

    def test_booking_validation_and_premium_window(self):
        for change in [
            {"recipient_phone": "letters"},
            {"payment_method": "cod", "cod_amount": "-1"},
            {"service": "premium"},
            {"pickup_address": ""},
        ]:
            with self.subTest(change=change):
                form = BookingForm(self.booking_data(**change))
                self.assertFalse(form.is_valid())
        now = timezone.now()
        premium = self.draft(
            service="premium",
            window_start=now + timedelta(days=1),
            window_end=now + timedelta(days=1, hours=2),
        )
        self.assertEqual(premium.job.current_quote.amount, Decimal("30.00"))

    def test_already_paid_does_not_keep_cod_value(self):
        delivery = self.draft(payment_method="paid", cod_amount="999")
        self.assertEqual(delivery.cod_amount, 0)

    def test_edit_replaces_quote_and_rejects_stale_revision(self):
        delivery = self.draft()
        old_quote = delivery.job.current_quote
        data = self.booking_data(revision=delivery.job.revision, recipient_name="Updated recipient")
        edited = workflow.save_booking(actor=self.customer, data=data, delivery_id=delivery.pk)
        self.assertNotEqual(edited.job.current_quote_id, old_quote.pk)
        self.assertEqual(old_quote.breakdown["delivery"]["recipient"], "Ama Test")
        with self.assertRaises(ValidationError):
            workflow.confirm_quote(actor=self.customer, quote_id=old_quote.pk)
        with self.assertRaises(ValidationError):
            workflow.save_booking(actor=self.customer, data=data, delivery_id=delivery.pk)

    def test_confirmed_edit_and_foreign_edit_denied(self):
        delivery = self.confirmed()
        with self.assertRaises(ValidationError):
            workflow.save_booking(
                actor=self.customer,
                data=self.booking_data(revision=delivery.job.revision),
                delivery_id=delivery.pk,
            )
        with self.assertRaises(PermissionDenied):
            workflow.save_booking(
                actor=self.other_customer, data=self.booking_data(), delivery_id=delivery.pk
            )

    def test_confirmation_reference_idempotency_and_audiences(self):
        delivery = self.draft()
        quote_id = delivery.job.current_quote_id
        first = workflow.confirm_quote(actor=self.customer, quote_id=quote_id)
        workflow.confirm_quote(actor=self.customer, quote_id=quote_id)
        delivery.refresh_from_db()
        self.assertRegex(delivery.reference, r"^DPT-\d{6}-\d{4}$")
        self.assertEqual(QuoteConfirmation.objects.count(), 1)
        event = DomainEvent.objects.get(kind="quote.confirmed")
        self.assertEqual(Assignment.objects.filter(delivery=delivery).count(), 1)
        self.assertEqual(
            DomainEvent.objects.filter(delivery=delivery, kind="delivery.assigned").count(), 1
        )
        self.assertSetEqual(
            set(Notification.objects.filter(event=event).values_list("recipient_id", flat=True)),
            {self.admin.pk, self.customer.pk},
        )
        self.assertEqual(first.quote_id, quote_id)
        second = self.confirmed()
        self.assertNotEqual(second.reference, delivery.reference or "")

    def test_confirmation_automatically_assigns_standard_without_inventing_a_time(self):
        delivery = self.auto_confirmed()
        assignment = delivery.assignments.get()
        self.assertEqual(delivery.status, Delivery.Status.ASSIGNED)
        self.assertEqual(assignment.resource_id, min(self.bike.pk, self.second_bike.pk, key=str))
        self.assertIsNone(assignment.starts_at)
        self.assertIsNone(assignment.ends_at)
        self.assertFalse(Availability.objects.filter(delivery=delivery).exists())
        event = DomainEvent.objects.get(delivery=delivery, kind="delivery.assigned")
        self.assertEqual(event.payload["assignment_source"], "automatic")
        self.assertEqual(event.payload["resource_id"], str(assignment.resource_id))
        self.assertEqual(event.payload["resource_workload"], 0)
        self.assertEqual(event.payload["operational_date"], timezone.localdate().isoformat())
        self.assertEqual(
            set(Notification.objects.filter(event=event).values_list("recipient_id", flat=True)),
            {self.admin.pk, self.customer.pk, assignment.resource.user_id},
        )
        self.client.force_login(self.admin)
        self.assertContains(
            self.client.get(reverse("delivery-detail", args=[delivery.pk])),
            "Automatically assigned to",
        )
        self.client.force_login(self.customer)
        client_detail = self.client.get(reverse("delivery-detail", args=[delivery.pk]))
        self.assertNotContains(client_detail, "resource_workload")
        self.assertNotContains(client_detail, "Automatically assigned to")

    def test_automatic_assignment_skips_off_duty_and_unapproved_resources(self):
        self.bike.status = "off_duty"
        self.bike.save(update_fields=["status"])
        delivery = self.auto_confirmed()
        self.assertEqual(delivery.assignments.get().resource_id, self.second_bike.pk)

        self.second_bike.user.operational_access_approved = False
        self.second_bike.user.save(update_fields=["operational_access_approved"])
        delivery = self.auto_confirmed()
        self.assertEqual(delivery.status, Delivery.Status.CONFIRMED)
        self.assertFalse(delivery.assignments.exists())
        self.assertTrue(
            DomainEvent.objects.filter(
                delivery=delivery, kind="delivery.auto_assignment_unavailable"
            ).exists()
        )

    def test_inactive_and_vehicle_incompatible_resources_are_excluded(self):
        self.rider.is_active = False
        self.rider.save(update_fields=["is_active"])
        self.second_bike.status = "off_duty"
        self.second_bike.save(update_fields=["status"])
        delivery = self.auto_confirmed()
        self.assertEqual(delivery.status, Delivery.Status.CONFIRMED)
        self.assertFalse(delivery.assignments.exists())

        self.rider.is_active = True
        self.rider.save(update_fields=["is_active"])
        self.second_bike.status = "available"
        self.second_bike.save(update_fields=["status"])
        delivery = self.auto_confirmed(vehicle_type="car")
        self.assertEqual(delivery.assignments.get().resource_id, self.car.pk)

    def test_block_availability_and_expired_blocks_use_the_relevant_period(self):
        now = timezone.now()
        block = Availability.objects.create(
            resource=self.bike,
            kind="break",
            starts_at=now + timedelta(minutes=15),
            ends_at=now + timedelta(hours=2),
        )
        delivery = self.auto_confirmed()
        self.assertEqual(delivery.assignments.get().resource_id, self.second_bike.pk)
        block.refresh_from_db()
        self.assertIsNone(block.cancelled_at)

        expired = Availability.objects.create(
            resource=self.second_bike,
            kind="break",
            starts_at=now - timedelta(hours=2),
            ends_at=now - timedelta(minutes=1),
        )
        delivery = self.auto_confirmed()
        self.assertEqual(delivery.assignments.get().resource_id, self.second_bike.pk)
        expired.refresh_from_db()
        self.assertIsNone(expired.cancelled_at)

    def test_premium_uses_exact_window_and_standard_respects_same_day_run(self):
        slot_start = timezone.now() + timedelta(days=1)
        slot_start = slot_start.replace(hour=12, minute=0, second=0, microsecond=0)
        slot_end = slot_start + timedelta(hours=1)
        Availability.objects.create(
            resource=self.bike,
            kind="break",
            starts_at=slot_start - timedelta(hours=2),
            ends_at=slot_start - timedelta(hours=1),
        )
        self.second_bike.status = "off_duty"
        self.second_bike.save(update_fields=["status"])
        delivery = self.auto_confirmed(
            service="premium",
            window_start=timezone.localtime(slot_start).strftime("%Y-%m-%dT%H:%M"),
            window_end=timezone.localtime(slot_end).strftime("%Y-%m-%dT%H:%M"),
        )
        assignment = delivery.assignments.get()
        self.assertEqual(assignment.resource_id, self.bike.pk)
        self.assertEqual(assignment.starts_at, delivery.window_start)
        self.assertEqual(assignment.ends_at, delivery.window_end)

        self.second_bike.status = "available"
        self.second_bike.save(update_fields=["status"])
        source = self.confirmed()
        from . import runs

        runs.create_run(
            actor=self.admin,
            resource_id=self.bike.pk,
            operational_date=timezone.localdate(),
            delivery_ids=[source.pk],
            request_key=uuid.uuid4(),
        )
        standard = self.auto_confirmed()
        self.assertEqual(standard.assignments.get().resource_id, self.second_bike.pk)

    def test_premium_block_overlapping_saved_window_excludes_resource(self):
        slot_start = timezone.now() + timedelta(days=1)
        slot_start = slot_start.replace(hour=14, minute=0, second=0, microsecond=0)
        slot_end = slot_start + timedelta(hours=1)
        Availability.objects.create(
            resource=self.bike,
            kind="personal",
            starts_at=slot_start + timedelta(minutes=15),
            ends_at=slot_start + timedelta(minutes=30),
        )
        delivery = self.auto_confirmed(
            service="premium",
            window_start=timezone.localtime(slot_start).strftime("%Y-%m-%dT%H:%M"),
            window_end=timezone.localtime(slot_end).strftime("%Y-%m-%dT%H:%M"),
        )
        self.assertEqual(delivery.assignments.get().resource_id, self.second_bike.pk)

    def test_workload_ignores_terminal_deliveries_and_uses_stable_tie_breaks(self):
        self.second_bike.status = "off_duty"
        self.second_bike.save(update_fields=["status"])
        first = self.auto_confirmed()
        second = self.auto_confirmed()
        self.assertEqual(first.assignments.get().resource_id, self.bike.pk)
        self.assertEqual(second.assignments.get().resource_id, self.bike.pk)

        self.second_bike.status = "available"
        self.second_bike.save(update_fields=["status"])
        third = self.auto_confirmed()
        self.assertEqual(third.assignments.get().resource_id, self.second_bike.pk)
        Delivery.objects.filter(pk=first.pk).update(status=Delivery.Status.DELIVERED)
        fourth = self.auto_confirmed()
        self.assertEqual(fourth.assignments.get().resource_id, self.bike.pk)

    def test_tie_prefers_longest_since_assignment_then_resource_primary_key(self):
        old_delivery = self.confirmed()
        recent_delivery = self.confirmed()
        Assignment.objects.create(
            delivery=old_delivery,
            resource=self.bike,
            actor=self.admin,
            created_at=timezone.now() - timedelta(days=2),
        )
        Assignment.objects.create(
            delivery=recent_delivery,
            resource=self.second_bike,
            actor=self.admin,
            created_at=timezone.now() - timedelta(days=1),
        )
        Delivery.objects.filter(pk__in=[old_delivery.pk, recent_delivery.pk]).update(
            status=Delivery.Status.DELIVERED
        )
        delivery = self.auto_confirmed()
        self.assertEqual(delivery.assignments.order_by("-pk").first().resource_id, self.bike.pk)

        self.bike.status = "off_duty"
        self.bike.save(update_fields=["status"])
        self.second_bike.status = "off_duty"
        self.second_bike.save(update_fields=["status"])
        unassigned = self.auto_confirmed()
        self.assertEqual(unassigned.status, Delivery.Status.CONFIRMED)
        self.assertFalse(unassigned.assignments.exists())
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.admin,
                event__delivery=unassigned,
                event__kind="delivery.auto_assignment_unavailable",
            ).exists()
        )

    def test_admin_can_reassign_auto_assigned_delivery_and_client_cannot_choose_resource(self):
        delivery = self.auto_confirmed()
        original = delivery.assignments.get()
        replacement = self.second_bike if original.resource_id == self.bike.pk else self.bike
        start = timezone.now() + timedelta(hours=3)
        changed = workflow.assign_delivery(
            actor=self.admin,
            delivery_id=delivery.pk,
            resource_id=replacement.pk,
            starts_at=start,
            ends_at=start + timedelta(hours=1),
            request_key=uuid.uuid4(),
            expected_assignment=original.pk,
        )
        self.assertEqual(changed.resource_id, replacement.pk)

        fresh = self.draft()
        self.client.force_login(self.customer)
        response = self.client.post(
            reverse("quote-review", args=[fresh.job.current_quote_id]),
            {"resource_id": str(self.car.pk)},
        )
        self.assertEqual(response.status_code, 302)
        fresh.refresh_from_db()
        self.assertEqual(fresh.assignments.get().resource.vehicle_type, "bike")

    @skipUnlessDBFeature("has_select_for_update")
    def test_automatic_candidate_lock_targets_only_resource_rows(self):
        delivery = self.draft()
        with CaptureQueriesContext(connection) as queries:
            workflow.confirm_quote(actor=self.customer, quote_id=delivery.job.current_quote_id)
        lock_sql = next(
            query["sql"]
            for query in queries
            if 'FROM "operations_resource"' in query["sql"]
            and "FOR UPDATE" in query["sql"]
        )
        self.assertIn('FOR UPDATE OF "operations_resource"', lock_sql)

    def test_pricing_changes_do_not_change_accepted_quote(self):
        delivery = self.confirmed()
        original = delivery.job.current_quote
        rate = price_cards.current_rates().get(
            card=self.general_card, vehicle="bike", service="standard",
        )
        price_cards.save_rate(
            actor=self.admin, card_id=self.general_card.pk,
            rate_key=rate.rate_key, expected_version=rate.version,
            origin=self.origin, destination=self.destination, vehicle="bike",
            service="standard", amount=Decimal("99.00"), bidirectional=True,
            active=True, effective_at=timezone.now(), expires_at=None,
            reason="Future test price change",
        )
        new_delivery = self.draft()
        self.assertEqual(new_delivery.job.current_quote.amount, Decimal("99.00"))
        original.refresh_from_db()
        self.assertEqual(original.amount, Decimal("20.00"))
        with self.assertRaises(PermissionDenied):
            workflow.update_pricing(actor=self.customer, rules={})

    @override_settings(ALLOW_DEVELOPMENT_PRICING=False)
    def test_development_pricing_disabled_requests_admin_quote(self):
        # A delivery with no selected pricing areas has no eligible Price Card.
        delivery = self.draft(pickup_zone="", destination_zone="")
        self.assertIsNone(delivery.job.current_quote_id)
        self.assertTrue(delivery.job.quote_requested)
        self.assertEqual(Quote.objects.count(), 0)
        self.assertEqual(Delivery.objects.count(), 1)
        self.assertEqual(Location.objects.count(), 2)

    def test_assignment_conflicts_and_explicit_override(self):
        first = self.confirmed()
        a = self.assign(first)
        second = self.confirmed()
        with self.assertRaises(ValidationError):
            self.assign(second, starts_at=a.starts_at, ends_at=a.ends_at)
        with self.assertRaises(ValidationError):
            self.assign(second, starts_at=a.starts_at, ends_at=a.ends_at, override=True, reason=" ")
        result = self.assign(
            second,
            starts_at=a.starts_at,
            ends_at=a.ends_at,
            override=True,
            reason="Dispatch approved combined pickup",
        )
        self.assertEqual(result.override_reason, "Dispatch approved combined pickup")
        event = DomainEvent.objects.filter(delivery=second, kind="delivery.assigned").get()
        self.assertTrue(event.payload["override"])
        self.assertEqual(len(event.payload["conflicting_blocks"]), 1)

    def test_assignment_off_duty_wrong_vehicle_and_non_admin(self):
        delivery = self.confirmed()
        with self.assertRaises(ValidationError):
            self.assign(delivery, resource_id=self.car.pk)
        with self.assertRaises(PermissionDenied):
            self.assign(delivery, actor=self.rider)
        self.bike.status = "off_duty"
        self.bike.save()
        with self.assertRaises(ValidationError):
            self.assign(delivery)
        self.assign(delivery, override=True, reason="Rider has agreed to return to duty")

    def test_assignment_duplicate_and_reassignment_permissions(self):
        delivery = self.confirmed()
        key = uuid.uuid4()
        first = self.assign(delivery, request_key=key)
        again = self.assign(delivery, request_key=key)
        self.assertEqual(first.pk, again.pk)
        self.assign(delivery, resource_id=self.second_bike.pk, expected_assignment=first.pk)
        self.client.force_login(self.rider)
        self.assertEqual(
            self.client.get(reverse("delivery-detail", args=[delivery.pk])).status_code, 404
        )
        with self.assertRaises(PermissionDenied):
            workflow.transition_delivery(
                actor=self.rider,
                delivery_id=delivery.pk,
                expected_status="assigned",
                target="to_pickup",
                request_key=uuid.uuid4(),
            )
        self.assertEqual(
            Availability.objects.filter(delivery=delivery, cancelled_at__isnull=True).count(), 1
        )
        with self.assertRaises(ValidationError):
            self.assign(delivery, expected_assignment=first.pk)

    def test_transition_no_jumps_and_duplicates(self):
        delivery = self.confirmed()
        self.assign(delivery)
        with self.assertRaises(ValidationError):
            workflow.transition_delivery(
                actor=self.rider,
                delivery_id=delivery.pk,
                target="delivered",
                expected_status="assigned",
                request_key=uuid.uuid4(),
            )
        key = uuid.uuid4()
        for _ in range(2):
            workflow.transition_delivery(
                actor=self.rider,
                delivery_id=delivery.pk,
                target="to_pickup",
                expected_status="assigned",
                request_key=key,
            )
        self.assertEqual(
            StatusEvent.objects.filter(delivery=delivery, status="to_pickup").count(), 1
        )
        event = StatusEvent.objects.get(delivery=delivery, status="to_pickup")
        self.assertEqual(event.previous_status, "assigned")

    def test_exception_requires_note_and_resumes_without_completion(self):
        delivery = self.underway(payment_method="paid")
        with self.assertRaises(ValidationError):
            workflow.transition_delivery(
                actor=self.rider,
                delivery_id=delivery.pk,
                target="unreachable",
                expected_status="in_transit",
                request_key=uuid.uuid4(),
            )
        workflow.transition_delivery(
            actor=self.rider,
            delivery_id=delivery.pk,
            target="unreachable",
            expected_status="in_transit",
            note="Called twice; no answer",
            request_key=uuid.uuid4(),
        )
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, "unreachable")
        self.assertFalse(ProofOfDelivery.objects.filter(delivery=delivery).exists())
        self.assertTrue(
            Notification.objects.filter(
                event__delivery=delivery,
                recipient=self.admin,
                event__kind="delivery.status_changed",
            ).exists()
        )
        workflow.transition_delivery(
            actor=self.rider,
            delivery_id=delivery.pk,
            target="resume",
            expected_status="unreachable",
            request_key=uuid.uuid4(),
        )
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, "in_transit")

    def test_pod_requires_recipient_name_but_no_photo(self):
        delivery = self.underway(payment_method="paid")
        with self.assertRaises(ValidationError):
            workflow.complete_delivery(
                actor=self.rider, delivery_id=delivery.pk,
                request_key=uuid.uuid4(), recipient_name="",
            )
        proof = workflow.complete_delivery(
            actor=self.rider, delivery_id=delivery.pk,
            request_key=uuid.uuid4(), recipient_name="Ama", note="Left at reception",
        )
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, "delivered")
        self.assertEqual(proof.recipient_name, "Ama")
        self.assertEqual(proof.note, "Left at reception")
        self.assertEqual(proof.policy_snapshot, {"recipient_name": True})
        self.assertEqual(delivery.history.order_by("-created_at").first().status, "delivered")

    def test_cod_exact_collection_reconcile_idempotency_and_roles(self):
        delivery = self.underway()
        with self.assertRaises(ValidationError):
            self.collect(delivery, amount=179)
        with self.assertRaises(ValidationError):
            self.collect(delivery, method="momo", reference="")
        key = uuid.uuid4()
        collection = self.collect(delivery, request_key=key)
        self.collect(delivery, request_key=key)
        self.assertEqual(LedgerEntry.objects.filter(kind="cod_collected").count(), 1)
        with self.assertRaises(PermissionDenied):
            workflow.reconcile_cod(
                actor=self.rider,
                delivery_id=delivery.pk,
                request_key=uuid.uuid4(),
                reference="TEST",
            )
        key = uuid.uuid4()
        first = workflow.reconcile_cod(
            actor=self.admin,
            delivery_id=delivery.pk,
            request_key=key,
            reference="BATCH-001",
            note="Cash counted",
        )
        again = workflow.reconcile_cod(
            actor=self.admin, delivery_id=delivery.pk, request_key=key, reference="BATCH-001"
        )
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(first.amount, collection.amount)
        self.assertEqual(workflow.cod_summary(delivery)["state"], "Reconciled")
        with self.assertRaises(ValidationError):
            workflow.reconcile_cod(
                actor=self.admin,
                delivery_id=delivery.pk,
                request_key=uuid.uuid4(),
                reference="again",
            )

    def test_new_history_columns_are_database_immutable(self):
        delivery = self.confirmed()
        assignment = self.assign(delivery)
        with self.assertRaises(DatabaseError), transaction.atomic():
            Assignment.objects.filter(pk=assignment.pk).update(override_reason="erased")
        with self.assertRaises(DatabaseError), transaction.atomic():
            StatusEvent.objects.filter(delivery=delivery).update(previous_status="changed")
        with self.assertRaises(DatabaseError), transaction.atomic():
            DomainEvent.objects.filter(delivery=delivery).delete()

    def test_admin_queue_hides_drafts_and_clients_do_not_see_private_notes(self):
        delivery = self.draft()
        self.client.force_login(self.admin)
        self.assertEqual(
            self.client.get("/admin/deliveries/").context["page_obj"].paginator.count, 0
        )
        self.confirm_without_auto_assignment(delivery)
        self.assign(delivery, override=True, reason="PRIVATE operational reason")
        self.client.force_login(self.customer)
        page = self.client.get(reverse("delivery-detail", args=[delivery.pk]))
        self.assertNotContains(page, "PRIVATE operational reason")
        self.assertNotContains(page, "Audit history")
        self.assertContains(page, "Delivery fee")

    def test_queue_query_count_does_not_grow_per_delivery(self):
        self.confirmed()
        self.client.force_login(self.admin)
        with CaptureQueriesContext(connection) as one:
            self.client.get("/admin/deliveries/")
        for _ in range(9):
            self.confirmed()
        with CaptureQueriesContext(connection) as ten:
            self.client.get("/admin/deliveries/")
        self.assertEqual(len(one), len(ten))
        self.assertLessEqual(len(ten), 6)

    def test_unauthorized_post_routes_and_csrf(self):
        delivery = self.confirmed()
        self.client.force_login(self.customer)
        for name in ("assign-delivery", "reconcile-cod", "collect-cod", "complete-delivery"):
            self.assertEqual(
                self.client.post(reverse(name, args=[delivery.pk]), {}).status_code, 403
            )
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.customer)
        self.assertEqual(strict.post("/client/booking/", self.booking_data()).status_code, 403)

    def test_complete_workflow_through_http_database_state(self):
        self.client.force_login(self.customer)
        booking = self.client.post(reverse("booking"), self.booking_data())
        self.assertEqual(booking.status_code, 302)
        delivery = Delivery.objects.get()
        quote_url = booking["Location"]
        self.assertContains(self.client.get(quote_url), "GHS 20.00")
        self.assertEqual(self.client.post(quote_url).status_code, 302)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, "assigned")
        self.client.force_login(self.admin)
        self.assertContains(
            self.client.get("/admin/deliveries/?status=assigned"), delivery.reference or ""
        )
        current_assignment = delivery.assignments.order_by("-pk").first()
        assert current_assignment is not None
        start = timezone.now() + timedelta(minutes=5)
        response = self.client.post(
            reverse("assign-delivery", args=[delivery.pk]),
            {
                "request_key": uuid.uuid4(),
                "resource": self.bike.pk,
                "starts_at": start.isoformat(),
                "ends_at": (start + timedelta(hours=1)).isoformat(),
                "expected_assignment": current_assignment.pk,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertContains(self.client.get("/admin/calendar/"), delivery.reference or "")
        self.client.force_login(self.rider)
        self.assertContains(self.client.get("/field/dashboard/"), delivery.reference or "")
        for before, target in [
            ("assigned", "to_pickup"),
            ("to_pickup", "picked_up"),
            ("picked_up", "in_transit"),
        ]:
            response = self.client.post(
                reverse("status-action", args=[delivery.pk, target]),
                {"request_key": uuid.uuid4(), "expected_status": before, "target": target},
            )
            self.assertEqual(response.status_code, 302)
        response = self.client.post(
            reverse("collect-cod", args=[delivery.pk]),
            {"request_key": uuid.uuid4(), "amount": "180.00", "method": "cash"},
        )
        self.assertEqual(response.status_code, 302)
        response = self.client.post(
            reverse("complete-delivery", args=[delivery.pk]),
            {
                "request_key": uuid.uuid4(),
                "recipient_name": "Ama Test",
                "note": "Received at front door",
            },
        )
        self.assertEqual(response.status_code, 302)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, "delivered")
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("reconcile-cod", args=[delivery.pk]),
            {"request_key": uuid.uuid4(), "reference": "CASH-001", "note": "Counted"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(workflow.cod_summary(delivery)["state"], "Reconciled")
        self.client.force_login(self.customer)
        page = self.client.get(reverse("delivery-detail", args=[delivery.pk]))
        self.assertContains(page, "Delivered")
        self.assertContains(page, "Received at front door")
        self.assertNotContains(page, "Download delivery photo")
        self.assertContains(page, "Reconciled")
        self.assertEqual(
            list(delivery.history.order_by("created_at").values_list("status", flat=True)),
            [
                "requested",
                "confirmed",
                "assigned",
                "assigned",
                "to_pickup",
                "picked_up",
                "in_transit",
                "delivered",
            ],
        )

    def test_legacy_image_upload_and_download_route_are_unavailable(self):
        delivery = self.underway(payment_method="paid")
        self.client.force_login(self.rider)
        response = self.client.post(
            reverse("complete-delivery", args=[delivery.pk]),
            {"request_key": uuid.uuid4(), "recipient_name": "Ama", "photo": SimpleUploadedFile("legacy.jpg", b"old upload", content_type="image/jpeg")},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get(f"/deliveries/{delivery.pk}/proof/").status_code, 404)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, "in_transit")
        self.assertFalse(ProofOfDelivery.objects.exists())

    def test_status_sequence_resolves_equal_timestamps_and_exception_resume(self):
        instant = timezone.now()
        with (
            patch("django.utils.timezone.now", return_value=instant),
            patch.object(
                StatusEvent._meta.get_field("created_at"), "get_default", return_value=instant
            ),
        ):
            delivery = self.underway(payment_method="paid")
            for _ in range(2):
                workflow.transition_delivery(
                    actor=self.rider,
                    delivery_id=delivery.pk,
                    target="failed",
                    expected_status="in_transit",
                    note="Gate locked",
                    request_key=uuid.uuid4(),
                )
                workflow.transition_delivery(
                    actor=self.admin,
                    delivery_id=delivery.pk,
                    target="resume",
                    expected_status="failed",
                    request_key=uuid.uuid4(),
                )
        events = list(delivery.history.order_by("sequence"))
        self.assertEqual([event.sequence for event in events], list(range(1, len(events) + 1)))
        self.assertEqual(len({event.created_at for event in events}), 1)
        self.assertEqual(events[-1].status, "in_transit")
        self.client.force_login(self.customer)
        response = self.client.get(reverse("delivery-detail", args=[delivery.pk]))
        self.assertEqual(response.context["history_page"][0].sequence, events[-1].sequence)

    def test_completed_booking_remains_in_calendar_without_blocking_dispatch(self):
        delivery = self.underway(payment_method="paid")
        block = Availability.objects.get(delivery=delivery, cancelled_at__isnull=True)
        proof = workflow.complete_delivery(
            actor=self.rider,
            delivery_id=delivery.pk,
            request_key=uuid.uuid4(),
            recipient_name="Ama",
        )
        self.assertEqual(proof.policy_snapshot, {"recipient_name": True})
        block.refresh_from_db()
        self.assertEqual(block.release_reason, "completed")
        self.assertIsNotNone(block.cancelled_at)
        second = self.confirmed()
        self.assign(second, starts_at=block.starts_at, ends_at=block.ends_at)
        self.client.force_login(self.admin)
        response = self.client.get("/admin/calendar/", {"day": block.starts_at.date().isoformat()})
        self.assertContains(response, delivery.reference or "")

    def test_availability_form_duplicate_creates_one_block_and_notification(self):
        self.client.force_login(self.admin)
        start = timezone.now() + timedelta(days=1)
        data = {
            "request_key": uuid.uuid4(),
            "resource": self.bike.pk,
            "kind": "break",
            "starts_at": start.isoformat(),
            "ends_at": (start + timedelta(hours=1)).isoformat(),
            "note": "Lunch",
        }
        for _ in range(2):
            self.assertEqual(self.client.post("/admin/availability/", data).status_code, 302)
        self.assertEqual(Availability.objects.count(), 1)
        self.assertEqual(DomainEvent.objects.filter(kind="availability.created").count(), 1)
        self.assertEqual(Notification.objects.filter(recipient=self.rider).count(), 1)

    def test_admin_can_set_rider_and_driver_duty_states(self):
        self.client.force_login(self.admin)
        for resource in (self.bike, self.car):
            with self.subTest(resource=resource.pk):
                resource.status = "off_duty"
                resource.save(update_fields=["status"])
                response = self.client.post(
                    reverse("resource-duty", args=[resource.pk]), {"duty_status": "on_duty"}
                )
                self.assertEqual(response.status_code, 302)
                resource.refresh_from_db()
                self.assertEqual(resource.status, "available")
                resource.user.refresh_from_db()
                self.assertTrue(resource.user.is_active)
                self.assertTrue(resource.user.operational_access_approved)

                response = self.client.post(
                    reverse("resource-duty", args=[resource.pk]), {"duty_status": "off_duty"}
                )
                self.assertEqual(response.status_code, 302)
                resource.refresh_from_db()
                self.assertEqual(resource.status, "off_duty")

    def test_fleet_shows_current_duty_state_and_action(self):
        self.bike.status = "off_duty"
        self.bike.save(update_fields=["status"])
        self.client.force_login(self.admin)

        response = self.client.get("/admin/fleet/")

        self.assertContains(response, "Duty status")
        self.assertContains(response, "Off Duty")
        self.assertContains(response, "Set On Duty")
        self.assertContains(response, "Set Off Duty")
        self.assertContains(response, "Schedule blocks still apply to their dates and times.")

    def test_duty_status_changes_are_audited(self):
        self.client.force_login(self.admin)
        self.bike.status = "off_duty"
        self.bike.save(update_fields=["status"])

        response = self.client.post(
            reverse("resource-duty", args=[self.bike.pk]), {"duty_status": "on_duty"}
        )

        self.assertEqual(response.status_code, 302)
        event = DomainEvent.objects.get(kind="resource.duty_status_changed")
        self.assertEqual(event.actor, self.admin)
        self.assertEqual(
            event.payload,
            {
                "target_resource_id": str(self.bike.pk),
                "target_user_id": str(self.rider.pk),
                "old_state": "off_duty",
                "new_state": "on_duty",
            },
        )
        self.assertIsNotNone(event.created_at)

    def test_only_admin_post_can_change_duty_status(self):
        url = reverse("resource-duty", args=[self.bike.pk])
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(url).status_code, 405)

        for actor in (self.customer, self.rider, self.driver):
            with self.subTest(actor=actor.pk):
                self.client.force_login(actor)
                self.assertEqual(
                    self.client.post(url, {"duty_status": "on_duty"}).status_code, 403
                )
        self.bike.refresh_from_db()
        self.assertEqual(self.bike.status, "available")

    def test_duty_status_post_keeps_csrf_protection(self):
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.admin)
        page = strict.get("/admin/fleet/")
        self.assertEqual(page.status_code, 200)
        url = reverse("resource-duty", args=[self.bike.pk])
        data = {"duty_status": "on_duty"}
        self.assertEqual(strict.post(url, data).status_code, 403)

        csrf_token = strict.cookies["csrftoken"].value
        response = strict.post(url, data, HTTP_X_CSRFTOKEN=csrf_token)
        self.assertEqual(response.status_code, 302)

    def test_on_duty_preserves_blocks_and_blocks_still_protect_only_their_window(self):
        self.client.force_login(self.admin)
        starts_at = timezone.now() + timedelta(days=1)
        ends_at = starts_at + timedelta(hours=1)
        resources = ((self.bike, "bike"), (self.car, "car"))

        for resource, vehicle in resources:
            with self.subTest(vehicle=vehicle):
                resource.status = "off_duty"
                resource.save(update_fields=["status"])
                self.assertEqual(
                    self.client.post(
                        "/admin/availability/",
                        {
                            "request_key": uuid.uuid4(),
                            "resource": resource.pk,
                            "kind": "off_duty",
                            "starts_at": starts_at.isoformat(),
                            "ends_at": ends_at.isoformat(),
                            "note": "Planned time away",
                        },
                    ).status_code,
                    302,
                )
                block = Availability.objects.get(resource=resource, kind="off_duty")

                self.assertEqual(
                    self.client.post(
                        reverse("resource-duty", args=[resource.pk]),
                        {"duty_status": "on_duty"},
                    ).status_code,
                    302,
                )
                resource.refresh_from_db()
                self.assertEqual(resource.status, "available")
                block.refresh_from_db()
                self.assertIsNone(block.cancelled_at)

                delivery = self.confirmed(vehicle_type=vehicle)
                with self.assertRaises(ValidationError):
                    self.assign(
                        delivery,
                        resource_id=resource.pk,
                        starts_at=block.starts_at + timedelta(minutes=5),
                        ends_at=block.ends_at - timedelta(minutes=5),
                    )

                assignment = self.assign(
                    delivery,
                    resource_id=resource.pk,
                    starts_at=block.ends_at + timedelta(minutes=15),
                    ends_at=block.ends_at + timedelta(hours=1),
                )
                self.assertEqual(assignment.resource_id, resource.pk)
                block.refresh_from_db()
                self.assertIsNone(block.cancelled_at)

    def test_expired_block_does_not_prevent_admin_returning_resource_to_duty(self):
        now = timezone.now()
        block = Availability.objects.create(
            resource=self.bike,
            starts_at=now - timedelta(days=2),
            ends_at=now - timedelta(days=1),
            kind="off_duty",
            note="Completed time away",
        )
        self.bike.status = "off_duty"
        self.bike.save(update_fields=["status"])
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("resource-duty", args=[self.bike.pk]), {"duty_status": "on_duty"}
        )

        self.assertEqual(response.status_code, 302)
        self.bike.refresh_from_db()
        self.assertEqual(self.bike.status, "available")
        block.refresh_from_db()
        self.assertIsNone(block.cancelled_at)
        delivery = self.confirmed()
        assignment = self.assign(delivery)
        self.assertEqual(assignment.resource_id, self.bike.pk)

    def test_driver_car_and_premium_workflow(self):
        start = timezone.now() + timedelta(days=1)
        delivery = self.confirmed(
            vehicle_type="car",
            service="premium",
            payment_method="paid",
            window_start=start,
            window_end=start + timedelta(hours=1),
        )
        with self.assertRaises(ValidationError):
            self.assign(
                delivery,
                resource_id=self.car.pk,
                starts_at=start + timedelta(minutes=10),
                ends_at=start + timedelta(hours=1),
            )
        self.assign(
            delivery, resource_id=self.car.pk, starts_at=start, ends_at=start + timedelta(hours=1)
        )
        for before, after in [
            ("assigned", "to_pickup"),
            ("to_pickup", "picked_up"),
            ("picked_up", "in_transit"),
        ]:
            workflow.transition_delivery(
                actor=self.driver,
                delivery_id=delivery.pk,
                target=after,
                expected_status=before,
                request_key=uuid.uuid4(),
            )
        workflow.complete_delivery(
            actor=self.driver,
            delivery_id=delivery.pk,
            request_key=uuid.uuid4(),
            recipient_name="Ama",
        )
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, "delivered")
        self.assertEqual(delivery.job.current_quote.amount, Decimal("50.00"))

    def test_operational_lists_do_not_query_per_delivery(self):
        self.assign(self.confirmed())

        def counts():
            result = []
            for actor, url in [
                (self.admin, "/admin/finance/"),
                (self.customer, "/client/deliveries/"),
                (self.rider, "/field/dashboard/"),
            ]:
                self.client.force_login(actor)
                with CaptureQueriesContext(connection) as queries:
                    response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                result.append(len(queries))
            return result

        baseline = counts()
        for index in range(1, 5):
            later = self.confirmed()
            start = timezone.now() + timedelta(days=index)
            self.assign(later, starts_at=start, ends_at=start + timedelta(hours=1))
        self.assertEqual(baseline, counts())

    def test_calendar_prefetch_does_not_query_per_person(self):
        self.client.force_login(self.admin)
        self.assign(self.confirmed())
        with CaptureQueriesContext(connection) as before:
            self.client.get("/admin/calendar/")
        for index in range(5):
            user = User.objects.create_user(f"extra-rider-{index}", role="rider")
            resource = Resource.objects.create(
                user=user,
                vehicle_type="bike",
                vehicle_name="Extra bike",
                registration=f"EXTRA-{index}",
                status="available",
            )
            self.assign(self.confirmed(), resource_id=resource.pk)
        with CaptureQueriesContext(connection) as after:
            response = self.client.get("/admin/calendar/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(before), len(after))
