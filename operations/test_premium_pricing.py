import uuid
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import price_cards, pricing, workflow
from .general_price_handoff import import_general_dpot
from .models import (
    OperationalZone,
    Organization,
    PremiumPricingRule,
    QuoteConfirmation,
    User,
)
from .phase25_forms import OrganizationPricingForm, PremiumRuleForm


class PremiumPricingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user("premium-admin", role="admin")
        cls.organization = Organization.objects.create(name="Premium General Client")
        cls.client_user = User.objects.create_user(
            "premium-client", role="client", organization=cls.organization
        )
        import_general_dpot(actor=cls.admin)
        cls.origin = OperationalZone.objects.get(name="East Legon")
        cls.general_card = price_cards.current_rates().first().card

    def set_general_rule(self, *, enabled=True, method="percentage", value="20.00"):
        rule, _ = PremiumPricingRule.objects.update_or_create(
            scope_key="general",
            defaults={
                "organization": None,
                "enabled": enabled,
                "method": method,
                "value": Decimal(value),
            },
        )
        return rule

    def book_premium(self, destination, *, vehicle="bike"):
        zone = OperationalZone.objects.get(name=destination)
        start = timezone.now() + timedelta(days=1)
        return workflow.save_booking(
            actor=self.client_user,
            data={
                "booking_key": str(uuid.uuid4()),
                "revision": 0,
                "pickup_zone": self.origin.pk,
                "destination_zone": zone.pk,
                "pickup_address": "Trinity Avenue, East Legon",
                "pickup_area": self.origin.name,
                "pickup_instructions": "",
                "destination_address": f"{destination} {uuid.uuid4()}",
                "destination_area": destination,
                "destination_instructions": "",
                "recipient_name": "Premium Test Recipient",
                "recipient_phone": "+233200000001",
                "package_description": "Test parcel",
                "instructions": "",
                "vehicle_type": vehicle,
                "service": "premium",
                "window_start": start,
                "window_end": start + timedelta(hours=2),
                "payment_method": "paid",
                "cod_amount": Decimal("0"),
            },
        )

    def test_general_standard_40_and_50_receive_20_percent_uplift(self):
        self.set_general_rule(value="20")
        for destination, expected in (("Madina", Decimal("48.00")), ("Osu", Decimal("60.00"))):
            with self.subTest(destination=destination):
                delivery = self.book_premium(destination)
                self.assertEqual(delivery.job.current_quote.amount, expected)
                calculation = delivery.job.current_quote.breakdown["premium_calculation"]
                self.assertEqual(calculation["standard_amount"], "40.00" if destination == "Madina" else "50.00")
                self.assertEqual(calculation["premium_amount"], str(expected))

    def test_general_standard_40_and_50_receive_fixed_15_uplift(self):
        self.set_general_rule(method="fixed", value="15")
        for destination, expected in (("Madina", Decimal("55.00")), ("Osu", Decimal("65.00"))):
            with self.subTest(destination=destination):
                delivery = self.book_premium(destination)
                self.assertEqual(delivery.job.current_quote.amount, expected)
                self.assertEqual(
                    delivery.job.current_quote.breakdown["premium_calculation"]["adjustment"],
                    "15.00",
                )

    def test_percentage_uses_decimal_and_currency_half_up_rounding(self):
        adjustment, total = pricing.calculate_premium(
            Decimal("12.34"), method="percentage", value=Decimal("12.5")
        )
        self.assertEqual(adjustment, Decimal("1.54"))
        self.assertEqual(total, Decimal("13.88"))
        self.assertIsInstance(total, Decimal)

    def test_requoting_uses_standard_base_and_never_adds_premium_twice(self):
        self.set_general_rule(value="20")
        delivery = self.book_premium("Osu")
        first_quote = delivery.job.current_quote
        second_quote = pricing.prepare_quote(delivery)
        self.assertEqual(first_quote.amount, Decimal("60.00"))
        self.assertEqual(second_quote.amount, Decimal("60.00"))

    def test_negative_and_over_limit_adjustments_are_rejected(self):
        for method in ("percentage", "fixed"):
            with self.subTest(method=method):
                form = PremiumRuleForm(
                    {"enabled": "on", "method": method, "value": "-0.01"}
                )
                self.assertFalse(form.is_valid())
        over_limit = PremiumRuleForm(
            {"enabled": "on", "method": "percentage", "value": "500.01"}
        )
        self.assertFalse(over_limit.is_valid())
        with self.assertRaises(ValidationError):
            rule = PremiumPricingRule(
                enabled=True,
                method=PremiumPricingRule.Method.PERCENTAGE,
                value=Decimal("500.01"),
            )
            rule.full_clean()

    def test_enabled_zero_uplift_is_rejected_in_general_and_custom_forms_and_model(self):
        for method in ("percentage", "fixed"):
            with self.subTest(method=method):
                general = PremiumRuleForm(
                    {"enabled": "on", "method": method, "value": "0"}
                )
                self.assertFalse(general.is_valid())
                self.assertIn("value", general.errors)
                custom = OrganizationPricingForm(
                    {
                        "use_custom_pricing": "general",
                        "premium_rule_source": "custom",
                        "premium_enabled": "on",
                        "premium_method": method,
                        "premium_value": "0",
                    },
                    organization=self.organization,
                )
                self.assertFalse(custom.is_valid())
                self.assertIn("premium_value", custom.errors)
                with self.assertRaises(ValidationError):
                    PremiumPricingRule(enabled=True, method=method, value=Decimal("0")).full_clean()
                disabled = PremiumRuleForm({"method": method, "value": "0"})
                self.assertTrue(disabled.is_valid(), disabled.errors)

    def test_legacy_enabled_zero_rules_request_admin_quote(self):
        for method in ("percentage", "fixed"):
            with self.subTest(method=method):
                self.set_general_rule(method=method, value="0")
                delivery = self.book_premium("Osu")
                self.assertTrue(delivery.job.quote_requested)
                self.assertIsNone(delivery.job.current_quote_id)

    def test_tiny_percentage_with_zero_rounded_adjustment_requests_admin_quote(self):
        self.set_general_rule(value="0.01")
        with self.assertRaises(ValidationError):
            pricing.calculate_premium(
                Decimal("40.00"), method="percentage", value=Decimal("0.01")
            )
        delivery = self.book_premium("Madina")
        self.assertTrue(delivery.job.quote_requested)
        self.assertIsNone(delivery.job.current_quote_id)
        adjustment, total = pricing.calculate_premium(
            Decimal("50.00"), method="percentage", value=Decimal("0.01")
        )
        self.assertEqual(adjustment, Decimal("0.01"))
        self.assertEqual(total, Decimal("50.01"))

    def test_fixed_uplift_above_500_is_valid_and_has_no_html_percentage_limit(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("cards"),
            {
                "organization_id": self.organization.pk,
                "use_custom_pricing": "general",
                "premium_rule_source": "custom",
                "premium_enabled": "on",
                "premium_method": "fixed",
                "premium_value": "501.00",
            },
        )
        self.assertEqual(response.status_code, 302)
        rule = PremiumPricingRule.objects.get(organization=self.organization)
        self.assertEqual(rule.value, Decimal("501.00"))
        rule.full_clean()
        self.organization.refresh_from_db()
        delivery = self.book_premium("Osu")
        self.assertEqual(delivery.job.current_quote.amount, Decimal("551.00"))
        page = self.client.get(reverse("cards"))
        self.assertNotContains(page, 'max="500"')

    def test_invalid_stored_general_rule_does_not_break_cards_preview(self):
        self.set_general_rule(value="0")
        self.client.force_login(self.admin)
        response = self.client.get(reverse("cards"))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["premium_preview"])

    def test_admin_can_enable_then_uncheck_custom_premium_on_cards_page(self):
        self.client.force_login(self.admin)
        data = {
            "organization_id": self.organization.pk,
            "use_custom_pricing": "general",
            "premium_rule_source": "custom",
            "premium_enabled": "on",
            "premium_method": "fixed",
            "premium_value": "15",
        }
        self.assertEqual(self.client.post(reverse("cards"), data).status_code, 302)
        rule = PremiumPricingRule.objects.get(organization=self.organization)
        self.assertTrue(rule.enabled)
        self.assertEqual(self.client.get(reverse("cards")).status_code, 200)
        del data["premium_enabled"]
        self.assertEqual(self.client.post(reverse("cards"), data).status_code, 302)
        rule.refresh_from_db()
        self.assertFalse(rule.enabled)
        self.organization.refresh_from_db()
        delivery = self.book_premium("Osu")
        self.assertTrue(delivery.job.quote_requested)
        self.assertIsNone(delivery.job.current_quote_id)

    def test_card_settings_unchecked_premium_and_active_are_false(self):
        card = price_cards.save_card(
            actor=self.admin, name="Custom Premium card", organization=self.organization
        )
        self.client.force_login(self.admin)
        url = reverse("card-edit", args=[card.pk])
        data = {
            "name": card.name,
            "organization": self.organization.pk,
            "active": "on",
            "use_custom_pricing": "custom",
            "premium_rule_source": "custom",
            "premium_enabled": "on",
            "premium_method": "percentage",
            "premium_value": "20",
        }
        self.assertEqual(self.client.post(url, data).status_code, 302)
        rule = PremiumPricingRule.objects.get(organization=self.organization)
        self.assertTrue(rule.enabled)
        self.assertEqual(self.client.get(url).status_code, 200)
        del data["premium_enabled"]
        del data["active"]
        self.assertEqual(self.client.post(url, data).status_code, 302)
        rule.refresh_from_db()
        card.refresh_from_db()
        self.assertFalse(rule.enabled)
        self.assertFalse(card.active)

    def test_general_premium_unchecked_checkbox_disables_rule(self):
        self.set_general_rule()
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("cards"),
            {"action": "save_general_premium_rule", "method": "percentage", "value": "20"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(PremiumPricingRule.objects.get(scope_key="general").enabled)

    def test_custom_zero_uplift_post_shows_validation_and_does_not_save(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("cards"),
            {
                "organization_id": self.organization.pk,
                "use_custom_pricing": "general",
                "premium_rule_source": "custom",
                "premium_enabled": "on",
                "premium_method": "fixed",
                "premium_value": "0",
            },
        )
        self.assertContains(response, "Enabled Premium pricing requires a positive uplift.")
        self.assertFalse(PremiumPricingRule.objects.filter(organization=self.organization).exists())

    def test_no_standard_price_or_disabled_rule_requests_admin_quote(self):
        self.set_general_rule(enabled=True, value="20")
        unpriced = OperationalZone.objects.create(name="No Standard Price Zone")
        delivery = self.book_premium(unpriced.name)
        self.assertTrue(delivery.job.quote_requested)
        self.assertIsNone(delivery.job.current_quote_id)

        self.set_general_rule(enabled=False, value="20")
        disabled = self.book_premium("Madina")
        self.assertTrue(disabled.job.quote_requested)
        self.assertIsNone(disabled.job.current_quote_id)

    def test_unset_tema_community_18_car_remains_admin_quote(self):
        self.set_general_rule(enabled=True, method="fixed", value="15")
        delivery = self.book_premium("Tema Community 18", vehicle="car")
        self.assertTrue(delivery.job.quote_requested)
        self.assertIsNone(delivery.job.current_quote_id)

    def test_custom_standard_override_is_the_premium_base(self):
        self.set_general_rule(value="20")
        card = price_cards.save_card(
            actor=self.admin,
            name="Premium test client override",
            organization=self.organization,
        )
        price_cards.save_route_price(
            actor=self.admin,
            card_id=card.pk,
            origin_id=self.origin.pk,
            destination_id=OperationalZone.objects.get(name="Madina").pk,
            bike_price=Decimal("25"),
            bidirectional=False,
            reason="Custom Standard test rate",
        )
        delivery = self.book_premium("Madina")
        self.assertEqual(delivery.job.current_quote.amount, Decimal("30.00"))
        self.assertEqual(
            delivery.job.current_quote.source,
            "CLIENT_CONTRACT_RATE",
        )
        self.assertEqual(
            delivery.job.current_quote.breakdown["premium_calculation"]["standard_amount"],
            "25.00",
        )

    def test_custom_client_general_fallback_uses_general_standard_base(self):
        self.set_general_rule(value="20")
        card = price_cards.save_card(
            actor=self.admin,
            name="Client with no matching Standard route",
            organization=self.organization,
        )
        delivery = self.book_premium("Osu")
        self.assertEqual(delivery.job.current_quote.amount, Decimal("60.00"))
        self.assertEqual(delivery.job.current_quote.source, "GENERAL_FIXED_RATE")
        self.assertNotEqual(delivery.job.current_quote.breakdown["card_id"], str(card.pk))

    def test_general_rule_is_used_by_clients_inheriting_it(self):
        self.set_general_rule(value="20")
        self.assertEqual(self.organization.premium_rule_source, "general")
        delivery = self.book_premium("Madina")
        self.assertEqual(delivery.job.current_quote.amount, Decimal("48.00"))
        self.assertEqual(
            delivery.job.current_quote.breakdown["premium_calculation"]["rule_scope"],
            "general",
        )

    def test_custom_client_premium_rule_overrides_general(self):
        self.set_general_rule(value="20")
        self.organization.premium_rule_source = "custom"
        self.organization.save(update_fields=["premium_rule_source"])
        PremiumPricingRule.objects.update_or_create(
            scope_key=f"client:{self.organization.pk}",
            defaults={
                "organization": self.organization,
                "enabled": True,
                "method": "fixed",
                "value": Decimal("15.00"),
            },
        )
        delivery = self.book_premium("Osu")
        self.assertEqual(delivery.job.current_quote.amount, Decimal("65.00"))
        self.assertEqual(
            delivery.job.current_quote.breakdown["premium_calculation"]["rule_scope"],
            "custom",
        )

    def test_changing_rule_does_not_change_confirmed_quote(self):
        self.set_general_rule(value="20")
        delivery = self.book_premium("Osu")
        quote = delivery.job.current_quote
        self.assertEqual(quote.amount, Decimal("60.00"))
        workflow.confirm_quote(actor=self.client_user, quote_id=quote.pk)
        self.set_general_rule(value="25")
        confirmation = QuoteConfirmation.objects.get(job=delivery.job)
        self.assertEqual(confirmation.quote.amount, Decimal("60.00"))
        self.assertEqual(confirmation.quote.breakdown["premium_calculation"]["value"], "20.00")
        new_delivery = self.book_premium("Osu")
        self.assertEqual(new_delivery.job.current_quote.amount, Decimal("62.50"))

    def test_admin_can_save_general_premium_rule_and_clients_cannot(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("cards"),
            {
                "action": "save_general_premium_rule",
                "enabled": "on",
                "method": "percentage",
                "value": "20",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            PremiumPricingRule.objects.get(organization__isnull=True).value,
            Decimal("20.00"),
        )
        self.client.force_login(self.client_user)
        self.assertEqual(self.client.get(reverse("cards")).status_code, 403)
        self.assertEqual(
            self.client.post(
                reverse("cards"),
                {
                    "action": "save_general_premium_rule",
                    "enabled": "on",
                    "method": "fixed",
                    "value": "15",
                },
            ).status_code,
            403,
        )
