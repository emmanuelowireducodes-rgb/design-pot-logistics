import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, connection, transaction
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from . import workflow
from .booking_forms import BookingForm
from .models import (
    Assignment,
    Availability,
    Delivery,
    DomainEvent,
    LedgerEntry,
    Location,
    Notification,
    Organization,
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
            delivery = self.draft()
        self.assertIsNone(delivery.reference)
        self.assertIsNone(delivery.pickup.latitude)
        self.assertIsNone(delivery.destination.longitude)
        self.assertEqual(delivery.pickup.area, "Osu")
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
        self.assertSetEqual(
            set(Notification.objects.filter(event=event).values_list("recipient_id", flat=True)),
            {self.admin.pk, self.customer.pk},
        )
        self.assertEqual(first.quote_id, quote_id)
        second = self.confirmed()
        self.assertNotEqual(second.reference, delivery.reference or "")

    def test_pricing_changes_do_not_change_accepted_quote(self):
        delivery = self.confirmed()
        original = delivery.job.current_quote
        rules = {
            "bike": {"base": "99.00", "minimum": "99.00", "per_km": "9.00"},
            "car": {"base": "120.00", "minimum": "120.00", "per_km": "12.00"},
            "premium_surcharge": "20.00",
            "rounding": "1.00",
            "quote_minutes": 30,
        }
        workflow.update_pricing(actor=self.admin, rules=rules)
        new_delivery = self.draft()
        self.assertEqual(new_delivery.job.current_quote.amount, Decimal("99.00"))
        original.refresh_from_db()
        self.assertEqual(original.amount, Decimal("20.00"))
        with self.assertRaises(PermissionDenied):
            workflow.update_pricing(actor=self.customer, rules=rules)

    @override_settings(ALLOW_DEVELOPMENT_PRICING=False)
    def test_development_pricing_disabled_requests_admin_quote(self):
        # Phase 2.5 retains the ban on unapproved prices, but saves the request for Admin review.
        delivery = self.draft()
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
        workflow.confirm_quote(actor=self.customer, quote_id=delivery.job.current_quote_id)
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
        self.assertContains(self.client.get(quote_url), "Development quote")
        self.assertEqual(self.client.post(quote_url).status_code, 302)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, "confirmed")
        self.client.force_login(self.admin)
        self.assertContains(
            self.client.get("/admin/deliveries/?status=unassigned"), delivery.reference or ""
        )
        start = timezone.now() + timedelta(minutes=5)
        response = self.client.post(
            reverse("assign-delivery", args=[delivery.pk]),
            {
                "request_key": uuid.uuid4(),
                "resource": self.bike.pk,
                "starts_at": start.isoformat(),
                "ends_at": (start + timedelta(hours=1)).isoformat(),
                "expected_assignment": "",
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
