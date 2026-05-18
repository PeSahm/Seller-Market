# mgmt_ui

Management UI for the **Seller-Market** trading bot fleet. It sits on a separate VPS and
manages multiple trading servers over SSH + `docker compose`. The UI exposes two roles:
**admin** (full control: servers, users, secrets, deployments) and **agent** (operate
their assigned bots only). Built with **FastAPI + PostgreSQL + HTMX/Jinja**, deployed
as Docker.

## Production hardening (Phase 10)

The mgmt UI is safe to expose on the public internet behind TLS. Defences in place:

| Threat | Mitigation |
|---|---|
| Cross-origin form forgery | **Double-submit CSRF tokens** — every state-changing request requires a matching `X-CSRF-Token` header (HTMX) or `csrf_token` form field, validated against the `csrf_token` cookie. See `app/security/csrf.py`. |
| Cookie-attached WS upgrade | **Short-lived WS JWT** — `/ws/runs/{id}` requires a 30 s JWT in `?token=...` minted via `POST /auth/ws-token`. CSRF middleware doesn't run on WS upgrades, so the WS token closes that gap. |
| Brute-force login | **Per-IP rate limit** — `/auth/login` capped at 10/min/IP; `/auth/ws-token` at 60/min/IP. In-process token bucket. Behind a proxy, start uvicorn with `--forwarded-allow-ips=*`. |
| Secret leak via JSON / audit log | **Auto-redaction** — payloads with keys matching `password / secret / token / raw_pem / private_key / fernet / api_key` render as `***` in audit-log views and diff entries. See `app/services/audit.py::redact_payload`. |
| Key compromise without re-encrypt outage | **Versioned Fernet keyset** — `encrypt()` writes envelopes `{"v": N, "ct": "<token>"}`; `decrypt()` looks up the key by version. Old envelopes still decrypt after rotation. See `app/security/crypto.py`. |

### Required env vars in production

```bash
MGMT_SECRET_KEY=<openssl rand -base64 48>            # JWT signing
MGMT_CSRF_SECRET=<openssl rand -base64 48>           # min 32 chars; CSRF HMAC
MGMT_FERNET_KEY_PART1=<Fernet.generate_key()>        # split-key part 1 (in env)
MGMT_FERNET_KEY_PART2_PATH=/etc/sm/key.part2         # split-key part 2 (chmod 400)
COOKIE_SECURE=true                                   # Secure flag on cookies
```

Optional (only when running a versioned keyset, see Key rotation below):

```bash
MGMT_FERNET_KEY_VERSIONS='{"1":"<key1>","2":"<key2>"}'
MGMT_FERNET_CURRENT_VERSION=2
```

### Behind a reverse proxy

Caddy / nginx / Cloudflare etc. — uvicorn sees the proxy's IP unless launched with:

```bash
uvicorn app.main:app --forwarded-allow-ips='*' --proxy-headers
```

Without this the per-IP rate limiter buckets every request against the proxy IP.

## Quickstart (Docker)

1. Copy the env template and fill in secrets:

   ```bash
   cp .env.example .env
   ```

   Generate the two secrets:

   ```bash
   # MGMT_SECRET_KEY (JWT / session signing)
   openssl rand -base64 48

   # MGMT_FERNET_KEY_PART1 (first half of the split Fernet key)
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   The second half of the Fernet key (`/etc/sm/key.part2`, `chmod 400`) is provisioned
   out-of-band on the host. See `../docs/management-ui-plan.md` for the split-key model.

2. Start Postgres, then run migrations, then start everything:

   ```bash
   docker compose up -d postgres
   docker compose run --rm api alembic upgrade head
   docker compose up -d
   ```

3. Visit <http://localhost:8000>. On first run the app will prompt you to create the
   initial admin user.

## Architecture

See [`../docs/management-ui-plan.md`](../docs/management-ui-plan.md) for the full design:
data model, SSH worker, OCR integration, split-key encryption, and role/permission model.

## Dev quickstart (no Docker)

You'll need a local Postgres reachable on `localhost:5432`.

```bash
pip install -e ".[dev]"
export DATABASE_URL=postgresql+asyncpg://mgmt:changeme@localhost:5432/mgmt_ui
alembic upgrade head
uvicorn mgmt_ui.app.main:app --reload
```

## Backup & key rotation

The mgmt UI holds three categories of secret material. **All three must be
backed up — together they constitute the recovery set.** A DB-only backup
without the keys is useless; the keys without the DB are useless.

### 1. Database (`pg_dump`)

Daily encrypted dump to off-host storage:

```bash
docker exec mgmt_ui-postgres-1 pg_dump -U mgmt -Fc mgmt_ui \
  | age -r $(cat ~/.config/age/recipients.txt) \
  > "/backups/mgmt_ui-$(date -u +%Y%m%d).pgdump.age"
