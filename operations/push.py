"""Authenticated browser subscription endpoints and asynchronous Web Push delivery."""

import base64
import binascii
import json
import re
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from .models import PushOutbox, PushSubscription

_B64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_PUSH_HOSTS = (
    "fcm.googleapis.com",
    "push.services.mozilla.com",
    "push-service.mozilla.org",
    "push.apple.com",
    "notify.windows.com",
)


def _decode_key(value, expected_length):
    if not isinstance(value, str) or not _B64URL.fullmatch(value):
        raise ValidationError("The browser subscription key is invalid.")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error) as exc:
        raise ValidationError("The browser subscription key is invalid.") from exc
    if len(decoded) != expected_length:
        raise ValidationError("The browser subscription key has an invalid length.")
    return value


def validate_subscription(value):
    if not isinstance(value, dict) or set(value) - {"endpoint", "expirationTime", "keys"}:
        raise ValidationError("The browser subscription is invalid.")
    endpoint = value.get("endpoint")
    if not isinstance(endpoint, str) or len(endpoint) > 2048:
        raise ValidationError("The browser subscription endpoint is invalid.")
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as exc:
        raise ValidationError("The browser subscription endpoint is invalid.") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username
        or parsed.password
        or port not in (None, 443)
        or not any(host == suffix or host.endswith("." + suffix) for suffix in _PUSH_HOSTS)
        or not parsed.path.startswith("/")
    ):
        raise ValidationError("This browser push service is not supported.")
    keys = value.get("keys")
    if not isinstance(keys, dict) or set(keys) != {"p256dh", "auth"}:
        raise ValidationError("The browser subscription keys are invalid.")
    return endpoint, _decode_key(keys["p256dh"], 65), _decode_key(keys["auth"], 16)


@require_http_methods(["GET", "POST", "DELETE"])
def subscription(request):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Authentication required."}, status=401)
    if not settings.WEB_PUSH_ENABLED:
        return JsonResponse({"error": "Push notifications are not configured."}, status=503)
    if request.method == "GET":
        return JsonResponse(
            {"enabled": PushSubscription.objects.filter(user=request.user).exists()}
        )
    try:
        data = json.loads(request.body or b"{}")
        if request.method == "DELETE":
            endpoint = data.get("endpoint") if isinstance(data, dict) else None
            if not isinstance(endpoint, str) or len(endpoint) > 2048:
                raise ValidationError("Choose a valid subscription.")
            PushSubscription.objects.filter(user=request.user, endpoint=endpoint).delete()
            return JsonResponse({"enabled": False})
        endpoint, p256dh, auth = validate_subscription(data)
        with transaction.atomic():
            existing = PushSubscription.objects.select_for_update().filter(endpoint=endpoint).first()
            if existing and existing.user_id != request.user.pk:
                raise PermissionDenied
            PushSubscription.objects.update_or_create(
                endpoint=endpoint,
                defaults={"user": request.user, "p256dh": p256dh, "auth": auth},
            )
        return JsonResponse({"enabled": True}, status=201)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as exc:
        return JsonResponse({"error": str(exc)[:160]}, status=400)


def deliver_pending(*, send=None, limit=100):
    """Deliver committed notification outbox rows; push errors never touch domain transactions."""
    if send is None:
        from pywebpush import webpush

        send = webpush
    pending = list(
        PushOutbox.objects.filter(sent_at__isnull=True, attempts__lt=6)
        .select_related("notification__recipient")
        .order_by("created_at")[:limit]
    )
    sent = failed = removed = 0
    for item in pending:
        notification = item.notification
        payload = json.dumps(
            {
                "title": "Design-Pot Logistics",
                "body": "You have a new update. Open the app to view it.",
                "url": reverse("notification-open", kwargs={"notification_id": notification.pk}),
                "tag": f"notification-{notification.pk}",
            }
        )
        subscriptions = list(PushSubscription.objects.filter(user_id=notification.recipient_id))
        item_failed = False
        for device in subscriptions:
            try:
                send(
                    {"endpoint": device.endpoint, "keys": {"p256dh": device.p256dh, "auth": device.auth}},
                    data=payload,
                    vapid_private_key=settings.VAPID_PRIVATE_KEY,
                    vapid_claims={"sub": settings.VAPID_SUBJECT},
                    ttl=86400,
                    timeout=5,
                )
                sent += 1
            except Exception as exc:  # Push failure is isolated from application work.
                response = getattr(exc, "response", None)
                code = getattr(response, "status_code", None)
                if code in (404, 410):
                    device.delete()
                    removed += 1
                else:
                    item_failed = True
                    failed += 1
        item.attempts += 1
        if not subscriptions or not item_failed or item.attempts >= 6:
            from django.utils import timezone

            item.sent_at = timezone.now()
        item.last_error = "provider delivery failed" if item_failed else ""
        item.save(update_fields=["attempts", "sent_at", "last_error"])
    return {"queued": len(pending), "sent": sent, "failed": failed, "removed": removed}
