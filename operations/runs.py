"""Delivery run planning and rider actions. No geographic route claims are made."""

import uuid
from datetime import datetime, time, timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from .models import (
    Assignment,
    Availability,
    Delivery,
    DeliveryRun,
    DomainEvent,
    Resource,
    RunStop,
    User,
)
from .services import emit_event

ACTIVE_RUNS = (DeliveryRun.Status.READY, DeliveryRun.Status.IN_PROGRESS)
DEFERRED = (RunStop.Status.SKIPPED, RunStop.Status.UNAVAILABLE)


def _admin(actor):
    if not actor.is_active or actor.role != User.Role.ADMIN:
        raise PermissionDenied


def _replay(actor, key, kind, run=None):
    key = uuid.UUID(str(key))
    event = DomainEvent.objects.filter(request_key=key).first()
    if not event:
        return None
    if event.actor_id != actor.pk or event.kind != kind:
        raise PermissionDenied
    replay_run = DeliveryRun.objects.get(pk=event.payload["run_id"])
    if run and replay_run.pk != run.pk:
        raise PermissionDenied
    return replay_run


def _notify_run(*, actor, run, kind, title, message, payload=None, delivery=None, request_key=None, admins=False, notify_resource=True):
    recipients = [run.resource.user] if notify_resource else []
    if admins:
        recipients.extend(User.objects.filter(role=User.Role.ADMIN, is_active=True))
    return emit_event(
        actor=actor, kind=kind, title=title, message=message, recipients=recipients,
        delivery=delivery, payload={"run_id": str(run.pk), **(payload or {})},
        request_key=uuid.UUID(str(request_key)) if request_key else None,
    )


def _reference(day):
    return f"DPT-RUN-{day:%y%m%d}-{uuid.uuid4().hex[:6].upper()}"


def _valid_delivery_for_resource(delivery, resource, day):
    if delivery.vehicle_type != resource.vehicle_type:
        raise ValidationError(f"{delivery.reference}: this delivery requires a different vehicle.")
    expected_role = "rider" if resource.vehicle_type == "bike" else "driver"
    if resource.user.role != expected_role or not resource.user.is_active:
        raise ValidationError("Choose an active Rider or Driver with the associated vehicle.")
    if delivery.service == "premium" and (
        not delivery.window_start or delivery.window_start.date() != day
        or delivery.window_end.date() != day
    ):
        raise ValidationError(
            f"{delivery.reference}: its Premium delivery window must fall on the run date."
        )
    if delivery.status not in (Delivery.Status.CONFIRMED, Delivery.Status.ASSIGNED):
        raise ValidationError(f"{delivery.reference} is not available for a delivery run.")
    if delivery.status == Delivery.Status.ASSIGNED:
        assignment = Assignment.objects.filter(delivery=delivery).order_by("-id").first()
        if not assignment or assignment.resource_id != resource.pk:
            raise ValidationError(f"{delivery.reference} is already assigned to another Rider.")


def _record_run_assignment(*, actor, run, delivery):
    latest = Assignment.objects.filter(delivery=delivery).order_by("-id").first()
    if latest and latest.resource_id == run.resource_id:
        return latest
    start = timezone.make_aware(datetime.combine(run.operational_date, time.min))
    end = start + timedelta(days=1)
    return Assignment.objects.create(
        delivery=delivery, resource=run.resource, actor=actor, starts_at=start, ends_at=end,
        override_reason=run.override_reason,
        resource_snapshot={
            "name": run.resource.user.get_full_name() or run.resource.user.username,
            "vehicle": run.resource.vehicle_name,
            "registration": run.resource.registration,
        },
    )


def _has_run_membership(delivery, *, excluding=None):
    stops = RunStop.objects.filter(
        delivery=delivery, status__in=[RunStop.Status.PENDING, RunStop.Status.STARTED, *DEFERRED],
        run__status__in=[DeliveryRun.Status.DRAFT, *ACTIVE_RUNS],
    )
    if excluding:
        stops = stops.exclude(run=excluding)
    return stops.exists()


