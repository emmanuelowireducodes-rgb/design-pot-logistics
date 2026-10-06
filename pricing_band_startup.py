"""Temporary, explicitly authorized production band apply and read-only verification."""

import os
from decimal import Decimal
from types import SimpleNamespace

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction
from django.utils import timezone

from operations import price_cards
from operations.models import FixedRate, OperationalZone, User
from operations.production_pricing_bands import preview_rollout, protected_state


def exact_rate_report():
    current = price_cards.current_rates().filter(card__scope_key="general", service="standard")
    available = current.filter(active=True, card__active=True, effective_at__lte=timezone.now())
    from django.db.models import Q

    available = available.filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()))
    counts = {vehicle: available.filter(vehicle=vehicle).count() for vehicle in ("bike", "car")}
    print(
        f"CURRENT General Standard exact rates: {counts}; total={sum(counts.values())}", flush=True
    )
    if counts != {"bike": 114, "car": 79}:
        raise CommandError("Current General exact-rate counts changed; no apply permitted.")
    stored = FixedRate.objects.filter(card__scope_key="general", service="standard")
    current_ids = set(current.values_list("pk", flat=True))
    available_ids = set(available.values_list("pk", flat=True))
    extra = stored.exclude(pk__in=available_ids).order_by("vehicle", "rate_key", "version")
    print(
        f"All stored General Standard exact rows: {stored.count()}; non-available rows: {extra.count()}",
        flush=True,
    )
    for row in extra:
        reason = (
            "historical/non-current"
            if row.pk not in current_ids
            else "current but inactive/expired/future"
        )
        print(
            f"EXTRA STORED ROW: id={row.pk}; vehicle={row.vehicle}; rate_key={row.rate_key}; "
            f"version={row.version}; active={row.active}; status={reason}; "
            f"origin={row.origin_id}; destination={row.destination_id}",
            flush=True,
        )


def route_report():
    def delivery(origin, destination, vehicle):
        a = OperationalZone.objects.get(name=origin)
        b = OperationalZone.objects.get(name=destination)
        return SimpleNamespace(
            pickup=SimpleNamespace(zone_id=a.pk),
            destination=SimpleNamespace(zone_id=b.pk),
            vehicle_type=vehicle,
            service="standard",
        )

    for destination, expected in (("Accra Central", "50"), ("Adenta", "40")):
        request = delivery("Madina", destination, "bike")
        band = price_cards.matching_band_rate(request)
        if band is None or band.amount != Decimal(expected):
            raise CommandError(f"Unexpected Bike matrix fallback for Madina -> {destination}.")
        resolved = price_cards.resolve_standard_price(request, None)
        print(
            f"ROUTE Madina -> {destination} Bike: matrix fallback=GHS {band.amount}; "
            f"actual source={resolved.source}; actual amount={resolved.amount}",
            flush=True,
        )
    for destination in ("Tema Community 18", "Tema Community 25", "Afienya", "Kasoa"):
        request = delivery("East Legon", destination, "car")
        if price_cards.matching_band_rate(request) is not None:
            raise CommandError(f"Unexpected H/I Car band price for {destination}.")
        resolved = price_cards.resolve_standard_price(request, None)
        print(
            f"ROUTE East Legon -> {destination} Car: "
            f"{'Admin Quote (no exact rate)' if resolved is None else 'exact override GHS ' + str(resolved.amount)}; "
            "matrix remains unset",
            flush=True,
        )
    tested = False
    for rate in (
        price_cards.current_rates()
        .filter(card__scope_key="general", service="standard", active=True, card__active=True)
        .select_related("origin", "destination")
    ):
        request = delivery(rate.origin.name, rate.destination.name, rate.vehicle)
        if price_cards.matching_band_rate(request) is None:
            continue
        resolved = price_cards.resolve_standard_price(request, None)
        if resolved is None or resolved.source != "general_exact":
            raise CommandError("Exact General rate did not outrank matrix pricing.")
        print(
            f"EXACT PRECEDENCE VERIFIED: {rate.origin.name} -> {rate.destination.name} "
            f"{rate.vehicle}: source={resolved.source}; amount={resolved.amount}",
            flush=True,
        )
        tested = True
        break
    if not tested:
        raise CommandError("Could not verify an existing exact rate overriding the matrix.")


def complete(preview):
    return (
        not preview["conflicts"]
        and preview["bands_identical"] == 9
        and preview["assignments_matching"] == 114
        and len(preview["identical_matrix_rates"]) == 73
        and not preview["matrix_to_create"]
        and preview["hi_car_unset"] == 17
    )


def run_rollout():
    if connection.vendor != "postgresql" or not settings.PRODUCTION or settings.DEBUG:
        raise CommandError("Temporary apply requires production PostgreSQL and DEBUG off.")
    actor_name = os.environ.get("RENDER_GENERAL_PRICING_APPLY_ACTOR")
    if (
        not actor_name
        or not User.objects.filter(username=actor_name, role="admin", is_active=True).exists()
    ):
        raise CommandError("Configured actor must identify an existing active production Admin.")
    with transaction.atomic():
        call_command("apply_general_pricing_bands")
        exact_rate_report()
        preview = preview_rollout()
        before = protected_state()
        if complete(preview):
            print(
                "ONE-TIME APPLY SKIPPED: approved configuration already installed; no writes.",
                flush=True,
            )
        else:
            counts = {
                vehicle: sum(row[0] == vehicle for row in preview["matrix_to_create"])
                for vehicle in ("bike", "car")
            }
            if (
                preview["conflicts"]
                or preview["bands_to_create"] != 9
                or preview["assignments_to_assign"] != 114
                or counts != {"bike": 45, "car": 28}
                or preview["zones"]["missing"]
                or preview["zones"]["ambiguous"]
                or preview["zones"]["duplicate_assignments"]
                or preview["zones"]["duplicate_config_labels"]
                or preview["hi_car_unset"] != 17
            ):
                raise CommandError("Pre-apply state differs from the approved empty configuration.")
            print(
                "==> ONE-TIME AUTHORIZED BAND APPLY; configured active Admin verified", flush=True
            )
            call_command("apply_general_pricing_bands", apply=True, actor=actor_name)
        print("==> MANDATORY POST-APPLY PREVIEW", flush=True)
        call_command("apply_general_pricing_bands")
        if not complete(preview_rollout()) or protected_state() != before:
            raise CommandError("Post-apply integrity failure; outer transaction will roll back.")
        exact_rate_report()
        route_report()
        print(
            "PROTECTED DATA VERIFIED: FixedRates, client pricing and Premium unchanged", flush=True
        )
    print("ONE-TIME BAND ROLLOUT VERIFIED AND COMMITTED", flush=True)


if __name__ == "__main__":
    try:
        run_rollout()
    except Exception:
        print(
            "BAND ROLLOUT FAILED; no further writes attempted; transaction rolled back", flush=True
        )
        raise
