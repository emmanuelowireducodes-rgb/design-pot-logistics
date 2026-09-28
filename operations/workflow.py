"""Transactional Phase 2 operations. HTTP adapters never write operational state directly."""

import uuid
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max, Q, Sum
from django.utils import timezone

from .booking_forms import BookingForm, CollectionForm
from .models import (
    Assignment,
    Availability,
    ClientSettlement,
    DailySequence,
    Delivery,
    DeliveryRun,
    DomainEvent,
    Job,
    LedgerEntry,
    Organization,
    PricingConfiguration,
    ProofOfDelivery,
    Quote,
    QuoteConfirmation,
    Resource,
    RunStop,
    StatusEvent,
    User,
)
from .pricing import prepare_quote, version_for
from .services import emit_event

ACTIVE = ("assigned", "to_pickup", "picked_up", "in_transit")
EXCEPTIONS = ("unreachable", "failed")
NEXT_STATUS = {"assigned": "to_pickup", "to_pickup": "picked_up", "picked_up": "in_transit"}
ACTION_LABELS = {
    "to_pickup": "Start pickup",
    "picked_up": "Picked up",
    "in_transit": "Start delivery",
}


def require_role(actor, *roles):
    if not actor.is_active or actor.role not in roles:
        raise PermissionDenied
    if actor.role in (User.Role.RIDER, User.Role.DRIVER) and not actor.operational_access_approved:
        raise PermissionDenied


def latest_assignment(delivery):
    return (
        Assignment.objects.filter(delivery=delivery)
        .select_related("resource__user")
        .order_by("-id")
        .first()
    )


def require_assignee(actor, delivery):
    require_role(actor, "rider", "driver")
    assignment = latest_assignment(delivery)
    in_run = RunStop.objects.filter(
        delivery=delivery, run__resource__user=actor,
        run__status__in=[DeliveryRun.Status.READY, DeliveryRun.Status.IN_PROGRESS],
    ).exclude(status=RunStop.Status.REMOVED).exists()
    if in_run:
        return assignment
    if not assignment or assignment.resource.user_id != actor.pk:
        raise PermissionDenied
    return assignment


def request_uuid(key):
    try:
        return uuid.UUID(str(key))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError("This action has expired. Reload the page and try again.") from exc


def replay(actor, delivery, key, kind):
    event = DomainEvent.objects.filter(request_key=request_uuid(key)).first()
    if event and (
        event.actor_id != actor.pk or event.delivery_id != delivery.pk or event.kind != kind
    ):
        raise PermissionDenied
    return event


def audiences(delivery, *, clients=True, admins=True, field=False):
    query = Q(pk__in=[])
    if clients:
        query |= Q(role="client", organization_id=delivery.job.organization_id)
    if admins:
        query |= Q(role="admin")
    if field:
        assignment = latest_assignment(delivery)
        if assignment:
            query |= Q(pk=assignment.resource.user_id)
        run_users = RunStop.objects.filter(
            delivery=delivery, run__status__in=[DeliveryRun.Status.READY, DeliveryRun.Status.IN_PROGRESS]
        ).exclude(status=RunStop.Status.REMOVED).values_list("run__resource__user_id", flat=True)
        query |= Q(pk__in=run_users)
    return list(User.objects.filter(query, is_active=True))


def record_status(delivery, actor, target, note=""):
    previous = delivery.status
    sequence = (delivery.history.aggregate(value=Max("sequence"))["value"] or 0) + 1
    StatusEvent.objects.create(
        delivery=delivery,
        actor=actor,
        previous_status=previous,
        status=target,
        note=note,
        sequence=sequence,
    )
    delivery.status = target
    delivery.save(update_fields=["status"])


