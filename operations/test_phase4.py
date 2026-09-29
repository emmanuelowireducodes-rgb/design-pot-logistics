# mypy: disable-error-code="attr-defined"
import uuid
from datetime import timedelta
from decimal import Decimal
from threading import Barrier, Thread

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, close_old_connections, transaction
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone

from . import workflow
from .finance_queries import dashboard_totals
from .models import (
    Assignment,
    ClientSettlement,
    Delivery,
    DomainEvent,
    Job,
    LedgerEntry,
    Location,
    OperationalZone,
    Organization,
    PricingVersion,
    Quote,
    QuoteConfirmation,
    Resource,
    StatusEvent,
    User,
)


class Phase4FinanceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name="Phase Four Client")
        cls.other_org = Organization.objects.create(name="=1+1")
        cls.client_user = User.objects.create_user(
            "phase4-client", password="sample-pass", role="client", organization=cls.org
        )
        cls.other_client = User.objects.create_user(
            "phase4-other", password="sample-pass", role="client", organization=cls.other_org
        )
        cls.admin = User.objects.create_user("phase4-admin", password="sample-pass", role="admin")
        cls.rider = User.objects.create_user("phase4-rider", password="sample-pass", role="rider")
        cls.driver = User.objects.create_user("phase4-driver", password="sample-pass", role="driver")
        cls.other_rider = User.objects.create_user(
            "phase4-rider2", password="sample-pass", role="rider"
        )
        cls.resource = Resource.objects.create(
            user=cls.rider,
            vehicle_type="bike",
            vehicle_name="Phase 4 bike",
            registration="P4-BIKE-1",
        )
        cls.zone = OperationalZone.objects.create(name="Phase4 Osu")
        cls.pickup = Location.objects.create(display_address="Pickup", area="Osu", zone=cls.zone)
        cls.destination = Location.objects.create(
            display_address="Destination", area="Labone", zone=cls.zone
        )
        cls.pricing = PricingVersion.objects.create(label="Phase 4 test pricing")

    def make_delivery(self, *, organization=None, reference="DPT-P4-001", cod="350.00"):
        organization = organization or self.org
        job = Job.objects.create(
            organization=organization,
            created_by=(self.client_user if organization.pk == self.org.pk else self.other_client),
        )
        quote = Quote.objects.create(
            job=job,
            pricing_version=self.pricing,
            amount=Decimal("70.00"),
            breakdown={},
            expires_at=timezone.now() + timedelta(days=1),
        )
        QuoteConfirmation.objects.create(job=job, quote=quote, actor=job.created_by)
        delivery = Delivery.objects.create(
            reference=reference,
            job=job,
            pickup=self.pickup,
            destination=self.destination,
            recipient_name="Recipient",
            recipient_phone="+233200000001",
            package_description="Parcel",
            vehicle_type="bike",
            status=Delivery.Status.DELIVERED,
            payment_method="cod",
            cod_amount=Decimal(cod),
        )
        completed_at = timezone.now() - timedelta(hours=1)
        StatusEvent.objects.create(
            delivery=delivery,
            actor=self.rider,
            sequence=1,
            status=Delivery.Status.DELIVERED,
            created_at=completed_at,
        )
        Assignment.objects.create(
            delivery=delivery,
            resource=self.resource,
            actor=self.admin,
            resource_snapshot={},
        )
        return delivery

    def make_reconciled(self, delivery):
        collection = LedgerEntry.objects.create(
            delivery=delivery,
            actor=self.rider,
            kind="cod_collected",
            amount=delivery.cod_amount,
            currency="GHS",
            method="cash",
        )
        reconciliation = LedgerEntry.objects.create(
            delivery=delivery,
            actor=self.admin,
            kind="cod_reconciled",
            amount=collection.amount,
            currency="GHS",
            method="cash",
        )
        return collection, reconciliation

    def test_dashboard_uses_confirmed_delivery_fee_and_separate_cod(self):
        delivery = self.make_delivery()
        self.make_reconciled(delivery)
        totals = dashboard_totals(
            {}, timezone.now() - timedelta(days=1), timezone.now() + timedelta(days=1)
        )
        self.assertEqual(totals["delivery_revenue"], Decimal("70.00"))
        self.assertEqual(totals["cod_collected"], Decimal("350.00"))
        self.assertEqual(totals["cod_outstanding"], Decimal("0.00"))
        self.assertEqual(totals["cod_pending_settlement"], Decimal("350.00"))

    def test_settlement_links_reconciliations_idempotently_and_prevents_duplicates(self):
        first = self.make_delivery(reference="DPT-P4-002", cod="120.00")
        second = self.make_delivery(reference="DPT-P4-003", cod="80.00")
        _, one = self.make_reconciled(first)
        _, two = self.make_reconciled(second)
        key = uuid.uuid4()
        settlement = workflow.settle_reconciled_cod(
            actor=self.admin,
            organization_id=self.org.pk,
            reconciliation_ids=[one.pk, two.pk],
            request_key=key,
            method="Reported bank transfer",
            external_reference="BANK-123",
        )
        self.assertEqual(settlement.amount, Decimal("200.00"))
        self.assertEqual(settlement.entries.filter(kind="cod_settled").count(), 2)
        self.assertEqual(
            workflow.settle_reconciled_cod(
                actor=self.admin,
                organization_id=self.org.pk,
                reconciliation_ids=[one.pk, two.pk],
                request_key=key,
            ).pk,
            settlement.pk,
        )
        self.assertTrue(DomainEvent.objects.filter(kind="cod.client_settlement_recorded").exists())
        with self.assertRaises(ValidationError):
            workflow.settle_reconciled_cod(
                actor=self.admin,
                organization_id=self.org.pk,
                reconciliation_ids=[one.pk],
                request_key=uuid.uuid4(),
            )

    def test_settlement_rejects_cross_client_unreconciled_and_non_admin(self):
        first = self.make_delivery(reference="DPT-P4-004")
        other = self.make_delivery(organization=self.other_org, reference="DPT-P4-005")
        _, one = self.make_reconciled(first)
        _, two = self.make_reconciled(other)
        with self.assertRaises(ValidationError):
            workflow.settle_reconciled_cod(
                actor=self.admin,
                organization_id=self.org.pk,
                reconciliation_ids=[one.pk, two.pk],
                request_key=uuid.uuid4(),
            )
        with self.assertRaises(PermissionDenied):
            workflow.settle_reconciled_cod(
                actor=self.rider,
                organization_id=self.org.pk,
                reconciliation_ids=[one.pk],
                request_key=uuid.uuid4(),
            )
        with self.assertRaises(ValidationError):
            workflow.settle_reconciled_cod(
                actor=self.admin,
                organization_id=self.org.pk,
                reconciliation_ids=[uuid.uuid4()],
                request_key=uuid.uuid4(),
            )

    def test_settlement_selector_shows_only_available_reconciled_cod(self):
        eligible = self.make_delivery(reference="DPT-P4-SET-AVAILABLE", cod="120.00")
        self.make_reconciled(eligible)
        other_client_delivery = self.make_delivery(
            organization=self.other_org, reference="DPT-P4-SET-OTHER", cod="90.00"
        )
        self.make_reconciled(other_client_delivery)
        settled_delivery = self.make_delivery(reference="DPT-P4-SET-SETTLED", cod="30.00")
        _, settled_reconciliation = self.make_reconciled(settled_delivery)
        workflow.settle_reconciled_cod(
            actor=self.admin,
            organization_id=self.org.pk,
            reconciliation_ids=[settled_reconciliation.pk],
            request_key=uuid.uuid4(),
        )
        unrec_delivery = self.make_delivery(reference="DPT-P4-SET-COLLECTED", cod="55.00")
        LedgerEntry.objects.create(
            delivery=unrec_delivery,
            actor=self.rider,
            kind="cod_collected",
            amount=unrec_delivery.cod_amount,
            currency="GHS",
            method="cash",
        )

        self.client.force_login(self.admin)
        response = self.client.get(
            "/admin/finance/settlements/", {"organization": str(self.org.pk)}
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Available to settle for Phase Four Client:")
        self.assertContains(response, "GH₵120.00")
        self.assertContains(response, eligible.reference)
        self.assertNotContains(response, other_client_delivery.reference)
        self.assertNotContains(response, settled_delivery.reference)
        self.assertNotContains(response, unrec_delivery.reference)
        self.assertContains(response, "Collected but unreconciled COD cannot be settled.")
        self.assertContains(response, 'method="get"')
        self.assertContains(response, "Show eligible COD")
        self.assertContains(response, "Selected settlement amount:")
        self.assertContains(response, 'data-settlement-item-amount-cents="12000"')
        self.assertEqual(response.context["available_amount"], Decimal("120.00"))

    def test_client_without_eligible_cod_sees_explicit_empty_state(self):
        delivery = self.make_delivery(reference="DPT-P4-SET-UNRECONCILED")
        LedgerEntry.objects.create(
            delivery=delivery,
            actor=self.rider,
            kind="cod_collected",
            amount=delivery.cod_amount,
            currency="GHS",
            method="cash",
        )
        self.client.force_login(self.admin)
        response = self.client.get(
            "/admin/finance/settlements/", {"organization": str(self.org.pk)}
        )
        self.assertContains(
            response, "No reconciled COD is currently available to settle for this Client."
        )
        self.assertContains(response, "Available to settle for Phase Four Client:")
        self.assertContains(response, "GH₵0.00")

    def test_settlement_post_records_selected_items_and_ignores_manipulated_amount(self):
        delivery = self.make_delivery(reference="DPT-P4-SET-POST", cod="145.25")
        _, reconciliation = self.make_reconciled(delivery)
        key = uuid.uuid4()
        self.client.force_login(self.admin)

        response = self.client.post(
            "/admin/finance/settlements/",
            {
                "request_key": str(key),
                "organization": str(self.org.pk),
                "reconciliation_ids": [str(reconciliation.pk)],
                "amount": "999999.99",
                "method": "Reported bank transfer",
                "external_reference": "BANK-P4-POST",
            },
        )

        self.assertEqual(response.status_code, 302)
        settlement = ClientSettlement.objects.get()
        settled_entry = LedgerEntry.objects.get(kind="cod_settled")
        self.assertEqual(settlement.amount, Decimal("145.25"))
        self.assertEqual(settlement.organization, self.org)
        self.assertEqual(settled_entry.reconciled_entry, reconciliation)
        event = DomainEvent.objects.get(kind="cod.client_settlement_recorded")
        self.assertEqual(event.actor, self.admin)
        self.assertEqual(event.request_key, key)
        self.assertEqual(event.payload["amount"], "145.25")
        self.assertEqual(event.payload["organization_id"], str(self.org.pk))
        self.assertContains(self.client.get(response["Location"]), "Settlement recorded.")

        self.client.force_login(self.client_user)
        client_finance = self.client.get("/client/finance/")
        self.assertContains(client_finance, "GH₵145.25")
        self.assertContains(client_finance, "GH₵0.00")

    def test_invalid_empty_stale_and_cross_client_submissions_show_errors(self):
        own = self.make_delivery(reference="DPT-P4-SET-OWN", cod="100.00")
        _, own_reconciliation = self.make_reconciled(own)
        other = self.make_delivery(
            organization=self.other_org, reference="DPT-P4-SET-FOREIGN", cod="75.00"
        )
        _, foreign_reconciliation = self.make_reconciled(other)
        self.client.force_login(self.admin)
        base = {"request_key": str(uuid.uuid4()), "organization": str(self.org.pk)}

        empty = self.client.post("/admin/finance/settlements/", base)
        self.assertEqual(empty.status_code, 200)
        self.assertContains(empty, "Select at least one reconciled COD item.")

        cross_client = self.client.post(
            "/admin/finance/settlements/",
            {**base, "reconciliation_ids": [str(foreign_reconciliation.pk)]},
        )
        self.assertEqual(cross_client.status_code, 200)
        self.assertContains(
            cross_client,
            "One or more selected COD items are no longer eligible for this Client.",
        )
        self.assertFalse(ClientSettlement.objects.exists())

        malformed_client = self.client.post(
            "/admin/finance/settlements/",
            {**base, "organization": "not-a-client-id", "reconciliation_ids": [str(own_reconciliation.pk)]},
        )
        self.assertEqual(malformed_client.status_code, 200)
        self.assertContains(malformed_client, "Select a valid choice")

        valid_data = {
            **base,
            "request_key": str(uuid.uuid4()),
            "reconciliation_ids": [str(own_reconciliation.pk)],
        }
        self.client.post("/admin/finance/settlements/", valid_data)
        duplicate = self.client.post("/admin/finance/settlements/", valid_data)
        self.assertEqual(duplicate.status_code, 200)
        self.assertContains(duplicate, "no longer eligible for this Client")
        self.assertEqual(ClientSettlement.objects.count(), 1)
        self.assertEqual(LedgerEntry.objects.filter(kind="cod_settled").count(), 1)

    def test_client_rider_and_driver_cannot_post_client_settlement(self):
        for user in (self.client_user, self.rider, self.driver):
            with self.subTest(role=user.role):
                self.client.force_login(user)
                response = self.client.post(
                    "/admin/finance/settlements/",
                    {"organization": str(self.org.pk), "reconciliation_ids": [str(uuid.uuid4())]},
                )
                self.assertEqual(response.status_code, 403)
        self.assertFalse(ClientSettlement.objects.exists())

    def test_client_settlement_is_immutable_at_database_layer(self):
        settlement = ClientSettlement.objects.create(
            organization=self.org,
            recorded_by=self.admin,
            reference="DPT-SET-TEST-1",
            amount=Decimal("10.00"),
        )
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                ClientSettlement.objects.filter(pk=settlement.pk).update(note="changed")
        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                ClientSettlement.objects.filter(pk=settlement.pk).delete()
        self.assertTrue(ClientSettlement.objects.filter(pk=settlement.pk).exists())

    def test_admin_finance_and_reports_render_and_enforce_roles(self):
        delivery = self.make_delivery()
        self.make_reconciled(delivery)
        self.client.force_login(self.admin)
        for path in (
            "/admin/finance/",
            "/admin/finance/cod/",
            "/admin/finance/settlements/",
            "/admin/reports/",
            "/admin/reports/revenue/",
            "/admin/reports/clients/",
            "/admin/reports/riders/",
            "/admin/reports/status/",
            "/admin/reports/runs/",
            "/admin/reports/cod/",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)
        self.client.force_login(self.rider)
        self.assertEqual(self.client.get("/admin/finance/").status_code, 403)
        self.assertEqual(self.client.get("/admin/reports/revenue/export.csv").status_code, 403)

    def test_cod_report_includes_expected_but_uncollected_items(self):
        collected = self.make_delivery(reference="DPT-P4-COLLECTED")
        expected_only = self.make_delivery(reference="DPT-P4-EXPECTED", cod="111.00")
        self.make_reconciled(collected)
        self.client.force_login(self.admin)
        page = self.client.get("/admin/reports/cod/")
        self.assertContains(page, collected.reference)
        self.assertContains(page, expected_only.reference)
        self.assertContains(page, "Uncollected")
        export = self.client.get("/admin/reports/cod/export.csv")
        body = b"".join(export.streaming_content).decode()
        self.assertIn("111.00", body)
        self.assertIn("Uncollected", body)

    def test_client_finance_is_organization_scoped_and_admin_csv_is_safe(self):
        own = self.make_delivery()
        self.make_delivery(organization=self.other_org, reference="DPT-P4-006")
        self.client.force_login(self.client_user)
        response = self.client.get("/client/finance/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, own.reference)
        self.assertNotContains(response, "DPT-P4-006")
        self.client.force_login(self.admin)
        response = self.client.get("/admin/reports/clients/export.csv")
        self.assertEqual(response.status_code, 200)
        body = b"".join(response.streaming_content).decode()
        self.assertIn("'=1+1", body)

    def test_rider_cannot_access_client_finance_or_admin_reconciliation(self):
        self.client.force_login(self.rider)
        self.assertEqual(self.client.get("/client/finance/").status_code, 403)
        self.assertEqual(self.client.get("/admin/finance/cod/").status_code, 403)


@skipUnlessDBFeature("has_select_for_update")
class SettlementConcurrencyTests(TransactionTestCase):
    def test_two_admin_settlements_cannot_settle_the_same_reconciliation_twice(self):
        organization = Organization.objects.create(name="Concurrent settlement Client")
        admin = User.objects.create_user(
            "settlement-concurrency-admin", password="safe-test-password", role="admin"
        )
        rider = User.objects.create_user(
            "settlement-concurrency-rider", password="safe-test-password", role="rider"
        )
        resource = Resource.objects.create(
            user=rider, vehicle_type="bike", vehicle_name="Concurrency bike", registration="CT-1"
        )
        zone = OperationalZone.objects.create(name="Settlement concurrency zone")
        pickup = Location.objects.create(display_address="Pickup", area="Osu", zone=zone)
        destination = Location.objects.create(display_address="Drop", area="Labone", zone=zone)
        job = Job.objects.create(organization=organization, created_by=admin)
        pricing = PricingVersion.objects.create(label="Settlement concurrency pricing")
        quote = Quote.objects.create(
            job=job,
            pricing_version=pricing,
            amount=Decimal("10.00"),
            breakdown={},
            expires_at=timezone.now() + timedelta(days=1),
        )
        QuoteConfirmation.objects.create(job=job, quote=quote, actor=admin)
        delivery = Delivery.objects.create(
            reference="DPT-SET-CONCURRENCY",
            job=job,
            pickup=pickup,
            destination=destination,
            recipient_name="Recipient",
            recipient_phone="+233200000001",
            package_description="Parcel",
            vehicle_type="bike",
            status=Delivery.Status.DELIVERED,
            payment_method="cod",
            cod_amount=Decimal("80.00"),
        )
        Assignment.objects.create(
            delivery=delivery, resource=resource, actor=admin, resource_snapshot={}
        )
        reconciliation = LedgerEntry.objects.create(
            delivery=delivery,
            actor=admin,
            kind="cod_reconciled",
            amount=Decimal("80.00"),
            currency="GHS",
        )
        barrier = Barrier(2)
        outcomes = []

        def settle(key):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                workflow.settle_reconciled_cod(
                    actor=admin,
                    organization_id=organization.pk,
                    reconciliation_ids=[reconciliation.pk],
                    request_key=key,
                )
                outcomes.append("settled")
            except ValidationError:
                outcomes.append("already settled")
            finally:
                close_old_connections()

        threads = [Thread(target=settle, args=(uuid.uuid4(),)) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertCountEqual(outcomes, ["settled", "already settled"])
        self.assertEqual(ClientSettlement.objects.count(), 1)
        self.assertEqual(LedgerEntry.objects.filter(kind="cod_settled").count(), 1)
