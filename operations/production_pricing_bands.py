"""Strict rollout validation around the canonical, approved band seed."""

import hashlib
import json
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from . import general_pricing_band_seed as seed
from . import price_cards
from .general_price_handoff import _plan
from .models import (
    AreaAlias,
    BandRate,
    FixedRate,
    OperationalZone,
    Organization,
    PremiumPricingRule,
    PriceCard,
    PricingBand,
    User,
)
from .services import emit_event


def approved_exact_state():
    """Require the canonical dataset plus only the separately approved overrides."""
    conflicts = []
    try:
        plan = _plan()
    except ValidationError as exc:
        return {"conflicts": exc.messages, "canonical": {}, "overrides": [], "counts": {}}
    conflicts.extend(plan["conflicts"])
    if plan["identical_by_vehicle"] != {"bike": 114, "car": 79}:
        conflicts.append("Canonical required exact rates must be 114 Bike + 79 Car = 193.")
    expected = {}
    if plan["origin"]:
        for row in plan["rows"]:
            zone = plan["zones"].get(row["to_area"])
            for vehicle in ("bike", "car"):
                if zone and row[f"{vehicle}_price"]:
                    expected[(plan["origin"].pk, zone.pk, vehicle)] = Decimal(
                        row[f"{vehicle}_price"]
                    )
    now = timezone.now()
    available = list(
        price_cards.current_rates()
        .filter(
            card__scope_key="general",
            service="standard",
            active=True,
            card__active=True,
            effective_at__lte=now,
        )
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
        .select_related("card", "origin", "destination")
    )
    overrides = []
    for rate in available:
        key = (rate.origin_id, rate.destination_id, rate.vehicle)
        if (
            rate.card_id == getattr(plan["card"], "pk", None)
            and rate.card.organization_id is None
            and rate.origin.active
            and rate.destination.active
        ):
            if (
                key in expected
                and rate.amount == expected[key]
                and not rate.bidirectional
                and rate.expires_at is None
            ):
                continue
            if (
                {rate.origin_id, rate.destination_id}
                == {
                    getattr(plan["zones"].get("Adenta"), "pk", None),
                    getattr(plan["zones"].get("Madina"), "pk", None),
                }
                and rate.vehicle in {"bike", "car"}
                and rate.amount == {"bike": Decimal("30"), "car": Decimal("45")}[rate.vehicle]
                and rate.bidirectional
                and rate.expires_at is None
            ):
                overrides.append(rate)
                continue
        conflicts.append(f"Unexpected available General Standard exact rate: {rate.pk}.")
    if sorted(rate.vehicle for rate in overrides) != ["bike", "car"]:
        conflicts.append("Require exactly one approved Adenta-Madina override per vehicle.")
    counts = {
        vehicle: sum(rate.vehicle == vehicle for rate in available) for vehicle in ("bike", "car")
    }
    if counts != {"bike": 115, "car": 80} or len(available) != 195:
        conflicts.append("Approved available General exact set must contain exactly 195 rows.")
    return {
        "conflicts": conflicts,
        "canonical": plan["identical_by_vehicle"],
        "overrides": overrides,
        "counts": counts,
    }