@transaction.atomic
def save_booking(*, actor, data, delivery_id=None):
    require_role(actor, "client")
    if not actor.organization_id:
        raise PermissionDenied
    form = BookingForm(data, allow_zone_classification=True)
    if not form.is_valid():
        raise ValidationError("Please correct the delivery details.")
    values = form.cleaned_data
    # Serializes repeated create requests from one account, even before a job row exists.
    User.objects.select_for_update().get(pk=actor.pk)
    if delivery_id:
        reference = Delivery.objects.only("job_id").get(pk=delivery_id)
        job = Job.objects.select_for_update().get(pk=reference.job_id)
        if job.organization_id != actor.organization_id:
            raise PermissionDenied
        delivery = Delivery.objects.select_for_update().get(pk=delivery_id)
        if delivery.status != "requested" or QuoteConfirmation.objects.filter(job=job).exists():
            raise ValidationError("A confirmed delivery cannot be edited.")
        if values["revision"] != job.revision:
            raise ValidationError("This delivery has changed. Reload it before editing.")
        job.revision += 1
        job.save(update_fields=["revision"])
    else:
        existing = Job.objects.filter(booking_key=values["booking_key"]).first()
        if existing:
            if existing.created_by_id != actor.pk:
                raise PermissionDenied
            return existing.deliveries.get()
        job = Job.objects.create(
            organization_id=actor.organization_id,
            created_by=actor,
            booking_key=values["booking_key"],
            revision=1,
        )
        delivery = Delivery(job=job)
    delivery.job = job
    from .locations import booking_location

    for prefix in ("pickup", "destination"):
        location = booking_location(actor, values, prefix)
        setattr(delivery, prefix, location)
    for name in (
        "recipient_name",
        "recipient_phone",
        "package_description",
        "instructions",
        "service",
        "vehicle_type",
        "window_start",
        "window_end",
        "payment_method",
        "cod_amount",
    ):
        setattr(delivery, name, values[name])
    delivery.pod_policy = dict(settings.POD_POLICIES[delivery.service])
    delivery.full_clean()
    delivery.save()
    if not delivery_id:
        StatusEvent.objects.create(
            delivery=delivery,
            actor=actor,
            status="requested",
            previous_status="",
            sequence=1,
            note="Delivery drafted.",
        )
    quote = prepare_quote(delivery)
    emit_event(
        actor=actor,
        kind="delivery.edited" if delivery_id else "delivery.created",
        delivery=delivery,
        title="Delivery prepared",
        message="Review your delivery quote before confirming.",
        recipients=[],
        payload={"quote_id": str(quote.pk) if quote else None, "revision": job.revision},
    )
    return delivery


def allocate_reference():
    day = timezone.localdate()
    sequence, _ = DailySequence.objects.get_or_create(day=day)
    sequence = DailySequence.objects.select_for_update().get(pk=day)
    prefix = f"DPT-{day:%y%m%d}-"
    # Preserve existing Phase 1/sample IDs. Generated suffixes are capped at four digits.
    last = (
        Delivery.objects.filter(reference__startswith=prefix)
        .order_by("-reference")
        .values_list("reference", flat=True)
        .first()
    )
    last_number = int(last.rsplit("-", 1)[1]) if last and last.rsplit("-", 1)[1].isdigit() else 0
    sequence.value = max(sequence.value, last_number) + 1
    if sequence.value > 9999:
        raise ValidationError("The daily delivery ID capacity has been reached. Contact dispatch.")
    sequence.save(update_fields=["value"])
    return f"{prefix}{sequence.value:04d}"


@transaction.atomic
def confirm_quote(*, actor, quote_id):
    require_role(actor, "client")
    quote = Quote.objects.select_related("job").get(pk=quote_id)
    job = Job.objects.select_for_update().get(pk=quote.job_id)
    if not actor.organization_id or actor.organization_id != job.organization_id:
        raise PermissionDenied
    existing = QuoteConfirmation.objects.filter(job=job).first()
    if existing:
        if existing.quote_id != quote.pk:
            raise ValidationError("This order already has a confirmed quote.")
        return existing
    if job.quote_requested or (job.current_quote_id and job.current_quote_id != quote.pk):
        raise ValidationError("This quote has been replaced. Review the latest quote.")
    if quote.expires_at <= timezone.now():
        raise ValidationError("This quote has expired. Edit the delivery to prepare a new quote.")
    deliveries = list(Delivery.objects.select_for_update().filter(job=job))
    if len(deliveries) != 1 or deliveries[0].status != "requested":
        raise ValidationError("This order is not ready for confirmation.")
    delivery = deliveries[0]
    if delivery.service == "premium" and (
        not delivery.window_start or delivery.window_start <= timezone.now()
    ):
        raise ValidationError("The requested window has passed. Edit the delivery first.")
    confirmation = QuoteConfirmation.objects.create(job=job, quote=quote, actor=actor)
    if not delivery.reference:
        delivery.reference = allocate_reference()
        delivery.save(update_fields=["reference"])
    record_status(delivery, actor, "confirmed", "Client confirmed the delivery and quote.")
    emit_event(
        actor=actor,
        kind="quote.confirmed",
        delivery=delivery,
        title=f"{delivery.reference} confirmed",
        message="Your delivery is confirmed and awaiting dispatch.",
        recipients=audiences(delivery),
        payload={
            "quote_id": str(quote.pk),
            "amount": str(quote.amount),
            "pricing_version": quote.pricing_version_id.hex,
            "job_id": str(job.pk),
        },
    )
    return confirmation


