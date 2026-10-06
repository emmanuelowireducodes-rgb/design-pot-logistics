import uuid
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import price_cards, pricing, pricing_bands, workflow
from .general_price_handoff import import_general_dpot
from .models import (
    BandRate,
    Delivery,
    FixedRate,
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


class PricingBandEngineTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user("band-admin", role="admin")
        cls.organization = Organization.objects.create(
            name="Band General Client", use_custom_pricing=True
        )
        cls.client_user = User.objects.create_user(
            "band-client", role="client", organization=cls.organization
        )
        cls.origin = OperationalZone.objects.create(name="Band Origin")
        cls.destination = OperationalZone.objects.create(name="Band Destination")
        cls.reverse_zone = OperationalZone.objects.create(name="Band Reverse Zone")
        cls.origin_band = PricingBand.objects.create(name="Test Band Alpha")
        cls.destination_band = PricingBand.objects.create(name="Test Band Beta")
        OperationalZone.objects.filter(pk=cls.origin.pk).update(pricing_band=cls.origin_band)
        OperationalZone.objects.filter(pk__in=[cls.destination.pk, cls.reverse_zone.pk]).update(
            pricing_band=cls.destination_band
        )
        cls.general_card = price_cards.save_card(actor=cls.admin, name="General Test Card")
        cls.client_card = price_cards.save_card(
            actor=cls.admin, name="Client Test Card", organization=cls.organization
        )

    def delivery(self, *, origin=None, destination=None, vehicle="bike", service="standard"):
        origin, destination = origin or self.origin, destination or self.destination
        job = Job.objects.create(organization=self.organization, created_by=self.client_user)
        pickup = Location.objects.create(
            display_address=f"Pickup {uuid.uuid4()}", area=origin.name, zone=origin
        )
        dropoff = Location.objects.create(
            display_address=f"Dropoff {uuid.uuid4()}", area=destination.name, zone=destination
        )
        start = timezone.now() + timedelta(days=1) if service == "premium" else None
        return Delivery.objects.create(
            job=job,
            pickup=pickup,
            destination=dropoff,
            recipient_name="Band Test Recipient",
            recipient_phone="+233200000000",
            package_description="Test parcel",
            service=service,
            vehicle_type=vehicle,
            window_start=start,
            window_end=start + timedelta(hours=1) if start else None,
        )

    def rate(
        self,
        *,
        origin=None,
        destination=None,
        amount="45.00",
        organization=None,
        vehicle="bike",
        bidirectional=False,
    ):
        return BandRate.objects.create(
            organization=organization,
            origin=origin or self.origin_band,
            destination=destination or self.destination_band,
            vehicle=vehicle,
            amount=Decimal(amount),
            bidirectional=bidirectional,
        )

    def exact(self, *, organization=None, amount="32.00", origin=None, destination=None):
        card = self.client_card if organization else self.general_card
        return price_cards.save_route_price(
            actor=self.admin,
            card_id=card.pk,
            origin_id=(origin or self.origin).pk,
            destination_id=(destination or self.destination).pk,
            bike_price=Decimal(amount),
            car_price=None,
            bidirectional=False,
            reason="Band precedence test",
        )

    def save_matrix_rate(
        self,
        *,
        amount="50.00",
        organization=None,
        vehicle="bike",
        bidirectional=False,
        reverse_direction=False,
    ):
        origin, destination = self.origin_band, self.destination_band
        if reverse_direction:
            origin, destination = destination, origin
        return pricing_bands.save_band_rate(
            actor=self.admin,
            organization_id=organization.pk if organization else None,
            origin_id=origin.pk,
            destination_id=destination.pk,
            vehicle=vehicle,
            amount=amount,
            bidirectional=bidirectional,
        )

    def quote(self, delivery):
        return pricing.prepare_quote(delivery)

    def test_general_band_and_general_source(self):
        self.organization.use_custom_pricing = False
        self.organization.save(update_fields=["use_custom_pricing"])
        self.rate(amount="48.25")
        quote = self.quote(self.delivery())
        self.assertEqual((quote.amount, quote.source), (Decimal("48.25"), "GENERAL_BAND_RATE"))
        self.assertEqual(quote.breakdown["standard_resolution"]["source"], "general_band")

    def test_general_exact_fixed_rate_beats_general_band(self):
        self.rate(amount="55")
        self.exact(amount="50")
        self.assertEqual(self.quote(self.delivery()).amount, Decimal("50.00"))

    def test_client_exact_fixed_rate_beats_both_band_scopes(self):
        self.rate(amount="55")
        self.rate(amount="60", organization=self.organization)
        self.exact(organization=self.organization, amount="50")
        self.assertEqual(self.quote(self.delivery()).amount, Decimal("50.00"))

    def test_general_exact_precedes_client_band_override(self):
        self.rate(amount="55")
        self.rate(amount="60", organization=self.organization)
        self.exact(amount="50")
        self.assertEqual(self.quote(self.delivery()).amount, Decimal("50.00"))

    def test_client_band_override_precedes_general_matrix(self):
        self.rate(amount="55")
        self.rate(amount="60", organization=self.organization)
        quote = self.quote(self.delivery())
        self.assertEqual((quote.amount, quote.source), (Decimal("60.00"), "CLIENT_BAND_RATE"))

    def test_custom_client_falls_back_to_general_band(self):
        self.rate(amount="55")
        quote = self.quote(self.delivery())
        self.assertEqual((quote.amount, quote.source), (Decimal("55.00"), "GENERAL_BAND_RATE"))

    def test_bike_car_prices_are_independent(self):
        self.rate(amount="55", vehicle="bike")
        self.rate(amount="77", vehicle="car")
        self.assertEqual(self.quote(self.delivery(vehicle="bike")).amount, Decimal("55.00"))
        self.assertEqual(self.quote(self.delivery(vehicle="car")).amount, Decimal("77.00"))

    def test_missing_car_matrix_requests_quote_never_zero(self):
        self.rate(amount="55", vehicle="bike")
        delivery = self.delivery(vehicle="car")
        self.assertIsNone(self.quote(delivery))
        delivery.job.refresh_from_db()
        self.assertTrue(delivery.job.quote_requested)
        self.assertIsNone(delivery.job.current_quote_id)

    def test_unassigned_origin_skips_matrix_but_exact_still_works(self):
        OperationalZone.objects.filter(pk=self.origin.pk).update(pricing_band=None)
        self.rate(amount="55")
        self.assertIsNone(self.quote(self.delivery()))
        self.exact(amount="31")
        self.assertEqual(self.quote(self.delivery()).amount, Decimal("31.00"))

    def test_unassigned_destination_skips_matrix(self):
        OperationalZone.objects.filter(pk=self.destination.pk).update(pricing_band=None)
        self.rate(amount="55")
        self.assertIsNone(self.quote(self.delivery()))

    def test_missing_band_cell_requests_quote(self):
        self.assertIsNone(self.quote(self.delivery()))

    def test_directional_rate_does_not_price_reverse(self):
        self.rate(amount="55")
        self.assertEqual(self.quote(self.delivery()).amount, Decimal("55.00"))
        self.assertIsNone(
            self.quote(self.delivery(origin=self.reverse_zone, destination=self.origin))
        )

    def test_symmetric_rate_prices_both_directions(self):
        self.rate(amount="55", bidirectional=True)
        self.assertEqual(self.quote(self.delivery()).amount, Decimal("55.00"))
        self.assertEqual(
            self.quote(self.delivery(origin=self.reverse_zone, destination=self.origin)).amount,
            Decimal("55.00"),
        )

    def test_directional_rate_precedes_symmetric_rate(self):
        self.rate(amount="55", bidirectional=True)
        self.rate(amount="60")
        self.assertEqual(self.quote(self.delivery()).amount, Decimal("60.00"))
        self.assertEqual(
            self.quote(self.delivery(origin=self.reverse_zone, destination=self.origin)).amount,
            Decimal("55.00"),
        )

    def test_duplicate_directional_and_symmetric_rates_are_rejected(self):
        for organization in (None, self.organization):
            with self.subTest(organization=organization):
                self.rate(amount="55", organization=organization)
                with self.assertRaises(IntegrityError), transaction.atomic():
                    self.rate(amount="56", organization=organization)
                self.rate(amount="55", organization=organization, bidirectional=True)
                with self.assertRaises(ValidationError):
                    BandRate(
                        organization=organization,
                        origin=self.destination_band,
                        destination=self.origin_band,
                        vehicle="bike",
                        amount=Decimal("56"),
                        bidirectional=True,
                    ).full_clean()
                with self.assertRaises(IntegrityError), transaction.atomic():
                    self.rate(amount="56", organization=organization, bidirectional=True)

    def assert_matrix_rate_lifecycle(self, *, organization, vehicle, bidirectional):
        options = {
            "organization": organization,
            "vehicle": vehicle,
            "bidirectional": bidirectional,
        }
        initial = self.save_matrix_rate(amount="50.00", **options)
        for amount in ("55.00", "60.00"):
            edited = self.save_matrix_rate(
                amount=amount, reverse_direction=bidirectional, **options
            )
            self.assertEqual(edited.pk, initial.pk)
            self.assertEqual(edited.amount, Decimal(amount))
            self.assertTrue(edited.active)
        self.assertIsNone(self.save_matrix_rate(amount="", **options))
        initial.refresh_from_db()
        self.assertFalse(initial.active)
        recreated = self.save_matrix_rate(amount="65.00", **options)
        self.assertEqual(recreated.pk, initial.pk)
        self.assertEqual(recreated.amount, Decimal("65.00"))
        self.assertTrue(recreated.active)
        self.assertEqual(
            BandRate.objects.filter(
                scope_key=recreated.scope_key, vehicle=vehicle, bidirectional=bidirectional
            ).count(),
            1,
        )

    def test_directional_matrix_rate_create_edit_twice_clear_recreate(self):
        for organization in (None, self.organization):
            for vehicle in ("bike", "car"):
                with self.subTest(organization=organization, vehicle=vehicle):
                    self.assert_matrix_rate_lifecycle(
                        organization=organization, vehicle=vehicle, bidirectional=False
                    )
        self.assertEqual(BandRate.objects.count(), 4)

    def test_symmetric_matrix_rate_create_edit_twice_clear_recreate(self):
        for organization in (None, self.organization):
            for vehicle in ("bike", "car"):
                with self.subTest(organization=organization, vehicle=vehicle):
                    self.assert_matrix_rate_lifecycle(
                        organization=organization, vehicle=vehicle, bidirectional=True
                    )
        self.assertEqual(BandRate.objects.count(), 4)

    def test_symmetric_save_reuses_rate_and_replaces_both_directional_overrides(self):
        symmetric = self.save_matrix_rate(amount="50.00", bidirectional=True)
        forward = self.save_matrix_rate(amount="55.00")
        reverse = self.save_matrix_rate(amount="56.00", reverse_direction=True)
        updated = self.save_matrix_rate(
            amount="60.00", bidirectional=True, reverse_direction=True
        )
        self.assertEqual(updated.pk, symmetric.pk)
        forward.refresh_from_db()
        reverse.refresh_from_db()
        self.assertFalse(forward.active)
        self.assertFalse(reverse.active)
        self.assertEqual(BandRate.objects.count(), 3)
        self.assertEqual(self.quote(self.delivery()).amount, Decimal("60.00"))
        self.assertEqual(
            self.quote(self.delivery(origin=self.reverse_zone, destination=self.origin)).amount,
            Decimal("60.00"),
        )
        reactivated = self.save_matrix_rate(amount="70.00")
        self.assertEqual(reactivated.pk, forward.pk)
        self.assertEqual(BandRate.objects.count(), 3)
        self.assertEqual(self.quote(self.delivery()).amount, Decimal("70.00"))
        self.assertEqual(
            self.quote(self.delivery(origin=self.reverse_zone, destination=self.origin)).amount,
            Decimal("60.00"),
        )

    def test_clear_changes_only_selected_directionality_and_direction(self):
        symmetric = self.save_matrix_rate(amount="50.00", bidirectional=True)
        forward = self.save_matrix_rate(amount="55.00")
        reverse = self.save_matrix_rate(amount="56.00", reverse_direction=True)
        self.save_matrix_rate(amount="", bidirectional=True, reverse_direction=True)
        symmetric.refresh_from_db()
        forward.refresh_from_db()
        reverse.refresh_from_db()
        self.assertFalse(symmetric.active)
        self.assertTrue(forward.active)
        self.assertTrue(reverse.active)
        self.save_matrix_rate(amount="")
        forward.refresh_from_db()
        reverse.refresh_from_db()
        self.assertFalse(forward.active)
        self.assertTrue(reverse.active)
        self.assertIsNone(self.quote(self.delivery()))
        self.assertEqual(
            self.quote(self.delivery(origin=self.reverse_zone, destination=self.origin)).amount,
            Decimal("56.00"),
        )

    def test_conflicting_raw_symmetric_rows_require_review_before_matrix_edit(self):
        symmetric = self.save_matrix_rate(amount="50.00", bidirectional=True)
        BandRate.objects.bulk_create([
            BandRate(
                scope_key="general",
                origin=symmetric.destination,
                destination=symmetric.origin,
                vehicle="bike",
                amount=Decimal("51.00"),
                bidirectional=True,
            )
        ])
        with self.assertRaisesMessage(ValidationError, "Overlapping band rates"):
            self.save_matrix_rate(amount="60.00", bidirectional=True)
        symmetric.refresh_from_db()
        self.assertEqual(symmetric.amount, Decimal("50.00"))
        self.assertEqual(BandRate.objects.filter(active=True).count(), 2)

    def test_matrix_post_can_edit_clear_and_recreate_existing_rate(self):
        self.client.force_login(self.admin)
        data = {
            "organization_id": "",
            "origin_id": str(self.origin_band.pk),
            "destination_id": str(self.destination_band.pk),
            "vehicle": "bike",
            "directionality": "symmetric",
        }
        for amount in ("50.00", "55.00", "60.00", "", "65.00"):
            with self.subTest(amount=amount):
                response = self.client.post(reverse("band-matrix"), {**data, "amount": amount})
                self.assertEqual(response.status_code, 302)
                rate = BandRate.objects.get(scope_key="general", vehicle="bike")
                self.assertEqual(rate.active, bool(amount))
                if amount:
                    self.assertEqual(rate.amount, Decimal(amount))

    def test_inactive_band_is_unresolved_but_exact_still_works(self):
        self.rate(amount="55")
        self.origin_band.active = False
        self.origin_band.save(update_fields=["active"])
        self.assertIsNone(self.quote(self.delivery()))
        self.exact(amount="31")
        self.assertEqual(self.quote(self.delivery()).amount, Decimal("31.00"))

    def test_premium_percentage_and_single_application_on_band_standard(self):
        self.rate(amount="50")
        PremiumPricingRule.objects.create(
            enabled=True, method="percentage", value=Decimal("20"), organization=None
        )
        delivery = self.delivery(service="premium")
        first = self.quote(delivery)
        second = pricing.prepare_quote(delivery)
        self.assertEqual(first.amount, Decimal("60.00"))
        self.assertEqual(second.amount, Decimal("60.00"))
        self.assertEqual(first.breakdown["premium_calculation"]["standard_amount"], "50.00")

    def test_premium_fixed_uplift_on_band_standard(self):
        self.rate(amount="50")
        PremiumPricingRule.objects.create(
            enabled=True, method="fixed", value=Decimal("15"), organization=None
        )
        self.assertEqual(self.quote(self.delivery(service="premium")).amount, Decimal("65.00"))

    def test_premium_without_standard_requests_admin_quote(self):
        PremiumPricingRule.objects.create(
            enabled=True, method="percentage", value=Decimal("20"), organization=None
        )
        self.assertIsNone(self.quote(self.delivery(service="premium")))

    def test_confirmed_snapshot_survives_rate_and_band_changes(self):
        rate = self.rate(amount="50")
        quote = self.quote(self.delivery())
        workflow.confirm_quote(actor=self.client_user, quote_id=quote.pk)
        rate.amount = Decimal("99")
        rate.save(update_fields=["amount", "updated_at"])
        OperationalZone.objects.filter(pk=self.origin.pk).update(pricing_band=self.destination_band)
        confirmation = QuoteConfirmation.objects.get(job=quote.job)
        self.assertEqual(confirmation.quote.amount, Decimal("50.00"))
        self.assertEqual(
            confirmation.quote.breakdown["standard_resolution"]["band_rate_id"], rate.pk
        )
        self.assertEqual(
            confirmation.quote.breakdown["standard_resolution"]["origin_band"], "Test Band Alpha"
        )

    def test_confirmed_quote_and_version_survive_matrix_service_edit_clear_recreate(self):
        rate = self.save_matrix_rate(amount="50.00")
        quote = self.quote(self.delivery())
        workflow.confirm_quote(actor=self.client_user, quote_id=quote.pk)
        quote_snapshot = Quote.objects.filter(pk=quote.pk).values(
            "amount", "breakdown", "pricing_version_id"
        ).get()
        version_snapshot = PricingVersion.objects.filter(pk=quote.pricing_version_id).values(
            "label", "rules", "currency"
        ).get()
        confirmation_snapshot = QuoteConfirmation.objects.filter(job=quote.job).values().get()
        for amount in ("55.00", "60.00", "", "65.00"):
            self.save_matrix_rate(amount=amount)
        self.assertEqual(BandRate.objects.get(pk=rate.pk).amount, Decimal("65.00"))
        self.assertEqual(BandRate.objects.count(), 1)
        self.assertEqual(
            Quote.objects.filter(pk=quote.pk).values(
                "amount", "breakdown", "pricing_version_id"
            ).get(),
            quote_snapshot,
        )
        self.assertEqual(
            PricingVersion.objects.filter(pk=quote.pricing_version_id).values(
                "label", "rules", "currency"
            ).get(),
            version_snapshot,
        )
        self.assertEqual(
            QuoteConfirmation.objects.filter(job=quote.job).values().get(), confirmation_snapshot
        )
        self.assertEqual(quote_snapshot["amount"], Decimal("50.00"))

    def test_general_193_rates_remain_exact_fixedrates_unchanged(self):
        imported = import_general_dpot(actor=self.admin)
        self.assertEqual(imported["bike_created"], 114)
        self.assertEqual(imported["car_created"], 79)
        before = list(
            FixedRate.objects.filter(card__scope_key="general")
            .order_by("pk")
            .values_list(
                "pk", "amount", "version", "origin_id", "destination_id", "vehicle", "service"
            )
        )
        self.rate(amount="55")
        after = list(
            FixedRate.objects.filter(card__scope_key="general")
            .order_by("pk")
            .values_list(
                "pk", "amount", "version", "origin_id", "destination_id", "vehicle", "service"
            )
        )
        self.assertEqual(len(after), 193)
        self.assertEqual(before, after)

    def test_bulk_assignment_reuses_rows_and_deduplicates_ids(self):
        before = OperationalZone.objects.count()
        changed = pricing_bands.assign_zones(
            actor=self.admin,
            zone_ids=[str(self.origin.pk), str(self.origin.pk)],
            band_id=self.destination_band.pk,
        )
        self.assertEqual(changed, 1)
        self.assertEqual(OperationalZone.objects.count(), before)
        self.origin.refresh_from_db()
        self.assertEqual(self.origin.pricing_band_id, self.destination_band.pk)

    def test_bulk_select_all_and_move_assign_existing_zones(self):
        self.client.force_login(self.admin)
        page = self.client.get(reverse("pricing-bands"))
        self.assertContains(page, "Select All")
        self.assertNotContains(page, "pagination")
        response = self.client.post(
            reverse("pricing-bands"),
            {
                "action": "assign_zones",
                "band_id": str(self.origin_band.pk),
                "zone_ids": [
                    str(self.origin.pk),
                    str(self.destination.pk),
                    str(self.reverse_zone.pk),
                ],
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(OperationalZone.objects.filter(pricing_band=self.origin_band).count(), 3)

    def test_unassigned_filter_and_clear_assignment(self):
        pricing_bands.assign_zones(
            actor=self.admin, zone_ids=[str(self.origin.pk)], band_id=self.destination_band.pk
        )
        self.client.force_login(self.admin)
        response = self.client.get(reverse("pricing-bands"), {"unassigned": "1"})
        self.assertNotContains(response, self.origin.name)
        pricing_bands.assign_zones(actor=self.admin, zone_ids=[str(self.origin.pk)])
        self.origin.refresh_from_db()
        self.assertIsNone(self.origin.pricing_band_id)

    def test_client_users_cannot_access_band_admin(self):
        self.client.force_login(self.client_user)
        self.assertEqual(self.client.get(reverse("pricing-bands")).status_code, 403)
        self.assertEqual(self.client.get(reverse("band-matrix")).status_code, 403)

    def test_matrix_displays_fallback_and_exact_route_overrides(self):
        self.rate(amount="55")
        self.exact(amount="50")
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("band-matrix")), "exact route override")
        self.assertContains(
            self.client.get(reverse("band-matrix"), {"organization": self.organization.pk}),
            "General fallback",
        )

    def test_distance_provider_is_not_called(self):
        class Provider:
            def __call__(self, *args, **kwargs):
                raise AssertionError("Distance pricing is future work.")

        self.assertIsNone(pricing.prepare_quote(self.delivery(), distance_provider=Provider()))
