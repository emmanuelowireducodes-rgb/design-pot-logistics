from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import OuterRef, Q, Subquery

from .models import Assignment, Delivery, DeliveryRun, RunStop, User


def roles_required(*roles):
    def decorator(view):
        @login_required
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if request.user.role not in roles:
                raise PermissionDenied
            if request.user.role in (User.Role.RIDER, User.Role.DRIVER) and not request.user.operational_access_approved:
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return wrapped

    return decorator


def visible_deliveries(user):
    base = Delivery.objects.select_related("job__organization", "pickup", "destination")
    if user.role == User.Role.ADMIN:
        return base.exclude(reference__isnull=True)
    if user.role == User.Role.CLIENT:
        return (
            base.filter(job__organization_id=user.organization_id)
            if user.organization_id
            else base.none()
        )
    latest = (
        Assignment.objects.filter(delivery_id=OuterRef("pk"))
        .order_by("-id")
        .values("resource__user_id")[:1]
    )
    run_delivery_ids = RunStop.objects.filter(
        delivery_id=OuterRef("pk"),
        run__resource__user=user,
        run__status__in=[DeliveryRun.Status.READY, DeliveryRun.Status.IN_PROGRESS],
    ).exclude(status=RunStop.Status.REMOVED).values("delivery_id")
    return base.annotate(current_resource_user=Subquery(latest)).filter(
        Q(current_resource_user=user.pk) | Q(pk__in=run_delivery_ids)
    ).distinct()