@transaction.atomic
def assign_delivery(
    *,
    actor,
    delivery_id,
    resource_id,
    starts_at,
    ends_at,
    request_key,
    expected_assignment=None,
    override=False,
    reason="",
):
    require_role(actor, "admin")
    delivery = Delivery.objects.select_for_update().select_related("job").get(pk=delivery_id)
    if replay(actor, delivery, request_key, "delivery.assigned"):
        return latest_assignment(delivery)
    if delivery.status not in ("confirmed", "assigned"):
        raise ValidationError(
            "Only confirmed or not-yet-started assigned deliveries can be dispatched."
        )
    if RunStop.objects.filter(
        delivery=delivery,
        run__status__in=[DeliveryRun.Status.DRAFT, DeliveryRun.Status.READY, DeliveryRun.Status.IN_PROGRESS],
    ).exclude(status=RunStop.Status.REMOVED).exists():
        raise ValidationError("Remove this delivery from its current run before changing its assignment.")
    if not QuoteConfirmation.objects.filter(job=delivery.job).exists():
        raise ValidationError("The client must confirm a quote before dispatch.")
    previous = latest_assignment(delivery)
    if (previous.pk if previous else None) != expected_assignment:
        raise ValidationError("The assignment has changed. Reload before assigning.")
    if ends_at <= starts_at or ends_at - starts_at > timedelta(days=7) or ends_at <= timezone.now():
        raise ValidationError("Choose a current or future reservation of up to seven days.")
    if delivery.service == "premium" and (
        not delivery.window_start
        or not delivery.window_end
        or starts_at > delivery.window_start
        or ends_at < delivery.window_end
    ):
        raise ValidationError("Reserve enough time to cover the full Premium delivery window.")
    # A consistent order prevents deadlocks when two dispatchers swap people.
    ids = {resource_id}
    if previous:
        ids.add(previous.resource_id)
    locked = list(Resource.objects.select_for_update().filter(pk__in=ids).order_by("pk"))
    resource = next((person for person in locked if person.pk == resource_id), None)
    if not resource or not resource.user.is_active:
        raise ValidationError("This resource is not available for assignment.")
    expected_role = "rider" if delivery.vehicle_type == "bike" else "driver"
    if resource.vehicle_type != delivery.vehicle_type or resource.user.role != expected_role:
        raise ValidationError("Choose a person with the required vehicle and role.")
    conflicts = Availability.objects.filter(
        resource=resource, cancelled_at__isnull=True, starts_at__lt=ends_at, ends_at__gt=starts_at
    ).exclude(delivery=delivery)
    conflict_ids = list(conflicts.values_list("pk", flat=True))
    if override and not reason.strip():
        raise ValidationError("An explicit reason is required for an override.")
    if (conflict_ids or resource.status == "off_duty") and not override:
        raise ValidationError(
            "This person is off duty or has overlapping work. Choose another time/person, or explicitly override with a reason."
        )
    Availability.objects.filter(
        delivery=delivery, kind="booking", cancelled_at__isnull=True
    ).update(cancelled_at=timezone.now(), release_reason="reassigned")
    assignment = Assignment.objects.create(
        delivery=delivery,
        resource=resource,
        actor=actor,
        starts_at=starts_at,
        ends_at=ends_at,
        override_reason=reason.strip() if override else "",
        resource_snapshot={
            "name": resource.user.get_full_name() or resource.user.username,
            "vehicle": resource.vehicle_name,
            "registration": resource.registration,
        },
    )
    Availability.objects.create(
        resource=resource,
        delivery=delivery,
        kind="booking",
        starts_at=starts_at,
        ends_at=ends_at,
        note=f"Assignment {assignment.pk}",
    )
    record_status(
        delivery,
        actor,
        "assigned",
        "Assignment changed." if previous else "Rider / driver assigned.",
    )
    recipients = audiences(delivery) + [resource.user]
    if previous and previous.resource_id != resource.pk:
        recipients.append(previous.resource.user)
    emit_event(
        actor=actor,
        kind="delivery.assigned",
        delivery=delivery,
        title=f"{delivery.reference} assignment updated",
        message="The delivery assignment has changed. Check your workspace for current work.",
        recipients=recipients,
        request_key=request_uuid(request_key),
        payload={
            "assignment_id": assignment.pk,
            "previous_assignment_id": previous.pk if previous else None,
            "override": override,
            "reason": reason.strip() if override else "",
            "conflicting_blocks": [str(pk) for pk in conflict_ids],
        },
    )
    return assignment


