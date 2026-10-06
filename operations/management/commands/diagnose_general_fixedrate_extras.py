"""Temporary read-only comparison of live General rates with the approved dataset."""

import json
from decimal import Decimal
from types import SimpleNamespace

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from operations import price_cards
from operations.general_price_handoff import _plan
from operations.models import FixedRate


class Command(BaseCommand):
    help = "READ ONLY: report extra current General Standard rates and resolver effects."

    def handle(self, *args, **options):
        self.stdout.write("EXTRA FIXEDRATE DIAGNOSTIC: READ ONLY; no apply option exists")
        self.stdout.write(f"Database engine: {connection.vendor}; DEBUG: {settings.DEBUG}")
        with transaction.atomic():
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
            self.report()
        self.stdout.write("EXTRA FIXEDRATE DIAGNOSTIC COMPLETE; no writes made")

    def report(self):
        plan = _plan()
        if plan["conflicts"] or plan["pending"] or plan["areas_to_create"]:
            raise CommandError("Canonical General preview changed; " + str(plan["conflicts"]))
        expected = {}
        for row in plan["rows"]:
            for vehicle in ("bike", "car"):
                raw = row[f"{vehicle}_price"]
                if raw:
                    expected[(plan["origin"].pk, plan["zones"][row["to_area"]].pk, vehicle)] = (
                        Decimal(raw)
                    )
        now = timezone.now()
        # Match the failed safety check exactly, then report zone applicability
        # separately because the canonical resolver also requires active zones.
        available = (
            price_cards.current_rates()
            .filter(
                card__scope_key="general",
                service="standard",
                active=True,
                card__active=True,
                effective_at__lte=now,
            )
            .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        )
        extras = []
        for rate in available.select_related(
            "card__organization", "origin", "destination"
        ).order_by("vehicle", "pk"):
            key = (rate.origin_id, rate.destination_id, rate.vehicle)
            if (
                key in expected
                and rate.amount == expected[key]
                and not rate.bidirectional
                and rate.expires_at is None
            ):
                continue
            extras.append(rate)
        self.stdout.write(
            f"Approved matching counts: {plan['identical_by_vehicle']}; total={plan['identical']}"
        )
        self.stdout.write(
            f"Current available counts: Bike={available.filter(vehicle='bike').count()}; "
            f"Car={available.filter(vehicle='car').count()}; total={available.count()}"
        )
        self.stdout.write(f"EXTRA ROW COUNT: {len(extras)}")
        for rate in extras:
            key = (rate.origin_id, rate.destination_id, rate.vehicle)
            reverse = (rate.destination_id, rate.origin_id, rate.vehicle)
            overlaps = key in expected or (rate.bidirectional and reverse in expected)
            history = list(
                FixedRate.objects.filter(rate_key=rate.rate_key)
                .order_by("version")
                .values(
                    "id",
                    "version",
                    "amount",
                    "active",
                    "effective_at",
                    "expires_at",
                    "created_at",
                    "reason",
                )
            )
            result = {
                "id": rate.pk,
                "card_id": rate.card_id,
                "card_name": rate.card.name,
                "scope": rate.card.scope_key,
                "organization_id": rate.card.organization_id,
                "organization_name": rate.card.organization.name
                if rate.card.organization_id
                else None,
                "origin": rate.origin.name,
                "origin_id": rate.origin_id,
                "destination": rate.destination.name,
                "destination_id": rate.destination_id,
                "origin_active": rate.origin.active,
                "destination_active": rate.destination.active,
                "vehicle": rate.vehicle,
                "service": rate.service,
                "amount": rate.amount,
                "directionality": "bidirectional" if rate.bidirectional else "directional",
                "active": rate.active,
                "current_latest_version": True,
                "effective_now": True,
                "effective_at": rate.effective_at,
                "expires_at": rate.expires_at,
                "created_at": rate.created_at,
                "updated_at": getattr(rate, "updated_at", None),
                "rate_key": rate.rate_key,
                "version": rate.version,
                "actor_id": rate.actor_id,
                "reason": rate.reason,
                "history": history,
                "overlaps_approved_general_route": overlaps,
                "not_counted_reason": (
                    "Outside the canonical East Legon/destination/vehicle route set"
                    if key not in expected
                    else "Differs from canonical amount/directionality/dates"
                ),
                "exact_precedence": "General exact routes outrank all BandRates when applicable",
                "resolver": [],
            }
            directions = [(rate.origin_id, rate.destination_id)]
            if rate.bidirectional and rate.origin_id != rate.destination_id:
                directions.append((rate.destination_id, rate.origin_id))
            for origin, destination in directions:
                delivery = SimpleNamespace(
                    pickup=SimpleNamespace(zone_id=origin),
                    destination=SimpleNamespace(zone_id=destination),
                    vehicle_type=rate.vehicle,
                    service="standard",
                )
                try:
                    resolution = price_cards.resolve_standard_price(delivery, None)
                    fixed = resolution.fixed_rate if resolution else None
                    result["resolver"].append(
                        {
                            "origin_id": origin,
                            "destination_id": destination,
                            "selected_extra": bool(fixed and fixed.pk == rate.pk),
                            "selected_rate_id": fixed.pk if fixed else None,
                            "source": resolution.source if resolution else "Admin Quote",
                            "amount": resolution.amount if resolution else None,
                        }
                    )
                except ValidationError as exc:
                    result["resolver"].append({"error": exc.messages})
            self.stdout.write("EXTRA ROW: " + json.dumps(result, default=str, sort_keys=True))
        self.stdout.write(
            "Bike/Car extras same ordered route: "
            + str(
                len(extras) == 2
                and extras[0].vehicle != extras[1].vehicle
                and (extras[0].origin_id, extras[0].destination_id)
                == (extras[1].origin_id, extras[1].destination_id)
            )
        )
        self.stdout.write(
            "Intent cannot be inferred from a rate alone; reason/actor/history above are evidence only."
        )
