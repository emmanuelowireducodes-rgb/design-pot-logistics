from decimal import Decimal
from typing import Any, cast

from django import forms
from django.utils import timezone

from .models import (
    Delivery,
    OperationalZone,
    Organization,
    PremiumPricingRule,
    PricingConfiguration,
    Resource,
)


class ZoneForm(forms.Form):
    name = forms.CharField(max_length=100)
    active = forms.BooleanField(required=False, initial=True)
    boundary = forms.JSONField(
        required=False,
        initial=list,
        help_text="Optional closed ring of [longitude, latitude] points. Empty means Admin assigns this zone manually.",
    )


class CardForm(forms.Form):
    name = forms.CharField(max_length=120)
    organization = forms.ModelChoiceField(
        queryset=Organization.objects.order_by("name"),
        required=False,
        empty_label="General price card",
    )
    active = forms.BooleanField(required=False, initial=True)


class OrganizationPricingForm(forms.Form):
    use_custom_pricing = forms.ChoiceField(
        label="Pricing source",
        choices=[
            ("general", "Use General Dpot Pricing — default Dpot delivery rates"),
            ("custom", "Use Custom Client Pricing — client rates, then General fallback"),
        ],
        widget=forms.RadioSelect,
    )
    premium_rule_source = forms.ChoiceField(
        label="Premium rule",
        choices=[
            ("general", "Use General Premium Rule"),
            ("custom", "Use Custom Premium Rule"),
        ],
    )
    premium_enabled = forms.BooleanField(label="Premium pricing enabled", required=False)
    premium_method = forms.ChoiceField(
        label="Adjustment method",
        choices=PremiumPricingRule.Method.choices,
    )
    premium_value = forms.DecimalField(
        label="Percentage or GHS amount",
        min_value=0,
        max_digits=8,
        decimal_places=2,
        help_text="Percentage is limited to 500%. Fixed amounts are in GHS.",
    )

    def __init__(self, *args, organization, **kwargs):
        self.organization = organization
        super().__init__(*args, **kwargs)
        rule = PremiumPricingRule.objects.filter(organization=organization).first()
        self.initial.setdefault(
            "use_custom_pricing",
            "custom" if organization.use_custom_pricing else "general",
        )
        self.initial.setdefault("premium_rule_source", organization.premium_rule_source)
        self.initial.setdefault("premium_enabled", rule.enabled if rule else False)
        self.initial.setdefault(
            "premium_method",
            rule.method if rule else PremiumPricingRule.Method.PERCENTAGE,
        )
        self.initial.setdefault("premium_value", rule.value if rule else Decimal("0.00"))

    def clean(self):
        cleaned = super().clean()
        value = cleaned.get("premium_value")
        if cleaned.get("premium_enabled") and value is not None and value <= 0:
            self.add_error("premium_value", "Enabled Premium pricing requires a positive uplift.")
        if (
            cleaned.get("premium_method") == PremiumPricingRule.Method.PERCENTAGE
            and cleaned.get("premium_value", Decimal("0")) > Decimal("500")
        ):
            self.add_error("premium_value", "Percentage uplift cannot exceed 500%.")
        return cleaned

    def save(self):
        self.organization.use_custom_pricing = (
            self.cleaned_data["use_custom_pricing"] == "custom"
        )
        self.organization.premium_rule_source = self.cleaned_data["premium_rule_source"]
        self.organization.save(
            update_fields=["use_custom_pricing", "premium_rule_source"]
        )
        if self.organization.premium_rule_source == "custom":
            PremiumPricingRule.objects.update_or_create(
                scope_key=f"client:{self.organization.pk}",
                defaults={
                    "organization": self.organization,
                    "enabled": self.cleaned_data["premium_enabled"],
                    "method": self.cleaned_data["premium_method"],
                    "value": self.cleaned_data["premium_value"],
                },
            )
        return self.organization