@transaction.atomic
def transition_delivery(*, actor, delivery_id, target, expected_status, request_key, note=""):
    delivery = Delivery.objects.select_for_update().select_related("job").get(pk=delivery_id)
    if actor.role == "admin" and target == "resume":
        require_role(actor, "admin")
    else:
        require_assignee(actor, delivery)
    run_stop = RunStop.objects.select_related("run").filter(
        delivery=delivery, run__status__in=[DeliveryRun.Status.READY, DeliveryRun.Status.IN_PROGRESS]
    ).exclude(status=RunStop.Status.REMOVED).order_by("run__created_at").first()
    if run_stop and run_stop.run.status == DeliveryRun.Status.READY and target != "resume":
        raise ValidationError("Start the delivery run before changing this stop.")
    if replay(actor, delivery, request_key, "delivery.status_changed"):
        return delivery
    if expected_status != delivery.status:
        raise ValidationError("This delivery has changed. Reload before continuing.")
    old = delivery.status
    if target == "resume" and old in EXCEPTIONS:
        event = delivery.history.filter(status=old).order_by("-sequence", "-created_at").first()
        if not event or event.previous_status not in ACTIVE:
            raise ValidationError("This exception needs an administrator's review.")
        target = event.previous_status
    elif target in EXCEPTIONS and old in ACTIVE:
        if not note.strip():
            raise ValidationError("Add a short note explaining what happened.")
    elif NEXT_STATUS.get(old) != target:
        raise ValidationError("This status transition is not allowed.")
    record_status(delivery, actor, target, note.strip())
    if target == "to_pickup":
        from .runs import mark_stop_started

        mark_stop_started(actor=actor, delivery=delivery)
    elif target == "unreachable":
        from .runs import record_unavailable

        record_unavailable(actor=actor, delivery=delivery, note=note)
    elif target == "resume":
        from .runs import restore_deferred_stop

        restore_deferred_stop(actor=actor, delivery=delivery)
    titles = {
        "picked_up": "Pickup completed",
        "unreachable": "Customer unavailable",
        "failed": "Delivery issue",
    }
    emit_event(
        actor=actor,
        kind="delivery.status_changed",
        delivery=delivery,
        title=f"{delivery.reference} · {titles.get(target, delivery.get_status_display())}",
        message=f"Delivery status: {delivery.get_status_display()}.",
        recipients=audiences(delivery, admins=target in EXCEPTIONS, field=actor.role == "admin"),
        request_key=request_uuid(request_key),
        payload={"previous_status": old, "new_status": target, "note": note.strip()},
    )
    return delivery


def cod_summary(delivery):
    totals = dict(
        delivery.ledger.filter(kind__in=["cod_collected", "cod_reconciled", "cod_settled"])
        .values_list("kind")
        .annotate(total=Sum("amount"))
    )
    collected = totals.get("cod_collected", Decimal(0))
    reconciled = totals.get("cod_reconciled", Decimal(0))
    settled = totals.get("cod_settled", Decimal(0))
    state = "Not applicable"
    if delivery.cod_amount:
        if not collected:
            state = "Expected"
        elif collected != delivery.cod_amount:
            state = "Mismatch"
        elif reconciled < collected:
            state = "Collected"
        elif settled < reconciled:
            state = "Reconciled"
        else:
            state = "Settled to Client"
    return {
        "expected": delivery.cod_amount,
        "collected": collected,
        "reconciled": reconciled,
        "pending": collected - reconciled,
        "pending_settlement": reconciled - settled,
        "settled": settled,
        "difference": delivery.cod_amount - collected,
        "state": state,
    }


