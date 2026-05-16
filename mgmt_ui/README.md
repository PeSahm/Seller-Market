# mgmt_ui

Management UI for the **Seller-Market** trading bot fleet. It sits on a separate VPS and
manages multiple trading servers over SSH + `docker compose`. The UI exposes two roles:
**admin** (full control: servers, users, secrets, deployments) and **agent** (operate
their assigned bots only). Built with **FastAPI + PostgreSQL + HTMX/Jinja**, deployed
as Docker.

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
