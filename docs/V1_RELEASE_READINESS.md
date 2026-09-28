# Design-Pot Logistics V1 release readiness

## Current release status

The Phase 4 baseline is commit `aabcfc447b584462eaf286eedf66663ac5df3966`. This release-candidate sprint preserves that history and adds mobile navigation, simpler client location entry, account-level Web Push subscriptions and a push outbox. PostgreSQL, public HTTPS delivery and real-device push still require environment verification before production use.

The local release gate passed 133 Django tests, 8 JavaScript tests, Ruff, Mypy, Django system checks, migration consistency, static collection and read-only SQLite integrity checks. Browser verification passed for the Client, Rider and Admin layouts with no console exceptions, failed requests or horizontal overflow. The integrated test follows a written-address booking through Admin zone classification/manual quote, Client confirmation, Rider run and proof of delivery, exact COD collection, Admin reconciliation and recorded settlement. `check --deploy` still reports six development-environment warnings; configure and rerun it in production before release.

## Run locally

Follow the PowerShell steps in the root README. Install `requirements-dev.lock`, migrate, collect static files, provision a private test account and run `manage.py runserver`. The local SQLite database is for development only. `DPOT_DEMO=1` uses the separate, safety-checked `demo.sqlite3` sample workspace; never seed the company database.

The process reads environment variables directly; it does not load `.env` files. `.env.example` contains placeholders only. Keep credentials and private media outside Git.

## Production setup

- Use PostgreSQL and a dedicated database role. Back up the database and the private POD media store together; test restore procedures before launch.
- Set `DPOT_ENV=production`, `DPOT_DEMO=0`, a random `DJANGO_SECRET_KEY` of at least 50 characters, explicit `DJANGO_ALLOWED_HOSTS`, and `DJANGO_CSRF_TRUSTED_ORIGINS` for HTTPS origins as required.
- Set `PGDATABASE`, `PGUSER`, `PGPASSWORD`, `PGHOST`, `PGPORT`, and `PGSSLMODE=require`. Apply migrations using a deployment/migration role, then run static collection and start the WSGI application behind a correctly configured HTTPS proxy.
- Production turns on HTTPS redirects and secure session/CSRF cookies. Configure `DPOT_TRUST_PROXY_SSL_HEADER=1` only when the trusted edge proxy overwrites `X-Forwarded-Proto` and strips client-supplied values.
- HSTS is deliberately disabled until the domain is verified on HTTPS. After confirming every affected hostname and subdomain is HTTPS-only, set `DPOT_ENABLE_HSTS=1` and the chosen `DPOT_HSTS_SECONDS`; set subdomain/preload options only after separate review. Run `manage.py check --deploy` against the final deployment environment.
- Configure rate limiting, monitoring, error reporting, log rotation, database/media backup and a production WSGI server at the selected host. Django's development server is not a production server.

## Web Push setup and limits

Web Push uses the browser Push API, the existing root-scope service worker, VAPID and `pywebpush`. Set `DPOT_VAPID_PUBLIC_KEY`, `DPOT_VAPID_PRIVATE_KEY`, and `DPOT_VAPID_SUBJECT` to a matching key pair and contact address. The private key is URL-safe base64 without padding, not a PEM file. Generate it with the project's installed `py-vapid` package and place the output only in your deployment secret store:

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

Keep the private output in a secret manager; never paste it into `.env.example`, source, logs or this guide. Use one stable key pair per deployment and all workers. Configure a scheduler/worker to run `python manage.py send_web_push` regularly (for example, once per minute). Delivery uses a transactional outbox; transient provider failures are retried up to six attempts, and expired 404/410 endpoints are removed. Domain actions never call push providers synchronously.

The app requests permission only after the user chooses **Turn on notifications** in Account. HTTPS is required outside localhost. Notifications show generic text; opening one goes to that signed-in account's notification center. Browser/device settings control notification sound, vibration and presentation. When the app is open, a quiet optional foreground tone plays only after the browser records a user gesture; users can mute it in Account.

On iPhone/iPad, Web Push is supported for web apps added to the Home Screen on iOS/iPadOS 16.4 or later; the user must grant permission from an explicit gesture. It is not enabled for an ordinary browser tab. See [Apple's Web Push requirements](https://developer.apple.com/documentation/usernotifications/sending-web-push-notifications-in-web-apps-and-browsers). Browser delivery remains best-effort; verify on the actual supported phones and browsers used by the team.

## Role workflows

- **Client:** enter pickup/destination written addresses, recipient and package details, optional map share links, service and COD. Areas/zones are internal. Unclassified locations go to Admin price review; no location text is used to guess a zone or price. The client reviews/confirms a current quote before dispatch.
- **Admin / Dispatch:** review new requests, correct pickup and destination zones on the delivery record, complete a manual quote when the rate card cannot authoritatively price it, then assign a rider/driver and manage the run. Confirmed quotes remain immutable.
- **Rider / Driver:** use My day/My stops and the assigned run to open the next stop, call the recipient, open client-supplied map links, progress delivery, capture proof, record COD and report exceptions.

The menu only shows pages for the signed-in role. Phone layouts use a focus-managed drawer, overlay dismissal and Escape; desktop Admin retains the sidebar. Sign Out is a POST session logout. The service worker never caches authenticated navigation or operational/financial pages.

## Known V1 limitations and production gates

- This sprint's local machine has no PostgreSQL service on `127.0.0.1:5432`; the migration and operational concurrency behavior still need a PostgreSQL deployment test.
- Real push-service delivery needs a public HTTPS origin, configured VAPID keys, a scheduled outbox command and verification on the target devices. Automated encryption and outbox tests do not prove background delivery.
- Configure production host/origin values and confirm HTTPS/proxy behavior before launch. HSTS must stay off until HTTPS and all included hostnames are verified.
- Booking has no geocoder, maps API, road distance service, automatic route optimization, GPS tracking, SMS, WhatsApp, email or payment transfer integration. Admin review is necessary for unclassified routes.
- COD and settlement reports record operational events; they do not move money. Riders need network access to submit changes; offline writes are not queued.

Do not treat the V1 as production-ready until PostgreSQL checks, the final production `check --deploy`, HTTPS/proxy checks, push worker scheduling, real-device push and full business-role sign-off pass in the deployment environment.
