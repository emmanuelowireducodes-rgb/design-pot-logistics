from django import forms
from django.core.validators import RegexValidator
from django.utils import timezone

from .models import OperationalZone, Resource


class BookingForm(forms.Form):
    pickup_zone = forms.ModelChoiceField(
        queryset=OperationalZone.objects.filter(active=True).order_by("name"),
        required=False,
        widget=forms.HiddenInput,
    )
    destination_zone = forms.ModelChoiceField(
        queryset=OperationalZone.objects.filter(active=True).order_by("name"),
        required=False,
        widget=forms.HiddenInput,
    )
    pickup_google_maps_url = forms.CharField(
        required=False,
        max_length=2048,
        label="Google Maps location link",
        widget=forms.URLInput(attrs={"inputmode": "url", "autocomplete": "url"}),
    )
    destination_google_maps_url = forms.CharField(
        required=False,
        max_length=2048,
        label="Google Maps location link",
        widget=forms.URLInput(attrs={"inputmode": "url", "autocomplete": "url"}),
    )
    pickup_landmark = forms.CharField(required=False, max_length=160, label="Pickup landmark")
    destination_landmark = forms.CharField(
        required=False, max_length=160, label="Destination landmark"
    )
    booking_key = forms.UUIDField(widget=forms.HiddenInput)
    revision = forms.IntegerField(min_value=0, initial=0, widget=forms.HiddenInput)
    pickup_address = forms.CharField(label="Pickup address", max_length=255)
    pickup_area = forms.CharField(required=False, max_length=100, widget=forms.HiddenInput)
    pickup_instructions = forms.CharField(
        label="Pickup instructions", max_length=500, required=False
    )
    destination_address = forms.CharField(label="Destination address", max_length=255)
    destination_area = forms.CharField(required=False, max_length=100, widget=forms.HiddenInput)
    destination_instructions = forms.CharField(
        label="Destination instructions", max_length=500, required=False
    )

    def clean_pickup_google_maps_url(self):
        from .validators import validate_google_maps_link

        value = self.cleaned_data["pickup_google_maps_url"].strip()
        validate_google_maps_link(value)
        return value

    def clean_destination_google_maps_url(self):
        from .validators import validate_google_maps_link

        value = self.cleaned_data["destination_google_maps_url"].strip()
        validate_google_maps_link(value)
        return value
    recipient_name = forms.CharField(label="Recipient name", max_length=120)
    recipient_phone = forms.CharField(
        label="Recipient phone",
        max_length=24,
        validators=[RegexValidator(r"^\+?[0-9 ()-]{7,24}$", "Enter a valid phone number.")],
        widget=forms.TextInput(attrs={"type": "tel", "autocomplete": "tel"}),
    )
    package_description = forms.CharField(label="Package description", max_length=255)
    instructions = forms.CharField(
        label="Delivery instructions",
        required=False,
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    vehicle_type = forms.ChoiceField(
        label="Vehicle required", choices=[("bike", "Bike"), ("car", "Car")]
    )
    service = forms.ChoiceField(
        label="Service", choices=[("standard", "Standard"), ("premium", "Premium Scheduled")]
    )
    window_start = forms.DateTimeField(
        label="Window starts (Accra / GMT)",
        required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
    )
    window_end = forms.DateTimeField(
        label="Window ends (Accra / GMT)",
        required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
    )
    payment_method = forms.ChoiceField(
        label="Product payment",
        choices=[("paid", "Already paid"), ("cod", "Collect product payment (COD)")],
    )
    cod_amount = forms.DecimalField(
        label="Product / COD amount (GHS)",
        max_digits=12,
        decimal_places=2,
        min_value=0,
        required=False,
    )

    def __init__(self, *args, allow_zone_classification=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.allow_zone_classification = allow_zone_classification

    def clean(self):
        data = super().clean() or {}
        if not self.allow_zone_classification:
            # Public clients cannot choose pricing zones, including by editing
            # a hidden form value. The internal service may receive an explicit
            # authoritative classification from a trusted caller.
            data["pickup_area"] = "Unclassified"
            data["destination_area"] = "Unclassified"
            data["pickup_zone"] = None
            data["destination_zone"] = None
        else:
            data["pickup_area"] = data.get("pickup_area") or "Unclassified"
            data["destination_area"] = data.get("destination_area") or "Unclassified"
        if data.get("service") == "premium":
            start, end = data.get("window_start"), data.get("window_end")
            if not start or not end or end <= start:
                self.add_error("window_end", "Choose a start and end for the delivery window.")
            elif start <= timezone.now():
                self.add_error("window_start", "Choose a future delivery window.")
        else:
            data["window_start"] = data["window_end"] = None
        if data.get("payment_method") == "cod":
            if not data.get("cod_amount") or data["cod_amount"] <= 0:
                self.add_error("cod_amount", "Enter the product amount to collect.")
        else:
            data["cod_amount"] = 0
        return data


class ResourceChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return f"{obj.user.get_full_name() or obj.user.username} · {obj.vehicle_name} · {obj.get_status_display()}"


class AssignmentForm(forms.Form):
    request_key = forms.UUIDField(widget=forms.HiddenInput)
    expected_assignment = forms.IntegerField(required=False, widget=forms.HiddenInput)
    resource = ResourceChoiceField(
        queryset=Resource.objects.none(), label="Rider / driver", empty_label="Choose a person"
    )
    starts_at = forms.DateTimeField(
        label="Reserve from (GMT)",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
    )
    ends_at = forms.DateTimeField(
        label="Reserve until (GMT)",
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
    )
    override = forms.BooleanField(
        label="Override a schedule conflict or off-duty status", required=False
    )
    reason = forms.CharField(
        label="Override reason",
        required=False,
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 2}),
    )

    def clean(self):
        data = super().clean() or {}
        if data.get("override") and not data.get("reason", "").strip():
            self.add_error("reason", "An explicit override reason is required.")
        if data.get("starts_at") and data.get("ends_at") and data["ends_at"] <= data["starts_at"]:
            self.add_error("ends_at", "The end must be after the start.")
        return data


