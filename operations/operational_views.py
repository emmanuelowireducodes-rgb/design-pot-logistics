import uuid
from datetime import datetime, time, timedelta

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import OuterRef, Prefetch, Q, Subquery, Sum, Value
from django.db.models.functions import Concat
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from . import workflow
from .access import roles_required, visible_deliveries
from .booking_forms import (
    ActionForm,
    AssignmentForm,
    AvailabilityForm,
    BookingForm,
    CollectionForm,
    CompletionForm,
    PricingForm,
    ReconcileForm,
)
from .models import (
    Assignment,
    Availability,
    Delivery,
    DeliveryRun,
    DomainEvent,
    LedgerEntry,
    Notification,
    Quote,
    QuoteConfirmation,
    Resource,
    RunStop,
)
from .pricing import active_version
from .services import reserve_resource

GROUPS = {
    "unassigned": ["requested", "confirmed"],
    "assigned": ["assigned"],
    "progress": ["to_pickup", "picked_up", "in_transit"],
    "completed": ["delivered"],
    "issues": ["unreachable", "failed", "returned", "rescheduled"],
}


def errors(form, exc):
    for message in exc.messages:
        form.add_error(None, message)


def list_query(user):
    latest = Assignment.objects.filter(delivery_id=OuterRef("pk")).order_by("-id")
    quote = QuoteConfirmation.objects.filter(job_id=OuterRef("job_id")).values("quote__amount")[:1]
    return visible_deliveries(user).annotate(
        assigned_name=Subquery(
            latest.annotate(
                name=Concat("resource__user__first_name", Value(" "), "resource__user__last_name")
            ).values("name")[:1]
        ),
        assigned_resource_id=Subquery(latest.values("resource_id")[:1]),
        scheduled_start=Subquery(latest.values("starts_at")[:1]),
        delivery_fee=Subquery(quote),
    )


@roles_required("client")
def booking(request, delivery_id=None):
    delivery = (
        get_object_or_404(visible_deliveries(request.user), pk=delivery_id) if delivery_id else None
    )
    initial = {
        "booking_key": uuid.uuid4(),
        "revision": 0,
        "vehicle_type": "bike",
        "service": "standard",
        "payment_method": "paid",
    }
    if delivery:
        if delivery.status != "requested":
            messages.error(request, "A confirmed delivery cannot be edited.")
            return redirect("delivery-detail", delivery_id=delivery.pk)
        initial.update(
            {
                name: getattr(delivery, name)
                for name in (
                    "recipient_name",
                    "recipient_phone",
                    "package_description",
                    "instructions",
                    "vehicle_type",
                    "service",
                    "window_start",
                    "window_end",
                    "payment_method",
                    "cod_amount",
                )
            }
        )
        initial["revision"] = delivery.job.revision
        initial["booking_key"] = delivery.job.booking_key or uuid.uuid4()
        for prefix in ("pickup", "destination"):
            location = getattr(delivery, prefix)
            initial.update(
                {
                    f"{prefix}_address": location.display_address,
                    f"{prefix}_area": location.area,
                    f"{prefix}_zone": location.zone_id,
                    f"{prefix}_google_maps_url": location.google_maps_url,
                    f"{prefix}_instructions": location.instructions,
                    f"{prefix}_landmark": location.landmark,
                }
            )
    form = BookingForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            saved = workflow.save_booking(
                actor=request.user,
                data=form.cleaned_data,
                delivery_id=delivery.pk if delivery else None,
            )
            if saved.job.current_quote_id:
                return redirect("quote-review", quote_id=saved.job.current_quote_id)
            return redirect("price-pending", delivery_id=saved.pk)
        except ValidationError as exc:
            errors(form, exc)
    return render(
        request,
        "operations/booking.html",
        {
            "title": "Edit delivery" if delivery else "Create delivery",
            "section": "booking",
            "form": form,
            "delivery": delivery,
        },
    )


