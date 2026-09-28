# Phase 2: manually dispatched delivery workflow

The Django architecture, roles, design tokens and Phase 1 tests are preserved. This release connects single-delivery booking, quote review, confirmation, manual dispatch, rider/driver progress, photo POD, COD collection/reconciliation and client tracking to the same database.

V1's Phase 2.5 location decision is manual: clients choose active Design-Pot operational zones, enter written addresses and may supply Google Maps share links. No Google Maps Platform API or billing is required. See [V1 location and navigation](PHASE25_V1_LOCATION.md).

Fixed prices are managed as simple route rows on the General or Client Price Card: From, To, Bike, and Car. New standard prices apply in both directions by default; less common service and effective-date controls are under Advanced Options. Price changes create immutable versions, so confirmed quote snapshots do not change. Client-card prices take precedence for that client, with the general card used according to the configured pricing mode. See the Admin Pricing link for card and operational area management.

## Getting started

Install the updated lockfile and apply migrations:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py collectstatic --noinput
.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000
```

For the existing isolated sample database, set `$env:DPOT_DEMO="1"` before migrating/running and use port 8001. Do not seed again into a populated database. Existing sample records remain historical examples; create a new delivery to exercise the full new workflow. The generated local demo credentials are in ignored `artifacts/demo-access.txt`.

## Routes and workflow

- Client: `/client/booking/` creates a draft with structured address, area and instructions. No provider ID or coordinates are invented.
- Quote: `/client/quotes/<uuid>/` shows a versioned delivery fee, COD separately, expiry, edit and confirm actions.
- Confirmation freezes the quote, allocates a DPT-YYMMDD-XXXX reference and notifies the client organization and dispatch.
- Queue: `/admin/deliveries/`, with status groups, search, service/vehicle filters and pagination. Drafts without a friendly reference are hidden from dispatch.
- Dispatch: `/admin/deliveries/<uuid>/assign/` reserves an eligible person, checks vehicle/role and time conflicts, and supports an explicit override with a reason.
- Calendar: `/admin/calendar/` shows a day grouped by person and associated vehicle. `/admin/availability/` records breaks, off-duty periods, maintenance and unavailability.
- Field: `/field/dashboard/` shows the next assignment in scheduled/creation order, remaining work and exceptions. It is not an optimized route.
- Delivery: `/deliveries/<uuid>/` shows scoped details, fee, assignment, timeline, POD and COD; internal audit/notifications are admin-only.
- COD: `/<admin|client>/finance/` shows organization-scoped collections and reconciliation. Admin reconciles from each delivery.
- Pricing: `/admin/pricing/` publishes a new immutable **development** pricing version.

Normal transitions are Confirmed → Assigned → En route to pickup → Picked up → In transit → Delivered. Only the current assignee progresses work. Customer unavailable and Report issue require notes, keep the delivery unfinished, and notify dispatch. Resume returns to the recorded previous active state, rather than skipping steps. Dispatch can resume an exception but cannot bypass POD completion.

Assignment changes are allowed before pickup begins. Active journeys must first be resolved operationally; this phase does not support mid-journey handoffs, cancellations, returns or financial reversals.

## Development pricing: explicit limitations

Initial example GHS settings: bike base/minimum 20, car base/minimum 40, bike per-km 2, car per-km 3, Premium surcharge 10, round up to nearest 1, quote validity 30 minutes. **These are development placeholders, not approved business rates.**

A distance-provider protocol is isolated from the pricing service. The Phase 2 implementation returns unavailable; per-km settings are stored but no road-distance charge is calculated. No precise distance or ETA is fabricated. The formula is max(minimum, base + known distance charge) + Premium surcharge, rounded upward. Current production settings disable development-priced booking unless `DPOT_ALLOW_DEVELOPMENT_PRICING=1` is explicitly set; the UI warning remains even when enabled.

Confirmed quotes never change with new pricing. Editing an unconfirmed draft replaces its current quote, records a new revision and makes the prior quote ineligible for confirmation. Stale edits and expired quotes are rejected.

## Database changes

Migration 0003 adds draft reference support, booking idempotency keys/revisions/current quote, nullable paired coordinates, assignment windows and override reasons, block release metadata, POD policy/photo/hash, payment method/reference/note, previous status and event idempotency keys. It adds the daily reference allocator and active pricing-version selector, plus operational indexes.

Migration 0004 adds ordered status sequences and calendar release reasons. Legacy event sequences are backfilled without rewriting their existing content; where Phase 1 did not record a previous state, that value remains unknown. Existing immutable-table triggers are temporarily removed and restored inside migrations because SQLite table reconstruction can otherwise discard them.

New state changes lock the relevant job/delivery/resource rows on PostgreSQL and append history, events and in-app notifications in the same transaction. Duplicate actions use unique request keys; confirmed quotes and financial collections cannot be silently replaced. SQLite is development-only and does not verify PostgreSQL row-lock behavior.

## Proof of delivery

Default Standard/Premium policy requires recipient name and photo. Policy is copied onto each draft and preserved at confirmation; changing defaults does not alter an existing delivery. A service can support future OTP/signature policy fields without making them available prematurely.

Uploads accept JPEG/PNG only, up to 5 MiB and 16 megapixels. The upload handler stops oversized streams. Pillow decodes/verifies files, strips metadata and trailing data by re-encoding to JPEG, and downsizes to at most 1600×1600. Names are randomized; hashes and the applied policy are recorded on immutable POD records.

Files live in `private_media/`, never in static assets. There is no public media route or storage URL. `/deliveries/<uuid>/proof/` authorizes the request, serves an attachment as JPEG, and sets private/no-store and nosniff. File writes are cleaned up when completion fails. A process crash between file write and database commit can leave an orphan; add retention/orphan cleanup and backups for a production host.

Set `DPOT_PRIVATE_MEDIA_ROOT` to durable private storage on the deployment host. Multi-instance deployment needs shared private storage or a private object-storage adapter, not independent instance disks.

The upload design follows [Django file guidance](https://docs.djangoproject.com/en/5.2/ref/models/fields/) and [Pillow security guidance](https://pillow.readthedocs.io/en/stable/handbook/security.html).

## COD and notifications

Expected COD is the product value, separate from the quote. Current assignees record the exact expected amount during In transit using cash or mobile money; mobile money needs a reference. Partial/excess collections are rejected and should be reported as an issue. Completion requires the expected COD to be recorded.

Admins reconcile recorded collections with a reference and optional internal note. Expected, Collected and Reconciled are supported. Settlement, refunds, reversals and partial reconciliation are deliberately unavailable. Ledger records remain immutable and reconciliation records its source collection.

In-app notifications cover confirmation, assignment/reassignment, progress, pickup, exceptions, completion, COD awaiting reconciliation and reconciliation. Old assignees are informed when an assignment changes. Client notifications avoid internal reasons, and client timeline/finance views omit staff-only notes. SMS, WhatsApp, maps, GPS and automatic dispatch are not integrated.

## Quality verification

See [the completion and verification report](PHASE2_VERIFICATION.md) for executed checks and environmental limits. The final suite passes 57 tests (26 Phase 1 and 31 Phase 2). Coverage includes the complete HTTP/database journey, organization and assignment isolation, quote locking, duplicate actions, scheduling conflicts/overrides, status transitions/exceptions, secure POD, COD, immutable records and constant-query operational lists. Existing Phase 1 tests are retained unchanged.

PostgreSQL must be tested on a real PostgreSQL service before launch. The repository's CI workflow is configured for PostgreSQL, but configuration alone is not evidence of execution.

## Decisions before business use / Phase 3

Approve actual GHS pricing and the road-distance provider, COD handling/reversal/settlement rules, photo retention and hosting/storage. Confirm the policy that assignments cannot change after pickup starts, override governance, the daily four-digit ID capacity (9,999), and the exact-amount COD rule. Phase 3 should start with production validation and then approved road-distance pricing/routing; no Phase 3 work is started automatically.
