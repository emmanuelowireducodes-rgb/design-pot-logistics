from django.conf import settings

ADMIN_NAV = [
    ("dashboard", "Overview", "◫"),
    ("deliveries", "Deliveries", "▤"),
    ("dispatch", "Dispatch", "⇄"),
    ("calendar", "Calendar", "▦"),
    ("fleet", "Riders & drivers", "♧"),
    ("clients", "Clients", "◉"),
    ("live", "Live operations", "◎"),
    ("finance", "Finance", "▣"),
    ("notifications", "Notifications", "♧"),
    ("reports", "Reports", "▥"),
    ("pricing", "Pricing", "≡"),
    ("settings", "Settings", "⚙"),
]
CLIENT_NAV = [
    ("dashboard", "Overview", "◫"),
    ("booking", "Create delivery", "+"),
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
    return {
        "csp_nonce": getattr(request, "csp_nonce", ""),
        "demo_mode": settings.DEMO_MODE,
        "navigation": [
            {"key": k, "label": label, "icon": icon, "url": f"/{prefix}/{k}/"}
            for k, label, icon in nav
        ],
        "portal_label": {
            "admin": "Operations workspace",
            "client": "Client workspace",
            "rider": "Rider workspace",
            "driver": "Driver workspace",
        }.get(role),
        "field_mode": role in ("rider", "driver"),
    }
