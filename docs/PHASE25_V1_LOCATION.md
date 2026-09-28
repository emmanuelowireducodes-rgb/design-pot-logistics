# Phase 2.5 V1 location decision

Management has removed Google Maps Platform API services from the V1 runtime. V1 does not require Google Cloud billing, API credentials, Maps JavaScript, Places API, or Routes API.

## Client booking

For pickup and destination, the client selects an active Design-Pot operational zone, enters the area/locality and written address, and can add a Google Maps share link, landmark, and location-specific delivery instructions. The share link is client-provided and stored as entered (after trimming surrounding whitespace). Design-Pot does not fetch, expand, geocode, or otherwise resolve it. Coordinates and provider place IDs remain optional legacy/future fields; the V1 form does not request them or create them.

Links are restricted to HTTP/HTTPS Google Maps URLs, including `google.com/maps`, common `maps.google.com` query links, `maps.app.goo.gl` share links, and legacy `goo.gl/maps` links. They are rendered as escaped link attributes and opened with `noopener noreferrer`. Other domains, unsafe schemes, credentials, and nonstandard ports are rejected.

The operational zone is selected from active server-side `OperationalZone` records and is the source for fixed price-card matching. Area text and written addresses do not determine price. Missing/inactive zones cannot be submitted as valid zone choices; if no matching rate or road distance exists, the resolver requests an Admin quote.

## Rider / Driver

Delivery details show pickup and destination zones, written addresses, landmarks, and instructions. The next-stop screen displays the current stop prominently, provides a large **Navigate in Google Maps** action only when that location has a client-supplied link, and provides a call-customer action. With no link, the screen keeps the written directions visible and explains how to contact dispatch/customer. Google Maps or the device browser performs navigation. Design-Pot no longer renders an embedded map, asks for GPS, generates navigation URLs from coordinates, computes routes, or displays an ETA.

## Pricing and routing

General Fixed Price Cards and client-specific cards remain available and match using Design-Pot origin/destination zones, vehicle, service, dates, and direction rules. They do not need coordinates or a map provider. Distance-based pricing requires a real road distance; when no routing provider is configured, the resolver creates no estimated distance or distance-based quote and requests Admin review through the established workflow. A provider-neutral routing protocol/cache seam remains dormant for a later provider decision. Automatic route optimization remains out of scope.

## Configuration and verification

No `GOOGLE_MAPS_BROWSER_KEY`, `GOOGLE_MAPS_SERVER_KEY`, `GOOGLE_ROUTES_SERVER_KEY`, or bike-mode variable is read. `.env.example` documents that no Maps Platform credentials or billing are needed. Only the generic `ROAD_ROUTE_CACHE_SECONDS` remains for a future explicitly configured route provider.

The PWA manifest, install prompt, service worker, static-asset cache, no-store authenticated responses, and offline page remain active. The service worker does not cache authenticated records, API/write requests, or external links.

Migration `0006_client_maps_links` adds the optional validated Maps share URL to structured locations. Existing nullable coordinates, provider IDs, operational zones, immutable quote snapshots, pricing cards, and confirmed quote history are preserved.

Run the Django and JavaScript suites, Ruff, Mypy, migration consistency, and system checks before release. Browser testing should confirm the client manual-location flow, role-scoped location display, and external link behavior at desktop and mobile sizes. PostgreSQL must be tested on a real PostgreSQL service before production; SQLite does not verify PostgreSQL locking behavior.
