"""Coordinates and internal zone authority are independent of provider locality labels."""

import math
from decimal import Decimal, InvalidOperation

from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Delivery, Location, OperationalZone
from .services import emit_event


def coordinates(lat, lng):
    try:
        lat, lng = Decimal(str(lat)), Decimal(str(lng))
        if (
            not lat.is_finite()
            or not lng.is_finite()
            or not -90 <= lat <= 90
            or not -180 <= lng <= 180
        ):
            raise ValueError
        return lat.quantize(Decimal("0.0000001")), lng.quantize(Decimal("0.0000001"))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationError("Choose valid latitude and longitude coordinates.") from exc


def validate_boundary(boundary):
    if not boundary:
        return []
    if (
        not isinstance(boundary, list)
        or not 4 <= len(boundary) <= 200
        or boundary[0] != boundary[-1]
    ):
        raise ValidationError(
            "Use a closed polygon with 4 to 200 [longitude, latitude] points, or leave it empty for manual assignment."
        )
    for point in boundary:
        if not isinstance(point, list) or len(point) != 2:
            raise ValidationError("Each boundary point must be [longitude, latitude].")
        coordinates(point[1], point[0])
    area = sum(
        float(a[0]) * float(b[1]) - float(b[0]) * float(a[1])
        for a, b in zip(boundary, boundary[1:])
    )
    if math.isclose(area, 0, abs_tol=1e-12):
        raise ValidationError("The boundary must enclose an area.")
    return boundary


def contains(boundary, lat, lng):
    x, y, inside = float(lng), float(lat), False
    for a, b in zip(boundary, boundary[1:]):
        x1, y1, x2, y2 = float(a[0]), float(a[1]), float(b[0]), float(b[1])
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def resolve_zone(lat, lng):
    matches = [
        z
        for z in OperationalZone.objects.filter(active=True).iterator()
        if z.boundary and contains(z.boundary, lat, lng)
    ]
    # Overlaps are deliberately unresolved; an Admin must choose, not an arbitrary locality match.
    return matches[0] if len(matches) == 1 else None


def signed_location(actor, data):
    lat, lng = coordinates(data.get("latitude"), data.get("longitude"))
    safe = {
        key: str(data.get(key, ""))[:limit]
        for key, limit in [("address", 255), ("area", 100), ("place_id", 255), ("provider", 40)]
    }
    safe.update(latitude=str(lat), longitude=str(lng), actor=actor.pk)
    return signing.dumps(safe, salt="confirmed-location", compress=True)


def read_location(actor, token):
    try:
        data = signing.loads(token, salt="confirmed-location", max_age=7200)
    except signing.BadSignature as exc:
        raise ValidationError("Your confirmed pin has expired. Confirm it again.") from exc
    if data.get("actor") != actor.pk:
        raise PermissionDenied
    coordinates(data["latitude"], data["longitude"])
    return data


def booking_location(actor, values, prefix):
    from .models import OperationalZone

    common = {
        "display_address": values[f"{prefix}_address"],
        "area": values[f"{prefix}_area"],
        "google_maps_url": values.get(f"{prefix}_google_maps_url", ""),
        "landmark": values.get(f"{prefix}_landmark", ""),
        "instructions": values[f"{prefix}_instructions"],
    }
    selected_zone = values.get(f"{prefix}_zone")
    if selected_zone:
        try:
            zone = OperationalZone.objects.get(pk=getattr(selected_zone, "pk", selected_zone), active=True)
        except (OperationalZone.DoesNotExist, ValidationError, ValueError, TypeError) as exc:
            raise ValidationError("Choose an active Design-Pot operational zone.") from exc
        common["zone"] = zone
    token = values.get(f"{prefix}_token")
    if token:
        data = read_location(actor, token)
        lat, lng = coordinates(data["latitude"], data["longitude"])
        common.update(
            latitude=lat,
            longitude=lng,
            zone=common.get("zone") or resolve_zone(lat, lng),
            confirmed_at=timezone.now(),
            source="confirmed_pin",
            provider=data["provider"],
            place_id=data["place_id"],
        )
    location = Location(**common)
    location.full_clean()
    location.save()
    return location


@transaction.atomic
def save_zone(*, actor, name, active, boundary, zone_id=None):
    if actor.role != "admin" or not actor.is_active:
        raise PermissionDenied
    boundary = validate_boundary(boundary)
    zone = (
        OperationalZone.objects.select_for_update().get(pk=zone_id)
        if zone_id
        else OperationalZone()
    )
    before = (
        {"name": zone.name, "active": zone.active, "boundary": zone.boundary} if zone_id else None
    )
    zone.name, zone.active, zone.boundary = name, active, boundary
    zone.full_clean()
    zone.save()
    emit_event(
        actor=actor,
        kind="zone.updated",
        title="Operational zone updated",
        message="Zone saved.",
        recipients=[],
        payload={
            "zone_id": str(zone.pk),
            "before": before,
            "after": {"name": name, "active": active, "boundary": boundary},
        },
    )
    return zone


@transaction.atomic
def assign_zone(*, actor, delivery_id, side, zone_id, reason):
    if actor.role != "admin" or not actor.is_active:
        raise PermissionDenied
    if side not in ("pickup", "destination") or not reason.strip():
        raise ValidationError("Choose pickup or destination and provide a correction reason.")
    from .models import Job

    reference = Delivery.objects.only("job_id").get(pk=delivery_id)
    Job.objects.select_for_update().get(pk=reference.job_id)
    delivery = (
        Delivery.objects.select_for_update()
        .select_related("job", "pickup", "destination")
        .get(pk=delivery_id)
    )
    zone = OperationalZone.objects.get(pk=zone_id, active=True)
    old = getattr(delivery, side)
    new = Location.objects.create(
        **{
            f.name: getattr(old, f.name)
            for f in Location._meta.fields
            if f.name not in ("id", "created_at", "zone")
        },
        zone=zone,
    )
    setattr(delivery, side, new)
    delivery.save(update_fields=[side])
    if delivery.status == "requested":
        from .pricing import prepare_quote

        delivery.job.revision += 1
        delivery.job.save(update_fields=["revision"])
        prepare_quote(delivery)
    emit_event(
        actor=actor,
        kind="location.zone_corrected",
        delivery=delivery,
        title="Operational zone corrected",
        message="Zone updated.",
        recipients=[],
        payload={
            "old_location": str(old.pk),
            "new_location": str(new.pk),
            "zone": str(zone.pk),
            "side": side,
            "reason": reason,
        },
    )
    return new
