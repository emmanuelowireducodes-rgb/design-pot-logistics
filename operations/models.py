import uuid

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from .validators import validate_google_maps_link


class Organization(models.Model):
    name = models.CharField(max_length=160)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class User(AbstractUser):
    class Role(models.TextChoices):
        ADMIN = "admin", "Admin / Dispatch"
        CLIENT = "client", "Client"
        RIDER = "rider", "Rider"
        DRIVER = "driver", "Driver"

    role = models.CharField(max_length=12, choices=Role.choices, default=Role.CLIENT)
    organization = models.ForeignKey(Organization, null=True, blank=True, on_delete=models.PROTECT)
    phone = models.CharField(max_length=24, blank=True)
    operational_access_approved = models.BooleanField(default=True)


class Record(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(default=timezone.now, editable=False)

    class Meta:
        abstract = True


class ImmutableHistory(models.Model):
    """Services append history; migration adds database-level update/delete protection."""

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Historical records cannot be changed.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Historical records cannot be deleted.")


class AppendOnly(ImmutableHistory, Record):
    class Meta:
        abstract = True


class Location(AppendOnly):
    display_address = models.CharField(max_length=255)
    google_maps_url = models.CharField(
        max_length=2048, blank=True, validators=[validate_google_maps_link]
    )
    provider = models.CharField(max_length=40, blank=True)
    place_id = models.CharField(max_length=255, blank=True)
    latitude = models.DecimalField(
        max_digits=10,
        decimal_places=7,
        null=True,
        blank=True,
        validators=[MinValueValidator(-90), MaxValueValidator(90)],
    )
    longitude = models.DecimalField(
        max_digits=10,
        decimal_places=7,
        null=True,
        blank=True,
        validators=[MinValueValidator(-180), MaxValueValidator(180)],
    )
    area = models.CharField(max_length=100, db_index=True)
    instructions = models.TextField(blank=True)
    landmark = models.CharField(max_length=160, blank=True)
    zone = models.ForeignKey("OperationalZone", null=True, blank=True, on_delete=models.PROTECT)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    source = models.CharField(max_length=24, default="manual")

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(latitude__isnull=True, longitude__isnull=True)
                | Q(
                    latitude__isnull=False,
                    longitude__isnull=False,
                    latitude__gte=-90,
                    latitude__lte=90,
                    longitude__gte=-180,
                    longitude__lte=180,
                ),
                name="valid_coordinates",
            )
        ]


class Resource(Record):
    user = models.OneToOneField(User, on_delete=models.PROTECT, related_name="resource")
    vehicle_type = models.CharField(max_length=4, choices=[("bike", "Bike"), ("car", "Car")])
    vehicle_name = models.CharField(max_length=80)
    registration = models.CharField(max_length=30, unique=True)
    status = models.CharField(
        max_length=12,
        choices=[("available", "Available"), ("booked", "Booked"), ("off_duty", "Off duty")],
        default="off_duty",
        db_index=True,
    )

    def clean(self):
        expected = User.Role.RIDER if self.vehicle_type == "bike" else User.Role.DRIVER
        if self.user.role != expected:
            raise ValidationError("Bikes belong to riders; cars belong to drivers.")


class PricingVersion(AppendOnly):
    label = models.CharField(max_length=100, unique=True)
    currency = models.CharField(max_length=3, default="GHS")
    rules = models.JSONField(default=dict)
    is_sample = models.BooleanField(default=False)