@transaction.atomic
def collect_cod(*, actor, delivery_id, request_key, amount, method, reference="", note=""):
    delivery = Delivery.objects.select_for_update().select_related("job").get(pk=delivery_id)
    require_assignee(actor, delivery)
    if replay(actor, delivery, request_key, "cod.collected"):
        return delivery.ledger.get(kind="cod_collected")
    if delivery.status != "in_transit" or delivery.payment_method != "cod":
        raise ValidationError("COD can be collected only during delivery on a COD booking.")
    validation = CollectionForm(
        {
            "request_key": request_key,
            "amount": amount,
            "method": method,
            "reference": reference,
            "note": note,
        }
    )
    if not validation.is_valid():
        raise ValidationError("Enter a valid collection amount, method and payment reference.")
    amount = validation.cleaned_data["amount"]
    if amount != delivery.cod_amount:
        raise ValidationError(
            "Record the exact expected product amount. Report an issue if it differs."
        )
    if delivery.ledger.filter(kind="cod_collected").exists():
        raise ValidationError("This collection is already recorded.")
    entry = LedgerEntry.objects.create(
        delivery=delivery,
        actor=actor,
        kind="cod_collected",
        amount=amount,
        currency=delivery.currency,
        method=method,
        reference=reference,
        note=note,
        idempotency_key=request_uuid(request_key),
    )
    emit_event(
        actor=actor,
        kind="cod.collected",
        delivery=delivery,
        title=f"{delivery.reference} · COD collected",
        message="Product payment has been collected and is awaiting reconciliation.",
        recipients=audiences(delivery),
        request_key=request_uuid(request_key),
        payload={"transaction_id": str(entry.pk), "amount": str(amount), "method": method},
    )
    return entry


@transaction.atomic
def reconcile_cod(*, actor, delivery_id, request_key, reference, note=""):
    require_role(actor, "admin")
    delivery = Delivery.objects.select_for_update().select_related("job").get(pk=delivery_id)
    if replay(actor, delivery, request_key, "cod.reconciled"):
        return delivery.ledger.get(kind="cod_reconciled")
    if not reference.strip() or len(reference) > 120 or len(note) > 500:
        raise ValidationError("Enter a reconciliation reference of up to 120 characters.")
    summary = cod_summary(delivery)
    if summary["pending"] <= 0:
        raise ValidationError("There is no collection awaiting reconciliation.")
    collection = delivery.ledger.get(kind="cod_collected")
    if collection.amount != delivery.cod_amount:
        raise ValidationError(
            "Collected COD differs from the expected amount. Resolve the discrepancy before reconciliation."
        )
    entry = LedgerEntry.objects.create(
        delivery=delivery,
        actor=actor,
        kind="cod_reconciled",
        amount=summary["pending"],
        currency=delivery.currency,
        method=collection.method,
        reference=reference.strip(),
        note=note,
        idempotency_key=request_uuid(request_key),
    )
    emit_event(
        actor=actor,
        kind="cod.reconciled",
        delivery=delivery,
        title=f"{delivery.reference} · COD reconciled",
        message="The recorded product collection has been reconciled. Settlement is separate.",
        recipients=audiences(delivery, field=True),
        request_key=request_uuid(request_key),
        payload={
            "transaction_id": str(entry.pk),
            "collection_id": str(collection.pk),
            "amount": str(entry.amount),
        },
    )
    return entry


