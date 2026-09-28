from urllib.parse import parse_qs, urlsplit

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator

_http_url = URLValidator(schemes=["http", "https"])


def validate_google_maps_link(value):
    """Accept common Google Maps share links while rejecting redirector/lookalike hosts."""
    if not value:
        return
    try:
        _http_url(value)
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except (ValidationError, ValueError) as exc:
        raise ValidationError("Enter a valid Google Maps link using HTTP or HTTPS.") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 80, 443)
    ):
        raise ValidationError("Enter a safe Google Maps link.")

    short_link = host == "maps.app.goo.gl" and parsed.path not in ("", "/")
    legacy_short_link = host == "goo.gl" and parsed.path.startswith("/maps/")
    google_host = host == "google.com" or host.endswith(".google.com")
    maps_path = parsed.path.rstrip("/") == "/maps" or parsed.path.startswith("/maps/")
    query_keys = parse_qs(parsed.query).keys()
    root_map_query = parsed.path in ("", "/") and bool(
        {"q", "ll", "query", "daddr", "destination"}.intersection(query_keys)
    )
    if not (short_link or legacy_short_link or (google_host and (maps_path or root_map_query))):
        raise ValidationError("Use a Google Maps share link, such as maps.app.goo.gl or google.com/maps.")