class PricingConfiguration(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    current_version = models.ForeignKey(PricingVersion, on_delete=models.PROTECT)
    mode = models.CharField(
        max_length=24,
        default="DISTANCE_BASED",
        choices=[("DISTANCE_BASED", "Distance based"), ("FIXED_PRICE_CARD", "Fixed price card")],
    )
    fallback = models.CharField(
        max_length=32,
        default="REQUIRE_ADMIN_QUOTE",
        choices=[
            ("FALLBACK_TO_DISTANCE_PRICING", "Use distance pricing"),
            ("REQUIRE_ADMIN_QUOTE", "Require Admin quote"),
        ],
    )


class Job(Record):
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT)
    is_bulk = models.BooleanField(default=False)
    booking_key = models.UUIDField(null=True, blank=True, unique=True)
    revision = models.PositiveIntegerField(default=0)
    quote_requested = models.BooleanField(default=False, db_index=True)
    current_quote = models.ForeignKey(
        "Quote", null=True, blank=True, on_delete=models.PROTECT, related_name="current_for_jobs"
    )

    class Meta:
        indexes = [models.Index(fields=["organization", "-created_at"])]


class Delivery(Record):
    class Status(models.TextChoices):
        REQUESTED = "requested", "Requested"
        CONFIRMED = "confirmed", "Confirmed"
        ASSIGNED = "assigned", "Assigned"
        TO_PICKUP = "to_pickup", "En route to pickup"
        PICKED_UP = "picked_up", "Picked up"
        IN_TRANSIT = "in_transit", "In transit"
        DELIVERED = "delivered", "Delivered"
        UNREACHABLE = "unreachable", "Customer unreachable"
        RESCHEDULED = "rescheduled", "Rescheduled"
        RETURNED = "returned", "Returned"
        CANCELLED = "cancelled", "Cancelled"
        FAILED = "failed", "Failed delivery"

    reference = models.CharField(max_length=24, unique=True, null=True, blank=True)
    job = models.ForeignKey(Job, on_delete=models.PROTECT, related_name="deliveries")
    pickup = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="pickups")
    destination = models.ForeignKey(Location, on_delete=models.PROTECT, related_name="destinations")
    recipient_name = models.CharField(max_length=120)
    recipient_phone = models.CharField(max_length=24)
    package_description = models.CharField(max_length=255)
    instructions = models.TextField(blank=True)
    service = models.CharField(
        max_length=8, choices=[("standard", "Standard"), ("premium", "Premium")], default="standard"
    )
    vehicle_type = models.CharField(max_length=4, choices=[("bike", "Bike"), ("car", "Car")])
    window_start = models.DateTimeField(null=True, blank=True)
    window_end = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.REQUESTED)
    payment_method = models.CharField(
        max_length=12,
        choices=[
            ("paid", "Already paid"),
            ("cash", "Cash"),
            ("momo", "Mobile money"),
            ("cod", "COD"),
        ],
        default="paid",
    )
    cod_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    currency = models.CharField(max_length=3, default="GHS")
    pod_policy = models.JSONField(default=dict)

    class Meta:
        indexes = [
            models.Index(fields=["-created_at"]),
            models.Index(fields=["window_start", "status"]),
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["job", "-created_at"]),
            models.Index(fields=["service", "window_start"]),
        ]
        constraints = [
            models.CheckConstraint(condition=Q(cod_amount__gte=0), name="nonnegative_cod"),
            models.CheckConstraint(
                condition=Q(service="standard", window_start__isnull=True, window_end__isnull=True)
                | Q(
                    service="premium",
                    window_start__isnull=False,
                    window_end__isnull=False,
                    window_end__gt=models.F("window_start"),
                ),
                name="service_window_valid",
            ),
        ]


class Quote(AppendOnly):
    job = models.ForeignKey(Job, on_delete=models.PROTECT, related_name="quotes")
    pricing_version = models.ForeignKey(PricingVersion, on_delete=models.PROTECT)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="GHS")
    breakdown = models.JSONField(default=dict)
    expires_at = models.DateTimeField()
    source = models.CharField(max_length=32, default="GENERAL_DISTANCE_RATE")

    @property
    def source_label(self):
        return {
            "CLIENT_CONTRACT_RATE": "Client contract rate",
            "GENERAL_FIXED_RATE": "General fixed rate",
            "GENERAL_DISTANCE_RATE": "General distance rate",
            "ADMIN_MANUAL_QUOTE": "Admin manual quote",
        }.get(self.source, self.source)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(amount__gte=0), name="nonnegative_quote")]