@roles_required("client")
def quote_review(request, quote_id):
    quote = get_object_or_404(
        Quote.objects.select_related("job__organization", "pricing_version"),
        pk=quote_id,
        job__organization_id=request.user.organization_id,
    )
    if not request.user.organization_id:
        raise PermissionDenied
    delivery = get_object_or_404(visible_deliveries(request.user), job=quote.job)
    confirmation = QuoteConfirmation.objects.filter(job=quote.job).first()
    stale = quote.job.quote_requested or bool(
        quote.job.current_quote_id and quote.job.current_quote_id != quote.pk
    )
    expired = quote.expires_at <= timezone.now()
    error = ""
    if request.method == "POST":
        try:
            workflow.confirm_quote(actor=request.user, quote_id=quote.pk)
            messages.success(request, "Delivery confirmed. Dispatch has been notified.")
            return redirect("delivery-detail", delivery_id=delivery.pk)
        except ValidationError as exc:
            error = " ".join(exc.messages)
    return render(
        request,
        "operations/quote.html",
        {
            "title": "Review your quote",
            "section": "booking",
            "quote": quote,
            "delivery": delivery,
            "confirmed": confirmation,
            "stale": stale,
            "expired": expired,
            "error": error,
        },
    )


@login_required
def queue(request, portal):
    from .views import check_portal

    check_portal(request, portal)
    deliveries = list_query(request.user)
    q = request.GET.get("q", "").strip()[:100]
    status = request.GET.get("status", "")
    if status in GROUPS:
        deliveries = deliveries.filter(status__in=GROUPS[status])
    elif status in Delivery.Status.values:
        deliveries = deliveries.filter(status=status)
    if q:
        deliveries = deliveries.filter(
            Q(reference__icontains=q)
            | Q(recipient_name__icontains=q)
            | Q(destination__area__icontains=q)
            | Q(pickup__area__icontains=q)
            | Q(job__organization__name__icontains=q)
        )
    vehicle = request.GET.get("vehicle", "")
    service = request.GET.get("service", "")
    if vehicle in ("bike", "car"):
        deliveries = deliveries.filter(vehicle_type=vehicle)
    if service in ("standard", "premium"):
        deliveries = deliveries.filter(service=service)
    if portal == "field":
        deliveries = deliveries.order_by("scheduled_start", "created_at", "pk")
    else:
        deliveries = deliveries.order_by("-created_at", "-pk")
    page_obj = Paginator(deliveries, 20).get_page(request.GET.get("page"))
    return render(
        request,
        "operations/queue.html",
        {
            "title": "Deliveries" if portal == "admin" else "My deliveries",
            "section": "deliveries",
            "page_obj": page_obj,
            "q": q,
            "status_filter": status,
            "vehicle_filter": vehicle,
            "service_filter": service,
            "groups": GROUPS,
        },
    )


