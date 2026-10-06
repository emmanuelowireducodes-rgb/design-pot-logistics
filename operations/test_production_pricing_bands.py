from io import StringIO
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from . import general_pricing_band_seed as seed
from . import price_cards
from .models import (
    AreaAlias,
    BandRate,
    DomainEvent,
    OperationalZone,
    Organization,
    PremiumPricingRule,
    PricingBand,
    User,
)
from .production_pricing_bands import apply_rollout, preview_rollout, protected_state


class ProductionPricingBandsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.zones = {
            name: OperationalZone.objects.create(name=name)
            for _, _, names in seed.BANDS
            for name in names
        }
        cls.admin = User.objects.create_user("band-rollout-admin", role="admin")
        cls.organization = Organization.objects.create(name="Protected Client")
        for organization in (None, cls.organization):
            card = price_cards.save_card(
                actor=cls.admin, name="Protected", organization=organization
            )
            price_cards.save_route_price(
                actor=cls.admin,
                card_id=card.pk,
                origin_id=cls.zones["Madina"].pk,
                destination_id=cls.zones["Accra Central"].pk,
                bike_price="50",
                car_price="90",
                reason="Protected exact prices",
            )
        PremiumPricingRule.objects.create(value="20", enabled=True)
        PremiumPricingRule.objects.create(organization=cls.organization, value="15", enabled=True)

    def run_preview(self, **options):
        output = StringIO()
        call_command("apply_general_pricing_bands", stdout=output, **options)
        return output.getvalue()

    def test_preview_is_default_and_executes_no_writes(self):
        before = protected_state()
        with CaptureQueriesContext(connection) as queries:
            output = self.run_preview(actor=self.admin.username)
        self.assertTrue(all(q["sql"].lstrip().upper().startswith("SELECT") for q in queries))
        self.assertIn("Mode: PREVIEW ONLY", output)
        self.assertIn("to create=9", output)
        self.assertIn("to assign=114", output)
        self.assertIn("General Bike BandRates: identical=0; to create=45; conflicts=0", output)
        self.assertIn("General Car BandRates: identical=0; to create=28; conflicts=0", output)
        self.assertIn("H/I Car combinations currently unset: 17 / 17", output)
        self.assertEqual(PricingBand.objects.count(), 0)
        self.assertEqual(BandRate.objects.count(), 0)
        self.assertEqual(before, protected_state())

    def test_exact_configuration_preservation_audit_and_idempotence(self):
        before = protected_state()
        apply_rollout(actor=self.admin)
        self.assertEqual(before, protected_state())
        self.assertEqual(PricingBand.objects.count(), 9)
        self.assertEqual(OperationalZone.objects.exclude(pricing_band=None).count(), 114)
        self.assertEqual(BandRate.objects.filter(vehicle="bike").count(), 45)
        self.assertEqual(BandRate.objects.filter(vehicle="car").count(), 28)
        codes = {
            band.pk: code
            for code, name, _ in seed.BANDS
            for band in [PricingBand.objects.get(name=name)]
        }
        actual = {
            (row.vehicle, frozenset((codes[row.origin_id], codes[row.destination_id])), row.amount)
            for row in BandRate.objects.all()
        }
        expected = {
            (vehicle, frozenset((a, b)), amount) for vehicle, a, b, amount in seed.matrix_config()
        }
        self.assertEqual(actual, expected)
        preview = preview_rollout()
        self.assertEqual(preview["hi_car_unset"], 17)
        self.assertFalse(preview["matrix_to_create"])
        self.assertEqual(preview["assignments_matching"], 114)
        state = list(BandRate.objects.order_by("pk").values())
        apply_rollout(actor=self.admin)
        self.run_preview()
        self.assertEqual(state, list(BandRate.objects.order_by("pk").values()))
        self.assertEqual(
            DomainEvent.objects.filter(kind="pricing.general_bands_applied").count(), 1
        )

    def test_missing_zone_refuses_apply(self):
        self.zones["American House"].delete()
        self.assert_refused()

    def test_ambiguous_mapping_refuses_apply(self):
        # Current schema forbids duplicate normalized names; verify the command
        # still refuses an ambiguous resolver result (e.g. a legacy directory).
        resolution = seed.resolve_zone_assignments()
        resolution["ambiguous"] = [("American House", ["Area one", "Area two"])]
        with patch.object(seed, "resolve_zone_assignments", return_value=resolution):
            self.assert_refused()

    def test_duplicate_mapping_refuses_apply(self):
        self.zones["American House"].delete()
        AreaAlias.objects.create(zone=self.zones["Madina"], name="American House")
        self.assert_refused()

    def test_conflicting_assignment_refuses_apply(self):
        band = PricingBand.objects.create(name=seed.BANDS[0][1], display_order=1)
        OperationalZone.objects.filter(pk=self.zones["Madina"].pk).update(pricing_band=band)
        self.assert_refused()

    def test_conflicting_band_rate_refuses_apply(self):
        seed.apply_seed()
        BandRate.objects.filter(vehicle="bike").update(amount="999")
        self.assert_refused()

    def test_unexpected_band_refuses_apply(self):
        PricingBand.objects.create(name="Unexpected", display_order=99)
        self.assert_refused()

    def test_forbidden_hi_car_rate_refuses_apply(self):
        seed.apply_seed()
        h = PricingBand.objects.get(name=seed.BANDS[7][1])
        BandRate.objects.create(
            origin=h, destination=h, vehicle="car", amount="100", bidirectional=True
        )
        self.assertEqual(preview_rollout()["hi_car_unset"], 16)
        self.assert_refused()

    def test_client_band_rates_preserved(self):
        seed.apply_seed()
        a = PricingBand.objects.get(name=seed.BANDS[0][1])
        BandRate.objects.create(
            organization=self.organization,
            origin=a,
            destination=a,
            vehicle="bike",
            amount="42",
            bidirectional=True,
        )
        before = protected_state()
        apply_rollout(actor=self.admin)
        self.assertEqual(protected_state(), before)

    def test_failure_rolls_back_bands_assignments_rates_and_audit(self):
        with patch(
            "operations.production_pricing_bands.emit_event",
            side_effect=RuntimeError("audit failed"),
        ):
            with self.assertRaises(RuntimeError):
                apply_rollout(actor=self.admin)
        self.assertEqual(PricingBand.objects.count(), 0)
        self.assertEqual(BandRate.objects.count(), 0)
        self.assertFalse(OperationalZone.objects.exclude(pricing_band=None).exists())

    def test_protected_state_failure_rolls_back(self):
        with patch(
            "operations.production_pricing_bands.protected_state",
            side_effect=[{}, {"changed": True}],
        ):
            with self.assertRaises(ValidationError):
                apply_rollout(actor=self.admin)
        self.assertEqual(PricingBand.objects.count(), 0)

    def test_apply_requires_production_postgres_debug_off(self):
        with self.assertRaises(CommandError):
            self.run_preview(apply=True, actor=self.admin.username)
        self.assertEqual(PricingBand.objects.count(), 0)

    def test_debug_or_nonproduction_refuses_apply_even_on_postgres(self):
        with patch("operations.management.commands.apply_general_pricing_bands.connection") as conn:
            conn.vendor = "postgresql"
            for production, debug in ((True, True), (False, False)):
                with override_settings(PRODUCTION=production, DEBUG=debug):
                    with self.assertRaises(CommandError):
                        self.run_preview(apply=True, actor=self.admin.username)

    @override_settings(PRODUCTION=True, DEBUG=False)
    def test_production_default_still_preview_only(self):
        with patch("operations.management.commands.apply_general_pricing_bands.connection") as conn:
            conn.vendor = "postgresql"
            output = self.run_preview(actor=self.admin.username)
        self.assertIn("Mode: PREVIEW ONLY", output)
        self.assertEqual(PricingBand.objects.count(), 0)

    @override_settings(PRODUCTION=True, DEBUG=False)
    def test_apply_requires_explicit_active_admin(self):
        with patch("operations.management.commands.apply_general_pricing_bands.connection") as conn:
            conn.vendor = "postgresql"
            for actor in (None, "missing"):
                with self.assertRaises(CommandError):
                    self.run_preview(apply=True, actor=actor)
            User.objects.filter(pk=self.admin.pk).update(is_active=False)
            with self.assertRaises(CommandError):
                self.run_preview(apply=True, actor=self.admin.username)
        self.assertEqual(PricingBand.objects.count(), 0)

    def assert_refused(self):
        before = (
            list(PricingBand.objects.values()),
            list(BandRate.objects.values()),
            protected_state(),
        )
        with self.assertRaises(ValidationError):
            apply_rollout(actor=self.admin)
        self.assertEqual(
            before,
            (
                list(PricingBand.objects.values()),
                list(BandRate.objects.values()),
                protected_state(),
            ),
        )
