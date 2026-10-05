"""Idempotent importer for the approved General Dpot Standard price card."""

import csv
from decimal import Decimal
from pathlib import Path

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .area_directory import matches, normalize_name
from .locations import save_zone
from .models import PriceCard, User
from .price_cards import current_rates, save_card, save_rate


def approved_rows():
    path = Path(__file__).resolve().parent.parent / "data" / "elorah_approved_prices.csv"
    with path.open(encoding="utf-8-sig", newline="") as source:
        rows = list(csv.DictReader(source))
    if len(rows) != 114 or len({normalize_name(row["to_area"]) for row in rows}) != 114:
        raise ValidationError("The approved Dpot dataset must contain 114 distinct destinations.")
    if sum(bool(row["bike_price"]) for row in rows) != 114:
        raise ValidationError("The approved dataset must contain 114 Bike prices.")
    if sum(bool(row["car_price"]) for row in rows) != 79:
        raise ValidationError("The approved dataset must contain 79 Car prices.")
    for row in rows:
        if row["from_area"] != "East Legon" or row["service"] != "standard":
            raise ValidationError("Only East Legon / Standard terms are approved.")
        for vehicle in ("bike", "car"):
            raw = row[f"{vehicle}_price"]
            if raw:
                amount = Decimal(raw)
                if not amount.is_finite() or amount < 0 or amount.as_tuple().exponent < -2:
                    raise ValidationError("Invalid approved price.")
    for number in range(1, 26):
        row = next(row for row in rows if row["to_area"] == f"Tema Community {number}")
        if row["bike_price"] != "100" or row["car_price"]:
            raise ValidationError("Tema community terms must be Bike 100 / Car unset.")
    return rows


def _plan():
    rows = approved_rows()
    zone_map = {}
    for row in rows:
        found = list(matches(row["to_area"]))
        if len(found) > 1 or (found and not found[0].active):
            raise ValidationError(f"Area {row['to_area']} is ambiguous or inactive.")
        if found:
            zone_map[row["to_area"]] = found[0]
    origin_matches = list(matches("East Legon"))
    if len(origin_matches) > 1 or (origin_matches and not origin_matches[0].active):
        raise ValidationError("East Legon is ambiguous or inactive.")

    cards = list(PriceCard.objects.filter(organization__isnull=True).order_by("pk"))
    if len(cards) > 1:
        raise ValidationError("Multiple General PriceCards exist; resolve before importing.")
    card = cards[0] if cards else None
    if card and (card.scope_key != "general" or not card.active):
        raise ValidationError("The existing General PriceCard has unexpected scope or is inactive.")

    if "East Legon" in zone_map:
        origin = zone_map["East Legon"]
    elif origin_matches:
        origin = origin_matches[0]
    else:
        origin = None

    current = list(current_rates().filter(card=card)) if card else []
    now = timezone.now()
    pending = []
    identical = 0
    identical_by_vehicle = {"bike": 0, "car": 0}
    conflicts = []
    for row in rows:
        destination = zone_map.get(row["to_area"])
        for vehicle in ("bike", "car"):
            raw = row[f"{vehicle}_price"]
            matches_route = [
                rate for rate in current
                if rate.vehicle == vehicle and rate.service == "standard"
                and origin is not None and destination is not None
                and (
                    (rate.origin_id == origin.pk and rate.destination_id == destination.pk)
                    or (rate.bidirectional and rate.origin_id == destination.pk
                        and rate.destination_id == origin.pk)
                )
            ]
            effective = [
                rate for rate in matches_route
                if rate.active and rate.effective_at <= now
                and (rate.expires_at is None or rate.expires_at > now)
            ]
            if not raw:
                if effective:
                    conflicts.append(
                        f"{destination.name} / {vehicle}: approved price is unset but an active rate exists."
                    )
                continue
            amount = Decimal(raw)
            if matches_route:
                if (
                    len(matches_route) == 1 and matches_route[0].active
                    and not matches_route[0].bidirectional and matches_route[0].amount == amount
                    and matches_route[0].effective_at <= now
                    and matches_route[0].expires_at is None
                ):
                    identical += 1
                    identical_by_vehicle[vehicle] += 1
                else:
                    conflicts.append(
                        f"{destination.name} / {vehicle}: existing General rate differs in amount, direction, status or dates."
                    )
            else:
                pending.append((row["to_area"], vehicle, amount))
    missing_origin_area = origin is None and "East Legon" not in zone_map
    return {
        "rows": rows,
        "zones": zone_map,
        "origin": origin,
        "card": card,
        "pending": pending,
        "identical": identical,
        "identical_by_vehicle": identical_by_vehicle,
        "conflicts": conflicts,
        "areas_to_create": len(rows) - len(zone_map) + int(missing_origin_area),
    }