@login_required
def detail(request, delivery_id):
    delivery = get_object_or_404(visible_deliveries(request.user), pk=delivery_id)
    assignment = workflow.latest_assignment(delivery)
    run_stop = RunStop.objects.select_related("run__resource__user").filter(
        delivery=delivery,
        run__status__in=[DeliveryRun.Status.DRAFT, DeliveryRun.Status.READY, DeliveryRun.Status.IN_PROGRESS],
    ).exclude(status=RunStop.Status.REMOVED).order_by("-run__created_at").first()
    confirmation = (
        QuoteConfirmation.objects.filter(job=delivery.job)
        .select_related("quote__pricing_version")
        .first()
    )
    quote = confirmation.quote if confirmation else delivery.job.current_quote
    history = Paginator(
        delivery.history.select_related("actor").order_by("-sequence", "-created_at", "-pk"), 20
    ).get_page(request.GET.get("history_page"))
    staff = request.user.role == "admin"
    field = request.user.role in ("rider", "driver")
    proof = getattr(delivery, "proof", None)
    context = {
        "title": delivery.reference or "Delivery draft",
        "section": "deliveries",
        "delivery": delivery,
        "assignment": assignment,
        "run_stop": run_stop,
        "quote": quote,
        "confirmation": confirmation,
        "history_page": history,
        "proof": proof,
        "cod": workflow.cod_summary(delivery),
        "can_assign": staff and delivery.status in ("confirmed", "assigned") and run_stop is None,
        "can_collect": field
        and delivery.status == "in_transit"
        and delivery.payment_method == "cod",
        "can_complete": field and delivery.status == "in_transit",
        "can_resume": (staff or field) and delivery.status in workflow.EXCEPTIONS,
        "can_exception": field and delivery.status in workflow.ACTIVE,
        "next_target": workflow.NEXT_STATUS.get(delivery.status) if field else None,
        "next_label": workflow.ACTION_LABELS.get(workflow.NEXT_STATUS.get(delivery.status, "")),
    }
    if staff:
        context["audit_page"] = Paginator(
            DomainEvent.objects.filter(delivery=delivery)
            .select_related("actor")
            .order_by("-created_at"),
            15,
        ).get_page(request.GET.get("audit_page"))
        context["delivery_notifications"] = (
            Notification.objects.filter(event__delivery=delivery)
            .select_related("recipient")
            .order_by("-created_at")[:20]
        )
    context["ledger"] = delivery.ledger.select_related("actor").order_by("-created_at")[:20]
    return render(request, "operations/detail.html", context)


@roles_required("admin")
def assign(request, delivery_id):
    delivery = get_object_or_404(visible_deliveries(request.user), pk=delivery_id)
    previous = workflow.latest_assignment(delivery)
    q = request.GET.get("q", "").strip()[:100]
    people = (
        Resource.objects.filter(
            vehicle_type=delivery.vehicle_type,
            user__is_active=True,
            user__role="rider" if delivery.vehicle_type == "bike" else "driver",
        )
        .select_related("user")
        .order_by("user__first_name", "pk")
    )
    if q:
        people = people.filter(
            Q(user__first_name__icontains=q)
            | Q(user__last_name__icontains=q)
            | Q(vehicle_name__icontains=q)
        )
    page_obj = Paginator(people, 20).get_page(request.GET.get("page"))
    resource_ids = [resource.pk for resource in page_obj]
    now = timezone.now().replace(second=0, microsecond=0)
    start = delivery.window_start or now + timedelta(minutes=5)
    end = delivery.window_end or start + timedelta(hours=1)
    form = AssignmentForm(
        request.POST or None,
        initial={
            "request_key": uuid.uuid4(),
            "expected_assignment": previous.pk if previous else None,
            "starts_at": start,
            "ends_at": end,
        },
    )
    resource_field = form.fields["resource"]
    assert isinstance(resource_field, forms.ModelChoiceField)
    resource_field.queryset = Resource.objects.filter(pk__in=resource_ids).select_related("user")
    blocks = (
        Availability.objects.filter(
            resource_id__in=resource_ids, cancelled_at__isnull=True, ends_at__gt=now
        )
        .select_related("delivery", "resource__user")
        .order_by("starts_at")[:100]
    )
    if request.method == "POST" and form.is_valid():
        values = form.cleaned_data
        try:
            workflow.assign_delivery(
                actor=request.user,
                delivery_id=delivery.pk,
                resource_id=values["resource"].pk,
                starts_at=values["starts_at"],
                ends_at=values["ends_at"],
                request_key=values["request_key"],
                expected_assignment=values["expected_assignment"],
                override=values["override"],
                reason=values["reason"],
            )
            messages.success(request, "Assignment saved. The rider or driver has been notified.")
            return redirect("delivery-detail", delivery_id=delivery.pk)
        except ValidationError as exc:
            errors(form, exc)
    return render(
        request,
        "operations/assign.html",
        {
            "title": "Assign delivery",
            "section": "dispatch",
            "delivery": delivery,
            "form": form,
            "page_obj": page_obj,
            "blocks": blocks,
            "q": q,
        },
    )


