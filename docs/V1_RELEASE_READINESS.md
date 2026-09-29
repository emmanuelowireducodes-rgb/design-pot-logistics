# Design-Pot Logistics V1 release readiness

## Current release status

The Phase 4 baseline is commit `aabcfc447b584462eaf286eedf66663ac5df3966`. This release-candidate sprint preserves that history and adds mobile navigation, simpler client location entry, account-level Web Push subscriptions and a push outbox. PostgreSQL, public HTTPS delivery and real-device push still require environment verification before production use.

The local release gate passed 134 Django tests, 8 JavaScript tests, Ruff, Mypy, Django system checks, migration consistency, static collection and read-only SQLite integrity checks. The integrated test follows a written-address booking through Admin zone classification/manual quote, Client confirmation, Rider run and proof of delivery, exact COD collection, Admin reconciliation and recorded settlement. The original release candidate received browser verification for the Client, Rider and Admin layouts with no console exceptions, failed requests or horizontal overflow. The additional role dashboards and mobile table layouts in the previous follow-up passed automated/template checks, but could not be rechecked in a browser because Edge headless failed to start. This final sprint also remains visually unverified: no `agent-browser` executable is available in this workspace. `check --deploy` reports six development-environment warnings; configure and rerun it in production before release.

The follow-up improves the Client phone dashboard around booking and active deliveries, gives Admin a “Needs attention” phone view, promotes the Rider's Next Stop above run statistics, adds mobile disclosure filters and card-style financial/dispatch tables, and adds booking submission feedback. These changes do not add a new product phase. Recheck all final-sprint phone layouts, Google onboarding, pending/approved rider screens, Admin approval and the updated brand assets in a supported browser before company rollout.

## Run locally

Follow the PowerShell steps in the root README. Install `requirements-dev.lock`, migrate, collect static files, provision a private test account and run `manage.py runserver`. The local SQLite database is for development only. `DPOT_DEMO=1` uses the separate, safety-checked `demo.sqlite3` sample workspace; never seed the company database.

Local development optionally loads the ignored repository-root `.env` using `python-dotenv`; existing process environment variables take precedence. Production mode is selected from `DPOT_ENV` in the real process environment and skips `.env` entirely. `.env.example` contains safe placeholders only. Keep credentials and private media outside Git.

## Production setup

- Use PostgreSQL and a dedicated database role. Back up the database and test restore procedures before launch; V1 no longer accepts or serves POD images.
- Set `DPOT_ENV=production`, `DPOT_DEMO=0`, a random `DJANGO_SECRET_KEY` of at least 50 characters, explicit `DJANGO_ALLOWED_HOSTS`, and `DJANGO_CSRF_TRUSTED_ORIGINS` for HTTPS origins as required.
- Set `PGDATABASE`, `PGUSER`, `PGPASSWORD`, `PGHOST`, `PGPORT`, and `PGSSLMODE=require`. Apply migrations using a deployment/migration role, then run static collection and start the WSGI application behind a correctly configured HTTPS proxy.
- Production turns on HTTPS redirects and secure session/CSRF cookies. Configure `DPOT_TRUST_PROXY_SSL_HEADER=1` only when the trusted edge proxy overwrites `X-Forwarded-Proto` and strips client-supplied values.
- HSTS is deliberately disabled until the domain is verified on HTTPS. After confirming every affected hostname and subdomain is HTTPS-only, set `DPOT_ENABLE_HSTS=1` and the chosen `DPOT_HSTS_SECONDS`; set subdomain/preload options only after separate review. Run `manage.py check --deploy` against the final deployment environment.
- Configure rate limiting, monitoring, error reporting, log rotation, database/media backup and a production WSGI server at the selected host. Django's development server is not a production server.

## Web Push setup and limits

Web Push uses the browser Push API, the existing root-scope service worker, VAPID and `pywebpush`. `WEB_PUSH_ENABLED` is true only when all three process environment variables `DPOT_VAPID_PUBLIC_KEY`, `DPOT_VAPID_PRIVATE_KEY`, and `DPOT_VAPID_SUBJECT` are set. Set them to a matching key pair and contact address. The private key is URL-safe base64 without padding, not a PEM file. Generate it with the project's installed `py-vapid` package and place the output only in your deployment secret store:

```python
import base64
from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid

encode = lambda value: base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")
key = Vapid()
key.generate_keys()
private = key.private_key.private_numbers().private_value.to_bytes(32, "big")
public = key.public_key.public_bytes(
    serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
)
print("DPOT_VAPID_PUBLIC_KEY=" + encode(public))
print("DPOT_VAPID_PRIVATE_KEY=" + encode(private))
```

