"""Database-backed Phase 4 finance and reporting query helpers."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db.models import (
    Case,
    CharField,
    Count,
    DecimalField,
    F,
    OuterRef,
    Prefetch,
    Q,
    Subquery,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce, Concat
from django.utils import timezone

from .models import (
    Assignment,
    ClientSettlement,
    Delivery,
    DeliveryRun,
    LedgerEntry,
    QuoteConfirmation,
    RunStop,
    StatusEvent,
)

MONEY = DecimalField(max_digits=14, decimal_places=2)
ZERO = Value(Decimal("0.00"), output_field=MONEY)
DELIVERY_STATUSES = list(Delivery.Status.choices)


def date_bounds(data):
    """Return a validated date form and aware half-open bounds in the local timezone."""
    from .finance_forms import DateRangeForm

    values = data or {"period": "this_month"}
    form = DateRangeForm(values)
    if not form.is_valid():
        today = timezone.localdate()
        return form, _aware(today), _aware(today + timedelta(days=1)), today, today
    today = timezone.localdate()
    period = form.cleaned_data.get("period") or "this_month"
    if period == "custom":
        start, end = form.cleaned_data["start_date"], form.cleaned_data["end_date"]
    elif period == "today":
        start = end = today
    elif period == "yesterday":
        start = end = today - timedelta(days=1)
    elif period == "this_week":
        start = today - timedelta(days=today.weekday())
        end = today
    else:
        start = today.replace(day=1)
        end = today
    return form, _aware(start), _aware(end + timedelta(days=1)), start, end


def _aware(day: date):
    return timezone.make_aware(datetime.combine(day, time.min))


def money_subquery(kind, *, outer="pk", value="amount"):
    return (
        LedgerEntry.objects.filter(delivery_id=OuterRef(outer), kind=kind)
        .order_by()
        .values(value)[:1]
    )


def financial_deliveries(queryset):
    collected = money_subquery("cod_collected")
    reconciled = money_subquery("cod_reconciled")
    settled = (
        LedgerEntry.objects.filter(reconciled_entry__delivery_id=OuterRef("pk"), kind="cod_settled")
        .order_by()
        .values("reconciled_entry__delivery_id")
        .annotate(total=Sum("amount"))
        .values("total")[:1]
    )
    completion = (
        StatusEvent.objects.filter(delivery_id=OuterRef("pk"), status=Delivery.Status.DELIVERED)
        .order_by("-sequence", "-created_at")
        .values("created_at")[:1]
    )
    confirmation = QuoteConfirmation.objects.filter(job_id=OuterRef("job_id")).select_related(
        "quote"
    )
    return (
        queryset.prefetch_related(
            Prefetch(
                "assignments",
                queryset=Assignment.objects.select_related("resource__user").order_by("-id"),
                to_attr="finance_assignments",
            )
        )
        .annotate(
            collected_amount=Coalesce(Subquery(collected), ZERO),
            reconciled_amount=Coalesce(Subquery(reconciled), ZERO),
            settled_amount=Coalesce(Subquery(settled), ZERO),
            completed_at=Subquery(completion),
            delivery_fee=Subquery(confirmation.values("quote__amount")[:1]),
            delivery_fee_currency=Subquery(confirmation.values("quote__currency")[:1]),
        )
        .annotate(
            pending_settlement_amount=F("reconciled_amount") - F("settled_amount"),
            cod_status=Case(
                When(cod_amount=0, then=Value("Not applicable")),
                When(collected_amount=0, then=Value("Expected")),
                When(collected_amount__lt=F("cod_amount"), then=Value("Mismatch")),
                When(collected_amount__gt=F("cod_amount"), then=Value("Mismatch")),
                When(reconciled_amount__lt=F("collected_amount"), then=Value("Collected")),
                When(settled_amount__lt=F("reconciled_amount"), then=Value("Reconciled")),
                default=Value("Settled to Client"),
                output_field=CharField(),
            ),
        )
    )


def latest_resource_user_subquery():
    return (
        Assignment.objects.filter(delivery_id=OuterRef("pk"))
        .order_by("-id")
        .values("resource__user_id")[:1]
    )


def apply_delivery_filters(queryset, filters, *, date_field=None, start=None, end=None):
    if date_field and start and end:
        queryset = queryset.filter(**{f"{date_field}__gte": start, f"{date_field}__lt": end})
    if organization := filters.get("organization"):
        queryset = queryset.filter(job__organization_id=organization.pk)
    if resource := filters.get("resource"):
        queryset = queryset.annotate(
            current_resource_user_id=Subquery(latest_resource_user_subquery())
        ).filter(current_resource_user_id=resource.user_id)
    if vehicle := filters.get("vehicle"):
        queryset = queryset.filter(vehicle_type=vehicle)
    if service := filters.get("service"):
        queryset = queryset.filter(service=service)
    if status := filters.get("status"):
        queryset = queryset.filter(status=status)
    if area := filters.get("area"):
        queryset = queryset.filter(
            Q(pickup__area__icontains=area) | Q(destination__area__icontains=area)
        )
    if q := filters.get("q"):
        queryset = queryset.filter(
            Q(reference__icontains=q)
            | Q(recipient_name__icontains=q)
            | Q(job__organization__name__icontains=q)
            | Q(destination__area__icontains=q)
        )
    return queryset


def completed_revenue(filters, start, end):
    queryset = financial_deliveries(
        Delivery.objects.filter(status=Delivery.Status.DELIVERED).select_related(
            "job__organization", "pickup__zone", "destination__zone"
        )
    )
    queryset = apply_delivery_filters(
        queryset, filters, date_field="completed_at", start=start, end=end
    )
    return queryset.filter(delivery_fee__isnull=False, delivery_fee_currency="GHS").order_by(
        "-completed_at", "-pk"
    )


def collected_entries(filters, start, end):
    reconciliation = (
        LedgerEntry.objects.filter(delivery_id=OuterRef("delivery_id"), kind="cod_reconciled")
        .order_by()
        .values("delivery_id")
        .annotate(total=Sum("amount"))
        .values("total")[:1]
    )
    settled = (
        LedgerEntry.objects.filter(
            reconciled_entry__delivery_id=OuterRef("delivery_id"),
            reconciled_entry__kind="cod_reconciled",
            kind="cod_settled",
        )
        .order_by()
        .values("reconciled_entry__delivery_id")
        .annotate(total=Sum("amount"))
        .values("total")[:1]
    )
    queryset = LedgerEntry.objects.filter(
        kind="cod_collected", currency="GHS", created_at__gte=start, created_at__lt=end
    ).select_related("actor__resource", "delivery__job__organization", "delivery__destination")
    if organization := filters.get("organization"):
        queryset = queryset.filter(delivery__job__organization=organization)
    if resource := filters.get("resource"):
        queryset = queryset.filter(actor=resource.user)
    if vehicle := filters.get("vehicle"):
        queryset = queryset.filter(delivery__vehicle_type=vehicle)
    if service := filters.get("service"):
        queryset = queryset.filter(delivery__service=service)
    if status := filters.get("status"):
        queryset = queryset.filter(delivery__status=status)
    if area := filters.get("area"):
        queryset = queryset.filter(
            Q(delivery__pickup__area__icontains=area)
            | Q(delivery__destination__area__icontains=area)
        )
    return queryset.annotate(
        reconciled_amount=Coalesce(Subquery(reconciliation), ZERO),
        settled_amount=Coalesce(Subquery(settled), ZERO),
    ).order_by("-created_at", "-pk")


def reconciliation_entries(*, organization=None, resource=None, start=None, end=None, filters=None):
    filters = filters or {}
    collected = (
        LedgerEntry.objects.filter(delivery_id=OuterRef("pk"), kind="cod_collected")
        .order_by("-created_at")
        .values("amount")[:1]
    )
    collected_at = (
        LedgerEntry.objects.filter(delivery_id=OuterRef("pk"), kind="cod_collected")
        .order_by("-created_at")
        .values("created_at")[:1]
    )
    collection_actor = (
        LedgerEntry.objects.filter(delivery_id=OuterRef("pk"), kind="cod_collected")
        .order_by("-created_at")
        .values("actor_id")[:1]
    )
    collection_actor_name = (
        LedgerEntry.objects.filter(delivery_id=OuterRef("pk"), kind="cod_collected")
        .order_by("-created_at")
        .annotate(name=Concat("actor__first_name", Value(" "), "actor__last_name"))
        .values("name")[:1]
    )
    reconciled = money_subquery("cod_reconciled")
    settled = (
        LedgerEntry.objects.filter(reconciled_entry__delivery_id=OuterRef("pk"), kind="cod_settled")
        .order_by()
        .values("reconciled_entry__delivery_id")
        .annotate(total=Sum("amount"))
        .values("total")[:1]
    )
    assigned_user = latest_resource_user_subquery()
    queryset = (
        Delivery.objects.filter(payment_method="cod", currency="GHS")
        .select_related("job__organization", "destination__zone")
        .annotate(
            collected_amount=Coalesce(Subquery(collected), ZERO),
            collected_at=Subquery(collected_at),
            collection_actor_id=Subquery(collection_actor),
            collection_actor_name=Subquery(collection_actor_name),
            reconciled_amount=Coalesce(Subquery(reconciled), ZERO),
            settled_amount=Coalesce(Subquery(settled), ZERO),
            assigned_user_id=Subquery(assigned_user),
            difference=F("cod_amount") - Coalesce(Subquery(collected), ZERO),
        )
    )
    if organization:
        queryset = queryset.filter(job__organization=organization)
    if resource:
        queryset = queryset.filter(collection_actor_id=resource.user_id)
    if start and end:
        queryset = queryset.filter(
            Q(collected_at__gte=start, collected_at__lt=end)
            | Q(collected_at__isnull=True, created_at__gte=start, created_at__lt=end)
        )
    if vehicle := filters.get("vehicle"):
        queryset = queryset.filter(vehicle_type=vehicle)
    if service := filters.get("service"):
        queryset = queryset.filter(service=service)
    if status := filters.get("status"):
        queryset = queryset.filter(status=status)
    if area := filters.get("area"):
        queryset = queryset.filter(
            Q(pickup__area__icontains=area) | Q(destination__area__icontains=area)
        )
    if q := filters.get("q"):
        queryset = queryset.filter(
            Q(reference__icontains=q)
            | Q(recipient_name__icontains=q)
            | Q(job__organization__name__icontains=q)
        )
    return queryset.order_by("-collected_at", "-created_at", "-pk")


def rider_cod_positions(filters, start, end):
    entries = (
        collected_entries(filters, start, end)
        .values("actor_id")
        .annotate(
            name=F("actor__first_name"),
            last_name=F("actor__last_name"),
            cod_collected=Sum("amount"),
            cod_reconciled=Sum("reconciled_amount"),
            cod_settled=Sum("settled_amount"),
            cod_outstanding=Sum(F("amount") - F("reconciled_amount")),
            cod_pending_settlement=Sum(F("reconciled_amount") - F("settled_amount")),
        )
        .order_by("name", "last_name")
    )
    return entries


def dashboard_totals(filters, start, end):
    revenue = completed_revenue(filters, start, end).aggregate(
        total=Coalesce(Sum("delivery_fee"), ZERO), count=Count("pk")
    )
    collected = collected_entries(filters, start, end).aggregate(
        total_collected=Coalesce(Sum("amount"), ZERO),
        total_outstanding=Coalesce(Sum(F("amount") - F("reconciled_amount")), ZERO),
    )
    reconciled_transactions = LedgerEntry.objects.filter(
        kind="cod_reconciled", currency="GHS", created_at__gte=start, created_at__lt=end
    )
    if organization := filters.get("organization"):
        reconciled_transactions = reconciled_transactions.filter(
            delivery__job__organization=organization
        )
    if resource := filters.get("resource"):
        reconciled_transactions = reconciled_transactions.filter(
            delivery__assignments__resource=resource
        ).distinct()
    if vehicle := filters.get("vehicle"):
        reconciled_transactions = reconciled_transactions.filter(delivery__vehicle_type=vehicle)
    if service := filters.get("service"):
        reconciled_transactions = reconciled_transactions.filter(delivery__service=service)
    if status := filters.get("status"):
        reconciled_transactions = reconciled_transactions.filter(delivery__status=status)
    if area := filters.get("area"):
        reconciled_transactions = reconciled_transactions.filter(
            Q(delivery__pickup__area__icontains=area)
            | Q(delivery__destination__area__icontains=area)
        )
    if q := filters.get("q"):
        reconciled_transactions = reconciled_transactions.filter(
            Q(delivery__reference__icontains=q)
            | Q(delivery__recipient_name__icontains=q)
            | Q(delivery__job__organization__name__icontains=q)
        )
    outstanding_settlement = reconciled_transactions.annotate(
        settled=Coalesce(
            Subquery(
                LedgerEntry.objects.filter(reconciled_entry_id=OuterRef("pk"), kind="cod_settled")
                .order_by()
                .values("reconciled_entry_id")
                .annotate(total=Sum("amount"))
                .values("total")[:1]
            ),
            ZERO,
        )
    ).aggregate(amount=Coalesce(Sum(F("amount") - F("settled")), ZERO))["amount"]
    settled = ClientSettlement.objects.filter(
        currency="GHS", created_at__gte=start, created_at__lt=end
    )
    if organization := filters.get("organization"):
        settled = settled.filter(organization=organization)
    return {
        "completed_count": revenue["count"],
        "delivery_revenue": revenue["total"],
        "cod_collected": collected["total_collected"],
        "cod_outstanding": collected["total_outstanding"],
        "cod_reconciled": reconciled_transactions.aggregate(amount=Coalesce(Sum("amount"), ZERO))[
            "amount"
        ],
        "cod_pending_settlement": outstanding_settlement,
        "cod_settled": settled.aggregate(amount=Coalesce(Sum("amount"), ZERO))["amount"],
    }


def run_report(filters, start, end):
    queryset = DeliveryRun.objects.filter(
        operational_date__gte=start.date(), operational_date__lt=end.date()
    )
    if resource := filters.get("resource"):
        queryset = queryset.filter(resource=resource)
    if vehicle := filters.get("vehicle"):
        queryset = queryset.filter(resource__vehicle_type=vehicle)
    if status := filters.get("status"):
        queryset = queryset.filter(status=status)
    return (
        queryset.select_related("resource__user")
        .annotate(
            total_stops=Count("stops", filter=~Q(stops__status=RunStop.Status.REMOVED)),
            completed_stops=Count("stops", filter=Q(stops__status=RunStop.Status.COMPLETED)),
            deferred_stops=Count(
                "stops",
                filter=Q(stops__status__in=[RunStop.Status.SKIPPED, RunStop.Status.UNAVAILABLE]),
            ),
        )
        .order_by("-operational_date", "-created_at")
    )
