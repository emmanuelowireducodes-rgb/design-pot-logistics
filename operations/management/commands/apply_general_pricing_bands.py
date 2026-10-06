"""Preview by default; explicitly apply the canonical bands on production PostgreSQL."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from operations.models import User
from operations.production_pricing_bands import apply_rollout, preview_rollout


class Command(BaseCommand):
    help = "Preview General pricing bands; production writes require --apply --actor ADMIN."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true", help="Apply the validated configuration."
        )
        parser.add_argument("--actor", help="Existing active Admin username for audit history.")

    def handle(self, *args, **options):
        self.stdout.write(f"Database engine: {connection.vendor}")
        self.stdout.write(f"DEBUG: {'ON' if settings.DEBUG else 'OFF'}")
        self.stdout.write(f"Mode: {'APPLY REQUESTED' if options['apply'] else 'PREVIEW ONLY'}")
        if connection.vendor not in {"sqlite", "postgresql"}:
            raise CommandError("Unsupported database engine; no changes made.")
        if options["apply"] and (
            connection.vendor != "postgresql" or not settings.PRODUCTION or settings.DEBUG
        ):
            raise CommandError("Apply requires production PostgreSQL with DEBUG=False.")
        preview = preview_rollout()
        self.write_preview(preview)
        if preview["conflicts"]:
            raise CommandError("Conflicts found; no changes made.")
        if not options["apply"]:
            self.stdout.write(
                "PREVIEW COMPLETE; no writes made. Production apply is a separate operation."
            )
            return
        if not options["actor"]:
            raise CommandError("--actor is required with --apply; no changes made.")
        try:
            actor = User.objects.get(username=options["actor"], role="admin", is_active=True)
        except User.DoesNotExist as exc:
            raise CommandError("Choose an existing active Admin; no changes made.") from exc
        try:
            result = apply_rollout(actor=actor)
        except ValidationError as exc:
            raise CommandError("Apply rolled back: " + "; ".join(exc.messages)) from exc
        self.stdout.write(f"Applied: {result}")
        self.stdout.write("FixedRates, client pricing and Premium configuration unchanged.")

    def write_preview(self, preview):
        exact = preview["exact_state"]
        self.stdout.write(
            f"Canonical required exact rates: {exact['canonical']}; expected total=193"
        )
        for rate in exact["overrides"]:
            self.stdout.write(
                f"Approved additional exact override: {rate.pk}; Adenta <-> Madina; "
                f"{rate.vehicle}; amount={rate.amount}; active/current/effective; bidirectional"
            )
        self.stdout.write(
            f"Approved available General Standard exact rates: {exact['counts']}; expected total=195"
        )
        self.stdout.write(
            "Expected bands: 9; assigned zones: 114; Bike rows: 45; Car rows: 28; total rows: 73"
        )
        self.stdout.write("Expected unset H/I Car combinations: 17")
        self.stdout.write(
            f"PricingBands: identical={preview['bands_identical']}; "
            f"to create={preview['bands_to_create']}; conflicts={preview['band_conflicts']}"
        )
        zones = preview["zones"]
        self.stdout.write(
            f"OperationalZone assignments: matching={preview['assignments_matching']}; "
            f"to assign={preview['assignments_to_assign']}; conflicts={preview['assignment_conflicts']}; "
            f"missing={len(zones['missing'])}; ambiguous={len(zones['ambiguous'])}; "
            f"duplicate mappings={len(zones['duplicate_assignments'])}; "
            f"duplicate configured labels={zones['duplicate_config_labels']}"
        )
        for vehicle in ("bike", "car"):
            identical = sum(row[0] == vehicle for row in preview["identical_matrix_rates"])
            create = sum(row[0] == vehicle for row in preview["matrix_to_create"])
            self.stdout.write(
                f"General {vehicle.title()} BandRates: identical={identical}; "
                f"to create={create}; conflicts={preview['matrix_conflicts'][vehicle]}"
            )
        self.stdout.write(f"H/I Car combinations currently unset: {preview['hi_car_unset']} / 17")
        self.stdout.write(
            f"General Standard FixedRates before: Bike={preview['fixed_bike']}; "
            f"Car={preview['fixed_car']}; total={preview['fixed_total']}; affected=0"
        )
        self.stdout.write("Organization-specific FixedRates affected: 0")
        self.stdout.write("Premium rules affected: 0")
        for label in zones["missing"]:
            self.stdout.write(f"MISSING: {label}")
        for label, matches in zones["ambiguous"]:
            self.stdout.write(f"AMBIGUOUS: {label} -> {matches}")
        self.stdout.write(f"Conflicts: {len(preview['conflicts'])}")
        for conflict in preview["conflicts"]:
            self.stdout.write(f"CONFLICT: {conflict}")
