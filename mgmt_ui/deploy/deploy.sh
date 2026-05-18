#!/usr/bin/env bash
# Seller-Market management UI — single-VPS deploy script.
#
# Usage on a clean Ubuntu/Debian VPS, as root or sudo user:
#
#     curl -fsSL https://raw.githubusercontent.com/PeSahm/Seller-Market/main/mgmt_ui/deploy/deploy.sh -o deploy.sh
#     chmod +x deploy.sh
#     ./deploy.sh
#
# The script is idempotent: re-running it pulls the latest image and
# restarts containers without touching existing secrets or the database.
# Only the first run prompts for an admin password and seeds the user.
#
# What it does:
#   1. Verifies docker + compose plugin + openssl + python3 are installed.
#   2. Prompts for image tag, host port, admin credentials.
#   3. Generates secrets (Postgres, JWT, CSRF, Fernet) on first run.
#   4. Writes /opt/seller-market-mgmt/{.env,docker-compose.yml} (chmod 600 on .env).
#   5. Prepares /var/lib/sm-mgmt/{postgres,ssh_keys,run_logs} with the
#      correct ownership for the non-root `app` user (uid 1000) inside
#      the container.
#   6. docker compose pull && up -d  (the image's entrypoint auto-runs
#      alembic upgrade head before starting uvicorn).
#   7. Waits for /health to return 200.
#   8. Seeds the initial admin (idempotent — exits cleanly if already exists).
#   9. Prints a summary with next-step pointers.

set -euo pipefail

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INSTALL_DIR="/opt/seller-market-mgmt"
DATA_DIR="/var/lib/sm-mgmt"
COMPOSE_FILE="${INSTALL_DIR}/docker-compose.yml"
ENV_FILE="${INSTALL_DIR}/.env"
# Where deploy.sh fetches its compose template from when run via curl|bash.
# Override by exporting COMPOSE_TEMPLATE_URL=file:///path/to/local/compose.yml
# for offline / dev testing.
COMPOSE_TEMPLATE_URL="${COMPOSE_TEMPLATE_URL:-https://raw.githubusercontent.com/PeSahm/Seller-Market/main/mgmt_ui/deploy/docker-compose.prod.yml}"

# The non-root user uid inside the image. The Dockerfile's `useradd --system`
# allocates dynamically — typically 1000 on Debian-slim, but not guaranteed.
# Override via environment if your image was built with a different uid/gid:
#   APP_UID=1001 APP_GID=1001 ./deploy.sh
APP_UID="${APP_UID:-1000}"
APP_GID="${APP_GID:-1000}"

# ANSI colours (no-op if stdout isn't a TTY).
if [ -t 1 ]; then
  C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
  C_BOLD=$'\033[1m'; C_RESET=$'\033[0m'
else
  C_RED=""; C_GREEN=""; C_YELLOW=""; C_BOLD=""; C_RESET=""
fi