class QuoteConfirmation(AppendOnly):
    job = models.OneToOneField(Job, on_delete=models.PROTECT)
    quote = models.OneToOneField(Quote, on_delete=models.PROTECT)
    actor = models.ForeignKey(User, on_delete=models.PROTECT)


class Assignment(ImmutableHistory):
    id = models.BigAutoField(primary_key=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    delivery = models.ForeignKey(Delivery, on_delete=models.PROTECT, related_name="assignments")
    resource = models.ForeignKey(Resource, on_delete=models.PROTECT)
    actor = models.ForeignKey(User, on_delete=models.PROTECT, related_name="assignments_made")
    resource_snapshot = models.JSONField(default=dict)
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    override_reason = models.CharField(max_length=500, blank=True)
    # Supersession is append-only. The newest assignment is authoritative.

    class Meta:
        indexes = [
            models.Index(fields=["delivery", "-id"]),
            models.Index(fields=["resource", "starts_at"]),
        ]


class DeliveryRun(Record):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        READY = "ready", "Released"
        IN_PROGRESS = "in_progress", "In progress"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"

    reference = models.CharField(max_length=32, unique=True)
    resource = models.ForeignKey(Resource, on_delete=models.PROTECT, related_name="delivery_runs")
    operational_date = models.DateField(db_index=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="runs_created")
    notes = models.CharField(max_length=500, blank=True)
    override_reason = models.CharField(max_length=500, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    version = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["resource", "status", "operational_date"])]

    def __str__(self):
        return self.reference


class RunStop(Record):
    class Status(models.TextChoices):
        PENDING = "pending", "Remaining"
        STARTED = "started", "In progress"
        COMPLETED = "completed", "Completed"
        SKIPPED = "skipped", "Skipped for now"
        UNAVAILABLE = "unavailable", "Customer unavailable"
        REMOVED = "removed", "Removed"

    run = models.ForeignKey(DeliveryRun, on_delete=models.PROTECT, related_name="stops")
    delivery = models.ForeignKey(Delivery, on_delete=models.PROTECT, related_name="run_stops")
    stop_type = models.CharField(max_length=16, default="delivery", choices=[("delivery", "Delivery")])
    sequence = models.PositiveIntegerField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    deferred_at = models.DateTimeField(null=True, blank=True)
    deferred_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.PROTECT, related_name="run_stops_deferred"
    )
    note = models.CharField(max_length=500, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["run", "delivery"], name="unique_run_delivery_stop")]
        indexes = [models.Index(fields=["run", "status", "sequence"])]


class Availability(Record):
    resource = models.ForeignKey(Resource, on_delete=models.PROTECT)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    kind = models.CharField(
        max_length=16,
        choices=[
            ("booking", "Booking"),
            ("off_duty", "Off duty"),
            ("break", "Break"),
            ("maintenance", "Maintenance"),
            ("personal", "Personal"),
        ],
    )
    delivery = models.ForeignKey(Delivery, null=True, blank=True, on_delete=models.PROTECT)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    release_reason = models.CharField(max_length=16, blank=True)
    note = models.CharField(max_length=500, blank=True)

    class Meta:
        indexes = [models.Index(fields=["resource", "starts_at", "ends_at"])]
        constraints = [
            models.CheckConstraint(
                condition=Q(ends_at__gt=models.F("starts_at")),
                name="availability_positive_duration",
            )
        ]


class StatusEvent(AppendOnly):
    delivery = models.ForeignKey(Delivery, on_delete=models.PROTECT, related_name="history")
    actor = models.ForeignKey(User, on_delete=models.PROTECT)
    status = models.CharField(max_length=16, choices=Delivery.Status.choices)
    previous_status = models.CharField(max_length=16, blank=True)
    sequence = models.PositiveIntegerField(default=0)
    note = models.CharField(max_length=500, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["delivery", "created_at"]),
            models.Index(fields=["delivery", "-sequence"], name="status_delivery_sequence_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["delivery", "sequence"],
                condition=Q(sequence__gt=0),
                name="unique_positive_status_sequence",
            )
        ]


