import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any, cast
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, connection, transaction
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from . import locations, maps, price_cards, pricing, workflow
from .models import (
    Delivery,
    DomainEvent,
    FixedRate,
    Location,
    Notification,
    OperationalZone,
    Organization,
    PremiumPricingRule,
    Quote,
    Resource,
    User,
)
from .phase25_forms import RoutePriceForm
from .validators import validate_google_maps_link


class Foundation25Tests(TestCase):
    organization: Organization
    other_organization: Organization
    customer: User
    other_customer: User
    admin: User
    rider: User
    bike: Resource
    origin: OperationalZone
    destination: OperationalZone

    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(name="Contract client")
        cls.other_organization = Organization.objects.create(name="Other client")
        cls.customer = User.objects.create_user(
            "p25client", role="client", organization=cls.organization
        )
        cls.other_customer = User.objects.create_user(
            "p25other", role="client", organization=cls.other_organization
        )
        cls.admin = User.objects.create_user("p25admin", role="admin")
        PremiumPricingRule.objects.create(
            enabled=True,
            method=PremiumPricingRule.Method.PERCENTAGE,
            value=Decimal("20.00"),
        )
        cls.rider = User.objects.create_user("p25rider", role="rider")
        cls.bike = Resource.objects.create(
            user=cls.rider,
            vehicle_type="bike",
            vehicle_name="Bike",
            registration="P25-BIKE",
            status="available",
        )
        cls.origin = locations.save_zone(
            actor=cls.admin,
            name="Zone A",
            active=True,
            boundary=[[-1, 5], [0, 5], [0, 6], [-1, 6], [-1, 5]],
        )
        cls.destination = locations.save_zone(
            actor=cls.admin,
            name="Zone B",
            active=True,
            boundary=[[0, 5], [1, 5], [1, 6], [0, 6], [0, 5]],
        )

    def booking_data(self, **changes):
        data = {
            "booking_key": str(uuid.uuid4()),
            "revision": 0,
            "pickup_address": "Pickup shop",
            "pickup_area": "Locality A",
            "pickup_instructions": "Call first",
            "destination_address": "Destination house",
            "destination_area": "Locality B",
            "destination_instructions": "Gate",
            "recipient_name": "Recipient",
            "recipient_phone": "+233200000000",
            "package_description": "Parcel",
            "instructions": "Keep dry",
            "vehicle_type": "bike",
            "service": "standard",
            "payment_method": "paid",
            "cod_amount": "0",
        }
        data.update(changes)
        return data

    def draft(self, **changes):
        return workflow.save_booking(actor=self.customer, data=self.booking_data(**changes))

    def setUp(self):
        cache.clear()
        road_distance = patch(
            "operations.maps.RoadDistance.road_kilometres", return_value=Decimal("10")
        )
        road_distance.start()
        self.addCleanup(road_distance.stop)

    def resolved(self, **changes):
        data = {
            "pickup_zone": self.origin.pk,
            "destination_zone": self.destination.pk,
        }
        data.update(changes)
        with patch("operations.maps.RoadDistance.road_kilometres", return_value=Decimal("10")):
            return self.draft(**data)

    def confirm_without_auto_assignment(self, quote_id):
        # These tests cover location/pricing behavior and leave dispatch to their own steps.
        statuses = dict(Resource.objects.values_list("pk", "status"))
        Resource.objects.filter(status="available").update(status="off_duty")
        try:
            return workflow.confirm_quote(actor=self.customer, quote_id=quote_id)
        finally:
            for resource_id, status in statuses.items():
                Resource.objects.filter(pk=resource_id).update(status=status)

    def card(self, client=False):
        return price_cards.save_card(
            actor=self.admin,
            name="Client card" if client else "General",
            organization=self.organization if client else None,
        )

    def rate(self, card, **changes):
        data = {
            "origin": self.origin,
            "destination": self.destination,
            "vehicle": "bike",
            "service": "standard",
            "amount": Decimal("75.00"),
            "bidirectional": False,
            "active": True,
            "effective_at": timezone.now() - timedelta(hours=1),
            "expires_at": None,
            "reason": "Agreed commercial terms",
        }
        data.update(changes)
        return price_cards.save_rate(actor=self.admin, card_id=card.pk, **data)

    def mode(self, mode="FIXED_PRICE_CARD", fallback="REQUIRE_ADMIN_QUOTE"):
        return price_cards.change_mode(actor=self.admin, mode=mode, fallback=fallback)

    def test_manifest_worker_and_offline_are_public(self):
        response = self.client.get("/manifest.webmanifest")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["name"], "Design-Pot Logistics")
        self.assertEqual(response.json()["display"], "standalone")
        self.assertEqual(
            {icon["sizes"] for icon in response.json()["icons"]}, {"192x192", "512x512"}
        )
        worker = self.client.get("/service-worker.js")
        self.assertEqual(worker["Service-Worker-Allowed"], "/")
        self.assertContains(worker, "credentials: 'omit'")
        self.assertContains(worker, "request.method !== 'GET'")
        self.assertNotContains(worker, "__ASSETS__")
        self.assertNotContains(worker, "__ICON__")
        self.assertContains(self.client.get("/offline/"), "No delivery changes have been queued")

    def test_operational_and_location_responses_are_no_store(self):
        self.client.force_login(self.customer)
        for url in ("/client/booking/", "/client/deliveries/"):
            response = self.client.get(url)
            self.assertEqual(response["Cache-Control"], "private, no-store")
            self.assertIn("geolocation=()", response["Permissions-Policy"])
            self.assertNotContains(response, "maps.googleapis.com")
            self.assertNotContains(response, "places.googleapis.com")
        self.assertEqual(self.client.post("/locations/confirm/", {}).status_code, 403)

    def test_manual_location_zone_landmark_and_no_fabricated_coordinates_persist(self):
        delivery = self.resolved(
            pickup_landmark="Blue gate", pickup_area="Provider locality is not Zone A"
        )
        self.assertEqual(delivery.pickup.zone_id, self.origin.pk)
        self.assertIsNone(delivery.pickup.latitude)
        self.assertIsNone(delivery.pickup.longitude)
        self.assertEqual(delivery.pickup.landmark, "Blue gate")
        self.assertEqual(delivery.pickup.source, "manual")

    def test_invalid_coordinates_and_tokens_rejected(self):
        for lat, lng in [(91, 0), (0, 181), ("NaN", 0), ("Infinity", 0), ("", 0)]:
            with self.subTest(lat=lat, lng=lng), self.assertRaises(ValidationError):
                locations.coordinates(lat, lng)

    def test_client_zone_must_be_an_active_server_side_choice(self):
        delivery = self.resolved(
            pickup_zone=str(self.destination.pk),
            pricing_source="CLIENT_CONTRACT_RATE",
            amount="0.01",
        )
        self.assertEqual(delivery.pickup.zone_id, self.destination.pk)
        self.assertIsNone(delivery.job.current_quote_id)
        self.assertTrue(delivery.job.quote_requested)
        with self.assertRaises(ValidationError):
            self.draft(pickup_zone="not-a-zone")

    def test_overlapping_zone_boundaries_require_admin_resolution(self):
        locations.save_zone(
            actor=self.admin, name="Overlap", active=True, boundary=self.origin.boundary
        )
        self.assertIsNone(locations.resolve_zone(Decimal("5.5"), Decimal("-.5")))
        self.assertIsNone(locations.resolve_zone(Decimal("0"), Decimal("0")))

    def test_invalid_boundary_and_non_admin_zone_writes_denied(self):
        with self.assertRaises(ValidationError):
            locations.save_zone(
                actor=self.admin,
                name="Invalid",
                active=True,
                boundary=[[0, 0], [1, 1], [2, 2], [0, 0]],
            )
        with self.assertRaises(PermissionDenied):
            locations.save_zone(actor=self.customer, name="Cheap zone", active=True, boundary=[])

    def test_zone_correction_is_append_only_and_audited(self):
        self.rate(self.card(), bidirectional=True)
        delivery = self.resolved()
        old_id = delivery.pickup_id
        self.confirm_without_auto_assignment(delivery.job.current_quote_id)
        quote_id = delivery.job.current_quote_id
        locations.assign_zone(
            actor=self.admin,
            delivery_id=delivery.pk,
            side="pickup",
            zone_id=self.destination.pk,
            reason="Dispatch verified boundary exception",
        )
        delivery.refresh_from_db()
        self.assertNotEqual(delivery.pickup_id, old_id)
        self.assertEqual(Location.objects.get(pk=old_id).zone_id, self.origin.pk)
        self.assertEqual(delivery.job.current_quote_id, quote_id)
        self.assertTrue(
            DomainEvent.objects.filter(kind="location.zone_corrected", delivery=delivery).exists()
        )

    def test_manual_location_saves_link_zone_and_written_fields_without_coordinates(self):
        link = "https://maps.app.goo.gl/ExampleShareCode"
        delivery = self.draft(
            pickup_zone=self.origin.pk,
            destination_zone=self.destination.pk,
            pickup_google_maps_url=link,
            pickup_address="House 14, Oxford Street",
            pickup_area="Osu",
            pickup_landmark="Blue pharmacy",
            pickup_instructions="Call when outside",
        )
        delivery.refresh_from_db()
        self.assertEqual(delivery.pickup.google_maps_url, link)
        self.assertEqual(delivery.pickup.zone_id, self.origin.pk)
        self.assertEqual(delivery.pickup.display_address, "House 14, Oxford Street")
        self.assertEqual(delivery.pickup.landmark, "Blue pharmacy")
        self.assertEqual(delivery.pickup.instructions, "Call when outside")
        self.assertIsNone(delivery.pickup.latitude)
        self.assertIsNone(delivery.pickup.longitude)
        self.assertEqual(delivery.destination.google_maps_url, "")

    def test_http_booking_accepts_written_location_without_link_or_coordinates(self):
        self.client.force_login(self.customer)
        with patch("operations.maps.RoadDistance.road_kilometres", return_value=None):
            response = self.client.post(
                reverse("booking"),
                self.booking_data(
                    pickup_zone=self.origin.pk,
                    destination_zone=self.destination.pk,
                    pickup_address="Apartment 4, Ring Road",
                    pickup_area="Labone",
                    pickup_google_maps_url="",
                ),
            )
        self.assertRedirects(response, reverse("price-pending", args=[Delivery.objects.get().pk]))
        delivery = Delivery.objects.get()
        self.assertEqual(delivery.pickup.display_address, "Apartment 4, Ring Road")
        self.assertEqual(delivery.pickup.zone_id, self.origin.pk)
        self.assertEqual(delivery.pickup.area, self.origin.name)
        self.assertEqual(delivery.pickup.google_maps_url, "")
        self.assertIsNone(delivery.pickup.latitude)

    def test_google_maps_share_link_formats_and_unsafe_urls(self):
        accepted = (
            "https://www.google.com/maps/place/Osu/",
            "https://maps.google.com/?q=Osu%2C+Accra",
            "https://maps.app.goo.gl/ExampleCode",
            "https://goo.gl/maps/ExampleCode",
        )
        for link in accepted:
            with self.subTest(link=link):
                validate_google_maps_link(link)
        rejected = (
            "javascript:alert(1)",
            "data:text/html,evil",
            "https://maps.app.goo.gl.evil.example/abc",
            "https://google.com.evil.example/maps/place/Osu",
            "https://user:pass@google.com/maps/place/Osu",
            "https://example.com/maps/place/Osu",
        )
        for link in rejected:
            with self.subTest(link=link), self.assertRaises(ValidationError):
                validate_google_maps_link(link)

    def test_manual_booking_form_rejects_unsafe_maps_scheme(self):
        from .booking_forms import BookingForm

        form = BookingForm(self.booking_data(pickup_google_maps_url="javascript:alert(1)"))
        self.assertFalse(form.is_valid())
        self.assertIn("pickup_google_maps_url", form.errors)

    def test_no_road_provider_never_quotes_fabricated_distance(self):
        with patch("operations.maps.RoadDistance.road_kilometres", return_value=None):
            delivery = self.draft()
        self.assertIsNone(delivery.job.current_quote_id)
        self.assertTrue(delivery.job.quote_requested)

    def test_road_route_is_cached_directionally(self):
        provider = Mock()
        provider.route.return_value = maps.RoadRoute(10000, 1200, "polyline")
        first = maps.road_route((5, -0.5), (5, 0.5), provider=provider)
        second = maps.road_route((5, -0.5), (5, 0.5), provider=provider)
        self.assertEqual(first, second)
        self.assertEqual(provider.route.call_count, 1)
        maps.road_route((5, 0.5), (5, -0.5), provider=provider)
        self.assertEqual(provider.route.call_count, 2)

    def test_route_provider_failure_requests_admin_quote(self):
        with patch(
            "operations.maps.RoadDistance.road_kilometres",
            side_effect=maps.MapUnavailable("Route unavailable"),
        ):
            delivery = self.draft(
                pickup_token=locations.signed_location(
                    self.customer, {"latitude": 5.5, "longitude": -0.5}
                ),
                destination_token=locations.signed_location(
                    self.customer, {"latitude": 5.5, "longitude": 0.5}
                ),
            )
        self.assertIsNone(delivery.job.current_quote_id)
        self.assertTrue(delivery.job.quote_requested)

    def test_client_rate_takes_priority_over_general(self):
        self.mode()
        self.rate(self.card(), amount=Decimal("70"))
        contract = self.rate(self.card(client=True), amount=Decimal("55"))
        delivery = self.resolved()
        quote = delivery.job.current_quote
        self.assertEqual(quote.amount, Decimal("55"))
        self.assertEqual(quote.source, "CLIENT_CONTRACT_RATE")
        self.assertEqual(quote.breakdown["rate_id"], str(contract.pk))

    def test_contract_applies_even_when_general_is_distance(self):
        self.rate(self.card(client=True), amount=Decimal("55"))
        quote = self.resolved().job.current_quote
        self.assertEqual(quote.source, "CLIENT_CONTRACT_RATE")

    def test_nonmatching_client_contract_uses_general_fixed(self):
        self.mode()
        self.rate(self.card(), amount=Decimal("70"))
        self.rate(self.card(client=True), vehicle="car", amount=Decimal("55"))
        quote = self.resolved().job.current_quote
        self.assertEqual(quote.source, "GENERAL_FIXED_RATE")
        self.assertEqual(quote.amount, Decimal("70"))

    def test_price_card_pricing_never_calls_road_distance(self):
        with patch(
            "operations.maps.RoadDistance.road_kilometres", return_value=Decimal("12.4")
        ) as road:
            delivery = self.draft()
        self.assertEqual(road.call_count, 0)
        self.assertIsNone(delivery.job.current_quote_id)
        self.assertTrue(delivery.job.quote_requested)

    def test_missing_fixed_rate_never_falls_back_to_distance(self):
        self.mode(fallback="FALLBACK_TO_DISTANCE_PRICING")
        delivery = self.resolved()
        self.assertIsNone(delivery.job.current_quote_id)
        self.assertTrue(delivery.job.quote_requested)

    def test_missing_fixed_rate_requests_quote_without_amount(self):
        self.mode()
        self.client.force_login(self.customer)
        response = self.client.post(reverse("booking"), self.booking_data())
        delivery = Delivery.objects.get()
        self.assertRedirects(response, reverse("price-pending", args=[delivery.pk]))
        self.assertEqual(Quote.objects.count(), 0)
        self.assertTrue(delivery.job.quote_requested)
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.admin, event__kind="quote.review_requested"
            ).exists()
        )

    def test_inactive_future_expired_rates_are_ignored(self):
        self.mode()
        card = self.card()
        now = timezone.now()
        cases: list[dict] = [
            {"active": False},
            {"effective_at": now + timedelta(days=1)},
            {"effective_at": now - timedelta(days=2), "expires_at": now - timedelta(days=1)},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                rate = self.rate(card, **changes)
                self.assertIsNone(self.resolved().job.current_quote_id)
                self.rate(card, rate_key=rate.rate_key, expected_version=1, active=False)

    def test_vehicle_and_service_specific_rates(self):
        self.mode()
        card = self.card()
        self.rate(card, vehicle="bike", amount=Decimal("75"))
        self.rate(card, vehicle="car", amount=Decimal("100"))
        self.assertEqual(self.resolved().job.current_quote.amount, Decimal("75"))
        self.assertEqual(self.resolved(vehicle_type="car").job.current_quote.amount, Decimal("100"))
        now = timezone.now()
        self.assertEqual(
            self.resolved(
                service="premium",
                window_start=now + timedelta(days=1),
                window_end=now + timedelta(days=1, hours=2),
            ).job.current_quote.amount,
            Decimal("90"),
        )

    def test_directional_and_bidirectional_rates(self):
        self.mode()
        card = self.card()
        rate = self.rate(card)
        reverse_tokens = {"pickup_zone": self.destination.pk, "destination_zone": self.origin.pk}
        self.assertIsNone(self.resolved(**reverse_tokens).job.current_quote_id)
        self.rate(card, rate_key=rate.rate_key, expected_version=1, bidirectional=True)
        self.assertEqual(self.resolved(**reverse_tokens).job.current_quote.amount, Decimal("75"))

    def test_rate_overlap_and_stale_edits_rejected(self):
        card = self.card()
        rate = self.rate(card, bidirectional=True)
        with self.assertRaises(ValidationError):
            self.rate(card, origin=self.destination, destination=self.origin)
        self.rate(card, rate_key=rate.rate_key, expected_version=1, amount=Decimal("80"))
        with self.assertRaises(ValidationError):
            self.rate(card, rate_key=rate.rate_key, expected_version=1, amount=Decimal("85"))

    def test_confirmed_quote_unchanged_after_rate_and_mode_changes(self):
        self.mode()
        card = self.card()
        rate = self.rate(card)
        delivery = self.resolved()
        quote = delivery.job.current_quote
        original = dict(quote.breakdown)
        self.confirm_without_auto_assignment(quote.pk)
        self.rate(card, rate_key=rate.rate_key, expected_version=1, amount=Decimal("95"))
        self.mode("DISTANCE_BASED")
        quote.refresh_from_db()
        self.assertEqual(quote.amount, Decimal("75"))
        self.assertEqual(quote.breakdown, original)
        self.assertEqual(FixedRate.objects.filter(rate_key=rate.rate_key).count(), 2)

    def test_mode_and_price_card_changes_are_audited(self):
        self.mode()
        card = self.card()
        self.rate(card)
        for kind in ("pricing.mode_changed", "pricing.card_saved", "pricing.rate_versioned"):
            self.assertTrue(DomainEvent.objects.filter(kind=kind, actor=self.admin).exists())

    def test_manual_quote_review_confirmation_and_duplicate(self):
        self.mode()
        delivery = self.draft()
        key = uuid.uuid4()
        quote = pricing.manual_quote(
            actor=self.admin,
            delivery_id=delivery.pk,
            amount="88.00",
            reason="Agreed by dispatch",
            expected_revision=1,
            request_key=key,
        )
        again = pricing.manual_quote(
            actor=self.admin,
            delivery_id=delivery.pk,
            amount="88.00",
            reason="Agreed by dispatch",
            expected_revision=1,
            request_key=key,
        )
        self.assertEqual(quote.pk, again.pk)
        self.assertEqual(quote.source, "ADMIN_MANUAL_QUOTE")
        self.assertEqual(quote.breakdown["admin_actor"], self.admin.pk)
        self.confirm_without_auto_assignment(quote.pk)
        with self.assertRaises(ValidationError):
            pricing.manual_quote(
                actor=self.admin,
                delivery_id=delivery.pk,
                amount="90.00",
                reason="Change",
                expected_revision=2,
                request_key=uuid.uuid4(),
            )

    def test_pending_reprice_cannot_confirm_old_quote(self):
        self.rate(self.card(), bidirectional=True)
        delivery = self.resolved()
        old_quote = delivery.job.current_quote
        self.mode()
        workflow.save_booking(
            actor=self.customer, delivery_id=delivery.pk,
            data=self.booking_data(revision=1, pickup_zone=self.origin.pk,
                                   destination_zone=self.destination.pk),
        )
        with self.assertRaises(ValidationError):
            workflow.confirm_quote(actor=self.customer, quote_id=old_quote.pk)
        self.client.force_login(self.customer)
        self.assertContains(
            self.client.get(reverse("quote-review", args=[old_quote.pk])),
            "This quote was replaced",
        )

    def test_rate_history_is_immutable_in_database(self):
        rate = self.rate(self.card())
        with self.assertRaises(DatabaseError), transaction.atomic():
            FixedRate.objects.filter(pk=rate.pk).update(amount=1)
        with self.assertRaises(DatabaseError), transaction.atomic():
            FixedRate.objects.filter(pk=rate.pk).delete()

    def test_pricing_management_authorization(self):
        delivery = self.draft()
        for actor in (self.customer, self.rider):
            self.client.force_login(actor)
            for url in (
                "/admin/pricing/",
                "/admin/pricing/cards/new/",
                "/admin/zones/new/",
                f"/admin/pricing/requests/{delivery.pk}/",
            ):
                self.assertEqual(self.client.post(url, {}).status_code, 403)
            with self.assertRaises(PermissionDenied):
                pricing.manual_quote(
                    actor=actor,
                    delivery_id=delivery.pk,
                    amount=1,
                    reason="No",
                    expected_revision=1,
                    request_key=uuid.uuid4(),
                )

    def test_new_screens_render_and_client_internal_terms_hidden(self):
        self.client.force_login(self.admin)
        card = self.card()
        rate = self.rate(card)
        for url in (
            "/admin/pricing/",
            "/admin/pricing/cards/",
            f"/admin/pricing/cards/{card.pk}/",
            f"/admin/pricing/cards/{card.pk}/rates/{rate.rate_key}/",
            "/admin/pricing/history/",
            "/admin/zones/",
            "/admin/pricing/requests/",
        ):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)
        self.mode()
        delivery = self.resolved()
        self.client.force_login(self.customer)
        page = self.client.get(reverse("quote-review", args=[delivery.job.current_quote_id]))
        self.assertNotContains(page, "GENERAL_FIXED_RATE")
        self.assertNotContains(page, "Version")
        self.assertNotContains(page, "Development quote")

    def test_map_and_route_access_are_scoped(self):
        delivery = self.resolved()
        self.client.force_login(self.other_customer)
        self.assertEqual(self.client.get(f"/deliveries/{delivery.pk}/map/").status_code, 404)
        self.client.force_login(self.customer)
        response = self.client.get(f"/deliveries/{delivery.pk}/map/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pickup shop")
        self.assertNotContains(response, "data-delivery-map")
        self.assertEqual(self.client.post(f"/deliveries/{delivery.pk}/road-route/").status_code, 404)

    def test_price_card_lists_avoid_n_plus_one(self):
        card = self.card()
        self.rate(card)
        self.client.force_login(self.admin)
        url = f"/admin/pricing/cards/{card.pk}/"
        with CaptureQueriesContext(connection) as first:
            self.client.get(url)
        for number in range(5):
            zone = OperationalZone.objects.create(name=f"Other {number}")
            self.rate(card, destination=zone)
        with CaptureQueriesContext(connection) as many:
            self.client.get(url)
        self.assertEqual(len(first), len(many))

    def test_simple_route_price_creates_bike_and_car_return_rates(self):
        card = self.card()
        price_cards.save_route_price(
            actor=self.admin, card_id=card.pk, origin_id=self.origin.pk,
            destination_id=self.destination.pk, bike_price=Decimal("70.00"),
            car_price=Decimal("120.00"), bidirectional=True,
        )
        rates = price_cards.current_rates().filter(card=card, active=True)
        self.assertEqual(set(rates.values_list("vehicle", "amount")), {
            ("bike", Decimal("70.00")), ("car", Decimal("120.00")),
        })
        self.assertTrue(all(rate.bidirectional and rate.service == "standard" for rate in rates))

    def test_route_price_form_uses_active_named_zones_and_requires_a_price(self):
        form = RoutePriceForm({"origin": self.origin.pk, "destination": self.destination.pk})
        self.assertFalse(form.is_valid())
        self.assertIn("Enter a Bike price", str(form.errors))
        self.assertEqual(
            list(cast(Any, form.fields["origin"]).queryset), [self.origin, self.destination]
        )

    def test_simplified_card_screens_render_price_columns_and_advanced_options(self):
        self.client.force_login(self.admin)
        card = self.card()
        self.assertEqual(self.client.get(reverse("route-price-create", args=[card.pk])).status_code, 200)
        form_page = self.client.get(reverse("route-price-create", args=[card.pk]))
        self.assertContains(form_page, "From")
        self.assertContains(form_page, "Bike price")
        self.assertContains(form_page, "Same price for return direction")
        self.assertContains(form_page, "Advanced Options")
        self.assertContains(self.client.get(reverse("card-rates", args=[card.pk])), "+ Add Route Price")

    @override_settings(ALLOW_DEVELOPMENT_PRICING=False)
    def test_approved_distance_configuration_is_not_used_for_client_quote(self):
        rules = pricing.active_version().rules
        version = workflow.update_pricing(actor=self.admin, rules=rules, is_sample=False)
        self.assertFalse(version.is_sample)
        delivery = self.resolved()
        self.assertIsNone(delivery.job.current_quote_id)
        self.assertTrue(delivery.job.quote_requested)
        self.assertTrue(
            DomainEvent.objects.filter(kind="pricing.distance_approved", actor=self.admin).exists()
        )

    def test_approved_distance_missing_coordinates_requires_admin(self):
        workflow.update_pricing(
            actor=self.admin, rules=pricing.active_version().rules, is_sample=False
        )
        with patch("operations.maps.RoadDistance.road_kilometres", return_value=None):
            delivery = self.draft()
        self.assertTrue(delivery.job.quote_requested)
        self.assertIsNone(delivery.job.current_quote_id)

    def test_inactive_card_never_applies(self):
        self.mode()
        card = self.card()
        self.rate(card)
        price_cards.save_card(actor=self.admin, card_id=card.pk, name=card.name, active=False)
        self.assertIsNone(self.resolved().job.current_quote_id)

    def test_manual_quote_http_and_client_confirmation(self):
        self.mode()
        delivery = self.draft()
        self.client.force_login(self.admin)
        url = reverse("manual-quote", args=[delivery.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.post(
            url,
            {
                "request_key": uuid.uuid4(),
                "expected_revision": 1,
                "amount": "64.00",
                "reason": "Approved special delivery",
            },
        )
        self.assertRedirects(response, reverse("pending-quotes"))
        delivery.refresh_from_db()
        quote = delivery.job.current_quote
        self.client.force_login(self.customer)
        self.assertContains(
            self.client.get(reverse("price-pending", args=[delivery.pk]), follow=True), "64.00"
        )
        self.assertEqual(
            self.client.post(reverse("quote-review", args=[quote.pk])).status_code, 302
        )
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, "assigned")
        self.assertEqual(
            delivery.assignments.order_by("-pk").first().resource.vehicle_type, "bike"
        )

    def test_manual_booking_has_no_google_api_runtime_or_billing_requirement(self):
        self.client.force_login(self.customer)
        response = self.client.get("/client/booking/")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Design-Pot pickup area / zone")
        self.assertNotContains(response, "Pickup area")
        self.assertNotContains(response, "Destination area")
        self.assertContains(response, "Google Maps location link")
        self.assertNotContains(response, "maps.googleapis.com")
        self.assertNotContains(response, "places.googleapis.com")
        self.assertNotIn("googleapis.com", response["Content-Security-Policy"])

    def test_rider_next_stop_opens_client_link_and_no_link_has_no_button(self):
        self.mode()
        self.rate(self.card())
        link = "https://maps.app.goo.gl/RiderStop"
        delivery = self.draft(
            pickup_zone=self.origin.pk,
            destination_zone=self.destination.pk,
            pickup_google_maps_url=link,
            pickup_address="12 Oxford Street",
            pickup_landmark="Blue pharmacy",
            pickup_instructions="Call at the gate",
        )
        self.confirm_without_auto_assignment(delivery.job.current_quote_id)
        start = timezone.now() + timedelta(minutes=1)
        workflow.assign_delivery(
            actor=self.admin,
            delivery_id=delivery.pk,
            resource_id=self.bike.pk,
            starts_at=start,
            ends_at=start + timedelta(hours=1),
            request_key=uuid.uuid4(),
        )
        self.client.force_login(self.rider)
        response = self.client.get(f"/deliveries/{delivery.pk}/map/")
        self.assertContains(response, "Navigate in Google Maps")
        self.assertContains(response, 'href="https://maps.app.goo.gl/RiderStop"')
        self.assertContains(response, 'rel="noopener noreferrer"')
        self.assertContains(response, "Blue pharmacy")
        self.assertContains(response, "Call at the gate")
        no_link_delivery = self.draft(
            pickup_zone=self.origin.pk,
            destination_zone=self.destination.pk,
            pickup_address="No link pickup",
        )
        self.confirm_without_auto_assignment(no_link_delivery.job.current_quote_id)
        later = start + timedelta(hours=2)
        workflow.assign_delivery(
            actor=self.admin,
            delivery_id=no_link_delivery.pk,
            resource_id=self.bike.pk,
            starts_at=later,
            ends_at=later + timedelta(hours=1),
            request_key=uuid.uuid4(),
        )
        self.assertNotContains(
            self.client.get(f"/deliveries/{no_link_delivery.pk}/map/"), "Navigate in Google Maps"
        )

    def test_zone_fixed_general_and_client_rates_need_no_route_or_coordinates(self):
        self.mode()
        general = self.rate(self.card(), amount=Decimal("70"))
        with patch("operations.maps.RoadDistance.road_kilometres", return_value=None):
            general_delivery = self.draft(
                pickup_zone=self.origin.pk,
                destination_zone=self.destination.pk,
                pickup_address="Written pickup",
                destination_address="Written destination",
            )
        self.assertEqual(general_delivery.job.current_quote.amount, general.amount)
        self.assertEqual(general_delivery.job.current_quote.source, "GENERAL_FIXED_RATE")
        self.assertIsNone(general_delivery.pickup.latitude)

        contract = self.rate(self.card(client=True), amount=Decimal("55"))
        with patch("operations.maps.RoadDistance.road_kilometres", return_value=None):
            contract_delivery = self.draft(
                pickup_zone=self.origin.pk,
                destination_zone=self.destination.pk,
                pickup_address="Another written pickup",
                destination_address="Another written destination",
            )
        self.assertEqual(contract_delivery.job.current_quote.amount, contract.amount)
        self.assertEqual(contract_delivery.job.current_quote.source, "CLIENT_CONTRACT_RATE")

    def test_route_abstraction_is_dormant_without_provider(self):
        with self.assertRaises(maps.MapUnavailable):
            maps.road_route((5.5, -0.2), (5.6, -0.1))

    def test_fixed_prices_do_not_require_development_pricing_opt_in(self):
        self.mode()
        self.rate(self.card())
        with override_settings(ALLOW_DEVELOPMENT_PRICING=False):
            self.assertEqual(self.resolved().job.current_quote.amount, Decimal("75"))