say()  { printf '%s\n' "$*"; }
ok()   { printf '%s✓%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '%s⚠%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
die()  { printf '%s✗%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 1. Pre-flight
# ---------------------------------------------------------------------------

require_tool() {
  command -v "$1" >/dev/null 2>&1 || die "missing dependency: $1 (install with: $2)"
}

preflight() {
  say "${C_BOLD}1/9 Pre-flight checks${C_RESET}"
  require_tool docker  "apt-get install -y docker.io"
  # `docker compose` (plugin) — not the legacy `docker-compose` binary.
  if ! docker compose version >/dev/null 2>&1; then
    die "docker compose plugin missing (install with: apt-get install -y docker-compose-plugin)"
  fi
  require_tool openssl "apt-get install -y openssl"
  require_tool python3 "apt-get install -y python3"
  require_tool curl    "apt-get install -y curl"

  # /opt and /var/lib usually need root; check before we ask for any input.
  if [ "$(id -u)" -ne 0 ]; then
    die "deploy.sh must run as root (try: sudo ./deploy.sh)"
  fi

  # Verify cryptography is importable for Fernet key generation. python3 is
  # mostly always there; cryptography may not be. On PEP-668 systems (Ubuntu
  # 23.04+ / Debian 12+) bare `pip3 install` refuses with "externally-managed-
  # environment" — so we try plain pip, then pip with --break-system-packages,
  # then apt-get, and only `die` if all three fail.
  if ! python3 -c "from cryptography.fernet import Fernet" 2>/dev/null; then
    say "  python3 'cryptography' module missing — attempting install..."
    install_ok=0
    if command -v pip3 >/dev/null 2>&1; then
      pip3 install --quiet cryptography 2>/dev/null \
        || pip3 install --quiet --break-system-packages cryptography 2>/dev/null \
        || true
      python3 -c "from cryptography.fernet import Fernet" 2>/dev/null && install_ok=1
    fi
    if [ "$install_ok" -eq 0 ] && command -v apt-get >/dev/null 2>&1; then
      apt-get install -y --no-install-recommends python3-cryptography 2>/dev/null || true
      python3 -c "from cryptography.fernet import Fernet" 2>/dev/null && install_ok=1
    fi
    if [ "$install_ok" -eq 0 ]; then
      die "could not install the python3 cryptography module via pip3 or apt-get (try \`apt-get update\` first)"
    fi
  fi
  ok "all dependencies present"
}

# ---------------------------------------------------------------------------
# 2 & 3. Prompts + secret generation
# ---------------------------------------------------------------------------

# Load existing .env values into shell variables if present. Re-runs reuse
# everything from the original install; only the admin-seeding step is
# skipped on subsequent runs.
load_existing_env() {
  if [ -f "$ENV_FILE" ]; then
    say "${C_BOLD}existing install detected${C_RESET} at $ENV_FILE — reusing secrets"
    # shellcheck disable=SC1090
    set -a; . "$ENV_FILE"; set +a
    EXISTING_INSTALL=1
  else
    EXISTING_INSTALL=0
  fi
}

prompt_or_default() {
  local prompt="$1" default="${2:-}" var
  if [ -n "$default" ]; then
    read -r -p "$prompt [$default]: " var
    printf '%s' "${var:-$default}"
  else
    read -r -p "$prompt: " var
    printf '%s' "$var"
  fi
}

prompt_secret() {
  local prompt="$1" var confirm
  while :; do
    read -r -s -p "$prompt: " var; echo
    read -r -s -p "Confirm: " confirm; echo
    if [ "$var" = "$confirm" ] && [ -n "$var" ]; then
      printf '%s' "$var"
      return
    fi
    warn "passwords don't match (or empty), try again"
  done
}

is_valid_port() {
  # Numeric, 1-65535. Bash `[[ =~ ]]` and integer compare; no external deps.
  local v="$1"
  [[ "$v" =~ ^[0-9]+$ ]] && [ "$v" -ge 1 ] && [ "$v" -le 65535 ]
}

prompt_port() {
  local prompt="$1" default="$2" v
  while :; do
    v="$(prompt_or_default "$prompt" "$default")"
    if is_valid_port "$v"; then
      printf '%s' "$v"
      return
    fi
    warn "invalid port: '$v' (must be an integer in 1-65535)"
  done
}

gather_inputs() {
  say "${C_BOLD}2/9 Configuration${C_RESET}"

  MGMT_UI_IMAGE_TAG="${MGMT_UI_IMAGE_TAG:-$(prompt_or_default 'mgmt UI image tag' 'ghcr.io/pesahm/seller-market-mgmt-ui:latest')}"

  # Validate MGMT_HOST_PORT — invalid free-form text would otherwise blow up
  # later inside compose with a less actionable error.
  if [ -n "${MGMT_HOST_PORT:-}" ]; then
    if ! is_valid_port "$MGMT_HOST_PORT"; then
      die "MGMT_HOST_PORT='$MGMT_HOST_PORT' is not a valid TCP port (1-65535)"
    fi
  else
    MGMT_HOST_PORT="$(prompt_port 'host port for the UI' '8000')"
  fi

  if [ "$EXISTING_INSTALL" -eq 0 ]; then
    INITIAL_ADMIN_USERNAME="$(prompt_or_default 'initial admin username' 'admin')"
    INITIAL_ADMIN_PASSWORD="$(prompt_secret 'initial admin password (min 8 chars)')"
    if [ "${#INITIAL_ADMIN_PASSWORD}" -lt 8 ]; then
      die "password too short (need at least 8 characters)"
    fi
  fi
}

generate_secrets() {
  say "${C_BOLD}3/9 Secret generation${C_RESET}"

  # Only mint when not already in the env. ${VAR:-...} idiom is just a
  # shell-level guard; the actual secrets live in the .env file.
  # POSTGRES_PASSWORD is interpolated into DATABASE_URL — use hex output so it
  # contains no URL-reserved characters (base64 emits `+` and `/`).
  POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-$(openssl rand -hex 24)}"
  MGMT_SECRET_KEY="${MGMT_SECRET_KEY:-$(openssl rand -base64 48 | tr -d '\n')}"
  MGMT_CSRF_SECRET="${MGMT_CSRF_SECRET:-$(openssl rand -base64 48 | tr -d '\n')}"
  MGMT_FERNET_KEY_PART1="${MGMT_FERNET_KEY_PART1:-$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')}"

  ok "secrets ready (4 generated on first run, reused on re-run)"
}

# ---------------------------------------------------------------------------
# 4. Write .env (chmod 600)
# ---------------------------------------------------------------------------

write_env_file() {
  say "${C_BOLD}4/9 Writing $ENV_FILE${C_RESET}"
  install -d -m 0755 "$INSTALL_DIR"

  # Heredoc into a tmpfile, then atomic-mv. Prevents a half-written .env
  # being read by a concurrent docker compose call.
  local tmp
  tmp="$(mktemp "${ENV_FILE}.XXXXXX")"
  trap 'rm -f "$tmp"' EXIT

  cat > "$tmp" <<EOF
# Seller-Market mgmt UI — production environment.
# Generated by deploy.sh. Manage backups of this file separately from the DB
# (see mgmt_ui/README.md "Backup & key rotation"). Keep chmod 600.

MGMT_UI_IMAGE_TAG=${MGMT_UI_IMAGE_TAG}
MGMT_HOST_PORT=${MGMT_HOST_PORT}

# Database
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}

# JWT signing (access_token cookie + WS short-lived token)
MGMT_SECRET_KEY=${MGMT_SECRET_KEY}

# CSRF double-submit cookie HMAC
MGMT_CSRF_SECRET=${MGMT_CSRF_SECRET}

# Fernet keyset — part 1 lives in env. Part 2 (optional, defence-in-depth)
# can be added as a chmod-400 file path via MGMT_FERNET_KEY_PART2_PATH.
MGMT_FERNET_KEY_PART1=${MGMT_FERNET_KEY_PART1}

# Cookie security flag. False is fine for IP-based plain-HTTP access; flip to
# true when you put HTTPS in front (see README "Behind a reverse proxy").
COOKIE_SECURE=${COOKIE_SECURE:-false}

# Default OCR URL — admin can change in /admin/settings.
DEFAULT_OCR_SERVICE_URL=${DEFAULT_OCR_SERVICE_URL:-http://5.10.248.55:18080}
EOF

  mv -f "$tmp" "$ENV_FILE"
  trap - EXIT
  chmod 600 "$ENV_FILE"
  chown root:root "$ENV_FILE"
  ok "wrote $ENV_FILE (chmod 600)"
}

# ---------------------------------------------------------------------------
# 5. Write docker-compose.yml from the bundled template
# ---------------------------------------------------------------------------

write_compose_file() {
  say "${C_BOLD}5/9 Writing $COMPOSE_FILE${C_RESET}"
  case "$COMPOSE_TEMPLATE_URL" in
    file://*)
      cp "${COMPOSE_TEMPLATE_URL#file://}" "$COMPOSE_FILE"
      ;;
    http://*|https://*)
      curl -fsSL "$COMPOSE_TEMPLATE_URL" -o "$COMPOSE_FILE" \
        || die "failed to fetch compose template from $COMPOSE_TEMPLATE_URL"
      ;;
    *)
      die "COMPOSE_TEMPLATE_URL must be file://, http://, or https://"
      ;;
  esac
  chmod 644 "$COMPOSE_FILE"
  ok "wrote $COMPOSE_FILE"
}