@transaction.atomic
def create_run(*, actor, resource_id, operational_date, delivery_ids, notes="", request_key):
    _admin(actor)
    if existing := _replay(actor, request_key, "run.created"):
        return existing
    resource = Resource.objects.select_for_update().select_related("user").get(pk=resource_id)
    day = operational_date
    if day < timezone.localdate() or day > timezone.localdate() + timedelta(days=31):
        raise ValidationError("Choose today or a date within the next 31 days.")
    ids = list(dict.fromkeys(str(value) for value in delivery_ids))
    if not ids:
        raise ValidationError("Select at least one confirmed delivery.")
    locked_deliveries = list(Delivery.objects.select_for_update(of=("self",)).select_related(
        "job", "pickup", "destination", "destination__zone"
    ).filter(pk__in=ids).order_by("pk"))
    if len(locked_deliveries) != len(ids):
        raise ValidationError("One or more deliveries are no longer available.")
    deliveries_by_id = {str(delivery.pk): delivery for delivery in locked_deliveries}
    deliveries = [deliveries_by_id[delivery_id] for delivery_id in ids]
    for delivery in deliveries:
        _valid_delivery_for_resource(delivery, resource, day)
        if _has_run_membership(delivery):
            raise ValidationError(f"{delivery.reference} is already in another delivery run.")
    run = DeliveryRun.objects.create(
        reference=_reference(day), resource=resource, operational_date=day,
        created_by=actor, notes=notes.strip(),
    )
    RunStop.objects.bulk_create([
        RunStop(run=run, delivery=delivery, sequence=index)
        for index, delivery in enumerate(deliveries, start=1)
    ])
    _notify_run(
        actor=actor, run=run, kind="run.created", title=f"{run.reference} drafted",
        message="A delivery run draft is ready for dispatch review.", admins=False,
        payload={"deliveries": [str(item.pk) for item in deliveries]}, request_key=request_key,
        notify_resource=False,
    )
    return run


def _reserve_run_day(*, run, override, reason, delivery_ids):
    day_start = timezone.make_aware(datetime.combine(run.operational_date, time.min))
    day_end = day_start + timedelta(days=1)
    conflicts = Availability.objects.filter(
        resource=run.resource, cancelled_at__isnull=True,
        starts_at__lt=day_end, ends_at__gt=day_start,
    ).exclude(delivery_id__in=delivery_ids)
    conflict = conflicts.exists()
    off_duty = run.resource.status == "off_duty"
    if (conflict or off_duty) and (not override or not reason.strip()):
        raise ValidationError(
            "This Rider is off duty or has a scheduling conflict. Resolve it or use a recorded Admin override."
        )
    Availability.objects.filter(
        resource=run.resource, delivery_id__in=delivery_ids, kind="booking", cancelled_at__isnull=True
    ).update(cancelled_at=timezone.now(), release_reason="run_planned")
    if not Availability.objects.filter(
        resource=run.resource, kind="booking", note=f"run:{run.pk}", cancelled_at__isnull=True
    ).exists():
        Availability.objects.create(
            resource=run.resource, kind="booking", starts_at=day_start, ends_at=day_end,
            note=f"run:{run.pk}",
        )
    run.override_reason = reason.strip() if override else ""


@transaction.atomic
def release_run(*, actor, run_id, expected_version, request_key, override=False, reason=""):
    _admin(actor)
    run = DeliveryRun.objects.select_for_update().select_related("resource__user").get(pk=run_id)
    if replay := _replay(actor, request_key, "run.released", run):
        return replay
    if run.version != expected_version:
        raise ValidationError("This run changed. Reload it before releasing.")
    if run.status != DeliveryRun.Status.DRAFT:
        raise ValidationError("Only a draft run can be released.")
    stops = list(run.stops.select_for_update().select_related("delivery").order_by("sequence"))
    if not stops:
        raise ValidationError("Add at least one delivery before releasing this run.")
    _reserve_run_day(
        run=run, override=override, reason=reason,
        delivery_ids=[stop.delivery_id for stop in stops],
    )
    for stop in stops:
        delivery = Delivery.objects.select_for_update().get(pk=stop.delivery_id)
        _valid_delivery_for_resource(delivery, run.resource, run.operational_date)
        if _has_run_membership(delivery, excluding=run):
            raise ValidationError(f"{delivery.reference} was added to another active run.")
        if delivery.status == Delivery.Status.CONFIRMED:
            from .workflow import record_status

            _record_run_assignment(actor=actor, run=run, delivery=delivery)
            record_status(delivery, actor, Delivery.Status.ASSIGNED, f"Released in {run.reference}.")
    now = timezone.now()
    run.status = DeliveryRun.Status.READY
    run.released_at = now
    run.version += 1
    run.save(update_fields=["status", "released_at", "version", "override_reason", "updated_at"])
    _notify_run(
        actor=actor, run=run, kind="run.released", title=f"{run.reference} is ready",
        message="Dispatch released a delivery run to you.", admins=False,
        payload={"stop_count": len(stops), "override": bool(override), "reason": reason.strip()},
        request_key=request_key,
    )
    return run


