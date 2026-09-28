from datetime import timedelta
from typing import Protocol

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from .models import (
    Availability,
    DomainEvent,
    Notification,
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
    Notification.objects.bulk_create(
        [
            Notification(event=event, recipient=user, title=title, message=message)
            for user in {user.pk: user for user in recipients}.values()
        ]
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
