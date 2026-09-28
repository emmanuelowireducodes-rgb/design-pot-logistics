# Phase 4 Finance and Reporting

## Money definitions

- **Delivery revenue** is the confirmed quote amount and currency for a delivered booking, recognized at its latest stored `delivered` status event. Current price cards are never used to rewrite historical amounts. Only GHS confirmations are included in GHS totals.
- **COD expected** is the amount on the booking. It is not revenue.
- **COD collected** is the immutable collection ledger amount, counted at its ledger timestamp and attributed to the person who recorded collection.
- **COD outstanding for reconciliation** is collected COD less the reconciliation ledger amount, using the collection-time cohort for the dashboard and Rider position.
- **COD reconciled** is the immutable Admin reconciliation ledger amount, counted at reconciliation time on the dashboard.
- **COD pending Client settlement** is reconciled COD less linked settlement ledger entries, for the reconciliation-time cohort.
- **COD settled** is the amount of Admin-recorded client settlement batches, counted at settlement-record time. Recording a settlement does not initiate or independently verify a transfer.

All calculations use Decimal amounts in GHS. Delivery charges and product COD stay separate. Client views and settlement lists are scoped by organization; Admin finance pages and exports are role protected. Financial and authenticated pages are network-only in the service worker.

## Controls and limits

Settlement batches link whole reconciled COD entries for one organization. Each reconciliation can appear in only one settlement due to a database one-to-one constraint. Settlement and ledger history is append-only with database triggers on SQLite and PostgreSQL. Reconciliation continues to require an exact expected collection; differences remain visible and no automatic write-off is provided.

Settlement channel text is optional and descriptive. The application does not move funds. Partial item settlement, cancellation/reversal, corrections, deductions from COD, and discrepancy approval are not implemented because the corresponding operating policies require management decisions.

## Date and report behavior

Date ranges use the configured local timezone and half-open day boundaries. Revenue uses delivery completion time, collections use collection ledger time, reconciliations use reconciliation ledger time, and settlements use settlement creation time. Client activity counts bookings by booking creation date; its completed fee totals use completion time. Delivery status reports filter by creation date and show the current lifecycle state. Run reports use operational date and stored stop statuses; they make no distance, ETA, fuel, or route-efficiency claims.

CSV reports apply the same authorization and filters as their page. Text cells beginning with spreadsheet formula characters are prefixed with an apostrophe.