# ---------------------------------------------------------------------------
# 6. Data directories
# ---------------------------------------------------------------------------

prepare_data_dirs() {
  say "${C_BOLD}6/9 Preparing data directories under $DATA_DIR${C_RESET}"

  install -d -m 0755 "$DATA_DIR"
  # Postgres data dir — owned by the postgres container's runtime uid (999
  # on alpine images). Setting wide-open here so postgres can chown it
  # itself on first boot; postgres's entrypoint fixes ownership.
  install -d -m 0700 "$DATA_DIR/postgres"

  # SSH keys + run logs — both owned by the app container's uid:gid so the
  # non-root runtime user can read/write without sudo. The user is created
  # in mgmt_ui/Dockerfile as system uid (typically 1000).
  install -d -m 0700 "$DATA_DIR/ssh_keys"
  install -d -m 0700 "$DATA_DIR/run_logs"
  chown -R "$APP_UID:$APP_GID" "$DATA_DIR/ssh_keys" "$DATA_DIR/run_logs"

  ok "data directories ready"
}

# ---------------------------------------------------------------------------
# 7 & 8. Pull, up, wait for health
# ---------------------------------------------------------------------------

deploy_containers() {
  say "${C_BOLD}7/9 Pulling images${C_RESET}"
  (cd "$INSTALL_DIR" && docker compose pull) || die "image pull failed"

  say "${C_BOLD}8/9 Starting containers${C_RESET}"
  (cd "$INSTALL_DIR" && docker compose up -d) || die "docker compose up failed"

  # Wait for /health on the host port. 30 s max — alembic on first start
  # against an empty DB is fast (<5 s), the cap is for slow VPS disks.
  say "  waiting for the API to report healthy..."
  local i=0
  until curl -fsS "http://127.0.0.1:${MGMT_HOST_PORT}/health" >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "$i" -ge 30 ]; then
      warn "health endpoint did not respond after 30 s"
      say "  inspect with: docker compose -f $COMPOSE_FILE logs api"
      die "deployment timed out waiting for /health"
    fi
    sleep 1
  done
  ok "API is responding on port $MGMT_HOST_PORT"
}

