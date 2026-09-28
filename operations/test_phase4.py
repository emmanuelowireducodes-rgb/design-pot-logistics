# mypy: disable-error-code="attr-defined"
import uuid
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, transaction
from django.test import TestCase
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
