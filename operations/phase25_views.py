import json
import uuid
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.templatetags.static import static
from django.urls import reverse
from django.views.decorators.http import require_GET

from . import locations, price_cards, pricing
from .access import roles_required, visible_deliveries
from .models import (
    Delivery,
    DomainEvent,
    FixedRate,
    OperationalZone,
    PriceCard,
    PricingConfiguration,
)
from .operational_views import errors
from .phase25_forms import (
    CardForm,
    ManualQuoteForm,
    ModeForm,
    RateForm,
    RoutePriceForm,
    ZoneAssignmentForm,
    ZoneForm,
)


def edit_page(request, title, form, back, **extra):
    return render(
        request,
        "operations/editor.html",
        {"title": title, "section": "pricing", "form": form, "back": back, **extra},
    )


@roles_required("admin")
def pricing_home(request):
    config = PricingConfiguration.objects.first()
    form = ModeForm(
        request.POST or None,
        initial={
            "mode": config.mode if config else "DISTANCE_BASED",
            "fallback": config.fallback if config else "REQUIRE_ADMIN_QUOTE",
        },
    )
    if request.method == "POST" and form.is_valid():
        price_cards.change_mode(actor=request.user, **form.cleaned_data)
        messages.success(request, "General pricing mode saved. Confirmed quotes are unchanged.")
        return redirect("pricing")
    return render(
        request,
        "operations/pricing_home.html",
        {
            "title": "Pricing",
            "section": "pricing",
            "form": form,
            "mode": config.get_mode_display() if config else "Distance based",
            "pending": Delivery.objects.filter(
                job__quote_requested=True, status="requested"
            ).count(),
        },
    )


@roles_required("admin")
def cards(request):
    query = PriceCard.objects.select_related("organization").order_by("name")
    q = request.GET.get("q", "")[:100]
    if q:
        query = query.filter(Q(name__icontains=q) | Q(organization__name__icontains=q))
    return render(
        request,
        "operations/cards.html",
        {
            "title": "Price cards",
            "section": "pricing",
            "q": q,
            "page_obj": Paginator(query, 20).get_page(request.GET.get("page")),
            "general_card": PriceCard.objects.filter(scope_key="general").first(),
        },
    )


@roles_required("admin")
def card_edit(request, card_id=None):
    card = get_object_or_404(PriceCard, pk=card_id) if card_id else None
    initial = (
        {"name": card.name, "organization": card.organization_id, "active": card.active}
        if card
        else {}
    )
    form = CardForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            saved = price_cards.save_card(actor=request.user, card_id=card_id, **form.cleaned_data)
            return redirect("card-rates", card_id=saved.pk)
        except ValidationError as exc:
            errors(form, exc)
    return edit_page(
        request, "Edit price card" if card else "Create price card", form, "/admin/pricing/cards/"
    )


@roles_required("admin")
def card_rates(request, card_id):
    card = get_object_or_404(PriceCard.objects.select_related("organization"), pk=card_id)
    query = (
        price_cards.current_rates()
        .filter(card=card)
        .select_related("origin", "destination")
        .order_by("origin__name", "destination__name", "vehicle", "service")
    )
    q = request.GET.get("q", "")[:100]
    vehicle = request.GET.get("vehicle", "")
    active = request.GET.get("active", "all")
    if q:
        query = query.filter(Q(origin__name__icontains=q) | Q(destination__name__icontains=q))
    if vehicle in ("bike", "car"):
        query = query.filter(vehicle=vehicle)
    if active == "active":
        query = query.filter(active=True)
    elif active == "inactive":
        query = query.filter(active=False)
    grouped: dict[tuple, dict[str, object]] = {}
    for rate in query:
        key = (rate.origin_id, rate.destination_id, rate.bidirectional, rate.service)
        row = grouped.setdefault(
            key,
            {"origin": rate.origin, "destination": rate.destination,
             "bidirectional": rate.bidirectional, "service": rate.service,
             "active": rate.active, "bike": None, "car": None,
             "edit_url": reverse("route-price-edit", args=[card.pk, rate.origin_id, rate.destination_id])
             + f"?return={1 if rate.bidirectional else 0}&service={rate.service}"},
        )
        row[rate.vehicle] = rate
        row["active"] = row["active"] or rate.active
    return render(
        request,
        "operations/rates.html",
        {
            "title": card.name,
            "section": "pricing",
            "card": card,
            "rows": list(grouped.values()), "q": q, "vehicle": vehicle,
            "active_filter": active,
        },
    )


