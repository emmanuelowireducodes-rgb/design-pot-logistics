# DESIGN-POT LOGISTICS

Phase 2 connects booking, development quotes, confirmation, manual dispatch, delivery progress, lightweight recipient-based delivery completion and COD reconciliation. Django architecture and Phase 1 roles/design are preserved. Read [Phase 2 setup, routes and limitations](docs/PHASE2.md). V1 location entry is manual and uses Design-Pot operational zones plus optional client-supplied Google Maps links; no Google Maps Platform key, API or billing account is required. See [V1 location and navigation](docs/PHASE25_V1_LOCATION.md).

## Local setup (PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.lock
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py collectstatic --noinput
.\.venv\Scripts\python.exe manage.py provision_user dispatch --role admin --first-name Akosua
.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000
```

Open http://127.0.0.1:8000. The provision command privately prompts for a password. It does not make a public demo user. Reset an account password with `manage.py changepassword USERNAME`.

## Isolated sample workspace

In a separate shell, use a different port if your real local database is already running:

```powershell
$env:DPOT_DEMO = "1"
.\.venv\Scripts\python.exe manage.py migrate
# Supply a strong local-only password through the environment.
$env:DPOT_DEMO_PASSWORD = Read-Host "Choose a sample account password"
.\.venv\Scripts\python.exe manage.py seed_demo
Remove-Item Env:DPOT_DEMO_PASSWORD
.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8001
```

Usernames: `demo.admin`, `demo.client`, `demo.rider`, `demo.driver`. All use the password you supply. Every page displays the sample banner. Data is stored in `demo.sqlite3`, separate from `db.sqlite3`. The seed command rejects production, PostgreSQL, non-demo and non-empty databases. No password is committed.

## Routes

- `/login/`: secure session sign-in; `/logout/` accepts POST only.
- `/admin/dashboard/`: operations overview; deliveries, fleet, clients, notifications and account views. New Rider/Driver Google signups remain pending until Admin links a vehicle.
- `/client/dashboard/`: organization-scoped overview and delivery history.
- `/field/dashboard/`: mobile rider/driver workspace and assigned delivery cards.
- `/deliveries/<uuid>/`: scoped details, status history and recipient/note completion details.
- `/admin/components/`: design system reference.
- Booking, quotes, dispatch, calendar, finance/COD settlement tracking, management reports and development pricing are functional. Settlement tracking records an Admin-reported remittance; it does not move funds.

## Quality checks

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy config operations manage.py
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.\.venv\Scripts\python.exe manage.py collectstatic --noinput
.\.venv\Scripts\python.exe manage.py test
```

Django has no JavaScript bundle build; collectstatic builds fingerprinted, compressed assets. Tests cover roles, object ownership, login/CSRF, current assignments, immutable history, quote confirmation, constraints, notification ownership and schedule overlap. PostgreSQL CI is included; a passing local SQLite run does not verify PostgreSQL concurrency.

## Production configuration

See `.env.example` for safe placeholders. In local development, copy it to the ignored repository-root `.env`; Django loads it without overriding values already present in the process environment. Production skips `.env` and uses the hosting environment variables. Set `DPOT_ENV=production`, a random `DJANGO_SECRET_KEY` of 50+ characters, explicit `DJANGO_ALLOWED_HOSTS`, trusted HTTPS origins if needed, and PostgreSQL connection variables. Demo mode must be off. Use `DPOT_POSTGRES=1` to test PostgreSQL in development; `PGSSLMODE=disable` is for an isolated local test service only.

Install `requirements.lock`, provision PostgreSQL, run migrations and collectstatic, then serve `config.wsgi:application` with an appropriate production WSGI server behind HTTPS. The development server is not a production deployment. Configure proxy trust, edge rate limits, logging, backup/restore and monitoring for the selected host. Use an application database role without schema/trigger modification privileges after a separate migration role applies migrations. Run `manage.py check --deploy` in the real production environment.

Google OAuth is optional until its environment variables are configured. No map keys, SMS, WhatsApp, email, payment or file-storage service is needed for V1 delivery completion. Production PostgreSQL and hosting have not been provisioned.

Read [architecture](docs/ARCHITECTURE.md), [design system](docs/DESIGN_SYSTEM.md) and [verification report](docs/VERIFICATION.md). Phase 2.5 V1 location decisions are in [the location note](docs/PHASE25_V1_LOCATION.md); Phase 3 dispatch runs are described in [the Phase 3 guide](docs/PHASE3_DISPATCH_RUNS.md), with results in [the Phase 3 verification report](docs/PHASE3_VERIFICATION.md). Phase 4 money definitions and reporting timestamps are in [the finance and reporting guide](docs/PHASE4_FINANCE.md).

See [V1 release readiness](docs/V1_RELEASE_READINESS.md) for production setup, Web Push, operational workflows and outstanding release gates.

## Google sign-in and field approval

Set `GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_CLIENT_SECRET` in the local ignored `.env` or hosting environment to enable Google sign-in. Do not put real values in `.env.example`. Google authenticates identity only; onboarding allows Client, Rider, or Driver and never Admin. Clients activate after required profile completion. Rider/Driver accounts remain pending until an Admin approves the request and links a bike/car profile in **Admin ? Riders & drivers**. Existing username/password Admin sign-in stays at `/login/`. Email collisions never auto-link accounts; the user must access the existing Design-Pot account. Google access tokens are not stored.

In Google Cloud Console, add the authorized redirect URI `https://<render-production-hostname>/accounts/google/login/callback/` after Render assigns the production hostname. Local callback URI is `http://127.0.0.1:8000/accounts/google/login/callback/`. OAuth state protection and PKCE are handled by django-allauth.

## PostgreSQL test limitation

This Windows workspace has no local PostgreSQL server, Docker/Podman runtime, or PostgreSQL service available. The persistent Supabase project is production and is never used for test database create/drop. See [the isolated PostgreSQL test note](docs/POSTGRESQL_TESTING.md); SQLite tests do not prove PostgreSQL row-lock behavior.
