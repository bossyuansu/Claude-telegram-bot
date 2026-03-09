#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_FILE="$SCRIPT_DIR/claude-telegram-bot.service"
SERVICE_NAME="claude-telegram-bot.service"
TARGET_SERVICE_FILE="/etc/systemd/system/$SERVICE_NAME"

# Default behavior: hot-reload via SIGHUP (no downtime).
# Falls back to delayed restart if service file changed.
DELAY_SECONDS="${DELAY_SECONDS:-15}"
IMMEDIATE=false
RESTART=false

usage() {
    cat <<'EOF'
Usage: ./update-service.sh [--delay <seconds>] [--immediate] [--restart]

Options:
  --delay <seconds>   Delay before restart when scheduling via systemd-run (default: 15)
  --immediate         Restart service immediately (old behavior)
  --restart           Force full restart instead of hot reload
  -h, --help          Show this help

Default: sends SIGHUP for hot reload (no downtime, preserves state).
Use --restart when the service file itself changed or loader.py changed.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --delay)
            if [[ $# -lt 2 ]]; then
                echo "ERROR: --delay requires a value."
                exit 1
            fi
            DELAY_SECONDS="$2"
            shift 2
            ;;
        --immediate)
            IMMEDIATE=true
            shift
            ;;
        --restart)
            RESTART=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "ERROR: Unknown argument: $1"
            usage
            exit 1
            ;;
    esac
done

if ! [[ "$DELAY_SECONDS" =~ ^[0-9]+$ ]]; then
    echo "ERROR: Delay must be a non-negative integer, got: $DELAY_SECONDS"
    exit 1
fi

# Check if service file exists, if not generate it.
if [[ ! -f "$SERVICE_FILE" ]]; then
    echo "Generating service file..."
    CURRENT_USER="$(whoami)"
    cat > "$SERVICE_FILE" << EOF
[Unit]
Description=Claude Telegram Bot
After=network.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$SCRIPT_DIR
EnvironmentFile=$SCRIPT_DIR/.env
Environment="PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=$SCRIPT_DIR/venv/bin/python $SCRIPT_DIR/loader.py
ExecReload=/bin/kill -HUP \$MAINPID
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
fi

# Always sync the service file
sudo cp "$SERVICE_FILE" "$TARGET_SERVICE_FILE"
sudo systemctl daemon-reload

if [[ "$IMMEDIATE" == true ]]; then
    sudo systemctl restart "$SERVICE_NAME"
    echo "Service restarted immediately."
    sudo systemctl status "$SERVICE_NAME" --no-pager
    exit 0
fi

if [[ "$RESTART" == true ]]; then
    # Full restart via delayed systemd-run (for loader.py or service file changes)
    UNIT_NAME="claudebot-update-$(date +%s)"
    RESTART_CMD="systemctl restart '$SERVICE_NAME'"

    sudo systemd-run \
        --unit "$UNIT_NAME" \
        --on-active "${DELAY_SECONDS}s" \
        /bin/bash -lc "$RESTART_CMD"

    echo "Full restart scheduled in ${DELAY_SECONDS}s."
    echo "Timer: $UNIT_NAME.timer"
    sudo systemctl list-timers "$UNIT_NAME.timer" --all --no-pager
else
    # Hot reload: send SIGHUP to loader.py (no downtime)
    sudo systemctl reload "$SERVICE_NAME"
    echo "Hot reload sent (SIGHUP). Active tasks preserved."
fi

echo
echo "Current service status:"
sudo systemctl status "$SERVICE_NAME" --no-pager
