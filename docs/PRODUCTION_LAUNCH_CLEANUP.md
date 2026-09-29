# Production launch cleanup

`prepare_production_launch` reports a proposed cleanup by default. It is a
management command, has no web route, and is not part of request handling.
Use a maintenance window for any future execution: the PostgreSQL execution
path takes exclusive locks on the operational and protected configuration
tables while it works.

## Scope and classification

The command uses the pre-launch assumption that **all rows in its listed
transactional scope are test activity**. It does not infer this from dates,
names, account roles, or record contents. A dry run reports the exact counts
before any execution can occur. Production execution remains a separate,
explicitly authorized step.

| Classification | Models and records | Default handling |
| --- | --- | --- |
| Transactional booking/dispatch | `Delivery`, `Job`, `BulkBooking`, `Quote`, `QuoteConfirmation`, `Assignment`, `StatusEvent`, `ProofOfDelivery`, `DeliveryRun`, `RunStop`, assignment and run `Availability` booking blocks, `RoutePlan`, `RouteStop`, `LocationUpdate` | Report and remove together |
| Financial transactions | all `LedgerEntry` kinds and `ClientSettlement` rows | Report by COD collection, reconciliation, settlement and delivery-fee kind; remove as one graph after integrity validation |
| Transaction notifications/audit | notifications attached to transaction `DomainEvent`s, their `PushOutbox` rows, transaction-family events, and run events | Remove together; unclassified delivery-linked events block execution |
| Preserve by default | `User`, `Organization`, `Resource`, `OperationalZone`, `PriceCard`, `FixedRate`, `PricingVersion`, `PricingConfiguration`, `Location`, `PushSubscription`, social identities/tokens/apps, email identities, sessions, migration history, `DailySequence`, `LoginAttempt`, non-booking availability and unlinked booking blocks | Report counts; never remove or edit |
| Manual review | unused or old `Location` rows and booking availability without a delivery or `run:` link | Preserve and report; there is no reliable test-data marker |

All Admin, Client, Rider and Driver users are retained, including disabled
accounts. Counts show role and active/disabled status. Resources and their
current duty state are retained. No user or resource deletion option is
provided. An active Admin must exist before execution. Price Cards, all
FixedRate versions, operational zones and pricing versions are retained even
if test transactions used them.

`Availability` blocks created for assignments (linked to a delivery) and run
reservations (`note` starts with `run:`) are transaction records. Non-booking
schedules and unlinked booking blocks are preserved because the command cannot
know whether they are launch setup. It does not force a Rider/Driver duty
status.

## Finance, notifications, and audit

The COD chain is checked as `expected COD amount -> collected entry ->
reconciliation entry -> optional settled entry -> ClientSettlement`. Collection
amount/currency must match the delivery, reconciliation must match the
collection's currency, and one collection may be reconciled in multiple
partial entries whose total cannot exceed the collection. Settled entries
must point to a matching reconciliation and client; settlement totals must
match their settled entries. Any inconsistency is shown by count and blocks
execution for manual review. There are no cached
Client COD balances; reporting derives them from these ledger rows.

The command removes only transaction-family `DomainEvent`s (`delivery.*`,
`quote.*`, `cod.*`, `bulk.*`, `run.*`, `sample.*`, and the delivery-scoped
`location.zone_corrected` event) plus `availability.created` events whose
payload identifies a booking block being removed. Account/security events
(`USER_*`, `account.*`, `session.*`, notifications read events), pricing and
zone configuration events, duty changes, and other unrelated audit events are
preserved. An unrecognized `DomainEvent` attached to a delivery blocks
execution; review the kind before changing the allowlist. Notifications for
preserved events remain. Outbox rows for removed notifications are removed by
the command and their normal foreign-key cascade.

Push subscriptions are device/account identity data, not transaction data, so
they are preserved. Google/social account links, tokens, email identities,
sessions, users, and password/account audit are also preserved.

The command does not delete filesystem uploads or other media. Any files
associated with removed transactions remain for separate manual review.

