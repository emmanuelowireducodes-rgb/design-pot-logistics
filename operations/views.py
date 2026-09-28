import hashlib
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from .access import visible_deliveries
from .forms import DeliveryFilterForm, SignInForm
from .models import (
    DeliveryRun,
    DomainEvent,
    LedgerEntry,
    LoginAttempt,
    Notification,
    Organization,
    Resource,
    RunStop,
)
from .services import emit_event


class SignInView(LoginView):
    template_name = "registration/login.html"
    authentication_form = SignInForm
    redirect_authenticated_user = True

    def post(self, request, *args, **kwargs):
        # No forwarded header trust. Configure proxy rate limiting before public deployment.
        key = hashlib.sha256(request.META.get("REMOTE_ADDR", "unknown").encode()).hexdigest()
        with transaction.atomic():
            attempt, _ = LoginAttempt.objects.select_for_update().get_or_create(key=key)
            now = timezone.now()
            if now - attempt.started_at > timedelta(minutes=15):
                attempt.started_at, attempt.count = now, 0
            if attempt.count >= 10:
                return render(
                    request,
                    "registration/login.html",
                    {"form": self.get_form(), "limited": True},
                    status=429,
                )
            attempt.count += 1
            attempt.save()
        return super().post(request, *args, **kwargs)

    def form_valid(self, form):
        response = super().form_valid(form)
        emit_event(
            actor=self.request.user,
            kind="session.signed_in",
            title="Signed in",
            message="A new session was started.",
            recipients=[],
        )
        return response


@login_required
def home(request):
    prefix = (
        "admin"
        if request.user.role == "admin"
        else "client"
        if request.user.role == "client"
        else "field"
    )
    return redirect(f"/{prefix}/dashboard/")


def check_portal(request, portal):
    allowed = {"admin": ("admin",), "client": ("client",), "field": ("rider", "driver")}
    if request.user.role not in allowed.get(portal, ()):
        raise PermissionDenied


@login_required
def page(request, portal, section):
    check_portal(request, portal)
    from .context import shell

    nav = shell(request)["navigation"]
    sections = {item["key"]: item["label"] for item in nav}
    if portal == "admin":
        sections["components"] = "Design system"
        sections["live"] = "Live operations"
    if section == "components" and portal == "admin":
        return render(
            request, "operations/components.html", {"title": "Design system", "section": section}
        )
    if section not in sections:
        from django.http import Http404

        raise Http404
    context = {"section": section, "title": sections[section], "today": timezone.localdate()}
    deliveries = visible_deliveries(request.user)
    if section == "dashboard":
        start = timezone.make_aware(datetime.combine(timezone.localdate(), time.min))
        today = deliveries.filter(created_at__gte=start, created_at__lt=start + timedelta(days=1))
        counts = dict(today.values_list("status").annotate(total=Count("id")))
        context.update(
            {
                "counts": counts,
                "total": sum(counts.values()),
                "unassigned": counts.get("requested", 0) + counts.get("confirmed", 0),
                "in_progress": sum(
                    counts.get(s, 0) for s in ("assigned", "to_pickup", "picked_up", "in_transit")
                ),
                "completed": counts.get("delivered", 0),
                "issues": sum(counts.get(s, 0) for s in ("failed", "unreachable", "returned")),
                "deliveries": deliveries.order_by("-created_at")[:6],
                "premium": deliveries.filter(service="premium", window_end__gte=timezone.now())
                .exclude(status__in=["delivered", "cancelled"])
                .order_by("window_start")[:3],
            }
        )
        if portal == "admin":
            context["fleet"] = Resource.objects.select_related("user").order_by(
                "user__first_name", "pk"
            )[:4]
            context["active_fleet"] = Resource.objects.exclude(status="off_duty").count()
            ledger = (
                LedgerEntry.objects.filter(currency="GHS")
                .values("kind")
                .annotate(total=Sum("amount"))
            )
            sums = {row["kind"]: row["total"] for row in ledger}
            context["cod_collected"] = sums.get("cod_collected", Decimal(0))
            context["cod_pending"] = sums.get("cod_collected", Decimal(0)) - sums.get(
                "cod_reconciled", Decimal(0)
            )
            active_memberships = RunStop.objects.filter(
                run__status__in=[
                    DeliveryRun.Status.DRAFT,
                    DeliveryRun.Status.READY,
                    DeliveryRun.Status.IN_PROGRESS,
                ]
            ).exclude(status=RunStop.Status.REMOVED).values("delivery_id")
            context["admin_attention"] = {
                "quotes": deliveries.filter(job__quote_requested=True).count(),
                "dispatch": deliveries.filter(status="confirmed")
                .exclude(pk__in=active_memberships)
                .count(),
                "runs": DeliveryRun.objects.filter(
                    status__in=[DeliveryRun.Status.READY, DeliveryRun.Status.IN_PROGRESS]
                ).count(),
                "exceptions": deliveries.filter(
                    status__in=["failed", "unreachable", "returned", "rescheduled"]
                ).count(),
                "cod_pending": context["cod_pending"],
                "unread": Notification.objects.filter(
                    recipient=request.user, read_at__isnull=True
                ).count(),
            }
        elif portal == "client":
            context["active_deliveries"] = deliveries.exclude(
                status__in=["delivered", "cancelled"]
            ).order_by("-created_at")[:4]
        return render(request, "operations/dashboard.html", context)
    if section == "deliveries":
        form = DeliveryFilterForm(request.GET)
        if form.is_valid():
            if form.cleaned_data["q"]:
                q = form.cleaned_data["q"]
                deliveries = deliveries.filter(
                    Q(reference__icontains=q)
                    | Q(recipient_name__icontains=q)
                    | Q(destination__area__icontains=q)
                )
            if form.cleaned_data["status"]:
                status = form.cleaned_data["status"]
                if status == "unassigned":
                    deliveries = deliveries.filter(status__in=["requested", "confirmed"])
                elif status == "issues":
                    deliveries = deliveries.filter(
                        status__in=["failed", "unreachable", "returned", "rescheduled"]
                    )
                else:
                    deliveries = deliveries.filter(status=status)
        context.update(
            {
                "filter_form": form,
                "page_obj": Paginator(deliveries.order_by("-created_at"), 20).get_page(
                    request.GET.get("page")
                ),
                "q": request.GET.get("q", "")[:100],
                "status_filter": request.GET.get("status", ""),
            }
        )
        return render(request, "operations/deliveries.html", context)
    if section == "fleet":
        context["fleet"] = Paginator(
            Resource.objects.select_related("user").order_by("user__first_name", "pk"), 20
        ).get_page(request.GET.get("page"))
        return render(request, "operations/fleet.html", context)
    if section == "clients":
        context["clients"] = Paginator(Organization.objects.order_by("name"), 20).get_page(
            request.GET.get("page")
        )
        return render(request, "operations/clients.html", context)
    if section == "notifications":
        context["unread_count"] = Notification.objects.filter(
            recipient=request.user, read_at__isnull=True
        ).count()
        context["page_obj"] = Paginator(
            Notification.objects.filter(recipient=request.user)
            .select_related("event")
            .order_by("-created_at"),
            20,
        ).get_page(request.GET.get("page"))
        return render(request, "operations/notifications.html", context)
    if section == "settings":
        context["vapid_public_key"] = settings.VAPID_PUBLIC_KEY
        context["web_push_configured"] = settings.WEB_PUSH_ENABLED
        return render(request, "operations/settings.html", context)
    descriptions = {
        "dispatch": (
            "Assignment starts with availability.",
            "Manual assignment will bring together vehicle requirements, workload and the rider or driver's schedule.",
        ),
        "calendar": (
            "A clearer view of everyone's day.",
            "People and their associated vehicles will appear here with delivery windows, breaks and availability.",
        ),
        "live": (
            "Your operation, in view.",
            "Live locations will appear once tracking is connected. No location data is being collected in this phase.",
        ),
        "finance": (
            "Every collection accounted for.",
            "Delivery charges and product COD will be tracked separately, with a complete reconciliation history.",
        ),
        "reports": (
            "Turn delivery history into insight.",
            "Operational and financial reporting will be built on auditable delivery and transaction records.",
        ),
        "pricing": (
            "Consistent pricing. Clear expectations.",
            "Versioned bike, car, Standard and Premium rules are prepared in the schema. Business rates have not been set.",
        ),
        "booking": (
            "Your next delivery starts here.",
            "Single and bulk booking, quote review and confirmation will be introduced in the next phase.",
        ),
    }
    context["empty_title"], context["empty_description"] = descriptions[section]
    return render(request, "operations/upcoming.html", context)