@transaction.atomic
def settle_reconciled_cod(
    *, actor, organization_id, reconciliation_ids, request_key, method="", external_reference="", note=""
):
    """Record an Admin-reported full-item COD settlement; no payment is initiated here."""
    require_role(actor, "admin")
    key = request_uuid(request_key)
    existing = DomainEvent.objects.filter(request_key=key).first()
    if existing:
        if existing.actor_id != actor.pk or existing.kind != "cod.client_settlement_recorded":
            raise PermissionDenied
        return ClientSettlement.objects.get(pk=existing.payload["settlement_id"])
    ids = list(dict.fromkeys(str(value) for value in reconciliation_ids))
    if not ids:
        raise ValidationError("Select at least one reconciled COD item.")
    if len(method.strip()) > 40 or len(external_reference.strip()) > 120 or len(note.strip()) > 500:
        raise ValidationError("Settlement details exceed the allowed length.")
    organization = Organization.objects.select_for_update().get(pk=organization_id)
    entries = list(
        LedgerEntry.objects.select_for_update()
        .filter(pk__in=ids, kind="cod_reconciled")
        .select_related("delivery__job")
        .order_by("pk")
    )
    if len(entries) != len(ids):
        raise ValidationError("One or more selected items are not eligible for settlement.")
    if any(entry.delivery.job.organization_id != organization.pk for entry in entries):
        raise ValidationError("All selected COD items must belong to the same Client.")
    if any(entry.currency != "GHS" for entry in entries):
        raise ValidationError("Only supported GHS reconciliations can be settled.")
    if any(hasattr(entry, "settlement_entry") for entry in entries):
        raise ValidationError("One or more selected COD items have already been settled.")
    total = sum((entry.amount for entry in entries), Decimal("0.00"))
    settlement = ClientSettlement.objects.create(
        organization=organization,
        recorded_by=actor,
        reference=f"DPT-SET-{timezone.localdate():%y%m%d}-{uuid.uuid4().hex[:6].upper()}",
        amount=total,
        currency="GHS",
        method=method.strip(),
        external_reference=external_reference.strip(),
        note=note.strip(),
    )
    settled_entries = [
        LedgerEntry(
            delivery=entry.delivery,
            actor=actor,
            kind="cod_settled",
            amount=entry.amount,
            currency=entry.currency,
            reference=external_reference.strip(),
            note=note.strip(),
            settlement=settlement,
            reconciled_entry=entry,
        )
        for entry in entries
    ]
    for settled in settled_entries:
        settled.save()
    emit_event(
        actor=actor,
        kind="cod.client_settlement_recorded",
        title=f"{settlement.reference} recorded",
        message="Client COD settlement was recorded. This app did not initiate a transfer.",
        recipients=[],
        request_key=key,
        payload={
            "settlement_id": str(settlement.pk),
            "organization_id": str(organization.pk),
            "entry_ids": [str(entry.pk) for entry in settled_entries],
            "amount": str(total),
        },
    )
    return settlement


def complete_delivery(*, actor, delivery_id, request_key, recipient_name="", note=""):
    candidate = Delivery.objects.get(pk=delivery_id)
    require_assignee(actor, candidate)
    if replay(actor, candidate, request_key, "delivery.completed"):
        return ProofOfDelivery.objects.get(delivery=candidate)
    with transaction.atomic():
        delivery = Delivery.objects.select_for_update().select_related("job").get(pk=delivery_id)
        require_assignee(actor, delivery)
        if replay(actor, delivery, request_key, "delivery.completed"):
            return ProofOfDelivery.objects.get(delivery=delivery)
        if delivery.status != "in_transit":
            raise ValidationError("Start the delivery before recording completion.")
        policy = delivery.pod_policy or settings.POD_POLICIES[delivery.service]
        if policy.get("recipient_name") and not recipient_name.strip():
            raise ValidationError("Enter the name of the person who received the package.")
        if len(recipient_name) > 120 or len(note) > 500:
            raise ValidationError("The recipient name or note is too long.")
        if delivery.cod_amount and cod_summary(delivery)["collected"] != delivery.cod_amount:
            raise ValidationError("Record the expected COD collection before completing delivery.")
        proof = ProofOfDelivery(
            delivery=delivery,
            actor=actor,
            recipient_name=recipient_name.strip(),
            note=note.strip(),
            policy_snapshot={"recipient_name": bool(policy.get("recipient_name"))},
        )
        proof.save()
        record_status(delivery, actor, "delivered", "Delivery completed.")
        Availability.objects.filter(
            delivery=delivery, kind="booking", cancelled_at__isnull=True
        ).update(cancelled_at=timezone.now(), release_reason="completed")
        emit_event(
            actor=actor,
            kind="delivery.completed",
            delivery=delivery,
            title=f"{delivery.reference} delivered",
            message="Delivery completed. Recipient and operational details are recorded.",
            recipients=audiences(delivery),
            request_key=request_uuid(request_key),
            payload={"proof_id": str(proof.pk)},
        )
        from .runs import complete_stop

        complete_stop(actor=actor, delivery=delivery)
        return proof


@transaction.atomic
def update_pricing(*, actor, rules, is_sample=True):
    require_role(actor, "admin")
    User.objects.select_for_update().get(pk=actor.pk)
    version = version_for(rules, is_sample=is_sample)
    PricingConfiguration.objects.update_or_create(pk=1, defaults={"current_version": version})
    emit_event(
        actor=actor,
        kind="pricing.development_updated" if is_sample else "pricing.distance_approved",
        title="Distance pricing updated",
        message="Future quotes will use the new distance pricing version.",
        recipients=[],
        payload={"pricing_version": str(version.pk), "is_sample": is_sample},
    )
    return version
