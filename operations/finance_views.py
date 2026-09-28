# mypy: disable-error-code="attr-defined"
import csv
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.db.models import Count, F, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django.http import Http404, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods

from . import workflow
from .access import roles_required
from .finance_forms import ReconciliationFilterForm, ReportFilterForm, SettlementForm
from .finance_queries import (
    DELIVERY_STATUSES,
    ZERO,
    apply_delivery_filters,
    collected_entries,
    completed_revenue,
    dashboard_totals,
    date_bounds,
    financial_deliveries,
    reconciliation_entries,
    rider_cod_positions,
    run_report,
)
from .models import (
    Assignment,
    ClientSettlement,
    Delivery,
    DeliveryRun,
    LedgerEntry,
    Organization,
    Resource,
    StatusEvent,
    User,
)


def _period_form(request):
    form, start, end, start_day, end_day = date_bounds(
        request.GET if request.GET else {"period": "this_month"}
    )
    return form, start, end, start_day, end_day


def _report_form(request, *, status_choices=None):
    form = ReportFilterForm(
        request.GET if request.GET else {"period": "this_month"},
        status_choices=status_choices or DELIVERY_STATUSES,
    )
    form.fields["organization"].queryset = Organization.objects.order_by("name")
    form.fields["resource"].queryset = (
        Resource.objects.filter(
            user__is_active=True, user__role__in=[User.Role.RIDER, User.Role.DRIVER]
        )
        .select_related("user")
        .order_by("user__first_name", "user__last_name", "pk")
    )
    return form


def _filters(form):
    return form.cleaned_data if form.is_valid() else {}


def _period_context(form, start_day, end_day):
    return {
        "period_form": form,
        "start_day": start_day,
        "end_day": end_day,
        "range_label": f"{start_day:%d %b %Y} – {end_day:%d %b %Y}",
    }


@roles_required("admin")
@require_GET
def finance_dashboard(request):
    form, start, end, start_day, end_day = _period_form(request)
    filters = _filters(form)
    totals = dashboard_totals(filters, start, end)
    rider_positions = rider_cod_positions(filters, start, end)
    reconciliation_url = "/admin/finance/cod/"
    return render(
        request,
        "operations/finance_dashboard.html",
        {
            "title": "Finance",
            "section": "finance",
            "totals": totals,
            "rider_positions": rider_positions,
            "reconciliation_url": reconciliation_url,
            **_period_context(form, start_day, end_day),
        },
    )


@roles_required("admin")
@require_GET
def cod_reconciliation(request):
    form, start, end, start_day, end_day = _period_form(request)
    filters_form = ReconciliationFilterForm(
        request.GET if request.GET else {"period": "this_month"}
    )
    filters_form.fields["organization"].queryset = Organization.objects.order_by("name")
    filters_form.fields["resource"].queryset = (
        Resource.objects.filter(
            user__is_active=True, user__role__in=[User.Role.RIDER, User.Role.DRIVER]
        )
        .select_related("user")
        .order_by("user__first_name", "user__last_name", "pk")
    )
    values = filters_form.cleaned_data if filters_form.is_valid() else {}
    organization = values.get("organization")
    resource = values.get("resource")
    deliveries = reconciliation_entries(
        organization=organization,
        resource=resource,
        start=start,
        end=end,
        filters=values,
    )
    state = values.get("cod_status")
    if state == "expected":
        deliveries = deliveries.filter(collected_amount=0)
    elif state == "collected":
        deliveries = deliveries.filter(collected_amount__gt=F("reconciled_amount"))
    elif state == "reconciled":
        deliveries = deliveries.filter(reconciled_amount__gt=F("settled_amount"))
    elif state == "settled":
        deliveries = deliveries.filter(
            collected_amount__gt=0, settled_amount__gte=F("reconciled_amount")
        )
    elif state == "mismatch":
        deliveries = deliveries.filter(collected_amount__gt=0).exclude(
            collected_amount=F("cod_amount")
        )
    positions = rider_cod_positions(
        {"organization": organization, "resource": resource}, start, end
    )
    return render(
        request,
        "operations/cod_reconciliation.html",
        {
            "title": "COD reconciliation",
            "section": "finance",
            "form": filters_form,
            "page_obj": Paginator(deliveries, 25).get_page(request.GET.get("page")),
            "rider_positions": positions,
            "range_label": f"{start_day:%d %b %Y} – {end_day:%d %b %Y}",
        },
    )