@transaction.atomic
def start_run(*, actor, run_id, expected_version, request_key):
    if actor.role not in (User.Role.RIDER, User.Role.DRIVER) or not actor.is_active:
        raise PermissionDenied
    run = DeliveryRun.objects.select_for_update().select_related("resource__user").get(pk=run_id)
    if run.resource.user_id != actor.pk:
        raise PermissionDenied
    if replay := _replay(actor, request_key, "run.started", run):
        return replay
    if run.version != expected_version:
        raise ValidationError("This run changed. Reload it before starting.")
    if run.status != DeliveryRun.Status.READY:
        raise ValidationError("This run is not ready to start.")
    run.status = DeliveryRun.Status.IN_PROGRESS
    run.started_at = timezone.now()
    run.version += 1
    run.save(update_fields=["status", "started_at", "version", "updated_at"])
    first = run.stops.filter(status=RunStop.Status.PENDING).order_by("sequence").first()
    if first:
        first.status = RunStop.Status.STARTED
        first.started_at = timezone.now()
        first.save(update_fields=["status", "started_at"])
    _notify_run(
        actor=actor, run=run, kind="run.started", title=f"{run.reference} started",
        message="The Rider has started this delivery run.", admins=True,
        request_key=request_key,
    )
    return run


def _resequence(stops, *, run):
    pending_order = list(stops)
    pending_iter = iter(pending_order)
    all_stops = list(run.stops.exclude(status=RunStop.Status.REMOVED).order_by("sequence", "created_at"))
    ordered = [next(pending_iter) if stop.status == RunStop.Status.PENDING else stop for stop in all_stops]
    for index, stop in enumerate(ordered, start=1):
        if stop.sequence != index:
            RunStop.objects.filter(pk=stop.pk).update(sequence=index)
            stop.sequence = index


def _touch_run(run):
    run.version += 1
    run.save(update_fields=["version", "updated_at"])


def _notify_order_change(*, actor, run, kind, title, message, stops, request_key=None):
    _touch_run(run)
    _notify_run(
        actor=actor, run=run, kind=kind, title=title, message=message,
        payload={"version": run.version, "order": [str(stop.pk) for stop in stops]},
        request_key=request_key,
    )


@transaction.atomic
def move_stop(*, actor, run_id, stop_id, direction, expected_version, request_key=None):
    _admin(actor)
    run = DeliveryRun.objects.select_for_update().get(pk=run_id)
    if request_key and (replay := _replay(actor, request_key, "run.order_changed", run)):
        return replay
    if run.version != expected_version:
        raise ValidationError("The stop order changed in another session. Reload the run.")
    if run.status not in (DeliveryRun.Status.DRAFT, *ACTIVE_RUNS):
        raise ValidationError("This run can no longer be reordered.")
    stops = list(run.stops.select_for_update().filter(status=RunStop.Status.PENDING).order_by("sequence"))
    index = next((i for i, stop in enumerate(stops) if str(stop.pk) == str(stop_id)), None)
    if index is None or direction not in ("up", "down"):
        raise ValidationError("This stop cannot be moved.")
    target = index - 1 if direction == "up" else index + 1
    if 0 <= target < len(stops):
        stops[index], stops[target] = stops[target], stops[index]
        _resequence(stops, run=run)
        _notify_order_change(
            actor=actor, run=run, kind="run.order_changed", title=f"{run.reference} order updated",
            message="Dispatch changed the remaining stop order.", stops=stops, request_key=request_key,
        )
    return run