@roles_required("rider", "driver", "admin")
def status_action(request, delivery_id, target):
    delivery = get_object_or_404(visible_deliveries(request.user), pk=delivery_id)
    if request.user.role == "admin" and target != "resume":
        raise PermissionDenied
    if target not in (*workflow.NEXT_STATUS.values(), "unreachable", "failed", "resume"):
        raise Http404
    label = {
        "unreachable": "Customer unavailable",
        "failed": "Report an issue",
        "resume": "Resume delivery",
        **workflow.ACTION_LABELS,
    }[target]
    form = ActionForm(
        request.POST or None,
        initial={"request_key": uuid.uuid4(), "expected_status": delivery.status, "target": target},
    )
    if request.method == "POST" and form.is_valid():
        try:
            workflow.transition_delivery(
                actor=request.user,
                delivery_id=delivery.pk,
                target=target,
                expected_status=form.cleaned_data["expected_status"],
                request_key=form.cleaned_data["request_key"],
                note=form.cleaned_data["note"],
            )
            messages.success(request, "Delivery status updated.")
            run_stop = RunStop.objects.filter(
                delivery=delivery, run__status__in=[DeliveryRun.Status.READY, DeliveryRun.Status.IN_PROGRESS]
            ).exclude(status=RunStop.Status.REMOVED).select_related("run").first()
            if run_stop and request.user.role != "admin":
                return redirect("field-run", run_id=run_stop.run_id)
            return redirect("delivery-detail", delivery_id=delivery.pk)
        except ValidationError as exc:
            errors(form, exc)
    return render(
        request,
        "operations/action.html",
        {
            "title": label,
            "section": "deliveries",
            "form": form,
            "delivery": delivery,
            "action_label": label,
            "description": "Confirm this operational update. It will be recorded in the delivery history.",
            "exception": target in workflow.EXCEPTIONS,
        },
    )


@roles_required("rider", "driver")
def complete(request, delivery_id):
    delivery = get_object_or_404(visible_deliveries(request.user), pk=delivery_id)
    if request.method == "POST" and request.FILES:
        return HttpResponseBadRequest("Delivery image uploads are not accepted.")
    form = CompletionForm(
        request.POST or None,
        initial={"request_key": uuid.uuid4(), "recipient_name": delivery.recipient_name},
    )
    if request.method == "POST" and form.is_valid():
        try:
            workflow.complete_delivery(
                actor=request.user, delivery_id=delivery.pk, **form.cleaned_data
            )
            messages.success(request, "Delivery completed and operational details recorded.")
            run_stop = RunStop.objects.filter(delivery=delivery).select_related("run").order_by("-run__created_at").first()
            if run_stop:
                return redirect("field-run", run_id=run_stop.run_id)
            return redirect("delivery-detail", delivery_id=delivery.pk)
        except ValidationError as exc:
            errors(form, exc)
    return render(
        request,
        "operations/action.html",
        {
            "title": "Complete delivery",
            "section": "deliveries",
            "form": form,
            "delivery": delivery,
            "action_label": "Complete delivery",
            "description": "Record who received the package. Any expected COD must be collected first.",
        },
    )


@roles_required("rider", "driver")
def collect(request, delivery_id):
    delivery = get_object_or_404(visible_deliveries(request.user), pk=delivery_id)
    form = CollectionForm(
        request.POST or None, initial={"request_key": uuid.uuid4(), "amount": delivery.cod_amount}
    )
    if request.method == "POST" and form.is_valid():
        try:
            workflow.collect_cod(actor=request.user, delivery_id=delivery.pk, **form.cleaned_data)
            messages.success(request, "Product collection recorded. Dispatch can now reconcile it.")
            return redirect("delivery-detail", delivery_id=delivery.pk)
        except ValidationError as exc:
            errors(form, exc)
    return render(
        request,
        "operations/action.html",
        {
            "title": "Record COD collection",
            "section": "deliveries",
            "form": form,
            "delivery": delivery,
            "action_label": "Record collection",
            "description": "Record only the product payment. The Design-Pot delivery charge is separate.",
        },
    )


