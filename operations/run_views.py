"""Admin dispatch and Rider delivery-run pages."""

import json
import uuid
from datetime import timedelta
from typing import Any, cast

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Prefetch, Q, Subquery
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import runs
from .access import roles_required
from .models import Delivery, DeliveryRun, Resource, RunStop
from .phase25_forms import (
    DeliveryRunForm,
    RunAddStopForm,
    RunCancelForm,
    RunReleaseForm,
    RunRemoveStopForm,
    RunSkipForm,
    RunVersionForm,
)


def _form_error(form, exc):
    for message in exc.messages:
        form.add_error(None, message)


@roles_required("admin")
def dispatch_board(request):
    query = Delivery.objects.select_related(
        "job__organization", "pickup__zone", "destination__zone"
    ).exclude(reference__isnull=True)
    q = request.GET.get("q", "").strip()[:100]
    if q:
        query = query.filter(
            Q(reference__icontains=q) | Q(recipient_name__icontains=q)
            | Q(job__organization__name__icontains=q) | Q(pickup__area__icontains=q)
            | Q(destination__area__icontains=q) | Q(pickup__zone__name__icontains=q)
            | Q(destination__zone__name__icontains=q)
        )
    active_run_delivery_ids = RunStop.objects.filter(
        run__status__in=[DeliveryRun.Status.DRAFT, DeliveryRun.Status.READY, DeliveryRun.Status.IN_PROGRESS]
    ).exclude(status=RunStop.Status.REMOVED).values("delivery_id")
    unassigned = query.filter(status=Delivery.Status.CONFIRMED).exclude(pk__in=Subquery(active_run_delivery_ids)).order_by("created_at")
    run_queryset = DeliveryRun.objects.select_related("resource__user").prefetch_related(
        Prefetch("stops", queryset=RunStop.objects.select_related(
            "delivery__job__organization", "delivery__pickup__zone", "delivery__destination__zone"
        ).order_by("sequence"), to_attr="board_stops")
    ).filter(operational_date__gte=timezone.localdate() - timedelta(days=7)).order_by(
        "-operational_date", "-created_at"
    )
    status_filter = request.GET.get("run_status", "")
    if status_filter in DeliveryRun.Status.values:
        run_queryset = run_queryset.filter(status=status_filter)
    runs_page = Paginator(run_queryset, 25).get_page(request.GET.get("page"))
    for run in runs_page:
        run_view = cast(Any, run)
        board_stops = cast(Any, run).board_stops
        run_view.progress = {
            "completed": sum(stop.status == RunStop.Status.COMPLETED for stop in board_stops),
            "remaining": sum(stop.status in (RunStop.Status.PENDING, RunStop.Status.STARTED) for stop in board_stops),
            "deferred": sum(stop.status in (RunStop.Status.SKIPPED, RunStop.Status.UNAVAILABLE) for stop in board_stops),
            "total": sum(stop.status != RunStop.Status.REMOVED for stop in board_stops),
        }
        run_view.current_stop = next(
            (stop for stop in board_stops if stop.status == RunStop.Status.STARTED),
            next((stop for stop in board_stops if stop.status == RunStop.Status.PENDING), None),
        )
    return render(request, "operations/dispatch_board.html", {
        "title": "Dispatch", "section": "dispatch", "q": q,
        "unassigned": Paginator(unassigned, 15).get_page(request.GET.get("deliveries_page")),
        "runs_page": runs_page, "run_status": status_filter,
        "counts": {
            "unassigned": unassigned.count(),
            "assigned": Delivery.objects.filter(status=Delivery.Status.ASSIGNED).exclude(pk__in=Subquery(active_run_delivery_ids)).count(),
            "active": DeliveryRun.objects.filter(status__in=[DeliveryRun.Status.READY, DeliveryRun.Status.IN_PROGRESS]).count(),
            "completed": DeliveryRun.objects.filter(status=DeliveryRun.Status.COMPLETED).count(),
        },
    })


@roles_required("admin")
def create_run(request):
    form = DeliveryRunForm(request.POST or None, initial={"request_key": uuid.uuid4(), "operational_date": timezone.localdate()})
    resource_id = request.POST.get("resource") if request.method == "POST" else request.GET.get("resource")
    if resource_id:
        try:
            resource = Resource.objects.get(pk=resource_id)
            delivery_field = cast(Any, form.fields["deliveries"])
            delivery_field.queryset = delivery_field.queryset.filter(vehicle_type=resource.vehicle_type)
        except (Resource.DoesNotExist, ValueError):
            pass
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            run = runs.create_run(
                actor=request.user, resource_id=data["resource"].pk,
                operational_date=data["operational_date"],
                delivery_ids=[delivery.pk for delivery in data["deliveries"]],
                notes=data["notes"], request_key=data["request_key"],
            )
            messages.success(request, "Draft delivery run created. Review the order before release.")
            return redirect("run-detail", run_id=run.pk)
        except ValidationError as exc:
            _form_error(form, exc)
    return render(request, "operations/run_create.html", {
        "title": "Create delivery run", "section": "dispatch", "form": form,
    })


