from django.test import TestCase
from django.urls import reverse

from . import price_cards, workflow
from .general_price_handoff import import_general_dpot
from .models import (
    Delivery,
    FixedRate,
    OperationalZone,
    Organization,
    PriceCard,
    QuoteConfirmation,
    User,
)


class PricingSourceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user("pricing-source-admin", role="admin")
        cls.organization = Organization.objects.create(name="General pricing client")
        cls.client_user = User.objects.create_user(
            "pricing-source-client", role="client", organization=cls.organization
        )
        cls.import_result = import_general_dpot(actor=cls.admin)
        cls.origin = OperationalZone.objects.get(name="East Legon")
        cls.general_card = PriceCard.objects.get(scope_key="general")

    def book(self, destination, vehicle="bike"):
        zone = OperationalZone.objects.get(name=destination)
        response = self.client.post(
            reverse("booking"),
            {
                "booking_key": __import__("uuid").uuid4(),
                "revision": "0",
                "pickup_zone": str(self.origin.pk),
                "destination_zone": str(zone.pk),
                "pickup_address": "Trinity Avenue, East Legon",
                "destination_address": f"Unique address for {destination} {vehicle}",
                "recipient_name": "Pricing Source Recipient",
                "recipient_phone": "+233200000001",
                "package_description": "Test package",
                "vehicle_type": vehicle,
                "service": "standard",
                "payment_method": "paid",
                "cod_amount": "",
            },
            follow=True,
        )
        delivery = Delivery.objects.filter(
            job__organization=self.organization,
            destination__display_address=f"Unique address for {destination} {vehicle}",
        ).latest("created_at")
        return response, delivery

    def test_new_organization_defaults_to_general_pricing(self):
        organization = Organization.objects.create(name="New Dpot client")
        self.assertFalse(organization.use_custom_pricing)

    def test_general_pricing_client_uses_madina_osu_and_tema_bike_rates(self):
        self.client.force_login(self.client_user)
        for destination, amount in (
            ("Madina", 40),
            ("Osu", 50),
            ("Tema Community 18", 100),
        ):
            with self.subTest(destination=destination):
                _, delivery = self.book(destination)
                self.assertEqual(delivery.job.current_quote.amount, amount)
                self.assertEqual(delivery.job.current_quote.source, "GENERAL_FIXED_RATE")

    def test_unset_tema_car_stays_unquoted_for_admin(self):
        self.client.force_login(self.client_user)
        _, delivery = self.book("Tema Community 18", "car")
        self.assertIsNone(delivery.job.current_quote_id)
        self.assertTrue(delivery.job.quote_requested)
        self.assertFalse(
            FixedRate.objects.filter(
                card=self.general_card,
                origin=self.origin,
                destination__name="Tema Community 18",
                vehicle="car",
            ).exists()
        )

    def test_custom_rate_overrides_general(self):
        self.client.force_login(self.admin)
        card = price_cards.save_card(
            actor=self.admin,
            name="General pricing client custom card",
            organization=self.organization,
        )
        price_cards.save_route_price(
            actor=self.admin,
            card_id=card.pk,
            origin_id=self.origin.pk,
            destination_id=OperationalZone.objects.get(name="Madina").pk,
            bike_price=25,
            bidirectional=False,
            reason="Negotiated test rate",
        )
        self.client.force_login(self.client_user)
        _, delivery = self.book("Madina")
        self.assertEqual(delivery.job.current_quote.amount, 25)
        self.assertEqual(delivery.job.current_quote.source, "CLIENT_CONTRACT_RATE")

    def test_custom_client_without_matching_rate_falls_back_to_general(self):
        price_cards.save_card(
            actor=self.admin,
            name="Empty custom card",
            organization=self.organization,
        )
        self.client.force_login(self.client_user)
        _, delivery = self.book("Osu")
        self.assertEqual(delivery.job.current_quote.amount, 50)
        self.assertEqual(delivery.job.current_quote.source, "GENERAL_FIXED_RATE")

    def test_changing_source_does_not_change_confirmed_quote(self):
        self.client.force_login(self.client_user)
        _, delivery = self.book("Madina")
        quote = delivery.job.current_quote
        workflow.confirm_quote(actor=self.client_user, quote_id=quote.pk)
        self.organization.use_custom_pricing = True
        self.organization.save(update_fields=["use_custom_pricing"])
        confirmation = QuoteConfirmation.objects.get(job=delivery.job)
        self.assertEqual(confirmation.quote_id, quote.pk)
        self.assertEqual(confirmation.quote.amount, 40)
        self.assertEqual(delivery.job.current_quote_id, quote.pk)

    def test_admin_can_assign_pricing_source_for_organization(self):
        self.client.force_login(self.admin)
        page = self.client.get(reverse("cards"))
        self.assertContains(page, "CLIENT PRICING ASSIGNMENT")
        self.assertContains(page, "General Dpot Pricing")
        self.assertContains(page, "Custom — General fallback")
        response = self.client.post(
            reverse("cards"),
            {
                "organization_id": str(self.organization.pk),
                "use_custom_pricing": "custom",
                "premium_rule_source": "general",
                "premium_method": "percentage",
                "premium_value": "0",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.organization.refresh_from_db()
        self.assertTrue(self.organization.use_custom_pricing)
        self.client.get(response["Location"])
        response = self.client.post(
            reverse("cards"),
            {
                "organization_id": str(self.organization.pk),
                "use_custom_pricing": "general",
                "premium_rule_source": "general",
                "premium_method": "percentage",
                "premium_value": "0",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.organization.refresh_from_db()
        self.assertFalse(self.organization.use_custom_pricing)

    def test_existing_zones_can_be_selected_without_creating_duplicates(self):
        self.client.force_login(self.admin)
        test_origin = OperationalZone.objects.create(name="Bulk Selection Origin")
        test_destination = OperationalZone.objects.create(name="Bulk Selection Destination")
        zone_count = OperationalZone.objects.count()
        active_ids = list(
            OperationalZone.objects.filter(active=True).values_list("pk", flat=True)
        )
        response = self.client.get(reverse("route-price-create", args=[self.general_card.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Import Existing Zones")
        self.assertContains(response, "Select All")
        self.assertContains(response, "Clear All")
        self.assertContains(response, "Import/Add Selected Zones")
        self.assertEqual(len(response.context["form"]["selected_zones"]), len(active_ids))

        response = self.client.post(
            reverse("route-price-create", args=[self.general_card.pk]),
            {
                "selected_zones": [str(pk) for pk in active_ids],
                "origin": str(test_origin.pk),
                "destination": str(test_destination.pk),
                "bike_price": "123",
                "same_return": "on",
                "service": "standard",
                "active": "on",
                "reason": "Zone selection reuse test",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(OperationalZone.objects.count(), zone_count)

    def test_client_cannot_access_pricing_assignment_or_zone_selection(self):
        self.client.force_login(self.client_user)
        self.assertEqual(self.client.get(reverse("cards")).status_code, 403)
        self.assertEqual(
            self.client.post(
                reverse("cards"),
                {
                    "organization_id": str(self.organization.pk),
                    "use_custom_pricing": "custom",
                },
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(reverse("route-price-create", args=[self.general_card.pk])).status_code,
            403,
        )
