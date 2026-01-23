# Claude Telegram Bot

A Telegram bot that provides access to Claude CLI with streaming responses, session management, and parallel task support.

## Features

### Main Bot (`bot.py`)
- **Streaming responses** - See Claude's response as it generates
- **Multi-session support** - Work on multiple projects simultaneously
- **Session persistence** - Resume conversations with `--resume`
- **Smart compaction** - Auto-summarizes context when hitting limits
- **Long message chunking** - Splits long responses into multiple messages
- **Parallel tasks** - Run tasks in different sessions concurrently
- **Question handling** - Interactive buttons for Claude's questions

### Userbot (`userbot.py`)
- **Readonly mode** - Can only read files, search code, answer questions
- **Auto-responds** in a specific chat as yourself (not as a bot)
- **Security guardrails** - Won't read .env or output secrets
- **Streaming** - Same streaming support as main bot

## Commands

| Command | Description |
|---------|-------------|
| `/new <project>` | Start new session in ~/project |
| `/sessions` | List all sessions |
| `/resume` | Pick a session to resume |
| `/switch <name>` | Switch to session by name |
| `/reset` | Clear conversation history (fresh start) |
| `/delete <name>` | Delete a session |
| `/status` | Show current session info |
| `/cancel` | Cancel current task |
| `/plan` | Ask Claude to enter plan mode |
| `/approve` | Approve current plan |
| `/reject` | Reject current plan |

## Setup

1. Clone the repo
2. Create virtual environment:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. Create `.env` file:
   ```
   TELEGRAM_TOKEN=your_bot_token
   ALLOWED_CHAT_IDS=your_chat_id
   PROJECTS_DIR=/home/user
   ```

4. For userbot, also add:
   ```
   TG_API_ID=your_api_id
   TG_API_HASH=your_api_hash
   TARGET_CHAT_ID=chat_to_monitor
   ```

5. Run:
   ```bash
   python bot.py      # Main bot
   python userbot.py  # Userbot
   ```

## Systemd Service

Use the setup script to automatically install as a service:
```bash
./setup.sh
```

Or manually:
```bash
sudo systemctl enable claude-telegram-bot
sudo systemctl start claude-telegram-bot
```

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `TELEGRAM_TOKEN` | Bot token from @BotFather | Required |
| `ALLOWED_CHAT_IDS` | Comma-separated allowed user IDs | All users |
| `PROJECTS_DIR` | Base directory for projects | `~` |
| `CLAUDE_ALLOWED_TOOLS` | Tools Claude can use | All tools |

### Claude CLI

The bot uses your system's Claude CLI configuration. Set model with:
```bash
claude config set model claude-opus-4-5-20250514
```

## License

MIT
