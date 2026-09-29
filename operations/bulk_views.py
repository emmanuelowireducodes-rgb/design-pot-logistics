"""Mobile-first grouped entry while retaining one Job/Delivery per order."""

import uuid
from decimal import Decimal

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.validators import RegexValidator
from django.db import transaction
from django.forms import formset_factory
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from . import locations, workflow
from .access import roles_required
from .booking_forms import BookingForm
from .models import BulkBooking, OperationalZone, Quote, QuoteConfirmation


class BulkPickupForm(forms.Form):
    request_key = forms.UUIDField(widget=forms.HiddenInput)
    pickup_zone = forms.ModelChoiceField(
        queryset=OperationalZone.objects.filter(active=True).order_by("name"),
        required=False,
        label="Pickup pricing area",
        empty_label="Choose pickup area",
    )
    pickup_address = forms.CharField(label="Pickup address", max_length=255)
    pickup_landmark = forms.CharField(required=False, max_length=160, label="Pickup landmark")
    pickup_instructions = forms.CharField(required=False, max_length=500, label="Pickup instructions")
    pickup_google_maps_url = forms.CharField(
        required=False, max_length=2048, label="Google Maps location link",
        widget=forms.URLInput(attrs={"inputmode": "url"}),
    )

    def clean_pickup_google_maps_url(self):
        from .validators import validate_google_maps_link

        value = self.cleaned_data["pickup_google_maps_url"].strip()
        validate_google_maps_link(value)
        return value


class BulkRowForm(forms.Form):
    destination_zone = forms.ModelChoiceField(
        queryset=OperationalZone.objects.filter(active=True).order_by("name"),
        required=False,
        label="Destination pricing area",
        empty_label="Choose destination area",
    )
    destination_address = forms.CharField(label="Destination address", max_length=255, required=False)
    destination_landmark = forms.CharField(required=False, max_length=160, label="Landmark")
    destination_instructions = forms.CharField(required=False, max_length=500, label="Stop instructions")
    destination_google_maps_url = forms.CharField(
        required=False, max_length=2048, label="Maps link",
        widget=forms.URLInput(attrs={"inputmode": "url"}),
    )
    recipient_name = forms.CharField(label="Recipient name", max_length=120, required=False)
    recipient_phone = forms.CharField(
        label="Recipient phone", max_length=24, required=False,
        validators=[RegexValidator(r"^\+?[0-9 ()-]{7,24}$", "Enter a valid phone number.")],
        widget=forms.TextInput(attrs={"type": "tel", "autocomplete": "tel"}),
    )
    package_description = forms.CharField(label="Package", max_length=255, required=False)
    instructions = forms.CharField(label="Delivery instructions", max_length=500, required=False)
    vehicle_type = forms.ChoiceField(
        label="Vehicle", choices=[("bike", "Bike"), ("car", "Car")], required=False, initial="bike"
    )
    service = forms.ChoiceField(
        label="Service", choices=[("standard", "Standard"), ("premium", "Premium Scheduled")], required=False, initial="standard"
    )
    window_start = forms.DateTimeField(required=False, widget=forms.DateTimeInput(
        attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
    ))
    window_end = forms.DateTimeField(required=False, widget=forms.DateTimeInput(
        attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
    ))
    payment_method = forms.ChoiceField(
        label="Product payment", choices=[("paid", "Already paid"), ("cod", "Collect COD")], required=False, initial="paid"
    )
    cod_amount = forms.DecimalField(label="COD amount (GHS)", max_digits=12, decimal_places=2, min_value=0, required=False)

    def clean_destination_google_maps_url(self):
        from .validators import validate_google_maps_link

        value = self.cleaned_data["destination_google_maps_url"].strip()
        validate_google_maps_link(value)
        return value


BulkRows = formset_factory(
    BulkRowForm, extra=10, min_num=1, max_num=10, validate_min=True, validate_max=True
)


def row_data(common, row):
    data = {
        "booking_key": str(uuid.uuid4()),
        "revision": "0",
        "pickup_zone": str(common["pickup_zone"].pk) if common.get("pickup_zone") else "",
        "pickup_address": common["pickup_address"],
        "pickup_area": common["pickup_zone"].name if common.get("pickup_zone") else "Unclassified",
        "pickup_landmark": common["pickup_landmark"],
        "pickup_instructions": common["pickup_instructions"],
        "pickup_google_maps_url": common["pickup_google_maps_url"],
        "destination_zone": str(row.get("destination_zone").pk) if row.get("destination_zone") else "",
        "destination_address": row.get("destination_address", ""),
        "destination_area": row.get("destination_zone").name if row.get("destination_zone") else "Unclassified",
    }
    data.update({key: value for key, value in row.items() if key != "destination_zone"})
    return data


