#!/bin/bash
set -e

cp /home/yuansu/claude-telegram-bot/claude-telegram-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl restart claude-telegram-bot
echo "Service updated and restarted."
systemctl status claude-telegram-bot --no-pager
