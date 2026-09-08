#!/usr/bin/env bash
# Self-hosted Telegram Bot API server — lifts the 20MB getFile download cap to 2000MB.
#
# Why this exists: the CLOUD Bot API refuses `getFile` for anything over 20MB ("Bad Request: file
# is too big"), regardless of how large an upload the user's Telegram client permitted. So a 40MB
# video reaches the bot as a message but can never be fetched for analysis. A local server raises
# the ceiling to 2000MB and, in --local mode, returns an absolute path on disk — no transfer at all.
#
# THE CATCH: a bot token can be logged in to exactly ONE API server. Migrating cloud -> local
# requires calling logOut on the cloud API first, and until the bot is repointed it receives
# nothing. `cutover` does that as one step and ROLLS BACK automatically if the local server does
# not answer, so the bot is never left stranded.
#
# Usage:
#   ./scripts/telegram-local-api.sh setup     # install unit, start server, verify (non-disruptive)
#   ./scripts/telegram-local-api.sh cutover   # migrate the bot to it (brief interruption)
#   ./scripts/telegram-local-api.sh rollback  # return the bot to the cloud API
#   ./scripts/telegram-local-api.sh status
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"
ENV_FILE="$ROOT/.env"
PORT="${TELEGRAM_LOCAL_API_PORT:-8081}"
LOCAL_BASE="http://127.0.0.1:${PORT}"
CLOUD_BASE="https://api.telegram.org"
DATA_DIR="${TELEGRAM_LOCAL_API_DIR:-/var/lib/telegram-bot-api}"
UNIT=/etc/systemd/system/telegram-bot-api.service

die() { echo "❌ $*" >&2; exit 1; }

load_env() {
  [ -f "$ENV_FILE" ] || die "no .env at $ENV_FILE"
  # shellcheck disable=SC1090
  set -a; . "$ENV_FILE"; set +a
  : "${TELEGRAM_TOKEN:?TELEGRAM_TOKEN missing from .env}"
  : "${TG_API_ID:?TG_API_ID missing from .env (get one at https://my.telegram.org)}"
  : "${TG_API_HASH:?TG_API_HASH missing from .env}"
}

api_ok() {  # api_ok <base> -> getMe succeeds
  curl -sf --max-time 10 "$1/bot${TELEGRAM_TOKEN}/getMe" 2>/dev/null | grep -q '"ok":true'
}

set_env_var() {  # set_env_var KEY VALUE  (idempotent; removes the key when VALUE is empty)
  local key="$1" val="${2:-}"
  local tmp; tmp="$(mktemp)"
  grep -vE "^${key}=" "$ENV_FILE" > "$tmp" || true
  [ -n "$val" ] && echo "${key}=${val}" >> "$tmp"
  cat "$tmp" > "$ENV_FILE"   # preserve the original file's mode/ownership
  rm -f "$tmp"
}

cmd_setup() {
  load_env
  command -v telegram-bot-api >/dev/null || die "telegram-bot-api not installed"

  echo "→ installing $UNIT (port $PORT, data $DATA_DIR)"
  sudo mkdir -p "$DATA_DIR"
  sudo chown "$USER" "$DATA_DIR"
  sudo tee "$UNIT" >/dev/null <<UNITEOF
[Unit]
Description=Telegram Bot API server (local, raises file limits to 2000MB)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
# --local makes getFile return an absolute path on disk instead of a download URL.
ExecStart=/usr/local/bin/telegram-bot-api --local \\
  --api-id=${TG_API_ID} --api-hash=${TG_API_HASH} \\
  --http-port=${PORT} --dir=${DATA_DIR} --temp-dir=${DATA_DIR}/temp
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNITEOF

  sudo systemctl daemon-reload
  sudo systemctl enable --now telegram-bot-api.service
  sleep 3
  systemctl is-active --quiet telegram-bot-api.service || {
    sudo journalctl -u telegram-bot-api.service -n 20 --no-pager
    die "telegram-bot-api failed to start"
  }
  # The server is up even before any bot logs in; a 404/401 on getMe is expected pre-cutover.
  curl -sf --max-time 5 -o /dev/null "$LOCAL_BASE/" 2>/dev/null \
    && echo "✅ local API server responding on $LOCAL_BASE" \
    || echo "✅ local API server running (not serving this bot until cutover)"
  echo
  echo "Next: ./scripts/telegram-local-api.sh cutover"
}

cmd_cutover() {
  load_env
  systemctl is-active --quiet telegram-bot-api.service || die "run 'setup' first — server not running"
  if [ "${TELEGRAM_API_BASE:-$CLOUD_BASE}" = "$LOCAL_BASE" ] && api_ok "$LOCAL_BASE"; then
    echo "✅ already on the local API"; exit 0
  fi

  # The docs say a token must be logOut of the cloud before moving to a local server, but this
  # server serves getMe/getUpdates/sendMessage for the token without it. logOut is destructive —
  # it locks the token out of the cloud API for ~10 minutes, so a failed cutover would strand the
  # bot. Only fall back to it if the local server actually refuses to serve the token.
  echo "→ checking whether the local server already serves this token"
  if ! api_ok "$LOCAL_BASE"; then
    echo "  it does not — logging out of the cloud API (token unusable there for ~10min)"
    curl -s --max-time 30 "$CLOUD_BASE/bot${TELEGRAM_TOKEN}/logOut" >/dev/null || true
    sleep 5
    api_ok "$LOCAL_BASE" || die "local server still will not serve the token; aborting before restart"
  else
    echo "  it does — skipping logOut, so rollback stays instant"
  fi

  echo "→ pointing the bot at $LOCAL_BASE"
  set_env_var TELEGRAM_API_BASE "$LOCAL_BASE"

  echo "→ restarting the bot"
  sudo systemctl restart claude-telegram-bot.service
  sleep 8

  if api_ok "$LOCAL_BASE" && systemctl is-active --quiet claude-telegram-bot.service; then
    echo "✅ cutover complete — downloads now up to 2000MB"
    exit 0
  fi

  echo "⚠️  local API did not answer — rolling back so the bot is not stranded"
  cmd_rollback
  die "cutover failed; bot returned to the cloud API"
}

cmd_rollback() {
  load_env
  echo "→ returning the bot to the cloud API"
  # No logOut here: if cutover never logged out of the cloud, the token is still valid there and
  # rollback is just an env change. Calling logOut on the local server would be the destructive
  # move, not the safe one.
  set_env_var TELEGRAM_API_BASE ""
  sudo systemctl restart claude-telegram-bot.service
  sleep 5
  echo "✅ bot returned to the cloud API (20MB download cap)"
}

cmd_status() {
  load_env
  echo "configured base : ${TELEGRAM_API_BASE:-$CLOUD_BASE}"
  echo -n "local server    : "; systemctl is-active telegram-bot-api.service 2>/dev/null || true
  echo -n "cloud getMe     : "; api_ok "$CLOUD_BASE" && echo "ok" || echo "no (expected after cutover)"
  echo -n "local getMe     : "; api_ok "$LOCAL_BASE" && echo "ok" || echo "no"
}

case "${1:-status}" in
  setup)    cmd_setup ;;
  cutover)  cmd_cutover ;;
  rollback) cmd_rollback ;;
  status)   cmd_status ;;
  *) die "usage: $0 {setup|cutover|rollback|status}" ;;
esac
