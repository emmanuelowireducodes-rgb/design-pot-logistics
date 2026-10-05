import uuid
from io import StringIO
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import TestCase

from . import price_cards
from .general_price_handoff import import_general_dpot
from .models import Delivery, FixedRate, OperationalZone, Organization, PriceCard, User


class GeneralPriceHandoffTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user("general-handoff-admin", role="admin")
        cls.organization = Organization.objects.create(name="General fallback test client")
        cls.client_user = User.objects.create_user(
            "general-handoff-client", role="client", organization=cls.organization
        )
        cls.import_result = import_general_dpot(actor=cls.admin)
        cls.origin = OperationalZone.objects.get(name="East Legon")

    def booking_data(self, destination, vehicle="bike"):
        zone = OperationalZone.objects.get(name=destination)
        return {
            "booking_key": str(uuid.uuid4()),
            "revision": "0",
            "pickup_zone": str(self.origin.pk),
            "destination_zone": str(zone.pk),
            "pickup_address": "Trinity Avenue, East Legon",
            "destination_address": f"Delivery address, {destination}",
            "recipient_name": "Test recipient",
            "recipient_phone": "+233200000001",
            "package_description": "Test package",
            "vehicle_type": vehicle,
            "service": "standard",
            "payment_method": "paid",
            "cod_amount": "",
        }

    def post_booking(self, destination, vehicle="bike"):
        self.client.force_login(self.client_user)
        return self.client.post("/client/booking/", self.booking_data(destination, vehicle), follow=True)

    def test_import_creates_exact_general_card_and_is_idempotent(self):
        card = PriceCard.objects.get(scope_key="general")
        self.assertIsNone(card.organization_id)
        self.assertEqual(self.import_result["bike_created"], 114)
        self.assertEqual(self.import_result["car_created"], 79)
        repeated = import_general_dpot(actor=self.admin)
        self.assertEqual(repeated["bike_created"], 0)
        self.assertEqual(repeated["car_created"], 0)
        self.assertEqual(repeated["rates_skipped"], 193)

    def test_production_command_defaults_to_read_only_preview(self):
        before = (PriceCard.objects.count(), OperationalZone.objects.count(), FixedRate.objects.count())
        output = StringIO()
        call_command("import_general_dpot_prices", stdout=output)
        after = (PriceCard.objects.count(), OperationalZone.objects.count(), FixedRate.objects.count())
        self.assertEqual(after, before)
        self.assertIn("Database engine: sqlite", output.getvalue())
        self.assertRegex(output.getvalue(), r"DEBUG: (ON|OFF)")
        self.assertIn("General PriceCard found: YES", output.getvalue())
        self.assertIn("General Bike rates already identical: 114", output.getvalue())
        self.assertIn("General Car rates already identical: 79", output.getvalue())
        self.assertIn("Conflicting rates: 0", output.getvalue())
        self.assertIn("Mode: PREVIEW ONLY; no writes made.", output.getvalue())

    def test_production_command_refuses_apply_on_local_sqlite(self):
        before = FixedRate.objects.count()
        with self.assertRaises(CommandError):
            call_command("import_general_dpot_prices", apply=True, actor=self.admin.username)
        self.assertEqual(FixedRate.objects.count(), before)

    @patch("operations.management.commands.import_general_dpot_prices.connection.vendor", "postgresql")
    def test_production_command_refuses_apply_when_debug_is_enabled(self):
        from django.test import override_settings

        with override_settings(PRODUCTION=True, DEBUG=True):
            with self.assertRaises(CommandError):
                call_command("import_general_dpot_prices", apply=True, actor=self.admin.username)

    def test_general_price_card_page_renders_prices_without_bulk_links(self):
        self.client.force_login(self.admin)
        url = f"/admin/pricing/cards/{PriceCard.objects.get(scope_key='general').pk}/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Manage Areas")
        self.assertContains(response, "Card settings")
        self.assertContains(response, "+ Add Route Price")
        self.assertNotContains(response, "Import Prices")
        self.assertNotContains(response, "Bulk Edit Prices")
        self.assertNotContains(response, "price-import")
        self.assertNotContains(response, "price-editor")

        for destination, bike, car in (
            ("Madina", "40", "70"),
            ("Osu", "50", "90"),
            ("Tema Community 18", "100", None),
            ("Tema Community 25", "100", None),
            ("Afienya", "100", None),
            ("Kasoa", "100", None),
        ):
            with self.subTest(destination=destination):
                route = self.client.get(url, {"q": destination})
                self.assertEqual(route.status_code, 200)
                self.assertContains(route, destination)
                self.assertContains(route, f"GHS {bike}.00")
                if car is None:
                    self.assertContains(route, "—")
                    self.assertNotContains(route, "GHS 0.00")
                else:
                    self.assertContains(route, f"GHS {car}.00")

    def test_general_rate_is_used_without_client_card(self):
        response = self.post_booking("Madina", "bike")
        delivery = Delivery.objects.get(destination__display_address="Delivery address, Madina")
        self.assertEqual(delivery.job.current_quote.amount, 40)
        self.assertEqual(delivery.job.current_quote.source, "GENERAL_FIXED_RATE")
        self.assertContains(response, "GHS 40.00")

    def test_client_card_overrides_general_and_missing_car_needs_admin_quote(self):
        card = price_cards.save_card(
            actor=self.admin, name="Negotiated test client terms", organization=self.organization
        )
        madina = OperationalZone.objects.get(name="Madina")
        price_cards.save_route_price(
            actor=self.admin, card_id=card.pk, origin_id=self.origin.pk,
            destination_id=madina.pk, bike_price=25, bidirectional=False,
            reason="Test negotiated client rate",
        )
        self.post_booking("Madina", "bike")
        override = Delivery.objects.get(destination__display_address="Delivery address, Madina")
        self.assertEqual(override.job.current_quote.amount, 25)
        self.assertEqual(override.job.current_quote.source, "CLIENT_CONTRACT_RATE")

        response = self.post_booking("Tema Community 18", "car")
        unpriced = Delivery.objects.get(
            destination__display_address="Delivery address, Tema Community 18", vehicle_type="car"
        )
        self.assertIsNone(unpriced.job.current_quote_id)
        self.assertTrue(unpriced.job.quote_requested)
        self.assertContains(response, "Price confirmation required")
        self.assertNotContains(response, "GHS 0.00")
