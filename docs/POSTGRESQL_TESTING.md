# Isolated PostgreSQL testing

The production Supabase database is never used as Django's test database. Django test setup creates and destroys an isolated test database and needs predictable PostgreSQL connection lifecycle behavior; the Supabase Session Pooler previously closed or retained backend connections during that lifecycle.

At the time of the V1 pre-GitHub implementation, this Windows workspace has no `docker`, `podman`, `psql`, `postgres`, or `pg_ctl` executable and no PostgreSQL Windows service. Therefore, no safe local disposable PostgreSQL server is available. The full development suite runs only with local SQLite selected explicitly (`DPOT_POSTGRES=0`). This validates application behavior but does not validate PostgreSQL locking or SQL behavior.

Before GitHub release, provision a dedicated disposable PostgreSQL 16+ instance or local container with a database/user separate from production. Configure only that instance's credentials for a dedicated test shell, run `manage.py test`, and verify Django creates/drops only its test database. Never point this command at the persistent Supabase project or its pooler.
