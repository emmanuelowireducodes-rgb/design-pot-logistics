# Phase 3: dispatch runs and stop sequencing

Phase 3 adds a manual dispatch workflow on top of Phase 2 booking and delivery records. Dispatch can group confirmed deliveries into a dated run, select a Rider or Driver and vehicle, review and reorder stops, optionally group remaining stops by the Design-Pot operating area, and release the run. The area grouping is a convenience sort only. The product does not claim route optimization, traffic-aware sequencing, distance, or ETA.

## Workflow

1. Admin opens **Dispatch** and creates a run for an eligible active Rider or Driver and vehicle.
2. Dispatch selects confirmed deliveries with matching vehicle requirements. Premium delivery windows must be on the selected operational date. A delivery may belong to only one live run.
3. The draft shows addresses, operating areas, recipients, phone numbers, delivery and payment details, and the current sequence. Dispatch can move individual stops, drag pending stops, or apply the area grouping as a suggested order. The manual controls remain available without drag support.
4. Releasing reserves that resource for the operational day and creates assignment history for newly assigned deliveries. Off-duty resources and schedule conflicts block release unless an Admin records an explicit reason for overriding the conflict. The Rider is notified only when the run is released.
5. The assigned Rider or Driver sees their run on the field dashboard. Starting it establishes the current Next Stop. Existing delivery status, unavailable-customer, proof-of-delivery, and cash-on-delivery workflows remain authoritative. Completing a delivery closes the stop through the normal POD process.
6. Dispatch may defer an unstarted/current stop, record that a customer is unavailable, retry a deferred stop at the next position or end, and append an eligible delivery to an active run. Deferred deliveries remain pending; a run cannot complete while any stop remains unresolved.

Draft runs can be cancelled with a reason. Released runs are not cancelled through the simple draft action because they may already own delivery assignment and schedule state; Dispatch must resolve them through the delivery workflow.

## Access and records

Admin dispatch pages require the Admin role. Rider/Driver pages are scoped to that account's assigned run. Delivery details linked from a run use the same assignment/run scope and existing customer privacy rules. Run and stop changes use version checks and request keys; the run, ordered stops, assignment history, availability reservation, and DomainEvent audit trail are stored in the application database. The database migration is `operations.0007_deliveryrun_runstop_and_more`.

The field view is responsive and provides text directions, address links, and phone links. Map destinations may use client supplied links already stored on the delivery. No Google Maps Platform key or service is required. Run data is not added to the service worker cache; private navigation continues to use the network.

## Verification

The feature test file `operations/test_phase3.py` covers run creation and release, delivery/resource eligibility, schedule overrides, role scope, persistent ordering and stale versions, deferred stops and retry, lifecycle ordering, POD completion, COD protections, cancellation/audit events, and bounded dispatch-list queries. JavaScript tests cover manual stop ordering and service-worker cache boundaries.

`manage.py check --deploy` warnings in a local development configuration are expected until production HTTPS, secret-key, cookie, HSTS, and `DEBUG` settings are configured. SQLite tests do not establish PostgreSQL row-lock concurrency behavior. Browser-based Admin desktop and Rider mobile visual checks are a release validation step when the local browser runner is available.

## Deliberate limits

- There is no turn-by-turn route optimizer or live location tracking.
- A run reserves the selected resource for the operating day; dispatch should use the recorded override only after reviewing the conflict.
- Delivery records currently represent the delivery workflow as a single stop per delivery. Separate pickup and drop-off waypoints and multi-delivery bulk jobs are not implemented here.
- Production PostgreSQL concurrency validation and visual browser validation must be run in their respective environments.
