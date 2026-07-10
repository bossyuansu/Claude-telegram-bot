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
- **Goal Mode** - `/goal` decomposes a larger objective into milestones, iterates with assessment/verification, and records learnings
- **Question handling** - Interactive buttons for Claude's questions
- **Photo, video & file uploads** - Send images, videos, and files for Claude to analyze (up to 50MB)

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
| `/goal <description>` | Start an autonomous goal with milestones, verification, and learning |
| `/goal status` | Show current goal progress |
| `/goal plan` | Show milestone plan |
| `/goal pause` / `/goal resume` | Pause or resume the active goal |
| `/goal cancel` | Abandon the active goal and stop its subprocess |
| `/goal journal` | Show goal learnings |
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

4. Run:
   ```bash
   python bot.py
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

The bot routes Claude CLI calls by task type. General/implementation work uses `CLAUDE_GENERAL_MODEL` (default Opus); planning/decomposition/routing uses `CLAUDE_PLANNING_MODEL` (default Opus; `claude-fable-5` is a supported override). Both are overridable via their env vars.
```bash
export CLAUDE_GENERAL_MODEL=opus
export CLAUDE_PLANNING_MODEL=opus
```

## Goal Mode Manual Tests

After automated tests pass, use [GOAL_MODE_MANUAL_TESTS.md](GOAL_MODE_MANUAL_TESTS.md)
to validate the live `/goal` workflow against a disposable project.

## License

MIT