@transaction.atomic
def set_stop_order(*, actor, run_id, ordered_stop_ids, expected_version, request_key):
    _admin(actor)
    run = DeliveryRun.objects.select_for_update().get(pk=run_id)
    if replay := _replay(actor, request_key, "run.order_changed", run):
        return replay
    if run.version != expected_version:
        raise ValidationError("The stop order changed in another session. Reload the run.")
    if run.status not in (DeliveryRun.Status.DRAFT, *ACTIVE_RUNS):
        raise ValidationError("This run can no longer be reordered.")
    stops = list(run.stops.select_for_update().filter(status=RunStop.Status.PENDING).order_by("sequence"))
    by_id = {str(stop.pk): stop for stop in stops}
    if len(ordered_stop_ids) != len(by_id) or set(ordered_stop_ids) != set(by_id):
        raise ValidationError("The submitted stop order is incomplete or out of date.")
    ordered = [by_id[stop_id] for stop_id in ordered_stop_ids]
    _resequence(ordered, run=run)
    _notify_order_change(
        actor=actor, run=run, kind="run.order_changed", title=f"{run.reference} order updated",
        message="Dispatch changed the remaining stop order.", stops=ordered, request_key=request_key,
    )
    return run


@transaction.atomic
def group_stops_by_area(*, actor, run_id, expected_version, request_key=None):
    _admin(actor)
    run = DeliveryRun.objects.select_for_update().get(pk=run_id)
    if request_key and (replay := _replay(actor, request_key, "run.area_grouped", run)):
        return replay
    if run.version != expected_version:
        raise ValidationError("The stop order changed in another session. Reload the run.")
    if run.status not in (DeliveryRun.Status.DRAFT, *ACTIVE_RUNS):
        raise ValidationError("This run can no longer be arranged by area.")
    stops = list(run.stops.select_for_update(of=("self",)).filter(status=RunStop.Status.PENDING).select_related(
        "delivery__destination__zone"
    ).order_by("sequence"))
    stops.sort(key=lambda stop: (
        (stop.delivery.destination.zone.name if stop.delivery.destination.zone else stop.delivery.destination.area).casefold(),
        stop.sequence,
    ))
    _resequence(stops, run=run)
    _notify_order_change(
        actor=actor, run=run, kind="run.area_grouped", title=f"{run.reference} arranged by area",
        message="Dispatch grouped remaining stops by Design-Pot area. Review the sequence before continuing.",
        stops=stops, request_key=request_key,
    )
    return run


@transaction.atomic
def skip_stop(*, actor, run_id, stop_id, expected_version, note="", request_key):
    run = DeliveryRun.objects.select_for_update().select_related("resource__user").get(pk=run_id)
    if actor.role == User.Role.ADMIN:
        _admin(actor)
    elif not actor.is_active or run.resource.user_id != actor.pk:
        raise PermissionDenied
    if replay := _replay(actor, request_key, "run.stop_skipped", run):
        return replay
    if run.status != DeliveryRun.Status.IN_PROGRESS or run.version != expected_version:
        raise ValidationError("This run changed. Reload it before skipping a stop.")
    stop = RunStop.objects.select_for_update().select_related("delivery").get(pk=stop_id, run=run)
    if stop.status not in (RunStop.Status.PENDING, RunStop.Status.STARTED):
        raise ValidationError("This stop is no longer available to skip.")
    current = next_stop(run)
    if current and current.pk != stop.pk:
        raise ValidationError("Skip the current Next Stop before moving to another delivery.")
    stop.status = RunStop.Status.SKIPPED
    stop.deferred_at = timezone.now()
    stop.deferred_by = actor
    stop.note = note.strip()
    stop.save(update_fields=["status", "deferred_at", "deferred_by", "note"])
    _touch_run(run)
    _notify_run(
        actor=actor, run=run, kind="run.stop_skipped", title=f"{stop.delivery.reference} skipped for now",
        message="A delivery stop was deferred and remains on the run.", delivery=stop.delivery,
        payload={"stop_id": str(stop.pk), "note": stop.note, "version": run.version},
        request_key=request_key, admins=True,
    )
    return stop


