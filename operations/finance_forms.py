# mypy: disable-error-code="attr-defined"
import uuid

from django import forms

from .models import LedgerEntry, Organization, Resource


class DateRangeForm(forms.Form):
    period = forms.ChoiceField(
        choices=[
            ("today", "Today"),
            ("yesterday", "Yesterday"),
            ("this_week", "This week"),
            ("this_month", "This month"),
            ("custom", "Custom range"),
        ],
        required=False,
    )
    start_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    end_date = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))

    def clean(self):
        data = super().clean() or {}
        period = data.get("period") or "this_month"
        if period == "custom":
            start, end = data.get("start_date"), data.get("end_date")
            if not start or not end:
                raise forms.ValidationError("Choose both a start and end date.")
            if end < start:
                raise forms.ValidationError("The end date must be on or after the start date.")
            if (end - start).days > 366:
                raise forms.ValidationError("Choose a date range of one year or less.")
        return data


class ReportFilterForm(DateRangeForm):
    q = forms.CharField(required=False, max_length=100, label="Search")
    organization = forms.ModelChoiceField(
        queryset=Organization.objects.none(), required=False, empty_label="All Clients"
    )
    resource = forms.ModelChoiceField(
        queryset=Resource.objects.none(), required=False, empty_label="All Riders / Drivers"
    )
    vehicle = forms.ChoiceField(
        choices=[("", "All vehicles"), ("bike", "Bike"), ("car", "Car")], required=False
    )
    service = forms.ChoiceField(
        choices=[("", "All services"), ("standard", "Standard"), ("premium", "Premium")],
        required=False,
    )
    status = forms.ChoiceField(choices=[], required=False)
    area = forms.CharField(required=False, max_length=100)

    def __init__(self, *args, status_choices=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["status"].choices = [("", "All statuses"), *status_choices]


class SettlementForm(forms.Form):
    request_key = forms.UUIDField(widget=forms.HiddenInput, initial=uuid.uuid4)
    organization = forms.ModelChoiceField(queryset=Organization.objects.none(), label="Client")
    reconciliation_ids = forms.ModelMultipleChoiceField(
        queryset=LedgerEntry.objects.none(),
        required=True,
        widget=forms.MultipleHiddenInput,
        error_messages={
            "required": "Select at least one reconciled COD item.",
            "invalid_choice": (
                "One or more selected COD items are no longer eligible for this Client. "
                "Review the list and try again."
            ),
        },
    )
    method = forms.CharField(
        required=False,
        max_length=40,
        label="Recorded method or channel (optional)",
        help_text="Record the method reported by Dispatch. No payment is initiated here.",
    )
    external_reference = forms.CharField(required=False, max_length=120, label="External reference")
    note = forms.CharField(required=False, max_length=500, label="Note")


class ReconciliationFilterForm(DateRangeForm):
    resource = forms.ModelChoiceField(
        queryset=Resource.objects.none(), required=False, empty_label="All Riders / Drivers"
    )
    organization = forms.ModelChoiceField(
        queryset=Organization.objects.none(), required=False, empty_label="All Clients"
    )
    cod_status = forms.ChoiceField(
        required=False,
        choices=[
            ("", "All COD statuses"),
            ("expected", "Expected"),
            ("collected", "Collected / outstanding"),
            ("reconciled", "Reconciled"),
            ("settled", "Settled to Client"),
            ("mismatch", "Mismatch"),
        ],
    )
