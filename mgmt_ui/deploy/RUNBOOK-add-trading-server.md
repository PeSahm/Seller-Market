# Runbook: add a new trading VPS (bot host) to the fleet

How to onboard a new Iranian VPS so the mgmt UI can deploy bot stacks on it.
Validated on Tebyan6 (`185.232.152.39`, Ubuntu 24.04, ssh_user `bargozideh`).

You can do the WHOLE thing through the mgmt — **no local SSH / paramiko / key
install**: create the server row first (it stores the SSH password), then drive
every host-prep step via `run_command(server, …)` (the mgmt's pool TOFU-accepts
the new host key and logs in with the stored password; a `bargozideh`-style user
with passwordless sudo handles the privileged steps).

## ⚠️ THE GOTCHA THAT WILL BITE YOU: restart the mgmt api after `usermod -aG docker`

`usermod -aG docker <user>` only affects **new** SSH logins. The long-running
mgmt **uvicorn** process keeps a persistent SSH pool, and the leader's
**`service_probe` worker (every 5 min) connects to the new host as soon as the
server row exists — i.e. BEFORE you add the docker group.** That stale,
group-less session sticks in the pool (it evicts only on a transport error, NOT
on a permission-denied), so the operator's first dashboard **Redeploy** fails:

```
permission denied while trying to connect to the Docker daemon socket
at unix:///var/run/docker.sock
```

**Fix / prevention:** after `usermod -aG docker`, **restart BOTH mgmt api
containers** so the pool reconnects fresh (group-aware):

```bash
ssh root@5.10.248.55     'docker restart seller-market-mgmt-api-1'   # PouyanIt (leader)
ssh root@45.139.10.192   'docker restart seller-market-mgmt-api-1'   # ParsPack (standby)
```

Beware false "all good" signals: `test_connection` passes anyway (its docker
probe is client-only `docker --version`, no daemon socket), and a one-shot
`docker exec … python -c run_command(...)` works because each one-shot gets its
OWN fresh pool. **Neither catches the strand.** Only a real `redeploy_stack`
(or the dashboard Redeploy) after the restart proves it.

## Steps (all via `run_command` through the api container, unless noted)

1. **Create the server row** (stores the password, Fernet-encrypted; pins the
   host key TOFU on first connect):
   ```python
   from app.schemas.server import ServerCreatePassword
   from app.services import servers as svc
   data = ServerCreatePassword(name="TebyanN", host="<ip>", ssh_user="bargozideh",
       password="<pw>", ssh_port=22, base_dir="/root/seller-market/agents",
       image_pull_policy="never")          # never: ghcr is blocked / we pre-stage
   server = await svc.create_server(db, data, actor_id=None)
   ```

2. **Probe the host:** `hostname; id; (sudo -n true && echo SUDO_OK); grep PRETTY /etc/os-release`.
   Confirm passwordless sudo. Note the user's **primary group = the server name**
   (e.g. `Tebyan6`), NOT `bargozideh`.

3. **Probe egress** (`curl -s -o /dev/null -w '%{http_code}' --noproxy '*' --max-time 8`):
   `download.docker.com/`, `ghcr-mirror.liara.ir/v2/`, `marketdatagw.ephoenix.ir/`,
   `core.tadbirrlc.com/`, the OCR pool, the market-data sidecars (`:8077`).

4. **Base dir** (non-root user + hardened `/root`):
   ```bash
   sudo install -d -m 0755 -o bargozideh -g <PrimaryGroup> /root/seller-market/agents
   sudo chmod o+x /root          # traversal-into-/root only; contents keep perms
   ```
   (Or use `/home/<user>/seller-market/agents` to skip the `/root` dance — but the
   fleet convention is `/root/seller-market/agents`.)

5. **Tehran time:** `sudo timedatectl set-timezone Asia/Tehran`;
   `/etc/systemd/timesyncd.conf.d/10-iran.conf` → `NTP=ntp.time.ir`
   (`FallbackNTP=time.cloudflare.com time.google.com`); restart `systemd-timesyncd`.

6. **Docker.** Try the official installer; if `download.docker.com/linux/<distro>/gpg`
   returns **403** (it does on some Iranian providers — even though `/` is 200),
   `get.docker.com` fails at "add the apt repo". Fall back to the distro packages:
   ```bash
   sudo apt-get update -qq
   sudo DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose-v2
   sudo systemctl enable --now docker
   ```
   (Ubuntu 24.04 has both in its repos. On older Debian where `docker-compose-v2`
   is missing, copy the compose binary host-to-host — see the Session-10 notes.)

7. **Docker group:** `sudo usermod -aG docker bargozideh`.
   **→ then do "THE GOTCHA" restart above.**

8. **daemon.json** (fleet convention): `/etc/docker/daemon.json` =
   `{"registry-mirrors":["https://ghcr-mirror.liara.ir"],"dns":["78.157.42.101","217.218.155.155"]}`;
   `sudo systemctl restart docker`.

9. **Pre-stage the current fleet bot image by IMMUTABLE DIGEST** (so a stack
   deploys with `--pull never`). Get the digest from a host that has it:
   `docker image inspect ghcr.io/pesahm/seller-market:latest --format '{{index .RepoDigests 0}}'`,
   then on the new host:
   ```bash
   sudo docker pull ghcr-mirror.liara.ir/pesahm/seller-market@sha256:<digest>
   sudo docker tag  ghcr-mirror.liara.ir/pesahm/seller-market@sha256:<digest> ghcr.io/pesahm/seller-market:latest
   ```
   Verify it matches the fleet (revision label) and the expected code is baked in:
   ```bash
   sudo docker run --rm --entrypoint sh ghcr.io/pesahm/seller-market:latest \
     -c 'ls /app/runtime_config.py; grep -c marketdatagw /app/broker_enum.py'
   ```

10. **Restart both mgmt api containers** (THE GOTCHA — evicts the stale pre-group
    SSH pool connection so the dashboard deploy works).

11. **Verify:** `test_connection` → `ok / base_dir_writable / docker_version /
    clock`, then a real `redeploy_stack` (or dashboard Redeploy) of a stack on the
    new host → `status=up`, container on the fleet revision. A non-sudo
    `docker ps` for the ssh_user on a fresh connection should also work.

## Notes
- `marketdatagw.ephoenix.ir` is reachable from the Tebyan hosts (AS214751) but
  NOT from PouyanIt/ParsPack (the S29 routing block) — that's why bot stacks live
  on the Tebyan hosts and PouyanIt/ParsPack run mgmt/OCR/DB only.
- The bot image already bakes `marketdatagw` (no per-container live-patch) and the
  config.ini `[runtime]` reader (S33).
