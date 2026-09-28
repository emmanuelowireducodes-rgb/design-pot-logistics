"""Provider-neutral road-routing seam. V1 deliberately has no live provider."""

import hashlib
import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Protocol

from django.conf import settings
from django.core.cache import cache


class MapUnavailable(Exception):
    pass


@dataclass(frozen=True)
class RoadRoute:
    metres: int
    seconds: int
    polyline: str
    provider: str = "future-provider"


class RoadRoutingProvider(Protocol):
    def route(self, origin: tuple, destination: tuple, vehicle: str) -> RoadRoute: ...


def road_route(origin, destination, vehicle="car", provider=None):
    """Short-lived, directional cache for a future explicitly supplied provider."""
    if provider is None:
        raise MapUnavailable("Road routing is not available in this release.")
    raw = json.dumps([str(v) for v in (*origin, *destination)] + [vehicle, type(provider).__name__])
    key = "road-v1:" + hashlib.sha256(raw.encode()).hexdigest()
    saved = cache.get(key)
    if saved:
        return RoadRoute(**saved)
    if not cache.add(key + ":busy", True, 10):
        raise MapUnavailable("Route calculation is in progress. Please retry shortly.")
    try:
        result = provider.route(origin, destination, vehicle)
        cache.set(key, asdict(result), settings.ROAD_ROUTE_CACHE_SECONDS)
        return result
    finally:
        cache.delete(key + ":busy")


class RoadDistance:
    def __init__(self, vehicle="car", provider=None):
        self.vehicle = vehicle
        self.provider = provider

    def road_kilometres(self, pickup, destination):
        if (
            pickup.latitude is None
            or destination.latitude is None
            or self.provider is None
        ):
            return None
        route = road_route(
            (pickup.latitude, pickup.longitude),
            (destination.latitude, destination.longitude),
            self.vehicle,
            provider=self.provider,
        )
        return Decimal(route.metres) / 1000
