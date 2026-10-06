"""Regression checks for pricing-version identity and immutable quote history."""

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from . import price_cards, pricing, workflow
from .models import (
    BandRate,
    Delivery,
    Job,
    Location,
    OperationalZone,
    Organization,
    PremiumPricingRule,
    PricingBand,
    PricingVersion,
    Quote,
    QuoteConfirmation,
    User,
)


class PricingVersionSnapshotTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user("version-admin", role="admin")
        cls.organization = Organization.objects.create(name="Version Client")
        cls.client_user = User.objects.create_user(
            "version-client", role="client", organization=cls.organization
        )
        cls.origin_band = PricingBand.objects.create(name="Version Origin Band")
        cls.destination_band = PricingBand.objects.create(name="Version Destination Band")
        cls.origin = OperationalZone.objects.create(
            name="Version Origin", pricing_band=cls.origin_band
        )
        cls.destination = OperationalZone.objects.create(
            name="Version Destination", pricing_band=cls.destination_band
        )
        cls.band_rate = BandRate.objects.create(
            origin=cls.origin_band,
            destination=cls.destination_band,
            vehicle="bike",
            amount=Decimal("50.00"),
        )
        cls.rule = PremiumPricingRule.objects.create(enabled=True, value=Decimal("20.00"))

    def delivery(self, *, service="premium"):
        start = timezone.now() + timedelta(days=1) if service == "premium" else None
        return Delivery.objects.create(
            job=Job.objects.create(organization=self.organization, created_by=self.client_user),
            pickup=Location.objects.create(display_address="Version pickup", zone=self.origin),
            destination=Location.objects.create(
                display_address="Version destination", zone=self.destination
            ),
            recipient_name="Version Recipient",
            recipient_phone="+233200000000",
            package_description="Version parcel",
            vehicle_type="bike",
            service=service,
            window_start=start,
            window_end=start + timedelta(hours=1) if start else None,
        )

    def test_premium_requote_updates_version_and_preserves_confirmed_history(self):
        confirmed = pricing.prepare_quote(self.delivery())
        workflow.confirm_quote(actor=self.client_user, quote_id=confirmed.pk)
        original_rules = deepcopy(confirmed.pricing_version.rules)
        original_breakdown = deepcopy(confirmed.breakdown)

        draft = self.delivery()
        first = pricing.prepare_quote(draft)
        self.rule.value = Decimal("30.00")
        self.rule.save(update_fields=["value", "updated_at"])
        second = pricing.prepare_quote(draft)

        self.assertEqual(first.amount, Decimal("60.00"))
        self.assertEqual(second.amount, Decimal("65.00"))
        self.assertNotEqual(first.pricing_version_id, second.pricing_version_id)
        calculation = second.pricing_version.rules["premium_calculation"]
        self.assertEqual(calculation["value"], "30.00")
        self.assertEqual(calculation["adjustment"], "15.00")
        self.assertEqual(calculation["premium_amount"], "65.00")
        self.assertEqual(second.pricing_version.rules["amount"], "65.00")
        self.assertEqual(calculation, second.breakdown["premium_calculation"])

        confirmed.refresh_from_db()
        confirmed.pricing_version.refresh_from_db()
        self.assertEqual(confirmed.amount, Decimal("60.00"))
        self.assertEqual(confirmed.breakdown, original_breakdown)
        self.assertEqual(confirmed.pricing_version.rules, original_rules)
        self.assertEqual(QuoteConfirmation.objects.get(job=confirmed.job).quote_id, confirmed.pk)

    def test_identical_calculation_reuses_identical_version(self):
        delivery = self.delivery()
        first = pricing.prepare_quote(delivery)
        second = pricing.prepare_quote(delivery)
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(first.pricing_version_id, second.pricing_version_id)
        self.assertEqual(first.pricing_version.rules, second.pricing_version.rules)
        self.assertEqual(PricingVersion.objects.count(), 1)

    def test_band_amount_change_creates_current_version_metadata(self):
        delivery = self.delivery(service="standard")
        first = pricing.prepare_quote(delivery)
        self.band_rate.amount = Decimal("55.00")
        self.band_rate.save(update_fields=["amount", "updated_at"])
        second = pricing.prepare_quote(delivery)
        self.assertNotEqual(first.pricing_version_id, second.pricing_version_id)
        self.assertEqual(second.amount, Decimal("55.00"))
        self.assertEqual(second.pricing_version.rules["standard_resolution"]["amount"], "55.00")
        self.assertEqual(second.pricing_version.rules["amount"], "55.00")
        self.assertEqual(first.pricing_version.rules["amount"], "50.00")

    def test_premium_scope_and_method_changes_do_not_reuse_version(self):
        delivery = self.delivery()
        general = pricing.prepare_quote(delivery)
        custom_rule = PremiumPricingRule.objects.create(
            organization=self.organization, enabled=True, value=Decimal("20.00")
        )
        self.organization.premium_rule_source = "custom"
        self.organization.save(update_fields=["premium_rule_source"])
        custom = pricing.prepare_quote(delivery)
        self.assertEqual(custom.amount, general.amount)
        self.assertNotEqual(custom.pricing_version_id, general.pricing_version_id)
        self.assertEqual(custom.pricing_version.rules["premium_calculation"]["rule_scope"], "custom")
        custom_rule.method = "fixed"
        custom_rule.value = Decimal("10.00")
        custom_rule.save(update_fields=["method", "value", "updated_at"])
        fixed = pricing.prepare_quote(delivery)
        self.assertEqual(fixed.amount, custom.amount)
        self.assertNotEqual(fixed.pricing_version_id, custom.pricing_version_id)
        self.assertEqual(fixed.pricing_version.rules["premium_calculation"]["method"], "fixed")

    def test_exact_rate_revision_changes_version_even_for_same_amount(self):
        card = price_cards.save_card(actor=self.admin, name="Version General Exact")
        values = {
            "actor": self.admin,
            "card_id": card.pk,
            "origin_id": self.origin.pk,
            "destination_id": self.destination.pk,
            "bike_price": Decimal("50.00"),
            "bidirectional": False,
            "reason": "Initial exact version",
        }
        price_cards.save_route_price(**values)
        delivery = self.delivery(service="standard")
        first = pricing.prepare_quote(delivery)
        price_cards.save_route_price(**{**values, "editing": True, "reason": "New exact version"})
        second = pricing.prepare_quote(delivery)
        self.assertEqual(first.amount, second.amount)
        self.assertNotEqual(first.pricing_version_id, second.pricing_version_id)
        self.assertEqual(first.pricing_version.rules["standard_resolution"]["rate_version"], 1)
        self.assertEqual(second.pricing_version.rules["standard_resolution"]["rate_version"], 2)

    def test_source_change_creates_version_even_for_same_amount(self):
        delivery = self.delivery(service="standard")
        general = pricing.prepare_quote(delivery)
        self.organization.use_custom_pricing = True
        self.organization.save(update_fields=["use_custom_pricing"])
        BandRate.objects.create(
            organization=self.organization,
            origin=self.origin_band,
            destination=self.destination_band,
            vehicle="bike",
            amount=Decimal("50.00"),
        )
        custom = pricing.prepare_quote(delivery)
        self.assertEqual(custom.amount, general.amount)
        self.assertNotEqual(custom.pricing_version_id, general.pricing_version_id)
        self.assertEqual(custom.pricing_version.rules["source"], "CLIENT_BAND_RATE")

    def test_old_job_based_version_is_not_reused(self):
        delivery = self.delivery(service="standard")
        old = PricingVersion.objects.create(
            label=f"general_band_rate-{delivery.job_id}", rules={"amount": "999.00"}
        )
        quote = pricing.prepare_quote(delivery)
        self.assertNotEqual(quote.pricing_version_id, old.pk)
        self.assertEqual(quote.pricing_version.rules["amount"], "50.00")
        self.assertEqual(Quote.objects.count(), 1)