def preview_general_dpot():
    plan = _plan()
    bike_created = sum(vehicle == "bike" for _, vehicle, _ in plan["pending"])
    car_created = sum(vehicle == "car" for _, vehicle, _ in plan["pending"])
    current_bike = sum(rate.vehicle == "bike" and rate.service == "standard" and rate.active for rate in (current_rates().filter(card=plan["card"]) if plan["card"] else []))
    current_car = sum(rate.vehicle == "car" and rate.service == "standard" and rate.active for rate in (current_rates().filter(card=plan["card"]) if plan["card"] else []))
    return {
        "general_card_found": plan["card"] is not None,
        "general_card_name": plan["card"].name if plan["card"] else None,
        "existing_relevant_areas": len(plan["zones"]),
        "areas_to_create": plan["areas_to_create"],
        "bike_rates_to_create": bike_created,
        "car_rates_to_create": car_created,
        "identical_rates_to_skip": plan["identical"],
        "identical_bike_rates": plan["identical_by_vehicle"]["bike"],
        "identical_car_rates": plan["identical_by_vehicle"]["car"],
        "conflicts": plan["conflicts"],
        "expected_final_bike_rate_count": current_bike + bike_created,
        "expected_final_car_rate_count": current_car + car_created,
    }


@transaction.atomic
def import_general_dpot(*, actor):
    if actor.role != "admin" or not actor.is_active:
        raise ValidationError("An active Admin account is required.")
    list(User.objects.select_for_update().filter(role="admin", is_active=True).order_by("pk"))
    plan = _plan()
    if plan["conflicts"]:
        raise ValidationError(plan["conflicts"])

    counts = {
        "card_created": 0,
        "areas_created": 0,
        "areas_reused": len(plan["zones"]),
        "bike_created": 0,
        "car_created": 0,
        "rates_skipped": plan["identical"],
        "bike_skipped": plan["identical_by_vehicle"]["bike"],
        "car_skipped": plan["identical_by_vehicle"]["car"],
        "conflicts": 0,
    }
    card = plan["card"]
    if card is None:
        card = save_card(actor=actor, name="General Dpot Standard Price Card")
        counts["card_created"] = 1
    zones = dict(plan["zones"])
    for row in plan["rows"]:
        area = row["to_area"]
        if area not in zones:
            zones[area] = save_zone(actor=actor, name=area, active=True, boundary=[])
            counts["areas_created"] += 1
    origin = zones.get("East Legon") or plan["origin"]
    if origin is None:
        origin_matches = list(matches("East Legon"))
        if len(origin_matches) == 1 and origin_matches[0].active:
            origin = origin_matches[0]
        else:
            origin = save_zone(actor=actor, name="East Legon", active=True, boundary=[])
            counts["areas_created"] += 1
    elif "East Legon" not in zones:
        counts["areas_reused"] += 1

    for destination_name, vehicle, amount in plan["pending"]:
        save_rate(
            actor=actor,
            card_id=card.pk,
            origin=origin,
            destination=zones[destination_name],
            vehicle=vehicle,
            service="standard",
            amount=amount,
            bidirectional=False,
            active=True,
            effective_at=timezone.now(),
            expires_at=None,
            reason="Approved General Dpot Standard rate card; East Legon outbound, 2026-10-05.",
        )
        counts[f"{vehicle}_created"] += 1
    counts["card_id"] = str(card.pk)
    counts["card_name"] = card.name
    counts["final_current_rates"] = current_rates().filter(card=card).count()
    counts["final_bike_rates"] = current_rates().filter(card=card, vehicle="bike", service="standard", active=True).count()
    counts["final_car_rates"] = current_rates().filter(card=card, vehicle="car", service="standard", active=True).count()
    return counts