```

Restore:

```bash
age -d -i ~/.config/age/identity.key /backups/mgmt_ui-YYYYMMDD.pgdump.age \
  | docker exec -i mgmt_ui-postgres-1 pg_restore -U mgmt -d mgmt_ui --clean
```

`age` (or GPG) is the encryption layer — the dump itself is plaintext SQL,
so storing it unencrypted in any third-party bucket would leak the audit log,
agent usernames, and the encrypted broker passwords.

### 2. Fernet keyset (`MGMT_FERNET_KEY_PART1` + `key.part2`)

These two files are **NOT in the database**. Without them, the encrypted
broker passwords in `customers.password_enc` are unrecoverable.

Back them up separately (different storage, different access path) so a
compromise of the DB backup doesn't also compromise the keys:

```bash
# .env contains MGMT_FERNET_KEY_PART1 + MGMT_FERNET_KEY_PART2_PATH
cp .env /backups/secrets/mgmt_ui.env
cp /etc/sm/key.part2 /backups/secrets/key.part2
# Encrypt to a different recipient than the DB backup
age -r $(cat ~/.config/age/secrets-recipients.txt) \
    /backups/secrets/mgmt_ui.env > /backups/secrets/mgmt_ui.env.age
```

### 3. SSH private keys (`./.ssh_mgmt_ui/`)

One file per trading server, named `sm_<server_uuid>`, chmod 0600.
Back up the whole directory the same way as the Fernet keyset — these
are the credentials for SSH'ing into every trading host.

### Key rotation (online — no downtime)

Phase 10 introduced a versioned Fernet keyset, so rotation is online:

1. Generate a fresh Fernet key:

   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

2. Add it to the keyset as a new version. If you weren't using
   `MGMT_FERNET_KEY_VERSIONS` yet, this is also when you migrate off
   the legacy split-key:

   ```bash
   export MGMT_FERNET_KEY_VERSIONS='{"1":"<old-full-key>","2":"<new-key>"}'
   export MGMT_FERNET_CURRENT_VERSION=2
   ```

3. Restart the mgmt UI. From now on every `encrypt()` writes v=2;
   `decrypt()` still handles v=1 envelopes AND legacy unversioned
   ciphertexts.

4. Lazy re-encryption: every customer edit re-writes its
   `password_enc` field at the current version. To force a one-shot
   re-encrypt of every row, run the rekey script in `scripts/rekey.py`
   (operator-launched; not auto-run on boot).

5. Once the `secret_decrypt` audit log shows no more v=1 reads, drop
   v=1 from the keyset:

   ```bash
   export MGMT_FERNET_KEY_VERSIONS='{"2":"<new-key>"}'
   ```

   Restart. Any remaining v=1 ciphertext will now fail to decrypt —
   step 4 should have caught them all.

### Don't include in any backup

* The host SSH private key for the mgmt VPS itself (separate concern; back up via your normal host-key policy).
* Postgres role passwords (regenerate on restore via `ALTER USER`).
* The CSRF secret (`MGMT_CSRF_SECRET`) — regenerable; only invalidates active sessions.

## Tests

```bash
pytest
```

## Project layout

```
mgmt_ui/
  app/              FastAPI app (routes, models, services, templates)
  workers/          Background workers (ingest runner, SSH ops)
  alembic/          DB migrations
  tests/
  pyproject.toml
  Dockerfile
  docker-compose.yml
  alembic.ini
  .env.example
```

Abbreviated; see the plan doc for the full layout and module breakdown.
