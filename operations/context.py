from django.conf import settings
from django.urls import reverse

from .social_auth import google_oauth_configured

ADMIN_NAV = [
    ("dashboard", "Overview", "◫"),
    ("deliveries", "Deliveries", "▤"),
    ("dispatch", "Dispatch", "⇄"),
    ("calendar", "Calendar", "▦"),
    ("fleet", "Riders & drivers", "♧"),
    ("users", "User Management", "◎"),
    ("clients", "Clients", "◉"),
    ("finance", "Finance", "▣"),
    ("notifications", "Notifications", "♧"),
    ("reports", "Reports", "▥"),
    ("pricing", "Pricing", "≡"),
    ("settings", "Settings", "⚙"),
]
CLIENT_NAV = [
    ("dashboard", "Overview", "◫"),
    ("booking", "New Delivery", "+"),
    ("bulk-booking", "Bulk Delivery", "▦"),
    ("deliveries", "My deliveries", "▤"),
    ("finance", "Finance", "▣"),
    ("notifications", "Notifications", "♧"),
    ("settings", "Account", "⚙"),
]
FIELD_NAV = [
    ("dashboard", "My day", "◫"),
    ("deliveries", "My stops", "▤"),
    ("notifications", "Updates", "♧"),
    ("settings", "Profile", "⚙"),
]


def shell(request):
    user = request.user
    role = user.role if user.is_authenticated else ""
    nav = ADMIN_NAV if role == "admin" else CLIENT_NAV if role == "client" else FIELD_NAV
    prefix = "admin" if role == "admin" else "client" if role == "client" else "field"
    unread_count = 0
    notification_cursor = ""
    if user.is_authenticated:
        from .models import Notification

        unread_count = Notification.objects.filter(recipient=user, read_at__isnull=True).count()
        cursor_date = (
            Notification.objects.filter(recipient=user)
            .order_by("-created_at")
            .values_list("created_at", flat=True)
            .first()
        )
        notification_cursor = cursor_date.isoformat() if cursor_date else ""
    return {
        "csp_nonce": getattr(request, "csp_nonce", ""),
        "demo_mode": settings.DEMO_MODE,
        "google_oauth_enabled": google_oauth_configured(),
        "navigation": [
            {
                "key": k,
                "label": label,
                "icon": icon,
                "url": reverse("bulk-booking") if role == "client" and k == "bulk-booking"
                else f"/{prefix}/{k}/",
            }
            for k, label, icon in nav
        ],
        "portal_label": {
            "admin": "Operations workspace",
            "client": "Client workspace",
            "rider": "Rider workspace",
            "driver": "Driver workspace",
        }.get(role),
        "field_mode": role in ("rider", "driver"),
        "notification_unread_count": unread_count,
        "notification_cursor": notification_cursor,
        "web_push_enabled": settings.WEB_PUSH_ENABLED,
    }
