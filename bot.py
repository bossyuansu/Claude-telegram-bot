#!/usr/bin/env python3
"""
Telegram bot that forwards messages to Claude CLI with session support.
Supports interactive prompts, plan mode, and multiple working directories.
"""

import os
import re
import subprocess
import requests
import time
import json
import threading
import uuid
import select
import ctypes
from pathlib import Path
from datetime import datetime, timedelta

# Force glibc to release free heap pages back to OS
try:
    _libc = ctypes.CDLL("libc.so.6")
    def _malloc_trim():
        _libc.malloc_trim(0)
except Exception:
    def _malloc_trim():
        pass

# Configuration
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN_HERE")
ALLOWED_CHAT_IDS = os.environ.get("ALLOWED_CHAT_IDS", "").split(",")
BASE_PROJECTS_DIR = os.environ.get("PROJECTS_DIR", os.path.expanduser("~"))

# Pre-approved tools for Claude CLI (Option A: avoid permission prompts)
CLAUDE_ALLOWED_TOOLS = os.environ.get(
    "CLAUDE_ALLOWED_TOOLS",
    "Write,Edit,Bash,Read,Glob,Grep,Task,WebFetch,WebSearch,NotebookEdit,TodoWrite"
)

# Codex model for JustDoIt orchestration (update when newer models release)
CODEX_MODEL = os.environ.get("CODEX_MODEL", "gpt-5.2-codex")

API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
DATA_DIR = Path(__file__).parent / "data"
SESSIONS_FILE = DATA_DIR / "sessions.json"
ACTIVE_TASKS_FILE = DATA_DIR / "active_tasks.json"  # Track running tasks for crash recovery
ACTIVE_SESSIONS_FILE = DATA_DIR / "active_sessions.json"  # Track running Claude processes for crash recovery
UPLOADS_DIR = DATA_DIR / "uploads"  # Directory for downloaded files

last_update_id = 0

# In-memory state
user_sessions = {}  # chat_id -> {sessions: [], active: session_id}
pending_questions = {}  # chat_id -> {questions: [], answers: {}, current_idx: 0, session}
active_processes = {}  # session_id -> subprocess.Popen (allows parallel sessions)
message_queue = {}  # session_id -> [queued messages]
justdoit_active = {}  # "chat_id:session_id" -> {"active": True, "task": str, "step": int, "chat_id": str}
session_locks = {}  # session_id -> threading.Lock (prevents race conditions)
session_locks_lock = threading.Lock()  # protects session_locks dict itself


def save_active_tasks():
    """Persist active justdoit tasks to disk for crash recovery detection."""
    try:
        tasks = {}
        for jdi_key, state in justdoit_active.items():
            if state.get("active"):
                tasks[jdi_key] = {
                    "started": time.time(),
                    "task": state.get("task", "")[:200],
                    "step": state.get("step", 0),
                    "phase": state.get("phase", ""),
                    "chat_id": state.get("chat_id", ""),
                    "session_name": state.get("session_name", ""),
                }
        DATA_DIR.mkdir(exist_ok=True)
        if tasks:
            with open(ACTIVE_TASKS_FILE, "w") as f:
                json.dump(tasks, f)
        else:
            # No active tasks — remove the file
            if ACTIVE_TASKS_FILE.exists():
                ACTIVE_TASKS_FILE.unlink()
    except Exception as e:
        print(f"Error saving active tasks: {e}")


def clear_active_tasks():
    """Clear the active tasks file (called when all tasks are done)."""
    try:
        if ACTIVE_TASKS_FILE.exists():
            ACTIVE_TASKS_FILE.unlink()
    except Exception:
        pass


# --- Active sessions tracking (crash recovery for ALL sessions) ---

_active_sessions_lock = threading.Lock()


def _save_active_sessions_file(sessions_dict):
    """Write active sessions dict to disk (caller must hold _active_sessions_lock)."""
    try:
        DATA_DIR.mkdir(exist_ok=True)
        with open(ACTIVE_SESSIONS_FILE, "w") as f:
            json.dump(sessions_dict, f)
    except Exception as e:
        print(f"Error saving active sessions: {e}")


def mark_session_active(chat_id, session_name, session_id, prompt):
    """Record that a Claude process is running for this session."""
    with _active_sessions_lock:
        try:
            if ACTIVE_SESSIONS_FILE.exists():
                with open(ACTIVE_SESSIONS_FILE) as f:
                    data = json.load(f)
            else:
                data = {}
        except Exception:
            data = {}
        data[session_id] = {
            "chat_id": str(chat_id),
            "session_name": session_name,
            "prompt": prompt[:200],
            "started": time.time(),
        }
        _save_active_sessions_file(data)


def mark_session_done(session_id):
    """Remove a session from the active tracking file."""
    with _active_sessions_lock:
        try:
            if not ACTIVE_SESSIONS_FILE.exists():
                return
            with open(ACTIVE_SESSIONS_FILE) as f:
                data = json.load(f)
            data.pop(session_id, None)
            if data:
                _save_active_sessions_file(data)
            else:
                ACTIVE_SESSIONS_FILE.unlink(missing_ok=True)
        except Exception as e:
            print(f"Error clearing active session {session_id}: {e}")


def check_interrupted_sessions():
    """On startup, check if any sessions were interrupted by a crash and notify users."""
    if not ACTIVE_SESSIONS_FILE.exists():
        return

    try:
        with open(ACTIVE_SESSIONS_FILE) as f:
            data = json.load(f)

        if not data:
            return

        # Group by chat_id
        chat_notifications = {}
        for sid, info in data.items():
            chat_id = info.get("chat_id")
            if not chat_id:
                continue
            chat_notifications.setdefault(chat_id, []).append(info)

        for chat_id, infos in chat_notifications.items():
            msg = "⚠️ *Bot crashed and restarted* — interrupted sessions:\n"
            for info in infos:
                name = info.get("session_name", "unknown")
                prompt = info.get("prompt", "")
                msg += f"\n• *{name}*: _{prompt[:100]}_"
            msg += "\n\n_Sessions preserved — send a message to continue._"
            try:
                send_message(int(chat_id), msg)
            except Exception as e:
                print(f"Error notifying {chat_id} about interrupted sessions: {e}")

    except Exception as e:
        print(f"Error checking interrupted sessions: {e}")
    finally:
        try:
            ACTIVE_SESSIONS_FILE.unlink(missing_ok=True)
        except Exception:
            pass


# --- Memory pressure check ---

def get_available_memory_mb():
    """Get available system memory in MB from /proc/meminfo."""
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024
    except Exception:
        pass
    return 99999  # assume plenty if we can't read


def check_memory_pressure():
    """Return True if there's enough memory to launch a new Claude process.
    Returns (ok, available_mb)."""
    available = get_available_memory_mb()
    # Each Claude CLI process can use 300-500MB. Require at least 1GB free.
    return available >= 1024, available


def check_interrupted_tasks():
    """On startup, check if justdoit tasks were interrupted by a crash and notify users."""
    if not ACTIVE_TASKS_FILE.exists():
        return

    try:
        with open(ACTIVE_TASKS_FILE) as f:
            tasks = json.load(f)

        if not tasks:
            return

        # Group by chat_id
        chat_notifications = {}
        for key, info in tasks.items():
            chat_id = info.get("chat_id")
            if not chat_id:
                continue
            if chat_id not in chat_notifications:
                chat_notifications[chat_id] = []
            chat_notifications[chat_id].append(info)

        for chat_id, infos in chat_notifications.items():
            msg = "⚠️ *Bot crashed and restarted* — interrupted tasks:\n"
            for info in infos:
                task_desc = info.get("task", "unknown task")
                session_name = info.get("session_name", "unknown")
                step = info.get("step", "?")
                phase = info.get("phase", "")
                msg += f"\n• *{session_name}* JustDoIt step {step}"
                if phase:
                    msg += f" ({phase})"
                msg += f": _{task_desc[:100]}_"
            msg += "\n\n_Sessions preserved. Use `/justdoit` to restart or send a message to continue manually._"
            try:
                send_message(int(chat_id), msg)
            except Exception as e:
                print(f"Error notifying {chat_id} about interrupted tasks: {e}")

    except Exception as e:
        print(f"Error checking interrupted tasks: {e}")
    finally:
        clear_active_tasks()


def get_session_lock(session_id):
    """Get or create a threading.Lock for a given session_id."""
    with session_locks_lock:
        if session_id not in session_locks:
            session_locks[session_id] = threading.Lock()
        return session_locks[session_id]


def download_telegram_file(file_id, filename=None):
    """Download a file from Telegram and return the local path."""
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

    try:
        # Get file path from Telegram
        resp = requests.get(f"{API_URL}/getFile", params={"file_id": file_id}, timeout=30)
        file_info = resp.json().get("result", {})
        file_path = file_info.get("file_path")

        if not file_path:
            return None

        # Download the file
        download_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"
        resp = requests.get(download_url, timeout=60)

        if resp.status_code != 200:
            return None

        # Determine filename
        if not filename:
            filename = file_path.split("/")[-1]

        # Save to uploads directory with timestamp to avoid collisions
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        local_path = UPLOADS_DIR / f"{timestamp}_{filename}"

        with open(local_path, "wb") as f:
            f.write(resp.content)

        return str(local_path)
    except Exception as e:
        print(f"Error downloading file: {e}")
        return None


def load_sessions():
    """Load sessions from disk."""
    global user_sessions
    DATA_DIR.mkdir(exist_ok=True)
    if SESSIONS_FILE.exists():
        try:
            with open(SESSIONS_FILE) as f:
                user_sessions = json.load(f)
        except Exception as e:
            print(f"Error loading sessions: {e}")
            user_sessions = {}


def save_sessions():
    """Save sessions to disk."""
    DATA_DIR.mkdir(exist_ok=True)
    try:
        with open(SESSIONS_FILE, "w") as f:
            json.dump(user_sessions, f, indent=2)
    except Exception as e:
        print(f"Error saving sessions: {e}")


_tg_poll_failures = 0

def get_updates(offset=0):
    """Poll for new messages and callback queries with timeout backoff."""
    global _tg_poll_failures
    try:
        resp = requests.get(
            f"{API_URL}/getUpdates",
            params={"offset": offset, "timeout": 30},
            timeout=(10, 40)  # connect/read
        )
        resp.raise_for_status()
        _tg_poll_failures = 0
        return resp.json().get("result", [])
    except requests.exceptions.ReadTimeout:
        _tg_poll_failures = min(_tg_poll_failures + 1, 10)
        if _tg_poll_failures % 5 == 0:
            print(f"Telegram getUpdates read timeout x{_tg_poll_failures}; backing off")
        time.sleep(min(2 ** min(_tg_poll_failures, 4), 15))
        return []
    except Exception as e:
        _tg_poll_failures = min(_tg_poll_failures + 1, 10)
        print(f"Error getting updates (#{_tg_poll_failures}): {e}")
        time.sleep(min(2 ** min(_tg_poll_failures, 4), 15))
        return []


def send_message(chat_id, text, reply_markup=None, parse_mode="Markdown"):
    """Send a message back to the user. Returns message_id."""
    max_len = 4000
    chunks = [text[i:i + max_len] for i in range(0, len(text), max_len)]
    message_id = None

    for i, chunk in enumerate(chunks):
        payload = {"chat_id": chat_id, "text": chunk}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        # Only add reply_markup to last chunk
        if reply_markup and i == len(chunks) - 1:
            payload["reply_markup"] = reply_markup

        try:
            resp = requests.post(f"{API_URL}/sendMessage", json=payload, timeout=30)
            result = resp.json()
            if not result.get("ok") and parse_mode:
                # Retry without markdown
                payload.pop("parse_mode", None)
                resp = requests.post(f"{API_URL}/sendMessage", json=payload, timeout=30)
                result = resp.json()
            if result.get("ok") and i == 0:
                message_id = result.get("result", {}).get("message_id")
        except Exception as e:
            print(f"Error sending message: {e}")

    return message_id


_last_edit_time = {}  # message_id -> timestamp
_last_edit_cleanup = 0  # timestamp of last cleanup
EDIT_MIN_INTERVAL = 1.0  # Minimum seconds between edits to the same message


def edit_message(chat_id, message_id, text, parse_mode="Markdown", force=False):
    """Edit an existing message. Rate-limited to 1 edit/sec per message."""
    global _last_edit_cleanup

    if not message_id:
        if force:
            # No message_id but forced — send as new message instead
            send_message(chat_id, text, parse_mode=parse_mode)
        return

    # Rate-limit edits per message (skip unless forced, e.g. final update)
    now = time.time()
    if not force and message_id in _last_edit_time:
        elapsed = now - _last_edit_time[message_id]
        if elapsed < EDIT_MIN_INTERVAL:
            return
    _last_edit_time[message_id] = now

    # Periodically purge stale entries (older than 10 minutes)
    if now - _last_edit_cleanup > 300:
        _last_edit_cleanup = now
        cutoff = now - 600
        stale = [k for k, v in _last_edit_time.items() if v < cutoff]
        for k in stale:
            del _last_edit_time[k]

    # Truncate if too long
    if len(text) > 4000:
        text = text[:3997] + "..."

    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode

    # Retry logic for forced edits (final updates that must reach the user)
    max_attempts = 3 if force else 1

    for attempt in range(max_attempts):
        try:
            resp = requests.post(f"{API_URL}/editMessageText", json=payload, timeout=30)
            result = resp.json()
            if not result.get("ok"):
                error_desc = result.get("description", "")
                if "message is not modified" in error_desc:
                    return  # Expected when content hasn't changed
                elif parse_mode:
                    # Retry without markdown if parsing fails
                    payload.pop("parse_mode", None)
                    resp2 = requests.post(f"{API_URL}/editMessageText", json=payload, timeout=30)
                    result2 = resp2.json()
                    if not result2.get("ok") and force:
                        print(f"edit_message failed even without markdown (msg_id={message_id}): {result2.get('description')}", flush=True)
                elif force:
                    print(f"edit_message failed (msg_id={message_id}): {error_desc}", flush=True)
                else:
                    return
            else:
                return  # Success
        except Exception as e:
            print(f"edit_message exception (msg_id={message_id}, attempt {attempt+1}/{max_attempts}): {e}", flush=True)
            if attempt < max_attempts - 1:
                time.sleep(2)  # Wait before retry

    # All retries exhausted — fall back to sending a new message
    if force:
        print(f"edit_message: all retries failed for msg_id={message_id}, falling back to send_message", flush=True)
        send_message(chat_id, text, parse_mode=parse_mode)