class PremiumRuleForm(forms.Form):
    enabled = forms.BooleanField(label="Premium pricing enabled", required=False)
    method = forms.ChoiceField(
        label="Adjustment method",
        choices=PremiumPricingRule.Method.choices,
        widget=forms.RadioSelect,
    )
    value = forms.DecimalField(
        label="Percentage or GHS amount",
        min_value=0,
        max_digits=8,
        decimal_places=2,
        help_text="Percentage uplift is limited to 500%; fixed amounts are in GHS.",
    )

    def __init__(self, *args, rule=None, **kwargs):
        self.rule = rule
        initial = kwargs.setdefault("initial", {})
        if rule and not args and "data" not in kwargs:
            initial.update(
                enabled=rule.enabled,
                method=rule.method,
                value=rule.value,
            )
        else:
            initial.setdefault("enabled", False)
            initial.setdefault("method", PremiumPricingRule.Method.PERCENTAGE)
            initial.setdefault("value", Decimal("0.00"))
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        value = cleaned.get("value")
        if cleaned.get("enabled") and value is not None and value <= 0:
            self.add_error("value", "Enabled Premium pricing requires a positive uplift.")
        if (
            cleaned.get("method") == PremiumPricingRule.Method.PERCENTAGE
            and cleaned.get("value", Decimal("0")) > Decimal("500")
        ):
            self.add_error("value", "Percentage uplift cannot exceed 500%.")
        return cleaned

    def save(self):
        return PremiumPricingRule.objects.update_or_create(
            scope_key="general",
            defaults={
                "organization": None,
                "enabled": self.cleaned_data["enabled"],
                "method": self.cleaned_data["method"],
                "value": self.cleaned_data["value"],
            },
        )[0]


class RateForm(forms.Form):
    expected_version = forms.IntegerField(required=False, widget=forms.HiddenInput)
    origin = forms.ModelChoiceField(queryset=OperationalZone.objects.order_by("name"))
    destination = forms.ModelChoiceField(queryset=OperationalZone.objects.order_by("name"))
    vehicle = forms.ChoiceField(choices=[("bike", "Bike"), ("car", "Car")])
    service = forms.ChoiceField(choices=[("standard", "Standard"), ("premium", "Premium")])
    amount = forms.DecimalField(
        label="All-inclusive delivery fee (GHS)", min_value=0, max_digits=12, decimal_places=2
    )
    bidirectional = forms.BooleanField(
        required=False, initial=True, label="Apply this rate in both directions"
    )
    active = forms.BooleanField(required=False, initial=True)
    effective_at = forms.DateTimeField(
        initial=timezone.now,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
    )
    expires_at = forms.DateTimeField(
        required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
    )
    reason = forms.CharField(max_length=500, widget=forms.Textarea(attrs={"rows": 2}))


class RoutePriceForm(forms.Form):
    """Simple standard-service route card; advanced controls retain the full model."""

    origin = forms.ModelChoiceField(
        label="From", queryset=OperationalZone.objects.filter(active=True).order_by("name")
    )
    destination = forms.ModelChoiceField(
        label="To", queryset=OperationalZone.objects.filter(active=True).order_by("name")
    )
    bike_price = forms.DecimalField(
        label="Bike price (GHS)", required=False, min_value=0, max_digits=12, decimal_places=2
    )
    car_price = forms.DecimalField(
        label="Car price (GHS)", required=False, min_value=0, max_digits=12, decimal_places=2
    )
    same_return = forms.BooleanField(
        label="Same price for return direction", required=False, initial=True
    )
    service = forms.ChoiceField(
        choices=[("standard", "Standard"), ("premium", "Premium")],
        initial="standard",
        required=False,
    )
    active = forms.BooleanField(required=False, initial=True)
    effective_at = forms.DateTimeField(
        required=False,
        initial=timezone.now,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
    )
    expires_at = forms.DateTimeField(
        required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
    )
    reason = forms.CharField(max_length=500, required=False)
    selected_zones = forms.ModelMultipleChoiceField(
        label="Import Existing Zones",
        queryset=OperationalZone.objects.filter(active=True).order_by("name"),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Select existing active areas to make them easy to choose below. This reuses directory entries and creates no areas.",
    )

    def clean(self):
        cleaned = super().clean() or {}
        if cleaned.get("bike_price") is None and cleaned.get("car_price") is None:
            raise forms.ValidationError("Enter a Bike price, a Car price, or both.")
        return cleaned


class ModeForm(forms.Form):
    mode = forms.ChoiceField(choices=PricingConfiguration._meta.get_field("mode").choices or [])
    fallback = forms.ChoiceField(
        choices=PricingConfiguration._meta.get_field("fallback").choices or []
    )