@transaction.atomic
def retry_stop(*, actor, run_id, stop_id, position, expected_version, request_key=None):
    run = DeliveryRun.objects.select_for_update().select_related("resource__user").get(pk=run_id)
    if actor.role == User.Role.ADMIN:
        _admin(actor)
    elif not actor.is_active or run.resource.user_id != actor.pk:
        raise PermissionDenied
    if run.version != expected_version:
        raise ValidationError("This run changed. Reload it before retrying the stop.")
    if run.status not in ACTIVE_RUNS or position not in ("next", "end"):
        raise ValidationError("Choose a valid retry position.")
    stop = RunStop.objects.select_for_update().select_related("delivery").get(pk=stop_id, run=run)
    if stop.status not in DEFERRED:
        raise ValidationError("This stop is not deferred.")
    if stop.delivery.status in (Delivery.Status.UNREACHABLE, Delivery.Status.FAILED):
        from .workflow import transition_delivery

        transition_delivery(
            actor=actor, delivery_id=stop.delivery_id, target="resume",
            expected_status=stop.delivery.status, request_key=uuid.uuid4(), note="Retried from delivery run.",
        )
    current = next_stop(run)
    if position == "next" and current and current.pk != stop.pk and current.status == RunStop.Status.STARTED:
        raise ValidationError("Finish or defer the current stop before making another stop next.")
    stop.status = RunStop.Status.PENDING
    stop.deferred_at = None
    stop.deferred_by = None
    stop.note = ""
    stop.save(update_fields=["status", "deferred_at", "deferred_by", "note"])
    pending = list(run.stops.select_for_update().filter(status=RunStop.Status.PENDING).order_by("sequence"))
    pending.remove(stop)
    pending.insert(0, stop) if position == "next" else pending.append(stop)
    _resequence(pending, run=run)
    _notify_order_change(
        actor=actor, run=run, kind="run.stop_retried", title=f"{stop.delivery.reference} returned to the run",
        message="A deferred delivery is back in the stop sequence.", stops=pending,
        request_key=request_key,
    )
    return stop


@transaction.atomic
def add_stop(*, actor, run_id, delivery_id, expected_version, request_key):
    _admin(actor)
    run = DeliveryRun.objects.select_for_update().select_related("resource__user").get(pk=run_id)
    if _replay(actor, request_key, "run.stop_added", run):
        return run
    if run.version != expected_version or run.status not in (DeliveryRun.Status.DRAFT, *ACTIVE_RUNS):
        raise ValidationError("This run changed or is closed. Reload the run.")
    delivery = Delivery.objects.select_for_update().select_related("job").get(pk=delivery_id)
    _valid_delivery_for_resource(delivery, run.resource, run.operational_date)
    if _has_run_membership(delivery, excluding=run):
        raise ValidationError("This delivery is already in another run.")
    if RunStop.objects.filter(run=run, delivery=delivery).exists():
        raise ValidationError("This delivery is already on this run.")
    sequence = (run.stops.aggregate(value=Max("sequence"))["value"] or 0) + 1
    stop = RunStop.objects.create(run=run, delivery=delivery, sequence=sequence)
    if run.status in ACTIVE_RUNS and delivery.status == Delivery.Status.CONFIRMED:
        from .workflow import record_status

        _record_run_assignment(actor=actor, run=run, delivery=delivery)
        record_status(delivery, actor, Delivery.Status.ASSIGNED, f"Added to {run.reference}.")
    _touch_run(run)
    _notify_run(
        actor=actor, run=run, kind="run.stop_added", title=f"Delivery added to {run.reference}",
        message="A new delivery was appended to your remaining stops.", delivery=delivery,
        payload={"stop_id": str(stop.pk), "sequence": sequence, "version": run.version},
        request_key=request_key,
    )
    return stop


