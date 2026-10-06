"""Admin services for shared pricing bands and their scoped Standard matrix."""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from .models import BandRate, OperationalZone, Organization, PricingBand
from .price_cards import admin
from .services import emit_event


@transaction.atomic
def save_band(*, actor, band_id=None, name, active=True, display_order=0):
    admin(actor)
    band = PricingBand.objects.select_for_update().get(pk=band_id) if band_id else PricingBand()
    band.name = name.strip()
    band.active = active
    band.display_order = display_order
    band.full_clean()
    band.save()
    emit_event(
        actor=actor,
        kind="pricing.band_saved",
        title="Pricing band updated",
        message="Pricing band settings saved.",
        recipients=[],
        payload={"band_id": str(band.pk), "name": band.name, "active": band.active},
    )
    return band


@transaction.atomic
def assign_zones(*, actor, zone_ids, band_id=None):
    admin(actor)
    unique_ids = list(dict.fromkeys(zone_ids))
    if not unique_ids:
        raise ValidationError("Select at least one existing area.")
    band = PricingBand.objects.get(pk=band_id) if band_id else None
    if band and not band.active:
        raise ValidationError("Choose an active Pricing Band.")
    zones = list(OperationalZone.objects.select_for_update().filter(pk__in=unique_ids))
    if len(zones) != len(unique_ids):
        raise ValidationError("One or more selected areas no longer exist. Refresh and try again.")
    OperationalZone.objects.filter(pk__in=unique_ids).update(pricing_band=band)
    emit_event(
        actor=actor,
        kind="pricing.zone_bands_assigned",
        title="Area bands updated",
        message=f"Updated Pricing Band assignment for {len(zones)} existing areas.",
        recipients=[],
        payload={
            "zone_ids": [str(zone.pk) for zone in zones],
            "pricing_band_id": str(band.pk) if band else None,
        },
    )
    return len(zones)


@transaction.atomic
def save_band_rate(
    *, actor, organization_id, origin_id, destination_id, vehicle, amount, bidirectional
):
    admin(actor)
    if vehicle not in BandRate.Vehicle.values:
        raise ValidationError("Choose Bike or Car.")
    organization = Organization.objects.get(pk=organization_id) if organization_id else None
    # Lock stable parent rows, including when no matrix entry exists yet. Ordering
    # the pair makes concurrent directional/symmetric edits serialize consistently.
    list(
        PricingBand.objects.select_for_update()
        .filter(pk__in=[origin_id, destination_id])
        .order_by("pk")
    )
    origin = PricingBand.objects.get(pk=origin_id)
    destination = PricingBand.objects.get(pk=destination_id)
    if not origin.active or not destination.active:
        raise ValidationError("Rates can only be configured for active bands.")
    scope_key = f"client:{organization.pk}" if organization else "general"
    amount = Decimal(str(amount)) if amount not in (None, "") else None
    symmetric_origin, symmetric_destination = origin, destination
    if str(symmetric_destination.pk) < str(symmetric_origin.pk):
        symmetric_origin, symmetric_destination = symmetric_destination, symmetric_origin

    pair_rates = list(
        BandRate.objects.select_for_update()
        .filter(scope_key=scope_key, vehicle=vehicle)
        .filter(
            Q(origin=origin, destination=destination) | Q(origin=destination, destination=origin)
        )
        .order_by("pk")
    )
    if bidirectional:
        matching = [rate for rate in pair_rates if rate.bidirectional]
    else:
        matching = [
            rate
            for rate in pair_rates
            if not rate.bidirectional
            and rate.origin_id == origin.pk
            and rate.destination_id == destination.pk
        ]
    if len(matching) > 1:
        raise ValidationError("Overlapping band rates need Admin review before editing.")
    if amount is None:
        current = [rate for rate in matching if rate.active]
        for rate in current:
            rate.active = False
            rate.save(update_fields=["active", "updated_at"])
        if current:
            emit_event(
                actor=actor,
                kind="pricing.band_rate_cleared",
                title="Band price unset",
                message="A Standard band price was cleared.",
                recipients=[],
                payload={
                    "scope": scope_key,
                    "vehicle": vehicle,
                    "origin_band": origin.name,
                    "destination_band": destination.name,
                    "directionality": "symmetric" if bidirectional else "directional",
                },
            )
        return None
    if not amount.is_finite() or amount <= 0 or amount != amount.quantize(Decimal("0.01")):
        raise ValidationError("Enter a positive price to two decimal places, or leave it blank.")

    if bidirectional:
        # A symmetric entry represents both directions; replace either directional
        # entry so a hidden directional override cannot shadow the saved matrix cell.
        for rate in pair_rates:
            if rate.active and not rate.bidirectional:
                rate.active = False
                rate.save(update_fields=["active", "updated_at"])
        origin, destination = symmetric_origin, symmetric_destination

    # BandRate is current matrix configuration, not quote history. Quotes and their
    # pricing versions hold immutable snapshots, so reuse the unique row on edits
    # and reactivation instead of inserting history rows that occupy the same key.
    rate = matching[0] if matching else BandRate()
    rate.scope_key = scope_key
    rate.organization = organization
    rate.origin = origin
    rate.destination = destination
    rate.vehicle = vehicle
    rate.amount = amount
    rate.bidirectional = bidirectional
    rate.active = True
    rate.full_clean()
    rate.save()
    emit_event(
        actor=actor,
        kind="pricing.band_rate_saved",
        title="Band price updated",
        message="Standard band price saved.",
        recipients=[],
        payload={
            "band_rate_id": rate.pk,
            "scope": scope_key,
            "vehicle": vehicle,
            "origin_band": origin.name,
            "destination_band": destination.name,
            "amount": str(amount),
            "bidirectional": bidirectional,
        },
    )
    return rate
