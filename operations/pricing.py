"""Development-only pricing. No inferred or fabricated road-distance values."""

import hashlib
import json
import uuid
from datetime import timedelta
from decimal import ROUND_CEILING, Decimal
from typing import Protocol

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import PricingConfiguration, PricingVersion, Quote


class DistanceProvider(Protocol):
    def road_kilometres(self, pickup, destination) -> Decimal | None: ...


class UnavailableRoadDistance:
    def road_kilometres(self, pickup, destination) -> None:
        return None


def validate_rules(rules):
    try:
        values = [
            Decimal(str(rules[vehicle][field]))
            for vehicle in ("bike", "car")
            for field in ("base", "minimum", "per_km")
        ]
        values += [Decimal(str(rules["premium_surcharge"])), Decimal(str(rules["rounding"]))]
        if any(not value.is_finite() or value < 0 or value > 10000 for value in values):
            raise ValueError
        if values[-1] <= 0 or not 1 <= int(rules["quote_minutes"]) <= 1440:
            raise ValueError
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise ValidationError(
            "Pricing settings are invalid. Check rates, rounding and expiry."
        ) from exc
    return rules


def version_for(rules, is_sample=True):
    validate_rules(rules)
    digest = hashlib.sha256(json.dumps(rules, sort_keys=True).encode()).hexdigest()[:20]
    version, _ = PricingVersion.objects.get_or_create(
        label=f"{'development' if is_sample else 'approved'}-{digest}",
        defaults={"rules": rules, "is_sample": is_sample, "currency": "GHS"},
    )
    return version


def active_version():
    configured = PricingConfiguration.objects.select_related("current_version").first()
    return configured.current_version if configured else version_for(settings.DEVELOPMENT_PRICING)


def _distance_quote(delivery, distance_provider=None, mode="DISTANCE_BASED"):
    version = active_version()
    if version.is_sample and not settings.ALLOW_DEVELOPMENT_PRICING:
        raise ValidationError(
            "Development pricing is disabled. An approved pricing service is required before accepting bookings."
        )
    rules = validate_rules(version.rules)
    vehicle = rules[delivery.vehicle_type]
    from .maps import RoadDistance

    distance = (distance_provider or RoadDistance(delivery.vehicle_type)).road_kilometres(
        delivery.pickup, delivery.destination
    )
    if distance is None:
        from .maps import MapUnavailable

        raise MapUnavailable("Road distance is unavailable; dispatch must provide a quote.")
    distance = Decimal(str(distance))
    if not distance.is_finite() or distance < 0:
        from .maps import MapUnavailable

        raise MapUnavailable("A valid road distance could not be obtained.")
    base = Decimal(vehicle["base"])
    distance_charge = Decimal(0) if distance is None else distance * Decimal(vehicle["per_km"])
    minimum_adjustment = max(Decimal(0), Decimal(vehicle["minimum"]) - base - distance_charge)
    surcharge = Decimal(rules["premium_surcharge"]) if delivery.service == "premium" else Decimal(0)
    subtotal = base + distance_charge + minimum_adjustment + surcharge
    step = Decimal(rules["rounding"])
    amount = (subtotal / step).quantize(Decimal("1"), rounding=ROUND_CEILING) * step
    breakdown = {
        "source": "GENERAL_DISTANCE_RATE",
        "general_mode": mode,
        "development": version.is_sample,
        "base": str(base),
        "minimum_adjustment": str(minimum_adjustment),
        "distance_km": None if distance is None else str(distance),
        "distance_charge": str(distance_charge) if distance is not None else None,
        "distance_status": "unavailable" if distance is None else "provided",
        "per_km_rate": vehicle["per_km"],
        "premium_surcharge": str(surcharge),
        "rounding_adjustment": str(amount - subtotal),
        "revision": delivery.job.revision,
        "delivery": delivery_snapshot(delivery),
    }
    quote = Quote.objects.create(
        job=delivery.job,
        pricing_version=version,
        amount=amount,
        currency=version.currency,
        breakdown=breakdown,
        expires_at=timezone.now() + timedelta(minutes=int(rules["quote_minutes"])),
    )
    delivery.job.current_quote = quote
    delivery.job.quote_requested = False
    delivery.job.save(update_fields=["current_quote", "quote_requested"])
    return quote