def send_document(chat_id, file_path, caption=None):
    """Send a file to the user via Telegram."""
    try:
        with open(file_path, "rb") as f:
            payload = {"chat_id": chat_id}
            if caption:
                payload["caption"] = caption[:1024]
            resp = requests.post(
                f"{API_URL}/sendDocument",
                data=payload,
                files={"document": (os.path.basename(file_path), f)},
                timeout=120
            )
            result = resp.json()
            if not result.get("ok"):
                print(f"send_document failed: {result.get('description')}", flush=True)
            return result.get("ok", False)
    except Exception as e:
        print(f"Error sending document: {e}", flush=True)
        return False


def send_photo(chat_id, file_path, caption=None):
    """Send a photo to the user via Telegram."""
    try:
        with open(file_path, "rb") as f:
            payload = {"chat_id": chat_id}
            if caption:
                payload["caption"] = caption[:1024]
            resp = requests.post(
                f"{API_URL}/sendPhoto",
                data=payload,
                files={"photo": (os.path.basename(file_path), f)},
                timeout=120
            )
            result = resp.json()
            if not result.get("ok"):
                # Fall back to sendDocument for unsupported image formats
                print(f"send_photo failed, falling back to document: {result.get('description')}", flush=True)
                return send_document(chat_id, file_path, caption=caption)
            return result.get("ok", False)
    except Exception as e:
        print(f"Error sending photo, falling back to document: {e}", flush=True)
        return send_document(chat_id, file_path, caption=caption)


def send_typing(chat_id):
    """Send typing indicator."""
    try:
        requests.post(f"{API_URL}/sendChatAction",
                     json={"chat_id": chat_id, "action": "typing"}, timeout=10)
    except:
        pass


def answer_callback_query(callback_query_id, text=None):
    """Answer a callback query."""
    try:
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        requests.post(f"{API_URL}/answerCallbackQuery", json=payload, timeout=10)
    except Exception as e:
        print(f"Error answering callback: {e}")


def edit_message_reply_markup(chat_id, message_id, reply_markup=None):
    """Remove inline keyboard after selection."""
    try:
        requests.post(f"{API_URL}/editMessageReplyMarkup",
                     json={"chat_id": chat_id, "message_id": message_id,
                           "reply_markup": reply_markup}, timeout=10)
    except:
        pass


def create_inline_keyboard(options):
    """Create Telegram inline keyboard from options."""
    keyboard = []
    for i, opt in enumerate(options):
        label = opt.get("label", opt) if isinstance(opt, dict) else str(opt)
        # Truncate long labels
        if len(label) > 60:
            label = label[:57] + "..."
        keyboard.append([{"text": label, "callback_data": f"opt_{i}"}])
    # Add "Other" option for custom input
    keyboard.append([{"text": "📝 Other (type custom response)", "callback_data": "opt_other"}])
    return {"inline_keyboard": keyboard}


def send_pending_question(chat_id, pending):
    """Send the current pending question to the user."""
    idx = pending.get("current_idx", 0)
    questions = pending.get("questions", [])
    if idx < len(questions):
        q = questions[idx]
        keyboard = create_inline_keyboard(q.get("options", []))
        total = len(questions)
        header = q.get("header", "Question")
        if total > 1:
            header = f"{header} ({idx + 1}/{total})"
        send_message(chat_id, f"*{header}*\n\n{q['question']}", reply_markup=keyboard)


def set_pending_questions(chat_id, questions, session):
    """Set up pending questions state and send the first one."""
    print(f"[DEBUG] set_pending_questions called with {len(questions)} questions", flush=True)
    chat_key = str(chat_id)
    pending_questions[chat_key] = {
        "questions": questions,
        "answers": {},
        "current_idx": 0,
        "session": session,
    }
    send_pending_question(chat_id, pending_questions[chat_key])


def parse_claude_output(output):
    """Parse Claude's JSON stream output for interactive elements."""
    messages = []
    questions = []
    file_changes = []  # Track file modifications
    tool_results = {}  # Track tool results by id
    processed_tool_ids = set()  # Track processed tool_use IDs to avoid duplicates

    for line in output.strip().split("\n"):
        if not line.strip():
            continue
        try:
            data = json.loads(line)
            msg_type = data.get("type")

            if msg_type == "assistant":
                # Regular text response
                content = data.get("message", {}).get("content", [])
                for block in content:
                    if block.get("type") == "text":
                        messages.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        tool_name = block.get("name")
                        tool_input = block.get("input", {})
                        tool_id = block.get("id")

                        # Skip if we've already processed this tool_use
                        if tool_id and tool_id in processed_tool_ids:
                            continue
                        if tool_id:
                            processed_tool_ids.add(tool_id)

                        if tool_name == "AskUserQuestion":
                            questions.extend(tool_input.get("questions", []))
                        elif tool_name == "ExitPlanMode":
                            print(f"[DEBUG] parse_claude_output ExitPlanMode tool_id={tool_id}, current questions={len(questions)}", flush=True)
                            questions.append({
                                "question": "Plan is ready. Do you approve this plan?",
                                "header": "Plan Approval",
                                "options": [
                                    {"label": "✅ Approve", "description": "Proceed with implementation"},
                                    {"label": "❌ Reject", "description": "Revise the plan"},
                                ]
                            })
                        elif tool_name == "EnterPlanMode":
                            messages.append("📋 Entering plan mode...")
                        elif tool_name == "Write":
                            file_path = tool_input.get("file_path", "unknown")
                            file_changes.append({
                                "type": "create",
                                "path": file_path,
                                "tool_id": tool_id
                            })
                        elif tool_name == "Edit":
                            file_path = tool_input.get("file_path", "unknown")
                            old_str = tool_input.get("old_string", "")[:50]
                            new_str = tool_input.get("new_string", "")[:50]
                            file_changes.append({
                                "type": "edit",
                                "path": file_path,
                                "old": old_str,
                                "new": new_str,
                                "tool_id": tool_id
                            })
                        elif tool_name == "Bash":
                            cmd = tool_input.get("command", "")
                            if cmd and len(cmd) < 100:
                                file_changes.append({
                                    "type": "bash",
                                    "command": cmd,
                                    "tool_id": tool_id
                                })
                        elif tool_name == "Read":
                            file_path = tool_input.get("file_path", "unknown")
                            file_changes.append({
                                "type": "read",
                                "path": file_path,
                                "tool_id": tool_id
                            })

            elif msg_type == "user":
                # Tool results
                content = data.get("message", {}).get("content", [])
                for block in content:
                    if block.get("type") == "tool_result":
                        tool_id = block.get("tool_use_id")
                        is_error = block.get("is_error", False)
                        tool_results[tool_id] = {"error": is_error}

            elif msg_type == "result":
                # Final result
                result_text = data.get("result", "")
                if result_text and result_text not in messages:
                    messages.append(result_text)

        except json.JSONDecodeError:
            # Not JSON, treat as plain text
            if line.strip():
                messages.append(line)

    # Format file changes summary
    if file_changes:
        change_lines = ["\n📁 *File Operations:*"]
        for change in file_changes:
            tool_id = change.get("tool_id")
            result = tool_results.get(tool_id, {})
            status = "❌" if result.get("error") else "✅"

            if change["type"] == "create":
                change_lines.append(f"{status} Created: `{shorten_path(change['path'])}`")
            elif change["type"] == "edit":
                change_lines.append(f"{status} Edited: `{shorten_path(change['path'])}`")
            elif change["type"] == "bash":
                cmd = change["command"]
                if len(cmd) > 60:
                    cmd = cmd[:57] + "..."
                change_lines.append(f"{status} Ran: `{cmd}`")
            elif change["type"] == "read":
                change_lines.append(f"📖 Read: `{shorten_path(change['path'])}`")

        messages.append("\n".join(change_lines))

    return "\n".join(messages), questions


def shorten_path(path):
    """Shorten a file path for display."""
    if len(path) <= 50:
        return path
    parts = path.split("/")
    if len(parts) <= 2:
        return path
    return f".../{'/'.join(parts[-2:])}"


# Permission detection patterns (Option B: detect and prompt user)
PERMISSION_PATTERNS = [
    "need permission",
    "permission to write",
    "permission to edit",
    "permission to create",
    "please grant permission",
    "waiting for permission",
    "requires permission",
    "need to wait for permission",
    "grant me permission",
    "allow me to",
]


def detect_permission_request(text):
    """Check if Claude's output indicates it needs permission."""
    text_lower = text.lower()
    for pattern in PERMISSION_PATTERNS:
        if pattern in text_lower:
            return True
    return False


def create_permission_question():
    """Create a question asking user to grant permissions."""
    return {
        "question": "Claude needs permission to modify files. Would you like to grant permission?",
        "header": "Permission",
        "options": [
            {"label": "Yes, allow file operations", "description": "Grant permission for this task"},
            {"label": "No, don't modify files", "description": "Deny permission"},
        ]
    }


def run_claude(prompt, cwd=None, continue_session=False, extra_args=None):
    """Run Claude CLI with session support (non-streaming)."""
    cmd = ["claude", "-p", "--verbose", "--output-format", "stream-json", "--model", "opus"]

    # Add pre-approved tools to avoid permission prompts
    if CLAUDE_ALLOWED_TOOLS:
        cmd.extend(["--allowedTools", CLAUDE_ALLOWED_TOOLS])

    if continue_session:
        cmd.append("--continue")

    if extra_args:
        cmd.extend(extra_args)

    # Use -- to separate options from prompt (prevents arg parsing issues)
    cmd.append("--")
    cmd.append(prompt)

    env = os.environ.copy()
    work_dir = cwd or os.getcwd()

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=work_dir,
            env=env
        )

        output = result.stdout or ""
        stderr = result.stderr or ""

        # Try to parse as JSON stream
        if output.strip():
            text, questions = parse_claude_output(output)
            # Option B: Detect permission requests and create a question
            if text and detect_permission_request(text) and not questions:
                questions.append(create_permission_question())
            if text or questions:
                return text, questions

        # Fallback to raw output
        fallback_text = output.strip() or stderr.strip() or "No output"
        questions = []
        if detect_permission_request(fallback_text):
            questions.append(create_permission_question())
        return fallback_text, questions

    except FileNotFoundError:
        return "Error: Claude CLI not found. Make sure it's installed and in PATH", []
    except Exception as e:
        return f"Error running Claude: {e}", []


