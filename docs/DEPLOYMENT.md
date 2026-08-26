# Ubuntu VPS deployment

This runbook targets a small Ubuntu VPS (2 vCPU, 2–4 GB RAM) using Docker Compose. LLM inference is
remote, so PostgreSQL durability, network security, and operational discipline matter more than
local compute. Do not expose PostgreSQL publicly.

## 1. Prepare the host

Install supported Docker Engine and the Compose plugin from Docker's official Ubuntu repository.
Create a dedicated unprivileged operator, enable unattended security updates, and configure a
firewall allowing only SSH and HTTPS. Use SSH keys, disable password login after confirming access,
and keep the operating system patched.

Clone a reviewed release into a dedicated directory such as `/opt/fantasy-bench`. Restrict that
directory and its `.env` file to the deployment operator. Never commit `.env`.

## 2. Configure secrets and limits

```bash
cp .env.example .env
chmod 600 .env
openssl rand -base64 36   # generate ADMIN_API_KEY
openssl rand -hex 32      # generate a distinct URL-safe POSTGRES_PASSWORD
```

Set at least:

```dotenv
APP_ENV=production
DATABASE_URL=postgresql+psycopg://fantasy:REDACTED@postgres:5432/fantasy
POSTGRES_PASSWORD=REDACTED_DIFFERENT_SECRET
ADMIN_API_KEY=REDACTED_LONG_RANDOM_SECRET
LLM_PROVIDER=openrouter
OPENROUTER_API_KEY=REDACTED_OPENROUTER_KEY
OPENROUTER_DAILY_BUDGET_USD=10
OPENROUTER_SEASON_BUDGET_USD=200
OPENROUTER_MAX_SINGLE_REQUEST_USD=0.50
# Set only after configuring the dedicated key limit/organization guardrail in OpenRouter.
OPENROUTER_PROVIDER_SPEND_LIMIT_CONFIRMED=true
AUTO_RESUME_DRAFT=true
CORS_ORIGINS=https://league.example.com
```

Compose supplies its own `DATABASE_URL` using `POSTGRES_PASSWORD`; keep both values aligned if you
run outside Compose. Production deliberately refuses the fake provider, SQLite, missing/default
secrets, or a missing OpenRouter key. Set all three budget ceilings, request rate, timeout, and token
limit. Configure a dedicated OpenRouter API-key spending limit or organization guardrail no higher
than the application budget before confirming the setting above. Per-request routing also rejects
providers priced above the verified model table. Confirm every requested model slug is currently
available before deployment. The application fails provider calls rather than silently replacing a
manager.

Secrets should ultimately live in a host secret store or Docker secrets. Rotate the admin and
OpenRouter keys after suspected exposure. Do not place secrets in shell history, issue trackers,
images, proxy URLs, or logs.

## 3. Start and verify

```bash
docker compose config
docker compose build --pull
docker compose up -d
docker compose ps
docker compose logs --tail=100 app
curl --fail http://127.0.0.1:8000/health
curl --fail http://127.0.0.1:8000/ready
```

The entrypoint runs `alembic upgrade head` before Uvicorn. A failed migration prevents the API from
starting. Compose binds the application to `127.0.0.1`, and the database has no published port.
Containers drop Linux capabilities and the app runs as a non-root user.

Configure Caddy, nginx, or another maintained reverse proxy for TLS. Forward to
`127.0.0.1:8000`, preserve the client/proxy headers, set reasonable request and idle timeouts, and
rate-limit administrative routes. Restrict `/docs` at the proxy if API discovery should not be
public. Never pass the admin key in a query string.

## 4. Initialize—without starting the draft

```bash
curl -X POST https://api.example.com/api/v1/admin/initialize \
  -H 'Content-Type: application/json' \
  -H "X-Admin-API-Key: $ADMIN_API_KEY" \
  -d '{"name":"Fantasy Bench","nfl_season":2026,"seed_fixture_players":false}'
curl https://api.example.com/api/v1/draft
```

The draft must report `NOT_STARTED`. Initialization creates the eight franchises, settings,
schedule, and draft order; it does **not** ask an LLM to draft. Synchronize/verify player data and
model configuration, inspect draft order and budgets, then take a fresh backup.

Only a deliberate commissioner action starts drafting:

```bash
curl -X POST https://api.example.com/api/v1/draft/start \
  -H "X-Admin-API-Key: $ADMIN_API_KEY"
```

Do not automate that request in Compose, systemd, CI/CD, health checks, migrations, or host reboot
scripts. `AUTO_RESUME_DRAFT=true` only resumes work whose persisted status is already `ACTIVE`.

## Backups and restore drills

The named `postgres_data` volume is not itself a backup. Take consistent logical dumps, encrypt
them, copy them off the VPS, and apply retention (for example, 7 daily, 5 weekly, and 12 monthly).

```bash
mkdir -p backups
chmod 700 backups
docker compose exec -T postgres pg_dump -U fantasy -d fantasy -Fc > backups/fantasy.dump
sha256sum backups/fantasy.dump > backups/fantasy.dump.sha256
```

The dump contains manager decisions and possibly provider payloads; treat it as sensitive. Encrypt
before off-host transfer. Keep credentials and encryption keys outside the backup set. Monitor dump
exit status, size, checksum, age, and remote-copy success.

Test restoration regularly on an isolated database—not over production:

```bash
docker compose exec -T postgres createdb -U fantasy fantasy_restore_test
docker compose exec -T postgres pg_restore -U fantasy -d fantasy_restore_test \
  --clean --if-exists < backups/fantasy.dump
docker compose exec -T postgres psql -U fantasy -d fantasy_restore_test \
  -c 'select count(*) from leagues;'
```

Drop the isolated test database after verification. For disaster recovery, deploy the same
application revision, restore before admitting writes, run `alembic current`, start the app, verify
`/ready`, and inspect draft/waiver/trade state before enabling autonomous jobs.

## Safe upgrades and rollback

1. Review application and migration changes; run the full test suite.
2. Record the current source revision and image digest.
3. Pause an active draft and avoid upgrading during waiver/trade processing.
4. Take and verify a fresh PostgreSQL dump.
5. Build the new image, then run `docker compose up -d`.
6. Check migration output, `/health`, `/ready`, logs, league status, and critical reads.

Application rollback does not imply database rollback. Prefer forward-fix migrations. If a release
cannot read the migrated schema, stop writes and restore the pre-upgrade database backup alongside
the prior image. Never blindly downgrade a production schema.

## Monitoring and incidents

Monitor container restarts, readiness, disk space, PostgreSQL volume growth, backup freshness,
OpenRouter spend, LLM error rate/latency, failed draft picks, and job failures. Send structured logs
to a retained off-host destination but redact secrets and sensitive prompt payloads as policy
requires.

If `/health` fails, inspect the app process and recent deploy. If `/ready` fails, check PostgreSQL
health, disk, credentials, and migrations. If an LLM/provider fails, pause the automation, preserve
the audited failure, correct configuration or provider availability, then resume explicitly. Never
edit draft or roster tables by hand while the app is running.

Keep one app replica. The current in-process scheduler has no distributed leader election. Database
idempotency protects core operations, but multiple schedulers are unsupported.
