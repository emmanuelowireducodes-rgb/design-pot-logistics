import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import price_cards, pricing, workflow
from .bulk_views import BulkRows
from .models import (
    Assignment,
    BulkBooking,
    FixedRate,
    Job,
    Notification,
    OperationalZone,
    Organization,
    PriceCard,
    QuoteConfirmation,
    Resource,
    User,
)


class PriceCardBulkAndContactTests(TestCase):
    org: Organization
    other_org: Organization
    client_user: User
    other_client: User
    admin: User
    rider_user: User
    rider: Resource
    driver_user: User
    driver: Resource
    a: OperationalZone
    b: OperationalZone
    c: OperationalZone
    general: PriceCard

    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name="Seller A")
        cls.other_org = Organization.objects.create(name="Seller B")
        cls.client_user = User.objects.create_user(
            "bulk-client", role="client", organization=cls.org
        )
        cls.other_client = User.objects.create_user(
            "other-bulk-client", role="client", organization=cls.other_org
        )
        cls.admin = User.objects.create_user("bulk-admin", role="admin")
        cls.rider_user = User.objects.create_user(
            "bulk-rider", role="rider", phone="+233 24-111-2222"
        )
        cls.rider = Resource.objects.create(
            user=cls.rider_user, vehicle_type="bike", vehicle_name="Bike",
            registration="BULK-BIKE", status="available",
        )
        cls.driver_user = User.objects.create_user(
            "bulk-driver", role="driver", phone="+233201234567"
        )
        cls.driver = Resource.objects.create(
            user=cls.driver_user, vehicle_type="car", vehicle_name="Van",
            registration="BULK-CAR", status="available",
        )
        cls.a = OperationalZone.objects.create(name="Madina")
        cls.b = OperationalZone.objects.create(name="Accra")
        cls.c = OperationalZone.objects.create(name="Tema")
        cls.general = PriceCard.objects.create(name="General", scope_key="general")

    def rate(self, card=None, *, origin=None, destination=None, amount="70.00", vehicle="bike", service="standard", allow_update=False):
        card = card or self.general
        return price_cards.save_route_price(
            actor=self.admin,
            card_id=card.pk,
            origin_id=(origin or self.a).pk,
            destination_id=(destination or self.b).pk,
            bike_price=Decimal(amount) if vehicle == "bike" else None,
            car_price=Decimal(amount) if vehicle == "car" else None,
            bidirectional=True,
            service=service,
            reason="Approved route price",
            allow_update=allow_update,
        )

    def booking(self, *, origin=None, destination=None, vehicle="bike", service="standard", **changes):
        data = {
            "booking_key": str(uuid.uuid4()), "revision": 0,
            "pickup_zone": (origin or self.a).pk,
            "destination_zone": (destination or self.b).pk,
            "pickup_address": "Shop, Madina", "pickup_landmark": "Blue gate",
            "pickup_instructions": "Call first", "pickup_google_maps_url": "",
            "destination_address": "House 4, Accra", "destination_landmark": "",
            "destination_instructions": "Leave at reception", "destination_google_maps_url": "",
            "recipient_name": "Customer", "recipient_phone": "+233200000000",
            "package_description": "Parcel", "instructions": "",
            "vehicle_type": vehicle, "service": service,
            "window_start": "", "window_end": "", "payment_method": "paid", "cod_amount": "0",
        }
        data.update(changes)
        return workflow.save_booking(actor=self.client_user, data=data)

    def test_general_card_instant_quote_does_not_call_distance_provider(self):
        self.rate()
        with patch("operations.maps.RoadDistance.road_kilometres", side_effect=AssertionError):
            delivery = self.booking()
        self.assertEqual(delivery.job.current_quote.amount, Decimal("70.00"))
        self.assertEqual(delivery.job.current_quote.source, "GENERAL_FIXED_RATE")

    def test_client_specific_card_overrides_general_and_isolated_from_other_client(self):
        self.rate()
        contract = price_cards.save_card(
            actor=self.admin, name="Seller A terms", organization=self.org
        )
        self.rate(contract, amount="55.00")
        own = self.booking()
        self.assertEqual(own.job.current_quote.amount, Decimal("55.00"))
        self.assertEqual(own.job.current_quote.source, "CLIENT_CONTRACT_RATE")
        self.client_user = self.other_client
        other = self.booking()
        self.assertEqual(other.job.current_quote.amount, Decimal("70.00"))

    def test_reverse_match_is_canonical_and_reverse_duplicate_is_rejected(self):
        self.rate(origin=self.b, destination=self.a)
        saved = FixedRate.objects.get(card=self.general)
        self.assertLessEqual(saved.origin_id.int, saved.destination_id.int)
        data = {
            "booking_key": str(uuid.uuid4()), "revision": 0,
            "pickup_zone": self.b.pk, "destination_zone": self.a.pk,
            "pickup_address": "One", "destination_address": "Two",
            "recipient_name": "Customer", "recipient_phone": "+233200000000",
            "package_description": "Parcel", "pickup_instructions": "",
            "destination_instructions": "", "instructions": "", "vehicle_type": "bike",
            "service": "standard", "payment_method": "paid", "cod_amount": "0",
        }
        delivery = workflow.save_booking(actor=self.client_user, data=data)
        self.assertEqual(delivery.job.current_quote.amount, Decimal("70.00"))
        with self.assertRaises(ValidationError):
            self.rate(origin=self.a, destination=self.b, amount="80.00")
        self.assertEqual(FixedRate.objects.filter(card=self.general).count(), 1)

    def test_same_area_price_card_is_supported(self):
        self.rate(origin=self.a, destination=self.a, amount="30.00")
        delivery = self.booking(origin=self.a, destination=self.a)
        self.assertEqual(delivery.job.current_quote.amount, Decimal("30.00"))

    def test_no_card_requests_admin_quote_without_road_distance_or_fake_amount(self):
        with patch("operations.maps.RoadDistance.road_kilometres", side_effect=AssertionError):
            delivery = self.booking(destination=self.c)
        self.assertTrue(delivery.job.quote_requested)
        self.assertIsNone(delivery.job.current_quote_id)

    def test_confirmed_quote_remains_frozen_after_card_revision(self):
        self.rate()
        delivery = self.booking()
        quote = delivery.job.current_quote
        workflow.confirm_quote(actor=self.client_user, quote_id=quote.pk)
        self.rate(amount="80.00", allow_update=True)
        quote.refresh_from_db()
        self.assertEqual(quote.amount, Decimal("70.00"))
        future = self.booking()
        self.assertEqual(future.job.current_quote.amount, Decimal("80.00"))

    def bulk_post(self, *, request_key=None):
        return {
            "request_key": str(request_key or uuid.uuid4()),
            "pickup_zone": str(self.a.pk), "pickup_address": "Seller pickup, Madina",
            "pickup_landmark": "Blue gate", "pickup_instructions": "Call seller",
            "pickup_google_maps_url": "", "rows-TOTAL_FORMS": "3", "rows-INITIAL_FORMS": "0",
            "rows-MIN_NUM_FORMS": "1", "rows-MAX_NUM_FORMS": "1000",
            "rows-0-destination_zone": str(self.b.pk), "rows-0-destination_address": "One, Accra",
            "rows-0-recipient_name": "First", "rows-0-recipient_phone": "+233200000001",
            "rows-0-package_description": "First parcel", "rows-0-vehicle_type": "bike",
            "rows-0-service": "standard", "rows-0-payment_method": "paid", "rows-0-cod_amount": "",
            "rows-1-destination_zone": str(self.c.pk), "rows-1-destination_address": "Two, Tema",
            "rows-1-recipient_name": "Second", "rows-1-recipient_phone": "+233200000002",
            "rows-1-package_description": "Second parcel", "rows-1-vehicle_type": "bike",
            "rows-1-service": "standard", "rows-1-payment_method": "paid", "rows-1-cod_amount": "",
        }

    def test_bulk_entry_explains_flow_shared_pickup_and_ten_rows(self):
        self.client.force_login(self.client_user)
        response = self.client.get(reverse("bulk-booking"))
        self.assertEqual(response.status_code, 200)
        for label in (
            "Enter pickup details once",
            "Price Card exists",
            "Dispatch for a quote",
            "Confirm eligible priced deliveries together",
            "Add Another Delivery",
            "Delivery 1",
            "Delivery 2",
            "Delivery 3",
            "Delivery 10",
        ):
            self.assertContains(response, label)
        self.assertContains(response, 'name="pickup_address"')
        self.assertEqual(response.content.decode().count('name="pickup_address"'), 1)
        formset = BulkRows(data={
            "rows-TOTAL_FORMS": "11",
            "rows-INITIAL_FORMS": "0",
            "rows-MIN_NUM_FORMS": "1",
            "rows-MAX_NUM_FORMS": "1000",
        }, prefix="rows")
        self.assertFalse(formset.is_valid())
        self.assertIn("at most 10 forms", str(formset.non_form_errors()))

    def test_bulk_creation_route_remains_client_only(self):
        for user in (self.admin, self.rider_user, self.driver_user):
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse("bulk-booking")).status_code, 403)

    def test_bulk_shared_pickup_partial_pricing_and_independent_confirmation(self):
        self.rate()
        self.client.force_login(self.client_user)
        response = self.client.post(reverse("bulk-booking"), self.bulk_post())
        self.assertEqual(response.status_code, 302)
        batch = BulkBooking.objects.get()
        jobs = list(batch.jobs.order_by("created_at", "pk"))
        deliveries = [job.deliveries.get() for job in jobs]
        self.assertEqual(deliveries[0].pickup_id, deliveries[1].pickup_id)
        self.assertTrue(jobs[0].is_bulk and jobs[1].is_bulk)
        self.assertIsNotNone(jobs[0].current_quote_id)
        self.assertTrue(jobs[1].quote_requested)
        summary = self.client.get(reverse("bulk-booking-detail", args=[batch.pk]))
        self.assertEqual(summary.status_code, 200)
        self.assertContains(summary, "Partial total only")
        self.assertContains(summary, "70.00")
        response = self.client.post(
            reverse("bulk-booking-detail", args=[batch.pk]),
            {"quote_ids": str(jobs[0].current_quote_id), "amount": "0.01", "resource_id": str(self.driver.pk)},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(QuoteConfirmation.objects.filter(job__bulk_booking=batch).count(), 1)
        self.assertEqual(Assignment.objects.filter(delivery=deliveries[0]).count(), 1)
        self.assertFalse(QuoteConfirmation.objects.filter(job=jobs[1]).exists())
        self.assertFalse(Assignment.objects.filter(delivery=deliveries[1]).exists())
        first_quote = jobs[0].current_quote
        self.assertIsNotNone(first_quote)
        assert first_quote is not None
        self.assertEqual(first_quote.amount, Decimal("70.00"))
        self.assertEqual(jobs[1].current_quote_id, None)

    def test_bulk_confirmation_replay_does_not_duplicate_assignment(self):
        self.rate()
        self.client.force_login(self.client_user)
        self.client.post(reverse("bulk-booking"), self.bulk_post())
        batch = BulkBooking.objects.get()
        job = batch.jobs.order_by("created_at", "pk").first()
        assert job is not None
        payload = {"quote_ids": str(job.current_quote_id)}
        url = reverse("bulk-booking-detail", args=[batch.pk])
        self.client.post(url, payload)
        self.client.post(url, payload)
        self.assertEqual(QuoteConfirmation.objects.filter(job=job).count(), 1)
        self.assertEqual(Assignment.objects.filter(delivery__job=job).count(), 1)

    def test_bulk_page_is_scoped_and_rejects_foreign_quote_id(self):
        self.rate()
        self.client.force_login(self.client_user)
        self.client.post(reverse("bulk-booking"), self.bulk_post())
        batch = BulkBooking.objects.get()
        self.client.force_login(self.other_client)
        self.assertEqual(self.client.get(reverse("bulk-booking-detail", args=[batch.pk])).status_code, 404)
        self.client.force_login(self.client_user)
        other_job = Job.objects.create(
            organization=self.other_org, created_by=self.other_client, booking_key=uuid.uuid4()
        )
        from .models import PricingVersion, Quote
        version = PricingVersion.objects.create(label="foreign bulk quote", rules={})
        foreign = Quote.objects.create(
            job=other_job, pricing_version=version, amount=Decimal("1"),
            breakdown={}, expires_at=timezone.now() + timedelta(hours=1),
        )
        response = self.client.post(
            reverse("bulk-booking-detail", args=[batch.pk]), {"quote_ids": str(foreign.pk)}
        )
        self.assertEqual(response.status_code, 403)

    def test_admin_quote_can_save_reusable_route_without_confirming_delivery(self):
        delivery = self.booking(destination=self.c)
        self.assertTrue(delivery.job.quote_requested)
        self.client.force_login(self.admin)
        response = self.client.post(reverse("manual-quote", args=[delivery.pk]), {
            "request_key": str(uuid.uuid4()), "expected_revision": "1", "amount": "62.00",
            "reason": "Approved new route", "save_as_price_card": "on", "card_scope": "general",
        })
        self.assertEqual(response.status_code, 302)
        delivery.refresh_from_db()
        delivery.job.refresh_from_db()
        self.assertEqual(delivery.job.current_quote.amount, Decimal("62.00"))
        self.assertFalse(QuoteConfirmation.objects.filter(job=delivery.job).exists())
        self.assertEqual(PriceCard.objects.filter(scope_key="general").count(), 1)
        self.assertTrue(delivery.job.current_quote.breakdown["distance_km"] is None)
        self.assertTrue(Notification.objects.filter(
            recipient=self.client_user, event__kind="quote.manual_created"
        ).exists())
        future = self.booking(destination=self.c)
        self.assertEqual(future.job.current_quote.amount, Decimal("62.00"))
        self.assertEqual(future.job.current_quote.source, "GENERAL_FIXED_RATE")

    def test_repeat_admin_save_replays_without_duplicate_card_rate_or_quote(self):
        delivery = self.booking(destination=self.c)
        request_key = uuid.uuid4()
        first = pricing.manual_quote(
            actor=self.admin, delivery_id=delivery.pk, amount="62.00",
            reason="Approved new route", expected_revision=1, request_key=request_key,
            save_as_price_card=True, card_scope="general",
        )
        rates_before = FixedRate.objects.filter(card=self.general).count()
        again = pricing.manual_quote(
            actor=self.admin, delivery_id=delivery.pk, amount="62.00",
            reason="Approved new route", expected_revision=1, request_key=request_key,
            save_as_price_card=True, card_scope="general",
        )
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(FixedRate.objects.filter(card=self.general).count(), rates_before)
        self.assertEqual(QuoteConfirmation.objects.filter(job=delivery.job).count(), 0)

    def test_admin_can_save_route_to_client_only_card(self):
        delivery = self.booking(destination=self.c)
        pricing.manual_quote(
            actor=self.admin, delivery_id=delivery.pk, amount="45.00",
            reason="Seller-specific terms", expected_revision=1, request_key=uuid.uuid4(),
            save_as_price_card=True, card_scope="client",
        )
        own_future = self.booking(destination=self.c)
        self.assertEqual(own_future.job.current_quote.amount, Decimal("45.00"))
        self.assertEqual(own_future.job.current_quote.source, "CLIENT_CONTRACT_RATE")
        self.client_user = self.other_client
        other_future = self.booking(destination=self.c)
        self.assertIsNone(other_future.job.current_quote_id)
        self.assertTrue(other_future.job.quote_requested)

    def test_client_sees_current_assigned_phone_but_other_client_does_not(self):
        self.rate()
        delivery = self.booking()
        workflow.confirm_quote(actor=self.client_user, quote_id=delivery.job.current_quote_id)
        self.client.force_login(self.client_user)
        page = self.client.get(reverse("delivery-detail", args=[delivery.pk]))
        self.assertContains(page, "tel:+233241112222")
        self.client.force_login(self.other_client)
        self.assertEqual(self.client.get(reverse("delivery-detail", args=[delivery.pk])).status_code, 404)
        self.assertNotContains(self.client.get(reverse("delivery-queue", args=["client"])), "bulk-rider")

    def test_client_sees_current_assigned_driver_phone_for_car_delivery(self):
        self.rate(vehicle="car")
        delivery = self.booking(vehicle="car")
        workflow.confirm_quote(actor=self.client_user, quote_id=delivery.job.current_quote_id)
        self.client.force_login(self.client_user)
        self.assertContains(
            self.client.get(reverse("delivery-detail", args=[delivery.pk])),
            "tel:+233201234567",
        )

    def test_bulk_admin_quotes_send_one_grouped_quote_ready_notification(self):
        self.rate()
        self.client.force_login(self.client_user)
        self.client.post(reverse("bulk-booking"), self.bulk_post())
        batch = BulkBooking.objects.get()
        pending = batch.jobs.get(quote_requested=True).deliveries.get()
        quote = pricing.manual_quote(
            actor=self.admin,
            delivery_id=pending.pk,
            amount="50.00",
            reason="Dispatch reviewed",
            expected_revision=1,
            request_key=uuid.uuid4(),
        )
        self.assertEqual(quote.source, "ADMIN_MANUAL_QUOTE")
        self.assertEqual(
            Notification.objects.filter(
                recipient=self.client_user, event__kind="bulk.quote_ready"
            ).count(),
            1,
        )
        self.assertFalse(
            Notification.objects.filter(
                recipient=self.client_user, event__kind="quote.manual_created"
            ).exists()
        )
        notification = Notification.objects.get(
            recipient=self.client_user, event__kind="bulk.quote_ready"
        )
        from .notification_destinations import notification_destination

        self.assertEqual(
            notification_destination(notification, self.client_user),
            reverse("bulk-booking-detail", args=[batch.pk]),
        )

    def test_bulk_confirmation_runs_assignment_per_delivery_and_rebalances_workload(self):
        self.rate()
        second_user = User.objects.create_user("bulk-rider-second", role="rider")
        Resource.objects.create(
            user=second_user, vehicle_type="bike", vehicle_name="Second bike",
            registration="BULK-BIKE-SECOND", status="available",
        )
        self.client.force_login(self.client_user)
        payload = self.bulk_post()
        payload.update({
            "rows-1-destination_zone": str(self.b.pk),
            "rows-1-destination_address": "Another stop, Accra",
        })
        self.client.post(reverse("bulk-booking"), payload)
        batch = BulkBooking.objects.get()
        jobs = list(batch.jobs.order_by("created_at", "pk"))
        quote_ids = [str(job.current_quote_id) for job in jobs]
        self.client.post(
            reverse("bulk-booking-detail", args=[batch.pk]), {"quote_ids": quote_ids}
        )
        assignments = list(Assignment.objects.filter(delivery__job__bulk_booking=batch))
        self.assertEqual(len(assignments), 2)
        self.assertEqual(len({assignment.resource_id for assignment in assignments}), 2)
        self.assertEqual(
            QuoteConfirmation.objects.filter(job__bulk_booking=batch).count(), 2
        )

    def test_unassigned_delivery_has_no_courier_contact(self):
        self.rate()
        self.rider.status = "off_duty"
        self.rider.save(update_fields=["status"])
        self.driver.status = "off_duty"
        self.driver.save(update_fields=["status"])
        delivery = self.booking()
        workflow.confirm_quote(actor=self.client_user, quote_id=delivery.job.current_quote_id)
        self.client.force_login(self.client_user)
        page = self.client.get(reverse("delivery-detail", args=[delivery.pk]))
        self.assertNotContains(page, "Call +233")

    def test_reassignment_changes_client_visible_phone(self):
        self.rate()
        delivery = self.booking()
        workflow.confirm_quote(actor=self.client_user, quote_id=delivery.job.current_quote_id)
        previous = workflow.latest_assignment(delivery)
        # This is an explicit Admin override, so use a vehicle-compatible second bike.
        second_user = User.objects.create_user("second-rider", role="rider", phone="+233209876543")
        target = Resource.objects.create(
            user=second_user, vehicle_type="bike", vehicle_name="Bike 2",
            registration="BULK-BIKE-2", status="available",
        )
        workflow.assign_delivery(
            actor=self.admin, delivery_id=delivery.pk, resource_id=target.pk,
            starts_at=timezone.now() + timedelta(hours=1), ends_at=timezone.now() + timedelta(hours=2),
            request_key=uuid.uuid4(), expected_assignment=previous.pk, override=True,
            reason="Dispatch reassignment",
        )
        self.client.force_login(self.client_user)
        page = self.client.get(reverse("delivery-detail", args=[delivery.pk]))
        self.assertContains(page, "tel:+233209876543")
        self.assertNotContains(page, "tel:+233241112222")

    def test_client_manual_quote_confirmation_uses_existing_assignment_lifecycle(self):
        delivery = self.booking(destination=self.c)
        quote = pricing.manual_quote(
            actor=self.admin, delivery_id=delivery.pk, amount="44.00", reason="Reviewed",
            expected_revision=1, request_key=uuid.uuid4(),
        )
        workflow.confirm_quote(actor=self.client_user, quote_id=quote.pk)
        self.assertTrue(QuoteConfirmation.objects.filter(job=delivery.job).exists())
        self.assertEqual(Assignment.objects.filter(delivery=delivery).count(), 1)