## Immutable history and transaction safety

The models reject instance-level edits/deletes for append-only history, and
PostgreSQL migrations install database triggers backed by
`dpot_reject_history_mutation()`. The command verifies the PostgreSQL function
and every expected history trigger before reporting or execution. Execution
temporarily disables only the named history triggers on rows it must remove,
inside one Django `transaction.atomic()` block; it restores each trigger's
original enabled mode before commit. It never disables all triggers. On an
error, a nested savepoint rolls back data work so the command can restore the
triggers; the outer transaction then rolls back both data and DDL. PostgreSQL
table locks serialize the cleanup against normal requests during execution.

The local SQLite migration path has a known trigger coverage gap for
`ProofOfDelivery` and `LedgerEntry`: SQLite table-rebuild operations in
migrations 0003 and 0008 discard those two earlier triggers. The command
recognizes only these two SQLite-only gaps; it does not recreate or leave
triggers changed. The PostgreSQL path does not accept missing/disabled guards.
The local tests verify that existing SQLite guards are restored and that the
preserved FixedRate guard still rejects updates. This cleanup adds no schema
migration.

## Commands and safeguards

The default is dry run, even if no option is supplied:

```powershell
$env:DPOT_ENV = "development"
$env:DPOT_POSTGRES = "1"
$env:DPOT_DEMO = "0"
python manage.py prepare_production_launch --dry-run
```

In this locally run command, development mode loads the git-ignored root
`.env` while `DPOT_POSTGRES=1` selects the configured PostgreSQL Session
Pooler. Do not use a Direct Connection host.

The command reports each proposed removal category, preserved configuration
and security counts, unclassified audit records, and COD graph integrity
status and a SHA-256 fingerprint of the exact candidate primary keys. Dry run
only reads database rows and never runs migrations, changes triggers, or writes
data. Keep the fingerprint with the reviewed report; execution refuses if the
candidate set has changed.

Execution is deliberately not part of the launch preparation task. If a
separate future instruction authorizes it, first confirm a current production
backup/recovery point in the Supabase dashboard and review all dry-run counts.
Then execute only in a maintenance window using the existing Design-Pot
Session Pooler environment:

```powershell
$env:DPOT_ENV = "development"
$env:DPOT_POSTGRES = "1"
$env:DPOT_DEMO = "0"
$plan = "<fingerprint copied from the reviewed dry-run report>"
python manage.py prepare_production_launch --execute --backup-confirmed --confirm-scope=DELETE-ALL-TRANSACTIONAL-DATA "--confirm-plan=$plan" --confirm-production
```

The scope and backup flags are both required, as is the exact prior dry-run
fingerprint. PostgreSQL execution requires `--confirm-production` even when
started from local development settings. There is no yes/no prompt as the only
guard. The
command refuses unclassified delivery-linked audit, inconsistent COD chains,
missing immutable PostgreSQL guards, or a database without an active Admin.
This documented command is not authorization to execute it.

## Before go-live checklist

1. Take and verify a recoverable production backup separately.
2. Run the management command with `--dry-run` using the Design-Pot Session
   Pooler environment. Do not use Direct Connection, another Supabase project,
   raw SQL, or a local database for production counts.
3. Have the business owner confirm that every displayed transactional row is
   pre-launch test activity and approve the scope separately.
4. Review retained Client/Rider/Driver accounts, organizations, resources,
   duty state, and unlinked availability manually. Remove test accounts or
   fleet records only through a separately reviewed process.
5. Review retained locations and all Price Cards, FixedRate history, zones,
   and pricing configuration manually. Do not delete pricing or zone data as
   part of this command.
6. Schedule the separately authorized command during a maintenance window.
7. Afterward, verify zero transactional rows in the report, intact history
   triggers, retained accounts/configuration, and run booking, quote, dispatch,
   COD reconciliation/settlement, notification and login smoke tests.

No production record is deleted by the launch-preparation dry run.