@roles_required("admin")
def route_price_edit(request, card_id, origin_id=None, destination_id=None):
    card = get_object_or_404(PriceCard, pk=card_id)
    editing = origin_id is not None
    bidirectional = request.GET.get("return", "1") != "0"
    service = request.GET.get("service", "standard")
    initial = {"same_return": bidirectional, "service": service}
    if editing:
        origin = get_object_or_404(OperationalZone, pk=origin_id)
        destination = get_object_or_404(OperationalZone, pk=destination_id)
        initial.update({"origin": origin, "destination": destination})
        for rate in price_cards.current_rates().filter(
            card=card, origin=origin, destination=destination,
            bidirectional=bidirectional, service=service,
        ):
            initial[f"{rate.vehicle}_price"] = rate.amount
            initial["active"] = rate.active
    form = RoutePriceForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            data = form.cleaned_data
            price_cards.save_route_price(
                actor=request.user, card_id=card.pk, origin_id=data["origin"].pk,
                destination_id=data["destination"].pk, bike_price=data["bike_price"],
                car_price=data["car_price"], bidirectional=data["same_return"],
                service=data["service"] or "standard", active=data["active"],
                effective_at=data["effective_at"], expires_at=data["expires_at"],
                reason=data["reason"], editing=editing,
                original_bidirectional=bidirectional if editing else None,
            )
            messages.success(request, "Route price saved. Existing confirmed quotes are unchanged.")
            return redirect("card-rates", card_id=card.pk)
        except ValidationError as exc:
            errors(form, exc)
            candidate = price_cards.current_rates().filter(
                card=card, active=True, service=(form.cleaned_data.get("service") or "standard"),
            ).filter(
                Q(origin_id=form.cleaned_data["origin"].pk, destination_id=form.cleaned_data["destination"].pk)
                | Q(bidirectional=True, origin_id=form.cleaned_data["destination"].pk,
                    destination_id=form.cleaned_data["origin"].pk)
            ).first()
            if candidate:
                context_return = "1" if candidate.bidirectional else "0"
                existing_route_url = reverse("route-price-edit", args=[card.pk, candidate.origin_id, candidate.destination_id]) + f"?return={context_return}&service={candidate.service}"
            else:
                existing_route_url = ""
        else:
            existing_route_url = ""
    else:
        existing_route_url = ""
    return render(request, "operations/route_price_form.html", {
        "title": "Edit route price" if editing else "Add route price",
        "section": "pricing", "form": form, "card": card, "editing": editing,
        "back": reverse("card-rates", args=[card.pk]), "existing_route_url": existing_route_url,
    })


@roles_required("admin")
def rate_edit(request, card_id, rate_key=None):
    card = get_object_or_404(PriceCard, pk=card_id)
    rate = (
        get_object_or_404(price_cards.current_rates(), card=card, rate_key=rate_key)
        if rate_key
        else None
    )
    initial = (
        {name: getattr(rate, name) for name in RateForm.base_fields if hasattr(rate, name)}
        if rate
        else {}
    )
    if rate:
        initial["expected_version"] = rate.version
    form = RateForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            price_cards.save_rate(
                actor=request.user, card_id=card.pk, rate_key=rate_key, **form.cleaned_data
            )
            messages.success(request, "A new immutable rate version has been saved.")
            return redirect("card-rates", card_id=card.pk)
        except ValidationError as exc:
            errors(form, exc)
    history = (
        FixedRate.objects.filter(rate_key=rate_key).select_related("actor").order_by("-version")
        if rate
        else FixedRate.objects.none()
    )
    return edit_page(
        request,
        "Revise fixed rate" if rate else "Create fixed rate",
        form,
        f"/admin/pricing/cards/{card.pk}/",
        history_page=Paginator(history, 10).get_page(request.GET.get("page")),
    )


@roles_required("admin")
def pricing_history(request):
    query = (
        DomainEvent.objects.filter(Q(kind__startswith="pricing.") | Q(kind="quote.manual_created"))
        .select_related("actor")
        .order_by("-created_at", "pk")
    )
    return render(
        request,
        "operations/pricing_history.html",
        {
            "title": "Pricing history",
            "section": "pricing",
            "page_obj": Paginator(query, 25).get_page(request.GET.get("page")),
        },
    )


@roles_required("admin")
def pending_quotes(request):
    query = (
        Delivery.objects.filter(job__quote_requested=True, status="requested")
        .select_related("job__organization", "pickup", "destination")
        .order_by("created_at")
    )
    return render(
        request,
        "operations/pending_quotes.html",
        {
            "title": "Price confirmation requests",
            "section": "pricing",
            "page_obj": Paginator(query, 20).get_page(request.GET.get("page")),
        },
    )


@roles_required("client")
def price_pending(request, delivery_id):
    delivery = get_object_or_404(visible_deliveries(request.user), pk=delivery_id)
    if delivery.job.current_quote_id:
        return redirect("quote-review", quote_id=delivery.job.current_quote_id)
    return render(
        request,
        "operations/price_pending.html",
        {"title": "Price confirmation required", "delivery": delivery},
    )