@roles_required("admin")
def run_detail(request, run_id):
    run = get_object_or_404(DeliveryRun.objects.select_related("resource__user"), pk=run_id)
    stops = list(run.stops.select_related(
        "delivery__job__organization", "delivery__pickup__zone", "delivery__destination__zone",
        "deferred_by",
    ).order_by("sequence"))
    for stop in stops:
        stop_view = cast(Any, stop)
        stop_view.location = stop.delivery.pickup if stop.delivery.status in ("assigned", "to_pickup") else stop.delivery.destination
        stop_view.area_name = stop_view.location.zone.name if stop_view.location.zone else stop_view.location.area
        stop_view.premium = stop.delivery.service == "premium"
        stop_view.cod = stop.delivery.payment_method == "cod" and bool(stop.delivery.cod_amount)
        stop_view.is_remaining = stop.status in (RunStop.Status.PENDING, RunStop.Status.STARTED)
        stop_view.move_up_form = RunVersionForm(initial={"expected_version": run.version, "request_key": uuid.uuid4()})
        stop_view.move_down_form = RunVersionForm(initial={"expected_version": run.version, "request_key": uuid.uuid4()})
        stop_view.remove_form = RunRemoveStopForm(initial={"expected_version": run.version, "request_key": uuid.uuid4()})
        stop_view.retry_next_form = RunVersionForm(initial={"expected_version": run.version, "request_key": uuid.uuid4()})
        stop_view.retry_end_form = RunVersionForm(initial={"expected_version": run.version, "request_key": uuid.uuid4()})
        stop_view.skip_form = RunSkipForm(initial={"expected_version": run.version, "request_key": uuid.uuid4()})
    premium_windows = [
        (stop.sequence, stop.delivery.window_start)
        for stop in stops if stop.delivery.service == "premium" and stop.delivery.window_start
        and stop.status not in (RunStop.Status.COMPLETED, RunStop.Status.REMOVED)
    ]
    premium_order_warning = any(
        later_window < earlier_window
        for (_, earlier_window), (_, later_window) in zip(premium_windows, premium_windows[1:])
    )
    add_form = RunAddStopForm(initial={"expected_version": run.version, "request_key": uuid.uuid4()})
    excluded = RunStop.objects.filter(run__status__in=[DeliveryRun.Status.DRAFT, DeliveryRun.Status.READY, DeliveryRun.Status.IN_PROGRESS]).exclude(status=RunStop.Status.REMOVED).values("delivery_id")
    delivery_field = cast(Any, add_form.fields["delivery"])
    delivery_field.queryset = delivery_field.queryset.exclude(pk__in=Subquery(excluded)).filter(vehicle_type=run.resource.vehicle_type)
    return render(request, "operations/run_detail.html", {
        "title": run.reference, "section": "dispatch", "run": run, "stops": stops,
        "premium_order_warning": premium_order_warning,
        "next_stop": runs.next_stop(run), "progress": runs.stop_counts(run),
        "release_form": RunReleaseForm(initial={"expected_version": run.version, "request_key": uuid.uuid4()}),
        "version_form": RunVersionForm(initial={"expected_version": run.version, "request_key": uuid.uuid4()}),
        "order_form": RunVersionForm(initial={"expected_version": run.version, "request_key": uuid.uuid4()}),
        "add_form": add_form, "remove_form": RunRemoveStopForm(initial={"expected_version": run.version, "request_key": uuid.uuid4()}),
        "cancel_form": RunCancelForm(initial={"expected_version": run.version, "request_key": uuid.uuid4()}),
    })