def _eligible_reconciliations(organization):
    if not organization:
        return LedgerEntry.objects.none()
    return (
        LedgerEntry.objects.filter(
            kind="cod_reconciled",
            currency="GHS",
            delivery__job__organization=organization,
            settlement_entry__isnull=True,
        )
        .select_related("delivery__job__organization", "delivery")
        .order_by("created_at", "pk")
    )


@roles_required("admin")
@require_http_methods(["GET", "POST"])
def client_settlements(request):
    organization_id = (
        request.POST.get("organization")
        if request.method == "POST"
        else request.GET.get("organization")
    )
    organization = (
        Organization.objects.filter(pk=organization_id).first() if organization_id else None
    )
    form = SettlementForm(
        request.POST or None,
        initial={"request_key": uuid.uuid4(), "organization": organization},
    )
    form.fields["organization"].queryset = Organization.objects.order_by("name")
    form.fields["reconciliation_ids"].queryset = _eligible_reconciliations(organization)
    if request.method == "POST" and form.is_valid():
        try:
            settlement = workflow.settle_reconciled_cod(
                actor=request.user,
                organization_id=form.cleaned_data["organization"].pk,
                reconciliation_ids=[entry.pk for entry in form.cleaned_data["reconciliation_ids"]],
                request_key=form.cleaned_data["request_key"],
                method=form.cleaned_data["method"],
                external_reference=form.cleaned_data["external_reference"],
                note=form.cleaned_data["note"],
            )
            messages.success(request, "Settlement recorded. No transfer was initiated by this app.")
            return redirect("settlement-detail", settlement_id=settlement.pk)
        except ValidationError as exc:
            for error in exc.messages:
                form.add_error(None, error)
        except IntegrityError:
            form.add_error(
                None,
                "One or more selected items changed while you were recording this settlement. Refresh and review the eligible items.",
            )
    eligible = _eligible_reconciliations(organization)
    settled = ClientSettlement.objects.select_related("organization", "recorded_by").annotate(
        item_count=Count("entries", filter=Q(entries__kind="cod_settled"))
    )
    if organization:
        settled = settled.filter(organization=organization)
    return render(
        request,
        "operations/client_settlements.html",
        {
            "title": "Client settlements",
            "section": "finance",
            "form": form,
            "organization": organization,
            "eligible_page": Paginator(eligible, 50).get_page(request.GET.get("page")),
            "settlements": Paginator(settled.order_by("-created_at"), 20).get_page(
                request.GET.get("settled_page")
            ),
        },
    )


@roles_required("admin")
@require_GET
def settlement_detail(request, settlement_id):
    settlement = get_object_or_404(
        ClientSettlement.objects.select_related("organization", "recorded_by"), pk=settlement_id
    )
    entries = (
        settlement.entries.filter(kind="cod_settled")
        .select_related("delivery__job__organization", "reconciled_entry")
        .order_by("created_at", "pk")
    )
    return render(
        request,
        "operations/settlement_detail.html",
        {
            "title": settlement.reference,
            "section": "finance",
            "settlement": settlement,
            "entries": entries,
        },
    )


@roles_required("client")
@require_GET
def client_finance(request):
    form, start, end, start_day, end_day = _period_form(request)
    organization = request.user.organization if request.user.organization_id else None
    totals = (
        dashboard_totals({"organization": organization}, start, end)
        if organization
        else {
            "delivery_revenue": Decimal("0.00"),
            "cod_collected": Decimal("0.00"),
            "cod_pending_settlement": Decimal("0.00"),
            "cod_settled": Decimal("0.00"),
        }
    )
    deliveries = (
        financial_deliveries(Delivery.objects.filter(job__organization=organization))
        if organization
        else Delivery.objects.none()
    )
    deliveries = (
        deliveries.filter(
            Q(created_at__gte=start, created_at__lt=end)
            | Q(completed_at__gte=start, completed_at__lt=end)
        )
        .select_related("job__organization", "pickup", "destination")
        .order_by("-created_at", "-pk")
    )
    settlements = (
        ClientSettlement.objects.filter(organization=organization)
        if organization
        else ClientSettlement.objects.none()
    )
    settlements = (
        settlements.filter(created_at__gte=start, created_at__lt=end)
        .annotate(item_count=Count("entries", filter=Q(entries__kind="cod_settled")))
        .order_by("-created_at")
    )
    return render(
        request,
        "operations/client_finance.html",
        {
            "title": "COD & payments",
            "section": "finance",
            "totals": totals,
            "page_obj": Paginator(deliveries, 20).get_page(request.GET.get("page")),
            "settlements": settlements,
            **_period_context(form, start_day, end_day),
        },
    )


