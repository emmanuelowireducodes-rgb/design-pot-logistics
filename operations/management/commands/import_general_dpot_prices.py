"""Preview by default; explicitly apply approved General Dpot rates on PostgreSQL."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from operations.general_price_handoff import import_general_dpot, preview_general_dpot
from operations.models import User


class Command(BaseCommand):
    help = "Preview General Dpot prices; use --apply --actor USER only on production PostgreSQL."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Apply the validated import.")
        parser.add_argument("--actor", help="Existing active Admin username for audit history.")

    def handle(self, *args, **options):
        self.stdout.write(f"Database engine: {connection.vendor}")
        self.stdout.write(f"DEBUG: {'ON' if settings.DEBUG else 'OFF'}")
        if connection.vendor not in {"sqlite", "postgresql"}:
            raise CommandError("Unsupported database engine; no changes made.")
        if options["apply"] and (
            connection.vendor != "postgresql" or not settings.PRODUCTION or settings.DEBUG
        ):
            raise CommandError(
                "Apply requires PostgreSQL, DPOT_ENV=production and DEBUG=False; no changes made."
            )
        try:
            preview = preview_general_dpot()
        except ValidationError as exc:
            raise CommandError("Preview validation failed: " + "; ".join(exc.messages)) from exc
        self.write_preview(preview)
        if not options["apply"]:
            self.stdout.write("Mode: PREVIEW ONLY; no writes made.")
            return
        if preview["conflicts"]:
            raise CommandError("Conflicts found; no changes made.")
        if not options["actor"]:
            raise CommandError("--actor is required with --apply; no changes made.")
        try:
            actor = User.objects.get(username=options["actor"], role="admin", is_active=True)
        except User.DoesNotExist as exc:
            raise CommandError("Choose an existing active Admin; no changes made.") from exc
        try:
            result = import_general_dpot(actor=actor)
        except ValidationError as exc:
            raise CommandError("Import rolled back: " + "; ".join(exc.messages)) from exc
        self.write_result(result)

    def write_preview(self, preview):
        self.stdout.write(
            f"General PriceCard found: {'YES' if preview['general_card_found'] else 'NO'}"
        )
        self.stdout.write(f"Matching OperationalZones: {preview['existing_relevant_areas']}")
        self.stdout.write(f"Missing OperationalZones: {preview['areas_to_create']}")
        self.stdout.write(
            "General Bike rates already identical: "
            f"{preview['identical_bike_rates']}"
        )
        self.stdout.write(
            "General Car rates already identical: "
            f"{preview['identical_car_rates']}"
        )
        self.stdout.write(f"Bike rates to create: {preview['bike_rates_to_create']}")
        self.stdout.write(f"Car rates to create: {preview['car_rates_to_create']}")
        self.stdout.write(f"Conflicting rates: {len(preview['conflicts'])}")
        for conflict in preview["conflicts"]:
            self.stdout.write(f"CONFLICT: {conflict}")
        self.stdout.write("Organization-specific rates affected: 0")

    def write_result(self, result):
        self.stdout.write(
            "General PriceCard: "
            f"{'created' if result['card_created'] else 'reused'} ({result['card_name']})"
        )
        self.stdout.write(
            f"OperationalZones: {result['areas_created']} created; "
            f"{result['areas_reused']} reused"
        )
        self.stdout.write(
            f"Bike rates: {result['bike_created']} created; "
            f"{result['bike_skipped']} skipped"
        )
        self.stdout.write(
            f"Car rates: {result['car_created']} created; "
            f"{result['car_skipped']} skipped"
        )
        self.stdout.write(f"Conflicts: {result['conflicts']}")
        self.stdout.write(f"Total resulting General rates: {result['final_current_rates']}")
