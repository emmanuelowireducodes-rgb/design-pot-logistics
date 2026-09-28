import os
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from operations.models import (
    Assignment,
    Delivery,
    Job,
    LedgerEntry,
    Location,
    Organization,
    ProofOfDelivery,
    Resource,
    StatusEvent,
    User,
)
from operations.services import emit_event


class Command(BaseCommand):
    help = "Seed isolated development data. Requires DPOT_DEMO=1 and DPOT_DEMO_PASSWORD."

    @transaction.atomic
    def handle(self, *args, **options):
        if (
            settings.PRODUCTION
            or not settings.DEMO_MODE
            or settings.DATABASES["default"]["ENGINE"] != "django.db.backends.sqlite3"
        ):
            raise CommandError("Sample data is allowed only in the isolated local demo database.")
        password = os.getenv("DPOT_DEMO_PASSWORD", "")
        if not password:
            raise CommandError("Set DPOT_DEMO_PASSWORD to a strong local-only password.")
        validate_password(password)
        if User.objects.exists():
            raise CommandError("Database already contains users. Seed only an empty demo database.")
        organizations = [
            Organization.objects.create(name=name)
            for name in ("The Good Store", "Bloom & Co.", "Everyday Essentials")
        ]
        admin = User.objects.create_user(
            "demo.admin", password=password, first_name="Akosua", last_name="Mensah", role="admin"
        )
        client = User.objects.create_user(
            "demo.client",
            password=password,
            first_name="Ama",
            last_name="Owusu",
            role="client",
            organization=organizations[0],
        )
        resources = []
        for username, first, last, role, vehicle, registration, status in [
            ("demo.rider", "Kwame", "Asante", "rider", "Bike 01", "DEMO-B01", "available"),
            ("demo.driver", "Daniel", "Osei", "driver", "Car 03", "DEMO-C03", "booked"),
            ("demo.rider2", "Kofi", "Adjei", "rider", "Bike 02", "DEMO-B02", "booked"),
            ("demo.driver2", "Abena", "Boateng", "driver", "Car 01", "DEMO-C01", "off_duty"),
        ]:
            user = User.objects.create_user(
                username, password=password, first_name=first, last_name=last, role=role
            )
            resources.append(
                Resource.objects.create(
                    user=user,
                    vehicle_type="bike" if role == "rider" else "car",
                    vehicle_name=vehicle,
                    registration=registration,
                    status=status,
                )
            )
        pickup = Location.objects.create(
            display_address="Sample pickup, Osu, Accra",
            area="Osu",
            latitude="5.5560000",
            longitude="-0.1820000",
            instructions="Development address only",
        )
        now = timezone.now()
        statuses = [
            "in_transit",
            "confirmed",
            "delivered",
            "assigned",
            "in_transit",
            "confirmed",
            "delivered",
            "requested",
            "delivered",
            "failed",
            "delivered",
            "assigned",
        ]
        areas = ["East Legon", "Cantonments", "Airport Residential", "Adenta", "Tema", "Labone"]
        names = [
            "Nana Owusu",
            "Esi Appiah",
            "Kojo Mensah",
            "Akua Addo",
            "Yaw Boateng",
            "Efua Asare",
        ]
        for index, status in enumerate(statuses):
            organization = organizations[index % 3]
            job = Job.objects.create(
                organization=organization, created_by=client if index % 3 == 0 else admin
            )
            destination = Location.objects.create(
                display_address=f"Sample destination, {areas[index % 6]}, Accra",
                area=areas[index % 6],
                latitude=Decimal("5.6000000") + Decimal(index) / 1000,
                longitude="-0.1600000",
            )
            premium = index in (1, 5)
            delivery = Delivery.objects.create(
                reference=f"DPT-{now:%y%m%d}-{index + 1:04d}",
                job=job,
                pickup=pickup,
                destination=destination,
                recipient_name=names[index % 6],
                recipient_phone="",
                package_description="Sample packaged retail goods",
                vehicle_type="bike" if index % 2 == 0 else "car",
                status=status,
                service="premium" if premium else "standard",
                window_start=now + timedelta(hours=index + 1) if premium else None,
                window_end=now + timedelta(hours=index + 3) if premium else None,
                payment_method="cod" if index % 2 == 0 else "paid",
                cod_amount=Decimal("180.00") if index % 2 == 0 else Decimal(0),
                created_at=now - timedelta(minutes=index * 3),
            )
            StatusEvent.objects.create(
                delivery=delivery,
                actor=admin,
                status="requested",
                note="Sample delivery created.",
                created_at=delivery.created_at,
            )
            if status != "requested":
                StatusEvent.objects.create(
                    delivery=delivery,
                    actor=admin,
                    status=status,
                    note="Illustrative status, not a live delivery.",
                    created_at=delivery.created_at + timedelta(seconds=1),
                )
            if status not in ("requested", "confirmed"):
                resource = (
                    resources[1]
                    if delivery.vehicle_type == "car"
                    else resources[0 if index % 4 == 0 else 2]
                )
                Assignment.objects.create(
                    delivery=delivery,
                    resource=resource,
                    actor=admin,
                    resource_snapshot={
                        "name": resource.user.get_full_name(),
                        "vehicle": resource.vehicle_name,
                        "registration": resource.registration,
                    },
                )
            if status == "delivered":
                ProofOfDelivery.objects.create(
                    delivery=delivery,
                    actor=admin,
                    recipient_name=delivery.recipient_name,
                    note="Sample proof of delivery.",
                )
                if delivery.cod_amount:
                    LedgerEntry.objects.create(
                        delivery=delivery,
                        actor=admin,
                        kind="cod_collected",
                        amount=delivery.cod_amount,
                    )
            emit_event(
                actor=admin,
                kind="sample.delivery",
                title=f"{delivery.reference} · {delivery.get_status_display()}",
                message=f"Sample delivery to {delivery.destination.area}.",
                delivery=delivery,
                recipients=[admin] + ([client] if index % 3 == 0 else []),
            )
        for resource in resources:
            emit_event(
                actor=admin,
                kind="sample.welcome",
                title="Your workspace is ready",
                message="This is an isolated sample workspace. No live jobs are being dispatched.",
                recipients=[resource.user],
            )
        self.stdout.write(
            self.style.SUCCESS(
                "Sample workspace created. Accounts: demo.admin, demo.client, demo.rider, demo.driver. Use the password you supplied."
            )
        )
