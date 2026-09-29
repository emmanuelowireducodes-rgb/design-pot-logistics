import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from threading import Barrier

from django.db import close_old_connections
from django.test import TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone

from . import workflow
from .models import (
    Assignment,
    Delivery,
    Job,
    Location,
    Organization,
    PricingVersion,
    Quote,
    Resource,
    User,
)


@skipUnlessDBFeature("has_select_for_update")
class AutomaticAssignmentConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.organization = Organization.objects.create(name="Automatic assignment client")
        self.client_user = User.objects.create_user(
            "automatic-client", role=User.Role.CLIENT, organization=self.organization
        )
        self.admin = User.objects.create_user("automatic-admin", role=User.Role.ADMIN)
        self.resources = []
        for index in range(2):
            rider = User.objects.create_user(
                f"automatic-rider-{index}", role=User.Role.RIDER, first_name=f"Rider {index}"
            )
            self.resources.append(
                Resource.objects.create(
                    user=rider,
                    vehicle_type="bike",
                    vehicle_name=f"Bike {index}",
                    registration=f"AUTO-CONCURRENT-{index}",
                    status="available",
                )
            )
        self.pricing = PricingVersion.objects.create(
            label="Automatic assignment concurrency fixture", rules={}
        )
        self.quote_ids = [self._new_quote(index) for index in range(2)]

    def _new_quote(self, index):
        job = Job.objects.create(
            organization=self.organization,
            created_by=self.client_user,
            booking_key=uuid.uuid4(),
        )
        pickup = Location.objects.create(
            display_address=f"Pickup {index}", area="Osu", instructions=""
        )
        destination = Location.objects.create(
            display_address=f"Destination {index}", area="Labone", instructions=""
        )
        Delivery.objects.create(
            job=job,
            pickup=pickup,
            destination=destination,
            recipient_name=f"Recipient {index}",
            recipient_phone="+233200000000",
            package_description="Parcel",
            vehicle_type="bike",
            service="standard",
        )
        quote = Quote.objects.create(
            job=job,
            pricing_version=self.pricing,
            amount=Decimal("10.00"),
            breakdown={},
            expires_at=timezone.now() + timedelta(hours=1),
        )
        job.current_quote = quote
        job.save(update_fields=["current_quote"])
        return quote.pk

    def test_simultaneous_confirmations_observe_committed_resource_workload(self):
        gate = Barrier(2)

        def confirm(quote_id):
            close_old_connections()
            try:
                actor = User.objects.get(pk=self.client_user.pk)
                gate.wait(timeout=15)
                workflow.confirm_quote(actor=actor, quote_id=quote_id)
                return Assignment.objects.get(delivery__job__current_quote_id=quote_id).resource_id
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(confirm, self.quote_ids))

        self.assertEqual(len(set(results)), 2)
