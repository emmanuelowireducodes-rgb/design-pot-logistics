"""Approved initial General pricing-band configuration."""

from collections import defaultdict
from decimal import Decimal

BANDS = (
    ("A", "A — East Legon Core", (
        "East Legon", "American House", "Bawaleshie", "Shiashie", "Adjiringanor",
        "School Junction", "ARS / Boundary Road",
    )),
    ("B", "B — East / Inner Northeast", (
        "Nmai Dzorn", "Ogbojo", "Ashaley Botwe", "Nanakrom", "Trasacco", "Madina",
        "Madina Estate", "UPSA", "Legon / University of Ghana", "Airport Residential",
        "Airport", "East Airport", "Spintex",
    )),
    ("C", "C — North / Inner Accra", (
        "North Legon", "Haatso", "Agbogba", "Atomic", "Adenta", "Dzorwulu",
        "Abelemkpe", "37 / Liberation Road", "Cantonments", "Labone", "Kanda", "Ridge",
        "Achimota", "Christian Village", "Dome",
    )),
    ("D", "D — Central / South Accra", (
        "Osu", "La", "Tse Addo", "Baatsona", "Teshie", "Nungua", "Sakumono", "Lashibi",
        "Accra Central", "Makola", "Ministries", "Adabraka", "Circle", "Kokomlemle",
    )),
    ("E", "E — West / Northwest Accra", (
        "Lapaz", "Taifa", "Kwabenya", "Ashongman", "Sowutuom", "Kwashieman", "Kaneshie",
        "Abossey Okai", "Korle Bu", "Jamestown", "Awoshie", "Dansoman", "Mamprobi",
    )),
    ("F", "F — Outer East / North", (
        "East Legon Hills", "Lakeside", "Pantang", "Oyarifa", "Ayi Mensah", "Oyibi",
        "Abokobi", "Ofankor", "Pokuase", "Amasaman",
    )),
    ("G", "G — Outer West", (
        "Ablekuma", "Mallam", "Gbawe", "McCarthy Hill", "Weija", "West Hills / New Weija",
        "Bortianor",
    )),
    ("H", "H — Tema / Eastern Corridor", tuple(
        [f"Tema Community {n}" for n in range(1, 26)]
        + ["Tema New Town", "Tema Industrial Area", "Tema Harbour", "Ashaiman", "Afienya", "Dawhenya"]
    )),
    ("I", "I — Kasoa Corridor", ("Kasoa Barrier", "Kasoa", "Kasoa New Market", "Buduburam")),
)

_BIKE = (
    (30, 40, 45, 50, 60, 60, 80, 100, 100),
    (40, 35, 40, 50, 60, 55, 80, 90, 100),
    (45, 40, 40, 45, 55, 60, 75, 100, 100),
    (50, 50, 45, 40, 55, 70, 75, 100, 100),
    (60, 60, 55, 55, 45, 70, 60, 100, 95),
    (60, 55, 60, 70, 70, 50, 90, 90, 100),
    (80, 80, 75, 75, 60, 90, 50, 100, 80),
    (100, 90, 100, 100, 100, 90, 100, 60, 100),
    (100, 100, 100, 100, 95, 100, 80, 100, 50),
)
_CAR = (
    (65, 70, 85, 90, 100, 100, 130),
    (70, 70, 80, 90, 105, 100, 130),
    (85, 80, 80, 90, 100, 110, 125),
    (90, 90, 90, 85, 100, 120, 125),
    (100, 105, 100, 100, 90, 120, 115),
    (100, 100, 110, 120, 120, 100, 140),
    (130, 130, 125, 125, 115, 140, 100),
)


def matrix_config():
    """Return one symmetric rate per unordered pair, including the diagonal."""
    result = []
    for vehicle, matrix, size in (("bike", _BIKE, 9), ("car", _CAR, 7)):
        for i in range(size):
            for j in range(i, size):
                if matrix[i][j] != matrix[j][i]:
                    raise ValueError(f"The approved {vehicle} matrix is not symmetric at {i},{j}.")
                result.append((vehicle, BANDS[i][0], BANDS[j][0], Decimal(matrix[i][j])))
    return tuple(result)


def resolve_zone_assignments():
    """Resolve configured labels to existing zones without creating or guessing."""
    from .area_directory import normalize_name
    from .models import AreaAlias, OperationalZone

    requested = [(code, label) for code, _, names in BANDS for label in names]
    by_name = defaultdict(list)
    for zone in OperationalZone.objects.only("pk", "name", "normalized_name"):
        by_name[zone.normalized_name].append(zone)
    by_alias = defaultdict(list)
    for alias in AreaAlias.objects.select_related("zone").only(
        "pk", "name", "normalized_name", "zone_id", "zone__name"
    ):
        by_alias[alias.normalized_name].append(alias)

    assignments, missing, ambiguous = [], [], []
    for code, label in requested:
        matches = by_name.get(normalize_name(label), [])
        if len(matches) == 1:
            zone = matches[0]
            kind = "exact" if zone.name == label else "normalized"
        elif len(matches) > 1:
            ambiguous.append((label, sorted(zone.name for zone in matches)))
            continue
        else:
            aliases = by_alias.get(normalize_name(label), [])
            unique_zones = {alias.zone_id: alias for alias in aliases}
            if len(unique_zones) == 1:
                alias = next(iter(unique_zones.values()))
                zone, kind = alias.zone, "alias"
            elif len(unique_zones) > 1:
                ambiguous.append((label, sorted(alias.zone.name for alias in unique_zones.values())))
                continue
            else:
                missing.append(label)
                continue
        assignments.append({"code": code, "label": label, "zone": zone, "kind": kind})

    by_zone = defaultdict(list)
    for assignment in assignments:
        by_zone[assignment["zone"].pk].append(assignment["label"])
    duplicates = [labels for labels in by_zone.values() if len(labels) > 1]
    duplicate_labels = [label for _, label in requested]
    normalized_duplicates = len(duplicate_labels) - len(
        {normalize_name(label) for label in duplicate_labels}
    )
    return {
        "assignments": assignments,
        "missing": missing,
        "ambiguous": ambiguous,
        "duplicate_assignments": duplicates,
        "duplicate_config_labels": normalized_duplicates,
        "unique_zones": len(by_zone),
    }