def run_claude_streaming(prompt, chat_id, cwd=None, continue_session=False, session_id=None, session=None):
    """Run Claude CLI with streaming output to Telegram."""
    global active_processes

    cmd = ["claude", "-p", "--verbose", "--output-format", "stream-json", "--model", "opus"]

    # Add pre-approved tools to avoid permission prompts
    if CLAUDE_ALLOWED_TOOLS:
        cmd.extend(["--allowedTools", CLAUDE_ALLOWED_TOOLS])

    # Resume with Claude's session ID if available
    # Only use --resume with a valid session ID, never use --continue
    # (--continue resumes the global last conversation, which breaks new sessions)
    claude_session_id = session.get("claude_session_id") if session else None
    if claude_session_id:
        cmd.extend(["--resume", claude_session_id])
    elif session and session.get("last_summary"):
        # No session to resume but we have a saved summary from compaction (e.g. crash recovery)
        prompt = f"""[Session compacted - Previous context summary:]
{session['last_summary']}

[New request:]
{prompt}"""

    # Use -- to separate options from prompt (prevents arg parsing issues)
    cmd.append("--")
    cmd.append(prompt)

    work_dir = cwd or os.getcwd()
    # Use session_id for process tracking (allows parallel sessions)
    process_key = session_id or str(chat_id)

    # Send initial message
    message_id = send_message(chat_id, "⏳ _Thinking..._")
    message_ids = [message_id]  # Track all message IDs for chunked responses
    accumulated_text = ""
    current_chunk_text = ""  # Text in current message chunk
    last_update = time.time()
    update_interval = 1.0  # Update every 1 second
    max_chunk_len = 3500  # Start new message before hitting Telegram's 4096 limit
    max_accumulated = 1_000_000  # Cap accumulated text at 1MB to prevent memory bloat
    questions = []
    file_changes = []
    current_tool = None
    cancelled = False
    processed_tool_ids = set()  # Track processed tool_use IDs to avoid duplicates
    new_claude_session_id = None  # Capture Claude's session ID from init

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=work_dir,
            start_new_session=True  # Own process group so we can kill the whole tree on cancel
        )

        # Track active process for cancellation (by session_id for parallel support)
        active_processes[process_key] = process

        # Track for crash recovery
        session_name = session.get("name", "default") if session else "default"
        mark_session_active(chat_id, session_name, process_key, prompt)

        # Read stdout as binary and decode with replace to avoid UTF-8 split errors
        import io
        stdout_reader = io.TextIOWrapper(process.stdout, encoding='utf-8', errors='replace')

        line_count = 0
        total_bytes_read = 0
        LARGE_LINE_THRESHOLD = 50_000  # Lines above this use lightweight parsing

        def _process_tool_use(tool_id, tool_name, tool_input):
            """Handle a tool_use block (shared between normal and large-line paths)."""
            nonlocal current_tool, last_update
            if tool_name == "AskUserQuestion":
                new_qs = tool_input.get("questions", [])
                print(f"[DEBUG] AskUserQuestion tool_id={tool_id}, adding {len(new_qs)} questions", flush=True)
                questions.extend(new_qs)
            elif tool_name == "ExitPlanMode":
                print(f"[DEBUG] ExitPlanMode tool_id={tool_id}", flush=True)
                questions.append({
                    "question": "Plan is ready. Do you approve this plan?",
                    "header": "Plan Approval",
                    "options": [
                        {"label": "Approve", "description": "Proceed with implementation"},
                        {"label": "Reject", "description": "Revise the plan"},
                    ]
                })
            elif tool_name in ["Write", "Edit", "Bash", "Read", "Glob", "Grep"]:
                path = tool_input.get("file_path") or tool_input.get("command") or tool_input.get("pattern") or ""
                file_changes.append({"type": tool_name.lower(), "path": path[:100]})
                current_tool = tool_name
                now = time.time()
                if now - last_update >= update_interval:
                    display_text = current_chunk_text or ""
                    if tool_name == "Bash" and path:
                        cmd_preview = path[:60] + "..." if len(path) > 60 else path
                        status = f"\n\n🔧 _Running:_ `{cmd_preview}`"
                    else:
                        status = f"\n\n🔧 _Running {tool_name}..._"
                    edit_message(chat_id, message_id, display_text + status)
                    last_update = now

        def _process_text(text):
            """Handle a text block's content (shared between normal and large-line paths)."""
            nonlocal accumulated_text, current_chunk_text, current_tool, message_id, last_update
            if not text:
                return
            print(f"[STREAM] _process_text: {len(text)} chars, total_accumulated={len(accumulated_text)}, chunk={len(current_chunk_text)}", flush=True)
            spacing = ""
            if accumulated_text and not accumulated_text.endswith('\n') and not text.startswith('\n'):
                if accumulated_text.endswith(('.', '!', '?', ':')):
                    spacing = "\n\n"
                elif not accumulated_text.endswith(' '):
                    spacing = " "
            if len(accumulated_text) < max_accumulated:
                accumulated_text += spacing + text
            current_chunk_text += spacing + text
            current_tool = None
            while len(current_chunk_text) > max_chunk_len:
                # Send the first max_chunk_len chars, carry over the rest
                send_part = current_chunk_text[:max_chunk_len]
                carry_over = current_chunk_text[max_chunk_len:]
                edit_message(chat_id, message_id, send_part.strip() + "\n\n———\n_continued..._", force=True)
                message_id = send_message(chat_id, "⏳ _continuing..._")
                message_ids.append(message_id)
                current_chunk_text = carry_over
                last_update = time.time()
            now = time.time()
            if now - last_update >= update_interval and current_chunk_text.strip():
                edit_message(chat_id, message_id, current_chunk_text + "\n\n———\n⏳ _generating..._")
                last_update = now

        for line in stdout_reader:
            if not line.strip():
                continue

            line_count += 1
            line_len = len(line)
            total_bytes_read += line_len

            # Log large lines and periodic stats
            if line_len > 50_000:
                print(f"[STREAM] Large line #{line_count}: {line_len} bytes, total read: {total_bytes_read}, type_hint={line[:30]}", flush=True)
            elif line_count % 50 == 0:
                print(f"[STREAM] Line #{line_count}: total_bytes_read={total_bytes_read}, accumulated={len(accumulated_text)}, chunks={len(message_ids)}", flush=True)

            try:
                # ── Large lines: avoid full json.loads() ──
                # When Claude writes large files, the tool_use block's input can be
                # megabytes.  Full json.loads() on such lines creates huge transient
                # Python dicts that fragment memory and contribute to OOM.
                #
                # For large assistant/user lines, we extract only what we need
                # (tool_use metadata, text) from the raw string without a full parse.
                # Text in large lines is typically from tool output (Read results) or
                # large Write inputs that we don't need to display verbatim.
                if line_len > LARGE_LINE_THRESHOLD and '"type":"user"' in line[:200]:
                    # Large user events are tool results (e.g. Read output).
                    # We don't need anything from them — skip entirely.
                    print(f"[STREAM] Skipping large user line #{line_count}: {line_len} bytes", flush=True)
                    line = None
                    _malloc_trim()
                    continue

                if line_len > LARGE_LINE_THRESHOLD and '"type":"assistant"' in line[:200]:
                    # Extract text blocks from the head of the line (text appears before
                    # the huge tool_use input that makes the line large).
                    # Look at the first 10KB which should contain any text blocks.
                    head_size = min(line_len, 10_000)
                    head = line[:head_size]
                    for tm in re.finditer(r'"type"\s*:\s*"text"\s*,\s*"text"\s*:\s*"', head):
                        # Extract the text value — find the closing unescaped quote
                        start = tm.end()
                        text_chars = []
                        i = start
                        while i < len(head):
                            if head[i] == '\\' and i + 1 < len(head):
                                text_chars.append(head[i:i+2])
                                i += 2
                            elif head[i] == '"':
                                break
                            else:
                                text_chars.append(head[i])
                                i += 1
                        extracted = "".join(text_chars)
                        # Decode JSON escape sequences
                        try:
                            extracted = json.loads(f'"{extracted}"')
                        except (json.JSONDecodeError, ValueError):
                            pass
                        if extracted.strip():
                            _process_text(extracted)

                    # Scan the tail for new tool_use blocks (they appear at the end)
                    tail_size = min(line_len, 10_000)
                    tail = line[-tail_size:]

                    for m in re.finditer(r'"type"\s*:\s*"tool_use"', tail):
                        # Extract id and name with regex (avoids parsing huge input)
                        region = tail[max(0, m.start() - 200):min(len(tail), m.end() + 500)]
                        id_m = re.search(r'"id"\s*:\s*"([^"]+)"', region)
                        name_m = re.search(r'"name"\s*:\s*"([^"]+)"', region)
                        if not id_m or not name_m:
                            continue
                        tool_id = id_m.group(1)
                        tool_name = name_m.group(1)
                        if tool_id in processed_tool_ids:
                            continue
                        processed_tool_ids.add(tool_id)
                        # For file tools, try to extract the path without full parse
                        tool_input = {}
                        if tool_name in ["Write", "Edit", "Read"]:
                            fp_m = re.search(r'"file_path"\s*:\s*"([^"]*)"', tail[m.start():])
                            if fp_m:
                                tool_input["file_path"] = fp_m.group(1)[:100]
                        elif tool_name == "Bash":
                            cmd_m = re.search(r'"command"\s*:\s*"([^"]*)"', tail[m.start():])
                            if cmd_m:
                                tool_input["command"] = cmd_m.group(1)[:100]
                        elif tool_name in ["Glob", "Grep"]:
                            pat_m = re.search(r'"pattern"\s*:\s*"([^"]*)"', tail[m.start():])
                            if pat_m:
                                tool_input["pattern"] = pat_m.group(1)[:100]
                        elif tool_name == "AskUserQuestion":
                            # For AskUserQuestion, we need the full input — parse just this block
                            start_pos = tail.rfind('{', max(0, m.start() - 200), m.start())
                            if start_pos != -1:
                                brace_count = 0
                                end_pos = start_pos
                                for i in range(start_pos, len(tail)):
                                    if tail[i] == '{':
                                        brace_count += 1
                                    elif tail[i] == '}':
                                        brace_count -= 1
                                        if brace_count == 0:
                                            end_pos = i + 1
                                            break
                                if end_pos > start_pos:
                                    try:
                                        block = json.loads(tail[start_pos:end_pos])
                                        tool_input = block.get("input", {})
                                    except json.JSONDecodeError:
                                        pass
                        _process_tool_use(tool_id, tool_name, tool_input)

                    head = None
                    tail = None
                    line = None
                    _malloc_trim()
                    continue

                # ── Normal-sized lines: full JSON parsing ──
                if line_len > LARGE_LINE_THRESHOLD:
                    print(f"[STREAM] Large line #{line_count} ({line_len} bytes) fell through to json.loads! type_hint={line[:50]}", flush=True)
                data = json.loads(line)
                msg_type = data.get("type")

                # Capture Claude's session_id from init message
                if msg_type == "system" and data.get("subtype") == "init":
                    new_claude_session_id = data.get("session_id")

                if msg_type == "assistant":
                    content = data.get("message", {}).get("content", [])
                    for block in content:
                        if block.get("type") == "text":
                            _process_text(block.get("text", ""))
                        elif block.get("type") == "tool_use":
                            tool_id = block.get("id")
                            if tool_id and tool_id in processed_tool_ids:
                                continue
                            if tool_id:
                                processed_tool_ids.add(tool_id)
                            _process_tool_use(tool_id, block.get("name"), block.get("input", {}))

                elif msg_type == "result":
                    result_text = data.get("result", "")
                    print(f"[STREAM] result event: result_len={len(result_text)}, accumulated={len(accumulated_text)}, chunk={len(current_chunk_text)}, msgs={len(message_ids)}", flush=True)
                    if result_text:
                        # Use the longer of streamed text vs result as the authoritative output.
                        if len(result_text) >= len(accumulated_text):
                            accumulated_text = result_text
                        # For single-message responses, update display with result
                        if len(message_ids) == 1 and len(result_text) >= len(current_chunk_text.strip()):
                            current_chunk_text = result_text

            except json.JSONDecodeError:
                if line.strip() and not accumulated_text:
                    accumulated_text += line

            # Free large parsed objects and trim heap
            if line_len > LARGE_LINE_THRESHOLD:
                data = None
                line = None
                _malloc_trim()

        stdout_reader.close()
        process.wait()

        # Check if cancelled
        cancelled = process_key not in active_processes

        # Clean up process tracking
        active_processes.pop(process_key, None)
        mark_session_done(process_key)

        # Final update - no cursor, indicates completion
        # Use current_chunk_text for the last message. If empty (e.g. tool-only response),
        # fall back to result text. But if text was already chunked across messages, don't repeat.
        final_chunk = current_chunk_text.strip()
        print(f"[STREAM] Final: final_chunk={len(final_chunk)}, accumulated={len(accumulated_text)}, msgs={len(message_ids)}, lines={line_count}", flush=True)
        if not final_chunk:
            if len(message_ids) == 1 and accumulated_text.strip():
                # Single message, no text streamed yet — show the result summary
                final_chunk = accumulated_text.strip()[-3500:]
            else:
                final_chunk = ""

        # Option B: Detect permission requests and create a question
        if detect_permission_request(accumulated_text) and not questions:
            questions.append(create_permission_question())

        # Add file changes summary to final chunk
        if file_changes:
            final_chunk += "\n\n📁 *File Operations:*"
            for change in file_changes:
                if change["type"] == "write":
                    final_chunk += f"\n  ✅ Created: `{shorten_path(change['path'])}`"
                elif change["type"] == "edit":
                    final_chunk += f"\n  ✅ Edited: `{shorten_path(change['path'])}`"
                elif change["type"] == "bash":
                    final_chunk += f"\n  ✅ Ran: `{change['path'][:80]}{'...' if len(change['path']) > 80 else ''}`"
                elif change["type"] == "read":
                    final_chunk += f"\n  📖 Read: `{shorten_path(change['path'])}`"
                elif change["type"] in ["glob", "grep"]:
                    final_chunk += f"\n  🔍 Search: `{change['path'][:60]}{'...' if len(change['path']) > 60 else ''}`"

        # Add completion indicator
        if cancelled:
            final_chunk += "\n\n———\n⚠️ _cancelled_"
        else:
            final_chunk += "\n\n———\n✓ _complete_"

        # Handle final chunk - may need further splitting if file ops made it too long
        if len(final_chunk) <= 4000:
            if message_id:
                edit_message(chat_id, message_id, final_chunk, force=True)
            else:
                # message_id was lost (send_message failed earlier) — send as new message
                send_message(chat_id, final_chunk)
        else:
            # Final chunk is too long, need to split it
            if message_id:
                try:
                    requests.post(f"{API_URL}/deleteMessage",
                                json={"chat_id": chat_id, "message_id": message_id}, timeout=5)
                except:
                    pass
            # Send remaining chunks as new messages
            max_len = 3900
            chunks = [final_chunk[i:i + max_len] for i in range(0, len(final_chunk), max_len)]
            for chunk in chunks:
                send_message(chat_id, chunk)
                time.sleep(0.2)  # Small delay to maintain order

        # Check for context overflow error (text too long or too many images)
        context_overflow = ("prompt is too long" in accumulated_text.lower() or
                           "context length" in accumulated_text.lower() or
                           "too much media" in accumulated_text.lower())

        return accumulated_text, questions, message_id, new_claude_session_id, context_overflow

    except FileNotFoundError:
        active_processes.pop(process_key, None)
        mark_session_done(process_key)
        edit_message(chat_id, message_id, "❌ _Error: Claude CLI not found_", force=True)
        return "Error: Claude CLI not found", [], message_id, None, False
    except Exception as e:
        active_processes.pop(process_key, None)
        mark_session_done(process_key)
        # Ensure subprocess pipes are cleaned up
        try:
            if process and process.stdout:
                process.stdout.close()
            if process:
                process.kill()
                process.wait()
        except Exception:
            pass
        error_text = accumulated_text + f"\n\n———\n❌ _Error: {e}_"
        context_overflow = ("prompt is too long" in str(e).lower() or
                           "context length" in str(e).lower() or
                           "too much media" in str(e).lower())
        if len(error_text) <= 4000:
            edit_message(chat_id, message_id, error_text, force=True)
        else:
            edit_message(chat_id, message_id, error_text[:3950] + "\n\n_(...truncated)_", force=True)
        return f"Error: {e}", [], message_id, None, context_overflow


def create_session(chat_id, project_name, cwd):
    """Create a new session for a user. Always creates a new session even for same cwd."""
    chat_key = str(chat_id)

    if chat_key not in user_sessions:
        user_sessions[chat_key] = {"sessions": [], "active": None}

    # Count existing sessions with same base name to create unique name
    base_name = project_name
    existing_count = sum(1 for s in user_sessions[chat_key]["sessions"]
                        if s["name"] == base_name or s["name"].startswith(f"{base_name} ("))

    if existing_count > 0:
        display_name = f"{base_name} ({existing_count + 1})"
    else:
        display_name = base_name

    # Generate unique session ID
    session_id = str(uuid.uuid4())[:8]

    session = {
        "id": session_id,
        "name": display_name,
        "cwd": cwd,
        "created_at": datetime.now().isoformat(),
        "last_prompt": None,  # Track last prompt for context
        "claude_session_id": None,  # Claude CLI's session ID for --resume
        "message_count": 0,  # Track messages for proactive compaction
    }

    user_sessions[chat_key]["sessions"].append(session)
    user_sessions[chat_key]["active"] = session_id  # Use session_id as identifier
    save_sessions()

    return session


def get_active_session(chat_id):
    """Get the active session for a user."""
    chat_key = str(chat_id)
    user_data = user_sessions.get(chat_key, {})
    active_id = user_data.get("active")

    if not active_id:
        return None

    for s in user_data.get("sessions", []):
        # Support both new (id) and legacy (cwd) session identifiers
        if s.get("id") == active_id or s.get("cwd") == active_id:
            return s
    return None


def set_active_session(chat_id, session_id):
    """Set the active session for a user by session_id."""
    chat_key = str(chat_id)
    if chat_key in user_sessions:
        user_sessions[chat_key]["active"] = session_id
        save_sessions()


def get_session_by_id(chat_id, session_id):
    """Get a specific session by its ID (not the active one)."""
    chat_key = str(chat_id)
    for s in user_sessions.get(chat_key, {}).get("sessions", []):
        if s.get("id") == session_id or s.get("cwd") == session_id:
            return s
    return None