@transaction.atomic
def remove_stop(*, actor, run_id, stop_id, expected_version, reason, request_key=None):
    _admin(actor)
    run = DeliveryRun.objects.select_for_update().get(pk=run_id)
    if request_key and (replay := _replay(actor, request_key, "run.stop_removed", run)):
        return replay
    if run.version != expected_version:
        raise ValidationError("This run changed. Reload before removing a delivery.")
    stop = RunStop.objects.select_for_update().get(pk=stop_id, run=run)
    if stop.status != RunStop.Status.PENDING or not reason.strip():
        raise ValidationError("Only an unstarted stop can be removed. Add a reason for the record.")
    if stop.delivery.status not in (Delivery.Status.CONFIRMED, Delivery.Status.ASSIGNED):
        raise ValidationError("This delivery has started. Keep it on the run and use the lifecycle actions.")
    stop.status = RunStop.Status.REMOVED
    stop.note = reason.strip()
    stop.save(update_fields=["status", "note"])
    _touch_run(run)
    _notify_run(
        actor=actor, run=run, kind="run.stop_removed", title=f"Delivery removed from {run.reference}",
        message="Dispatch removed an unstarted delivery from your run.", delivery=stop.delivery,
        payload={"stop_id": str(stop.pk), "reason": reason.strip(), "version": run.version},
        request_key=request_key,
    )
    return stop


@transaction.atomic
def record_unavailable(*, actor, delivery, note):
    stop = RunStop.objects.select_for_update().select_related("run__resource__user").filter(
        delivery=delivery, run__status__in=ACTIVE_RUNS,
        status__in=[RunStop.Status.PENDING, RunStop.Status.STARTED],
    ).first()
    if not stop:
        return None
    stop.status = RunStop.Status.UNAVAILABLE
    stop.deferred_at = timezone.now()
    stop.deferred_by = actor
    stop.note = note.strip()
    stop.save(update_fields=["status", "deferred_at", "deferred_by", "note"])
    run = stop.run
    _touch_run(run)
    _notify_run(
        actor=actor, run=run, kind="run.stop_unavailable", title=f"{delivery.reference} customer unavailable",
        message="The delivery remains pending in the deferred stops list.", delivery=delivery,
        payload={"stop_id": str(stop.pk), "note": stop.note, "version": run.version}, admins=True,
    )
    return stop


@transaction.atomic
def restore_deferred_stop(*, actor, delivery):
    stop = RunStop.objects.select_for_update().select_related("run__resource__user").filter(
        delivery=delivery, run__status__in=ACTIVE_RUNS, status__in=DEFERRED,
    ).order_by("run__created_at").first()
    if not stop:
        return None
    stop.status = RunStop.Status.PENDING
    stop.sequence = (stop.run.stops.aggregate(value=Max("sequence"))["value"] or 0) + 1
    stop.deferred_at = None
    stop.deferred_by = None
    stop.note = "Returned to remaining stops."
    stop.save(update_fields=["status", "sequence", "deferred_at", "deferred_by", "note"])
    run = stop.run
    _touch_run(run)
    _notify_run(
        actor=actor, run=run, kind="run.stop_resumed", title=f"{delivery.reference} returned to the run",
        message="A deferred stop was resumed from its delivery details.", delivery=delivery,
        payload={"stop_id": str(stop.pk), "version": run.version}, admins=True,
    )
    return stop


@transaction.atomic
def complete_stop(*, actor, delivery):
    stop = RunStop.objects.select_for_update().select_related("run").filter(
        delivery=delivery, run__status__in=ACTIVE_RUNS,
        status__in=[RunStop.Status.PENDING, RunStop.Status.STARTED],
    ).first()
    if not stop:
        return None
    stop.status = RunStop.Status.COMPLETED
    stop.completed_at = timezone.now()
    stop.save(update_fields=["status", "completed_at"])
    run = DeliveryRun.objects.select_for_update().get(pk=stop.run_id)
    remaining = run.stops.exclude(status__in=[RunStop.Status.COMPLETED, RunStop.Status.REMOVED]).exists()
    if not remaining:
        run.status = DeliveryRun.Status.COMPLETED
        run.completed_at = timezone.now()
    _touch_run(run)
    run.save(update_fields=["status", "completed_at", "version", "updated_at"])
    _notify_run(
        actor=actor, run=run, kind="run.stop_completed", title=f"{delivery.reference} completed",
        message="The delivery was completed using the existing proof-of-delivery workflow.",
        delivery=delivery, payload={"stop_id": str(stop.pk), "version": run.version}, admins=True,
    )
    if run.status == DeliveryRun.Status.COMPLETED:
        Availability.objects.filter(
            resource=run.resource, kind="booking", note=f"run:{run.pk}", cancelled_at__isnull=True
        ).update(cancelled_at=timezone.now(), release_reason="completed")
        _notify_run(
            actor=actor, run=run, kind="run.completed", title=f"{run.reference} completed",
            message="Every delivery stop on this run is resolved.", admins=True,
        )
    return stop