def preview_seed():
    """Validate zone, band, assignment, and General matrix state without writes."""
    from .models import BandRate, PricingBand

    zones = resolve_zone_assignments()
    conflicts = []
    bands = {band.name: band for band in PricingBand.objects.all()}
    code_bands = {}
    for order, (code, name, _) in enumerate(BANDS, start=1):
        band = bands.get(name)
        if band and (not band.active or band.display_order != order):
            conflicts.append(
                f"Band {code} {name!r} exists with active={band.active}, "
                f"display_order={band.display_order}; expected active=True, display_order={order}."
            )
        if any(other.display_order == order and other.name != name for other in bands.values()):
            conflicts.append(f"Display order {order} is already used by another PricingBand.")
        code_bands[code] = band

    if zones["missing"] or zones["ambiguous"] or zones["duplicate_assignments"] or zones["duplicate_config_labels"]:
        conflicts.append("Zone list is incomplete, ambiguous, or duplicated.")
    if zones["unique_zones"] != 114:
        conflicts.append(f"Expected 114 unique zones; resolved {zones['unique_zones']}.")

    assignments_by_code = {code: [] for code, _, _ in BANDS}
    for item in zones["assignments"]:
        assignments_by_code[item["code"]].append(item["zone"])
        current_band_id = item["zone"].pricing_band_id
        expected_band = code_bands[item["code"]]
        if current_band_id and (not expected_band or current_band_id != expected_band.pk):
            conflicts.append(
                f"Zone {item['zone'].name!r} is already assigned to a different PricingBand."
            )

    matrix = matrix_config()
    existing = list(BandRate.objects.filter(scope_key="general", organization__isnull=True))
    band_ids = {code: band.pk for code, band in code_bands.items() if band}
    identical, to_create = [], []
    for vehicle, origin_code, destination_code, amount in matrix:
        origin_id = band_ids.get(origin_code)
        destination_id = band_ids.get(destination_code)
        if not origin_id or not destination_id:
            to_create.append((vehicle, origin_code, destination_code, amount))
            continue
        pair = frozenset((origin_id, destination_id))
        rows = [
            row for row in existing
            if row.vehicle == vehicle and frozenset((row.origin_id, row.destination_id)) == pair
        ]
        if not rows:
            to_create.append((vehicle, origin_code, destination_code, amount))
        elif (
            len(rows) == 1 and rows[0].active and rows[0].bidirectional
            and rows[0].amount == amount
        ):
            identical.append((vehicle, origin_code, destination_code, amount))
        else:
            conflicts.append(
                f"Conflicting General {vehicle} matrix data for {origin_code}-{destination_code}."
            )

    forbidden_ids = {band_ids[code] for code in ("H", "I") if code in band_ids}
    forbidden_car = [
        row for row in existing
        if row.vehicle == "car" and (row.origin_id in forbidden_ids or row.destination_id in forbidden_ids)
    ]
    if forbidden_car:
        conflicts.append("Existing General Car BandRates involve intentionally unpriced band H or I.")

    return {
        "zones": zones,
        "bands": code_bands,
        "assignments_by_code": assignments_by_code,
        "matrix_total": len(matrix),
        "identical_matrix_rates": identical,
        "matrix_to_create": to_create,
        "conflicts": sorted(set(conflicts)),
    }


def apply_seed(preview=None):
    """Apply the validated seed atomically; never overwrite conflicting data."""
    from django.core.exceptions import ValidationError
    from django.db import transaction

    from .models import BandRate, OperationalZone, PricingBand

    preview = preview or preview_seed()
    if preview["conflicts"]:
        raise ValidationError(preview["conflicts"])
    bands_to_create = sum(1 for code, _, _ in BANDS if not preview["bands"].get(code))
    rates_to_create = len(preview["matrix_to_create"])
    with transaction.atomic():
        preview = preview_seed()
        if preview["conflicts"]:
            raise ValidationError(preview["conflicts"])
        band_by_code = {}
        for order, (code, name, _) in enumerate(BANDS, start=1):
            band = preview["bands"].get(code)
            if band is None:
                band = PricingBand.objects.create(name=name, active=True, display_order=order)
            band_by_code[code] = band
        zone_updates = []
        for item in preview["zones"]["assignments"]:
            zone = item["zone"]
            band = band_by_code[item["code"]]
            if zone.pricing_band_id != band.pk:
                zone.pricing_band = band
                zone_updates.append(zone)
        if zone_updates:
            OperationalZone.objects.bulk_update(zone_updates, ["pricing_band"])

        rates = []
        for vehicle, origin_code, destination_code, amount in preview["matrix_to_create"]:
            origin, destination = band_by_code[origin_code], band_by_code[destination_code]
            if str(destination.pk) < str(origin.pk):
                origin, destination = destination, origin
            rates.append(BandRate(
                scope_key="general", organization=None, origin=origin, destination=destination,
                vehicle=vehicle, amount=amount, bidirectional=True, active=True,
            ))
        if rates:
            BandRate.objects.bulk_create(rates)
    return {"bands_created": bands_to_create,
            "zones_assigned": preview["zones"]["unique_zones"],
            "matrix_created": rates_to_create}