def delivery_snapshot(delivery):
    return {
        "recipient": delivery.recipient_name,
        "pickup": delivery.pickup.display_address,
        "destination": delivery.destination.display_address,
        "pickup_area": delivery.pickup.area,
        "destination_area": delivery.destination.area,
        "pickup_landmark": delivery.pickup.landmark,
        "destination_landmark": delivery.destination.landmark,
        "pickup_instructions": delivery.pickup.instructions,
        "destination_instructions": delivery.destination.instructions,
        "pickup_google_maps_url": delivery.pickup.google_maps_url,
        "destination_google_maps_url": delivery.destination.google_maps_url,
        "service": delivery.service,
        "vehicle": delivery.vehicle_type,
        "cod_product_value": str(delivery.cod_amount),
        "service_label": delivery.get_service_display(),
        "vehicle_label": delivery.get_vehicle_type_display(),
        "package": delivery.package_description,
        "payment_method": delivery.payment_method,
        "window": f"{delivery.window_start:%d %b %Y, %H:%M} - {delivery.window_end:%d %b %Y, %H:%M} GMT"
        if delivery.window_start and delivery.window_end
        else "",
        "pickup_location": str(delivery.pickup_id),
        "destination_location": str(delivery.destination_id),
        "origin_zone": str(delivery.pickup.zone_id) if delivery.pickup.zone_id else None,
        "destination_zone": str(delivery.destination.zone_id)
        if delivery.destination.zone_id
        else None,
    }


def priced_quote(delivery, *, amount, source, rules, mode, rate=None, actor=None):
    version, _ = PricingVersion.objects.get_or_create(
        label=f"{source.lower()}-{rate.pk if rate else delivery.job_id}"
        if not actor
        else f"manual-{uuid.uuid4()}",
        defaults={"rules": rules, "currency": "GHS", "is_sample": False},
    )
    quote = Quote.objects.create(
        job=delivery.job,
        pricing_version=version,
        source=source,
        amount=amount,
        expires_at=timezone.now() + timedelta(minutes=30),
        breakdown={
            "development": False,
            "source": source,
            "general_mode": mode,
            "fixed_amount": str(amount),
            "delivery": delivery_snapshot(delivery),
            "revision": delivery.job.revision,
            "rate_id": str(rate.pk) if rate else None,
            "rate_version": rate.version if rate else None,
            "card_id": str(rate.card_id) if rate else None,
            "adjustments": [],
            "distance_km": None,
            "admin_actor": actor.pk if actor else None,
        },
    )
    delivery.job.current_quote = quote
    delivery.job.quote_requested = False
    delivery.job.save(update_fields=["current_quote", "quote_requested"])
    return quote


def prepare_quote(delivery, distance_provider=None):
    """Resolve client terms, then general cards; unmatched routes need Admin review."""
    from .models import User
    from .price_cards import matching_rate
    from .services import emit_event

    contract = matching_rate(delivery, delivery.job.organization)
    rate = contract or matching_rate(delivery, None)
    if rate:
        return priced_quote(
            delivery,
            amount=rate.amount,
            source="CLIENT_CONTRACT_RATE" if contract else "GENERAL_FIXED_RATE",
            rules={"rate_id": str(rate.pk), "amount": str(rate.amount)},
            mode="PRICE_CARD",
            rate=rate,
        )
    delivery.job.current_quote = None
    delivery.job.quote_requested = True
    delivery.job.save(update_fields=["current_quote", "quote_requested"])
    emit_event(
        actor=delivery.job.created_by,
        kind="quote.review_requested",
        delivery=delivery,
        title="Price confirmation required",
        message="Dispatch will review the delivery price.",
        recipients=list(User.objects.filter(role="admin", is_active=True)),
        payload={"pricing": "price_card_unmatched"},
    )
    return None