class ManualQuoteForm(forms.Form):
    request_key = forms.UUIDField(widget=forms.HiddenInput)
    expected_revision = forms.IntegerField(widget=forms.HiddenInput)
    amount = forms.DecimalField(
        label="Delivery fee (GHS)", min_value=0, max_digits=12, decimal_places=2
    )
    reason = forms.CharField(max_length=500, widget=forms.Textarea(attrs={"rows": 3}))
    save_as_price_card = forms.BooleanField(
        required=False, label="Add this route to Price Cards for future deliveries"
    )
    card_scope = forms.ChoiceField(
        required=False,
        choices=[("general", "General card"), ("client", "This client's card")],
        initial="general",
        label="Save route for",
    )


class ZoneAssignmentForm(forms.Form):
    side = forms.ChoiceField(choices=[("pickup", "Pickup"), ("destination", "Destination")])
    zone = forms.ModelChoiceField(
        queryset=OperationalZone.objects.filter(active=True).order_by("name")
    )
    reason = forms.CharField(max_length=500)


class DeliveryRunForm(forms.Form):
    request_key = forms.UUIDField(widget=forms.HiddenInput)
    resource = forms.ModelChoiceField(label="Rider or Driver", queryset=Resource.objects.none())
    operational_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    deliveries = forms.ModelMultipleChoiceField(queryset=Delivery.objects.none(), widget=forms.CheckboxSelectMultiple,
        help_text="Only confirmed deliveries may be added. Premium delivery windows are shown on each delivery.")
    notes = forms.CharField(required=False, max_length=500, widget=forms.Textarea(attrs={"rows": 2}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        resource_field = cast(Any, self.fields["resource"])
        delivery_field = cast(Any, self.fields["deliveries"])
        resource_field.queryset = Resource.objects.select_related("user").filter(
            user__is_active=True
        ).order_by("user__first_name", "user__last_name")
        delivery_field.queryset = Delivery.objects.filter(
            status__in=["confirmed", "assigned"]
        ).select_related("job__organization", "pickup__zone", "destination__zone").order_by(
            "window_start", "created_at"
        )
        resource_field.label_from_instance = lambda resource: (
            f"{resource.user.get_full_name() or resource.user.username} · {resource.vehicle_name} · {resource.get_vehicle_type_display()}"
        )
        delivery_field.label_from_instance = lambda delivery: (
            f"{delivery.reference} · {delivery.job.organization.name} · "
            f"{delivery.pickup.zone.name if delivery.pickup.zone_id else delivery.pickup.area} → "
            f"{delivery.destination.zone.name if delivery.destination.zone_id else delivery.destination.area} · "
            f"{delivery.get_vehicle_type_display()} · {delivery.get_service_display()}"
            + (f" · {delivery.window_start:%H:%M}–{delivery.window_end:%H:%M}" if delivery.window_start else "")
        )


class RunReleaseForm(forms.Form):
    expected_version = forms.IntegerField(widget=forms.HiddenInput)
    request_key = forms.UUIDField(widget=forms.HiddenInput)
    override = forms.BooleanField(required=False, label="Use a schedule override")
    reason = forms.CharField(required=False, max_length=500, label="Override reason")


class RunVersionForm(forms.Form):
    expected_version = forms.IntegerField(widget=forms.HiddenInput)
    request_key = forms.UUIDField(widget=forms.HiddenInput)


class RunSkipForm(forms.Form):
    expected_version = forms.IntegerField(widget=forms.HiddenInput)
    request_key = forms.UUIDField(widget=forms.HiddenInput)
    note = forms.CharField(required=False, max_length=500, label="Note for Dispatch", widget=forms.Textarea(attrs={"rows": 2}))


class RunAddStopForm(forms.Form):
    expected_version = forms.IntegerField(widget=forms.HiddenInput)
    request_key = forms.UUIDField(widget=forms.HiddenInput)
    delivery = forms.ModelChoiceField(
        queryset=Delivery.objects.filter(status__in=["confirmed", "assigned"])
        .select_related("job__organization", "pickup__zone", "destination__zone")
        .order_by("created_at"),
        label="Delivery",
    )


class RunRemoveStopForm(forms.Form):
    expected_version = forms.IntegerField(widget=forms.HiddenInput)
    request_key = forms.UUIDField(widget=forms.HiddenInput)
    reason = forms.CharField(max_length=500, label="Reason for removal")




class RunCancelForm(forms.Form):
    expected_version = forms.IntegerField(widget=forms.HiddenInput)
    request_key = forms.UUIDField(widget=forms.HiddenInput)
    reason = forms.CharField(max_length=500, label="Reason for cancelling this run")