Keep the private output in a secret manager; never paste it into `.env.example`, source, logs or this guide. Use one stable key pair per deployment and all workers. Delivery uses a transactional outbox; run `python manage.py send_web_push` regularly to process it. This command is a one-shot batch processor, not a continuously running worker. The current Render Free Gunicorn service does not invoke it or provide a configured scheduler, so production has no reliable outbox processor until an operator configures an approved recurring runner. Each invocation retries transient failures and stops retrying a row after six attempts; expired 404/410 endpoints are removed. Domain actions never call push providers synchronously.

The app requests permission only after the user chooses **Turn on notifications** in Account. HTTPS is required outside localhost. Notifications show generic text; opening one first verifies the recipient and marks it read with a CSRF-protected POST, then navigates to the relevant existing authorized delivery, run or workspace page. The notification center also supports separate Mark Read, Mark All Read, Delete and confirmed Clear All actions. Deletion removes only the recipient's Notification rows; the related append-only domain events and logistics records remain. Deleting also removes its pending outbox row. A system notification already delivered to a phone cannot be remotely recalled; clicking a stale notification safely returns to the user's notification center.

The Web Push display options do not set `silent` or otherwise suppress normal OS alerts. The short optional foreground tone only operates while the page is open and is user-muteable. Design-Pot cannot guarantee a sound or custom sound: Android/iOS/browser settings, notification channels, Focus/Do Not Disturb and device silent mode control presentation.

On supported iPhone/iPad configurations, Web Push requires the web app to be added to the Home Screen and permission granted from an explicit gesture; it is not equivalent to ordinary browser-tab behavior. Android background push likewise depends on a supported browser, permission and OS settings. Badge counts are refreshed from the server's authoritative unread count in the foreground and, where the service worker Badging API is available, on push receipt. Unsupported platforms continue to use the in-app count. See [Apple's Web Push requirements](https://developer.apple.com/documentation/usernotifications/sending-web-push-notifications-in-web-apps-and-browsers). Browser delivery remains best-effort; verify on the actual supported phones and browsers used by the team.

## Role workflows

- **Client:** enter pickup/destination written addresses, recipient and package details, optional map share links, service and COD. Areas/zones are internal. Unclassified locations go to Admin price review; no location text is used to guess a zone or price. The client reviews/confirms a current quote before dispatch.
- **Admin / Dispatch:** review new requests, correct pickup and destination zones on the delivery record, complete a manual quote when the rate card cannot authoritatively price it, then assign a rider/driver and manage the run. Confirmed quotes remain immutable.
- **Rider / Driver:** use My day/My stops and the assigned run to open the next stop, call the recipient, open client-supplied map links, progress delivery, record the recipient and optional delivery note without an image upload, record COD and report exceptions.

The menu only shows pages for the signed-in role. Phone layouts use a focus-managed drawer, overlay dismissal and Escape; desktop Admin retains the sidebar. Sign Out is a POST session logout. The service worker never caches authenticated navigation or operational/financial pages.

## Known V1 limitations and production gates

- No safe disposable PostgreSQL service/container is available in this workspace; never use the persistent Supabase production database for Django test database lifecycle checks. A separate isolated PostgreSQL test instance is still needed to verify PostgreSQL locking and SQL behavior.
- Real push-service delivery needs a public HTTPS origin, configured VAPID keys, a scheduled outbox command and verification on the target devices. Automated encryption and outbox tests do not prove background delivery.
- Configure production host/origin values and confirm HTTPS/proxy behavior before launch. HSTS must stay off until HTTPS and all included hostnames are verified.
- Booking has no geocoder, maps API, road distance service, automatic route optimization, GPS tracking, SMS, WhatsApp, email or payment transfer integration. Admin review is necessary for unclassified routes.
- COD and settlement reports record operational events; they do not move money. Riders need network access to submit changes; offline writes are not queued.

Do not treat the V1 as production-ready until PostgreSQL checks, the final production `check --deploy`, HTTPS/proxy checks, push worker scheduling, real-device push and full business-role sign-off pass in the deployment environment.

## Google onboarding and delivery completion changes

Google sign-in is optional until `GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_CLIENT_SECRET` are configured as environment variables. OAuth state protection and PKCE are provided by django-allauth. Onboarding only permits Client, Rider, or Driver; a Client gets a new organization after valid profile completion. A Rider/Driver stays pending and cannot open field workspace routes until an Admin approves and links a bike/car Resource. Existing account roles are reused on sign-in. Email collisions do not automatically link accounts. Configure the Google authorized redirect URI after Render assigns the production hostname: `https://<render-production-hostname>/accounts/google/login/callback/`.

V1 completion is recipient-based and accepts no image uploads. Migration 0011 adds the operational-access approval flag and makes the legacy POD photo/hash columns nullable before removing them from Django's migration state. It does not drop those columns or apply itself to production during this sprint.
