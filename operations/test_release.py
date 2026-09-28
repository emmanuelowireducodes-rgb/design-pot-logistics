# mypy: disable-error-code="attr-defined"
import base64
import json
import uuid
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from django.test import TestCase, override_settings
from django.urls import reverse
from py_vapid import Vapid
from pywebpush import WebPusher, webpush

from . import runs
from .locations import assign_zone
from .models import (
    DomainEvent,
    Notification,
    OperationalZone,
    Organization,
    PushOutbox,
    PushSubscription,
    Resource,
    User,
)
from .pricing import manual_quote
from .push import deliver_pending
from .services import emit_event
from .workflow import save_booking


def b64url(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class V1ReleaseTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name="Release client")
        cls.client_user = User.objects.create_user(
            "release-client", password="Release-pass-293!", role="client", organization=cls.org
        )
        cls.rider = User.objects.create_user("release-rider", password="Release-pass-293!", role="rider")
        cls.admin = User.objects.create_user("release-admin", password="Release-pass-293!", role="admin")

    def test_role_navigation_shows_visible_post_sign_out_and_no_cross_role_items(self):
        for user, forbidden in (
            (self.client_user, ("Dispatch", "Riders &amp; drivers")),
            (self.rider, ("Finance", "Create delivery")),
            (self.admin, ()),
        ):
            self.client.force_login(user)
            response = self.client.get("/settings/")
            if response.status_code == 404:
                response = self.client.get(
                    "/admin/settings/" if user.role == "admin" else
                    "/client/settings/" if user.role == "client" else "/field/settings/"
                )
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Sign Out")
            self.assertContains(response, 'aria-controls="navigation"')
            self.assertContains(response, 'class="drawer-backdrop"')
            for label in forbidden:
                self.assertNotContains(response, label)

    def test_logout_clears_session_for_each_authenticated_role(self):
        for user, protected_url in (
            (self.client_user, "/client/settings/"),
            (self.rider, "/field/settings/"),
            (self.admin, "/admin/settings/"),
        ):
            self.client.force_login(user)
            response = self.client.post(reverse("logout"))
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response["Location"], "/login/")
            self.assertIn(self.client.get(protected_url).status_code, (302, 403))

    def test_client_booking_hides_internal_area_and_zone_fields(self):
        self.client.force_login(self.client_user)
        response = self.client.get("/client/booking/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pickup address")
        self.assertContains(response, "Destination address")
        self.assertContains(response, 'id="client-booking-form"')
        self.assertContains(response, 'form="client-booking-form"')
        self.assertContains(response, "Prepare quote")
        self.assertNotContains(response, "Pickup area")
        self.assertNotContains(response, "Destination area")
        self.assertNotContains(response, "operational zone")

    def test_mobile_dashboards_prioritize_the_role_primary_task(self):
        self.client.force_login(self.client_user)
        client_home = self.client.get("/client/dashboard/")
        self.assertEqual(client_home.status_code, 200)
        self.assertContains(client_home, 'class="client-phone-dashboard"')
        self.assertContains(client_home, "Book a delivery")
        self.assertContains(client_home, "Active deliveries")

        self.client.force_login(self.admin)
        admin_home = self.client.get("/admin/dashboard/")
        self.assertEqual(admin_home.status_code, 200)
        self.assertContains(admin_home, 'class="admin-phone-dashboard"')
        self.assertContains(admin_home, "Needs attention.")
        self.assertContains(admin_home, "Quotes to review")
        self.assertContains(admin_home, "Open dispatch")

        dispatch = self.client.get("/admin/dispatch/")
        self.assertContains(dispatch, 'class="mobile-filter-panel"')
        self.assertContains(dispatch, 'class="table-scroll mobile-card-table"')

    def test_simple_written_booking_keeps_both_zones_unclassified(self):
        data = {
            "booking_key": str(uuid.uuid4()), "revision": 0,
            "pickup_address": "Shop near the old station", "pickup_instructions": "Call first",
            "pickup_landmark": "Blue gate", "pickup_google_maps_url": "",
            "destination_address": "House by the community park", "destination_instructions": "",
            "destination_landmark": "Large mango tree", "destination_google_maps_url": "",
            "recipient_name": "Recipient", "recipient_phone": "+233200000000",
            "package_description": "Parcel", "instructions": "Keep upright",
            "vehicle_type": "bike", "service": "standard", "payment_method": "cod",
            "cod_amount": "45.00",
        }
        delivery = save_booking(actor=self.client_user, data=data)
        delivery.refresh_from_db()
        self.assertEqual(delivery.pickup.display_address, data["pickup_address"])
        self.assertEqual(delivery.destination.display_address, data["destination_address"])
        self.assertEqual(delivery.pickup.area, "Unclassified")
        self.assertEqual(delivery.destination.area, "Unclassified")
        self.assertIsNone(delivery.pickup.zone_id)
        self.assertIsNone(delivery.destination.zone_id)
        self.assertTrue(delivery.job.quote_requested)
        self.assertIsNone(delivery.job.current_quote_id)

    @override_settings(WEB_PUSH_ENABLED=True, VAPID_PUBLIC_KEY="public", VAPID_PRIVATE_KEY="private", VAPID_SUBJECT="mailto:ops@example.com")
    def test_push_subscription_is_authenticated_validated_and_owner_scoped(self):
        endpoint = "https://fcm.googleapis.com/fcm/send/release-token"
        payload = {"endpoint": endpoint, "keys": {"p256dh": b64url(b"p" * 65), "auth": b64url(b"a" * 16)}}
        url = reverse("push-subscription")
        self.assertEqual(self.client.post(url, data=json.dumps(payload), content_type="application/json").status_code, 401)
        self.client.force_login(self.client_user)
        response = self.client.post(url, data=json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(PushSubscription.objects.get().user, self.client_user)
        self.assertEqual(self.client.post(url, data=json.dumps(payload), content_type="application/json").status_code, 201)
        self.client.force_login(self.rider)
        self.assertEqual(self.client.post(url, data=json.dumps(payload), content_type="application/json").status_code, 403)
        bad = dict(payload, endpoint="https://127.0.0.1/private")
        self.assertEqual(self.client.post(url, data=json.dumps(bad), content_type="application/json").status_code, 400)
        self.client.force_login(self.client_user)
        self.assertEqual(self.client.delete(url, data=json.dumps({"endpoint": endpoint}), content_type="application/json").status_code, 200)
        self.assertFalse(PushSubscription.objects.exists())

    @override_settings(WEB_PUSH_ENABLED=True, VAPID_PUBLIC_KEY="public", VAPID_PRIVATE_KEY="private", VAPID_SUBJECT="mailto:ops@example.com")
    def test_push_failure_is_async_and_payload_contains_no_delivery_details(self):
        device = PushSubscription.objects.create(
            user=self.client_user,
            endpoint="https://fcm.googleapis.com/fcm/send/release-token",
            p256dh=b64url(b"p" * 65), auth=b64url(b"a" * 16),
        )
        event = emit_event(
            actor=self.admin, kind="delivery.created", title="Private title",
            message="Contains recipient 0240000000 and GHS 800", recipients=[self.client_user],
        )
        notification = Notification.objects.get(event=event)
        outbox = PushOutbox.objects.get(notification=notification)
        seen = {}
        def send(subscription_info, **kwargs):
            seen.update(kwargs)
            raise RuntimeError("network down")
        result = deliver_pending(send=send)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(notification.title, "Private title")
        self.assertIn("Design-Pot Logistics", seen["data"])
        self.assertNotIn("0240000000", seen["data"])
        self.assertNotIn("800", seen["data"])
        outbox.refresh_from_db()
        self.assertEqual(outbox.attempts, 1)
        self.assertIsNone(outbox.sent_at)
        self.assertTrue(PushSubscription.objects.filter(pk=device.pk).exists())

    @override_settings(WEB_PUSH_ENABLED=True, VAPID_PUBLIC_KEY="public", VAPID_PRIVATE_KEY="private", VAPID_SUBJECT="mailto:ops@example.com")
    def test_expired_push_subscription_is_removed(self):
        PushSubscription.objects.create(
            user=self.client_user, endpoint="https://fcm.googleapis.com/fcm/send/expired",
            p256dh=b64url(b"p" * 65), auth=b64url(b"a" * 16),
        )
        event = emit_event(actor=self.admin, kind="update", title="Update", message="Update", recipients=[self.client_user])
        outbox = PushOutbox.objects.get(notification__event=event)
        error = RuntimeError("gone")
        error.response = Mock(status_code=410)
        result = deliver_pending(send=Mock(side_effect=error))
        self.assertEqual(result["removed"], 1)
        self.assertFalse(PushSubscription.objects.exists())
        outbox.refresh_from_db()
        self.assertIsNotNone(outbox.sent_at)

    def test_pywebpush_encrypts_and_signs_without_sending_to_a_provider(self):
        vapid = Vapid()
        vapid.generate_keys()
        raw_private = vapid.private_key.private_numbers().private_value.to_bytes(32, "big")
        private_value = b64url(raw_private)
        public_value = b64url(vapid.public_key.public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        ))
        browser_key = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        with patch.object(WebPusher, "as_curl", autospec=True, return_value="curl") as as_curl:
            result = webpush(
                {"endpoint": "https://fcm.googleapis.com/fcm/send/offline-test", "keys": {
                    "p256dh": b64url(browser_key), "auth": b64url(b"a" * 16),
                }},
                data=json.dumps({"title": "Design-Pot Logistics"}),
                vapid_private_key=private_value,
                vapid_claims={"sub": "mailto:ops@example.com"},
                ttl=86400,
                timeout=2,
                curl=True,
            )
        encoded_body = as_curl.call_args.kwargs["encoded_data"]
        headers = as_curl.call_args.kwargs["headers"]
        self.assertEqual(result, "curl")
        self.assertGreater(len(encoded_body), 16)
        self.assertIn("vapid ", headers["Authorization"].lower())
        self.assertEqual(len(public_value), 87)

    def test_client_booking_to_cod_settlement_end_to_end(self):
        from decimal import Decimal

        from django.utils import timezone

        from .workflow import (
            collect_cod,
            complete_delivery,
            confirm_quote,
            reconcile_cod,
            settle_reconciled_cod,
            transition_delivery,
        )

        pickup_zone = OperationalZone.objects.create(name="Release Pickup", active=True)
        destination_zone = OperationalZone.objects.create(name="Release Destination", active=True)
        resource = Resource.objects.create(
            user=self.rider,
            vehicle_type="bike",
            vehicle_name="Release bike",
            registration="V1-RELEASE-1",
            status="available",
        )
        booking = {
            "booking_key": str(uuid.uuid4()), "revision": 0,
            "pickup_address": "Shop by the old station", "pickup_instructions": "Call first",
            "pickup_landmark": "Blue gate", "pickup_google_maps_url": "",
            "destination_address": "House near the park", "destination_instructions": "Ring bell",
            "destination_landmark": "Large mango tree", "destination_google_maps_url": "",
            "recipient_name": "Ama Recipient", "recipient_phone": "+233200000001",
            "package_description": "Parcel", "instructions": "Keep upright",
            "vehicle_type": "bike", "service": "standard", "payment_method": "cod",
            "cod_amount": "45.00",
        }
        delivery = save_booking(actor=self.client_user, data=booking)
        self.assertIsNone(delivery.pickup.zone_id)
        self.assertIsNone(delivery.destination.zone_id)
        assign_zone(
            actor=self.admin, delivery_id=delivery.pk, side="pickup",
            zone_id=pickup_zone.pk, reason="Release test classification",
        )
        assign_zone(
            actor=self.admin, delivery_id=delivery.pk, side="destination",
            zone_id=destination_zone.pk, reason="Release test classification",
        )
        delivery.refresh_from_db()
        quote = manual_quote(
            actor=self.admin,
            delivery_id=delivery.pk,
            amount=Decimal("70.00"),
            reason="Confirmed V1 test rate",
            expected_revision=delivery.job.revision,
            request_key=uuid.uuid4(),
        )
        confirm_quote(actor=self.client_user, quote_id=quote.pk)
        run = runs.create_run(
            actor=self.admin,
            resource_id=resource.pk,
            operational_date=timezone.localdate(),
            delivery_ids=[delivery.pk],
            notes="Release journey",
            request_key=uuid.uuid4(),
        )
        runs.release_run(
            actor=self.admin, run_id=run.pk, expected_version=run.version, request_key=uuid.uuid4()
        )
        run.refresh_from_db()
        runs.start_run(
            actor=self.rider, run_id=run.pk, expected_version=run.version, request_key=uuid.uuid4()
        )
        for before, after in (
            ("assigned", "to_pickup"),
            ("to_pickup", "picked_up"),
            ("picked_up", "in_transit"),
        ):
            transition_delivery(
                actor=self.rider,
                delivery_id=delivery.pk,
                target=after,
                expected_status=before,
                request_key=uuid.uuid4(),
            )
        collect_cod(
            actor=self.rider,
            delivery_id=delivery.pk,
            request_key=uuid.uuid4(),
            amount="45.00",
            method="cash",
        )
        complete_delivery(
            actor=self.rider,
            delivery_id=delivery.pk,
            request_key=uuid.uuid4(),
            recipient_name="Ama Recipient",
        )
        reconciliation = reconcile_cod(
            actor=self.admin,
            delivery_id=delivery.pk,
            request_key=uuid.uuid4(),
            reference="V1-RECON-1",
        )
        settlement = settle_reconciled_cod(
            actor=self.admin,
            organization_id=self.org.pk,
            reconciliation_ids=[reconciliation.pk],
            request_key=uuid.uuid4(),
            method="Reported transfer",
            external_reference="V1-PAYOUT-1",
        )
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, "delivered")
        self.assertEqual(settlement.amount, Decimal("45.00"))
        self.assertEqual(delivery.ledger.filter(kind="cod_settled").count(), 1)

    def test_notification_unread_count_read_all_and_recipient_scope(self):
        emit_event(actor=self.admin, kind="test", title="Update", message="Update", recipients=[self.client_user])
        self.client.force_login(self.client_user)
        self.assertEqual(self.client.get(reverse("notification-state")).json()["unread"], 1)
        response = self.client.post(reverse("notifications-read-all"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get(reverse("notification-state")).json()["unread"], 0)
        self.assertEqual(DomainEvent.objects.filter(kind="notifications.read_all").count(), 1)
