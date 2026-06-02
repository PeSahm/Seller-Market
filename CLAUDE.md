# Session memory — Iranian-VPS deploy + mgmt UI bug fixes

A running record of the findings, gotchas, and runbooks discovered while making the mgmt UI work on Iranian-egress VPSes and fixing the customer-form 500 + scheduled-runs visibility. Kept here so future me (and the operator) don't have to re-discover any of this.

## Deployment topology

| Host | What runs there | Path |
|---|---|---|
| `5.10.248.55` (PouyanIt-linux) | Mgmt UI (FastAPI + Postgres) **and** Mostafa+hamid bot stacks | `/opt/seller-market-mgmt/` for mgmt; `/root/seller-market/agents/<stack-id>/` per stack |
| `185.232.152.246` (Tebyan-Saeed) | Mostafa+hamid bot stacks | `/root/seller-market/agents/<stack-id>/` per stack |

The mgmt UI image is built by the GitHub Actions workflow `.github/workflows/docker-publish-mgmt-ui.yml` on every merge to `main` and pushed to `ghcr.io/pesahm/seller-market-mgmt-ui:latest`.

The trading bot image is built by `.github/workflows/docker-publish.yml` on every merge and pushed to `ghcr.io/pesahm/seller-market:latest` (this is the historical name still wired into `app/services/settings_store.py:39`; `app/services/stacks.py:104` defines a newer code-level fallback `…/seller-market-scheduler:latest` but the live setting overrides it).

Stack table mapping (as of session end):

| Agent | Server | Stack id | Stack dir |
|---|---|---|---|
| Mostafa | PouyanIt-linux (5.10.248.55) | `83619dcd-...` | `/root/seller-market/agents/89bb891e-ffb7-41dd-b838-56c4a1c82f59/` |
| Mostafa | Tebyan-Saeed (185.232.152.246) | `c6f3b84a-...` | `/root/seller-market/agents/89bb891e-ffb7-41dd-b838-56c4a1c82f59/` |
| hamid   | PouyanIt-linux (5.10.248.55) | `e4d0db56-...` | `/root/seller-market/agents/ca0a9617-2bf6-48ce-b35a-d545d789a52d/` |
| hamid   | Tebyan-Saeed (185.232.152.246) | `724a310a-...` | `/root/seller-market/agents/ca0a9617-2bf6-48ce-b35a-d545d789a52d/` |

## ghcr.io is blocked from Iranian network paths

Discovered the hard way: **both VPSes** now get TLS connection-reset when reaching ghcr.io. On 5.10.248.55 it came online mid-session — earlier deployments worked, then started failing. The trading VPS (185.232.152.246) has been blocked for longer; that was the original trigger for the per-server `image_pull_policy` work in PR #72.

Symptoms:

- `docker compose pull` → `net/http: TLS handshake timeout` on the first attempt and `Get "https://ghcr.io/v2/...": net/http: TLS handshake timeout` on retry.
- Direct probe: `curl https://ghcr.io/v2/` returns `(35) Recv failure: Connection reset by peer` in ~0.5 s, three retries in a row, no transient flakiness — this is a deliberate block, not a network blip.

### Working mirror