def protected_state():
    """Fingerprint complete rows, including client pricing and Premium settings."""
    result = {}
    for model in (FixedRate, PriceCard, Organization, PremiumPricingRule):
        rows = list(model.objects.order_by("pk").values())
        payload = json.dumps(rows, default=str, sort_keys=True, separators=(",", ":"))
        result[model._meta.label] = hashlib.sha256(payload.encode()).hexdigest()
    rows = list(BandRate.objects.exclude(scope_key="general").order_by("pk").values())
    result["client_band_rates"] = hashlib.sha256(
        json.dumps(rows, default=str, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return result


def preview_rollout():
    preview = seed.preview_seed()
    conflicts = list(preview["conflicts"])
    exact = approved_exact_state()
    conflicts.extend(exact["conflicts"])
    names = {name for _, name, _ in seed.BANDS}
    unexpected = list(PricingBand.objects.exclude(name__in=names).values_list("name", flat=True))
    conflicts.extend(f"Unexpected PricingBand: {name}" for name in unexpected)
    assigned_ids = [item["zone"].pk for item in preview["zones"]["assignments"]]
    if (
        OperationalZone.objects.exclude(pk__in=assigned_ids)
        .filter(pricing_band__isnull=False)
        .exists()
    ):
        conflicts.append("Unexpected zone assignments outside the approved 114 zones.")
    expected_pairs = {
        (vehicle, frozenset((origin, destination)))
        for vehicle, origin, destination, _ in seed.matrix_config()
    }
    codes = {band.pk: code for code, band in preview["bands"].items() if band}
    matrix_conflicts = {"bike": 0, "car": 0}
    forbidden = set()
    general_rates = list(
        BandRate.objects.filter(Q(scope_key="general") | Q(organization__isnull=True))
    )
    for rate in general_rates:
        origin, destination = codes.get(rate.origin_id), codes.get(rate.destination_id)
        key = (rate.vehicle, frozenset((origin, destination)))
        if rate.scope_key != "general" or rate.organization_id is not None:
            conflicts.append(f"Inconsistent General BandRate scope: {rate.pk}.")
        if key not in expected_pairs:
            conflicts.append(f"Unexpected General BandRate: {rate.pk} ({rate.vehicle}).")
            if rate.vehicle in matrix_conflicts:
                matrix_conflicts[rate.vehicle] += 1
        else:
            expected_amount = next(
                amount
                for vehicle, a, b, amount in seed.matrix_config()
                if (vehicle, frozenset((a, b))) == key
            )
            same_pair = sum(
                other.vehicle == rate.vehicle
                and frozenset((other.origin_id, other.destination_id))
                == frozenset((rate.origin_id, rate.destination_id))
                for other in general_rates
            )
            if (
                not rate.active
                or not rate.bidirectional
                or rate.amount != expected_amount
                or same_pair != 1
            ):
                matrix_conflicts[rate.vehicle] += 1
        if rate.vehicle == "car" and ({origin, destination} & {"H", "I"}):
            forbidden.add(frozenset((origin, destination)))
    assignments = preview["zones"]["assignments"]
    matching = sum(
        bool(preview["bands"][item["code"]])
        and item["zone"].pricing_band_id == preview["bands"][item["code"]].pk
        for item in assignments
    )
    assignment_conflicts = sum(
        bool(item["zone"].pricing_band_id)
        and (
            not preview["bands"][item["code"]]
            or item["zone"].pricing_band_id != preview["bands"][item["code"]].pk
        )
        for item in assignments
    )
    fixed = FixedRate.objects.filter(card__scope_key="general", service="standard")
    preview.update(
        exact_state=exact,
        conflicts=sorted(set(conflicts)),
        bands_identical=sum(
            bool(band and band.active and band.display_order == order)
            for order, (code, _, _) in enumerate(seed.BANDS, 1)
            for band in [preview["bands"][code]]
        ),
        bands_to_create=sum(band is None for band in preview["bands"].values()),
        band_conflicts=len(unexpected)
        + sum(
            bool(band and (not band.active or band.display_order != order))
            for order, (code, _, _) in enumerate(seed.BANDS, 1)
            for band in [preview["bands"][code]]
        ),
        assignments_matching=matching,
        assignments_to_assign=sum(not item["zone"].pricing_band_id for item in assignments),
        assignment_conflicts=assignment_conflicts,
        matrix_conflicts=matrix_conflicts,
        hi_car_unset=17 - len(forbidden),
        fixed_bike=fixed.filter(vehicle="bike").count(),
        fixed_car=fixed.filter(vehicle="car").count(),
        fixed_total=fixed.count(),
    )
    return preview


@transaction.atomic
def apply_rollout(*, actor):
    """Serialize PostgreSQL writers, revalidate, seed and audit in one transaction."""
    if not User.objects.filter(pk=actor.pk, role="admin", is_active=True).exists():
        raise ValidationError("An existing active Admin actor is required.")
    if connection.vendor == "postgresql":
        # Include empty tables: row locks alone cannot prevent concurrent inserts.
        models = (AreaAlias, BandRate, OperationalZone, PricingBand, FixedRate, PriceCard)
        tables = ", ".join(connection.ops.quote_name(model._meta.db_table) for model in models)
        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '10s'")
            cursor.execute(f"LOCK TABLE {tables} IN SHARE ROW EXCLUSIVE MODE")
    preview = preview_rollout()
    if preview["conflicts"]:
        raise ValidationError(preview["conflicts"])
    before = protected_state()
    result = seed.apply_seed(preview)
    after = preview_rollout()
    if (
        after["conflicts"]
        or after["bands_identical"] != 9
        or after["assignments_matching"] != 114
        or len(after["identical_matrix_rates"]) != 73
        or after["hi_car_unset"] != 17
        or protected_state() != before
    ):
        raise ValidationError("Post-apply integrity check failed; all changes rolled back.")
    changed = (
        preview["bands_to_create"]
        or preview["assignments_to_assign"]
        or preview["matrix_to_create"]
    )
    if changed:
        emit_event(
            actor=actor,
            kind="pricing.general_bands_applied",
            title="General pricing bands applied",
            message="Approved General band configuration applied.",
            recipients=[],
            payload={
                **result,
                "zones_changed": preview["assignments_to_assign"],
                "protected_state": before,
            },
        )
    return result