# ---------------------------------------------------------------------------
# 9. Seed the admin user
# ---------------------------------------------------------------------------

seed_admin() {
  if [ "$EXISTING_INSTALL" -eq 1 ]; then
    say "${C_BOLD}9/9 Admin seeding${C_RESET}"
    say "  skipped — existing install (seed_admin is idempotent but we already prompted only on first run)"
    return
  fi
  say "${C_BOLD}9/9 Seeding initial admin '${INITIAL_ADMIN_USERNAME}'${C_RESET}"
  # Pipe the password into stdin so it never appears in `ps aux` on either
  # the host or inside the container. seed_admin.py reads it via
  # --password-stdin (introduced alongside this change).
  (cd "$INSTALL_DIR" && printf '%s' "$INITIAL_ADMIN_PASSWORD" \
    | docker compose exec -T api \
        python -m scripts.seed_admin "$INITIAL_ADMIN_USERNAME" --password-stdin) \
    || die "admin seeding failed (run \`docker compose -f $COMPOSE_FILE logs api\` for details)"
  ok "admin user created"
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

print_summary() {
  # Best-effort public IP for the URL hint. Falls back to 'YOUR-IP' if the
  # outbound lookup fails (offline VPS, blocked egress).
  local public_ip
  public_ip="$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || echo 'YOUR-IP')"

  echo
  ok "Seller-Market mgmt UI deployed."
  echo
  say "  ${C_BOLD}URL:${C_RESET}        http://${public_ip}:${MGMT_HOST_PORT}/"
  if [ "$EXISTING_INSTALL" -eq 0 ]; then
    say "  ${C_BOLD}Admin:${C_RESET}      ${INITIAL_ADMIN_USERNAME}"
    say "              (password set during install — store it in your password manager)"
  fi
  say "  ${C_BOLD}Config:${C_RESET}     $ENV_FILE  (chmod 600)"
  say "  ${C_BOLD}Compose:${C_RESET}    $COMPOSE_FILE"
  say "  ${C_BOLD}Data:${C_RESET}       $DATA_DIR/{postgres,ssh_keys,run_logs}"
  echo
  say "  ${C_BOLD}Next steps:${C_RESET}"
  say "    1. Back up $ENV_FILE to a separate location (it has all secrets)."
  say "    2. Schedule pg_dump of the postgres volume (see README 'Backup & key rotation')."
  say "    3. To switch to HTTPS later, put Caddy/nginx in front and flip"
  say "       COOKIE_SECURE=true in $ENV_FILE."
  say "    4. Upgrade later with: cd $INSTALL_DIR && docker compose pull && docker compose up -d"
  echo
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
  preflight
  load_existing_env
  gather_inputs
  generate_secrets
  write_env_file
  write_compose_file
  prepare_data_dirs
  deploy_containers
  seed_admin
  print_summary
}

main "$@"
