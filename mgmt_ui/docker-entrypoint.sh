#!/bin/sh
# mgmt_ui container entrypoint.
#
# Runs ``alembic upgrade head`` before handing off to the main CMD (uvicorn).
# Idempotent — exits cleanly when the schema is already at head, so it's safe
# to invoke on every container start (including ``docker compose up`` after
# any image refresh).
#
# Escape hatch: set ``SKIP_MIGRATIONS=1`` in the environment to bypass the
# upgrade. Useful when an operator wants to run migrations manually as a
# one-off (e.g. against a freshly-restored DB before flipping traffic).

set -e

if [ "${SKIP_MIGRATIONS:-0}" != "1" ]; then
  echo "[entrypoint] running alembic upgrade head"
  alembic upgrade head
else
  echo "[entrypoint] SKIP_MIGRATIONS=1 — skipping alembic upgrade"
fi

# Hand off to the image's CMD (or whatever args docker compose passes in).
exec "$@"
