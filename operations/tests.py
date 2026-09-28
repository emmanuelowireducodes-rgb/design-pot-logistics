from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .access import visible_deliveries
from .models import (
    Assignment,
    Delivery,
    DomainEvent,
    Job,
    Location,
    Notification,
    Organization,
    PricingVersion,
    Quote,
    QuoteConfirmation,
    Resource,
    User,
)
from .services import confirm_quote, emit_event, reserve_resource


class FoundationTests(TestCase):
    org: Organization
    other_org: Organization
    admin: User
    client_user: User
    other_client: User
    rider: User
    driver: User
    resource: Resource
    driver_resource: Resource
    location: Location
    job: Job
    delivery: Delivery
    price: PricingVersion
    quote: Quote

    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name="Our client")
        cls.other_org = Organization.objects.create(name="Other client")
        cls.admin = User.objects.create_user(
            "admin", password="Long-Test-Password-293", role="admin"
        )
        cls.client_user = User.objects.create_user(
            "client", password="Long-Test-Password-293", role="client", organization=cls.org
        )
        cls.other_client = User.objects.create_user(
            "other", password="Long-Test-Password-293", role="client", organization=cls.other_org
        )
        cls.rider = User.objects.create_user(
            "rider", password="Long-Test-Password-293", role="rider"
        )
        cls.driver = User.objects.create_user(
            "driver", password="Long-Test-Password-293", role="driver"
        )
        cls.resource = Resource.objects.create(
            user=cls.rider, vehicle_type="bike", vehicle_name="Bike 01", registration="TEST-01"
        )
        cls.driver_resource = Resource.objects.create(
            user=cls.driver, vehicle_type="car", vehicle_name="Car 01", registration="TEST-02"
        )
        cls.location = Location.objects.create(
            display_address="Osu", area="Osu", latitude=5.55, longitude=-0.18
        )
        cls.job = Job.objects.create(organization=cls.org, created_by=cls.client_user)
        cls.delivery = Delivery.objects.create(
            reference="DPT-260926-0001",
            job=cls.job,
            pickup=cls.location,
            destination=cls.location,
            recipient_name="Recipient",
            recipient_phone="+233200000000",
            package_description="Package",
            vehicle_type="bike",
        )
        cls.price = PricingVersion.objects.create(
            label="test-v1", is_sample=True, rules={"note": "Not a business rate"}
        )
        cls.quote = Quote.objects.create(
            job=cls.job,
            pricing_version=cls.price,
            amount=Decimal("20.00"),
            breakdown={"sample": True},
            expires_at=timezone.now() + timedelta(hours=1),
        )

    def test_demo_seed_rejects_normal_database(self):
        with self.assertRaises(CommandError):
            call_command("seed_demo")

    def test_admin_component_gallery(self):
        self.client.force_login(self.admin)
        self.assertContains(self.client.get("/admin/components/"), "Clarity in every detail")
        self.client.force_login(self.client_user)
        self.assertEqual(self.client.get("/admin/components/").status_code, 403)

    def test_unassigned_filter_includes_requested(self):
        self.client.force_login(self.client_user)
        response = self.client.get("/client/deliveries/?status=unassigned")
        self.assertEqual(response.context["page_obj"].paginator.count, 1)

    def test_login_required(self):
        self.assertRedirects(
            self.client.get("/admin/dashboard/"),
            "/login/?next=/admin/dashboard/",
            fetch_redirect_response=False,
        )

    def test_all_role_boundaries(self):
        self.assertFalse(self.admin.is_staff)
        self.assertFalse(self.admin.is_superuser)
        for user, own in [
            (self.admin, "admin"),
            (self.client_user, "client"),
            (self.rider, "field"),
            (self.driver, "field"),
        ]:
            self.client.force_login(user)
            for portal in ("admin", "client", "field"):
                response = self.client.get(f"/{portal}/dashboard/")
                self.assertEqual(response.status_code, 200 if portal == own else 403)

        for role in (User.Role.RIDER, User.Role.DRIVER):
            pending = User.objects.create_user(
                f"pending-{role}-admin-boundary",
                role=role,
                operational_access_approved=False,
            )
            self.client.force_login(pending)
            self.assertEqual(self.client.get("/admin/dashboard/").status_code, 403)

    def test_inactive_admin_session_cannot_access_admin_dashboard(self):
        inactive_admin = User.objects.create_user(
            "inactive-admin-boundary", role=User.Role.ADMIN, is_active=False
        )
        self.client.force_login(inactive_admin)
        self.assertRedirects(
            self.client.get("/admin/dashboard/"),
            "/login/?next=/admin/dashboard/",
            fetch_redirect_response=False,
        )

    def test_client_cannot_read_other_organization(self):
        self.client.force_login(self.other_client)
        self.assertEqual(
            self.client.get(reverse("delivery-detail", args=[self.delivery.pk])).status_code, 404
        )
        self.assertEqual(visible_deliveries(self.other_client).count(), 0)
        self.assertEqual(visible_deliveries(self.client_user).count(), 1)

    def test_latest_assignment_controls_access(self):
        Assignment.objects.create(delivery=self.delivery, resource=self.resource, actor=self.admin)
        self.assertEqual(visible_deliveries(self.rider).count(), 1)
        Assignment.objects.create(
            delivery=self.delivery, resource=self.driver_resource, actor=self.admin
        )
        self.assertEqual(visible_deliveries(self.rider).count(), 0)
        self.assertEqual(visible_deliveries(self.driver).count(), 1)

    def test_unassigned_client_has_no_data(self):
        user = User.objects.create_user("unassigned", role="client")
        self.assertFalse(visible_deliveries(user).exists())

    def test_quote_confirmation_is_idempotent_and_scoped(self):
        with self.assertRaises(PermissionDenied):
            confirm_quote(actor=self.other_client, quote_id=self.quote.pk)
        first = confirm_quote(actor=self.client_user, quote_id=self.quote.pk)
        second = confirm_quote(actor=self.client_user, quote_id=self.quote.pk)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(QuoteConfirmation.objects.count(), 1)
        self.assertEqual(DomainEvent.objects.filter(kind="quote.confirmed").count(), 1)

    def test_expired_quote_rejected(self):
        expired = Quote.objects.create(
            job=self.job,
            pricing_version=self.price,
            amount=20,
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        with self.assertRaises(ValidationError):
            confirm_quote(actor=self.client_user, quote_id=expired.pk)

    def test_competing_quote_cannot_replace_confirmation(self):
        confirm_quote(actor=self.client_user, quote_id=self.quote.pk)
        replacement = Quote.objects.create(
            job=self.job,
            pricing_version=self.price,
            amount=30,
            expires_at=timezone.now() + timedelta(hours=1),
        )
        with self.assertRaises(ValidationError):
            confirm_quote(actor=self.client_user, quote_id=replacement.pk)

    def test_history_immutable_at_database_level(self):
        with self.assertRaises(DatabaseError), transaction.atomic():
            Quote.objects.filter(pk=self.quote.pk).update(amount=99)
        with self.assertRaises(DatabaseError), transaction.atomic():
            Location.objects.filter(pk=self.location.pk).update(area="Changed")
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.amount, Decimal("20.00"))

    def test_quote_save_cannot_mutate_history(self):
        self.quote.amount = Decimal("99")
        with self.assertRaises(ValidationError):
            self.quote.save()

    def test_negative_cod_rejected_by_database(self):
        with self.assertRaises(DatabaseError), transaction.atomic():
            Delivery.objects.filter(pk=self.delivery.pk).update(cod_amount=-1)

    def test_premium_requires_window(self):
        with self.assertRaises(DatabaseError), transaction.atomic():
            Delivery.objects.filter(pk=self.delivery.pk).update(service="premium")

    def test_coordinates_validated(self):
        location = Location(display_address="Invalid", area="Test", latitude=91, longitude=0)
        with self.assertRaises(ValidationError):
            location.full_clean()

    def test_schedule_conflict_and_adjacent_window(self):
        start = timezone.now()
        reserve_resource(
            actor=self.admin,
            resource_id=self.resource.pk,
            starts_at=start,
            ends_at=start + timedelta(hours=1),
            kind="break",
        )
        with self.assertRaises(ValidationError):
            reserve_resource(
                actor=self.admin,
                resource_id=self.resource.pk,
                starts_at=start + timedelta(minutes=30),
                ends_at=start + timedelta(hours=2),
                kind="booking",
            )
        reserve_resource(
            actor=self.admin,
            resource_id=self.resource.pk,
            starts_at=start + timedelta(hours=1),
            ends_at=start + timedelta(hours=2),
            kind="booking",
        )
        with self.assertRaises(PermissionDenied):
            reserve_resource(
                actor=self.rider,
                resource_id=self.resource.pk,
                starts_at=start,
                ends_at=start + timedelta(hours=1),
                kind="break",
            )

    def test_notification_audience_and_read_ownership(self):
        emit_event(
            actor=self.admin,
            kind="test",
            title="Test",
            message="Test",
            recipients=[self.client_user, self.client_user],
        )
        notification = Notification.objects.get()
        self.client.force_login(self.other_client)
        url = reverse("notification-read", args=[notification.pk])
        self.assertEqual(self.client.post(url).status_code, 404)
        self.client.force_login(self.client_user)
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertEqual(self.client.post(url).status_code, 302)
        self.client.post(url)
        notification.refresh_from_db()
        self.assertIsNotNone(notification.read_at)
        self.assertEqual(DomainEvent.objects.filter(kind="notification.read").count(), 1)

    def test_event_and_notifications_rollback_together(self):
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                emit_event(
                    actor=self.admin,
                    kind="rollback",
                    title="Test",
                    message="Test",
                    recipients=[self.admin],
                )
                raise RuntimeError
        self.assertFalse(DomainEvent.objects.filter(kind="rollback").exists())

    def test_csrf_required(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.admin)
        self.assertEqual(client.post("/logout/").status_code, 403)

    def test_login_throttled(self):
        for _ in range(10):
            response = self.client.post("/login/", {"username": "no-user", "password": "wrong"})
            self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.client.post("/login/", {"username": "no-user", "password": "wrong"}).status_code,
            429,
        )

    def test_login_redirects_and_logout_requires_post(self):
        response = self.client.post(
            "/login/",
            {
                "username": "admin",
                "password": "Long-Test-Password-293",
                "next": "https://malicious.example",
            },
        )
        self.assertEqual(response["Location"], "/")
        self.assertEqual(self.client.get("/logout/").status_code, 405)
        self.assertEqual(self.client.post("/logout/").status_code, 302)

    def test_all_navigation_routes(self):
        sections = {
            "admin": [
                "dashboard",
                "deliveries",
                "dispatch",
                "calendar",
                "fleet",
                "clients",
                "live",
                "finance",
                "notifications",
                "reports",
                "pricing",
                "settings",
            ],
            "client": [
                "dashboard",
                "booking",
                "deliveries",
                "finance",
                "notifications",
                "settings",
            ],
            "field": ["dashboard", "deliveries", "notifications", "settings"],
        }
        for user, portal in [
            (self.admin, "admin"),
            (self.client_user, "client"),
            (self.rider, "field"),
            (self.driver, "field"),
        ]:
            self.client.force_login(user)
            for section in sections[portal]:
                self.assertEqual(
                    self.client.get(f"/{portal}/{section}/").status_code, 200, f"{portal}/{section}"
                )

    def test_search_and_pagination(self):
        self.client.force_login(self.client_user)
        response = self.client.get("/client/deliveries/?q=missing&page=invalid")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["page_obj"].paginator.count, 0)

    def test_private_pages_and_security_headers(self):
        self.client.force_login(self.admin)
        response = self.client.get("/admin/dashboard/")
        self.assertEqual(response["Cache-Control"], "private, no-store")
        csp = response["Content-Security-Policy"]
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("form-action 'self'", csp)
        self.assertNotIn("https://accounts.google.com", csp)
        self.assertIn("default-src 'self'", csp)
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("form-action *", csp)

    @override_settings(DEBUG=False)
    def test_error_pages(self):
        self.client.force_login(self.client_user)
        self.assertContains(
            self.client.get("/admin/dashboard/"), "isn't available", status_code=403
        )
        self.assertContains(self.client.get("/client/not-real/"), "couldn't find", status_code=404)
