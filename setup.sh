#!/bin/bash
#
# Claude Telegram Bot Setup Script
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="claude-telegram-bot"
ENV_FILE="$SCRIPT_DIR/.env"
SERVICE_FILE="$SCRIPT_DIR/$SERVICE_NAME.service"

echo "========================================"
echo "  Claude Telegram Bot Setup"
echo "========================================"
echo

# Check for python3
if ! command -v python3 &> /dev/null; then
    echo "Error: python3 is not installed."
    exit 1
fi

# Check for claude CLI
if ! command -v claude &> /dev/null; then
    echo "Warning: 'claude' CLI not found in PATH."
    echo "Make sure it's installed before running the bot."
    echo
fi

# Install Python dependencies
echo "[1/4] Installing Python dependencies..."
pip install -q -r "$SCRIPT_DIR/requirements.txt"
echo "Done."
echo

# Get Telegram token
echo "[2/4] Telegram Bot Configuration"
echo
echo "If you don't have a bot yet:"
echo "  1. Open Telegram and message @BotFather"
echo "  2. Send /newbot and follow the prompts"
echo "  3. Copy the API token you receive"
echo
read -p "Enter your Telegram Bot Token: " TELEGRAM_TOKEN

if [ -z "$TELEGRAM_TOKEN" ]; then
    echo "Error: Token cannot be empty."
    exit 1
fi

# Get chat ID
echo
echo "To get your Chat ID:"
echo "  1. Send any message to your bot in Telegram"
echo "  2. Visit: https://api.telegram.org/bot$TELEGRAM_TOKEN/getUpdates"
echo "  3. Look for \"chat\":{\"id\":XXXXXXXX} in the response"
echo
read -p "Enter your Chat ID: " CHAT_ID

if [ -z "$CHAT_ID" ]; then
    echo "Error: Chat ID cannot be empty."
    exit 1
fi

# Write .env file
echo
echo "[3/4] Saving configuration..."
cat > "$ENV_FILE" << EOF
TELEGRAM_TOKEN=$TELEGRAM_TOKEN
ALLOWED_CHAT_IDS=$CHAT_ID
EOF
chmod 600 "$ENV_FILE"
echo "Saved to $ENV_FILE"
echo

# Generate and install systemd service
echo "[4/4] Setting up systemd service..."
echo

# Generate service file with correct paths
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
ExecStart=$SCRIPT_DIR/venv/bin/python $SCRIPT_DIR/bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

if [ "$EUID" -ne 0 ]; then
    echo "Need sudo to install systemd service."
    sudo cp "$SERVICE_FILE" /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable "$SERVICE_NAME"
    sudo systemctl restart "$SERVICE_NAME"
else
    cp "$SERVICE_FILE" /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
    systemctl restart "$SERVICE_NAME"
fi

echo
echo "========================================"
echo "  Setup Complete!"
echo "========================================"
echo
echo "The bot is now running and will auto-start on boot."
echo
echo "Useful commands:"
echo "  Status:  sudo systemctl status $SERVICE_NAME"
echo "  Logs:    journalctl -u $SERVICE_NAME -f"
echo "  Stop:    sudo systemctl stop $SERVICE_NAME"
echo "  Restart: sudo systemctl restart $SERVICE_NAME"
echo
echo "Send a message to your bot in Telegram to test!"
