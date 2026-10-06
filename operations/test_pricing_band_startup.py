import os
from contextlib import contextmanager, redirect_stdout
from io import StringIO
from unittest.mock import patch

from django.core.management.base import CommandError
from django.test import TestCase, override_settings

import pricing_band_startup as startup

from .models import BandRate, DomainEvent, OperationalZone, PricingBand
from .production_pricing_bands import protected_state
from .test_production_pricing_bands import ProductionPricingBandsTests


@override_settings(PRODUCTION=True, DEBUG=False)
class PricingBandStartupTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        ProductionPricingBandsTests.setUpTestData.__func__(cls)

    @contextmanager
    def production(self):
        with (
            patch.dict(os.environ, {"RENDER_GENERAL_PRICING_APPLY_ACTOR": self.admin.username}),
            patch("pricing_band_startup.connection") as conn,
            patch(
                "operations.management.commands.apply_general_pricing_bands.connection"
            ) as cmd_conn,
            redirect_stdout(StringIO()) as output,
        ):
            conn.vendor = cmd_conn.vendor = "postgresql"
            yield output

    def test_apply_postpreview_resolvers_integrity_and_restart_skip(self):
        before = protected_state()
        with self.production() as output:
            startup.run_rollout()
            startup.run_rollout()
        self.assertEqual(before, protected_state())
        self.assertEqual(PricingBand.objects.count(), 9)
        self.assertEqual(BandRate.objects.count(), 73)
        self.assertEqual(
            DomainEvent.objects.filter(kind="pricing.general_bands_applied").count(), 1
        )
        report = output.getvalue()
        self.assertIn("MANDATORY POST-APPLY PREVIEW", report)
        self.assertIn("ONE-TIME APPLY SKIPPED", report)
        self.assertEqual(report.count("VERIFIED ROUTE:"), 20)
        self.assertIn("Madina -> Accra Central bike: GHS 50.00 general_band", report)
        self.assertIn("Adenta -> Madina bike: GHS 30.00 general_exact", report)
        self.assertIn("Madina -> Adenta car: GHS 45.00 general_exact", report)

    def test_resolver_failure_rolls_back_every_rollout_write(self):
        before = protected_state()
        with (
            self.production(),
            patch.object(startup, "route_report", side_effect=CommandError("bad resolver")),
        ):
            with self.assertRaisesMessage(CommandError, "bad resolver"):
                startup.run_rollout()
        self.assertEqual(before, protected_state())
        self.assertEqual(PricingBand.objects.count(), 0)
        self.assertEqual(BandRate.objects.count(), 0)
        self.assertFalse(OperationalZone.objects.filter(pricing_band__isnull=False).exists())
        self.assertFalse(DomainEvent.objects.filter(kind="pricing.general_bands_applied").exists())

    def test_missing_actor_fails_without_apply(self):
        with self.production(), patch.dict(os.environ, {"RENDER_GENERAL_PRICING_APPLY_ACTOR": ""}):
            with self.assertRaises(CommandError):
                startup.run_rollout()
        self.assertFalse(PricingBand.objects.exists())

    @override_settings(DEBUG=True)
    def test_debug_fails_without_apply(self):
        with self.production():
            with self.assertRaises(CommandError):
                startup.run_rollout()
        self.assertFalse(PricingBand.objects.exists())

    def test_partial_band_state_fails_without_apply(self):
        PricingBand.objects.create(name="Unexpected", display_order=1)
        with self.production():
            with self.assertRaises(CommandError):
                startup.run_rollout()
        self.assertFalse(BandRate.objects.exists())
