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
import sys
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

    # Reload api.py and transplant new routes onto the running FastAPI app
    _reload_api()

    # Re-bind API function references to the new code
    bot._reinit_api_refs()

    # Clear the reload flag
    bot._reload_requested = False

    print(f"[Loader] Hot reload complete. {len(state)} state keys preserved.", flush=True)
    return True


def _reload_api():
    """Reload api.py and graft new routes onto the running uvicorn app.

    uvicorn holds a reference to the original FastAPI `app` object, so we
    can't just swap modules.  Instead we:
      1. Save the live app + event loop from the current api module.
      2. importlib.reload(api) — gives us a fresh module with new routes.
      3. Copy any routes from the new app that the old app doesn't have.
      4. Put the live app + loop back so init_refs / WS keep working.
    """
    api_mod = sys.modules.get("api")
    if not api_mod:
        return

    live_app = getattr(api_mod, "app", None)
    live_loop = getattr(api_mod, "_ws_event_loop", None)
    live_clients = getattr(api_mod, "_ws_clients", None)
    live_lock = getattr(api_mod, "_ws_lock", None)
    live_buffer = getattr(api_mod, "_ws_buffer", None)
    live_seq = getattr(api_mod, "_ws_seq", None)

    try:
        importlib.reload(api_mod)
    except Exception as e:
        print(f"[Loader] api.py RELOAD FAILED: {e}", flush=True)
        traceback.print_exc()
        return

    new_app = api_mod.app

    if live_app and new_app is not live_app:
        # Collect existing route paths+methods on the live app
        existing = set()
        for route in live_app.routes:
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None)
            if path:
                existing.add((path, frozenset(methods) if methods else None))

        added = 0
        for route in new_app.routes:
            path = getattr(route, "path", None)
            methods = getattr(route, "methods", None)
            key = (path, frozenset(methods) if methods else None)
            if path and key not in existing:
                live_app.routes.append(route)
                added += 1

        if added:
            print(f"[Loader] Grafted {added} new API route(s).", flush=True)

        # Put the live app back so everything references the same object
        api_mod.app = live_app

    # Restore WS state that was lost on reload
    if live_loop is not None:
        api_mod._ws_event_loop = live_loop
    if live_clients is not None:
        api_mod._ws_clients = live_clients
    if live_lock is not None:
        api_mod._ws_lock = live_lock
    if live_buffer is not None:
        api_mod._ws_buffer = live_buffer
    if live_seq is not None:
        api_mod._ws_seq = live_seq

    print("[Loader] api.py reloaded.", flush=True)


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