class ActionForm(forms.Form):
    request_key = forms.UUIDField(widget=forms.HiddenInput)
    expected_status = forms.CharField(widget=forms.HiddenInput)
    target = forms.CharField(widget=forms.HiddenInput)
    note = forms.CharField(
        label="Operational note",
        required=False,
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 3}),
    )


class CompletionForm(forms.Form):
    request_key = forms.UUIDField(widget=forms.HiddenInput)
    recipient_name = forms.CharField(label="Received by", max_length=120, required=False)
    photo = forms.FileField(
        label="Delivery photo",
        required=False,
        widget=forms.ClearableFileInput(
            attrs={"accept": "image/jpeg,image/png", "capture": "environment"}
        ),
    )
    note = forms.CharField(
        label="Delivery note",
        required=False,
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 3}),
    )


class CollectionForm(forms.Form):
    request_key = forms.UUIDField(widget=forms.HiddenInput)
    amount = forms.DecimalField(
        label="Product amount collected (GHS)", max_digits=12, decimal_places=2, min_value=0.01
    )
    method = forms.ChoiceField(
        label="Collection method", choices=[("cash", "Cash"), ("momo", "Mobile money")]
    )
    reference = forms.CharField(label="Payment reference", max_length=120, required=False)
    note = forms.CharField(
        label="Note", max_length=500, required=False, widget=forms.Textarea(attrs={"rows": 2})
    )

    def clean(self):
        data = super().clean() or {}
        if data.get("method") == "momo" and not data.get("reference"):
            self.add_error("reference", "Enter the mobile money reference.")
        return data


class ReconcileForm(forms.Form):
    request_key = forms.UUIDField(widget=forms.HiddenInput)
    reference = forms.CharField(label="Reconciliation reference", max_length=120)
    note = forms.CharField(
        label="Reconciliation note",
        max_length=500,
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
    )


class AvailabilityForm(forms.Form):
    request_key = forms.UUIDField(widget=forms.HiddenInput)
    resource = ResourceChoiceField(queryset=Resource.objects.none(), label="Person")
    starts_at = forms.DateTimeField(
        label="From (GMT)", widget=forms.DateTimeInput(attrs={"type": "datetime-local"})
    )
    ends_at = forms.DateTimeField(
        label="Until (GMT)", widget=forms.DateTimeInput(attrs={"type": "datetime-local"})
    )
    kind = forms.ChoiceField(
        choices=[
            ("off_duty", "Off duty"),
            ("break", "Break"),
            ("maintenance", "Maintenance"),
            ("personal", "Unavailable"),
        ]
    )
    note = forms.CharField(required=False, max_length=500, label="Reason / note")


class PricingForm(forms.Form):
    is_sample = forms.BooleanField(
        label="Development / sample rates (not approved business pricing)",
        required=False,
        initial=True,
    )
    bike_base = forms.DecimalField(
        label="Bike base (GHS)", min_value=0, max_value=10000, decimal_places=2
    )
    bike_minimum = forms.DecimalField(
        label="Bike minimum (GHS)", min_value=0, max_value=10000, decimal_places=2
    )
    bike_per_km = forms.DecimalField(
        label="Bike per km (inactive without road distance)",
        min_value=0,
        max_value=10000,
        decimal_places=2,
    )
    car_base = forms.DecimalField(
        label="Car base (GHS)", min_value=0, max_value=10000, decimal_places=2
    )
    car_minimum = forms.DecimalField(
        label="Car minimum (GHS)", min_value=0, max_value=10000, decimal_places=2
    )
    car_per_km = forms.DecimalField(
        label="Car per km (inactive without road distance)",
        min_value=0,
        max_value=10000,
        decimal_places=2,
    )
    premium_surcharge = forms.DecimalField(
        label="Premium surcharge (GHS)", min_value=0, max_value=10000, decimal_places=2
    )
    rounding = forms.DecimalField(
        label="Round up to nearest (GHS)", min_value=0.01, max_value=10000, decimal_places=2
    )
    quote_minutes = forms.IntegerField(
        label="Quote validity (minutes)", min_value=1, max_value=1440
    )