class ProofOfDelivery(AppendOnly):
    delivery = models.OneToOneField(Delivery, on_delete=models.PROTECT, related_name="proof")
    actor = models.ForeignKey(User, on_delete=models.PROTECT)
    recipient_name = models.CharField(max_length=120)
    note = models.CharField(max_length=500, blank=True)
    policy_snapshot = models.JSONField(default=dict)


class ClientSettlement(AppendOnly):
    """Admin-recorded evidence that reconciled COD was remitted to one client."""

    organization = models.ForeignKey(
        Organization, on_delete=models.PROTECT, related_name="cod_settlements"
    )
    recorded_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="settlements_recorded")
    reference = models.CharField(max_length=32, unique=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="GHS")
    method = models.CharField(max_length=40, blank=True)
    external_reference = models.CharField(max_length=120, blank=True)
    note = models.CharField(max_length=500, blank=True)

    class Meta:
        indexes = [models.Index(fields=["organization", "-created_at"])]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="positive_client_settlement")
        ]


class LedgerEntry(AppendOnly):
    delivery = models.ForeignKey(Delivery, on_delete=models.PROTECT, related_name="ledger")
    actor = models.ForeignKey(User, on_delete=models.PROTECT)
    kind = models.CharField(
        max_length=16,
        choices=[
            ("cod_collected", "COD collected"),
            ("cod_reconciled", "COD reconciled"),
            ("cod_settled", "COD settled"),
            ("delivery_fee", "Delivery fee"),
        ],
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="GHS")
    idempotency_key = models.UUIDField(default=uuid.uuid4, unique=True)
    method = models.CharField(
        max_length=12, choices=[("cash", "Cash"), ("momo", "Mobile money")], blank=True
    )
    reference = models.CharField(max_length=120, blank=True)
    note = models.CharField(max_length=500, blank=True)
    settlement = models.ForeignKey(
        ClientSettlement, null=True, blank=True, on_delete=models.PROTECT, related_name="entries"
    )
    reconciled_entry = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="settlement_entry"
    )

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="positive_ledger_amount"),
            models.CheckConstraint(
                condition=Q(
                    kind="cod_settled", settlement__isnull=False, reconciled_entry__isnull=False
                )
                | (~Q(kind="cod_settled") & Q(settlement__isnull=True, reconciled_entry__isnull=True)),
                name="settled_entry_links_reconciliation",
            ),
        ]
        indexes = [
            models.Index(fields=["kind", "created_at"]),
            models.Index(fields=["delivery", "kind", "created_at"]),
        ]


class DomainEvent(AppendOnly):
    kind = models.CharField(max_length=80)
    actor = models.ForeignKey(User, on_delete=models.PROTECT)
    delivery = models.ForeignKey(Delivery, null=True, on_delete=models.PROTECT)
    payload = models.JSONField(default=dict)
    request_key = models.UUIDField(null=True, blank=True, unique=True)

    class Meta:
        indexes = [models.Index(fields=["delivery", "-created_at"])]


class DailySequence(models.Model):
    day = models.DateField(primary_key=True)
    value = models.PositiveIntegerField(default=0)


class Notification(Record):
    event = models.ForeignKey(DomainEvent, on_delete=models.PROTECT)
    recipient = models.ForeignKey(User, on_delete=models.PROTECT)
    title = models.CharField(max_length=160)
    message = models.CharField(max_length=500)
    priority = models.CharField(max_length=12, default="normal")
    channel = models.CharField(max_length=12, default="in_app")
    delivery_status = models.CharField(max_length=12, default="sent")
    sent_at = models.DateTimeField(default=timezone.now)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["recipient", "read_at", "-created_at"])]
        constraints = [
            models.UniqueConstraint(
                fields=["event", "recipient", "channel"], name="notification_once_per_channel"
            )
        ]