@roles_required("admin")
@require_POST
def release_run(request, run_id):
    form = RunReleaseForm(request.POST)
    if form.is_valid():
        try:
            runs.release_run(actor=request.user, run_id=run_id, **form.cleaned_data)
            messages.success(request, "Run released to the Rider.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
    return redirect("run-detail", run_id=run_id)


@roles_required("admin")
@require_POST
def cancel_run(request, run_id):
    form = RunCancelForm(request.POST)
    if form.is_valid():
        try:
            runs.cancel_run(actor=request.user, run_id=run_id, **form.cleaned_data)
            messages.success(request, "Run cancelled. Unstarted deliveries remain available for dispatch.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
    return redirect("dispatch")


@roles_required("admin")
@require_POST
def reorder_stop(request, run_id, stop_id, direction):
    form = RunVersionForm(request.POST)
    if form.is_valid():
        try:
            runs.move_stop(
                actor=request.user, run_id=run_id, stop_id=stop_id, direction=direction,
                **form.cleaned_data,
            )
            messages.success(request, "Stop order updated.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
    return redirect("run-detail", run_id=run_id)


@roles_required("admin")
@require_POST
def set_run_order(request, run_id):
    form = RunVersionForm(request.POST)
    if form.is_valid():
        try:
            ordered_ids = json.loads(request.POST.get("ordered_ids", "[]"))
            if not isinstance(ordered_ids, list) or any(not isinstance(value, str) for value in ordered_ids):
                raise ValidationError("The submitted stop order is invalid.")
            runs.set_stop_order(actor=request.user, run_id=run_id, ordered_stop_ids=ordered_ids, **form.cleaned_data)
            messages.success(request, "Stop order saved.")
        except (ValidationError, ValueError) as exc:
            _form_error(form, exc) if isinstance(exc, ValidationError) else messages.error(request, "The submitted stop order is invalid.")
    return redirect("run-detail", run_id=run_id)


@roles_required("admin")
@require_POST
def group_run_areas(request, run_id):
    form = RunVersionForm(request.POST)
    if form.is_valid():
        try:
            runs.group_stops_by_area(actor=request.user, run_id=run_id, **form.cleaned_data)
            messages.success(request, "Remaining stops arranged by area. Review the sequence before proceeding.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
    return redirect("run-detail", run_id=run_id)


@roles_required("admin")
@require_POST
def add_run_stop(request, run_id):
    form = RunAddStopForm(request.POST)
    if form.is_valid():
        try:
            runs.add_stop(
                actor=request.user, run_id=run_id, delivery_id=form.cleaned_data["delivery"].pk,
                expected_version=form.cleaned_data["expected_version"], request_key=form.cleaned_data["request_key"],
            )
            messages.success(request, "Delivery appended to the remaining stops.")
        except ValidationError as exc:
            _form_error(form, exc)
            messages.error(request, "; ".join(exc.messages))
    return redirect("run-detail", run_id=run_id)


@roles_required("admin")
@require_POST
def remove_run_stop(request, run_id, stop_id):
    form = RunRemoveStopForm(request.POST)
    if form.is_valid():
        try:
            runs.remove_stop(actor=request.user, run_id=run_id, stop_id=stop_id, **form.cleaned_data)
            messages.success(request, "Unstarted delivery removed from the run.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
    return redirect("run-detail", run_id=run_id)


@roles_required("rider", "driver")
@require_POST
def start_run(request, run_id):
    form = RunVersionForm(request.POST)
    if form.is_valid():
        try:
            runs.start_run(actor=request.user, run_id=run_id, **form.cleaned_data)
            messages.success(request, "Run started. Follow the stop order below.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
    return redirect("field-dashboard")


@roles_required("rider", "driver")
def rider_run(request, run_id):
    run = get_object_or_404(
        DeliveryRun.objects.select_related("resource__user"), pk=run_id, resource__user=request.user,
        status__in=[DeliveryRun.Status.READY, DeliveryRun.Status.IN_PROGRESS, DeliveryRun.Status.COMPLETED],
    )
    stops = list(run.stops.select_related(
        "delivery__job__organization", "delivery__pickup__zone", "delivery__destination__zone",
        "delivery__pickup", "delivery__destination",
    ).exclude(status=RunStop.Status.REMOVED).order_by("sequence"))
    for stop in stops:
        stop_view = cast(Any, stop)
        stop_view.location = stop.delivery.pickup if stop.delivery.status in ("assigned", "to_pickup") else stop.delivery.destination
        stop_view.area_name = stop_view.location.zone.name if stop_view.location.zone else stop_view.location.area
        stop_view.retry_next_form = RunVersionForm(initial={"expected_version": run.version, "request_key": uuid.uuid4()})
        stop_view.retry_end_form = RunVersionForm(initial={"expected_version": run.version, "request_key": uuid.uuid4()})
        stop_view.skip_form = RunSkipForm(initial={"expected_version": run.version, "request_key": uuid.uuid4()})
    return render(request, "operations/field_run.html", {
        "title": run.reference, "section": "dashboard", "run": run,
        "stops": stops, "next_stop": runs.next_stop(run), "progress": runs.stop_counts(run),
        "version_form": RunVersionForm(initial={"expected_version": run.version, "request_key": uuid.uuid4()}),
    })


@roles_required("rider", "driver", "admin")
@require_POST
def skip_run_stop(request, run_id, stop_id):
    form = RunSkipForm(request.POST)
    if form.is_valid():
        try:
            runs.skip_stop(actor=request.user, run_id=run_id, stop_id=stop_id, **form.cleaned_data)
            messages.success(request, "Stop deferred. It remains available to retry.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
    return redirect("field-run" if request.user.role != "admin" else "run-detail", run_id=run_id)


@roles_required("rider", "driver", "admin")
@require_POST
def retry_run_stop(request, run_id, stop_id, position):
    form = RunVersionForm(request.POST)
    if form.is_valid():
        try:
            runs.retry_stop(actor=request.user, run_id=run_id, stop_id=stop_id, position=position, **form.cleaned_data)
            messages.success(request, "Deferred stop returned to the route sequence.")
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
    return redirect("field-run" if request.user.role != "admin" else "run-detail", run_id=run_id)
