from decimal import Decimal
from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.test import TestCase

from . import general_pricing_band_seed as seed
from . import price_cards
from .models import BandRate, FixedRate, OperationalZone, PricingBand, User


class GeneralPricingBandSeedTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.zone_by_name = {
            name: OperationalZone.objects.create(name=name)
            for _, _, names in seed.BANDS
            for name in names
        }

    def test_approved_configuration_resolves_114_unique_exact_zones(self):
        preview = seed.preview_seed()
        self.assertEqual(len(seed.BANDS), 9)
        self.assertEqual(sum(len(names) for _, _, names in seed.BANDS), 114)
        self.assertEqual(preview["zones"]["unique_zones"], 114)
        self.assertEqual(len(preview["zones"]["assignments"]), 114)
        self.assertFalse(preview["zones"]["missing"])
        self.assertFalse(preview["zones"]["ambiguous"])
        self.assertFalse(preview["zones"]["duplicate_assignments"])
        self.assertEqual(
            {row["kind"] for row in preview["zones"]["assignments"]}, {"exact"}
        )

    def test_apply_creates_nine_bands_114_assignments_and_73_symmetric_rates(self):
        preview = seed.preview_seed()
        result = seed.apply_seed(preview)
        self.assertEqual(result, {"bands_created": 9, "zones_assigned": 114, "matrix_created": 73})
        self.assertEqual(PricingBand.objects.count(), 9)
        self.assertEqual(OperationalZone.objects.exclude(pricing_band=None).count(), 114)
        self.assertEqual(BandRate.objects.filter(scope_key="general", active=True).count(), 73)
        self.assertFalse(
            BandRate.objects.filter(
                scope_key="general", vehicle="car",
                origin__name__startswith="H ",
            ).exists()
        )
        self.assertFalse(
            BandRate.objects.filter(
                scope_key="general", vehicle="car",
                destination__name__startswith="I ",
            ).exists()
        )
        for origin, destination, amount in (("B", "D", "50"), ("B", "C", "40"), ("A", "H", "100"), ("H", "H", "60"), ("I", "I", "50")):
            rate = self.get_rate("bike", origin, destination)
            self.assertEqual(rate.amount, Decimal(amount))
            self.assertTrue(rate.bidirectional)
        self.assertEqual(self.get_rate("car", "A", "B").amount, Decimal("70"))
        self.assertEqual(self.get_rate("car", "B", "D").amount, Decimal("90"))

    def test_named_zone_assignments_and_band_resolver(self):
        seed.apply_seed(seed.preview_seed())
        expected = {
            "Madina": "B",
            "Accra Central": "D",
            "Adenta": "C",
            "Tema Community 18": "H",
            "Tema Community 25": "H",
            "Afienya": "H",
            "Dawhenya": "H",
            "Kasoa": "I",
        }
        for zone_name, code in expected.items():
            self.zone_by_name[zone_name].refresh_from_db()
            self.assertTrue(self.zone_by_name[zone_name].pricing_band.name.startswith(code + " "))
        madina = self.zone_by_name["Madina"]
        central = self.zone_by_name["Accra Central"]
        adenta = self.zone_by_name["Adenta"]
        madina.refresh_from_db()
        central.refresh_from_db()
        adenta.refresh_from_db()
        delivery = SimpleNamespace(
            pickup=SimpleNamespace(zone_id=madina.pk),
            destination=SimpleNamespace(zone_id=central.pk),
            vehicle_type="bike",
        )
        self.assertEqual(price_cards.matching_band_rate(delivery).amount, Decimal("50"))
        delivery.destination.zone_id = adenta.pk
        self.assertEqual(price_cards.matching_band_rate(delivery).amount, Decimal("40"))

    def test_far_corridor_car_has_no_band_rate_and_seed_is_idempotent(self):
        seed.apply_seed(seed.preview_seed())
        first_count = BandRate.objects.filter(scope_key="general", active=True).count()
        preview = seed.preview_seed()
        self.assertEqual(len(preview["identical_matrix_rates"]), 73)
        self.assertEqual(set(preview["bands"]), {code for code, _, _ in seed.BANDS})
        self.assertEqual(preview["matrix_to_create"], [])
        result = seed.apply_seed(preview)
        self.assertEqual(result, {"bands_created": 0, "zones_assigned": 114, "matrix_created": 0})
        self.assertEqual(BandRate.objects.filter(scope_key="general", active=True).count(), first_count)
        h = PricingBand.objects.get(name__startswith="H ")
        i = PricingBand.objects.get(name__startswith="I ")
        self.assertFalse(BandRate.objects.filter(scope_key="general", vehicle="car", origin=h).exists())
        self.assertFalse(BandRate.objects.filter(scope_key="general", vehicle="car", destination=h).exists())
        self.assertFalse(BandRate.objects.filter(scope_key="general", vehicle="car", origin=i).exists())
        self.assertFalse(BandRate.objects.filter(scope_key="general", vehicle="car", destination=i).exists())

    def test_general_exact_override_beats_seeded_matrix(self):
        seed.apply_seed(seed.preview_seed())
        admin = User.objects.create_user("seed-exact-admin", role="admin")
        card = price_cards.save_card(actor=admin, name="General Seed Exact Test")
        price_cards.save_route_price(
            actor=admin,
            card_id=card.pk,
            origin_id=self.zone_by_name["Madina"].pk,
            destination_id=self.zone_by_name["Accra Central"].pk,
            bike_price=Decimal("45"),
            car_price=None,
            bidirectional=False,
            reason="Seeded matrix precedence test",
        )
        delivery = SimpleNamespace(
            pickup=SimpleNamespace(zone_id=self.zone_by_name["Madina"].pk),
            destination=SimpleNamespace(zone_id=self.zone_by_name["Accra Central"].pk),
            vehicle_type="bike",
            service="standard",
        )
        resolution = price_cards.resolve_standard_price(delivery, None)
        self.assertEqual((resolution.amount, resolution.source), (Decimal("45.00"), "general_exact"))

    def test_existing_matrix_conflict_is_refused_and_exact_rate_rows_unchanged(self):
        admin = User.objects.create_user("seed-admin", role="admin")
        card = price_cards.save_card(actor=admin, name="General Seed Test")
        price_cards.save_route_price(
            actor=admin,
            card_id=card.pk,
            origin_id=self.zone_by_name["Madina"].pk,
            destination_id=self.zone_by_name["Accra Central"].pk,
            bike_price=Decimal("45"),
            car_price=None,
            bidirectional=False,
            reason="Seed test exact override",
        )
        before = list(
            FixedRate.objects.filter(card=card).order_by("pk").values_list(
                "pk", "amount", "version", "origin_id", "destination_id", "vehicle", "service"
            )
        )
        a = PricingBand.objects.create(name="A — East Legon Core", display_order=1)
        b = PricingBand.objects.create(name="B — East / Inner Northeast", display_order=2)
        BandRate.objects.create(
            scope_key="general", origin=a, destination=b, vehicle="bike",
            amount=Decimal("999"), bidirectional=False,
        )
        preview = seed.preview_seed()
        self.assertTrue(any("Conflicting General bike" in value for value in preview["conflicts"]))
        with self.assertRaises(ValidationError):
            seed.apply_seed(preview)
        after = list(
            FixedRate.objects.filter(card=card).order_by("pk").values_list(
                "pk", "amount", "version", "origin_id", "destination_id", "vehicle", "service"
            )
        )
        self.assertEqual(before, after)
        self.assertEqual(FixedRate.objects.get(card=card).amount, Decimal("45.00"))

    def get_rate(self, vehicle, origin_code, destination_code):
        origin = PricingBand.objects.get(name__startswith=origin_code + " ")
        destination = PricingBand.objects.get(name__startswith=destination_code + " ")
        return BandRate.objects.get(
            scope_key="general", vehicle=vehicle, bidirectional=True,
            origin=origin if str(origin.pk) <= str(destination.pk) else destination,
            destination=destination if str(origin.pk) <= str(destination.pk) else origin,
            active=True,
        )
