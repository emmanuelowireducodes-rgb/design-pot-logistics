"""Preview or import the approved General Dpot rates into local db.sqlite3 only."""

import json
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from operations.general_price_handoff import import_general_dpot, preview_general_dpot
from operations.models import User


class Command(BaseCommand):
    help = "Preview or import approved General Dpot pricing into local db.sqlite3."

    def add_arguments(self, parser):
        parser.add_argument("--actor", help="Existing active Admin username for audit history.")
        parser.add_argument("--apply", action="store_true", help="Apply the import transactionally.")

    def handle(self, *args, **options):
        database = settings.DATABASES["default"]
        if (
            settings.PRODUCTION
            or database["ENGINE"] != "django.db.backends.sqlite3"
            or Path(database["NAME"]).resolve() != (settings.BASE_DIR / "db.sqlite3").resolve()
            or connection.vendor != "sqlite"
        ):
            raise CommandError("Refusing: this command is restricted to local db.sqlite3.")
        try:
            preview = preview_general_dpot()
            if not options["apply"]:
                self.stdout.write(json.dumps(preview, indent=2, default=str))
                return
            if preview["conflicts"]:
                raise CommandError("Conflicts found; no changes made:\n" + "\n".join(preview["conflicts"]))
            if not options["actor"]:
                raise CommandError("--actor is required with --apply.")
            actor = User.objects.get(username=options["actor"], role="admin", is_active=True)
            result = import_general_dpot(actor=actor)
        except User.DoesNotExist as exc:
            raise CommandError("Choose an existing active local administrator.") from exc
        except ValidationError as exc:
            raise CommandError("Import rolled back: " + "; ".join(exc.messages)) from exc
        self.stdout.write(json.dumps({"preview": preview, "result": result}, indent=2, default=str))