@roles_required("admin")
@require_GET
def reports_index(request):
    return render(
        request,
        "operations/reports_index.html",
        {
            "title": "Reports",
            "section": "reports",
            "report_items": [(kind, *details) for kind, details in REPORTS.items()],
        },
    )


REPORTS = {
    "revenue": (
        "Delivery Revenue",
        "Delivery fee revenue uses the confirmed quote snapshot and delivery completion time.",
    ),
    "clients": (
        "Client Activity",
        "Counts are based on booking creation; completed fees use completion time.",
    ),
    "riders": (
        "Rider / Driver Activity",
        "Operational facts are grouped by assigned or acting Rider / Driver.",
    ),
    "status": (
        "Delivery Status",
        "Counts show each delivery's current lifecycle status by creation date.",
    ),
    "runs": ("Delivery Runs", "Run activity uses the operational date and stored stop statuses."),
    "cod": (
        "COD Activity",
        "Collections use collection transaction time; reconciliations use ledger time.",
    ),
}


def _client_report_rows(filters, start, end):
    base = apply_delivery_filters(Delivery.objects.select_related("job__organization"), filters)
    created = (
        base.filter(created_at__gte=start, created_at__lt=end)
        .values("job__organization_id", "job__organization__name")
        .annotate(created=Count("pk"), cod_expected=Coalesce(Sum("cod_amount"), ZERO))
    )
    completed = (
        completed_revenue(filters, start, end)
        .values("job__organization_id", "job__organization__name")
        .annotate(completed=Count("pk"), delivery_fees=Coalesce(Sum("delivery_fee"), ZERO))
    )
    collected = (
        collected_entries(filters, start, end)
        .values("delivery__job__organization_id", "delivery__job__organization__name")
        .annotate(cod_collected=Coalesce(Sum("amount"), ZERO))
    )
    settlement_query = ClientSettlement.objects.filter(
        created_at__gte=start, created_at__lt=end, currency="GHS"
    )
    if organization := filters.get("organization"):
        settlement_query = settlement_query.filter(organization=organization)
    if q := filters.get("q"):
        settlement_query = settlement_query.filter(organization__name__icontains=q)
    settlements = settlement_query.values("organization_id", "organization__name").annotate(
        cod_settled=Coalesce(Sum("amount"), ZERO)
    )
    result: dict[object, dict[str, object]] = {}
    for group, key_id, key_name in [
        (created, "job__organization_id", "job__organization__name"),
        (completed, "job__organization_id", "job__organization__name"),
        (collected, "delivery__job__organization_id", "delivery__job__organization__name"),
        (settlements, "organization_id", "organization__name"),
    ]:
        for item in group:
            row = result.setdefault(
                item[key_id],
                {
                    "name": item[key_name],
                    "created": 0,
                    "completed": 0,
                    "delivery_fees": Decimal("0.00"),
                    "cod_expected": Decimal("0.00"),
                    "cod_collected": Decimal("0.00"),
                    "cod_settled": Decimal("0.00"),
                },
            )
            for field in (
                "created",
                "completed",
                "delivery_fees",
                "cod_expected",
                "cod_collected",
                "cod_settled",
            ):
                if field in item:
                    row[field] = item[field]
    rows = [
        [
            row[key]
            for key in (
                "name",
                "created",
                "completed",
                "delivery_fees",
                "cod_expected",
                "cod_collected",
                "cod_settled",
            )
        ]
        for row in result.values()
    ]
    rows.sort(key=lambda row: str(row[0]).casefold())
    return rows


