#!/usr/bin/env python3
"""
Hot-reload loader for Claude Telegram Bot.

Entry point that owns the polling loop and global state.
On SIGHUP or /reload command, reloads bot.py without losing runtime state.

Usage:
    python loader.py          # Normal start
    kill -HUP <pid>           # Hot reload
    systemctl reload claude-telegram-bot  # Hot reload via systemd
"""

import importlib
import os
import signal
import time
import traceback

import bot

# Global state keys to preserve across reloads.
# These are the module-level variables in bot.py that hold runtime state.
_STATE_KEYS = [
    # Telegram poll cursor
    "last_update_id",
    # Session and process state
    "user_sessions", "pending_questions", "active_processes",
    "message_queue", "cancelled_sessions", "user_feedback_queue",
    # Autonomous task state
    "justdoit_active", "deepreview_active", "omni_active",
    # Scheduled tasks
    "scheduled_tasks", "_scheduled_tasks_lock", "_scheduler_generation",
    # Threading locks (must survive to prevent races)
    "session_locks", "session_locks_lock",
    "_sessions_file_lock", "_active_sessions_lock",
    # Debounce state
    "_save_sessions_last", "_save_sessions_dirty",
    # Telegram poll backoff
    "_tg_poll_failures",
    # API module reference
    "_api_module",
]


def _hot_reload(source="SIGHUP"):
    """Reload bot.py, preserving all runtime state."""
    print(f"[Loader] Hot reload triggered ({source})...", flush=True)

    # Snapshot state from current bot module
    state = {}
    for key in _STATE_KEYS:
        try:
            state[key] = getattr(bot, key)
        except AttributeError:
            pass

    # Reload the module (picks up new code from disk)
    try:
        importlib.reload(bot)
    except Exception as e:
        print(f"[Loader] RELOAD FAILED: {e}", flush=True)
        traceback.print_exc()
        # Restore state to the (unchanged) module just in case
        for key, val in state.items():
            setattr(bot, key, val)
        return False

    # Restore state into the freshly reloaded module
    for key, val in state.items():
        setattr(bot, key, val)

    # Re-bind API function references to the new code
    bot._reinit_api_refs()

    # Clear the reload flag
    bot._reload_requested = False

    print(f"[Loader] Hot reload complete. {len(state)} state keys preserved.", flush=True)
    return True


def _sighup_handler(signum, frame):
    """Handle SIGHUP for hot reload."""
    _hot_reload("SIGHUP")


def _graceful_shutdown(signum, frame):
    """Terminate child processes and flush state on SIGTERM/SIGINT."""
    print(f"[Loader] Received signal {signum}, shutting down...", flush=True)
    for key, proc in list(bot.active_processes.items()):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            print(f"[Loader] Terminated process {proc.pid} ({key})", flush=True)
        except Exception:
            pass
    try:
        bot.save_sessions(force=True)
        print("[Loader] Sessions saved.", flush=True)
    except Exception:
        pass
    print("[Loader] Shutdown complete.", flush=True)
    raise SystemExit(0)


def main():
    # Initialize the bot (load sessions, start API, etc.)
    bot.startup()

    # Register signal handlers (owned by loader, not bot)
    signal.signal(signal.SIGHUP, _sighup_handler)
    signal.signal(signal.SIGTERM, _graceful_shutdown)
    signal.signal(signal.SIGINT, _graceful_shutdown)

    print("[Loader] Polling loop started. Send SIGHUP to hot-reload.", flush=True)

    while True:
        # Check for /reload command
        if getattr(bot, "_reload_requested", False):
            _hot_reload("/reload command")

        updates = bot.get_updates(bot.last_update_id + 1)

        for update in updates:
            bot.last_update_id = update["update_id"]

            try:
                # Handle callback queries (button presses)
                if "callback_query" in update:
                    cb = update["callback_query"]
                    chat_id = cb["message"]["chat"]["id"]
                    if bot.is_allowed(chat_id):
                        bot.handle_callback_query(cb)
                    continue

                # Handle messages
                message = update.get("message", {})
                chat_id = message.get("chat", {}).get("id")

                if not chat_id:
                    continue

                if not bot.is_allowed(chat_id):
                    print(f"Unauthorized access attempt from chat_id: {chat_id}")
                    bot.send_message(chat_id, "Unauthorized. Your chat ID is not in the allowed list.")
                    continue

                text = message.get("text", "") or message.get("caption", "")

                # Handle photo uploads
                if message.get("photo"):
                    photo = message["photo"][-1]
                    file_id = photo.get("file_id")
                    bot.send_message(chat_id, "📷 _Downloading image..._")
                    local_path = bot.download_telegram_file(file_id, "image.jpg")
                    if local_path:
                        prompt = f"[User uploaded an image: {local_path}]\n\n"
                        prompt += text if text else "Please analyze this image."
                        print(f"Received photo from {chat_id}, saved to {local_path}")
                        bot.handle_message(chat_id, prompt)
                    else:
                        bot.send_message(chat_id, "❌ Failed to download image.")
                    continue

                # Handle document/file uploads
                if message.get("document"):
                    doc = message["document"]
                    file_id = doc.get("file_id")
                    file_name = doc.get("file_name", "file")
                    file_size = doc.get("file_size", 0)

                    if file_size > 50 * 1024 * 1024:
                        bot.send_message(chat_id, "❌ File too large. Maximum size is 50MB.")
                        continue

                    bot.send_message(chat_id, f"📄 _Downloading {file_name}..._")
                    local_path = bot.download_telegram_file(file_id, file_name)
                    if local_path:
                        prompt = f"[User uploaded a file: {local_path}]\n\n"
                        prompt += text if text else "Please analyze this file."
                        print(f"Received file from {chat_id}: {file_name}, saved to {local_path}")
                        bot.handle_message(chat_id, prompt)
                    else:
                        bot.send_message(chat_id, "❌ Failed to download file.")
                    continue

                # Skip if no text content
                if not text:
                    continue

                print(f"Received from {chat_id}: {text[:50]}...")

                # Handle commands
                if text.startswith("/"):
                    if bot.handle_command(chat_id, text):
                        continue

                # Handle regular messages
                bot.handle_message(chat_id, text)

            except Exception as e:
                print(f"Error processing update {update.get('update_id')}: {e}", flush=True)
                traceback.print_exc()

        time.sleep(1)


if __name__ == "__main__":
    main()
