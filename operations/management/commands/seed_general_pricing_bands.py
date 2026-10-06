"""Preview or apply the approved initial General pricing band configuration."""

import hashlib
import json
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from operations.general_pricing_band_seed import BANDS, apply_seed, preview_seed
from operations.models import FixedRate


class Command(BaseCommand):
    help = "Preview the approved General pricing bands; use --apply locally to seed them."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Apply the seed transactionally.")

    def handle(self, *args, **options):
        database = settings.DATABASES["default"]
        if (
            settings.PRODUCTION
            or database["ENGINE"] != "django.db.backends.sqlite3"
            or Path(database["NAME"]).resolve() != (settings.BASE_DIR / "db.sqlite3").resolve()
            or connection.vendor != "sqlite"
        ):
            raise CommandError("This seed command is restricted to local db.sqlite3.")
        try:
            preview = preview_seed()
        except Exception as exc:
            raise CommandError(f"Preview failed; no changes made: {exc}") from exc
        self.write_preview(preview)
        exact_rate_count, exact_rate_fingerprint = self.general_rate_fingerprint()
        self.stdout.write(
            f"General FixedRates before: {exact_rate_count}; fingerprint: {exact_rate_fingerprint}"
        )
        if preview["conflicts"]:
            raise CommandError("Conflicts found; no changes made.")
        if not options["apply"]:
            self.stdout.write(self.style.SUCCESS("Mode: PREVIEW ONLY; no writes made."))
            return
        try:
            result = apply_seed(preview)
        except ValidationError as exc:
            raise CommandError("Seed rolled back: " + "; ".join(exc.messages)) from exc
        self.stdout.write(
            "Applied: "
            f"{result['bands_created']} bands created; {result['zones_assigned']} zones assigned; "
            f"{result['matrix_created']} General BandRates created."
        )
        after_count, after_fingerprint = self.general_rate_fingerprint()
        unchanged = (exact_rate_count, exact_rate_fingerprint) == (after_count, after_fingerprint)
        self.stdout.write(
            f"General FixedRates after: {after_count}; fingerprint: {after_fingerprint}; "
            f"unchanged: {'YES' if unchanged else 'NO'}"
        )
        if not unchanged:
            raise CommandError("General FixedRates changed unexpectedly during seed.")

    @staticmethod
    def general_rate_fingerprint():
        rows = list(
            FixedRate.objects.filter(card__scope_key="general")
            .order_by("pk")
            .values_list(
                "pk", "rate_key", "version", "origin_id", "destination_id", "vehicle",
                "service", "amount", "bidirectional", "active", "effective_at", "expires_at",
                "actor_id", "reason",
            )
        )
        payload = json.dumps(rows, default=str, separators=(",", ":")).encode("utf-8")
        return len(rows), hashlib.sha256(payload).hexdigest()

    def write_preview(self, preview):
        zones = preview["zones"]
        self.stdout.write(f"Configured bands: {len(BANDS)}")
        for code, name, names in BANDS:
            self.stdout.write(f"Band {code} {name}: {len(names)} zones")
        self.stdout.write(f"Zone labels requested: {sum(len(names) for _, _, names in BANDS)}")
        self.stdout.write(f"Unique OperationalZones resolved: {zones['unique_zones']}")
        self.stdout.write(f"Exact matches: {sum(row['kind'] == 'exact' for row in zones['assignments'])}")
        self.stdout.write(f"Normalized matches: {sum(row['kind'] == 'normalized' for row in zones['assignments'])}")
        self.stdout.write(f"Alias matches: {sum(row['kind'] == 'alias' for row in zones['assignments'])}")
        for row in zones["assignments"]:
            self.stdout.write(
                f"{row['kind'].upper()}: {row['label']} -> {row['zone'].name} "
                f"(Band {row['code']})"
            )
        self.stdout.write(f"Missing zone labels: {len(zones['missing'])}")
        for name in zones["missing"]:
            self.stdout.write(f"MISSING: {name}")
        self.stdout.write(f"Ambiguous matches: {len(zones['ambiguous'])}")
        for label, matches in zones["ambiguous"]:
            self.stdout.write(f"AMBIGUOUS: {label} -> {', '.join(matches)}")
        self.stdout.write(f"Duplicate zone assignments: {len(zones['duplicate_assignments'])}")
        self.stdout.write(f"General symmetric matrix rates already identical: {len(preview['identical_matrix_rates'])}")
        self.stdout.write(f"General symmetric matrix rates to create: {len(preview['matrix_to_create'])}")
        self.stdout.write("General Bike matrix rates: 45 expected")
        self.stdout.write("General Car matrix rates: 28 expected for bands A-G")
        self.stdout.write("Intentionally unset General Car combinations involving H or I: 17")
        self.stdout.write(f"Conflicts: {len(preview['conflicts'])}")
        for conflict in preview["conflicts"]:
            self.stdout.write(f"CONFLICT: {conflict}")
