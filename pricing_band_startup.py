"""Temporary authorized band apply; all verification precedes transaction commit."""

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

from operations import price_cards
from operations.models import OperationalZone, User
from operations.production_pricing_bands import preview_rollout, protected_state


def complete(preview):
    counts = {
        vehicle: sum(row[0] == vehicle for row in preview["identical_matrix_rates"])
        for vehicle in ("bike", "car")
    }
    return (
        not preview["conflicts"]
        and preview["bands_identical"] == 9
        and preview["bands_to_create"] == 0
        and preview["assignments_matching"] == 114
        and preview["assignments_to_assign"] == 0
        and counts == {"bike": 45, "car": 28}
        and not preview["matrix_to_create"]
        and preview["hi_car_unset"] == 17
    )


def route_report():
    checks = []
    for vehicle, amount in (("bike", "30"), ("car", "45")):
        for origin, destination in (("Adenta", "Madina"), ("Madina", "Adenta")):
            checks.append((origin, destination, vehicle, "general_exact", amount))
    checks.append(("Madina", "Accra Central", "bike", "general_band", "50"))
    for destination in ("Tema Community 18", "Tema Community 25", "Afienya", "Kasoa"):
        checks.append(("East Legon", destination, "car", None, None))
    checks.append(("East Legon", "Madina", "bike", "general_exact", None))
    for origin, destination, vehicle, source, amount in checks:
        a = OperationalZone.objects.get(name=origin, active=True)
        b = OperationalZone.objects.get(name=destination, active=True)
        request = SimpleNamespace(
            pickup=SimpleNamespace(zone_id=a.pk),
            destination=SimpleNamespace(zone_id=b.pk),
            vehicle_type=vehicle,
            service="standard",
        )
        resolved = price_cards.resolve_standard_price(request, None)
        if source is None:
            valid = resolved is None and price_cards.matching_band_rate(request) is None
        else:
            valid = (
                resolved is not None
                and resolved.source == source
                and (amount is None or resolved.amount == Decimal(amount))
            )
        if not valid:
            raise CommandError(
                f"Resolver verification failed: {origin} -> {destination} {vehicle}."
            )
        result = f"GHS {resolved.amount} {resolved.source}" if resolved else "Admin Quote"
        print(f"VERIFIED ROUTE: {origin} -> {destination} {vehicle}: {result}", flush=True)


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
        print("==> ONE-TIME ROLLOUT PRE-APPLY PREVIEW", flush=True)
        call_command("apply_general_pricing_bands")
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
                or preview["bands_identical"] != 0
                or preview["assignments_to_assign"] != 114
                or preview["assignments_matching"] != 0
                or counts != {"bike": 45, "car": 28}
                or preview["identical_matrix_rates"]
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
        route_report()
        print(
            "PROTECTED DATA VERIFIED: all 195 approved exact rates, client pricing and Premium unchanged",
            flush=True,
        )
    print("ONE-TIME BAND ROLLOUT VERIFIED AND COMMITTED", flush=True)


if __name__ == "__main__":
    try:
        run_rollout()
    except Exception:
        print("BAND ROLLOUT FAILED; transaction rolled back; STOP", flush=True)
        raise
