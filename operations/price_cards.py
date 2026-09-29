"""Append-only commercial terms, explicit matching and auditable administration."""

import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F, OuterRef, Q, Subquery
from django.utils import timezone

from .models import FixedRate, OperationalZone, PriceCard, PricingConfiguration, User
from .services import emit_event


def admin(actor):
    if not actor.is_active or actor.role != "admin":
        raise PermissionDenied


def current_rates():
    latest = (
        FixedRate.objects.filter(rate_key=OuterRef("rate_key"))
        .order_by("-version")
        .values("pk")[:1]
    )
    return FixedRate.objects.annotate(latest_id=Subquery(latest)).filter(pk=F("latest_id"))


def matching_rate(delivery, organization):
    origin, destination = delivery.pickup.zone_id, delivery.destination.zone_id
    if not origin or not destination:
        return None
    now = timezone.now()
    matches = (
        current_rates()
        .filter(
            card__active=True,
            card__organization=organization,
            active=True,
            origin__active=True,
            destination__active=True,
            vehicle=delivery.vehicle_type,
            service=delivery.service,
            effective_at__lte=now,
        )
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        .filter(
            Q(origin_id=origin, destination_id=destination)
            | Q(bidirectional=True, origin_id=destination, destination_id=origin)
        )
    )
    matches = list(matches.select_related("card")[:2])
    if len(matches) > 1:
        raise ValidationError("Overlapping price-card terms need Admin review.")
    return matches[0] if matches else None


@transaction.atomic
def save_card(*, actor, name, organization=None, active=True, card_id=None):
    admin(actor)
    # Serialize scope creation too: the general card has one stable scope key.
    list(User.objects.select_for_update().filter(role="admin", is_active=True).order_by("pk"))
    card = (
        PriceCard.objects.select_for_update().get(pk=card_id)
        if card_id
        else PriceCard(organization=organization)
    )
    if card_id and card.organization_id != (organization.pk if organization else None):
        raise ValidationError(
            "A card's client cannot be changed. Create the appropriate client card."
        )
    card.name, card.active = name, active
    card.scope_key = f"client:{organization.pk}" if organization else "general"
    card.full_clean()
    card.save()
    emit_event(
        actor=actor,
        kind="pricing.card_saved",
        title="Price card updated",
        message="Price card saved.",
        recipients=[],
        payload={
            "card": str(card.pk),
            "name": name,
            "organization": str(card.organization_id),
            "active": active,
        },
    )
    return card


@transaction.atomic
def get_or_create_scoped_card(*, actor, organization, name):
    admin(actor)
    list(User.objects.select_for_update().filter(role="admin", is_active=True).order_by("pk"))
    scope_key = f"client:{organization.pk}" if organization else "general"
    card = PriceCard.objects.select_for_update().filter(scope_key=scope_key).first()
    if card:
        return card
    return save_card(actor=actor, name=name, organization=organization)


@transaction.atomic
def save_rate(*, actor, card_id, expected_version=None, rate_key=None, **values):
    admin(actor)
    card = PriceCard.objects.select_for_update().get(pk=card_id)
    previous = (
        FixedRate.objects.filter(rate_key=rate_key, card=card).order_by("-version").first()
        if rate_key
        else None
    )
    if rate_key and (not previous or previous.version != expected_version):
        raise ValidationError("This rate has changed. Reload before saving.")
    allowed = {
        "origin",
        "destination",
        "vehicle",
        "service",
        "amount",
        "bidirectional",
        "active",
        "effective_at",
        "expires_at",
        "reason",
    }
    if set(values) - allowed:
        raise ValidationError("Unexpected rate fields.")
    if values.get("bidirectional") and values["destination"].pk.int < values["origin"].pk.int:
        values["origin"], values["destination"] = values["destination"], values["origin"]
    rate = FixedRate(
        card=card,
        actor=actor,
        rate_key=previous.rate_key if previous else uuid.uuid4(),
        version=previous.version + 1 if previous else 1,
        **values,
    )
    rate.full_clean()
    if not rate.reason.strip():
        raise ValidationError("Record the reason for these commercial terms.")
    if rate.active:
        pairs = Q(origin=rate.origin, destination=rate.destination)
        if rate.bidirectional:
            pairs |= Q(origin=rate.destination, destination=rate.origin)
        else:
            pairs |= Q(bidirectional=True, origin=rate.destination, destination=rate.origin)
        overlap = (
            current_rates()
            .filter(card=card, active=True, vehicle=rate.vehicle, service=rate.service)
            .exclude(rate_key=rate.rate_key)
            .filter(pairs)
        )
        overlap = overlap.filter(Q(expires_at__isnull=True) | Q(expires_at__gt=rate.effective_at))
        if rate.expires_at:
            overlap = overlap.filter(effective_at__lt=rate.expires_at)
        if overlap.exists():
            raise ValidationError(
                "An active rate already covers this route, vehicle, service and time window. Edit it or use non-overlapping effective dates."
            )
    rate.save()
    emit_event(
        actor=actor,
        kind="pricing.rate_versioned",
        title="Price card rate updated",
        message="New rate version saved.",
        recipients=[],
        payload={
            "rate": str(rate.pk),
            "rate_key": str(rate.rate_key),
            "version": rate.version,
            "card": str(card.pk),
            "amount": str(rate.amount),
            "active": rate.active,
            "reason": rate.reason,
        },
    )
    return rate


