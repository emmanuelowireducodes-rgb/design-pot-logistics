from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from operations.push import deliver_pending


class Command(BaseCommand):
    help = "Deliver committed Web Push notifications from the transactional outbox."

    def handle(self, *args, **options):
        if not settings.WEB_PUSH_ENABLED:
            raise CommandError("Configure the three DPOT_VAPID_* settings before sending push.")
        try:
            result = deliver_pending()
        except ImportError as exc:
            raise CommandError("Install project requirements to enable Web Push.") from exc
        self.stdout.write(
            f"Outbox rows: {result['queued']}; sent: {result['sent']}; "
            f"failed: {result['failed']}; expired devices removed: {result['removed']}"
        )