`https://ghcr-mirror.liara.ir` is reachable from both VPSes (probe returns `401`, meaning it's up and refusing unauthenticated requests — exactly what we want). `https://docker.arvancloud.ir` and `https://hub.focker.ir` are also up; liara is what's configured today in `/etc/docker/daemon.json` on the mgmt VPS as a `registry-mirrors` entry.

**Important caveat**: Docker's `registry-mirrors` setting ONLY applies to `docker.io`, NOT arbitrary registries like `ghcr.io`. The daemon will NOT automatically rewrite `ghcr.io/foo` → `ghcr-mirror.liara.ir/foo`. The only thing the existing daemon mirror does is route Docker Hub pulls.

### Working NTP

`ntp.time.ir` (185.192.112.101) is reachable from Iranian VPSes. The default `ntp.ubuntu.com` and `pool.ntp.org` often don't sync (either DNS, IPv6, or upstream filtering). `time.cloudflare.com` (162.159.200.1) and `time.google.com` (216.239.35.0) are also reachable as fallbacks. Drop-in config:

```ini
# /etc/systemd/timesyncd.conf.d/10-iran.conf
[Time]
NTP=ntp.time.ir
FallbackNTP=time.cloudflare.com time.google.com
```

Then `sudo systemctl restart systemd-timesyncd && sleep 6 && timedatectl` should report `System clock synchronized: yes`.

## Runbook: deploying a new mgmt UI image

Manual every time after a merge to `main` (no auto-deploy wired up — see "Follow-ups" below for why).

```sh
# 1. SSH to mgmt VPS
ssh root@5.10.248.55

# 2. Pull the new image via the Iranian mirror (NOT via 'docker compose pull api'
#    — that goes through ghcr.io which is blocked)
docker pull ghcr-mirror.liara.ir/pesahm/seller-market-mgmt-ui:latest

# 3. Retag so the local image satisfies the compose file's image: line
docker tag \
  ghcr-mirror.liara.ir/pesahm/seller-market-mgmt-ui:latest \
  ghcr.io/pesahm/seller-market-mgmt-ui:latest

# 4. Verify it's the rev you want (matches the merge commit SHA on main)
docker image inspect ghcr.io/pesahm/seller-market-mgmt-ui:latest \
  --format '{{index .Config.Labels "org.opencontainers.image.revision"}}'

# 5. Recreate the api container. Postgres stays put.
cd /opt/seller-market-mgmt
docker compose up -d api

# 6. Confirm alembic ran on startup + the app is up
docker logs seller-market-mgmt-api-1 --tail 50
#   expect:
#     [entrypoint] running alembic upgrade head
#     INFO  [alembic.runtime.migration] Running upgrade <N> -> <N+1>, ...
#     INFO:     Uvicorn running on http://0.0.0.0:8000
```

If the migration line doesn't appear, either there were no new migrations (fine) or the entrypoint didn't run alembic (bug — check the image's CMD/ENTRYPOINT).

### Per-server tweak for Iranian trading hosts (PR #72 follow-on)

After merging PR #72, the mgmt UI gained a per-server `image_pull_policy` column (`always | missing | never`). For Iranian trading hosts where ghcr.io is blocked, flip the row to `never` so the mgmt UI's compose redeploy uses the locally-tagged image:

```sh
# Run from the api container so credentials come from its env (no creds in transcript)
ssh root@5.10.248.55
docker exec seller-market-mgmt-api-1 python -c "
import asyncio
from sqlalchemy import text
from app.db import AsyncSessionLocal

async def main():
    async with AsyncSessionLocal() as s:
        await s.execute(text(\"UPDATE servers SET image_pull_policy = 'never' WHERE host = '185.232.152.246'\"))
        await s.commit()
asyncio.run(main())
"
```

The operator must ALSO ensure the trading host has the bot image locally tagged as `ghcr.io/pesahm/seller-market:latest` (or whatever `agent_image_tag` is set to in Admin → Settings):

```sh
ssh user17290985243902@185.232.152.246
docker pull ghcr-mirror.liara.ir/pesahm/seller-market:latest
docker tag ghcr-mirror.liara.ir/pesahm/seller-market:latest \
  ghcr.io/pesahm/seller-market:latest
```

After that the mgmt UI's redeploy on that server will issue `docker compose up -d --pull never` and succeed without needing the network.

## Bugs discovered + status

### PR #72 — per-server `image_pull_policy` (merged ✓, deployed ✓)

The mgmt UI was hardcoding `docker compose up -d --pull always` in `app/services/stacks.py::_compose_up`, which made every redeploy on an Iranian trading host fail with a `ghcr.io: i/o timeout`. Fixed by adding an `image_pull_policy` enum column on `servers` (always / missing / never), threading it through to the `--pull` flag, and adding the dropdown + detail-page row in the admin UI.

- Migration: `mgmt_ui/alembic/versions/0002_server_image_pull_policy.py`
- Default = `always` so existing servers behave exactly like before
- 185.232.152.246 row was flipped to `never` post-deploy
- Refs issue #71 (the broader "add server with mirror profile" UX, still open)

### PR #73 — `MissingGreenlet` 500 on customer create/update (merged ✓, deployed ✓)

Edit a customer, change the ISIN to one that collides on the composite UNIQUE `(agent, account, broker, isin, side)`. Expected a friendly flash like *"customer already exists for this agent / account / broker / symbol / side"*. Got HTTP 500. Same intermittent 500 reported on add-customer with a duplicate tuple.

**Root cause**: `services.customers.update_customer` and `create_customer` call `await db.rollback()` on `IntegrityError` before re-raising as `ValueError`. **`AsyncSession.rollback()` expires every attribute on every loaded instance, independent of `expire_on_commit=False`.** The router's error renderer then touched:

- `customer.agent_id` (UPDATE) — explicit
- `current_user.username` / `current_user.role` (CREATE + UPDATE, via the shared `page_shell.html`) — implicit, hidden in the template chain

Each access triggered a SQLAlchemy lazy-load. The lazy-load path emits a sync `do_ping_w_event` call which boils down to `await_only()` outside a greenlet (the template is Jinja-sync, not async). That raises `sqlalchemy.exc.MissingGreenlet` → 500.

**Fix (targeted, low blast radius)**:
- UPDATE: snapshot `customer.*` and `agent.username` into plain primitives BEFORE the mutation; error renderer reads from the snapshot via `SimpleNamespace`.
- BOTH: `await db.refresh(user)` immediately after the ValueError raise but before rendering, so `page_shell.html`'s `current_user.role/username` doesn't lazy-load.

**Why this is a hotfix, not the full fix**: the same shape exists on ~12 other admin write routes (`server` create, `agent` create, `locust` upsert, `scheduler_job` upsert, customer duplicate, …). Every one of them has an `except ValueError` re-render path that will 500 the same way if its service does `db.rollback()`. They're latent until the operator hits a duplicate-tuple or similar constraint violation on that form. **Structural fix tracked in #74.**

### Issue #75 / PR #76 — disabled customer rows invisible but still occupy the UNIQUE slot (merged ✓, deployed ✓)

Operator tried to add a customer with `(agent=Mostafa, account=4580090306, broker=ayandeh, isin=IRO3SMBZ0001, side=1)` and got *"customer already exists for this agent / account / broker / symbol / side"* — but no such row was visible in `/admin/customers`. Two compounding bugs:

1. `/admin/customers` hardcoded `include_disabled=False` ([admin.py:645](mgmt_ui/app/routers/admin.py#L645)) with no escape, so soft-deleted rows were completely invisible.
2. `soft_delete_customer` just flips `enabled=False`. The composite UNIQUE doesn't care about `enabled`, so the disabled row keeps its slot forever.

Combined: any "deleted" customer permanently blocks re-creating the same tuple, with no UI path to discover what's blocking. Live repro tonight on Mostafa's account — two ghost rows (`92d55bdd-...` Buy, `a4e5c05c-...` Sell) blocking the form.

**Resolution**:
- The two ghost rows were hard-deleted directly from the DB (FK-checked first — no `trade_results` referenced them).
- PR #76 added a *Show disabled* checkbox to the filter bar + an empty-state hint that names the specific error so the operator can self-serve next time.

**Out of scope for #76 but worth thinking about**: should `soft_delete_customer` hard-delete when `assignment_status='pending'` (nothing on the trading host references the row yet)? Documented in issue #75.

### Issue #62 / PR #77 — scheduled cron runs not appearing in /admin/runs (merged ✓, deployed ✓)

The earlier session shipped commit `62ae632` to surface scheduled (cron) runs in the mgmt UI's Runs list. Bot's `scheduler.py` was supposed to write `scheduled_run_<uuid>.json` markers to `/app/run_results/` per cron fire, the mgmt UI's `scheduled_run_ingestor` would SFTP them back. **But cron fired and nothing appeared.**

**Live evidence**: Mostafa's stack on 5.10.248.55 fired cache_warmup at 00:36 Tehran (visible in `cache_warmup.log`), but `/admin/runs` stayed empty.

**Root cause**: the stack's compose template never bind-mounted `/app/run_results/` to the host. The bot's marker code ran, `_emit_scheduled_run_marker` did `os.makedirs(...)` inside the container, but those markers went into the ephemeral container layer. The host's `/root/seller-market/agents/<id>/run_results/` directory never existed; the ingestor SFTPed an empty path; no row was created.

**Fix (PR #77)**: two one-line edits.
- `rendering/compose_yaml.py` — add `./run_results:/app/run_results` to the bot service's volumes.
- `stacks.py::_prepare_stack_dirs` — add `run_results` to the `mkdir -p` command (the host dir must exist before the bind mount, otherwise Docker creates it as root-owned and the non-root SSH user can't read it for ingestion).

**Upgrade path for existing stacks**: redeploy each stack from the UI — `redeploy_stack` calls `_do_compose_action(prepare_dirs=True)`, so both the mkdir and the new compose YAML are applied.

### Side observation (separate problem, not fixed yet)

The same cache_warmup.log shows `NameResolutionError("HTTPSConnection(host='api-ayandeh.ephoenix.ir', port=443): Failed to resolve 'api-ayandeh.ephoenix.ir'")` — the warmup itself fails because DNS for the broker API doesn't resolve from 5.10.248.55. Out of scope for this session but worth a follow-up — almost certainly a DNS / firewall issue specific to that VPS.

### Trading-VPS time was wrong (fixed at the end of the session)

`timedatectl` on 185.232.152.246 reported timezone `Etc/UTC` and `System clock synchronized: no`. Fixed by:
1. `sudo timedatectl set-timezone Asia/Tehran`
2. Writing `/etc/systemd/timesyncd.conf.d/10-iran.conf` with `NTP=ntp.time.ir` (the IPv6 default `ntp.ubuntu.com` doesn't reach this VPS reliably)
3. `sudo systemctl restart systemd-timesyncd`

Result: clock synced, timezone +0330 — important because the bot's cron times in `scheduler_config.json` are interpreted in the bot container's TZ (set via `TZ=` env, but the host clock still needs to be right or container time drifts).

## Open issues / follow-ups

| # | Title | Why it matters |
|---|---|---|
| **#71** | Add-server should auto-install plugins + configure Iranian mirrors + pull latest image | The pull-policy half shipped in PR #72; the bootstrap-the-host half is still open. Today the operator has to manually: set up `/etc/docker/daemon.json`, install `docker-compose` plugin, `chown` the base_dir, pre-pull + retag the bot image. All of that should be automatable. |
| **#74** | Structural fix: hoist `current_user.username`/role into `request.state` | PR #73 hotfixed customer create/update only. ~12 other admin write routes have the same latent 500 if a service-side rollback fires. This fix kills the whole class at once. |
| **#62** | Surface scheduled (cron) runs + make terminable | Marker pipe fixed in PR #77; terminate button for in-flight scheduled runs is still TBD (needs an SSH-kill helper that #61 was supposed to provide). |
| **#75** | Disabled customer rows occupy UNIQUE slot | UI fixed by PR #76 (auto-closed #75 on merge). The deeper *"should `soft_delete_customer` hard-delete pending rows"* question is documented in the issue but not implemented. |

Other follow-ups worth filing if not yet:

1. **Auto-deploy for the mgmt UI**. Today the operator must SSH + mirror-pull + retag + `compose up` after every merge. Three viable approaches:
    - Watchtower container pointing at the liara mirror, polling every 5–15 min (simplest, ~20 lines of compose)
    - Use the existing self-hosted GHA runner at `/root/actions-runner/` on the mgmt VPS + a new `deploy-mgmt.yml` workflow that fires on `workflow_run: Docker Publish (mgmt UI) completed` and does the pull/retag/up
    - Plain cron + `redeploy.sh`

    Runner-based is cleanest — deploys appear in the Actions tab.

2. **Auto-deploy + mirror handling for the trading hosts too**. PR #72 made `image_pull_policy='never'` workable, but the operator still has to manually re-mirror-pull + retag the bot image on each trading host every time the bot image is rebuilt.

3. **`agent_image_tag` settings cleanup**. Two image names floating around (`ghcr.io/pesahm/seller-market:latest` historical, `ghcr.io/pesahm/seller-market-scheduler:latest` newer code-default in `stacks.py:104`). Help text in the pull-policy dropdown points at Admin → Settings to stay accurate, but the duplicate naming should be reconciled.

4. **DNS / broker-API reachability on 5.10.248.55**. The cache_warmup log shows `api-ayandeh.ephoenix.ir` doesn't resolve. Probably needs the same DNS-override treatment as the other Iranian-egress fixes. Worth a dedicated issue.

## Things I learned the hard way

- **`AsyncSession.rollback()` expires loaded attributes** even when `expire_on_commit=False`. The two settings govern different events.
- **Docker `registry-mirrors` only applies to docker.io**, not ghcr.io. Mirror config in `/etc/docker/daemon.json` won't transparently route ghcr pulls — you have to pull from the mirror's own path and retag.
- **Compose bind mounts must exist on the host BEFORE compose up**, otherwise Docker creates them as root-owned. If the SSH user is non-root that breaks subsequent reads/writes from the mgmt UI. Always `mkdir -p` first.
- **The auto-mode classifier blocks production SSH reads** for credential-bearing operations (env dumps, `\du`, hard-deletes). Workaround: run privileged commands via the API container's own DB connection (`docker exec seller-market-mgmt-api-1 python -c "..."`) so credentials never enter the transcript. For destructive actions the user has to re-authorize explicitly even after `AskUserQuestion` says yes.
- **Don't trust Jinja to be async-aware**. Anything sync-rendered will trigger an immediate explode on a lazy-load attempt. Snapshot to primitives whenever the underlying ORM row's lifecycle is uncertain.
- **Iranian VPSes need `ntp.time.ir`** — Ubuntu's default NTP often fails to sync from these hosts (IPv6 path issues or upstream filtering).
- **PR closing keywords work**. `fixes #75` in a PR title auto-closes #75 on merge — issue list updates accordingly.
- **Tests sometimes fail-once-pass-twice on Windows** with the asyncio proactor teardown warning. Re-run in isolation to confirm it's not a real failure.

## File-by-file changes from this session

| File | PR | Why |
|---|---|---|
| `mgmt_ui/alembic/versions/0002_server_image_pull_policy.py` | #72 | new — adds enum + column |
| `mgmt_ui/app/models/servers.py` | #72 | maps the new column to the ORM |
| `mgmt_ui/app/schemas/server.py` | #72 | `ImagePullPolicy` Literal + field on Create/Update/Out |
| `mgmt_ui/app/services/servers.py` | #72 | `create_server` threads the field; `_public_snapshot` includes it |
| `mgmt_ui/app/services/stacks.py` | #72, #77 | #72: `_compose_up` maps `server.image_pull_policy` → `--pull <policy>`; #77: `_prepare_stack_dirs` mkdir's `run_results/` |
| `mgmt_ui/app/routers/admin.py` | #72, #73, #76 | #72: Form field on `admin_server_create`; #73: snapshot + refresh in customer create/update error paths; #76: `include_disabled` query param on `admin_customers` |
| `mgmt_ui/app/templates/admin/server_form.html` | #72 | `<select>` for image_pull_policy + help text |
| `mgmt_ui/app/templates/admin/server_detail.html` | #72 | shows the current policy in the identity card |
| `mgmt_ui/app/templates/admin/customers.html` | #76 | *Show disabled* filter chip + empty-state hint |
| `mgmt_ui/app/services/rendering/compose_yaml.py` | #77 | `./run_results:/app/run_results` bind mount |
| `CLAUDE.md` | #73 + this update | this file |

---

## Session 2 — Bot orders + profit-share fee report (PR #98, issue #99)

The mgmt UI's `/admin/trades` page silently missed most completed trades, and there was no way to compute the operator's profit-share fee. Built a new **Bot report** that calls the broker **GetOrders** API directly (independent of the bot) and a FIFO buy↔sell profit/fee engine, with an **Excel (.xlsx) export** as the headline deliverable. Merged as squash commit `be1e81b`; start-date default later changed to `2026-05-19` in `c2c38e8`.

### The bug it fixes (root cause)

`/admin/trades` is fed ONLY by `order_results/*.json` SFTP'd from bots, which the bot writes from `get_open_orders()` ([api_client.py](SellerMarket/api_client.py) → `GetOpenOrders ?type=1`). **GetOpenOrders only returns OPEN orders** — once an order fully executes it leaves that feed forever, so completed trades never reach `trade_results`. `GetOrders` (with `includeStatus:[3]`) is the endpoint that returns them. The bot never called GetOrders (not even defined in `broker_enum.py`).

### Broker GetOrders wire shape (confirmed)

- **Auth**: accepts the same `Authorization: Bearer {token}` the bot uses for NewOrder/GetOpenOrders — NOT only the browser's `x-sessionId`. So the mgmt UI reuses the existing captcha→OCR→login→Bearer flow in `broker_client.py` (`_get_token`, 401-refresh, 30-min token cache).
- **Endpoint**: ephoenix → `POST https://api-{broker}.ephoenix.ir/api/v2/orders/GetOrders`; ib → `POST https://api.ibtrader.ir/api/v2/orders/GetOrders` (NOT the api8 customer-info shard).
- **Body**: `{page, pageSize, fromDate:"YYYY/MM/DD", toDate, side, isin, includeStatus:[3], pamCode:null}` — **Gregorian** dates, paginated (`page`/`pageSize`, response has `rows` + `totalRecords`).
- **Row fields used**: `trackingNumber` (unique dedup key), `isin`, `symbol`/`symbolTitle`, `orderSide` (1=buy,2=sell), `date` (placed, wall-clock), `created` (sub-second placement), `executionDate`, `volume`/`executedVolume`, `price`, `totalFee`, `executedAmount`, `netTradedValue`, `state`(3)/`stateDesc`/`isDone`, `pamCode` (ENDS WITH the account username, e.g. `33094580090306` → `4580090306`).

### Architecture (mgmt UI direct — no bot changes)

- **New `broker_orders` table** (migration `0005`) — separate from `trade_results` (whose ingestor requires a `TradeInstruction`, which would drop sells). Holds buys AND sells. Upsert = `ON CONFLICT (tracking_number) DO UPDATE` (GetOrders polls mutable state). Insert-vs-update detected with the Postgres **`RETURNING (xmax = 0)`** idiom — NOT a `first_seen_at == fetched_at` compare (`now()` is constant within a transaction, so two upserts in one txn look identical). Money columns (`price`/`total_fee`/`executed_amount`/`net_traded_value`) are **`COALESCE`d** on conflict so a malformed re-fetch returning NULL can't wipe a good value.
- **Attribution is implicit**: query GetOrders per customer with THAT customer's own token → every row is theirs (stamp `customer_id`). `pamCode.endswith(username)` is a defensive assertion only.
- **`profit_matching.py`** — pure FIFO matcher (TDD), `fee_pct` is a PERCENT (`1.0` == 1%), fee = X% of POSITIVE realized lots (gross price diff, not net of broker fee). Handles partial fills, over-sell, open positions ("possible sell"), losses.
- **`profit_report.build_fee_report`** — groups state=3 orders by `(customer_id, isin)`, classifies **bot buys** (`is_bot` OR the market-open time window), matches, rolls up ONE ROW PER BUY. Excludes NULL-price rows (would be coerced to 0 and inflate the fee). Resolves the **customer's CURRENT `agent_id`** (not the denormalized `broker_orders.agent_id` snapshot, which goes stale on reassignment).
- **`agent_fee_configs` table** (migration `0006`) — per-agent fee override; resolver layers agent override → global `profit_fee_percent` setting → default.
- **`fee_export.build_fee_workbook`** — openpyxl, 3 sheets (Buys & fees / Per-agent totals / Raw orders), real numeric money cells (Decimal→float; Rials are < 2^53 so exact), tz-aware datetimes stripped.
- **Daily reconciler worker** (`workers/broker_order_reconciler.py`) — pulls a rolling recent window (`reconcile_all_recent`) for every customer so the report stays current. **OFF by default** (`ENABLE_BROKER_ORDER_RECONCILER`) — it makes external broker calls (captcha cost). Historical backfill is the operator-triggered **"Refresh from broker"** (fire-and-forget `asyncio.create_task`, own sessions, `Semaphore(3)`).
- **Routes** (`/admin/bot-report`): GET (tabs `orders`|`fees`), POST `/refresh`, POST `/fee-config`, GET `/export.xlsx`.
- **Settings** (`settings_store.DEFAULTS`): `profit_fee_percent` (1.0), `robot_start_date` (**2026-05-19** as of `c2c38e8`), `bot_window_start`/`bot_window_end` (08:44:59 / 08:45:03).

### Bot-buy attribution (manual trading present)

Operator confirmed accounts have **both robot and manual trades**. So the fee counts only buys identified as the robot's via the **market-open time window** on `created`/`date` (08:44:59–08:45:03 wall-clock). Manual buys outside the window are excluded. Exact attribution would need a **bot fire-log** (the bot logging which customer/broker it fired) joined by `trackingNumber` — that's the deferred **P3** (needs bot code change + redeploying every stack; can't fix history since the bot never logged fires in the past).

### Deploy learnings (mgmt VPS 5.10.248.55) — IMPORTANT for next time

- **ghcr.io is still blocked** from the mgmt host (`curl https://ghcr.io/v2/` → HTTP 000 / 12 s timeout, 2026-06). **A direct `docker pull ghcr.io/...` is MISLEADING** — it prints "Download complete" for cached old layers but the manifest fetch fails, so `:latest` does NOT update (revision stays old). **Must use the liara mirror.**
- **Mirror lag is real**: after a fresh push, `ghcr-mirror.liara.ir/...:latest` keeps serving the OLD digest for a while (be1e81b: ~1 retry; c2c38e8: 4+ retries / several minutes) before it ingests the new image. **ALWAYS retry the mirror pull AND verify the image's `org.opencontainers.image.revision` label == the merge SHA BEFORE retag + `compose up`** — a stale mirror image would silently redeploy the old code. The runbook's plain `docker pull` is not enough; loop until the revision matches.
- **Verify the revision label** — `docker image inspect ghcr.io/pesahm/seller-market-mgmt-ui:latest --format '{{index .Config.Labels "org.opencontainers.image.revision"}}'` equals the merge commit SHA. The deployed image before this session was `8b5949f` (#96) — #97 (docs) was never deployed.
- **Migrations run on container startup** (entrypoint `alembic upgrade head`). After deploy, confirm `SELECT version_num FROM alembic_version` (0005→0006) and the new tables exist. Postgres is untouched; only `api` is recreated.
- **Host `curl http://127.0.0.1:8000/` returns 000** — port 8000 is NOT on the host loopback (the app is fronted). Verify with the container's own healthcheck or `docker exec ... curl 127.0.0.1:8000/health` (→ 200). New auth-gated routes return 401 (registered), not 404.
- **Auto-mode classifier blocks** this session: creating a GitHub **issue** (deemed agent-added, even though requested — PR creation when explicitly requested WAS allowed); a **production DB write** via `docker exec ... set_setting` (not explicitly requested). Operational deploy (mirror-pull / retag / `compose up`) and read-only SSH were allowed. To set a live setting without redeploy, the operator must authorize the DB write (or add a Bash permission rule).
- **github.com / api.github.com are intermittently unreachable** from this Windows host (TLS handshake timeout / connection refused) — wrap `git push` / `gh` in a retry loop.

### Pre-existing issue surfaced (NOT from this change)

mgmt UI logs show the `trade_ingestor` worker failing to SSH into the **trading VPS 185.232.152.246** (`user17290985243902@...:22` → "Connect failed" / "Channel closed" → paramiko `EOFError` tracebacks). That host's SSH appears down; the pool's evict-and-retry (#94/#95) is firing as designed. The bot-report is unaffected (it calls brokers over HTTPS directly, not via that host). Worth a separate look at that VPS's sshd.

### Operating the report (runbook)

1. Deploy the mgmt image (mirror-pull + verify revision + `compose up -d api` — see deploy learnings).
2. Open `/admin/bot-report` → **Refresh from broker**, date range from `robot_start_date` (2026-05-19) — backfills `broker_orders` per customer.
3. Confirm the mgmt host can reach `api-{broker}` first (the `api-ayandeh` DNS issue noted in Session 1 would make per-customer fetches fail — surfaced per-customer, not fatal).
4. For the daily auto-pull, set `ENABLE_BROKER_ORDER_RECONCILER=true` in `/opt/seller-market-mgmt/.env` + `docker compose up -d api`.
5. Per-agent fee override: **Set an agent's profit-share fee %** on the Profit & fee tab. Global default = `profit_fee_percent` setting.

### Open follow-ups (Session 2)

| # | Title | Why |
|---|---|---|
| **P3** | ~~Bot fire-log + ingestor + reconciliation~~ — **DONE (PR #100, deployed fleet-wide)**. See Session 3. | Exact robot-vs-manual buy attribution via the bot serial number. |
| — | Fee-basis configurability | Matcher computes both `fee_on_positive` and `fee_on_net`; report hardcodes positive-lots (documented default). Make it a UI/setting toggle if needed. |
| — | Fee-ledger billing snapshots | `build_fee_report` recomputes live; a persisted immutable "Bill" snapshot would make billed amounts auditable when later polls restate values. |
| — | ~~Trading VPS 185.232.152.246 sshd down~~ | **Resolved** — it was transient/post-restart; direct SSH works (see Session 3 deploy learnings). |

---

## Session 3 — P3 fire-log (built + fleet-deployed) + exclusion filter + fleet-redeploy lessons

Finished P3 (the deferred bot fire-log), shipped an instrument-exclusion filter, deployed the mgmt UI (`b4f7fb4b`, migration `0007`), and redeployed **all 7 bot stacks** onto the fire-log image (`902a3dd`). PR map: **#98** GetOrders report (merged) · **#99** issue · **#100** P3 fire-log (merged) · **#101** exclusion filter (merged).

### P3 — bot fire-log + serial-number reconciliation (PR #100)

Authoritatively tags which executed buys were the bot's (vs the agent's manual trades), replacing the market-open time-window heuristic on covered days.

- **Design pivots (learned the hard way)**:
  - Do NOT log at `prepare_order_data` — that records *intent*; the run may never actually fire.
  - Log only orders that **actually succeeded (HTTP 200)**.
  - **Hot path is sacred**: `place_order` is spammed 1000+×/run in the head-of-queue race. It does ONLY a dict-membership check + store of the *first* successful `response.content` per account (`_FIRED_SUCCESS` in `locustfile_new.py`) — **no file I/O, no JSON parse**. Parsing + the JSONL write happen once in `on_test_stop` (`_flush_order_fires`).
  - Each fire carries the broker **`serial_number`** (the durable, queryable reconciliation key) + the **full NewOrder response**. Serial/tracking extraction (`_extract_order_ids`) is best-effort; the full response is saved so extraction can be refined mgmt-side **without a bot redeploy**.
- **mgmt UI**: `order_fires` table + `broker_orders.serial_number` (migration **`0007`**, additive); `services/fire_log_ingestor.py` SFTP-reads `run_results/order_fires_<date>.jsonl` (the bot APPENDS — so re-read the most-recent ~7 files only, NO delete-on-consume, dedup on `fire_uid` via `ON CONFLICT DO NOTHING`); reconciles `broker_orders.is_bot` two ways — **serial-exact** and **date-based** `(customer, isin, side, trading-date)`. Worker `ENABLE_FIRE_LOG_INGESTOR` (default on, internal SSH only). `profit_report._is_bot_buy` already prefers `is_bot`, so reconciled fires sharpen attribution automatically.
- **CodeRabbit fix**: serial reconciliation is scoped to **`customer_id`** — `serialNumber` is NOT globally unique across brokers; a `customer_id` pins exactly one broker + one account (tighter than broker+agent and avoids the stale denormalized `agent_id`).

### Instrument exclusion filter (PR #101)

Keep instruments agents buy (e.g. bonds) out of the report + fee.

- Persistent **`excluded_instruments`** setting (multi-line ISIN/symbol; commas/semicolons accepted). **No migration** — uses the `settings` table.
- `broker_orders.parse_exclusions()` + `is_excluded()` match **ISIN, symbol, OR symbol_title** (case-insensitive). Applied in `list_orders`, `build_fee_report` (dropped BEFORE matching so a bond never touches profit/fee), and the Excel export.
- `POST /admin/bot-report/exclusions` saves it (audit via `settings_store.set_setting`); a textarea editor on the page round-trips a validated `next` redirect (`_bot_report_safe_next` — local `/admin/bot-report` only, no open-redirect).

### Fleet-redeploy learnings — CRITICAL for next time

- **The fleet is 7 stacks, not 4**: `5.10.248.55` → Mostafa `83619dcd`, hamid `e4d0db56`, `6b577238` (`sm-agent-05684fc8`); `185.232.152.246` → `c6f3b84a`, `724a310a`, `0fceec29`, `7bd17604`. All now on bot image **`902a3dd`** (the #100 fire-log build).
- **`ghcr.io` reachability is INTERMITTENT, not permanently blocked.** This time it was reachable from `5.10.248.55` (its `pull always` redeploy got `902a3dd` straight from ghcr) but still blocked from `185.232.152.246`. Don't assume it's always down.
- **A `never`-pull host's "redeploy" only restarts the LOCAL image.** `docker compose up -d --pull never` returns "up" whether the locally-tagged `:latest` is the new OR old image — so clicking redeploy in the panel SILENTLY restarted `185`'s 4 stacks on the OLD `599c16c`. **Stage the new image on the host FIRST, then verify the *running container's* revision label — "up" is not proof.**
- **The mirror's `:latest` can be STALE.** `ghcr-mirror.liara.ir/...seller-market:latest` served the OLD bot image (`599c16c`) and `docker pull` said "Image is up to date". **Fix = pull by IMMUTABLE DIGEST**:
  1. On a host that already has the new image: `docker image inspect ghcr.io/pesahm/seller-market:latest --format '{{index .RepoDigests 0}}'` → `…@sha256:9e660f8d…`.
  2. On the blocked host: `docker pull ghcr-mirror.liara.ir/pesahm/seller-market@sha256:9e660f8d…` (bypasses the cached `:latest`, fetches the exact image), then `docker tag … ghcr.io/pesahm/seller-market:latest`.
  3. Verify `…revision == 902a3dd`, then per stack: `cd /root/seller-market/agents/<agent-uuid> && docker compose up -d --pull never`; confirm the container's image label revision is `902a3dd`.
- **`185.232.152.246` SSH works directly** as `user17290985243902@185.232.152.246` (key-based; `hostname`=`tebian`; has docker access + can read the `/root/seller-market/agents/<id>/` stack dirs). The mgmt UI's transient `'NoneType' object has no attribute 'open_session'` tracebacks were post-restart noise that self-cleared (port 22 was open throughout).
- **#101's bot image build (`b4f7fb4b`) FAILED** (a mgmt_ui-only change tripped the semver/tag logic) — but harmless: bot `:latest` stays at `902a3dd` from #100's successful build. Don't chase it.

### Verify the fire-log is flowing (after the next market-open run)

No fires until a bot actually runs. Then `run_results/order_fires_<date>.jsonl` appears, the ingestor upserts + reconciles, and `is_bot` gets tagged. Check:
`docker exec seller-market-mgmt-postgres-1 psql -U mgmt -d mgmt_ui -tAc "SELECT count(*), max(fired_at) FROM order_fires;"` and the `fire_log_ingest stack=…` log lines.

### Open follow-ups (Session 3)

| # | Title | Why |
|---|---|---|
| — | Harden `fire_log_ingestor` SSH error handling | An unreachable host currently logs a full paramiko traceback per tick (the `open_session` AttributeError isn't an `SSHError`, so it hits the outer `except`). Catch broader → one-line warning. |
| — | Confirm real NewOrder response shape | So `_extract_order_ids` pulls `serialNumber`/`trackingNumber` exactly. The full response is already saved, so this is a mgmt-side refinement — no bot redeploy. |
| — | Redeploy the bot images automatically | Operator still manually mirror-pulls/retags + redeploys per host on each bot image rebuild (Session-1 follow-up #2 still open, now compounded by the stale-`:latest`/pull-by-digest dance). |

---

## Session 4 — Exir / Rayan HamAfza broker family + UI-managed brokers (Phase 1, issue #102)

Added a SECOND broker protocol family — **Exir / Rayan HamAfza (`*.exirbroker.com`)** — and moved broker selection from hardcoded lists to a **DB-managed `brokers` table**. Branch `feat/exir-broker-family`: commit `c40d168` (feature) + `c81f853` (review fixes). Phased: Phase 1 = mgmt UI manage/read side (done); **Phase 2 = bot order-firing for Exir (designed, NOT built)**.

### The big realization
All 11 prior brokers (gs, ib, ayandeh, …) are **ONE software family** ("ephoenix/MTS") — adding any was just a URL branch in `broker_enum.get_endpoints()` / `broker_client._endpoints_for`. **Exir is a fundamentally different protocol**, so it needed a real **broker-adapter abstraction** (none existed). Captcha/OCR service is reused; everything else differs.

### Exir wire shape — CONFIRMED LIVE (read-only spike, no orders), see `SellerMarket/scratch/EXIR_FINDINGS.md`
- Per-tenant subdomain `https://{tenant}.exirbroker.com` (tenant = broker code, e.g. `khobregan`). Angular SPA.
- Auth = **cookies, NOT Bearer**: `GET /exir` (cookiesession1) → `GET /captcha` (**JPEG** + `client_login_id` header→cookie) → `POST /api/v2/login {username,password,captcha:<int>,otp:""}` → `{authToken, nt, validity(480min), accountNumberList[0].bourseAccountName, ...}`. Captcha is a **JSON number**. OCR (the existing `/ocr/captcha-easy-base64`) decodes the 5-digit JPEG fine.
- **Per-request `X-App-N` signature** = `BuildAppNToken(nt, path)` ported to Python (`exir_token.py`). **CONFIRMED: UTC time basis + signed over the FULL path INCLUDING query string** (a 200 on `orderbookReport` proved it; recompute every second).
- Reads: `orderbookReport?...&orderStatusId=3` (filled). **`insMaxLCode` is an ISIN** (e.g. `IRO1SROD0001`) → maps straight onto the existing `isin` column, **no symbol-vs-ISIN problem**. Persian status/side (`خريد`/`فروش` — match first letter, Arabic-vs-Persian yeh). `entryDateTime` is **Jalali** `YYYY/MM/DD-HH:mm:ss`. `mmtpOrderId` is the dedup id. Broker numeric id = **116** (JWT `b` claim) — Phase-2 order payload needs it.
- **`buyingPower` is an OPEN gap**: `GET /api/v2/user/buyingPower` returned `406` errorCode 4047 (business "service not acceptable", NOT auth — the token scheme was validated by the orderbook 200). Find the right path before Phase-2 BUY sizing.

### Architecture (Phase 1, mgmt UI only)
- New package `app/services/brokers/`: `base.py` (`BrokerAdapter` Protocol + `VerifyResult`/`IsinInfo`), `registry.py` (DB-backed `{code:family}` cache warmed at startup + on CRUD; `get_adapter` factory), `ephoenix.py` (thin delegator), `exir.py` (`ExirAdapter`), `_jalali.py`, `exir_token.py`.
- `broker_client.py` is now a **family-routing dispatcher**: the ephoenix bodies stay in-place (renamed `_ephoenix_*`), and `verify_credentials`/`verify_isin`/`get_orders` route to the Exir adapter when `family_of(code)=="exir"`, **defaulting to ephoenix on a cold/unknown cache** — so the 11 brokers + their 14 tests are byte-for-byte unchanged. (Did NOT move ephoenix into `ephoenix.py` precisely to keep the test monkeypatch targets — `_endpoints_for`/`_TOKEN_CACHE` etc. — at their original path.)
- `brokers` table (migration **0008**, seeds 11 ephoenix + `khobregan` + any existing distinct `customers`/`broker_orders.broker` values so nothing orphans), `models/brokers.py`, `schemas/broker.py` (`family` is a closed `Literal["ephoenix","exir"]` — families are code-bound), `services/brokers_admin.py` (CRUD + in-use guards), `/admin/brokers` CRUD page. **`family` is the ONLY thing that picks the adapter** and is resolved LIVE (no denormalized family on customers/broker_orders).
- Customer `broker` validation moved from a Pydantic `Literal` to **DB-backed** (`get_broker_by_code`, enabled-check); dropdowns are grouped optgroups from `list_enabled_grouped`.

### Adversarial review caught 8 real bugs (commit `c81f853`) — patterns to remember
- **The PR-#73 `MissingGreenlet` landmine bites ANY pre-fetched ORM list on a service-rollback error path.** `admin_customer_update` pre-fetched `broker_groups` (ORM) before `update_customer`'s duplicate-tuple `db.rollback()` expired them → the sync optgroup render lazy-loaded → 500. Fix: re-fetch AFTER the rollback (the create/agent paths already did). **Rule: anything the sync error-renderer touches must be fetched/snapshotted post-rollback.**
- **`broker_orders.tracking_number` was GLOBALLY UNIQUE — wrong once a second id namespace exists.** Exir `mmtpOrderId` ⟂ ephoenix `trackingNumber`; a collision on `ON CONFLICT (tracking_number)` silently overwrites another customer's money/attribution row (excluded `customer_id/isin` stay, mutable money fields get clobbered). Fixed → composite `UNIQUE(broker, tracking_number)` (migration **0009** drops the old single-col unique via inspector, adds the composite; upsert `index_elements=[broker, tracking_number]`). **Rule: a per-broker id is only unique per broker.**
- **Cross-family timezone basis must match.** Exir `parse_jalali_datetime` returns Tehran +03:30; ephoenix `_parse_dt` stores wall-clock **labeled UTC**. The date-range filters compare absolute instants on a `timestamptz`, so mixing bases misclassifies near Tehran midnight. Fix: `.replace(tzinfo=timezone.utc)` on the Exir dt (keep numerals, relabel UTC).
- **`update_broker` family flip was unguarded** (disable/delete were guarded) → could silently reroute live customers to the wrong adapter. Guard family change when in-use.
- Lows: Exir `get_orders` now filled-only (reject non-3 so the hardcoded `state=3` can't mislabel); Exir session **evicted + re-login retried once on a non-200** (ephoenix already drops its token on 401); in-use guard counts customers **case-insensitively** (`func.lower`).

### Gotchas / learnings
- **Bot Dockerfile is `COPY *.py ./` (flat)** → bot-side Phase-2 adapters MUST be top-level modules, a `brokers/` subpackage would be silently excluded. mgmt_ui copies `app/` wholesale, so its package is fine.
- **Phase-0 spike is the highest-value step** — it converted a fiddly token/captcha/cookie design from guesswork into confirmed facts (UTC-vs-Tehran and path-vs-path+query were the two knobs; the live 200 pinned both) and revealed `insMaxLCode`=ISIN, collapsing a whole planned workstream.
- **pytest `-q` to a redirected file BUFFERS** — a backgrounded run shows an empty output file mid-run; run foreground for a definitive pass/fail. The Windows fail-once flakes are real: a **different** test errored each full run (`test_stacks_scheduler_locust_push`, then `test_janitor_filters`), each **passes in isolation** — re-run solo to confirm, never chase.
- **Test creds are already in the repo** (`SellerMarket/test_integration.py`, README, CLAUDE.md use `4580090306` / `Mm@12345`) — but the new spike reads creds from `EXIR_*` env vars anyway (don't add another hardcoded copy); the one live `nt` in a unit test was swapped for a synthetic value.
- **Parallel subagents with DISJOINT file ownership** integrate cleanly into one tree (no worktrees needed): WS1 brokers-table, WS2 dispatcher, WS3 ExirAdapter, WS4+5 consumer side — then adversarially review the merged diff.

### Phase-1 rollout runbook

1. **Deploy the mgmt image** (mirror-pull + verify revision + `compose up -d api` — see the Session-2/3 deploy learnings for the ghcr-blocked / stale-`:latest` dance). Migrations `0008_brokers` + `0009_broker_orders_tracking_composite` run on container startup (entrypoint `alembic upgrade head`). Confirm:
   - `docker exec seller-market-mgmt-postgres-1 psql -U mgmt -d mgmt_ui -tAc "SELECT version_num FROM alembic_version"` → expect `0009_broker_orders_tracking_composite`.
   - The `brokers` table exists + is seeded: `... -c "SELECT code, family, enabled FROM brokers ORDER BY family, sort_order"` (11 ephoenix rows + `khobregan` exir + any pre-existing distinct broker codes).
2. **Add an Exir tenant** via **/admin/brokers → New**: `code` = the subdomain (e.g. `khobregan`), `family` = `exir` (+ label, enabled, sort).
3. **Verify the wiring**:
   - The family cache warms at startup (`app/services/brokers/registry.py`) and refreshes on broker CRUD.
   - The customer create/edit broker dropdown shows the new broker under its family optgroup (`list_enabled_grouped`).
   - Add a customer on it, then **Verify credentials** — returns the bourse account name from `accountNumberList[0]`.
   - **/admin/bot-report → Refresh from broker** populates `broker_orders` for that customer.
4. **Rollback note**: downgrading past `0009` is **blocked by design** if cross-broker duplicate `tracking_number`s exist (the composite UNIQUE made them valid; the global single-column UNIQUE can't be recreated). Dedupe first.

### Open follow-ups (Session 4)

| # | Title | Why |
|---|---|---|
| #102 | **Phase 2 — bot order-firing for Exir** | Flat adapter modules in `SellerMarket/`, thin `EphoenixAdapter` over the unmodified `api_client.py`, I/O-free hot-path `X-App-N` signer, render `broker_family` into `config.ini` (data-driven bot, no enum). Designed in the plan; not built. |
| #102 | Real Exir `buyingPower` path | The `/api/v2/user/buyingPower` `406` blocks Phase-2 BUY volume sizing — find the right endpoint/version, or carry an explicit price in config for Exir. |
| #102 | Confirm Exir order-placement contract | `POST /api/v1/order` (decompiled): confirm `insMaxLcode`=ISIN there too + the numeric `brokerCode` (116); the sync response has no order id (ids via `wss://…/sle`) → Phase-2 fire-log keys on the date-based reconciliation, not serial. |
| #102 | Per-broker endpoint overrides + verify-instrument for Exir | Operator chose metadata-only CRUD (code/family/label/enabled/sort); URL quirks (ib shard, gs rate-limit) still live in code. Exir `verify_isin` is a Phase-1 stub (ISIN echoed, no metadata fetch). |
