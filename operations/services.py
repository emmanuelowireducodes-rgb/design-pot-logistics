from datetime import timedelta
from typing import Protocol

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from .models import (
    Availability,
    DomainEvent,
    Notification,
    PushOutbox,
    Resource,
    User,
)


class RoadRoutingProvider(Protocol):
    """Future provider adapter; road distance, current origin, windows and cache policy."""

    def plan(self, *, origin: tuple[float, float], stops: list[dict], cache_key: str) -> dict: ...


@transaction.atomic
def emit_event(
    *, actor, kind, title, message, recipients, delivery=None, payload=None, request_key=None
):
    event = DomainEvent.objects.create(
        actor=actor, kind=kind, delivery=delivery, payload=payload or {}, request_key=request_key
    )
    notifications = [
            Notification(event=event, recipient=user, title=title, message=message)
            for user in {user.pk: user for user in recipients}.values()
        ]
    Notification.objects.bulk_create(notifications)
    if settings.WEB_PUSH_ENABLED and notifications:
        PushOutbox.objects.bulk_create(
            [PushOutbox(notification=notification) for notification in notifications]
        )
    return event


def confirm_quote(*, actor, quote_id):
    from .workflow import confirm_quote as confirm_delivery_quote

    return confirm_delivery_quote(actor=actor, quote_id=quote_id)


@transaction.atomic
def reserve_resource(*, actor, resource_id, starts_at, ends_at, kind, note="", request_key=None):
    """All schedule writers must lock the person; PostgreSQL serializes concurrent requests."""
    if actor.role != User.Role.ADMIN:
        raise PermissionDenied
    resource = Resource.objects.select_for_update().get(pk=resource_id)
    if request_key:
        existing = DomainEvent.objects.filter(request_key=request_key).first()
        if existing:
            if existing.actor_id != actor.pk or existing.kind != "availability.created":
                raise PermissionDenied
            return Availability.objects.get(pk=existing.payload["block_id"])
    if ends_at <= starts_at or ends_at - starts_at > timedelta(days=31):
        raise ValidationError("Choose a valid availability window of up to 31 days.")
    if kind not in ("booking", "off_duty", "break", "maintenance", "personal"):
        raise ValidationError("Unknown availability type.")
    if Availability.objects.filter(
        resource=resource, starts_at__lt=ends_at, ends_at__gt=starts_at, cancelled_at__isnull=True
    ).exists():
        raise ValidationError("This person already has a schedule block in that window.")
    block = Availability(
        resource=resource, starts_at=starts_at, ends_at=ends_at, kind=kind, note=note
    )
    block.full_clean()
    block.save()
    emit_event(
        actor=actor,
        kind="availability.created",
        title="Schedule updated",
        message="Your availability has changed.",
        recipients=[resource.user],
        payload={
            "block_id": str(block.pk),
            "resource_id": str(resource.pk),
            "kind": kind,
            "starts_at": starts_at.isoformat(),
            "ends_at": ends_at.isoformat(),
            "note": note,
        },
        request_key=request_key,
    )
    return block


@transaction.atomic
def set_resource_duty_status(*, actor, resource_id, duty_status):
    """Change the current duty state without changing date-specific schedule blocks."""
    if actor.role != User.Role.ADMIN or not actor.is_active:
        raise PermissionDenied
    status_by_duty = {"on_duty": "available", "off_duty": "off_duty"}
    if duty_status not in status_by_duty:
        raise ValidationError("Choose On Duty or Off Duty.")

    resource = Resource.objects.select_for_update().select_related("user").get(pk=resource_id)
    if resource.user.role not in (User.Role.RIDER, User.Role.DRIVER):
        raise ValidationError("Duty status is only available for Riders and Drivers.")
    if not resource.user.operational_access_approved:
        raise ValidationError("Approve this Rider or Driver before changing duty status.")

    old_state = "off_duty" if resource.status == "off_duty" else "on_duty"
    if old_state == duty_status:
        return resource

    resource.status = status_by_duty[duty_status]
    resource.save(update_fields=["status"])
    DomainEvent.objects.create(
        actor=actor,
        kind="resource.duty_status_changed",
        payload={
            "target_resource_id": str(resource.pk),
            "target_user_id": str(resource.user_id),
            "old_state": old_state,
            "new_state": duty_status,
        },
    )
    return resource
