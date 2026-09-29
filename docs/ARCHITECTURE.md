# Phase 1 decisions

The repository was empty. This implementation uses a modular Django 5.2 LTS monolith, server-rendered templates, native browser controls, a small progressive-enhancement script and plain CSS. Python 3.12 is the development runtime. PostgreSQL is the production database; SQLite exists for development and tests only. No API vendor is required to run Phase 1.

Django supplies password hashing, database sessions, CSRF, HTML escaping and migrations. A custom user exists from the initial migration, as recommended by [Django's authentication documentation](https://docs.djangoproject.com/en/5.2/topics/auth/customizing/). Production configuration follows the [deployment checklist](https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/).

## Boundaries

- `config/`: environment, middleware, routes, deployment entrypoint.
- `operations/models.py`: normalized records and database invariants.
- `operations/access.py`: role checks and organization/current-assignment query scoping.
- `operations/services.py`: transactional operations and integration protocols. Future JSON endpoints and mobile clients should reuse this layer.
- `operations/views.py`: server-side HTTP adapters and paginated read models.
- `templates/`, `static/`: shared shell, reusable UI partials and design tokens.
- `operations/management/`: explicit local fixtures and trusted account provisioning.

Four roles are supported. Admin/Dispatch is one role in this phase; Rider and Driver share a mobile workspace but retain separate account roles. Every workspace request checks its role on the server. Clients see only deliveries belonging to their organization; field users see only the current assignment. Inaccessible record IDs return 404. Django superuser/staff flags do not bypass the product's role boundaries. There is no public signup or role-changing endpoint.

## Data relationships

Organization → users and jobs → deliveries. Delivery snapshots recipient details and references immutable structured pickup/destination locations. A job is the grouping boundary for future bulk quotes. Quotes reference immutable pricing versions; quote breakdown JSON will hold per-package allocations and route assumptions. Confirmation is a separate immutable record, unique per job and quote. A changed price always creates another quote.

Resource is a person with their associated vehicle, never a separately schedulable vehicle. Availability belongs to that person. The reservation service locks the resource row on PostgreSQL before checking overlaps; adjacent intervals are allowed. No override endpoint exists. All future schedule writers must use the service. SQLite does not prove concurrent locking behavior.

Assignments are append-only and use a monotonically increasing key so identical timestamps cannot grant access to the previous assignee. The latest assignment is authoritative. Status events, assignment vehicle snapshots, quote records, proof records, ledger entries, locations and domain events preserve history. Database triggers reject UPDATE/DELETE of these tables, including queryset bypasses. Triggers have SQLite and PostgreSQL implementations. Corrections should be new events; a formal finance reversal workflow is deferred.

Delivery.reference is unique. Seed records use DPT-YYMMDD-XXXX. Transaction-safe daily sequence allocation for real booking belongs to Phase 2; there is no real delivery creation endpoint yet. Current status is a read projection; a future transition service must append history and update the projection atomically.

COD is separate from delivery fees. The ledger stores collections, reconciliations, settlements and delivery fees as independently auditable entries with idempotency keys. The current overview assumes GHS and shows only GHS entries. Settlement/reversal rules and any multi-currency support are not yet implemented. Sample dashboard amounts are illustrative product collections, not business delivery rates.

## Events and notifications

`emit_event` appends an immutable event, creates recipient-scoped in-app notifications and (when VAPID is configured) transactional Web Push outbox rows in the same transaction. Read and delete actions are recipient-scoped POSTs with CSRF protection; reads append audit events, while notification deletion leaves the append-only event and all operational records intact. The one-shot `send_web_push` command must be invoked by a recurring worker/scheduler; the Render Free Gunicorn service does not process it automatically. External calls never run inside the domain transaction.

## Integration seams

`RoadRoutingProvider` accepts a current origin, remaining stops and cache key. Route plans store origin, provider, expiration and ordered stops with road distance/time. No straight-line optimizer or fabricated ETA is implemented. Scheduling windows are enforced for Premium; Standard has no client-selected window.

Live location records include capture time and accuracy with a resource/time index. There is no GPS endpoint or browser location permission in Phase 1. A later API/native wrapper can reuse the same services. Tracking must consider movement, battery, retention and stale locations; browser background tracking is not promised.

POD supports a recipient and note initially. Media upload and OTP verification are intentionally unavailable. Future uploads require private object storage, size/type validation and scoped download URLs.

## Performance and security

Delivery lists are paginated to 20 rows with joined locations/organizations. Dashboard data is bounded. Compound indexes support status/time, organization via job, Premium windows, availability, notification inboxes and location history. No third-party font, map or CDN dependency runs in the browser. Authenticated responses are private/no-store.

Production startup rejects missing/short secrets and demo mode. Cookies use Secure/HttpOnly/SameSite, HTTPS redirect and HSTS; CSP disallows inline scripts and third-party execution. Login attempts are database-backed and keyed by connection IP, limited to 10 per 15 minutes. This is a baseline, not a substitute for edge rate limiting; shared proxy IPs require trusted proxy configuration before launch. Old attempt rows need an operational retention job.

Accounts are provisioned by a trusted management command. Recovery uses the trusted Django changepassword command for now. Public invitations, self-service password recovery, MFA and session revocation UI are follow-up identity work. Do not expose development DEBUG mode publicly.

## Decisions awaiting product approval

GHS and Africa/Accra are current defaults. Confirm admin/dispatcher permission separation, quote expiry, real bike/car/Premium rates, COD settlement rules, POD policy, location retention and any schedule override policy before implementing those workflows. PostgreSQL hosting and deployment services remain unselected. No paid service or external deployment was created.
