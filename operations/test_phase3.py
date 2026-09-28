# mypy: disable-error-code="attr-defined"
import tempfile
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from PIL import Image

from . import runs, workflow
from .models import (
    Availability,
    Delivery,
    DeliveryRun,
    DomainEvent,
    Notification,
    OperationalZone,
    Organization,
    Resource,
    RunStop,
    User,
)


def pod_photo():
    import io

    contents = io.BytesIO()
    Image.new("RGB", (32, 32), "green").save(contents, "PNG")
    return SimpleUploadedFile("proof.png", contents.getvalue(), content_type="image/png")


class DeliveryRunTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name="Run client")
        cls.client_user = User.objects.create_user("run-client", role="client", organization=cls.org)
        cls.admin = User.objects.create_user("run-admin", role="admin")
        cls.rider = User.objects.create_user("run-rider", role="rider", first_name="Emmanuel")
        cls.other_rider = User.objects.create_user("run-other", role="rider")
        cls.driver = User.objects.create_user("run-driver", role="driver")
        cls.bike = Resource.objects.create(
            user=cls.rider, vehicle_type="bike", vehicle_name="Bike 7", registration="RUN-BIKE-7", status="available"
        )
        cls.other_bike = Resource.objects.create(
            user=cls.other_rider, vehicle_type="bike", vehicle_name="Bike 8", registration="RUN-BIKE-8", status="available"
        )
        cls.car = Resource.objects.create(
            user=cls.driver, vehicle_type="car", vehicle_name="Van 2", registration="RUN-CAR-2", status="available"
        )
        cls.osu = OperationalZone.objects.create(name="Osu", active=True)
        cls.labone = OperationalZone.objects.create(name="Labone", active=True)
        cls.madina = OperationalZone.objects.create(name="Madina", active=True)

    def setUp(self):
        distance = patch("operations.maps.RoadDistance.road_kilometres", return_value=Decimal("5"))
        distance.start()
        self.addCleanup(distance.stop)
        self.media_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.media_dir.cleanup)
        self.settings_override = override_settings(PRIVATE_MEDIA_ROOT=self.media_dir.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

    def confirmed(self, *, area="Osu", service="standard", vehicle="bike", destination_zone=None, payment="paid"):
        key = uuid.uuid4()
        delivery = workflow.save_booking(actor=self.client_user, data={
            "booking_key": str(key), "revision": 0,
            "pickup_address": "12 Oxford Street", "pickup_area": "Osu",
            "pickup_zone": str(self.osu.pk), "pickup_instructions": "Blue door",
            "destination_address": f"{area} Road", "destination_area": area,
            "destination_zone": str((destination_zone or self.labone).pk),
            "destination_instructions": "Call at gate", "recipient_name": f"Recipient {area}",
            "recipient_phone": "+233200000000", "package_description": "Parcel",
            "instructions": "Keep dry", "vehicle_type": vehicle, "service": service,
            "window_start": (timezone.now() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M") if service == "premium" else "",
            "window_end": (timezone.now() + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M") if service == "premium" else "",
            "payment_method": payment, "cod_amount": "25.00" if payment == "cod" else "0",
        })
        workflow.confirm_quote(actor=self.client_user, quote_id=delivery.job.current_quote_id)
        delivery.refresh_from_db()
        return delivery

    def draft_run(self, deliveries, *, resource=None, day=None):
        return runs.create_run(
            actor=self.admin, resource_id=(resource or self.bike).pk,
            operational_date=day or timezone.localdate(),
            delivery_ids=[delivery.pk for delivery in deliveries], notes="Morning dispatch",
            request_key=uuid.uuid4(),
        )

    def release(self, run):
        runs.release_run(
            actor=self.admin, run_id=run.pk, expected_version=run.version,
            request_key=uuid.uuid4(),
        )
        run.refresh_from_db()
        return run

    def start(self, run):
        return runs.start_run(
            actor=run.resource.user, run_id=run.pk, expected_version=run.version,
            request_key=uuid.uuid4(),
        )

    def test_create_release_and_rider_scope(self):
        delivery = self.confirmed()
        run = self.draft_run([delivery])
        self.assertEqual(run.status, DeliveryRun.Status.DRAFT)
        self.assertEqual(run.stops.count(), 1)
        self.assertEqual(delivery.status, Delivery.Status.CONFIRMED)
        self.assertFalse(Notification.objects.filter(recipient=self.rider, event__kind="run.created").exists())
        with self.assertRaises(ValidationError):
            self.draft_run([delivery], resource=self.other_bike)
        self.release(run)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, Delivery.Status.ASSIGNED)
        self.assertEqual(delivery.assignments.order_by("-id").first().resource_id, self.bike.pk)
        self.assertTrue(Notification.objects.filter(recipient=self.rider, event__kind="run.released").exists())
        self.assertTrue(Availability.objects.filter(resource=self.bike, note=f"run:{run.pk}").exists())
        self.client.force_login(self.other_rider)
        self.assertEqual(self.client.get(f"/field/runs/{run.pk}/").status_code, 404)
        self.client.force_login(self.rider)
        self.assertEqual(self.client.get(f"/field/runs/{run.pk}/").status_code, 200)
        self.assertFalse(self.client.get("/field/deliveries/").wsgi_request.user.is_anonymous)

    def test_vehicle_eligibility_and_schedule_override(self):
        delivery = self.confirmed(vehicle="car")
        with self.assertRaises(ValidationError):
            self.draft_run([delivery])
        run = self.draft_run([delivery], resource=self.car)
        self.car.status = "off_duty"
        self.car.save(update_fields=["status"])
        with self.assertRaises(ValidationError):
            self.release(run)
        runs.release_run(
            actor=self.admin, run_id=run.pk, expected_version=run.version,
            request_key=uuid.uuid4(), override=True, reason="Supervisor approved this run.",
        )
        self.assertEqual(DeliveryRun.objects.get(pk=run.pk).override_reason, "Supervisor approved this run.")

    def test_run_start_next_stop_and_access_are_authorized(self):
        delivery = self.confirmed()
        run = self.draft_run([delivery])
        self.release(run)
        with self.assertRaises(PermissionDenied):
            runs.start_run(actor=self.other_rider, run_id=run.pk, expected_version=run.version, request_key=uuid.uuid4())
        run = self.start(run)
        self.assertEqual(run.status, DeliveryRun.Status.IN_PROGRESS)
        stop = runs.next_stop(run)
        self.assertEqual(stop.delivery_id, delivery.pk)
        self.assertEqual(stop.status, RunStop.Status.STARTED)
        self.assertTrue(DomainEvent.objects.filter(kind="run.started", actor=self.rider).exists())

    def test_ordering_is_persistent_accessible_and_rejects_stale_versions(self):
        deliveries = [self.confirmed(area=area, destination_zone=zone) for area, zone in [
            ("Labone", self.labone), ("Madina", self.madina), ("Osu", self.osu),
        ]]
        run = self.draft_run(deliveries)
        self.release(run)
        run = self.start(run)
        stops = list(run.stops.order_by("sequence"))
        runs.move_stop(actor=self.admin, run_id=run.pk, stop_id=stops[2].pk, direction="up", expected_version=run.version)
        stops = list(run.stops.order_by("sequence"))
        self.assertEqual(stops[1].pk, deliveries[2].run_stops.get().pk)
        with self.assertRaises(ValidationError):
            runs.move_stop(actor=self.admin, run_id=run.pk, stop_id=stops[0].pk, direction="down", expected_version=run.version)
        run.refresh_from_db()
        self.client.force_login(self.admin)
        response = self.client.get(f"/admin/dispatch/runs/{run.pk}/")
        self.assertContains(response, "Move Up")
        self.assertContains(response, "Move Down")
        runs.group_stops_by_area(actor=self.admin, run_id=run.pk, expected_version=run.version)
        ordered_areas = [stop.delivery.destination.zone.name for stop in run.stops.select_related("delivery__destination__zone").filter(status=RunStop.Status.PENDING).order_by("sequence")]
        self.assertEqual(ordered_areas, sorted(ordered_areas))
        self.assertTrue(DomainEvent.objects.filter(kind="run.area_grouped").exists())

    def test_skip_unavailable_retry_and_progression(self):
        first, second = self.confirmed(area="Labone"), self.confirmed(area="Madina")
        run = self.draft_run([first, second])
        self.release(run)
        run = self.start(run)
        first_stop = first.run_stops.get()
        runs.skip_stop(actor=self.rider, run_id=run.pk, stop_id=first_stop.pk,
            expected_version=run.version, note="Customer asked for later", request_key=uuid.uuid4())
        run.refresh_from_db()
        self.assertEqual(runs.next_stop(run).delivery_id, second.pk)
        runs.retry_stop(actor=self.rider, run_id=run.pk, stop_id=first_stop.pk, position="next",
            expected_version=run.version, request_key=uuid.uuid4())
        run.refresh_from_db()
        self.assertEqual(runs.next_stop(run).delivery_id, first.pk)
        workflow.transition_delivery(actor=self.rider, delivery_id=first.pk, target="to_pickup",
            expected_status="assigned", note="", request_key=uuid.uuid4())
        workflow.transition_delivery(actor=self.rider, delivery_id=first.pk, target="unreachable",
            expected_status="to_pickup", note="No answer at the door", request_key=uuid.uuid4())
        first_stop.refresh_from_db()
        self.assertEqual(first_stop.status, RunStop.Status.UNAVAILABLE)
        run.refresh_from_db()
        self.assertEqual(runs.next_stop(run).delivery_id, second.pk)
        self.assertTrue(Notification.objects.filter(recipient=self.admin, event__kind="run.stop_unavailable").exists())
        runs.retry_stop(actor=self.admin, run_id=run.pk, stop_id=first_stop.pk, position="end",
            expected_version=run.version, request_key=uuid.uuid4())
        first.refresh_from_db()
        self.assertEqual(first.status, Delivery.Status.TO_PICKUP)
        first_stop.refresh_from_db()
        self.assertEqual(first_stop.status, RunStop.Status.PENDING)

    def test_out_of_order_delivery_cannot_start_pickup(self):
        first, second = self.confirmed(area="Labone"), self.confirmed(area="Madina")
        run = self.draft_run([first, second])
        self.release(run)
        run = self.start(run)
        with self.assertRaises(ValidationError):
            workflow.transition_delivery(actor=self.rider, delivery_id=second.pk, target="to_pickup",
                expected_status="assigned", request_key=uuid.uuid4())
        second.refresh_from_db()
        self.assertEqual(second.status, Delivery.Status.ASSIGNED)

    def test_pod_completion_finishes_run_and_preserves_delivery_history(self):
        delivery = self.confirmed()
        run = self.draft_run([delivery])
        self.release(run)
        run = self.start(run)
        for before, after in [("assigned", "to_pickup"), ("to_pickup", "picked_up"), ("picked_up", "in_transit")]:
            workflow.transition_delivery(actor=self.rider, delivery_id=delivery.pk, target=after,
                expected_status=before, request_key=uuid.uuid4())
        with self.assertRaises(ValidationError):
            workflow.complete_delivery(actor=self.rider, delivery_id=delivery.pk, request_key=uuid.uuid4(), recipient_name="Ama")
        workflow.complete_delivery(actor=self.rider, delivery_id=delivery.pk, request_key=uuid.uuid4(), recipient_name="Ama", photo=pod_photo())
        run.refresh_from_db()
        self.assertEqual(run.status, DeliveryRun.Status.COMPLETED)
        self.assertEqual(run.stops.get().status, RunStop.Status.COMPLETED)
        self.assertEqual(delivery.history.filter(status=Delivery.Status.DELIVERED).count(), 1)
        self.assertFalse(Availability.objects.filter(resource=self.bike, note=f"run:{run.pk}", cancelled_at__isnull=True).exists())
        self.assertTrue(DomainEvent.objects.filter(kind="run.completed").exists())

    def test_cod_requirements_and_remove_rules_remain_authoritative(self):
        delivery = self.confirmed(payment="cod")
        run = self.draft_run([delivery])
        self.release(run)
        run = self.start(run)
        for before, after in [("assigned", "to_pickup"), ("to_pickup", "picked_up"), ("picked_up", "in_transit")]:
            workflow.transition_delivery(actor=self.rider, delivery_id=delivery.pk, target=after,
                expected_status=before, request_key=uuid.uuid4())
        with self.assertRaises(ValidationError):
            workflow.complete_delivery(actor=self.rider, delivery_id=delivery.pk, request_key=uuid.uuid4(), recipient_name="Ama", photo=pod_photo())
        stop = run.stops.get()
        with self.assertRaises(ValidationError):
            runs.remove_stop(actor=self.admin, run_id=run.pk, stop_id=stop.pk,
                expected_version=run.version, reason="Too late", request_key=uuid.uuid4())

    def test_duplicate_run_assignment_add_remove_cancel_and_audit(self):
        delivery = self.confirmed()
        run = self.draft_run([delivery])
        with self.assertRaises(ValidationError):
            self.draft_run([delivery], resource=self.other_bike)
        stop = run.stops.get()
        runs.remove_stop(actor=self.admin, run_id=run.pk, stop_id=stop.pk,
            expected_version=run.version, reason="Wrong date", request_key=uuid.uuid4())
        delivery2 = self.confirmed(area="Madina")
        runs.add_stop(actor=self.admin, run_id=run.pk, delivery_id=delivery2.pk,
            expected_version=run.version + 1, request_key=uuid.uuid4())
        run.refresh_from_db()
        self.assertEqual(run.stops.filter(status=RunStop.Status.PENDING).count(), 1)
        runs.cancel_run(actor=self.admin, run_id=run.pk, expected_version=run.version,
            request_key=uuid.uuid4(), reason="Dispatch plan changed")
        self.assertEqual(DeliveryRun.objects.get(pk=run.pk).status, DeliveryRun.Status.CANCELLED)
        self.assertTrue(DomainEvent.objects.filter(kind="run.cancelled").exists())

    def test_active_run_adds_stops_at_the_end_and_not_next(self):
        first, second = self.confirmed(area="Labone"), self.confirmed(area="Madina")
        run = self.draft_run([first])
        self.release(run)
        run = self.start(run)
        stop = runs.add_stop(actor=self.admin, run_id=run.pk, delivery_id=second.pk,
            expected_version=run.version, request_key=uuid.uuid4())
        run.refresh_from_db()
        self.assertEqual(stop.sequence, 2)
        self.assertEqual(runs.next_stop(run).delivery_id, first.pk)
        self.assertTrue(Notification.objects.filter(recipient=self.rider, event__kind="run.stop_added").exists())

    def test_admin_board_and_mobile_pages_render_without_n_plus_one(self):
        delivery = self.confirmed(area="Madina")
        run = self.draft_run([delivery])
        self.client.force_login(self.admin)
        with CaptureQueriesContext(connection) as first:
            response = self.client.get("/admin/dispatch/")
        self.assertEqual(response.status_code, 200)
        for _ in range(4):
            other = self.confirmed(area=f"Area {_}")
            self.draft_run([other])
        with CaptureQueriesContext(connection) as many:
            response = self.client.get("/admin/dispatch/")
        self.assertEqual(len(first), len(many))
        self.assertContains(response, "Unassigned confirmed deliveries")
        self.assertContains(self.client.get(f"/admin/dispatch/runs/{run.pk}/"), "Area grouping uses business-defined zones only")

    def test_premium_window_display_and_stale_run_order(self):
        premium = self.confirmed(service="premium", area="Madina")
        run = self.draft_run([premium])
        self.release(run)
        run = self.start(run)
        self.client.force_login(self.admin)
        page = self.client.get(f"/admin/dispatch/runs/{run.pk}/")
        self.assertContains(page, "PREMIUM")
        with self.assertRaises(ValidationError):
            runs.group_stops_by_area(actor=self.admin, run_id=run.pk, expected_version=run.version - 1)