def _rider_report_rows(filters, start, end):
    filtered_deliveries = apply_delivery_filters(Delivery.objects.all(), filters)
    resources = Resource.objects.filter(
        user__role__in=[User.Role.RIDER, User.Role.DRIVER]
    ).select_related("user")
    if filters.get("resource"):
        resources = resources.filter(pk=filters["resource"].pk)
    if filters.get("vehicle"):
        resources = resources.filter(vehicle_type=filters["vehicle"])
    assignments = (
        Assignment.objects.filter(
            created_at__gte=start,
            created_at__lt=end,
            delivery__in=filtered_deliveries,
        )
        .values("resource_id")
        .annotate(assigned=Count("delivery_id", distinct=True))
    )
    assigned_map = {row["resource_id"]: row["assigned"] for row in assignments}
    completed = (
        completed_revenue(filters, start, end)
        .annotate(
            resource_id=Subquery(
                Assignment.objects.filter(delivery_id=OuterRef("pk"))
                .order_by("-id")
                .values("resource_id")[:1]
            )
        )
        .values("resource_id")
        .annotate(completed=Count("pk"))
    )
    completed_map = {row["resource_id"]: row["completed"] for row in completed}
    exceptions = (
        StatusEvent.objects.filter(
            created_at__gte=start,
            created_at__lt=end,
            delivery__in=filtered_deliveries,
            status__in=[
                Delivery.Status.UNREACHABLE,
                Delivery.Status.FAILED,
                Delivery.Status.RETURNED,
            ],
            actor__role__in=[User.Role.RIDER, User.Role.DRIVER],
        )
        .values("actor_id")
        .annotate(total=Count("delivery_id", distinct=True))
    )
    exception_map = {row["actor_id"]: row["total"] for row in exceptions}
    runs = (
        DeliveryRun.objects.filter(
            status=DeliveryRun.Status.COMPLETED,
            completed_at__gte=start,
            completed_at__lt=end,
            stops__delivery__in=filtered_deliveries,
        )
        .values("resource_id")
        .annotate(total=Count("pk", distinct=True))
    )
    run_map = {row["resource_id"]: row["total"] for row in runs}
    cod = {row["actor_id"]: row for row in rider_cod_positions(filters, start, end)}
    rows = []
    for resource in resources.order_by("user__first_name", "user__last_name", "pk"):
        person = cod.get(resource.user_id, {})
        rows.append(
            [
                resource.user.get_full_name() or resource.user.username,
                resource.vehicle_name,
                assigned_map.get(resource.pk, 0),
                completed_map.get(resource.pk, 0),
                exception_map.get(resource.user_id, 0),
                run_map.get(resource.pk, 0),
                person.get("cod_collected", Decimal("0.00")),
                person.get("cod_reconciled", Decimal("0.00")),
                person.get("cod_outstanding", Decimal("0.00")),
            ]
        )
    return rows


def _report_dataset(kind, filters, start, end):
    if kind == "revenue":
        query = completed_revenue(filters, start, end)
        headers = [
            "Completion date",
            "Delivery ID",
            "Client",
            "Pickup area",
            "Destination area",
            "Vehicle",
            "Service",
            "Delivery status",
            "Rider / Driver",
            "Delivery Fee",
        ]
        return headers, query, "query"
    if kind == "cod":
        query = reconciliation_entries(
            organization=filters.get("organization"),
            resource=filters.get("resource"),
            start=start,
            end=end,
            filters=filters,
        )
        headers = [
            "Collection date",
            "Delivery ID",
            "Client",
            "Recipient",
            "Expected COD",
            "Collected COD",
            "Difference",
            "Reconciled COD",
            "Outstanding",
            "Rider / Driver",
        ]
        return headers, query, "cod"
    if kind == "runs":
        query = run_report(filters, start, end)
        headers = [
            "Run ID",
            "Date",
            "Rider / Driver",
            "Vehicle",
            "Stops",
            "Completed",
            "Deferred",
            "Status",
        ]
        return headers, query, "runs"
    if kind == "status":
        query = apply_delivery_filters(
            Delivery.objects.select_related("job__organization", "destination"),
            filters,
            date_field="created_at",
            start=start,
            end=end,
        ).order_by("status", "-created_at")
        headers = [
            "Created",
            "Delivery ID",
            "Client",
            "Destination",
            "Vehicle",
            "Service",
            "Status",
        ]
        return headers, query, "query"
    if kind == "clients":
        return (
            [
                "Client",
                "Created deliveries",
                "Completed deliveries",
                "Delivery fees",
                "COD expected",
                "COD collected",
                "COD settled",
            ],
            _client_report_rows(filters, start, end),
            "rows",
        )
    if kind == "riders":
        return (
            [
                "Rider / Driver",
                "Vehicle",
                "Assigned",
                "Completed",
                "Exceptions",
                "Runs completed",
                "COD collected",
                "COD reconciled",
                "COD outstanding",
            ],
            _rider_report_rows(filters, start, end),
            "rows",
        )
    raise PermissionDenied


