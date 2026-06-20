# Runbook — DB auto-failover to a warm spare (#156)

Goal end-state (operator's spec):

```
   DATABASE  ───►  Windows PostgreSQL 18   (main, 87.107.164.154:65444)
                          │  every few minutes: pg_dump ──► pg_restore
                          ▼
   WARM SPARE ───►  postgres:18 on PouyanIt (5.10.248.55)   ← always current copy
                          ▲
   mgmt ────────►  PouyanIt  +  ParsPack    (two instances; one is worker-leader)
                   both point at the Windows main; both auto-fail-over to the spare
   BACKUPS ─────►  visible on /admin/ha, retention = keep 4
```

**What the app does automatically** (once enabled): each mgmt instance probes the
main DB; after a short sustained outage it **rebinds itself to the warm spare in
seconds** and keeps serving. It **never auto-fails-back** — returning to the main
is a deliberate restart after a re-sync (prevents split-brain). A `/admin/ha`
banner shows when you're on the spare.

---

## 0. Prerequisites (already done this session)
- Main DB is the external Windows PG18 (`DATABASE_URL` → `…@87.107.164.154:65444/mgmt_ui`).
- The mgmt image on `feat/db-ha-multi` carries the failover code (merge PR #158 first).

## 1. Stand up the warm spare (postgres:18 on PouyanIt)

```sh
ssh root@5.10.248.55
docker volume create sm_spare_pg
docker run -d --name sm-spare-pg --restart unless-stopped \
  -e POSTGRES_USER=mgmt -e POSTGRES_PASSWORD="$SPARE_PG_PASSWORD" -e POSTGRES_DB=mgmt_ui \
  -p 127.0.0.1:5433:5432 \
  -v sm_spare_pg:/var/lib/postgresql/data \
  postgres:18
# seed it once from the main so it's not empty:
docker exec sm-spare-pg sh -lc 'pg_dump -Fc "postgresql://mgmt:'"$MAIN_PG_PASSWORD"'@87.107.164.154:65444/mgmt_ui" -f /tmp/seed.dump && pg_restore --clean --if-exists --no-owner --no-privileges -d "postgresql://mgmt:'"$SPARE_PG_PASSWORD"'@127.0.0.1:5432/mgmt_ui" /tmp/seed.dump'
```

The spare is `postgresql+asyncpg://mgmt:<spare-pw>@127.0.0.1:5433/mgmt_ui` from the
PouyanIt host (or `@5.10.248.55:5433` from ParsPack — publish on the WG/private IP,
not 0.0.0.0, since the link is still plaintext).

## 2. Update the backup cron — restore into the spare, keep 4, honour the marker

Edit the existing `/root/db_ha_backup_cron.sh` on PouyanIt so each tick (every
2–5 min):

1. **Skip if failed over** — first line:
   ```sh
   MARKER=/var/lib/sm-mgmt/backups/FAILOVER_ACTIVE
   [ -f "$MARKER" ] && { echo "failover active — skip"; exit 0; }
   ```
   (mgmt writes this marker on failover; skipping protects live writes on the spare.)
2. **Dump the Windows main** via the spare container (Option B — it has the PG18 client):
   `docker exec sm-spare-pg pg_dump -Fc "$MAIN_DSN" -f /tmp/t.dump`
3. **Restore into the spare**: `docker exec sm-spare-pg pg_restore --clean --if-exists --no-owner --no-privileges -d "$SPARE_LOCAL_DSN" /tmp/t.dump`
4. **Copy the dump to** `/var/lib/sm-mgmt/backups/mgmt_<ts>.dump`, append the
   `manifest.json` entry `{file,taken_at,size,sha256,source,restored_ok}`, and
   **prune to the newest 4** (`KEEP=4`).

> Equivalent one-shot using the shipped code (if you run it from a container that
> has both python+app AND the PG18 client): `python -m app.services.db_backup
> --main-dsn "$MAIN_DSN" --spare-dsn "$SPARE_LOCAL_DSN" --dump-dir /var/lib/sm-mgmt/backups
> --keep 4 --marker-path /var/lib/sm-mgmt/backups/FAILOVER_ACTIVE` — it already does the
> marker-skip, restore, manifest and keep-4.

`run_backup` aborts the tick if the **dump** fails (main down) — so it never
restores a stale dump. Combined with the marker, the spare can't be clobbered.

## 3. Configure BOTH mgmt instances (PouyanIt + ParsPack)

Same `/opt/seller-market-mgmt/.env` on each (secrets MUST match across instances
so sessions validate + broker passwords decrypt everywhere):

```ini
DATABASE_URL=postgresql+asyncpg://mgmt:<main-pw>@87.107.164.154:65444/mgmt_ui
SPARE_DSN=postgresql+asyncpg://mgmt:<spare-pw>@<spare-host>:5433/mgmt_ui
ENABLE_DB_AUTO_FAILOVER=true
BACKUP_DIR=/var/lib/sm-mgmt/backups
BACKUP_RETENTION=4
# tuning (defaults are fine): DB_PROBE_INTERVAL_SECONDS=5 DB_PROBE_FAILURE_THRESHOLD=2 DB_PROBE_TIMEOUT_SECONDS=3
# leader election is already ON (WS3): ENABLE_WORKER_LEADER_ELECTION=true
# same as the other instances:
MGMT_SECRET_KEY=…  MGMT_CSRF_SECRET=…  MGMT_FERNET_KEY_PART1=…   (+ /etc/sm/key.part2)
```

- **PouyanIt** mounts `BACKUP_DIR` so its mgmt can write the FAILOVER marker that
  the local cron reads, and `/admin/ha` shows the Backups card. (compose: add
  `- /var/lib/sm-mgmt/backups:/var/lib/sm-mgmt/backups` to the api service.)
- **ParsPack** is the same image + env; its `SPARE_DSN` points at PouyanIt's spare
  over the private IP. (Its local marker is unused — the PouyanIt instance + the
  cron share the marker, which is what gates clobbering.)

`docker compose up -d api` on each. Both register the failover supervisor at
startup; WS3 leader election guarantees only one runs the fleet workers.

## 4. Verify (no real outage needed)
- `/admin/ha`: **Database → Active = main**, auto-failover **on**, Backups card shows
  `Kept 4 (retention 4)` with a recent `restored` latest entry; the **HA** tab shows
  both servers + which instance is the worker leader.
- Logs: `db_failover supervisor started (interval=5s threshold=2 …)` on each instance.

## 5. Rehearse failover on a quiet day (optional)
Stop the main briefly (or block the port). Within ~10 s each mgmt logs
`DB FAILOVER: … rebound to the SPARE`, `/admin/ha` flips to a red **Running on the
SPARE** banner + Active=SPARE, a `db_failover` critical alert appears, and the
cron pauses (marker present). mgmt keeps serving reads+writes from the spare.

## 6. Fail BACK (deliberate — never automatic)
When the Windows main is healthy again:
1. **Re-sync the main from the spare** (the spare has the newest data):
   `docker exec sm-spare-pg sh -lc 'pg_dump -Fc "$SPARE_LOCAL_DSN" -f /tmp/fb.dump && pg_restore --clean --if-exists --no-owner --no-privileges -d "$MAIN_DSN" /tmp/fb.dump'`
2. **Restart both mgmt instances** (`docker compose up -d --force-recreate api`). They
   boot bound to the main again; the supervisor confirms the main is healthy and
   **clears the FAILOVER marker**, so the backup cron resumes (Windows → spare).
3. Confirm `/admin/ha` → Active = main, banner gone.

> Never run both DBs as live at once. The marker + "no auto-failback" enforce this;
> the only manual rule is **re-sync before you restart on the main**.

## 7. Cold last-resort (recovery console — Option B)
If even the spare is gone and you must restore from a dump file: run the mgmt
image with `MGMT_RECOVERY_MODE=true` + `MGMT_RECOVERY_TOKEN` (WireGuard/loopback
only), open `/recovery`, pick a dump → it `pg_restore`s into the spare container
(Option B) and brings mgmt up on it. This is the DB-down console; the warm
auto-failover above is the everyday path.