@roles_required("admin")
def manual_quote(request, delivery_id):
    delivery = get_object_or_404(
        Delivery.objects.select_related("job__organization", "pickup", "destination"),
        pk=delivery_id,
    )
    form = ManualQuoteForm(
        request.POST or None,
        initial={"request_key": uuid.uuid4(), "expected_revision": delivery.job.revision},
    )
    if request.method == "POST" and form.is_valid():
        try:
            pricing.manual_quote(actor=request.user, delivery_id=delivery.pk, **form.cleaned_data)
            messages.success(request, "Quote sent to the client for review and confirmation.")
            return redirect("pending-quotes")
        except ValidationError as exc:
            errors(form, exc)
    return edit_page(
        request, "Provide delivery quote", form, "/admin/pricing/requests/", delivery=delivery
    )


@roles_required("admin")
def zones(request):
    query = OperationalZone.objects.order_by("name")
    return render(
        request,
        "operations/zones.html",
        {
            "title": "Operational zones",
            "section": "pricing",
            "page_obj": Paginator(query, 25).get_page(request.GET.get("page")),
        },
    )


@roles_required("admin")
def zone_edit(request, zone_id=None):
    zone = get_object_or_404(OperationalZone, pk=zone_id) if zone_id else None
    form = ZoneForm(
        request.POST or None,
        initial={"name": zone.name, "active": zone.active, "boundary": zone.boundary}
        if zone
        else {},
    )
    if request.method == "POST" and form.is_valid():
        try:
            locations.save_zone(
                actor=request.user,
                zone_id=zone_id,
                **{**form.cleaned_data, "boundary": form.cleaned_data["boundary"] or []},
            )
            return redirect("zones")
        except ValidationError as exc:
            errors(form, exc)
    return edit_page(
        request,
        "Edit operational zone" if zone else "Create operational zone",
        form,
        "/admin/zones/",
    )


@roles_required("admin")
def assign_zone(request, delivery_id):
    delivery = get_object_or_404(
        Delivery.objects.select_related("job__organization", "pickup", "destination"),
        pk=delivery_id,
    )
    form = ZoneAssignmentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            data = form.cleaned_data
            locations.assign_zone(
                actor=request.user,
                delivery_id=delivery.pk,
                side=data["side"],
                zone_id=data["zone"].pk,
                reason=data["reason"],
            )
            messages.success(
                request,
                "Zone corrected. Confirmed prices are unchanged; draft prices were recalculated.",
            )
            if not delivery.reference:
                return redirect("pending-quotes")
            return redirect("delivery-detail", delivery_id=delivery.pk)
        except ValidationError as exc:
            errors(form, exc)
    return edit_page(
        request, "Correct operational zone", form, "/admin/pricing/requests/", delivery=delivery
    )


@login_required
@require_GET
def delivery_map(request, delivery_id):
    delivery = get_object_or_404(visible_deliveries(request.user), pk=delivery_id)
    field = request.user.role in ("rider", "driver")
    pickup_next = field and delivery.status in ("assigned", "to_pickup")
    next_location = delivery.pickup if pickup_next else delivery.destination
    return render(
        request,
        "operations/delivery_map.html",
        {
            "title": "Next stop" if field else "Location details",
            "delivery": delivery,
            "field": field,
            "pickup_next": pickup_next,
            "next_location": next_location,
        },
    )


@require_GET
def manifest(request):
    return JsonResponse(
        {
            "id": "/",
            "name": "Design-Pot Logistics",
            "short_name": "Design-Pot",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "background_color": "#f5f5f7",
            "theme_color": "#00634b",
            "icons": [
                {"src": static("icon-192.png"), "sizes": "192x192", "type": "image/png", "purpose": "any"},
                {"src": static("icon-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "any"},
                {"src": static("icon-maskable-512.png"), "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
            ],
        },
        content_type="application/manifest+json",
    )


@require_GET
def service_worker(request):
    assets = [
        static(name)
        for name in (
            "app.css",
            "phase2.css",
            "phase25.css",
            "location-v1.css",
            "phase3.css",
            "phase4.css",
            "run-order.js",
            "app.js",
            "pwa.js",
            "icon-192.png",
            "icon-512.png",
            "icon-maskable-512.png",
            "apple-touch-icon.png",
            "favicon.png",
            "design-pot-logo.png",
        )
    ]
    code = (
        (Path(settings.BASE_DIR) / "static" / "service-worker.js")
        .read_text(encoding="utf-8")
        .replace("__ASSETS__", json.dumps(assets))
        .replace("__ICON__", json.dumps(static("icon-192.png")))
    )
    response = HttpResponse(code, content_type="text/javascript")
    response["Cache-Control"] = "no-cache"
    response["Service-Worker-Allowed"] = "/"
    return response


@require_GET
def offline(request):
    content = (Path(settings.BASE_DIR) / "static" / "offline.html").read_text(encoding="utf-8")
    return HttpResponse(
        content.replace("/static/phase25.css", static("phase25.css")), content_type="text/html"
    )