@login_required
def delivery_detail(request, delivery_id):
    delivery = get_object_or_404(visible_deliveries(request.user), pk=delivery_id)
    return render(
        request,
        "operations/detail.html",
        {
            "title": delivery.reference,
            "section": "deliveries",
            "delivery": delivery,
            "history": delivery.history.select_related("actor").order_by("created_at"),
            "proof": getattr(delivery, "proof", None),
        },
    )


@login_required
@require_POST
def read_notification(request, notification_id):
    with transaction.atomic():
        notification = get_object_or_404(
            Notification.objects.select_for_update(), pk=notification_id, recipient=request.user
        )
        if not notification.read_at:
            notification.read_at = timezone.now()
            notification.save(update_fields=["read_at"])
            DomainEvent.objects.create(
                actor=request.user,
                kind="notification.read",
                payload={"notification_id": str(notification.pk)},
            )
    messages.success(request, "Notification marked as read.")
    prefix = (
        "admin"
        if request.user.role == "admin"
        else "client"
        if request.user.role == "client"
        else "field"
    )
    return redirect(f"/{prefix}/notifications/")


@login_required
@require_POST
def read_all_notifications(request):
    with transaction.atomic():
        unread = Notification.objects.select_for_update().filter(
            recipient=request.user, read_at__isnull=True
        )
        unread_count = unread.count()
        unread.update(read_at=timezone.now())
        if unread_count:
            DomainEvent.objects.create(
                actor=request.user,
                kind="notifications.read_all",
                payload={"count": unread_count},
            )
    messages.success(request, "All notifications marked as read.")
    prefix = "admin" if request.user.role == "admin" else "client" if request.user.role == "client" else "field"
    return redirect(f"/{prefix}/notifications/")


@login_required
@require_GET
@never_cache
def notification_state(request):
    cursor_text = request.GET.get("since", "")[:64]
    cursor = parse_datetime(cursor_text) if cursor_text else timezone.now()
    if cursor and timezone.is_naive(cursor):
        cursor = timezone.make_aware(cursor, timezone.get_current_timezone())
    query = Notification.objects.filter(recipient=request.user)
    new_items = list(query.filter(created_at__gt=cursor).order_by("created_at", "pk")[:10]) if cursor else []
    latest = (
        new_items[-1].created_at
        if len(new_items) == 10
        else query.order_by("-created_at").values_list("created_at", flat=True).first()
    )
    return JsonResponse(
        {
            "unread": query.filter(read_at__isnull=True).count(),
            "latest": latest.isoformat() if latest else cursor_text,
            "items": [
                {"title": item.title, "message": item.message, "id": str(item.pk)}
                for item in new_items
            ],
        }
    )