@roles_required("admin")
def reconcile(request, delivery_id):
    delivery = get_object_or_404(visible_deliveries(request.user), pk=delivery_id)
    form = ReconcileForm(request.POST or None, initial={"request_key": uuid.uuid4()})
    if request.method == "POST" and form.is_valid():
        try:
            workflow.reconcile_cod(actor=request.user, delivery_id=delivery.pk, **form.cleaned_data)
            messages.success(request, "COD collection reconciled. Settlement is not included.")
            return redirect("delivery-detail", delivery_id=delivery.pk)
        except ValidationError as exc:
            errors(form, exc)
    return render(
        request,
        "operations/action.html",
        {
            "title": "Reconcile COD",
            "section": "finance",
            "form": form,
            "delivery": delivery,
            "cod": workflow.cod_summary(delivery),
            "action_label": "Confirm reconciliation",
            "description": "Confirm that the collected product payment has been received and checked. This does not record settlement to the client.",
        },
    )


@roles_required("admin")
def calendar(request):
    try:
        day = datetime.strptime(request.GET.get("day", ""), "%Y-%m-%d").date()
    except ValueError:
        day = timezone.localdate()
    start = timezone.make_aware(datetime.combine(day, time.min))
    end = start + timedelta(days=1)
    blocks = (
        Availability.objects.filter(
            Q(cancelled_at__isnull=True) | Q(release_reason="completed"),
            starts_at__lt=end,
            ends_at__gt=start,
        )
        .select_related("delivery")
        .order_by("starts_at")
    )
    people = Resource.objects.select_related("user").order_by("user__first_name", "pk")
    kind = request.GET.get("vehicle", "")
    q = request.GET.get("q", "").strip()[:100]
    if kind in ("bike", "car"):
        people = people.filter(vehicle_type=kind)
    if q:
        people = people.filter(Q(user__first_name__icontains=q) | Q(user__last_name__icontains=q))
    status = request.GET.get("availability", "")
    booked_ids = blocks.values_list("resource_id", flat=True)
    if status == "off_duty":
        people = people.filter(status="off_duty")
    elif status == "booked":
        people = people.filter(pk__in=booked_ids)
    elif status == "available":
        people = people.exclude(status="off_duty").exclude(pk__in=booked_ids)
    people = people.prefetch_related(
        Prefetch("availability_set", queryset=blocks[:30], to_attr="day_blocks")
    )
    page_obj = Paginator(people, 20).get_page(request.GET.get("page"))
    return render(
        request,
        "operations/calendar.html",
        {
            "title": "Dispatch calendar",
            "section": "calendar",
            "page_obj": page_obj,
            "day": day,
            "previous_day": day - timedelta(days=1),
            "next_day": day + timedelta(days=1),
            "vehicle_filter": kind,
            "availability_filter": status,
            "q": q,
        },
    )


@roles_required("admin")
def availability(request):
    q = request.GET.get("q", "").strip()[:100]
    people = Resource.objects.select_related("user").order_by("user__first_name", "pk")
    if q:
        people = people.filter(Q(user__first_name__icontains=q) | Q(user__last_name__icontains=q))
    page_obj = Paginator(people, 20).get_page(request.GET.get("page"))
    form = AvailabilityForm(request.POST or None, initial={"request_key": uuid.uuid4()})
    resource_field = form.fields["resource"]
    assert isinstance(resource_field, forms.ModelChoiceField)
    resource_field.queryset = Resource.objects.filter(
        pk__in=[person.pk for person in page_obj]
    ).select_related("user")
    if request.method == "POST" and form.is_valid():
        try:
            values = form.cleaned_data
            reserve_resource(
                actor=request.user,
                resource_id=values["resource"].pk,
                starts_at=values["starts_at"],
                ends_at=values["ends_at"],
                kind=values["kind"],
                note=values["note"],
                request_key=values["request_key"],
            )
            messages.success(request, "Availability block saved.")
            return redirect("calendar")
        except ValidationError as exc:
            errors(form, exc)
    return render(
        request,
        "operations/availability.html",
        {
            "title": "Block availability",
            "section": "calendar",
            "form": form,
            "page_obj": page_obj,
            "q": q,
        },
    )