def get_session_id(session):
    """Get the session ID, supporting both new and legacy sessions."""
    return session.get("id") or session.get("cwd")


def update_session_last_prompt(chat_id, session, prompt):
    """Update the last prompt for a session."""
    chat_key = str(chat_id)
    if chat_key not in user_sessions:
        return

    session_id = get_session_id(session)
    for s in user_sessions[chat_key]["sessions"]:
        if get_session_id(s) == session_id:
            # Store truncated prompt
            s["last_prompt"] = prompt[:100] if len(prompt) > 100 else prompt
            save_sessions()
            break


def update_claude_session_id(chat_id, session, claude_session_id):
    """Update Claude's session ID for resuming conversations."""
    chat_key = str(chat_id)
    if chat_key not in user_sessions:
        return

    session_id = get_session_id(session)
    for s in user_sessions[chat_key]["sessions"]:
        if get_session_id(s) == session_id:
            s["claude_session_id"] = claude_session_id
            # Clear saved summary once we have a valid session to resume
            if claude_session_id:
                s.pop("last_summary", None)
            save_sessions()
            break


def save_session_summary(chat_id, session, summary):
    """Persist compaction summary so it survives crashes."""
    chat_key = str(chat_id)
    if chat_key not in user_sessions:
        return

    session_id = get_session_id(session)
    for s in user_sessions[chat_key]["sessions"]:
        if get_session_id(s) == session_id:
            s["last_summary"] = summary
            save_sessions()
            break


# Threshold for proactive compaction (number of messages before auto-compacting)
# Opus 4.6 has ~200K context window, so 30 messages keeps context focused
# without compacting too aggressively
COMPACTION_THRESHOLD = 30


def increment_message_count(chat_id, session):
    """Increment message count and return True if compaction is needed."""
    if not session:
        return False

    chat_key = str(chat_id)
    session_id = get_session_id(session)

    for s in user_sessions.get(chat_key, {}).get("sessions", []):
        if get_session_id(s) == session_id:
            s["message_count"] = s.get("message_count", 0) + 1
            save_sessions()
            return s["message_count"] >= COMPACTION_THRESHOLD
    return False


def reset_message_count(chat_id, session):
    """Reset the message count after compaction."""
    if not session:
        return

    chat_key = str(chat_id)
    session_id = get_session_id(session)

    for s in user_sessions.get(chat_key, {}).get("sessions", []):
        if get_session_id(s) == session_id:
            s["message_count"] = 0
            save_sessions()
            break


def is_allowed(chat_id):
    """Check if the chat ID is allowed."""
    if not ALLOWED_CHAT_IDS or ALLOWED_CHAT_IDS == [""]:
        print("Warning: No ALLOWED_CHAT_IDS set. Allowing all users.")
        return True
    return str(chat_id) in ALLOWED_CHAT_IDS


def run_codex_task(chat_id, task, cwd, session=None):
    """Run a Codex task on the project in background thread. Resumes session if available."""
    session_id = get_session_id(session) if session else str(chat_id)

    def codex_thread():
        try:
            codex_sid = session.get("codex_session_id") if session else None
            mode = "Resuming" if codex_sid else "Starting"
            send_message(chat_id, f"🔍 *{mode} Codex*\nModel: `gpt-5.2-codex`\nTask: _{task[:100]}_")

            # Build command — resume existing session or start new
            if codex_sid:
                cmd = [
                    "codex", "exec", "resume", codex_sid,
                    "-m", CODEX_MODEL,
                    "-c", 'model_reasoning_effort="xhigh"',
                    "--full-auto", "--json",
                    task
                ]
            else:
                cmd = [
                    "codex", "exec",
                    "-m", CODEX_MODEL,
                    "-c", 'model_reasoning_effort="xhigh"',
                    "--full-auto", "--json",
                    task
                ]

            process = subprocess.Popen(
                cmd, cwd=cwd,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
            )

            # Track as active so messages get queued
            active_processes[session_id] = process

            new_thread_id = None
            max_chunk_len = 3800
            update_interval = 1.5
            current_chunk_text = ""
            message_id = send_message(chat_id, "⏳ _Codex working..._")
            message_ids = [message_id]
            last_update = time.time()
            current_tool = None

            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    etype = event.get("type", "")

                    if etype == "thread.started":
                        new_thread_id = event.get("thread_id")

                    elif etype == "item.completed":
                        item = event.get("item", {})
                        if item.get("type") == "agent_message":
                            text = item.get("text", "")
                            if text:
                                if current_chunk_text and not current_chunk_text.endswith('\n'):
                                    current_chunk_text += "\n\n"
                                current_chunk_text += text
                                current_tool = None
                        elif item.get("type") == "function_call":
                            fname = item.get("name", "tool")
                            current_tool = fname

                    # Stream update: chunk overflow
                    while len(current_chunk_text) > max_chunk_len:
                        send_part = current_chunk_text[:max_chunk_len]
                        current_chunk_text = current_chunk_text[max_chunk_len:]
                        edit_message(chat_id, message_id, send_part.strip() + "\n\n———\n_continued..._", force=True)
                        message_id = send_message(chat_id, "⏳ _continuing..._")
                        message_ids.append(message_id)
                        last_update = time.time()

                    # Stream update: periodic edit
                    now = time.time()
                    if now - last_update >= update_interval and current_chunk_text.strip():
                        suffix = f"\n\n———\n🔧 _{current_tool}_" if current_tool else "\n\n———\n⏳ _generating..._"
                        edit_message(chat_id, message_id, current_chunk_text + suffix)
                        last_update = now

                except json.JSONDecodeError:
                    pass

            process.wait()

            # Save codex session ID for resume
            if new_thread_id and session:
                chat_key = str(chat_id)
                for s in user_sessions.get(chat_key, {}).get("sessions", []):
                    if get_session_id(s) == session_id:
                        s["codex_session_id"] = new_thread_id
                        save_sessions()
                        break

            # Final message
            if current_chunk_text.strip():
                edit_message(chat_id, message_id, current_chunk_text.strip(), force=True)
            else:
                edit_message(chat_id, message_id, "Codex completed but produced no output.", force=True)

        except FileNotFoundError:
            send_message(chat_id, "❌ Codex not found. Is it installed?")
        except Exception as e:
            send_message(chat_id, f"❌ Codex error: {str(e)[:200]}")
        finally:
            active_processes.pop(session_id, None)
            # Process queued messages
            process_message_queue(chat_id, session)

    # Mark active under lock to prevent race with incoming messages
    lock = get_session_lock(session_id)
    with lock:
        active_processes[session_id] = None
    thread = threading.Thread(target=codex_thread, daemon=True)
    thread.start()


def handle_justdoit_questions(questions):
    """Auto-answer Claude's questions during justdoit mode.

    Returns a string answer to send back to Claude.
    """
    answers = []
    for q in questions:
        header = q.get("header", "")
        question_text = q.get("question", "")
        options = q.get("options", [])

        if "plan approval" in header.lower() or "approve" in question_text.lower():
            answers.append("Yes, approved. Please proceed with implementation.")
        elif options:
            first_opt = options[0]
            label = first_opt.get("label", first_opt) if isinstance(first_opt, dict) else str(first_opt)
            answers.append(label)
        else:
            answers.append("Yes, please proceed with the most sensible approach.")

    if len(answers) == 1:
        return answers[0]

    return "\n".join(f"{i+1}. {a}" for i, a in enumerate(answers))


# Strict regex for detecting quota/rate-limit errors everywhere (stderr, response, exceptions).
# Uses word boundaries to avoid false positives from normal text containing words like
# "capacity", "quota" in non-error contexts, or line numbers like "4296".
QUOTA_REGEX = re.compile(
    r'\b(?:rate[ _-]?limit(?:ed)?|ratelimit|quota exceeded|too many requests'
    r'|resource ?exhausted|usage limit|token limit exceeded)\b'
    r'|(?:^|\s)429(?:\s|$|[,.\-:])'  # 429 only as standalone number
    r'|\berror.*(?:overloaded|over capacity)\b',
    re.IGNORECASE
)

QUOTA_WAIT_SECONDS = 3600  # 1 hour


def is_quota_error_response(text):
    """Check if text indicates a quota/rate-limit error using strict regex."""
    return bool(QUOTA_REGEX.search(text))