@roles_required("client")
@require_http_methods(["GET", "POST"])
def bulk_booking(request):
    if not request.user.organization_id:
        raise PermissionDenied
    common = BulkPickupForm(request.POST or None, initial={"request_key": uuid.uuid4()})
    rows = BulkRows(request.POST or None, prefix="rows")
    if request.method == "POST" and common.is_valid() and rows.is_valid():
        editable_fields = set(BulkRowForm.base_fields) - {"vehicle_type", "service", "payment_method"}
        populated = [
            form.cleaned_data
            for form in rows.forms
            if any(str(form.data.get(f"{form.prefix}-{field}", "")).strip() for field in editable_fields)
        ]
        prepared = []
        valid = True
        for index, row in enumerate(populated):
            candidate = BookingForm(row_data(common.cleaned_data, row), allow_zone_classification=True)
            if not candidate.is_valid():
                valid = False
                for field, errors in candidate.errors.items():
                    messages_for_field = "; ".join(str(error) for error in errors)
                    common.add_error(None, f"Delivery {index + 1}: {messages_for_field}")
            else:
                prepared.append(candidate.cleaned_data)
        if valid and prepared:
            with transaction.atomic():
                from .models import User

                User.objects.select_for_update().get(pk=request.user.pk)
                existing = BulkBooking.objects.filter(request_key=common.cleaned_data["request_key"]).first()
                if existing:
                    if existing.created_by_id != request.user.pk:
                        raise PermissionDenied
                    return redirect("bulk-booking-detail", bulk_id=existing.pk)
                pickup_values = prepared[0]
                pickup = locations.booking_location(request.user, pickup_values, "pickup")
                batch = BulkBooking.objects.create(
                    organization_id=request.user.organization_id,
                    created_by=request.user,
                    pickup=pickup,
                    request_key=common.cleaned_data["request_key"],
                )
                for values in prepared:
                    workflow.save_booking(
                        actor=request.user,
                        data=values,
                        bulk_booking=batch,
                        shared_pickup=pickup,
                    )
            messages.success(request, f"{len(prepared)} deliveries prepared. Review available prices below.")
            return redirect("bulk-booking-detail", bulk_id=batch.pk)
    return render(request, "operations/bulk_booking.html", {
        "title": "Bulk delivery entry", "section": "booking", "common": common, "rows": rows,
    })


@roles_required("client")
@require_http_methods(["GET", "POST"])
def bulk_booking_detail(request, bulk_id):
    batch = get_object_or_404(
        BulkBooking.objects.select_related("pickup"), pk=bulk_id,
        organization_id=request.user.organization_id,
    )
    jobs = list(batch.jobs.select_related("current_quote").prefetch_related("deliveries").order_by("created_at", "pk"))
    deliveries = [job.deliveries.first() for job in jobs]
    confirmed_ids = set(QuoteConfirmation.objects.filter(job__bulk_booking=batch).values_list("job_id", flat=True))
    priced = [d for d in deliveries if d and d.job.current_quote_id and not d.job.quote_requested]
    quote_ids = [str(d.job.current_quote_id) for d in priced if d.job_id not in confirmed_ids]
    if request.method == "POST":
        submitted = request.POST.getlist("quote_ids")
        if not submitted:
            messages.error(request, "Select at least one priced delivery to confirm.")
            return redirect("bulk-booking-detail", bulk_id=batch.pk)
        try:
            identifiers = {uuid.UUID(value) for value in submitted}
        except (ValueError, TypeError, AttributeError):
            raise PermissionDenied
        valid_quotes = list(Quote.objects.filter(
            pk__in=identifiers, job__bulk_booking=batch, job__organization_id=request.user.organization_id,
            job__quote_requested=False,
        ).order_by("job_id", "pk"))
        if {quote.pk for quote in valid_quotes} != identifiers:
            raise PermissionDenied
        with transaction.atomic():
            for quote in valid_quotes:
                workflow.confirm_quote(actor=request.user, quote_id=quote.pk)
        messages.success(request, f"{len(valid_quotes)} deliveries confirmed for dispatch.")
        return redirect("bulk-booking-detail", bulk_id=batch.pk)
    subtotal = Decimal("0.00")
    for delivery in priced:
        current_quote = delivery.job.current_quote
        if current_quote:
            subtotal += current_quote.amount
    context_rows = [{"delivery": d, "confirmed": bool(d and d.job_id in confirmed_ids)} for d in deliveries]
    return render(request, "operations/bulk_booking_detail.html", {
        "title": "Bulk delivery request", "section": "booking", "batch": batch,
        "rows": context_rows, "priced_count": len(priced),
        "quote_required_count": sum(bool(d and d.job.quote_requested) for d in deliveries),
        "subtotal": subtotal, "quote_ids": quote_ids,
    })
