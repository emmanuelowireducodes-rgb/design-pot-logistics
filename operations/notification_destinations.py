"""Safe, recipient-aware destinations for existing notification events."""

from uuid import UUID

from django.urls import reverse

from .access import visible_deliveries
from .models import DeliveryRun, User


def notification_destination(notification, user):
    """Return an existing authorized app URL, or None when no useful target exists."""
    event = notification.event
    payload = event.payload if isinstance(event.payload, dict) else {}

    run_id = payload.get("run_id")
    if run_id:
        try:
            run_id = UUID(str(run_id))
        except (ValueError, TypeError, AttributeError):
            run_id = None
        if run_id:
            run = DeliveryRun.objects.filter(pk=run_id).select_related("resource").first()
            if run and user.role == User.Role.ADMIN:
                return reverse("run-detail", kwargs={"run_id": run.pk})
            if run and user.role in (User.Role.RIDER, User.Role.DRIVER):
                if run.resource.user_id == user.pk:
                    return reverse("field-dashboard")

    if event.delivery_id and visible_deliveries(user).filter(pk=event.delivery_id).exists():
        if user.role == User.Role.ADMIN and event.kind in {
            "cod.collected",
            "cod.reconciled",
        }:
            return reverse("cod-reconciliation")
        return reverse("delivery-detail", kwargs={"delivery_id": event.delivery_id})

    if user.role == User.Role.ADMIN and event.kind.startswith("pricing."):
        return reverse("pricing")
    if user.role in (User.Role.RIDER, User.Role.DRIVER) and event.kind == "availability.created":
        return reverse("field-dashboard")
    return None