@transaction.atomic
def manual_quote(
    *, actor, delivery_id, amount, reason, expected_revision, request_key,
    save_as_price_card=False, card_scope="general",
):
    from .models import BulkBooking, Delivery, Job, QuoteConfirmation, User
    from .price_cards import admin
    from .services import emit_event
    from .workflow import audiences, replay, request_uuid

    admin(actor)
    reference = Delivery.objects.only("job_id").get(pk=delivery_id)
    job = Job.objects.select_for_update().get(pk=reference.job_id)
    delivery = (
        Delivery.objects.select_for_update()
        .select_related("pickup", "destination", "job")
        .get(pk=delivery_id)
    )
    event = replay(actor, delivery, request_key, "quote.manual_created")
    if event:
        return Quote.objects.get(pk=event.payload["quote_id"])
    if QuoteConfirmation.objects.filter(job=job).exists() or delivery.status != "requested":
        raise ValidationError("Confirmed quotes cannot be replaced.")
    if job.revision != expected_revision:
        raise ValidationError("The booking changed. Review the current delivery first.")
    amount = Decimal(str(amount))
    if (
        not amount.is_finite()
        or amount < 0
        or amount > Decimal("9999999999.99")
        or amount != amount.quantize(Decimal("0.01"))
        or not reason.strip()
    ):
        raise ValidationError("Enter a valid amount and a reason for the manual quote.")
    if save_as_price_card:
        if card_scope not in ("general", "client"):
            raise ValidationError("Choose a valid Price Card scope.")
        if not delivery.pickup.zone_id or not delivery.destination.zone_id:
            raise ValidationError("Assign both pricing areas before saving this route to Price Cards.")
        from . import price_cards

        organization = job.organization if card_scope == "client" else None
        card = price_cards.get_or_create_scoped_card(
            actor=actor,
            organization=organization,
            name=(f"{organization.name} delivery rates" if organization else "General delivery rates"),
        )
        if not card.active:
            raise ValidationError("The selected Price Card is inactive.")
        price_cards.save_route_price(
            actor=actor,
            card_id=card.pk,
            origin_id=delivery.pickup.zone_id,
            destination_id=delivery.destination.zone_id,
            bike_price=amount if delivery.vehicle_type == "bike" else None,
            car_price=amount if delivery.vehicle_type == "car" else None,
            bidirectional=True,
            service=delivery.service,
            active=True,
            reason=f"Admin quote {delivery.pk}: {reason.strip()}",
            allow_update=True,
        )
    config = PricingConfiguration.objects.first()
    delivery.job.revision += 1
    delivery.job.save(update_fields=["revision"])
    quote = priced_quote(
        delivery,
        amount=amount,
        source="ADMIN_MANUAL_QUOTE",
        rules={"reason": reason, "actor": actor.pk},
        mode=config.mode if config else "DISTANCE_BASED",
        actor=actor,
    )
    recipients = audiences(delivery, admins=False) if not job.bulk_booking_id else []
    emit_event(
        actor=actor,
        kind="quote.manual_created",
        delivery=delivery,
        title="Your delivery quote is ready",
        message="Review your delivery price before confirming.",
        recipients=recipients,
        request_key=request_uuid(request_key),
        payload={"quote_id": str(quote.pk), "amount": str(amount), "reason": reason},
    )
    if job.bulk_booking_id:
        batch = BulkBooking.objects.select_for_update().get(pk=job.bulk_booking_id)
        pending = Job.objects.filter(bulk_booking=batch, quote_requested=True).exists()
        if not pending and not batch.quote_ready_notified:
            ready_count = Job.objects.filter(
                bulk_booking=batch, current_quote__source="ADMIN_MANUAL_QUOTE",
                quoteconfirmation__isnull=True,
            ).count()
            batch.quote_ready_notified = True
            batch.save(update_fields=["quote_ready_notified"])
            emit_event(
                actor=actor,
                kind="bulk.quote_ready",
                title=f"{ready_count} delivery quote{'s' if ready_count != 1 else ''} ready",
                message="Review the new delivery prices in your bulk request.",
                recipients=list(
                    User.objects.filter(
                        role="client", organization_id=batch.organization_id, is_active=True
                    )
                ),
                payload={"bulk_booking_id": str(batch.pk), "count": ready_count},
            )
    return quote