class PushSubscription(Record):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="push_subscriptions")
    endpoint = models.URLField(max_length=2048, unique=True)
    p256dh = models.CharField(max_length=200)
    auth = models.CharField(max_length=100)
    updated_at = models.DateTimeField(auto_now=True)


class PushOutbox(Record):
    notification = models.OneToOneField(
        Notification, on_delete=models.CASCADE, related_name="push_outbox"
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    sent_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=80, blank=True)

    class Meta:
        indexes = [models.Index(fields=["sent_at", "attempts", "created_at"])]


class LocationUpdate(Record):
    resource = models.ForeignKey(Resource, on_delete=models.PROTECT)
    latitude = models.DecimalField(max_digits=10, decimal_places=7)
    longitude = models.DecimalField(max_digits=10, decimal_places=7)
    accuracy_metres = models.PositiveIntegerField()
    recorded_at = models.DateTimeField()

    class Meta:
        indexes = [models.Index(fields=["resource", "-recorded_at"])]
        constraints = [
            models.CheckConstraint(
                condition=Q(
                    latitude__gte=-90, latitude__lte=90, longitude__gte=-180, longitude__lte=180
                ),
                name="valid_live_coordinates",
            )
        ]


class RoutePlan(Record):
    resource = models.ForeignKey(Resource, on_delete=models.PROTECT)
    origin = models.ForeignKey(Location, on_delete=models.PROTECT)
    provider = models.CharField(max_length=40)
    cache_key = models.CharField(max_length=64, db_index=True)
    expires_at = models.DateTimeField()


class RouteStop(Record):
    route = models.ForeignKey(RoutePlan, on_delete=models.PROTECT)
    delivery = models.ForeignKey(Delivery, on_delete=models.PROTECT)
    position = models.PositiveIntegerField()
    road_metres = models.PositiveIntegerField()
    travel_seconds = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["route", "position"], name="unique_route_position"),
            models.UniqueConstraint(fields=["route", "delivery"], name="unique_route_delivery"),
        ]


class LoginAttempt(models.Model):
    key = models.CharField(max_length=64, primary_key=True)
    count = models.PositiveIntegerField(default=0)
    started_at = models.DateTimeField(default=timezone.now)


class OperationalZone(Record):
    name = models.CharField(max_length=100, unique=True)
    active = models.BooleanField(default=True)
    # Optional GeoJSON-style ring of [longitude, latitude]; empty means Admin assignment only.
    boundary = models.JSONField(default=list, blank=True)

    def __str__(self):
        return self.name


class PriceCard(Record):
    name = models.CharField(max_length=120)
    organization = models.OneToOneField(
        Organization, null=True, blank=True, on_delete=models.PROTECT
    )
    scope_key = models.CharField(max_length=80, unique=True)
    active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class FixedRate(AppendOnly):
    card = models.ForeignKey(PriceCard, on_delete=models.PROTECT, related_name="rates")
    rate_key = models.UUIDField(default=uuid.uuid4, db_index=True)
    version = models.PositiveIntegerField(default=1)
    origin = models.ForeignKey(
        OperationalZone, on_delete=models.PROTECT, related_name="origin_rates"
    )
    destination = models.ForeignKey(
        OperationalZone, on_delete=models.PROTECT, related_name="destination_rates"
    )
    vehicle = models.CharField(max_length=4, choices=[("bike", "Bike"), ("car", "Car")])
    service = models.CharField(
        max_length=8, choices=[("standard", "Standard"), ("premium", "Premium")]
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    bidirectional = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    effective_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(null=True, blank=True)
    actor = models.ForeignKey(User, on_delete=models.PROTECT)
    reason = models.CharField(max_length=500)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["rate_key", "version"], name="unique_rate_version"),
            models.CheckConstraint(condition=Q(amount__gte=0), name="fixed_rate_nonnegative"),
            models.CheckConstraint(
                condition=Q(expires_at__isnull=True) | Q(expires_at__gt=models.F("effective_at")),
                name="fixed_rate_valid_window",
            ),
        ]
        indexes = [models.Index(fields=["card", "origin", "destination", "vehicle", "service"])]
