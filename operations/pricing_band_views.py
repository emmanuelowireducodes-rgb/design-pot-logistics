"""Admin pages for Pricing Bands, existing-zone assignment and Standard matrices."""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from . import price_cards, pricing_bands
from .access import roles_required
from .area_directory import normalize_name
from .models import BandRate, OperationalZone, Organization, PricingBand


@roles_required("admin")
def pricing_bands_page(request):
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action in {"create_band", "update_band"}:
                band_id = request.POST.get("band_id") if action == "update_band" else None
                pricing_bands.save_band(
                    actor=request.user,
                    band_id=band_id,
                    name=request.POST.get("name", ""),
                    active=request.POST.get("active") == "on",
                    display_order=request.POST.get("display_order", 0),
                )
                messages.success(request, "Pricing Band saved.")
            elif action == "assign_zones":
                zone_ids = request.POST.getlist("zone_ids")
                pricing_bands.assign_zones(
                    actor=request.user,
                    zone_ids=zone_ids,
                    band_id=request.POST.get("band_id") or None,
                )
                messages.success(
                    request, f"Updated assignments for {len(set(zone_ids))} existing areas."
                )
            else:
                messages.error(request, "Choose a valid Pricing Band action.")
        except (ValidationError, ValueError) as exc:
            messages.error(
                request, "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc)
            )
        return redirect(
            f"{request.path}?q={request.GET.get('q', '')}&unassigned={request.GET.get('unassigned', '')}"
        )

    query = OperationalZone.objects.select_related("pricing_band").order_by("name")
    q = request.GET.get("q", "").strip()[:100]
    unassigned = request.GET.get("unassigned") == "1"
    if q:
        normalized = normalize_name(q)
        query = query.filter(
            Q(normalized_name__icontains=normalized)
            | Q(aliases__normalized_name__icontains=normalized)
            | Q(region__icontains=q)
            | Q(district__icontains=q)
        ).distinct()
    if unassigned:
        query = query.filter(pricing_band__isnull=True)
    bands = PricingBand.objects.annotate(zone_count=Count("zones")).order_by(
        "display_order", "name", "pk"
    )
    return render(
        request,
        "operations/pricing_bands.html",
        {
            "title": "Pricing Bands",
            "section": "pricing",
            "bands": bands,
            "zones": query,
            "q": q,
            "unassigned_only": unassigned,
            "total_unassigned": OperationalZone.objects.filter(pricing_band__isnull=True).count(),
            "total_zones": OperationalZone.objects.count(),
        },
    )


def _exact_overrides(organization, vehicle):
    scope_filter = Q(card__organization__isnull=True)
    if organization:
        scope_filter |= Q(card__organization=organization)
    rates = (
        price_cards.current_rates()
        .filter(
            scope_filter,
            card__active=True,
            active=True,
            service="standard",
            vehicle=vehicle,
            effective_at__lte=timezone.now(),
            origin__active=True,
            destination__active=True,
        )
        .filter(Q(expires_at__isnull=True) | Q(expires_at__gt=timezone.now()))
        .select_related(
            "card", "card__organization", "origin__pricing_band", "destination__pricing_band"
        )
    )
    result = {}
    for rate in rates:
        origin, destination = rate.origin.pricing_band_id, rate.destination.pricing_band_id
        if not origin or not destination:
            continue
        label = rate.card.organization.name if rate.card.organization_id else "General"
        result.setdefault((origin, destination), []).append({"rate": rate, "scope": label})
        if rate.bidirectional:
            result.setdefault((destination, origin), []).append({"rate": rate, "scope": label})
    return result


@roles_required("admin")
def band_matrix(request):
    organization_id = request.GET.get("organization", "")
    organization = get_object_or_404(Organization, pk=organization_id) if organization_id else None
    vehicle = request.GET.get("vehicle", "bike")
    if vehicle not in BandRate.Vehicle.values:
        vehicle = "bike"
    if request.method == "POST":
        organization_id = request.POST.get("organization_id", "")
        vehicle = request.POST.get("vehicle", "bike")
        try:
            pricing_bands.save_band_rate(
                actor=request.user,
                organization_id=organization_id or None,
                origin_id=request.POST.get("origin_id"),
                destination_id=request.POST.get("destination_id"),
                vehicle=vehicle,
                amount=request.POST.get("amount", ""),
                bidirectional=request.POST.get("directionality") == "symmetric",
            )
            messages.success(request, "Band price saved; blank prices remain unset.")
        except (ValidationError, ValueError) as exc:
            messages.error(
                request, "; ".join(exc.messages) if hasattr(exc, "messages") else str(exc)
            )
        return redirect(f"{request.path}?organization={organization_id}&vehicle={vehicle}")

    bands = list(PricingBand.objects.filter(active=True).order_by("display_order", "name", "pk"))
    scope_key = f"client:{organization.pk}" if organization else "general"
    scoped_rates = BandRate.objects.filter(
        scope_key=scope_key, vehicle=vehicle, active=True
    ).select_related("origin", "destination")
    general_rates = (
        BandRate.objects.filter(scope_key="general", vehicle=vehicle, active=True).select_related(
            "origin", "destination"
        )
        if organization
        else BandRate.objects.none()
    )

    def key_for(rate):
        return (rate.origin_id, rate.destination_id)

    scoped = {key_for(rate): rate for rate in scoped_rates if not rate.bidirectional}
    symmetric = {}
    for rate in scoped_rates.filter(bidirectional=True):
        pair = tuple(sorted((rate.origin_id, rate.destination_id), key=str))
        symmetric[pair] = rate
    general_directed = {key_for(rate): rate for rate in general_rates if not rate.bidirectional}
    general_symmetric = {
        tuple(sorted((rate.origin_id, rate.destination_id), key=str)): rate
        for rate in general_rates.filter(bidirectional=True)
    }
    exact_overrides = _exact_overrides(organization, vehicle)
    rows = []
    for origin in bands:
        cells = []
        for destination in bands:
            direct = scoped.get((origin.pk, destination.pk))
            pair = tuple(sorted((origin.pk, destination.pk), key=str))
            shared = symmetric.get(pair)
            symmetric_editor = bool(shared and str(origin.pk) == str(pair[0]))
            inherited = general_directed.get((origin.pk, destination.pk))
            inherited = inherited or general_symmetric.get(pair)
            cell_rate = direct or (shared if symmetric_editor else None)
            inherited_price = None if cell_rate else inherited
            cells.append(
                {
                    "origin": origin,
                    "destination": destination,
                    "rate": cell_rate,
                    "inherited": inherited_price,
                    "symmetric_editor": symmetric_editor,
                    "symmetric_from_other": bool(shared and not symmetric_editor and not direct),
                    "exact_overrides": exact_overrides.get((origin.pk, destination.pk), []),
                }
            )
        rows.append({"origin": origin, "cells": cells})
    return render(
        request,
        "operations/band_matrix.html",
        {
            "title": "Standard Band Matrix",
            "section": "pricing",
            "bands": bands,
            "rows": rows,
            "vehicle": vehicle,
            "organization": organization,
            "organizations": Organization.objects.order_by("name"),
            "inactive_band_count": PricingBand.objects.filter(active=False).count(),
        },
    )