@login_required
def finance(request, portal):
    from .views import check_portal

    check_portal(request, portal)
    if portal not in ("admin", "client"):
        raise PermissionDenied
    deliveries = (
        visible_deliveries(request.user)
        .filter(cod_amount__gt=0, currency="GHS")
        .exclude(status="requested")
    )
    ledger = (
        LedgerEntry.objects.filter(delivery__in=deliveries)
        .values("kind")
        .annotate(total=Sum("amount"))
    )
    totals = {row["kind"]: row["total"] for row in ledger}
    collected = totals.get("cod_collected", 0)
    reconciled = totals.get("cod_reconciled", 0)
    collection = LedgerEntry.objects.filter(
        delivery_id=OuterRef("pk"), kind="cod_collected"
    ).values("amount")[:1]
    reconciliation = LedgerEntry.objects.filter(
        delivery_id=OuterRef("pk"), kind="cod_reconciled"
    ).values("amount")[:1]
    deliveries = deliveries.annotate(
        collected_amount=Subquery(collection), reconciled_amount=Subquery(reconciliation)
    ).order_by("-created_at")
    return render(
        request,
        "operations/finance.html",
        {
            "title": "COD & finance",
            "section": "finance",
            "page_obj": Paginator(deliveries, 20).get_page(request.GET.get("page")),
            "collected": collected,
            "reconciled": reconciled,
            "pending": collected - reconciled,
        },
    )


@roles_required("admin")
def pricing(request):
    version = active_version()
    initial = {
        f"{vehicle}_{field}": version.rules[vehicle][field]
        for vehicle in ("bike", "car")
        for field in ("base", "minimum", "per_km")
    }
    initial.update(
        {key: version.rules[key] for key in ("premium_surcharge", "rounding", "quote_minutes")}
    )
    initial["is_sample"] = version.is_sample
    form = PricingForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        rules: dict = {
            vehicle: {
                field: str(data[f"{vehicle}_{field}"]) for field in ("base", "minimum", "per_km")
            }
            for vehicle in ("bike", "car")
        }
        rules.update({key: str(data[key]) for key in ("premium_surcharge", "rounding")})
        rules["quote_minutes"] = data["quote_minutes"]
        try:
            workflow.update_pricing(actor=request.user, rules=rules, is_sample=data["is_sample"])
            messages.success(
                request, "Distance pricing version saved. Confirmed prices are unchanged."
            )
            return redirect("pricing")
        except ValidationError as exc:
            errors(form, exc)
    return render(
        request,
        "operations/pricing.html",
        {"title": "Distance pricing", "section": "pricing", "form": form, "version": version},
    )


@roles_required("rider", "driver")
def field_dashboard(request):
    from .models import DeliveryRun

    run = DeliveryRun.objects.filter(
        resource__user=request.user,
        status__in=[DeliveryRun.Status.READY, DeliveryRun.Status.IN_PROGRESS],
    ).order_by("operational_date", "created_at").first()
    if run:
        return redirect("field-run", run_id=run.pk)
    pending = (
        list_query(request.user)
        .filter(status__in=workflow.ACTIVE)
        .order_by("scheduled_start", "created_at", "pk")
    )
    next_delivery = pending.first()
    return render(
        request,
        "operations/field_day.html",
        {
            "title": "My day",
            "section": "dashboard",
            "next_delivery": next_delivery,
            "remaining": pending[1:11],
            "issues": list_query(request.user)
            .filter(status__in=workflow.EXCEPTIONS)
            .order_by("-created_at")[:10],
            "today": timezone.localdate(),
        },
    )


@roles_required("admin")
def dispatch_queue(request):
    if "status" not in request.GET:
        updated = request.GET.copy()
        updated["status"] = "unassigned"
        request.GET = updated
    return queue(request, portal="admin")