@transaction.atomic
def save_route_price(
    *, actor, card_id, origin_id, destination_id, bike_price=None, car_price=None,
    bidirectional=True, service="standard", active=True, effective_at=None,
    expires_at=None, reason="", editing=False,
    original_bidirectional=None,
    allow_update=False,
):
    """Save the simple bike/car route row as immutable per-vehicle rate versions."""
    admin(actor)
    card = PriceCard.objects.select_for_update().get(pk=card_id)
    origin = OperationalZone.objects.get(pk=origin_id, active=True)
    destination = OperationalZone.objects.get(pk=destination_id, active=True)
    if bidirectional and destination.pk.int < origin.pk.int:
        origin, destination = destination, origin
    source_bidirectional = (
        original_bidirectional if original_bidirectional is not None else bidirectional
    )
    if bike_price is None and car_price is None and not editing:
        raise ValidationError("Enter a Bike price, a Car price, or both.")
    effective_at = effective_at or timezone.now()
    reason = reason.strip() or "Updated through the route price card."
    for vehicle, amount in (("bike", bike_price), ("car", car_price)):
        pair = Q(origin=origin, destination=destination)
        if source_bidirectional:
            pair |= Q(origin=destination, destination=origin)
        previous_rates = current_rates().filter(
            card=card, vehicle=vehicle, service=service,
        ).filter(pair)
        if not (allow_update and bidirectional):
            previous_rates = previous_rates.filter(bidirectional=source_bidirectional)
        previous_matches = list(previous_rates.order_by("pk")[:2])
        if len(previous_matches) > 1:
            raise ValidationError(
                "Conflicting directional rates cover this route. Review the existing card first."
            )
        previous = previous_matches[0] if previous_matches else None
        if previous and not editing and not allow_update:
            raise ValidationError(
                "A price already exists for this route, vehicle and service. Edit the existing route."
            )
        if amount is None:
            if editing and previous and previous.active:
                save_rate(
                    actor=actor, card_id=card.pk, rate_key=previous.rate_key,
                    expected_version=previous.version, origin=origin, destination=destination,
                    vehicle=vehicle, service=service, amount=previous.amount,
                    bidirectional=bidirectional, active=False,
                    effective_at=effective_at, expires_at=expires_at, reason=reason,
                )
            continue
        save_rate(
            actor=actor, card_id=card.pk,
            rate_key=previous.rate_key if previous else None,
            expected_version=previous.version if previous else None,
            origin=origin, destination=destination, vehicle=vehicle, service=service,
            amount=amount, bidirectional=bidirectional, active=active,
            effective_at=effective_at, expires_at=expires_at, reason=reason,
        )


@transaction.atomic
def change_mode(*, actor, mode, fallback):
    admin(actor)
    if mode not in ("DISTANCE_BASED", "FIXED_PRICE_CARD") or fallback not in (
        "FALLBACK_TO_DISTANCE_PRICING",
        "REQUIRE_ADMIN_QUOTE",
    ):
        raise ValidationError("Choose a supported pricing mode and fallback.")
    from .pricing import active_version

    config, _ = PricingConfiguration.objects.get_or_create(
        pk=1, defaults={"current_version": active_version()}
    )
    config = PricingConfiguration.objects.select_for_update().get(pk=config.pk)
    before = {"mode": config.mode, "fallback": config.fallback}
    config.mode, config.fallback = mode, fallback
    config.save(update_fields=["mode", "fallback"])
    emit_event(
        actor=actor,
        kind="pricing.mode_changed",
        title="General pricing changed",
        message="General pricing mode updated.",
        recipients=[],
        payload={"before": before, "mode": mode, "fallback": fallback},
    )
    return config
