# DESIGN-POT LOGISTICS

Phase 2 connects booking, development quotes, confirmation, manual dispatch, delivery progress, private photo POD and COD reconciliation. Django architecture and Phase 1 roles/design are preserved. Read [Phase 2 setup, routes and limitations](docs/PHASE2.md). V1 location entry is manual and uses Design-Pot operational zones plus optional client-supplied Google Maps links; no Google Maps Platform key, API or billing account is required. See [V1 location and navigation](docs/PHASE25_V1_LOCATION.md).

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
- `/admin/dashboard/`: operations overview; deliveries, fleet, clients, notifications and account views.
- `/client/dashboard/`: organization-scoped overview and delivery history.
- `/field/dashboard/`: mobile rider/driver workspace and assigned delivery cards.
- `/deliveries/<uuid>/`: scoped details, status history and available POD.
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

See `.env.example`. Environment values are read from the process, not automatically from a .env file. Set `DPOT_ENV=production`, a random `DJANGO_SECRET_KEY` of 50+ characters, explicit `DJANGO_ALLOWED_HOSTS`, trusted HTTPS origins if needed, and PostgreSQL connection variables. Demo mode must be off. Use `DPOT_POSTGRES=1` to test PostgreSQL in development; `PGSSLMODE=disable` is for an isolated local test service only.

Install `requirements.lock`, provision PostgreSQL, run migrations and collectstatic, then serve `config.wsgi:application` with an appropriate production WSGI server behind HTTPS. The development server is not a production deployment. Configure proxy trust, edge rate limits, logging, backup/restore and monitoring for the selected host. Use an application database role without schema/trigger modification privileges after a separate migration role applies migrations. Run `manage.py check --deploy` in the real production environment.

No map keys, SMS, WhatsApp, email, payment or storage service is needed in Phase 1. Production PostgreSQL and hosting have not been provisioned.

Read [architecture](docs/ARCHITECTURE.md), [design system](docs/DESIGN_SYSTEM.md) and [verification report](docs/VERIFICATION.md). Phase 2.5 V1 location decisions are in [the location note](docs/PHASE25_V1_LOCATION.md); Phase 3 dispatch runs are described in [the Phase 3 guide](docs/PHASE3_DISPATCH_RUNS.md), with results in [the Phase 3 verification report](docs/PHASE3_VERIFICATION.md). Phase 4 money definitions and reporting timestamps are in [the finance and reporting guide](docs/PHASE4_FINANCE.md).