def _row_values(kind, item):
    if kind == "revenue":
        assignment = item.finance_assignments[0] if item.finance_assignments else None
        return [
            item.completed_at,
            item.reference,
            item.job.organization.name,
            item.pickup.zone.name if item.pickup.zone_id else item.pickup.area,
            item.destination.zone.name if item.destination.zone_id else item.destination.area,
            item.get_vehicle_type_display(),
            item.get_service_display(),
            item.get_status_display(),
            (assignment.resource.user.get_full_name() or assignment.resource.user.username)
            if assignment
            else "Unassigned",
            item.delivery_fee,
        ]
    if kind == "cod":
        return [
            item.collected_at or item.created_at,
            item.reference,
            item.job.organization.name,
            item.recipient_name,
            item.cod_amount,
            item.collected_amount,
            item.difference,
            item.reconciled_amount,
            item.collected_amount - item.reconciled_amount,
            item.collection_actor_name or "Uncollected",
        ]
    if kind == "runs":
        return [
            item.reference,
            item.operational_date,
            item.resource.user.get_full_name() or item.resource.user.username,
            item.resource.vehicle_name,
            item.total_stops,
            item.completed_stops,
            item.deferred_stops,
            item.get_status_display(),
        ]
    if kind == "status":
        return [
            item.created_at,
            item.reference,
            item.job.organization.name,
            item.destination.zone.name if item.destination.zone_id else item.destination.area,
            item.get_vehicle_type_display(),
            item.get_service_display(),
            item.get_status_display(),
        ]
    return item


def _safe_csv_cell(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    if isinstance(value, datetime):
        return timezone.localtime(value).isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


class _Echo:
    def write(self, value):
        return value


def _csv_response(kind, headers, dataset, dataset_type):
    def rows():
        writer = csv.writer(_Echo())
        yield writer.writerow(headers)
        if dataset_type == "rows":
            iterator = iter(dataset)
            for item in iterator:
                yield writer.writerow([_safe_csv_cell(value) for value in item])
        else:
            for item in dataset.iterator(chunk_size=1000):
                yield writer.writerow([_safe_csv_cell(value) for value in _row_values(kind, item)])

    response = StreamingHttpResponse(rows(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="design-pot-{kind}.csv"'
    response["Cache-Control"] = "private, no-store"
    return response


@roles_required("admin")
@require_GET
def admin_report(request, kind, export=False):
    if kind not in REPORTS:
        raise Http404
    form = _report_form(request)
    _, start, end, start_day, end_day = _period_form(request)
    filters = _filters(form)
    headers, dataset, dataset_type = _report_dataset(kind, filters, start, end)
    if export:
        return _csv_response(kind, headers, dataset, dataset_type)
    page_obj = Paginator(dataset, 25).get_page(request.GET.get("page"))
    table_rows: Any
    if dataset_type == "query":
        table_rows = [_row_values(kind, item) for item in page_obj]
    elif dataset_type == "cod":
        table_rows = [_row_values(kind, item) for item in page_obj]
    elif dataset_type == "runs":
        table_rows = [_row_values(kind, item) for item in page_obj]
    else:
        table_rows = page_obj
    return render(
        request,
        "operations/report.html",
        {
            "title": REPORTS[kind][0],
            "section": "reports",
            "report_kind": kind,
            "description": REPORTS[kind][1],
            "headers": headers,
            "rows": table_rows,
            "page_obj": page_obj,
            "form": form,
            "export_url": f"/admin/reports/{kind}/export.csv",
            **_period_context(form, start_day, end_day),
        },
    )


@roles_required("admin")
@require_GET
def admin_report_export(request, kind):
    return admin_report(request, kind, export=True)