@transaction.atomic
def cancel_run(*, actor, run_id, expected_version, request_key, reason):
    _admin(actor)
    run = DeliveryRun.objects.select_for_update().select_related("resource__user").get(pk=run_id)
    if replay := _replay(actor, request_key, "run.cancelled", run):
        return replay
    if run.version != expected_version:
        raise ValidationError("This run changed. Reload before cancelling it.")
    if run.status != DeliveryRun.Status.DRAFT or not reason.strip():
        raise ValidationError("Only a draft run can be cancelled. Release requires Dispatch review; add a reason.")
    if run.stops.filter(status=RunStop.Status.STARTED).exists():
        raise ValidationError("A run with work already started needs an Admin review before cancellation.")
    run.status = DeliveryRun.Status.CANCELLED
    run.version += 1
    run.save(update_fields=["status", "version", "updated_at"])
    run.stops.filter(status=RunStop.Status.PENDING).update(
        status=RunStop.Status.REMOVED, note=reason.strip()
    )
    Availability.objects.filter(
        resource=run.resource, kind="booking", note=f"run:{run.pk}", cancelled_at__isnull=True
    ).update(cancelled_at=timezone.now(), release_reason="cancelled")
    _notify_run(
        actor=actor, run=run, kind="run.cancelled", title=f"{run.reference} cancelled",
        message="Dispatch cancelled this run. Contact Dispatch for reassignment.",
        payload={"reason": reason.strip(), "version": run.version}, request_key=request_key,
    )
    return run


def next_stop(run):
    stops = run.stops.filter(status__in=[RunStop.Status.STARTED, RunStop.Status.PENDING])
    return stops.select_related(
        "delivery__job__organization", "delivery__pickup__zone", "delivery__destination__zone",
        "delivery__pickup", "delivery__destination",
    ).order_by("-status", "sequence").first()


@transaction.atomic
def mark_stop_started(*, actor, delivery):
    stop = RunStop.objects.select_for_update().select_related("run").filter(
        delivery=delivery, run__status=DeliveryRun.Status.IN_PROGRESS,
        status__in=[RunStop.Status.PENDING, RunStop.Status.STARTED],
    ).order_by("run__created_at").first()
    if not stop:
        return None
    current = next_stop(stop.run)
    if current is None or current.pk != stop.pk:
        raise ValidationError("Work on the current Next Stop before moving to another delivery.")
    if stop.status != RunStop.Status.STARTED:
        stop.status = RunStop.Status.STARTED
        stop.started_at = timezone.now()
        stop.save(update_fields=["status", "started_at"])
        _touch_run(stop.run)
        _notify_run(
            actor=actor, run=stop.run, kind="run.stop_started",
            title=f"{delivery.reference} started", message="The Rider started this delivery stop.",
            delivery=delivery, payload={"stop_id": str(stop.pk), "version": stop.run.version}, admins=True,
        )
    return stop


def stop_counts(run):
    return {
        "completed": run.stops.filter(status=RunStop.Status.COMPLETED).count(),
        "remaining": run.stops.filter(status__in=[RunStop.Status.PENDING, RunStop.Status.STARTED]).count(),
        "deferred": run.stops.filter(status__in=DEFERRED).count(),
        "total": run.stops.exclude(status=RunStop.Status.REMOVED).count(),
    }