def run_codex_review(original_task, claude_output, step, history_summary, cwd, phase="implementing", pending_transition=None, stale_warning=None):
    """Call Codex to review Claude's output and determine next action.

    Returns: (next_prompt: str or None, is_done: bool, reasoning: str)
    The reasoning will start with "QUOTA:" if a rate-limit/quota error was detected.
    The reasoning will start with "PHASE:" if a phase transition is requested.

    pending_transition: if set (e.g. "reviewing", "testing", "done"), tells Codex that
    Claude's current output is a verification response and Codex may now transition.
    stale_warning: if set, a warning string appended to the prompt telling Codex that
    progress has stalled and it must try a fundamentally different approach.
    """
    max_output_len = 6000
    if len(claude_output) > max_output_len:
        claude_output = claude_output[:max_output_len] + "\n\n... (output truncated)"

    # When pending_transition is set, Codex knows Claude just did a verification pass
    if pending_transition:
        if pending_transition == "done":
            phase_block = f"""CONTEXT: You previously asked Claude to verify the work before finishing.
Claude's output above is the verification result.

- If Claude's verification found issues, incomplete work, or plan items that are clearly
  NOT implemented, tell Claude to fix them. Give a specific prompt about what needs to be
  fixed. Do NOT say DONE.
- Claude should have confirmed that the original plan items are implemented. If it has
  addressed the plan and the verification looks solid, that is sufficient.
- If Claude's verification confirms everything is solid (plan items addressed, tests pass,
  code is correct, requirements met), respond with: DONE
  followed by a summary of what was accomplished.
- Do NOT repeatedly ask Claude to re-read the plan if it has already provided a verification.
  If the verification is reasonable, say DONE."""
        else:
            phase_block = f"""CONTEXT: You previously asked Claude to verify the work before moving to {pending_transition}.
Claude's output above is the verification result.

- If Claude's verification found issues, incomplete code, or problems, tell Claude to fix them.
  Give a specific prompt about what needs to be fixed. Do NOT transition yet.
- If Claude has confirmed the work is complete and addressed the plan items (even if not in
  a strict checklist format), respond with: PHASE:{pending_transition}
  followed by a prompt for Claude to begin the {pending_transition} phase.
- Do NOT repeatedly ask Claude to re-read the plan if it has already provided a verification.
  If the verification looks reasonable, transition."""
    else:
        phase_instructions = {
            "implementing": """CURRENT PHASE: IMPLEMENTATION
Your goal is to drive the implementation to completion.

- If implementation is NOT complete, give Claude the next specific implementation step.
- If implementation looks complete, DO NOT transition yet. Instead, ask Claude to verify
  its work: craft a prompt telling Claude to re-read the ORIGINAL PLAN and the files it
  changed, then confirm that EVERY item from the plan has been implemented. Claude must
  explicitly list each plan item and state whether it is done or missing. Also check for
  TODOs, placeholder code, missed requirements, or incomplete sections, and report findings.
  Respond with: VERIFY:reviewing
  followed by the verification prompt for Claude.
- Do NOT say DONE during this phase.""",

            "reviewing": """CURRENT PHASE: CODE REVIEW
Claude should be reviewing the code that was implemented. Drive a thorough review.
Pay special attention to design and architecture flaws:
- Poor separation of concerns, god functions/classes, tight coupling
- Missing abstractions or wrong abstraction levels
- Patterns that won't scale or will be hard to maintain/extend
- Inconsistency with the existing codebase's architecture and conventions
- Hardcoded values that should be configurable, missing error boundaries
- Race conditions, state management issues, or concurrency problems

- If Claude found issues (including design/architecture flaws) during review, tell Claude
  to fix them. Be specific about what the flaw is and how to restructure. Stay in this phase.
- If the review looks clean, DO NOT transition yet. Instead, ask Claude to do one final
  verification pass: craft a prompt telling Claude to re-read changed files looking for
  bugs, edge cases, design flaws, and anything the review might have missed.
  Respond with: VERIFY:testing
  followed by the verification prompt for Claude.
- Do NOT say DONE during this phase.""",

            "testing": """CURRENT PHASE: TESTING
Claude should be writing and running tests. Prioritize integration and end-to-end tests
over unit tests — verify that components work together correctly, not just in isolation.

- Focus on INTEGRATION TESTS first: test real workflows, API interactions, data flowing
  through multiple components, and realistic user scenarios end-to-end.
- Unit tests are secondary — only add them for complex pure logic or tricky edge cases.
- If tests need to be written, tell Claude which integration/e2e tests to write.
- If tests are failing, tell Claude to fix them. Be specific.
- If tests are written AND passing, DO NOT say DONE yet. Instead, ask Claude to verify
  by re-running ALL tests and confirming everything passes.
  Respond with: VERIFY:done
  followed by the verification prompt for Claude.
- If anything is missing, tell Claude what else to test or fix.""",
        }
        phase_block = phase_instructions.get(phase, phase_instructions["implementing"])

    codex_prompt = f"""You are a senior engineering project manager overseeing an autonomous coding session.
You are responsible for driving the work through three phases: implementation → code review → testing.

ORIGINAL TASK:
{original_task}

PROGRESS SO FAR (step {step}):
{history_summary}

CLAUDE'S LATEST OUTPUT:
{claude_output}

{phase_block}

GENERAL RULES:
1. If Claude asked a question or needs a decision, provide a sensible answer and frame it as the next prompt.
2. If Claude presented a plan and is waiting for approval, approve it and tell Claude to proceed.
3. If there are errors or failing tests, craft a specific follow-up prompt to fix them.
4. If Claude seems stuck or going in circles, try a different approach.
5. NEVER ask Claude for a status update — you can already see its output above. Prompts like
   "what's the status?", "please continue", or "keep going" waste a step and produce no work.
   Instead, tell Claude what to do NEXT. If you're unsure of specifics (you don't have full
   codebase context), it's fine to say something like "Now implement the error handling for
   the upload feature" without specifying exact files — Claude has the full session context
   and will figure out the details. The key is: every prompt must drive NEW work forward.
6. Keep prompts concise but complete. Claude has full conversation context from the session.
7. Always watch for design and architecture flaws in Claude's output: poor abstractions, tight coupling, scalability issues, inconsistent patterns, or structural problems. If you spot any, include them in your next prompt so Claude addresses them.

RESPOND WITH ONE OF:
- "VERIFY:<next_phase>\\n<verification prompt for Claude>" to ask Claude to verify before transitioning
- "PHASE:<next_phase>\\n<prompt for Claude>" to transition (ONLY when reviewing a verification result)
- "DONE\\n<summary>" to finish (ONLY when reviewing a verification result where all tests pass)
- Or the exact next prompt to send to Claude (nothing else, no meta-commentary)"""

    if stale_warning:
        codex_prompt += f"\n\n⚠️ STALE PROGRESS WARNING:\n{stale_warning}"

    print(f"[Codex] Calling Codex. Step: {step}, phase: {phase}, pending_transition: {pending_transition}", flush=True)
    print(f"[Codex] Prompt length: {len(codex_prompt)}, Claude output length: {len(claude_output)}", flush=True)

    try:
        process = subprocess.Popen(
            [
                "codex", "exec",
                "-m", CODEX_MODEL,
                "-c", 'model_reasoning_effort="xhigh"',
                "--full-auto",
                codex_prompt
            ],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        stdout, stderr = process.communicate(timeout=120)

        output = (stdout or "").strip()
        error_output = (stderr or "").strip()
        print(f"[Codex] Raw output ({len(output)} chars): {output[:300]}...", flush=True)
        if error_output:
            print(f"[Codex] Stderr: {error_output[:200]}", flush=True)

        # Check for quota/rate-limit errors in stderr only (not stdout, which is Codex's response)
        # Exclude banner lines AND session/prompt lines that may contain quota-like words
        stderr_lines = [l for l in error_output.split("\n")
                        if l.strip() and not l.startswith(("OpenAI Codex", "---", "workdir:", "model:",
                                                           "provider:", "approval:", "sandbox:", "reasoning",
                                                           "session", "user ", "system "))]
        stderr_filtered = " ".join(stderr_lines)
        # Use strict regex to avoid false positives from prompt text in stderr
        if stderr_filtered and QUOTA_REGEX.search(stderr_filtered):
            return None, False, f"QUOTA: Codex rate limited — {stderr_filtered[:200]}"

        if not output:
            # Use filtered stderr (banner removed) for clearer error messages
            error_msg = stderr_filtered[:300] if stderr_filtered else error_output[:300]
            if error_msg:
                return None, False, f"Codex error: {error_msg}"
            return None, False, "Codex produced no output"

        if output.startswith("DONE"):
            summary = output[4:].strip().lstrip("\n")
            print(f"[Codex] Decision: DONE. Summary: {summary[:200]}", flush=True)
            return None, True, summary

        # Check for phase transition
        if output.startswith("PHASE:"):
            # Format: "PHASE:reviewing\n<prompt>"
            first_line, _, rest = output.partition("\n")
            new_phase = first_line[6:].strip()  # Remove "PHASE:" prefix
            prompt = rest.strip()
            print(f"[Codex] Decision: PHASE transition to {new_phase}. Prompt: {prompt[:200] if prompt else 'none'}", flush=True)
            if new_phase and prompt:
                return prompt, False, f"PHASE:{new_phase}"
            elif new_phase:
                return f"Continue with the {new_phase} phase.", False, f"PHASE:{new_phase}"

        # Check for verification request (pre-transition)
        if output.startswith("VERIFY:"):
            # Format: "VERIFY:reviewing\n<verification prompt for Claude>"
            first_line, _, rest = output.partition("\n")
            target = first_line[7:].strip()  # Remove "VERIFY:" prefix
            prompt = rest.strip()
            print(f"[Codex] Decision: VERIFY -> {target}. Prompt: {prompt[:200] if prompt else 'none'}", flush=True)
            if target and prompt:
                return prompt, False, f"VERIFY:{target}"
            elif target:
                return f"Please verify that all work is complete and report any issues.", False, f"VERIFY:{target}"

        print(f"[Codex] Decision: Continue. Next prompt: {output[:200]}", flush=True)
        return output, False, ""

    except subprocess.TimeoutExpired:
        process.kill()
        print(f"[Codex] TIMEOUT after 120s", flush=True)
        return "Please continue with the current task. What's the status?", False, "Codex timed out"
    except FileNotFoundError:
        print(f"[Codex] ERROR: codex binary not found", flush=True)
        return None, False, "Codex not found"
    except Exception as e:
        print(f"[Codex] EXCEPTION: {e}", flush=True)
        err_str = str(e)
        if QUOTA_REGEX.search(err_str):
            return None, False, f"QUOTA: Codex exception — {err_str[:200]}"
        return None, False, f"Codex error: {e}"


def _justdoit_wait(chat_key, seconds):
    """Sleep for `seconds` while checking cancellation every 30s.

    Returns True if wait completed, False if cancelled.
    """
    elapsed = 0
    interval = 30
    while elapsed < seconds:
        state = justdoit_active.get(chat_key, {})
        if not state.get("active", False):
            return False
        chunk = min(interval, seconds - elapsed)
        time.sleep(chunk)
        elapsed += chunk
    return justdoit_active.get(chat_key, {}).get("active", False)


def run_justdoit_loop(chat_id, task, session):
    """Main autonomous execution loop for /justdoit."""
    session_id = get_session_id(session)
    chat_key = f"{chat_id}:{session_id}"
    cwd = session["cwd"]
    log_prefix = f"[JustDoIt {chat_id}:{session.get('name', 'unknown')}]"

    print(f"{log_prefix} Starting. Task: {task[:200]}", flush=True)
    print(f"{log_prefix} Session ID: {session_id}, CWD: {cwd}", flush=True)

    justdoit_active[chat_key] = {
        "active": True,
        "task": task,
        "step": 0,
        "phase": "implementing",
        "chat_id": str(chat_id),
        "session_name": session.get("name", "unknown"),
    }
    save_active_tasks()

    step = 0
    phase = "implementing"
    history_summary = ""
    codex_fail_streak = 0
    pending_transition = None  # Set when Codex says VERIFY:<target>, cleared after verification
    verify_attempts = 0  # Track consecutive verification attempts to prevent loops
    recent_codex_actions = []  # Track last N (reasoning, prompt_prefix) tuples for loop detection
    notified_exit = False  # Track whether we sent a final status message to the user

    try:
        send_message(chat_id, f"""🚀 *JustDoIt Mode Activated*

Task: _{task[:200]}_

_Starting autonomous implementation..._
_Use /cancel to stop at any time._""")

        current_prompt = task

        while True:
            # Check cancellation
            state = justdoit_active.get(chat_key, {})
            if not state.get("active", False):
                send_message(chat_id, f"⚠️ *JustDoIt cancelled* at step {step}.")
                notified_exit = True
                break

            step += 1
            justdoit_active[chat_key]["step"] = step
            justdoit_active[chat_key]["phase"] = phase
            save_active_tasks()

            print(f"{log_prefix} === Step {step} === Phase: {phase}, Pending transition: {pending_transition}", flush=True)

            # --- Phase 1: Send prompt to Claude ---
            print(f"{log_prefix} Step {step}: Sending to Claude. Prompt: {current_prompt[:200]}...", flush=True)
            send_message(chat_id, f"🔄 *Step {step}* — Sending to Claude...")

            # Handle compaction
            needs_compaction = increment_message_count(chat_id, session)

            if needs_compaction:
                print(f"{log_prefix} Step {step}: Auto-compaction triggered", flush=True)
                send_message(chat_id, "📦 *Auto-compacting* session context...")

                summary_prompt = """Summarize this session for context continuity (max 500 words). Focus on ACTIONABLE STATE:
1. Files being edited — exact paths and what changed
2. Current task — what's in progress, what's done, what's left
3. Key decisions — architectural choices, approaches chosen and WHY
4. Bugs/issues — any errors encountered and their status (fixed/open)
5. Code snippets — any critical code patterns or values needed to continue

Omit: greetings, abandoned approaches, resolved debugging back-and-forth.
Format as a compact bullet list."""

                try:
                    summary_response, _, _, _, _ = run_claude_streaming(
                        summary_prompt, chat_id, cwd=cwd, continue_session=True,
                        session_id=session_id, session=session
                    )
                    summary = summary_response.split("———")[0].strip() if summary_response else ""
                except:
                    summary = ""

                # Persist summary before clearing session (survives crashes)
                if summary and len(summary) > 50:
                    save_session_summary(chat_id, session, summary)

                update_claude_session_id(chat_id, session, None)
                reset_message_count(chat_id, session)

                if summary and len(summary) > 50:
                    current_prompt = f"""[Session compacted - Previous context summary:]
{summary}

[Continuing task:]
{current_prompt}"""

                print(f"{log_prefix} Step {step}: Compaction done. Summary length: {len(summary) if summary else 0}", flush=True)
                send_message(chat_id, "🔄 Context preserved. Continuing...")

            # Check cancellation after compaction
            state = justdoit_active.get(chat_key, {})
            if not state.get("active", False):
                send_message(chat_id, f"⚠️ *JustDoIt cancelled* at step {step}.")
                notified_exit = True
                break

            update_session_last_prompt(chat_id, session, f"[justdoit step {step}] {current_prompt[:80]}")

            # Run Claude
            response, questions, _, claude_sid, context_overflow = run_claude_streaming(
                current_prompt, chat_id, cwd=cwd, continue_session=True,
                session_id=session_id, session=session
            )

            print(f"{log_prefix} Step {step}: Claude response length: {len(response) if response else 0}, questions: {bool(questions)}, context_overflow: {context_overflow}", flush=True)
            if response:
                print(f"{log_prefix} Step {step}: Claude response preview: {response[:300]}...", flush=True)

            # Check for Claude quota/rate-limit errors
            if response and is_quota_error_response(response):
                print(f"{log_prefix} Step {step}: Claude rate limited. Waiting 1 hour.", flush=True)
                send_message(chat_id,
                    f"⏳ *Claude rate limited* at step {step}.\n"
                    f"_Waiting 1 hour before retrying... ({datetime.now().strftime('%H:%M')} -> "
                    f"{(datetime.now() + timedelta(hours=1)).strftime('%H:%M')})_\n"
                    f"_Use /cancel to abort._")
                if not _justdoit_wait(chat_key, QUOTA_WAIT_SECONDS):
                    send_message(chat_id, f"⚠️ *JustDoIt cancelled* during rate-limit wait.")
                    break
                send_message(chat_id, "🔄 *Resuming after rate-limit wait...*")
                step -= 1  # Retry same step
                continue

            # Update session ID
            if claude_sid:
                update_claude_session_id(chat_id, session, claude_sid)
                session = get_session_by_id(chat_id, session_id) or session

            # Handle context overflow
            if context_overflow:
                print(f"{log_prefix} Step {step}: Context overflow detected, compacting.", flush=True)
                send_message(chat_id, "⚠️ Context overflow — compacting...")
                update_claude_session_id(chat_id, session, None)
                reset_message_count(chat_id, session)

                response, questions, _, claude_sid, _ = run_claude_streaming(
                    current_prompt, chat_id, cwd=cwd, continue_session=True,
                    session_id=session_id, session=session
                )
                if claude_sid:
                    update_claude_session_id(chat_id, session, claude_sid)
                    session = get_session_by_id(chat_id, session_id) or session

            # Handle questions from Claude (auto-answer)
            if questions:
                auto_answer = handle_justdoit_questions(questions)
                print(f"{log_prefix} Step {step}: Auto-answering {len(questions)} questions. Answer: {auto_answer[:200]}", flush=True)
                send_message(chat_id, f"🤖 *Auto-answering:* _{auto_answer[:100]}_")

                response2, questions2, _, claude_sid2, _ = run_claude_streaming(
                    auto_answer, chat_id, cwd=cwd, continue_session=True,
                    session_id=session_id, session=session
                )
                if claude_sid2:
                    update_claude_session_id(chat_id, session, claude_sid2)
                    session = get_session_by_id(chat_id, session_id) or session

                if response2:
                    response = (response or "") + "\n\n[After auto-answer:]\n" + response2

            # Clean response for review
            clean_response = response.split("———")[0].strip() if response else "No output"

            # Update rolling history — no cap, Codex models have large context windows
            step_summary = clean_response[:1500]
            history_summary += f"\n\nStep {step}: {step_summary}"

            # --- Phase 2: Pause (human-like pacing) ---
            time.sleep(3)

            # Check cancellation before Codex
            state = justdoit_active.get(chat_key, {})
            if not state.get("active", False):
                send_message(chat_id, f"⚠️ *JustDoIt cancelled* at step {step}.")
                break

            # --- Phase 3: Codex reviews ---
            phase_labels = {"implementing": "🔨 Implementing", "reviewing": "🔍 Reviewing", "testing": "🧪 Testing"}
            if pending_transition:
                send_message(chat_id, f"🧠 *Step {step}* ({phase_labels.get(phase, phase)}) — Codex reviewing verification...")
            else:
                send_message(chat_id, f"🧠 *Step {step}* ({phase_labels.get(phase, phase)}) — Codex reviewing output...")

            # Detect stale progress: check if recent actions are repetitive
            stale_warning = None
            if len(recent_codex_actions) >= 3:
                # Check if the last 3 actions have the same reasoning pattern (e.g. all VERIFY:reviewing)
                last_3_reasons = [a[0] for a in recent_codex_actions[-3:]]
                if len(set(last_3_reasons)) == 1:
                    stale_warning = (
                        f"The last {len(last_3_reasons)} steps all had the same action pattern: '{last_3_reasons[0]}'. "
                        f"Claude is NOT making progress — it is stuck in a loop. You MUST try a fundamentally different "
                        f"approach. Do NOT ask Claude to verify or re-read the plan again. Instead, either:\n"
                        f"1. Accept the current state and transition to the next phase, OR\n"
                        f"2. Give Claude a SPECIFIC, CONCRETE coding task (not a review/verify request)"
                    )
                    print(f"{log_prefix} Step {step}: STALE PROGRESS detected — same action '{last_3_reasons[0]}' repeated {len(last_3_reasons)} times", flush=True)

            print(f"{log_prefix} Step {step}: Calling Codex review. Phase: {phase}, pending_transition: {pending_transition}", flush=True)
            next_prompt, is_done, reasoning = run_codex_review(
                task, clean_response, step, history_summary, cwd, phase=phase,
                pending_transition=pending_transition, stale_warning=stale_warning
            )
            # Clear pending_transition after it's been used
            pending_transition = None
            print(f"{log_prefix} Step {step}: Codex result — is_done: {is_done}, reasoning: {reasoning[:200] if reasoning else 'none'}", flush=True)
            if next_prompt:
                print(f"{log_prefix} Step {step}: Codex next_prompt: {next_prompt[:200]}...", flush=True)

            # Track this action for loop detection
            action_key = reasoning[:30] if reasoning else "continue"
            recent_codex_actions.append((action_key, (next_prompt or "")[:50]))
            if len(recent_codex_actions) > 6:
                recent_codex_actions.pop(0)

            if is_done:
                print(f"{log_prefix} Step {step}: DONE. Summary: {reasoning[:300] if reasoning else 'none'}", flush=True)
                send_message(chat_id, f"""✅ *JustDoIt Complete!*

Completed in *{step}* steps.

*Summary:* {reasoning[:500] if reasoning else 'Task completed successfully.'}

_Session preserved. You can continue chatting with Claude in this session._""")
                break

            # Handle phase transitions
            if reasoning and reasoning.startswith("PHASE:"):
                new_phase = reasoning[6:].strip()
                if new_phase in ("implementing", "reviewing", "testing"):
                    print(f"{log_prefix} Step {step}: Phase transition {phase} -> {new_phase}", flush=True)
                    phase = new_phase
                    justdoit_active[chat_key]["phase"] = phase
                    verify_attempts = 0  # Reset on successful transition
                    recent_codex_actions.clear()  # Reset loop detection on phase change
                    phase_emoji = {"implementing": "🔨", "reviewing": "🔍", "testing": "🧪"}.get(phase, "📋")
                    send_message(chat_id, f"{phase_emoji} *Phase transition: {phase.upper()}*")

            # Handle verification requests (Codex wants Claude to verify before transitioning)
            if reasoning and reasoning.startswith("VERIFY:"):
                target = reasoning[7:].strip()
                verify_attempts += 1
                print(f"{log_prefix} Step {step}: Verification requested -> {target} (attempt {verify_attempts})", flush=True)
                if verify_attempts >= 3:
                    # Force transition to prevent infinite verification loops
                    print(f"{log_prefix} Step {step}: Forcing transition to {target} after {verify_attempts} verify attempts", flush=True)
                    if target in ("implementing", "reviewing", "testing"):
                        phase = target
                        justdoit_active[chat_key]["phase"] = phase
                        phase_emoji = {"implementing": "🔨", "reviewing": "🔍", "testing": "🧪"}.get(phase, "📋")
                        send_message(chat_id, f"{phase_emoji} *Phase transition: {phase.upper()}* (forced after {verify_attempts} verification attempts)")
                    elif target == "done":
                        send_message(chat_id, f"✅ *JustDoIt Complete!* (forced after {verify_attempts} verification attempts)\n\nCompleted in *{step}* steps.\n\n_Session preserved._")
                        notified_exit = True
                        break
                    verify_attempts = 0
                else:
                    pending_transition = target
                    send_message(chat_id, f"🔍 *Step {step}* — Verification requested before moving to {target}")

            # Handle Codex quota errors — wait and retry
            if next_prompt is None and reasoning and reasoning.startswith("QUOTA:"):
                print(f"{log_prefix} Step {step}: Codex rate limited. Waiting 1 hour.", flush=True)
                send_message(chat_id,
                    f"⏳ *Codex rate limited* at step {step}.\n"
                    f"_Waiting 1 hour before retrying... ({datetime.now().strftime('%H:%M')})_\n"
                    f"_Use /cancel to abort._")
                if not _justdoit_wait(chat_key, QUOTA_WAIT_SECONDS):
                    send_message(chat_id, f"⚠️ *JustDoIt cancelled* during rate-limit wait.")
                    break
                send_message(chat_id, "🔄 *Resuming after rate-limit wait...*")
                next_prompt, is_done, reasoning = run_codex_review(
                    task, clean_response, step, history_summary, cwd, phase=phase,
                    pending_transition=pending_transition
                )
                pending_transition = None

                if is_done:
                    send_message(chat_id, f"""✅ *JustDoIt Complete!*

Completed in *{step}* steps.

*Summary:* {reasoning[:500] if reasoning else 'Task completed successfully.'}

_Session preserved. You can continue chatting with Claude in this session._""")
                    break
                # Handle phase transition after quota retry
                if reasoning and reasoning.startswith("PHASE:"):
                    new_phase = reasoning[6:].strip()
                    if new_phase in ("implementing", "reviewing", "testing"):
                        phase = new_phase
                        justdoit_active[chat_key]["phase"] = phase
                        verify_attempts = 0
                        phase_emoji = {"implementing": "🔨", "reviewing": "🔍", "testing": "🧪"}.get(phase, "📋")
                        send_message(chat_id, f"{phase_emoji} *Phase transition: {phase.upper()}*")
                # Handle verification request after quota retry
                if reasoning and reasoning.startswith("VERIFY:"):
                    target = reasoning[7:].strip()
                    verify_attempts += 1
                    if verify_attempts >= 3:
                        print(f"{log_prefix} Step {step}: Forcing transition to {target} after {verify_attempts} verify attempts (post-quota)", flush=True)
                        if target in ("implementing", "reviewing", "testing"):
                            phase = target
                            justdoit_active[chat_key]["phase"] = phase
                            phase_emoji = {"implementing": "🔨", "reviewing": "🔍", "testing": "🧪"}.get(phase, "📋")
                            send_message(chat_id, f"{phase_emoji} *Phase transition: {phase.upper()}* (forced)")
                        verify_attempts = 0
                    else:
                        pending_transition = target
                        send_message(chat_id, f"🔍 *Step {step}* — Verification requested before moving to {target}")

            if next_prompt is None:
                codex_fail_streak += 1
                print(f"{log_prefix} Step {step}: Codex failed (streak: {codex_fail_streak}). Reason: {reasoning[:200] if reasoning else 'none'}", flush=True)
                if reasoning:
                    send_message(chat_id, f"⚠️ Codex issue: _{reasoning[:200]}_")
                if codex_fail_streak >= 3:
                    print(f"{log_prefix} Step {step}: Codex failed 3x in a row. Stopping.", flush=True)
                    send_message(chat_id, "❌ *Codex failed 3 times in a row.* Stopping justdoit.\n_Session preserved for manual continuation._")
                    break
                next_prompt = "Please continue with the current task. What's the current status and what's left to do?"
            else:
                codex_fail_streak = 0

            print(f"{log_prefix} Step {step}: Next prompt for Claude: {next_prompt[:200]}...", flush=True)
            send_message(chat_id, f"📋 *Next:* _{next_prompt[:150]}{'...' if len(next_prompt) > 150 else ''}_")

            current_prompt = next_prompt

            # --- Phase 4: Pause before next iteration ---
            time.sleep(2)

    except Exception as e:
        import traceback
        print(f"{log_prefix} EXCEPTION: {e}", flush=True)
        print(f"{log_prefix} Traceback:\n{traceback.format_exc()}", flush=True)
        try:
            send_message(chat_id, f"❌ *JustDoIt error:* {str(e)[:300]}")
        except Exception:
            pass  # Don't let a send failure hide the real error

    finally:
        print(f"{log_prefix} Loop ended. Total steps: {step}, final phase: {phase}", flush=True)
        # Always notify the user that justdoit has stopped
        try:
            state = justdoit_active.get(chat_key, {})
            if state.get("active", False):
                # Loop exited without sending a completion/cancellation message
                send_message(chat_id, f"⚠️ *JustDoIt stopped* at step {step} (phase: {phase}).\n_Session preserved._")
        except Exception:
            pass
        justdoit_active.pop(chat_key, None)
        save_active_tasks()


def handle_command(chat_id, text):
    """Handle bot commands. Returns True if handled."""
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    if cmd == "/start":
        send_message(chat_id, """🤖 *Claude Bot Ready!*

*Commands:*
• `/new <project>` - Start new session in ~/project
• `/resume` - Pick a session to resume
• `/sessions` - List your sessions
• `/plan` - Enter plan mode
• `/justdoit [task]` - Autonomous implementation mode
• `/status` - Show current session
• `/help` - Show this help

Send any message to chat with Claude!""")
        return True

    if cmd == "/help":
        send_message(chat_id, """*Claude Telegram Bot Help*

*Session Commands:*
• `/new <project>` - Start a new session
  Example: `/new lifecompanion`
  Creates a new session in `~/lifecompanion`
  _Multiple sessions per project supported!_

• `/resume` - Pick a session to resume (with buttons)
• `/sessions` - List all your sessions (🔄 = running)
• `/switch <name>` - Switch to a session by name
• `/delete <name>` - Delete a session (or `/delete all`)
• `/reset` - Clear conversation history (fresh start)
• `/end` - End current session
• `/status` - Show current session info

*Claude Commands:*
• `/plan` - Ask Claude to enter plan mode
• `/approve` - Approve current plan
• `/reject` - Reject current plan
• `/cancel` - Cancel current session's task
• `/codex [task]` - Run Codex task (session persists per project)
  Uses `gpt-5.2-codex` (reasoning: xhigh), auto-resumes previous session

*Autonomous Mode:*
• `/justdoit [task]` - Start autonomous implementation
  Claude implements, Codex reviews, loops until done.
  Use without args to continue current plan.
  _Use /cancel to stop._

*Files:*
• `/file <path>` - Download a file from the project
  Example: `/file src/main.py`
  _Also: `/f` as shorthand_

*Other:*
• `/init` - Run `claude init` to generate CLAUDE.md
• `/chatid` - Show your chat ID

*Parallel Tasks:*
You can run multiple tasks in parallel! Just `/new` or `/resume` to switch sessions while another is running. Messages to a busy session get queued.

Just send a message to chat with Claude!""")
        return True

    if cmd == "/chatid":
        send_message(chat_id, f"Your chat ID: `{chat_id}`")
        return True

    if cmd == "/new":
        if not args:
            send_message(chat_id, "Usage: `/new <project_name>`\nExample: `/new lifecompanion`")
            return True

        project_name = args.strip()
        # Resolve project directory
        if project_name.startswith("/"):
            cwd = project_name
        else:
            cwd = os.path.join(BASE_PROJECTS_DIR, project_name)

        if not os.path.isdir(cwd):
            send_message(chat_id, f"❌ Directory not found: `{cwd}`\n\nMake sure the project exists.")
            return True

        session = create_session(chat_id, project_name, cwd)
        send_message(chat_id, f"""✅ *Session Started*

• Project: `{project_name}`
• Directory: `{cwd}`

Send a message to start working!""")
        return True

    if cmd == "/sessions":
        chat_key = str(chat_id)
        user_data = user_sessions.get(chat_key, {})
        sessions = user_data.get("sessions", [])
        active_id = user_data.get("active")

        if not sessions:
            send_message(chat_id, "No sessions yet. Use `/new <project>` to start one.")
            return True

        lines = ["*Your Sessions:*\n"]
        for s in sessions[-10:]:  # Last 10 sessions
            session_id = get_session_id(s)
            is_active = session_id == active_id or s.get("cwd") == active_id
            is_busy = session_id in active_processes
            marker = "→ " if is_active else "  "
            status = " 🔄" if is_busy else ""
            lines.append(f"{marker}`{s['name']}`{status}")
            # Show last prompt snippet
            last_prompt = s.get("last_prompt")
            if last_prompt:
                snippet = last_prompt[:50] + "..." if len(last_prompt) > 50 else last_prompt
                lines.append(f"    _{snippet}_")

        lines.append("\n🔄 = running task")
        lines.append("\nUse `/resume` to pick a session or `/switch <name>`")
        send_message(chat_id, "\n".join(lines))
        return True

    if cmd == "/resume":
        chat_key = str(chat_id)
        user_data = user_sessions.get(chat_key, {})
        sessions = user_data.get("sessions", [])

        if not sessions:
            send_message(chat_id, "No sessions yet. Use `/new <project>` to start one.")
            return True

        # Build session list with last prompt info
        lines = ["*Pick a session to resume:*\n_🔄 = task running_\n"]
        keyboard = []
        for s in sessions[-8:]:  # Last 8 sessions (Telegram limit)
            session_id = get_session_id(s)
            is_busy = session_id in active_processes
            label = f"🔄 {s['name']}" if is_busy else s['name']
            # Use index as callback data
            idx = user_data["sessions"].index(s)
            keyboard.append([{"text": label, "callback_data": f"resume_{idx}"}])
            # Show last prompt snippet in message
            last_prompt = s.get("last_prompt")
            if last_prompt:
                snippet = last_prompt[:40] + "..." if len(last_prompt) > 40 else last_prompt
                lines.append(f"• *{s['name']}*: _{snippet}_")

        reply_markup = {"inline_keyboard": keyboard}
        send_message(chat_id, "\n".join(lines), reply_markup=reply_markup)
        return True

    if cmd == "/switch":
        if not args:
            send_message(chat_id, "Usage: `/switch <project_name>`")
            return True

        target = args.strip().lower()
        chat_key = str(chat_id)
        user_data = user_sessions.get(chat_key, {})

        for s in user_data.get("sessions", []):
            if s["name"].lower() == target or s["name"].lower().startswith(target):
                session_id = get_session_id(s)
                set_active_session(chat_id, session_id)
                send_message(chat_id, f"✅ Switched to `{s['name']}`")
                return True

        send_message(chat_id, f"❌ Session `{target}` not found. Use `/sessions` to list.")
        return True

    if cmd == "/delete":
        chat_key = str(chat_id)
        user_data = user_sessions.get(chat_key, {})
        sessions = user_data.get("sessions", [])

        if not sessions:
            send_message(chat_id, "No sessions to delete.")
            return True

        # /delete all — clear everything
        if args.strip().lower() == "all":
            for s in user_sessions.get(chat_key, {}).get("sessions", []):
                sid = get_session_id(s)
                session_locks.pop(sid, None)
                message_queue.pop(sid, None)
            user_sessions[chat_key] = {"sessions": [], "active": None}
            save_sessions()
            send_message(chat_id, "🗑️ All sessions deleted.")
            return True

        # /delete <name> — delete by name
        if args.strip():
            target = args.strip().lower()
            for i, s in enumerate(sessions):
                if s["name"].lower() == target or s["name"].lower().startswith(target):
                    deleted_name = s["name"]
                    sid = get_session_id(s)
                    sessions.pop(i)
                    if user_data.get("active") == sid:
                        user_data["active"] = None
                    session_locks.pop(sid, None)
                    message_queue.pop(sid, None)
                    save_sessions()
                    send_message(chat_id, f"🗑️ Deleted session `{deleted_name}`")
                    return True
            send_message(chat_id, f"❌ Session `{target}` not found. Use `/sessions` to list.")
            return True

        # /delete (no args) — show picker
        keyboard = []
        for s in sessions[-8:]:
            idx = sessions.index(s)
            keyboard.append([{"text": f"🗑️ {s['name']}", "callback_data": f"delete_{idx}"}])
        keyboard.append([{"text": "🗑️ Delete ALL", "callback_data": "delete_all"}])

        reply_markup = {"inline_keyboard": keyboard}
        send_message(chat_id, "*Pick a session to delete:*", reply_markup=reply_markup)
        return True

    if cmd == "/status":
        session = get_active_session(chat_id)
        if session:
            session_id = get_session_id(session)
            is_busy = session_id in active_processes

            jdi_key = f"{chat_id}:{session_id}"
            jdi_state = justdoit_active.get(jdi_key, {})
            if jdi_state.get("active"):
                jdi_phase = jdi_state.get('phase', 'implementing')
                status = f"🚀 JustDoIt step {jdi_state.get('step', '?')} — {jdi_phase}"
            elif is_busy:
                status = "🔄 Running"
            else:
                status = "✅ Idle"

            send_message(chat_id, f"""*Current Session:*
• Project: `{session['name']}`
• Directory: `{session['cwd']}`
• Status: {status}
• Created: {session['created_at'][:16]}""")
        else:
            send_message(chat_id, "No active session. Use `/new <project>` to start one.")
        return True

    if cmd == "/end":
        chat_key = str(chat_id)
        if chat_key in user_sessions:
            user_sessions[chat_key]["active"] = None
            save_sessions()
        send_message(chat_id, "Session ended. Use `/new <project>` to start a new one.")
        return True

    if cmd == "/reset":
        session = get_active_session(chat_id)
        if not session:
            send_message(chat_id, "No active session. Use `/new <project>` first.")
            return True
        # Clear the Claude session ID to start fresh
        update_claude_session_id(chat_id, session, None)
        send_message(chat_id, f"🔄 *Session Reset*\n\nCleared conversation history for `{session['name']}`.\nNext message will start a fresh conversation.")
        return True

    if cmd == "/cancel":
        session = get_active_session(chat_id)

        # Cancel justdoit mode if active on the current session
        justdoit_was_active = False
        if session:
            session_id = get_session_id(session)
            jdi_key = f"{chat_id}:{session_id}"
            if justdoit_active.get(jdi_key, {}).get("active"):
                justdoit_active[jdi_key]["active"] = False
                justdoit_was_active = True

        if session:
            session_id = get_session_id(session)
            process = active_processes.get(session_id)
            if process:
                try:
                    import signal
                    # Kill entire process group (Claude CLI + child processes) for immediate abort
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    # Close stdout pipe to unblock the reading thread and free buffers
                    try:
                        if process.stdout:
                            process.stdout.close()
                    except Exception:
                        pass
                    active_processes.pop(session_id, None)
                    if justdoit_was_active:
                        send_message(chat_id, f"⚠️ *JustDoIt cancelled* for `{session['name']}`.\n_Session preserved. You can continue manually._")
                    else:
                        send_message(chat_id, f"⚠️ Cancelled operation for `{session['name']}`.")
                except ProcessLookupError:
                    # Process already exited
                    active_processes.pop(session_id, None)
                    send_message(chat_id, f"⚠️ Cancelled (process already finished).")
                except Exception as e:
                    print(f"Cancel error: {e}", flush=True)
                    # Fallback: try regular kill
                    try:
                        process.kill()
                        active_processes.pop(session_id, None)
                    except:
                        pass
                    send_message(chat_id, f"⚠️ Cancelled operation for `{session['name']}`.")
            else:
                if justdoit_was_active:
                    send_message(chat_id, f"⚠️ *JustDoIt cancelled* for `{session['name']}`.\n_No active subprocess was running._")
                else:
                    send_message(chat_id, f"No active task for session `{session['name']}`.")
        else:
            if justdoit_was_active:
                send_message(chat_id, "⚠️ JustDoIt cancelled.")
            else:
                send_message(chat_id, "No active session. Nothing to cancel.")
        return True

    if cmd == "/plan":
        session = get_active_session(chat_id)
        if not session:
            send_message(chat_id, "No active session. Use `/new <project>` first.")
            return True

        send_typing(chat_id)
        response, questions = run_claude(
            "Enter plan mode to plan the implementation",
            cwd=session["cwd"]
        )

        if questions:
            set_pending_questions(chat_id, questions, session)
        elif response:
            send_message(chat_id, response)
        return True

    if cmd in ["/approve", "/yes"]:
        session = get_active_session(chat_id)
        if not session:
            send_message(chat_id, "No active session. Use `/new <project>` first.")
            return True
        send_typing(chat_id)
        response, _ = run_claude("yes, approved", cwd=session["cwd"], continue_session=True)
        send_message(chat_id, response or "✅ Approved")
        return True

    if cmd in ["/reject", "/no"]:
        session = get_active_session(chat_id)
        if not session:
            send_message(chat_id, "No active session. Use `/new <project>` first.")
            return True
        send_typing(chat_id)
        response, _ = run_claude("no, please revise", cwd=session["cwd"], continue_session=True)
        send_message(chat_id, response or "❌ Rejected")
        return True

    if cmd == "/codex":
        session = get_active_session(chat_id)
        if not session:
            send_message(chat_id, "No active session. Use `/new <project>` first.")
            return True

        task = args.strip() if args else "Review the code and identify any issues, bugs, or improvements"
        run_codex_task(chat_id, task, session["cwd"], session=session)
        return True

    if cmd == "/init":
        session = get_active_session(chat_id)
        if not session:
            send_message(chat_id, "No active session. Use `/new <project>` first.")
            return True

        cwd = session["cwd"]

        def init_thread():
            try:
                send_message(chat_id, f"🔧 *Running claude init* in `{cwd}`...")
                process = subprocess.Popen(
                    ["claude", "init"],
                    cwd=cwd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True
                )
                stdout, stderr = process.communicate(timeout=120)
                output = (stdout or "").strip()
                error = (stderr or "").strip()

                if output:
                    # Truncate if needed
                    if len(output) > 3800:
                        output = output[:3800] + "\n\n... (truncated)"
                    send_message(chat_id, f"✅ *claude init complete:*\n\n{output}")
                elif error:
                    send_message(chat_id, f"⚠️ *claude init:*\n\n{error[:500]}")
                else:
                    send_message(chat_id, "✅ *claude init* completed (no output).")
            except subprocess.TimeoutExpired:
                process.kill()
                send_message(chat_id, "❌ claude init timed out.")
            except FileNotFoundError:
                send_message(chat_id, "❌ Claude CLI not found.")
            except Exception as e:
                send_message(chat_id, f"❌ claude init error: {str(e)[:200]}")

        threading.Thread(target=init_thread, daemon=True).start()
        return True

    if cmd in ("/file", "/f"):
        if not args.strip():
            send_message(chat_id, "Usage: `/file <path>`\nExample: `/file src/main.py`\nFuzzy: `/file .../main.py`")
            return True
        session = get_active_session(chat_id)
        file_path = args.strip()
        # Fuzzy path: .../something searches recursively under session cwd
        if file_path.startswith(".../") and session:
            target = file_path[4:]
            skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv", ".next", "dist", "build", ".cache", ".tox", "vendor"}
            matches = []
            search_root = session["cwd"]
            max_matches = 50
            try:
                for dirpath, dirnames, filenames in os.walk(search_root):
                    # Prune junk directories in-place to avoid descending into them
                    dirnames[:] = [d for d in dirnames if d not in skip_dirs]
                    # Check if target matches end of any file path in this dir
                    for fname in filenames:
                        rel = os.path.relpath(os.path.join(dirpath, fname), search_root)
                        if rel == target or rel.endswith(os.sep + target) or fname == target:
                            matches.append(os.path.join(dirpath, fname))
                            if len(matches) >= max_matches:
                                break
                    if len(matches) >= max_matches:
                        break
            except OSError:
                pass
            if not matches:
                send_message(chat_id, f"❌ No files matching `{file_path[4:]}` found in project.")
                return True
            if len(matches) == 1:
                file_path = matches[0]
            else:
                # Multiple matches — show list and let user pick
                lines = [f"Found {len(matches)} matches:"]
                for m in matches[:15]:
                    rel = os.path.relpath(m, session["cwd"])
                    lines.append(f"• `{rel}`")
                if len(matches) > 15:
                    lines.append(f"_...and {len(matches) - 15} more_")
                lines.append("\nUse the full relative path: `/file <path>`")
                send_message(chat_id, "\n".join(lines))
                return True
        # Resolve relative paths against session cwd
        elif not os.path.isabs(file_path) and session:
            file_path = os.path.join(session["cwd"], file_path)
        if not os.path.isfile(file_path):
            send_message(chat_id, f"❌ File not found: `{args.strip()}`")
            return True
        # Check file size (Telegram limit: 50MB)
        file_size = os.path.getsize(file_path)
        if file_size > 50 * 1024 * 1024:
            send_message(chat_id, f"❌ File too large ({file_size // (1024*1024)}MB). Telegram limit is 50MB.")
            return True
        # Send as photo if it's an image, otherwise as document
        image_exts = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
        ext = os.path.splitext(file_path)[1].lower()
        if ext in image_exts and file_size < 10 * 1024 * 1024:
            ok = send_photo(chat_id, file_path)
        else:
            ok = send_document(chat_id, file_path)
        if not ok:
            send_message(chat_id, f"❌ Failed to send file: `{os.path.basename(file_path)}`")
        return True

    if cmd == "/justdoit":
        session = get_active_session(chat_id)
        if not session:
            send_message(chat_id, "No active session. Use `/new <project>` first.")
            return True

        session_id = get_session_id(session)
        jdi_key = f"{chat_id}:{session_id}"

        if justdoit_active.get(jdi_key, {}).get("active"):
            send_message(chat_id, "⚠️ JustDoIt is already running on this session. Use `/cancel` to stop it first.")
            return True

        if session_id in active_processes:
            send_message(chat_id, "⚠️ Session is busy. Wait for it to finish or `/cancel` first.")
            return True

        if args.strip():
            task = args.strip()
        else:
            task = "Continue with the current plan. Review what we've discussed, then implement it fully with proper tests passing and production-ready code."

        thread = threading.Thread(
            target=run_justdoit_loop,
            args=(chat_id, task, session),
            daemon=True
        )
        thread.start()
        return True

    return False


def handle_callback_query(callback_query):
    """Handle inline keyboard button presses."""
    query_id = callback_query["id"]
    chat_id = callback_query["message"]["chat"]["id"]
    message_id = callback_query["message"]["message_id"]
    data = callback_query.get("data", "")

    chat_key = str(chat_id)

    answer_callback_query(query_id)
    edit_message_reply_markup(chat_id, message_id, None)  # Remove buttons

    # Handle session resume
    if data.startswith("resume_"):
        try:
            idx = int(data[7:])  # Remove "resume_" prefix
            user_data = user_sessions.get(chat_key, {})
            sessions = user_data.get("sessions", [])

            if 0 <= idx < len(sessions):
                s = sessions[idx]
                session_id = get_session_id(s)
                is_busy = session_id in active_processes
                set_active_session(chat_id, session_id)
                if is_busy:
                    send_message(chat_id, f"✅ Switched to `{s['name']}`\n\n🔄 _Task is still running. New messages will be queued._")
                else:
                    send_message(chat_id, f"✅ Resumed `{s['name']}`\n\nSend a message to continue!")
                return

        except (ValueError, IndexError):
            pass

        send_message(chat_id, "❌ Session not found.")
        return

    # Handle session delete
    if data.startswith("delete_"):
        try:
            if data == "delete_all":
                for s in user_sessions.get(chat_key, {}).get("sessions", []):
                    sid = get_session_id(s)
                    session_locks.pop(sid, None)
                    message_queue.pop(sid, None)
                user_sessions[chat_key] = {"sessions": [], "active": None}
                save_sessions()
                send_message(chat_id, "🗑️ All sessions deleted.")
                return

            idx = int(data[7:])  # Remove "delete_" prefix
            user_data = user_sessions.get(chat_key, {})
            sessions = user_data.get("sessions", [])

            if 0 <= idx < len(sessions):
                s = sessions[idx]
                deleted_name = s["name"]
                sid = get_session_id(s)
                if user_data.get("active") == sid:
                    user_data["active"] = None
                sessions.pop(idx)
                session_locks.pop(sid, None)
                message_queue.pop(sid, None)
                save_sessions()
                send_message(chat_id, f"🗑️ Deleted session `{deleted_name}`")
                return

        except (ValueError, IndexError):
            pass

        send_message(chat_id, "❌ Session not found.")
        return

    pending = pending_questions.get(chat_key)

    if not pending:
        send_message(chat_id, "This question has expired. Please try again.")
        return

    session = pending.get("session") or get_active_session(chat_id)
    current_idx = pending.get("current_idx", 0)
    questions = pending.get("questions", [])

    if data == "opt_other":
        send_message(chat_id, "Please type your response:")
        pending_questions[chat_key]["awaiting_text"] = True
        return

    if data.startswith("opt_"):
        try:
            opt_idx = int(data.split("_")[1])
            if current_idx < len(questions):
                options = questions[current_idx].get("options", [])
                if opt_idx < len(options):
                    selected = options[opt_idx]
                    label = selected.get("label", selected) if isinstance(selected, dict) else str(selected)

                    send_message(chat_id, f"Selected: *{label}*")

                    # Store this answer
                    pending["answers"][current_idx] = label

                    # Move to next question
                    pending["current_idx"] = current_idx + 1

                    if pending["current_idx"] < len(questions):
                        # More questions to answer - send the next one
                        send_pending_question(chat_id, pending)
                    else:
                        # All questions answered - build combined answer and send to Claude
                        answers = pending["answers"]
                        pending_questions.pop(chat_key, None)

                        # Build answer text from all responses
                        if len(answers) == 1:
                            answer_text = answers[0]
                        else:
                            parts = []
                            for i in range(len(answers)):
                                q_header = questions[i].get("header", f"Q{i+1}")
                                parts.append(f"{q_header}: {answers[i]}")
                            answer_text = "\n".join(parts)

                        # Send to Claude non-blocking with streaming
                        if session:
                            s_id = get_session_id(session)
                            s_lock = get_session_lock(s_id)
                            with s_lock:
                                active_processes[s_id] = None
                            run_claude_in_thread(chat_id, answer_text, session)
                    return
        except (ValueError, IndexError):
            pass

    pending_questions.pop(chat_key, None)


def run_claude_in_thread(chat_id, text, session=None):
    """Run Claude in a background thread."""
    chat_key = str(chat_id)
    session_id = get_session_id(session) if session else None

    # Save last prompt for context
    if session:
        update_session_last_prompt(chat_id, session, text)

    def claude_task():
        try:
            if session:
                # Check if proactive compaction is needed BEFORE sending to Claude
                needs_compaction = increment_message_count(chat_id, session)

                if needs_compaction:
                    send_message(chat_id, "📦 *Proactive compaction* - summarizing context...")

                    # Get summary before context gets too long
                    summary_prompt = """Summarize this session for context continuity (max 500 words). Focus on ACTIONABLE STATE:
1. Files being edited — exact paths and what changed
2. Current task — what's in progress, what's done, what's left
3. Key decisions — architectural choices, approaches chosen and WHY
4. Bugs/issues — any errors encountered and their status (fixed/open)
5. Code snippets — any critical code patterns or values needed to continue

Omit: greetings, abandoned approaches, resolved debugging back-and-forth.
Format as a compact bullet list."""

                    try:
                        summary_response, _, _, _, _ = run_claude_streaming(
                            summary_prompt, chat_id, cwd=session["cwd"], continue_session=True,
                            session_id=session_id, session=session
                        )
                        summary = summary_response.split("———")[0].strip() if summary_response else ""
                    except:
                        summary = ""

                    # Persist summary before clearing session (survives crashes)
                    if summary and len(summary) > 50:
                        save_session_summary(chat_id, session, summary)

                    # Reset the session
                    update_claude_session_id(chat_id, session, None)
                    reset_message_count(chat_id, session)

                    # Continue with fresh session + context
                    if summary and len(summary) > 50:
                        text_with_context = f"""[Session compacted - Previous context summary:]
{summary}

[New request:]
{text}"""
                        send_message(chat_id, "🔄 Context preserved. Continuing...")
                    else:
                        text_with_context = text
                        send_message(chat_id, "🔄 Session refreshed. Continuing...")

                    response, questions, _, claude_sid, context_overflow = run_claude_streaming(
                        text_with_context, chat_id, cwd=session["cwd"], continue_session=True,
                        session_id=session_id, session=session
                    )
                else:
                    response, questions, _, claude_sid, context_overflow = run_claude_streaming(
                        text, chat_id, cwd=session["cwd"], continue_session=True,
                        session_id=session_id, session=session
                    )

                # Fallback: Smart compaction on context overflow (if proactive didn't catch it)
                if context_overflow:
                    send_message(chat_id, "⚠️ *Context too long* - compacting session...")

                    # First, ask Claude to summarize the conversation context (using old session)
                    summary_prompt = """Summarize this session for context continuity (max 500 words). Focus on ACTIONABLE STATE:
1. Files being edited — exact paths and what changed
2. Current task — what's in progress, what's done, what's left
3. Key decisions — architectural choices, approaches chosen and WHY
4. Bugs/issues — any errors encountered and their status (fixed/open)
5. Code snippets — any critical code patterns or values needed to continue

Omit: greetings, abandoned approaches, resolved debugging back-and-forth.
Format as a compact bullet list. This will be used to restore context after reset."""

                    # Try to get summary from the old session (may fail if too long)
                    try:
                        summary_response, _, _, _, _ = run_claude_streaming(
                            summary_prompt, chat_id, cwd=session["cwd"], continue_session=True,
                            session_id=session_id, session=session
                        )
                        # Extract just the summary text (remove completion indicators)
                        summary = summary_response.split("———")[0].strip() if summary_response else ""
                    except:
                        summary = ""

                    # Persist summary before clearing session (survives crashes)
                    if summary and len(summary) > 50:
                        save_session_summary(chat_id, session, summary)

                    # Reset the session
                    update_claude_session_id(chat_id, session, None)
                    reset_message_count(chat_id, session)

                    # Retry with fresh session, including summary as context
                    if summary and len(summary) > 50:
                        context_prompt = f"""[Session compacted - Previous context summary:]
{summary}

[New request:]
{text}"""
                        send_message(chat_id, "🔄 Session reset with context preserved. Continuing...")
                    else:
                        context_prompt = text
                        send_message(chat_id, "🔄 Session reset. Continuing with fresh context...")

                    response, questions, _, claude_sid, _ = run_claude_streaming(
                        context_prompt, chat_id, cwd=session["cwd"], continue_session=True,
                        session_id=session_id, session=session
                    )

                # Save Claude's session ID for future --resume
                if claude_sid and session:
                    update_claude_session_id(chat_id, session, claude_sid)
            else:
                response, questions, _, _, _ = run_claude_streaming(text, chat_id)

            if questions:
                set_pending_questions(chat_id, questions, session)

            # Process queued messages for this session
            process_message_queue(chat_id, session)
        except Exception as e:
            print(f"Error in claude thread: {e}")
            if session_id:
                active_processes.pop(session_id, None)

    thread = threading.Thread(target=claude_task, daemon=True)
    thread.start()


def process_message_queue(chat_id, session=None):
    """Process queued messages for a session."""
    if not session:
        session = get_active_session(chat_id)
    if not session:
        return

    session_id = get_session_id(session)
    lock = get_session_lock(session_id)
    with lock:
        if message_queue.get(session_id):
            queued_text = message_queue[session_id].pop(0)
            # Mark as active under lock before launching thread
            active_processes[session_id] = None
        else:
            return

    # Run next queued message in thread (outside lock)
    run_claude_in_thread(chat_id, queued_text, session)


def handle_message(chat_id, text):
    """Handle a regular message."""
    chat_key = str(chat_id)

    # Block non-command input during justdoit mode on the active session
    session = get_active_session(chat_id)
    if session:
        jdi_key = f"{chat_id}:{get_session_id(session)}"
        jdi_state = justdoit_active.get(jdi_key, {})
        if jdi_state.get("active"):
            send_message(chat_id,
                f"🤖 *JustDoIt is running* on `{session.get('name', '?')}` "
                f"(step {jdi_state.get('step', '?')} — {jdi_state.get('phase', 'implementing')})\n\n"
                f"_Switch to another session with /switch, use /cancel to stop, or wait for completion._\n"
                f"_Commands like /status and /sessions still work._")
            return

    # Check if awaiting text response for "Other" option
    pending = pending_questions.get(chat_key)
    if pending and pending.get("awaiting_text"):
        pending["awaiting_text"] = False
        session = pending.get("session") or get_active_session(chat_id)
        current_idx = pending.get("current_idx", 0)
        questions = pending.get("questions", [])

        # Store this answer
        pending["answers"][current_idx] = text
        pending["current_idx"] = current_idx + 1

        if pending["current_idx"] < len(questions):
            # More questions - send the next one
            send_pending_question(chat_id, pending)
        else:
            # All questions answered - send combined answer to Claude
            answers = pending["answers"]
            pending_questions.pop(chat_key, None)

            if len(answers) == 1:
                answer_text = answers[0]
            else:
                parts = []
                for i in range(len(answers)):
                    q_header = questions[i].get("header", f"Q{i+1}") if i < len(questions) else f"Q{i+1}"
                    parts.append(f"{q_header}: {answers[i]}")
                answer_text = "\n".join(parts)

            if session:
                session_id = get_session_id(session)
                lock = get_session_lock(session_id)
                with lock:
                    active_processes[session_id] = None
                run_claude_in_thread(chat_id, answer_text, session)
        return

    # Get active session
    session = get_active_session(chat_id)
    session_id = get_session_id(session) if session else str(chat_id)

    # Atomically check if Claude is running and either queue or launch
    lock = get_session_lock(session_id)
    with lock:
        if session_id in active_processes:
            # Queue the message for this session
            if session_id not in message_queue:
                message_queue[session_id] = []
            message_queue[session_id].append(text)
            queue_pos = len(message_queue[session_id])
            session_name = session.get("name", "default") if session else "default"
            send_message(chat_id, f"📋 _Message queued (#{queue_pos}) for session `{session_name}`. Will process after current task._")
            return

        # Check memory pressure before launching new Claude process
        mem_ok, avail_mb = check_memory_pressure()
        if not mem_ok:
            n_active = len(active_processes)
            send_message(chat_id, f"⚠️ _Low memory ({avail_mb:.0f} MB free, {n_active} active sessions). "
                        f"Please wait for a session to finish or use /cancel._")
            print(f"[MEMORY] Refused new session: {avail_mb:.0f} MB available, {n_active} active", flush=True)
            return

        # Mark as active immediately under the lock to prevent races
        active_processes[session_id] = None  # placeholder until real process starts

    # Regular message - send to Claude with streaming in background
    run_claude_in_thread(chat_id, text, session)


def main():
    global last_update_id

    load_sessions()
    check_interrupted_sessions()
    check_interrupted_tasks()

    # Register bot commands for the Telegram menu button
    try:
        commands = [
            {"command": "new", "description": "Start new session - /new <project>"},
            {"command": "resume", "description": "Pick a session to resume"},
            {"command": "sessions", "description": "List all sessions"},
            {"command": "status", "description": "Show current session info"},
            {"command": "plan", "description": "Enter plan mode"},
            {"command": "approve", "description": "Approve current plan"},
            {"command": "reject", "description": "Reject current plan"},
            {"command": "cancel", "description": "Cancel current task"},
            {"command": "justdoit", "description": "Autonomous implementation mode"},
            {"command": "codex", "description": "Run Codex task"},
            {"command": "file", "description": "Download a file - /file <path>"},
            {"command": "reset", "description": "Clear conversation history"},
            {"command": "delete", "description": "Delete a session"},
            {"command": "init", "description": "Run claude init"},
            {"command": "help", "description": "Show help"},
        ]
        resp = requests.post(f"{API_URL}/setMyCommands", json={"commands": commands}, timeout=10)
        if resp.json().get("ok"):
            print("Bot menu commands registered.")
        else:
            print(f"Failed to register commands: {resp.json().get('description')}")
    except Exception as e:
        print(f"Error registering commands: {e}")

    print("Claude Telegram Bot started!")
    print(f"Allowed chat IDs: {ALLOWED_CHAT_IDS}")
    print(f"Projects directory: {BASE_PROJECTS_DIR}")

    # Start memory monitor thread
    def memory_monitor():
        def get_rss_mb():
            """Get current RSS in MB from /proc/self/status."""
            try:
                with open("/proc/self/status") as f:
                    for l in f:
                        if l.startswith("VmRSS:"):
                            return int(l.split()[1]) / 1024
            except Exception:
                pass
            return 0

        while True:
            try:
                rss_mb = get_rss_mb()
                if rss_mb > 500:
                    print(f"[MEMORY] RSS: {rss_mb:.0f} MB, active_processes: {len(active_processes)}, "
                          f"justdoit: {len([k for k,v in justdoit_active.items() if v.get('active')])}, "
                          f"threads: {threading.active_count()}", flush=True)
                if rss_mb > 2000:
                    print(f"[MEMORY] WARNING: RSS exceeds 2GB ({rss_mb:.0f} MB)! "
                          f"Forcing garbage collection and malloc_trim.", flush=True)
                    import gc
                    gc.collect()
                    _malloc_trim()
                    rss_after = get_rss_mb()
                    print(f"[MEMORY] After GC+trim: {rss_after:.0f} MB", flush=True)
            except Exception as e:
                print(f"[MEMORY] Monitor error: {e}", flush=True)
            time.sleep(30)

    threading.Thread(target=memory_monitor, daemon=True).start()

    while True:
        updates = get_updates(last_update_id + 1)

        for update in updates:
            last_update_id = update["update_id"]

            try:
                # Handle callback queries (button presses)
                if "callback_query" in update:
                    callback_query = update["callback_query"]
                    chat_id = callback_query["message"]["chat"]["id"]

                    if is_allowed(chat_id):
                        handle_callback_query(callback_query)
                    continue

                # Handle messages
                message = update.get("message", {})
                chat_id = message.get("chat", {}).get("id")

                if not chat_id:
                    continue

                if not is_allowed(chat_id):
                    print(f"Unauthorized access attempt from chat_id: {chat_id}")
                    send_message(chat_id, "Unauthorized. Your chat ID is not in the allowed list.")
                    continue

                # Get text from message or caption (for photos/files)
                text = message.get("text", "") or message.get("caption", "")

                # Handle photo uploads
                if message.get("photo"):
                    # Get the largest photo (last in array)
                    photo = message["photo"][-1]
                    file_id = photo.get("file_id")

                    send_message(chat_id, "📷 _Downloading image..._")
                    local_path = download_telegram_file(file_id, "image.jpg")

                    if local_path:
                        # Create prompt with image path for Claude to read
                        prompt = f"[User uploaded an image: {local_path}]\n\n"
                        if text:
                            prompt += text
                        else:
                            prompt += "Please analyze this image."

                        print(f"Received photo from {chat_id}, saved to {local_path}")
                        handle_message(chat_id, prompt)
                    else:
                        send_message(chat_id, "❌ Failed to download image.")
                    continue

                # Handle document/file uploads
                if message.get("document"):
                    doc = message["document"]
                    file_id = doc.get("file_id")
                    file_name = doc.get("file_name", "file")
                    file_size = doc.get("file_size", 0)

                    # Limit file size (50MB)
                    if file_size > 50 * 1024 * 1024:
                        send_message(chat_id, "❌ File too large. Maximum size is 50MB.")
                        continue

                    send_message(chat_id, f"📄 _Downloading {file_name}..._")
                    local_path = download_telegram_file(file_id, file_name)

                    if local_path:
                        # Create prompt with file path for Claude to read
                        prompt = f"[User uploaded a file: {local_path}]\n\n"
                        if text:
                            prompt += text
                        else:
                            prompt += "Please analyze this file."

                        print(f"Received file from {chat_id}: {file_name}, saved to {local_path}")
                        handle_message(chat_id, prompt)
                    else:
                        send_message(chat_id, "❌ Failed to download file.")
                    continue

                # Skip if no text content
                if not text:
                    continue

                print(f"Received from {chat_id}: {text[:50]}...")

                # Handle commands
                if text.startswith("/"):
                    if handle_command(chat_id, text):
                        continue

                # Handle regular messages
                handle_message(chat_id, text)

            except Exception as e:
                print(f"Error processing update {update.get('update_id')}: {e}", flush=True)
                import traceback
                traceback.print_exc()

        time.sleep(1)


if __name__ == "__main__":
    main()
