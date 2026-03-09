#!/usr/bin/env python3
"""
Telegram bot that forwards messages to Claude CLI with session support.
Supports interactive prompts, plan mode, and multiple working directories.
"""

import os
import re
import signal
import subprocess
import requests
import time
import json
import threading
import uuid
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
CODEX_MODEL = os.environ.get("CODEX_MODEL", "gpt-5.3-codex")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-pro-preview")

API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
DATA_DIR = Path(__file__).parent / "data"
SESSIONS_FILE = DATA_DIR / "sessions.json"
ACTIVE_TASKS_FILE = DATA_DIR / "active_tasks.json"  # Track running tasks for crash recovery
ACTIVE_SESSIONS_FILE = DATA_DIR / "active_sessions.json"  # Track running Claude processes for crash recovery
SCHEDULED_TASKS_FILE = DATA_DIR / "scheduled_tasks.json"
UPLOADS_DIR = DATA_DIR / "uploads"  # Directory for downloaded files

last_update_id = 0

# In-memory state
user_sessions = {}  # chat_id -> {sessions: [], active: session_id}
pending_questions = {}  # chat_id -> {questions: [], answers: {}, current_idx: 0, session}
active_processes = {}  # session_id -> subprocess.Popen (allows parallel sessions)
message_queue = {}  # session_id -> [queued messages]
justdoit_active = {}  # "chat_id:session_id" -> {"active": True, "task": str, "step": int, "chat_id": str}
deepreview_active = {}  # "chat_id:session_id" -> {"active": True, "phase": str, "step": int, ...}
session_locks = {}  # session_id -> threading.Lock (prevents race conditions)
session_locks_lock = threading.Lock()  # protects session_locks dict itself
_sessions_file_lock = threading.Lock()  # protects user_sessions dict and sessions.json writes

omni_active = {}  # "chat_id:session_id" -> state
cancelled_sessions = set()  # session_ids explicitly cancelled via /cancel
user_feedback_queue = {}  # "chat_id:session_id" -> [messages] — user messages during justdoit/omni
scheduled_tasks = {}  # task_id -> {id, chat_id, session_name, prompt, schedule_type, cron_expr, ...}
_scheduled_tasks_lock = threading.Lock()
_scheduler_generation = 0


def save_active_tasks():
    """Persist active justdoit/omni tasks to disk for crash recovery detection."""
    try:
        tasks = {}
        for state_dict, mode in [
            (justdoit_active, "justdoit"),
            (omni_active, "omni"),
            (deepreview_active, "deepreview"),
        ]:
            for key, state in list(state_dict.items()):
                if state.get("active"):
                    tasks[key] = {
                        "started": state.get("started", time.time()),
                        "task": (state.get("task", "") or "")[:200],
                        "step": state.get("step", 0),
                        "phase": state.get("phase", ""),
                        "chat_id": state.get("chat_id", ""),
                        "session_name": state.get("session_name", ""),
                        "type": mode,
                        "paused": state.get("paused", False),
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
        tmp_file = ACTIVE_SESSIONS_FILE.with_suffix(".tmp")
        with open(tmp_file, "w") as f:
            json.dump(sessions_dict, f)
        tmp_file.replace(ACTIVE_SESSIONS_FILE)  # Atomic on POSIX
    except Exception as e:
        print(f"Error saving active sessions: {e}")


def get_active_sessions_data():
    """Return data from active_sessions.json (for API use)."""
    with _active_sessions_lock:
        try:
            with open(ACTIVE_SESSIONS_FILE) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pass
    return {}


def mark_session_active(chat_id, session_name, session_id, prompt):
    """Record that a Claude process is running for this session."""
    # Strip context bridge prefix so crash recovery shows the actual user prompt
    if "[NEW REQUEST]\n" in prompt:
        prompt = prompt.split("[NEW REQUEST]\n", 1)[1]
    elif "[NEW TASK]\n" in prompt:
        prompt = prompt.split("[NEW TASK]\n", 1)[1]
    with _active_sessions_lock:
        try:
            with open(ACTIVE_SESSIONS_FILE) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
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
            with open(ACTIVE_SESSIONS_FILE) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return
        try:
            data.pop(session_id, None)
            if data:
                _save_active_sessions_file(data)
            else:
                ACTIVE_SESSIONS_FILE.unlink(missing_ok=True)
        except Exception as e:
            print(f"Error clearing active session {session_id}: {e}")


def check_interrupted_sessions():
    """On startup, check if any sessions were interrupted by a crash and notify users."""
    try:
        with open(ACTIVE_SESSIONS_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return

    try:

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
    """On startup, check if justdoit/omni tasks were interrupted by a crash and notify users."""
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
                task_type = info.get("type", "justdoit")
                type_label = {"justdoit": "JustDoIt", "omni": "Omni"}.get(task_type, task_type.title())
                msg += f"\n• *{session_name}* {type_label} step {step}"
                if phase:
                    msg += f" ({phase})"
                msg += f": _{task_desc[:100]}_"
            msg += "\n\n_Sessions preserved. Use the original command to restart or send a message to continue manually._"
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
    with _sessions_file_lock:
        if SESSIONS_FILE.exists():
            try:
                with open(SESSIONS_FILE) as f:
                    user_sessions = json.load(f)
            except Exception as e:
                print(f"Error loading sessions: {e}")
                user_sessions = {}


_save_sessions_last = 0  # Timestamp of last actual save
_save_sessions_dirty = False  # Whether there are unsaved changes
_SAVE_DEBOUNCE_SECS = 5  # Minimum seconds between disk writes


def save_sessions(force=False):
    """Save sessions to disk atomically. Debounced to avoid excessive I/O.

    Args:
        force: If True, write immediately regardless of debounce timer.
               Use for important state changes (session creation, session ID updates).
    """
    global _save_sessions_last, _save_sessions_dirty
    now = time.time()

    if not force and (now - _save_sessions_last) < _SAVE_DEBOUNCE_SECS:
        _save_sessions_dirty = True
        return

    DATA_DIR.mkdir(exist_ok=True)
    with _sessions_file_lock:
        tmp_file = SESSIONS_FILE.with_suffix(".tmp")
        try:
            with open(tmp_file, "w") as f:
                json.dump(user_sessions, f, indent=2)
            tmp_file.replace(SESSIONS_FILE)  # Atomic on POSIX
            _save_sessions_last = now
            _save_sessions_dirty = False
        except Exception as e:
            print(f"Error saving sessions: {e}")
            try:
                tmp_file.unlink(missing_ok=True)
            except Exception:
                pass


def _flush_sessions_if_dirty():
    """Called periodically to flush any debounced session changes to disk."""
    if _save_sessions_dirty:
        save_sessions(force=True)


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


_api_module = None  # Set in main() after api.py is loaded
_ws_suppress = threading.local()  # Per-thread flag to suppress legacy WS broadcasts
_ws_session_override = threading.local()  # Per-thread session name for WS broadcasts (avoids get_active_session races)


def _ws_broadcast(chat_id, event_type, data):
    """Broadcast to WebSocket clients. No-op if API server not loaded."""
    if _api_module:
        try:
            _api_module.broadcast_ws(chat_id, event_type, data)
        except Exception:
            pass  # WS is independent — never affect TG delivery


def _ws_broadcast_status(chat_id, mode, phase, step, active=True, task="", started=0, paused=False):
    """Broadcast task status update to WS clients."""
    _ws_broadcast(chat_id, "status", {
        "mode": mode,
        "phase": phase,
        "step": step,
        "active": active,
        "paused": paused,
        "task": task[:200] if task else "",
        "started": int(started) if started else 0,
        "session": getattr(_ws_session_override, 'name', '') or "",
    })


def _ws_broadcast_schedule(chat_id, event, task_id, task):
    """Broadcast a schedule event (created/updated/deleted/triggered) over WS."""
    _ws_broadcast(chat_id, "schedule", {
        "event": event,
        "task_id": task_id,
        "task": {
            "id": task["id"],
            "session_name": task["session_name"],
            "prompt": (task.get("prompt") or "")[:200],
            "schedule_type": task["schedule_type"],
            "cron_expr": task.get("cron_expr"),
            "run_at": task.get("run_at"),
            "mode": task["mode"],
            "enabled": task["enabled"],
            "next_run": task.get("next_run"),
            "last_run": task.get("last_run"),
            "run_count": task.get("run_count", 0),
        }
    })


# --- Cron parser (no external deps) ---

_CRON_ALIASES = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
}

_DOW_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
_MONTH_NAMES = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
                "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


def _parse_cron_field(field, lo, hi, names=None):
    """Parse a single cron field into a set of ints."""
    result = set()
    for part in field.split(","):
        part = part.strip().lower()
        if names:
            for name, val in names.items():
                part = part.replace(name, str(val))
        if part == "*":
            result.update(range(lo, hi + 1))
        elif part.startswith("*/"):
            step = int(part[2:])
            result.update(range(lo, hi + 1, step))
        elif "-" in part:
            if "/" in part:
                range_part, step = part.split("/")
                a, b = range_part.split("-")
                result.update(range(int(a), int(b) + 1, int(step)))
            else:
                a, b = part.split("-")
                result.update(range(int(a), int(b) + 1))
        else:
            result.add(int(part))
    return result


def _parse_cron_expr(expr):
    """Parse a cron expression into a dict of sets for matching.
    Supports 5 fields: minute hour dom month dow.
    """
    expr = _CRON_ALIASES.get(expr.strip().lower(), expr.strip())
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"Invalid cron expression: {expr!r} (expected 5 fields)")
    dow_cron = _parse_cron_field(fields[4], 0, 6, _DOW_NAMES)
    # Pre-convert cron DOW (0=Sun) to Python weekday (0=Mon) to avoid rebuilding set on every match
    dow_py = frozenset((d - 1) % 7 for d in dow_cron)
    return {
        "minute": _parse_cron_field(fields[0], 0, 59),
        "hour": _parse_cron_field(fields[1], 0, 23),
        "dom": _parse_cron_field(fields[2], 1, 31),
        "month": _parse_cron_field(fields[3], 1, 12, _MONTH_NAMES),
        "dow": dow_py,
    }


def _cron_matches(parsed, dt):
    """Check if a datetime matches a parsed cron expression."""
    return (dt.minute in parsed["minute"]
            and dt.hour in parsed["hour"]
            and dt.day in parsed["dom"]
            and dt.month in parsed["month"]
            and dt.weekday() in parsed["dow"]
            )


def _next_cron_run(cron_expr, after_dt):
    """Compute the next datetime after `after_dt` that matches the cron expression.
    Minute-by-minute scan, capped at 366 days.
    """
    parsed = _parse_cron_expr(cron_expr)
    dt = after_dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = after_dt + timedelta(days=366)
    while dt <= limit:
        if _cron_matches(parsed, dt):
            return dt
        dt += timedelta(minutes=1)
    return None


# --- Scheduled tasks persistence ---

def load_scheduled_tasks():
    """Load scheduled_tasks from data/scheduled_tasks.json."""
    global scheduled_tasks
    try:
        with open(SCHEDULED_TASKS_FILE) as f:
            scheduled_tasks = json.load(f)
        print(f"Loaded {len(scheduled_tasks)} scheduled task(s).", flush=True)
    except (FileNotFoundError, json.JSONDecodeError):
        scheduled_tasks = {}


def save_scheduled_tasks():
    """Atomically save scheduled_tasks to disk."""
    with _scheduled_tasks_lock:
        try:
            DATA_DIR.mkdir(exist_ok=True)
            if scheduled_tasks:
                tmp = SCHEDULED_TASKS_FILE.with_suffix(".tmp")
                with open(tmp, "w") as f:
                    json.dump(scheduled_tasks, f, indent=2)
                tmp.replace(SCHEDULED_TASKS_FILE)
            else:
                SCHEDULED_TASKS_FILE.unlink(missing_ok=True)
        except Exception as e:
            print(f"Error saving scheduled tasks: {e}", flush=True)


def create_scheduled_task(chat_id, session_name, prompt, schedule_type, cron_expr=None, run_at=None, mode="justdoit"):
    """Create a new scheduled task. Returns (task_id, task_dict) or raises ValueError."""
    import uuid
    task_id = f"sched_{uuid.uuid4().hex[:8]}"

    # Validate
    if schedule_type == "cron":
        if not cron_expr:
            raise ValueError("cron_expr required for cron schedule")
        _parse_cron_expr(cron_expr)  # Validate syntax
        next_run_dt = _next_cron_run(cron_expr, datetime.now())
        if not next_run_dt:
            raise ValueError(f"No matching time found for cron expression: {cron_expr}")
        next_run = next_run_dt.timestamp()
    elif schedule_type == "once":
        if not run_at:
            raise ValueError("run_at required for once schedule")
        # Normalize space separator to T for Python <3.11 fromisoformat compatibility
        run_at_dt = datetime.fromisoformat(run_at.replace(" ", "T", 1))
        if run_at_dt <= datetime.now():
            raise ValueError("run_at must be in the future")
        next_run = run_at_dt.timestamp()
    else:
        raise ValueError(f"Invalid schedule_type: {schedule_type}")

    if mode not in ("justdoit", "remind"):
        raise ValueError(f"Invalid mode: {mode}")

    task = {
        "id": task_id,
        "chat_id": str(chat_id),
        "session_name": session_name,
        "prompt": prompt,
        "schedule_type": schedule_type,
        "cron_expr": cron_expr,
        "run_at": run_at,
        "mode": mode,
        "enabled": True,
        "created_at": time.time(),
        "last_run": None,
        "next_run": next_run,
        "run_count": 0,
    }

    with _scheduled_tasks_lock:
        scheduled_tasks[task_id] = task
    save_scheduled_tasks()
    _ws_broadcast_schedule(int(chat_id), "created", task_id, task)
    return task_id, task


# --- Scheduler thread ---

def _trigger_scheduled_task(task_id, task):
    """Execute a due scheduled task: launch JustDoIt or send a reminder."""
    chat_id = int(task["chat_id"])
    session_name = task["session_name"]

    # Find the session
    user_data = user_sessions.get(str(chat_id), {})
    session = None
    for s in user_data.get("sessions", []):
        if s.get("name") == session_name:
            session = s
            break

    if not session:
        send_message(chat_id, f"⚠️ Scheduled task `{task_id}` skipped: session `{session_name}` not found.")
        with _scheduled_tasks_lock:
            task["enabled"] = False
        save_scheduled_tasks()
        _ws_broadcast_schedule(chat_id, "updated", task_id, task)
        return

    # Update task state
    with _scheduled_tasks_lock:
        task["last_run"] = time.time()
        task["run_count"] = task.get("run_count", 0) + 1
        if task["schedule_type"] == "once":
            task["enabled"] = False
            task["next_run"] = None
        else:
            nxt = _next_cron_run(task["cron_expr"], datetime.now())
            task["next_run"] = nxt.timestamp() if nxt else None
    save_scheduled_tasks()
    _ws_broadcast_schedule(chat_id, "triggered", task_id, task)

    if task["mode"] == "justdoit":
        session_id = get_session_id(session)
        jdi_key = f"{chat_id}:{session_id}"
        if (justdoit_active.get(jdi_key, {}).get("active")
                or omni_active.get(jdi_key, {}).get("active")
                or deepreview_active.get(jdi_key, {}).get("active")
                or session_id in active_processes):
            send_message(chat_id, f"⏰ Scheduled task fired but session `{session_name}` is busy. Skipping.\n\nTask: _{task['prompt'][:200]}_")
            return
        send_message(chat_id, f"⏰ *Scheduled task triggered*\nSession: `{session_name}`\nTask: _{task['prompt'][:200]}_")
        thread = threading.Thread(
            target=run_justdoit_loop,
            args=(chat_id, task["prompt"], session),
            daemon=True,
        )
        thread.start()
    elif task["mode"] == "remind":
        send_message(chat_id, f"⏰ *Scheduled reminder*\nSession: `{session_name}`\n\n{task['prompt']}")


def _start_scheduler():
    """Start the scheduler daemon thread. Uses generation counter to retire old threads on hot-reload."""
    global _scheduler_generation
    _scheduler_generation += 1
    gen = _scheduler_generation

    def scheduler_loop():
        while _scheduler_generation == gen:
            try:
                now = time.time()
                with _scheduled_tasks_lock:
                    due = [(tid, t) for tid, t in scheduled_tasks.items()
                           if t.get("enabled") and t.get("next_run") and t["next_run"] <= now]
                for task_id, task in due:
                    try:
                        _trigger_scheduled_task(task_id, task)
                    except Exception as e:
                        print(f"[Scheduler] Error triggering {task_id}: {e}", flush=True)
            except Exception as e:
                print(f"[Scheduler] Loop error: {e}", flush=True)
            time.sleep(30)
        print(f"[Scheduler] Generation {gen} retired.", flush=True)

    threading.Thread(target=scheduler_loop, daemon=True).start()
    print(f"[Scheduler] Started (generation {gen}).", flush=True)


def _check_pause(state_dict, chat_key, chat_id, mode, phase, step):
    """Check if a task is paused and block until resumed. Returns False if cancelled."""
    state = state_dict.get(chat_key, {})
    if not state.get("active", False):
        return False
    if state.get("paused", False):
        _ws_broadcast_status(chat_id, mode, phase, step, paused=True)
        print(f"[{mode}] {chat_key} paused at step {step}, phase {phase}", flush=True)
        resume_event = state.get("resume_event")
        if resume_event:
            resume_event.wait()  # Block until resumed or cancelled
        # After waking, re-check active (cancel while paused)
        state = state_dict.get(chat_key, {})
        if not state.get("active", False):
            return False
        _ws_broadcast_status(chat_id, mode, phase, step, paused=False)
        print(f"[{mode}] {chat_key} resumed at step {step}, phase {phase}", flush=True)
    return True


def _ws_stream(chat_id, op, message_id, session="", **kwargs):
    """Send a WS-native stream event for the app.
    Unlike TG edits (full text, 1/sec), these carry lightweight deltas.
    ops: start, append, replace_last, tool, done
    """
    data = {"op": op, "message_id": message_id, "session": session, **kwargs}
    _ws_broadcast(chat_id, "stream", data)


def send_message(chat_id, text, reply_markup=None, parse_mode="Markdown", retries=3, session_name=None):
    """Send a message back to the user. Returns message_id.
    Retries on network/timeout errors with exponential backoff.
    Also broadcasts via WebSocket unless _ws_suppress is set (stream events replace it).
    session_name: if provided, use this as the WS session label instead of get_active_session().
    """
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

        chunk_msg_id = None
        for attempt in range(retries):
            try:
                # Use shorter timeout (3s connect, 7s read) to prevent blocking the app during network drops
                resp = requests.post(f"{API_URL}/sendMessage", json=payload, timeout=(3.0, 7.0))
                result = resp.json()
                if not result.get("ok") and parse_mode:
                    # Retry without markdown
                    payload.pop("parse_mode", None)
                    resp = requests.post(f"{API_URL}/sendMessage", json=payload, timeout=(3.0, 7.0))
                    result = resp.json()
                if result.get("ok"):
                    chunk_msg_id = result.get("result", {}).get("message_id")
                    if i == 0:
                        message_id = chunk_msg_id
                break  # Success or non-retryable API error
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                if attempt < retries - 1:
                    wait = 2 ** attempt  # 1s, 2s, 4s
                    print(f"send_message retry {attempt+1}/{retries} after {wait}s: {e}", flush=True)
                    time.sleep(wait)
                else:
                    print(f"send_message failed after {retries} attempts: {e}", flush=True)
            except Exception as e:
                print(f"Error sending message: {e}", flush=True)
                break  # Non-network error, don't retry

        if chunk_msg_id is None:
            # Generate pseudo-ID so WS clients can still receive and edit this message
            chunk_msg_id = -int(time.time() * 1000) % 1000000000
            if i == 0:
                message_id = chunk_msg_id

        # Broadcast via WebSocket (independent of TG success/failure)
        # Suppressed when streaming — stream events replace legacy message/edit broadcasts
        if not getattr(_ws_suppress, 'active', False):
            # Session name priority: explicit param > thread-local override > active session lookup
            _override = getattr(_ws_session_override, 'name', None)
            if session_name is not None:
                _sess_name = session_name
            elif _override is not None:
                _sess_name = _override
            else:
                _session = get_active_session(chat_id)
                _sess_name = _session.get("name", "") if _session else ""
            ws_data = {"text": chunk, "message_id": chunk_msg_id, "session": _sess_name}
            # Include inline keyboard buttons in WS payload (last chunk only)
            if reply_markup and i == len(chunks) - 1:
                ws_data["reply_markup"] = reply_markup
            _ws_broadcast(chat_id, "message", ws_data)

    return message_id


def send_message_no_ws(chat_id, text, reply_markup=None, parse_mode="Markdown"):
    """Send a message to TG only, without broadcasting via WebSocket.
    Used for echo messages from the app (app already shows them locally).
    """
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = requests.post(f"{API_URL}/sendMessage", json=payload, timeout=30)
        result = resp.json()
        if not result.get("ok") and parse_mode:
            payload.pop("parse_mode", None)
            resp = requests.post(f"{API_URL}/sendMessage", json=payload, timeout=30)
    except Exception as e:
        print(f"send_message_no_ws error: {e}", flush=True)


_last_edit_time = {}  # message_id -> timestamp
_last_edit_cleanup = 0  # timestamp of last cleanup
EDIT_MIN_INTERVAL = 1.0  # Minimum seconds between edits to the same message


def edit_message(chat_id, message_id, text, parse_mode="Markdown", force=False):
    """Edit an existing message. Rate-limited to 1 edit/sec per message.
    Also broadcasts via WebSocket unless _ws_suppress is set (stream events replace it).
    """
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

    # Broadcast edit via WebSocket (suppressed during streaming — stream events replace it)
    if not getattr(_ws_suppress, 'active', False):
        _override = getattr(_ws_session_override, 'name', None)
        if _override is not None:
            _sess_name = _override
        else:
            _session = get_active_session(chat_id)
            _sess_name = _session.get("name", "") if _session else ""
        _ws_broadcast(chat_id, "edit", {"message_id": message_id, "text": text, "session": _sess_name})

    if message_id < 0:
        # Pseudo-ID means the original Telegram send failed. Cannot edit on TG.
        # Don't retry send — would create duplicate if network recovers mid-retry.
        return

    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode

    # Retry logic for forced edits (final updates that must reach the user)
    max_attempts = 3 if force else 1
    # Non-forced edits are disposable progress updates — use short timeout
    # to avoid blocking the stream loop when TG is slow
    timeout = (3.0, 7.0) if force else (2.0, 3.0)

    for attempt in range(max_attempts):
        try:
            resp = requests.post(f"{API_URL}/editMessageText", json=payload, timeout=timeout)
            result = resp.json()
            if not result.get("ok"):
                error_desc = result.get("description", "")
                if "message is not modified" in error_desc:
                    return  # Expected when content hasn't changed
                elif parse_mode:
                    # Retry without markdown if parsing fails
                    payload.pop("parse_mode", None)
                    resp2 = requests.post(f"{API_URL}/editMessageText", json=payload, timeout=(3.0, 7.0))
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

    # All retries exhausted — don't send a new message (would create duplicate on TG)
    if force:
        print(f"edit_message: all retries failed for msg_id={message_id}, giving up", flush=True)


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
    except Exception:
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
    except Exception:
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


def format_tool_status(tool_name, path=""):
    """Format a tool-use status line matching Claude-level detail."""
    name = tool_name.lower()
    if name in ("bash", "run_shell_command", "shell", "command_execution") and path:
        preview = path[:60] + "..." if len(path) > 60 else path
        return f"\n\n🔧 _Running:_ `{preview}`"
    elif name in ("write", "write_file", "create_file") and path:
        return f"\n\n🔧 _Writing:_ `{shorten_path(path)}`"
    elif name in ("edit", "replace", "edit_file") and path:
        return f"\n\n🔧 _Editing:_ `{shorten_path(path)}`"
    elif name in ("read", "read_file") and path:
        return f"\n\n🔧 _Reading:_ `{shorten_path(path)}`"
    elif name in ("glob", "grep", "grep_search", "find_files") and path:
        preview = path[:50] + "..." if len(path) > 50 else path
        return f"\n\n🔧 _Searching:_ `{preview}`"
    elif path:
        return f"\n\n🔧 _{tool_name}:_ `{shorten_path(path)}`"
    else:
        return f"\n\n🔧 _{tool_name}_"


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
    cmd = ["claude", "-p", "--verbose", "--output-format", "stream-json", "--model", "opus"]

    # Add pre-approved tools to avoid permission prompts
    if CLAUDE_ALLOWED_TOOLS:
        cmd.extend(["--allowedTools", CLAUDE_ALLOWED_TOOLS])

    # Inject bridge to provide awareness of other CLI actions since this tool was last used
    if session:
        bridge = get_context_bridge(session, "Claude")
        if bridge:
            prompt = bridge + "[NEW REQUEST]\n" + prompt
            print(f"[Claude] Context bridge injected ({len(bridge)} chars): {bridge[:500]}", flush=True)
        else:
            activity_log = session.get("activity_log", [])
            last_claude_idx = -1
            for i in range(len(activity_log) - 1, -1, -1):
                if activity_log[i]["cli"] == "Claude":
                    last_claude_idx = i
                    break
            print(f"[Claude] No context bridge (no other CLI activity since last Claude use). "
                  f"activity_log has {len(activity_log)} entries, last Claude at idx {last_claude_idx}, "
                  f"last 3 entries: {activity_log[-3:] if activity_log else 'empty'}", flush=True)

    # Resume with Claude's session ID if available
    claude_session_id = session.get("claude_session_id") if session else None
    if claude_session_id:
        cmd.extend(["--resume", claude_session_id])

    # Update session with the latest action
    if session:
        update_session_state(chat_id, session, prompt, "Claude")

    # Use -- to separate options from prompt (prevents arg parsing issues)
    cmd.append("--")
    cmd.append(prompt)

    work_dir = cwd or os.getcwd()
    # Use session_id for process tracking (allows parallel sessions)
    process_key = session_id or str(chat_id)

    # Suppress legacy WS message/edit broadcasts — stream events replace them
    _ws_suppress.active = True

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
    process = None  # Initialize before try block so exception handler can safely reference it

    # WS-native streaming: the app uses stream events instead of edit events
    # Use the session passed in (captured at dispatch time), not get_active_session()
    # which could return a different session if the user switched mid-stream
    _stream_session = session.get("name", "") if session else ""
    _ws_stream(chat_id, "start", message_id, session=_stream_session)

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=work_dir,
            start_new_session=True  # Own process group so we can kill the whole tree on cancel
        )

        # Track active process for cancellation (by session_id for parallel support)
        active_processes[process_key] = process
        _ws_broadcast(chat_id, "status", {"mode": "busy", "active": True})

        # Drain stderr in background so errors are logged instead of silently lost
        claude_stderr_lines = []
        def _drain_claude_stderr():
            try:
                for raw_line in process.stderr:
                    line = raw_line.decode("utf-8", errors="replace").strip() if isinstance(raw_line, bytes) else raw_line.strip()
                    if line:
                        claude_stderr_lines.append(line[:500])
                        print(f"[Claude stderr] {line[:300]}", flush=True)
            except Exception:
                pass
        stderr_thread = threading.Thread(target=_drain_claude_stderr, daemon=True)
        stderr_thread.start()

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
            
            # Deduplicate by tool_id (critical for ExitPlanMode/AskUserQuestion)
            if tool_id:
                if tool_id in processed_tool_ids:
                    return
                processed_tool_ids.add(tool_id)

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
            elif tool_name == "Edit":
                path = tool_input.get("file_path", "")
                file_changes.append({
                    "type": "edit", "path": path[:100],
                    "old": tool_input.get("old_string", "")[:3000],
                    "new": tool_input.get("new_string", "")[:3000],
                })
                current_tool = tool_name
            elif tool_name == "Write":
                path = tool_input.get("file_path", "")
                file_changes.append({
                    "type": "write", "path": path[:100],
                    "content": tool_input.get("content", "")[:3000],
                })
                current_tool = tool_name
            elif tool_name in ["Bash", "Read", "Glob", "Grep"]:
                path = tool_input.get("file_path") or tool_input.get("command") or tool_input.get("pattern") or ""
                file_changes.append({"type": tool_name.lower(), "path": path[:100]})
                current_tool = tool_name
                # WS stream: send tool event to app
                _ws_stream(chat_id, "tool", message_ids[0], tool=tool_name.lower(), path=path[:100])
                now = time.time()
                if now - last_update >= update_interval:
                    display_text = current_chunk_text or ""
                    status = format_tool_status(tool_name, path)
                    edit_message(chat_id, message_id, display_text + status)
                    last_update = now

        def _process_text(text):
            """Handle a text block's content (shared between normal and large-line paths)."""
            nonlocal accumulated_text, current_chunk_text, current_tool, message_id, last_update
            if not text:
                return
            # Strip leading newlines from the very first text to avoid blank lines at top
            if not accumulated_text:
                text = text.lstrip('\n')
            print(f"[STREAM] _process_text: {len(text)} chars, total_accumulated={len(accumulated_text)}, chunk={len(current_chunk_text)}", flush=True)
            spacing = ""
            if accumulated_text and not accumulated_text.endswith('\n') and not text.startswith('\n'):
                if accumulated_text.endswith(('.', '!', '?', ':')):
                    spacing = "\n\n"
                elif not accumulated_text.endswith(' '):
                    spacing = " "
            # WS stream: send the delta to app (no rate limit, no size limit)
            _ws_stream(chat_id, "append", message_ids[0], text=spacing + text)
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

        # Check if explicitly cancelled via /cancel (explicit flag, no race condition)
        cancelled = process_key in cancelled_sessions
        if cancelled:
            cancelled_sessions.discard(process_key)

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

        # Wait for stderr drain
        try:
            stderr_thread.join(timeout=5)
        except Exception:
            pass

        # Add completion indicator
        if cancelled:
            final_chunk += "\n\n———\n⚠️ _cancelled_"
        elif not accumulated_text.strip() and claude_stderr_lines:
            final_chunk += f"\n\n———\n❌ _No output:_ {claude_stderr_lines[-1][:200]}"
        else:
            final_chunk += "\n\n———\n✓ _complete_"

        # WS stream: send done event with full text (app doesn't need TG chunking/splitting)
        _ws_stream(chat_id, "done", message_ids[0],
                   session=_stream_session,
                   text=accumulated_text.strip(),
                   cancelled=cancelled,
                   file_changes=file_changes)

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
                except Exception:
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

        # Clean up process tracking only after final output is flushed.
        active_processes.pop(process_key, None)
        _ws_broadcast(chat_id, "status", {"mode": "busy", "active": False})
        mark_session_done(process_key)

        _ws_suppress.active = False
        return accumulated_text, questions, message_id, new_claude_session_id, context_overflow

    except FileNotFoundError:
        _ws_suppress.active = False
        active_processes.pop(process_key, None)
        _ws_broadcast(chat_id, "status", {"mode": "busy", "active": False})
        mark_session_done(process_key)
        _ws_stream(chat_id, "done", message_id, session=_stream_session,
                   text="Error: Claude CLI not found", cancelled=False, file_changes=[])
        edit_message(chat_id, message_id, "❌ _Error: Claude CLI not found_", force=True)
        return "Error: Claude CLI not found", [], message_id, None, False
    except Exception as e:
        _ws_suppress.active = False
        active_processes.pop(process_key, None)
        _ws_broadcast(chat_id, "status", {"mode": "busy", "active": False})
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
        _ws_stream(chat_id, "done", message_ids[0] if message_ids else message_id,
                   session=_stream_session,
                   text=accumulated_text.strip() + f"\n\nError: {e}",
                   cancelled=False, file_changes=file_changes)
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
        "message_counts": {"claude": 0, "codex": 0, "gemini": 0},  # Per-CLI compaction counters
    }

    user_sessions[chat_key]["sessions"].append(session)
    user_sessions[chat_key]["active"] = session_id  # Use session_id as identifier
    save_sessions(force=True)

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
        save_sessions(force=True)


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



def get_context_bridge(session, current_cli):
    """Generate a context bridge message when switching between tools or starting fresh."""
    hints = []
    
    activity_log = session.get("activity_log", [])
    
    if activity_log:
        # Find the last time *this* current_cli was used
        last_used_index = -1
        for i in range(len(activity_log) - 1, -1, -1):
            if activity_log[i]["cli"] == current_cli:
                last_used_index = i
                break
        
        # If it was used before, find all activities SINCE then
        if last_used_index != -1:
            recent_activities = activity_log[last_used_index + 1:]
        else:
            # If never used, show recent activities
            recent_activities = activity_log[-10:]
            
        if recent_activities:
            # Group contiguous activities by the same CLI to form timeframes
            grouped = []
            for act in recent_activities:
                if not grouped or grouped[-1]["cli"] != act["cli"]:
                    grouped.append({
                        "cli": act["cli"],
                        "start": act["time"],
                        "end": act["time"]
                    })
                else:
                    grouped[-1]["end"] = act["time"]

            activity_strings = []

            # Resolve exact session log file paths
            abs_cwd = os.path.abspath(session["cwd"])
            project_name = os.path.basename(abs_cwd)
            home = os.path.expanduser("~")

            # Claude: {project_dir}/{claude_session_id}.jsonl
            claude_proj_id = abs_cwd.replace(os.sep, "-")
            claude_sid = session.get("claude_session_id")
            if claude_sid:
                claude_path = f"{home}/.claude/projects/{claude_proj_id}/{claude_sid}.jsonl"
            else:
                claude_path = f"~/.claude/projects/{claude_proj_id}/"

            # Gemini: ~/.gemini/tmp/<project>/chats/ (session files named by date)
            gemini_path = f"~/.gemini/tmp/{project_name}/chats/"

            # Codex: use cached path, or fall back to generic dir
            codex_path = session.get("codex_session_path", "~/.codex/sessions/")

            cli_paths = {
                "Claude": claude_path,
                "Codex": codex_path,
                "Gemini": gemini_path
            }

            for g in grouped:
                path_hint = f" (Session log: {cli_paths.get(g['cli'], 'standard locations')})"
                try:
                    start_dt = datetime.fromisoformat(g["start"])
                    start_str = start_dt.strftime("%I:%M %p")
                    if g["start"] != g["end"]:
                        end_dt = datetime.fromisoformat(g["end"])
                        end_str = end_dt.strftime("%I:%M %p")
                        activity_strings.append(f"- {g['cli']}{path_hint} from {start_str} to {end_str}")
                    else:
                        activity_strings.append(f"- {g['cli']}{path_hint} around {start_str}")
                except Exception:
                    activity_strings.append(f"- {g['cli']}{path_hint} at {g['start']}")

            if activity_strings:
                hint = (
                    f"Since you ({current_cli}) were last active on this project, the user has utilized other AI assistants.\n"
                    f"Read the session log files below to understand what they did. For JSONL logs, read the tail (last ~200 lines) for recent activity:\n"
                    + "\n".join(activity_strings)
                )
                hints.append(hint)
    else:
        # Fallback if no activity log
        last_cli = session.get("last_cli")
        last_prompt = session.get("last_prompt")
        if last_cli and last_cli != current_cli and last_prompt:
            hints.append(f"Previously, {last_cli} was working on this task: \"{last_prompt}\". Please check its session logs.")

    # Only include summary on CLI handover (when hints already has activity data)
    if hints:
        last_summary = session.get("last_summary")
        if last_summary:
            hints.append(f"CONSOLIDATED PROJECT STATE:\n{last_summary}")
        return f"[SHARED CONTEXT FROM PREVIOUS ACTIVITIES]\n" + "\n\n".join(hints) + "\n\n"
    return ""


def update_session_state(chat_id, session, prompt, cli_name):
    """Update the state for a session, tracking the last CLI used and the prompt."""
    chat_key = str(chat_id)
    if chat_key not in user_sessions:
        return

    session_id = get_session_id(session)
    for s in user_sessions[chat_key]["sessions"]:
        if get_session_id(s) == session_id:
            s["last_prompt"] = prompt[:200] if prompt else None
            s["last_cli"] = cli_name
            now_iso = datetime.now().isoformat()
            s["last_active"] = now_iso

            if "activity_log" not in s:
                s["activity_log"] = []

            s["activity_log"].append({
                "cli": cli_name,
                "time": now_iso
            })
            
            # Keep log bounded
            if len(s["activity_log"]) > 50:
                s["activity_log"] = s["activity_log"][-50:]
                
            save_sessions(force=True)
            break


def update_cli_session_id(chat_id, session, cli_name, new_sid):
    """Update a specific CLI's session ID for resuming conversations."""
    chat_key = str(chat_id)
    if chat_key not in user_sessions:
        return

    session_id = get_session_id(session)
    key_map = {
        "Claude": "claude_session_id",
        "Codex": "codex_session_id",
        "Gemini": "gemini_session_id"
    }
    sid_key = key_map.get(cli_name)
    if not sid_key:
        return

    for s in user_sessions[chat_key]["sessions"]:
        if get_session_id(s) == session_id:
            s[sid_key] = new_sid
            save_sessions(force=True)
            break


def update_claude_session_id(chat_id, session, claude_session_id):
    """Legacy wrapper for Claude session ID updates."""
    update_cli_session_id(chat_id, session, "Claude", claude_session_id)


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


def increment_message_count(chat_id, session, cli_name):
    """Increment per-CLI message count and return True if compaction is needed."""
    if not session:
        return False

    chat_key = str(chat_id)
    session_id = get_session_id(session)
    key = cli_name.lower()

    for s in user_sessions.get(chat_key, {}).get("sessions", []):
        if get_session_id(s) == session_id:
            # Migrate old single counter to per-CLI dict
            counts = s.get("message_counts")
            if not isinstance(counts, dict):
                s["message_counts"] = {"claude": 0, "codex": 0, "gemini": 0}
                counts = s["message_counts"]
            counts[key] = counts.get(key, 0) + 1
            save_sessions()
            return counts[key] >= COMPACTION_THRESHOLD
    return False


def reset_message_count(chat_id, session, cli_name):
    """Reset per-CLI message count after compaction."""
    if not session:
        return

    chat_key = str(chat_id)
    session_id = get_session_id(session)
    key = cli_name.lower()

    for s in user_sessions.get(chat_key, {}).get("sessions", []):
        if get_session_id(s) == session_id:
            counts = s.get("message_counts")
            if isinstance(counts, dict):
                counts[key] = 0
            save_sessions()
            break


def is_allowed(chat_id):
    """Check if the chat ID is allowed."""
    if not ALLOWED_CHAT_IDS or ALLOWED_CHAT_IDS == [""]:
        print("Warning: No ALLOWED_CHAT_IDS set. Allowing all users.")
        return True
    return str(chat_id) in ALLOWED_CHAT_IDS


def run_codex(prompt, cwd=None, session=None, stale_timeout=300):
    """Run Codex synchronously and return the output text.

    Uses a stale-output watchdog instead of a hard wall-clock timeout:
    the process is only killed if no stdout is produced for stale_timeout seconds.
    """
    codex_sid = session.get("codex_session_id") if session else None

    if codex_sid:
        cmd = [
            "codex", "exec",
            "-m", CODEX_MODEL,
            "-c", 'model_reasoning_effort="xhigh"',
            "--dangerously-bypass-approvals-and-sandbox", "--json",
            "resume", codex_sid,
            prompt
        ]
    else:
        cmd = [
            "codex", "exec",
            "-m", CODEX_MODEL,
            "-c", 'model_reasoning_effort="xhigh"',
            "--dangerously-bypass-approvals-and-sandbox", "--json",
            prompt
        ]

    try:
        process = subprocess.Popen(
            cmd, cwd=cwd or os.getcwd(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True
        )

        # Drain stderr in background to prevent pipe deadlock
        stderr_lines = []
        def _drain_stderr():
            try:
                for line in process.stderr:
                    line = line.strip()
                    if line:
                        stderr_lines.append(line[:500])
            except Exception:
                pass
        threading.Thread(target=_drain_stderr, daemon=True).start()

        # Read stdout line by line with stale-output watchdog
        last_output_time = time.time()
        timed_out = False
        watchdog_stop = threading.Event()

        def _watchdog():
            nonlocal timed_out
            while not watchdog_stop.is_set():
                watchdog_stop.wait(30)
                if watchdog_stop.is_set():
                    break
                elapsed = time.time() - last_output_time
                if elapsed > stale_timeout:
                    print(f"run_codex: no output for {elapsed:.0f}s, killing stale process", flush=True)
                    timed_out = True
                    try:
                        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    except Exception:
                        process.kill()
                    break

        watchdog_thread = threading.Thread(target=_watchdog, daemon=True)
        watchdog_thread.start()

        stdout_lines = []
        try:
            for line in process.stdout:
                last_output_time = time.time()
                stdout_lines.append(line)
        except Exception:
            pass

        watchdog_stop.set()
        process.wait(timeout=10)

        # Parse JSONL output to extract agent messages and session ID
        accumulated = []
        thread_id = None
        for line in stdout_lines:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                if event.get("type") == "thread.started" and event.get("thread_id"):
                    thread_id = event["thread_id"]
                if event.get("type") == "item.completed":
                    item = event.get("item", {})
                    if item.get("type") == "agent_message":
                        accumulated.append(item.get("text", ""))
            except json.JSONDecodeError:
                pass

        # Persist Codex session ID and resolved log path for future resume/context bridge
        if thread_id and session:
            session["codex_session_id"] = thread_id
            try:
                import glob as glob_mod
                home = os.path.expanduser("~")
                matches = glob_mod.glob(f"{home}/.codex/sessions/**/*{thread_id}*.jsonl", recursive=True)
                if matches:
                    session["codex_session_path"] = matches[0]
            except Exception:
                pass
            print(f"[Codex] Session ID saved: {thread_id}", flush=True)

        result = "\n".join(accumulated).strip()
        if timed_out and not result and stderr_lines:
            print(f"run_codex: stale timeout, stderr: {stderr_lines[-1][:300]}", flush=True)
        return result
    except Exception as e:
        print(f"run_codex error: {e}")
        return ""


def run_gemini(prompt, cwd=None, session=None):
    """Run Gemini synchronously and return the output text."""
    gemini_sid = session.get("gemini_session_id") if session else None

    cmd = ["gemini", "--prompt", prompt, "--output-format", "stream-json", "--yolo"]
    if gemini_sid:
        cmd.extend(["--resume", gemini_sid])
    if GEMINI_MODEL:
        cmd.extend(["-m", GEMINI_MODEL])

    try:
        process = subprocess.Popen(
            cmd, cwd=cwd or os.getcwd(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        stdout, _ = process.communicate(timeout=180)

        accumulated = []
        for line in stdout.strip().split("\n"):
            if not line: continue
            try:
                event = json.loads(line)
                if event.get("type") == "message" and event.get("role") == "assistant":
                    accumulated.append(event.get("content", ""))
            except json.JSONDecodeError:
                pass

        return "".join(accumulated).strip()
    except Exception as e:
        print(f"run_gemini error: {e}")
        return ""


def run_gemini_streaming(prompt, chat_id, cwd=None, session=None, session_id=None):
    """Run Gemini CLI with streaming output to Telegram. For use in omni loop.

    Returns (accumulated_text, new_gemini_session_id, error_bool, did_tool_work).
    Registers in active_processes for /cancel support.
    """
    import io

    process_key = session_id or (get_session_id(session) if session else str(chat_id))
    gemini_sid = session.get("gemini_session_id") if session else None

    # Inject context bridge
    if session:
        bridge = get_context_bridge(session, "Gemini")
        if bridge:
            prompt = bridge + "[NEW REQUEST]\n" + prompt
            print(f"[Gemini-stream] Context bridge injected ({len(bridge)} chars)", flush=True)

    if session:
        update_session_state(chat_id, session, prompt, "Gemini")

    cmd = ["gemini", "--prompt", prompt, "--output-format", "stream-json", "--yolo"]
    if gemini_sid:
        cmd.extend(["--resume", gemini_sid])
    if GEMINI_MODEL:
        cmd.extend(["-m", GEMINI_MODEL])

    accumulated_text = ""
    current_chunk_text = ""
    new_session_id = None
    message_id = None
    message_ids = []
    file_changes = []
    processed_tool_ids = set()
    max_chunk_len = 3500
    update_interval = 1.0
    startup_timeout = 90   # Kill if zero stdout within 90s (Gemini should emit init immediately)
    stale_timeout = 300    # Kill if no new output for 5 min after first output
    got_any_output = False
    process = None
    cancelled = False

    try:
        process = subprocess.Popen(
            cmd, cwd=cwd or os.getcwd(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True
        )

        # Register for /cancel support
        active_processes[process_key] = process
        _ws_broadcast(chat_id, "status", {"mode": "busy", "active": True})

        # Drain stderr in background
        stderr_lines = []
        def _drain_stderr():
            try:
                for raw_line in process.stderr:
                    line = raw_line.decode("utf-8", errors="replace").strip() if isinstance(raw_line, bytes) else raw_line.strip()
                    if line:
                        stderr_lines.append(line[:500])
                        print(f"[Gemini-stream stderr] {line[:300]}", flush=True)
            except Exception:
                pass
        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        # Watchdog: shorter timeout if no output received yet, longer after first output
        last_output_time = time.time()
        watchdog_stop = threading.Event()
        def _watchdog():
            while not watchdog_stop.is_set():
                watchdog_stop.wait(15)  # Check every 15s
                if watchdog_stop.is_set():
                    break
                elapsed = time.time() - last_output_time
                timeout = stale_timeout if got_any_output else startup_timeout
                if elapsed > timeout:
                    label = "stale" if got_any_output else "startup"
                    print(f"[Gemini-stream] Watchdog ({label}): no output for {elapsed:.0f}s, killing", flush=True)
                    try:
                        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                        time.sleep(5)
                        if process.poll() is None:
                            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                    except Exception:
                        pass
                    break
        watchdog_thread = threading.Thread(target=_watchdog, daemon=True)
        watchdog_thread.start()

        message_id = send_message(chat_id, "⏳ _Gemini working..._")
        message_ids.append(message_id)
        last_update = 0
        current_tool = None
        gemini_errors = []

        stdout_reader = io.TextIOWrapper(process.stdout, encoding='utf-8', errors='replace')

        for line in stdout_reader:
            line = line.strip()
            if not line:
                continue

            last_output_time = time.time()
            got_any_output = True
            line_len = len(line)
            try:
                event = json.loads(line)
                etype = event.get("type", "")

                if etype == "init":
                    new_session_id = event.get("session_id")

                elif etype == "message":
                    role = event.get("role")
                    if role == "assistant":
                        content = event.get("content", "")
                        if not isinstance(content, str):
                            content = str(content) if content is not None else ""
                        is_delta = bool(event.get("delta"))
                        append_text = content
                        if content and not is_delta:
                            if content.startswith(accumulated_text):
                                append_text = content[len(accumulated_text):]
                            elif accumulated_text.startswith(content):
                                append_text = ""
                        if append_text:
                            # Strip leading newlines from very first text
                            if not accumulated_text:
                                append_text = append_text.lstrip('\n')
                            if not append_text:
                                continue
                            spacing = ""
                            if accumulated_text and not accumulated_text.endswith('\n') and not append_text.startswith('\n'):
                                if accumulated_text.endswith(('.', '!', '?', ':')):
                                    spacing = "\n\n"
                                elif not accumulated_text.endswith(' '):
                                    spacing = " "
                            accumulated_text += spacing + append_text
                            current_chunk_text += spacing + append_text
                            current_tool = None

                elif etype == "tool_use":
                    tool_id = event.get("tool_id")
                    if tool_id and tool_id in processed_tool_ids:
                        continue
                    if tool_id:
                        processed_tool_ids.add(tool_id)
                    tool_name = event.get("tool_name") or "tool"
                    params = event.get("parameters", {})
                    path = params.get("file_path") or params.get("command") or params.get("pattern") or params.get("dir_path") or ""
                    change = {"type": tool_name.lower(), "path": path[:100]}
                    if tool_name.lower() in ("edit", "replace"):
                        change["old"] = (params.get("old_string") or "")[:3000]
                        change["new"] = (params.get("new_string") or "")[:3000]
                    elif tool_name.lower() in ("write", "write_file"):
                        change["content"] = (params.get("content") or "")[:3000]
                    file_changes.append(change)
                    current_tool = tool_name
                    now = time.time()
                    if now - last_update >= update_interval:
                        display_text = current_chunk_text if current_chunk_text.strip() else "⏳"
                        status = format_tool_status(tool_name, path)
                        edit_message(chat_id, message_id, display_text + status)
                        last_update = now

                elif etype == "tool_result":
                    current_tool = None

                elif etype == "result":
                    stats = event.get("stats", {})
                    print(f"[Gemini-stream] result: status={event.get('status')}, tokens={stats.get('total_tokens')}, tool_calls={stats.get('tool_calls')}", flush=True)

                elif etype == "error":
                    error_msg = event.get("message") or event.get("error") or str(event)
                    gemini_errors.append(error_msg[:300])
                    print(f"[Gemini-stream] Error event: {error_msg[:300]}", flush=True)

                # Chunk overflow
                while len(current_chunk_text) > max_chunk_len:
                    send_part = current_chunk_text[:max_chunk_len]
                    carry_over = current_chunk_text[max_chunk_len:]
                    edit_message(chat_id, message_id, send_part.strip() + "\n\n———\n_continued..._", force=True)
                    message_id = send_message(chat_id, "⏳ _continuing..._")
                    message_ids.append(message_id)
                    current_chunk_text = carry_over
                    last_update = time.time()

                # Periodic update
                now = time.time()
                if now - last_update >= update_interval:
                    display_text = current_chunk_text if current_chunk_text.strip() else "⏳"
                    suffix = f"\n\n———\n🔧 _{current_tool}_" if current_tool else ("" if not current_chunk_text.strip() else "\n\n———\n⏳ _generating..._")
                    edit_message(chat_id, message_id, display_text + suffix)
                    last_update = now

                if line_len > 50_000:
                    event = None
                    line = None
                    _malloc_trim()

            except json.JSONDecodeError:
                pass

        watchdog_stop.set()
        process.wait()
        # Check if explicitly cancelled via /cancel (explicit flag, no race condition)
        cancelled = process_key in cancelled_sessions
        if cancelled:
            cancelled_sessions.discard(process_key)

        # Save gemini session ID for resume
        if new_session_id and session:
            sid = get_session_id(session)
            chat_key_s = str(chat_id)
            for s in user_sessions.get(chat_key_s, {}).get("sessions", []):
                if get_session_id(s) == sid:
                    s["gemini_session_id"] = new_session_id
                    save_sessions(force=True)
                    break

        # Final message update
        final_chunk = current_chunk_text.strip()
        if not final_chunk and accumulated_text.strip():
            final_chunk = accumulated_text.strip()[-max_chunk_len:]

        elapsed_since_last = time.time() - last_output_time
        timed_out = elapsed_since_last > (stale_timeout - 10 if got_any_output else startup_timeout - 10)

        # If startup timeout with --resume, clear stale Gemini session so next attempt starts fresh
        if timed_out and not got_any_output and gemini_sid and session:
            print(f"[Gemini-stream] Startup timeout with --resume, clearing stale Gemini session ID", flush=True)
            update_cli_session_id(chat_id, session, "Gemini", None)

        error_occurred = False
        if cancelled:
            final_chunk += "\n\n———\n⚠️ _cancelled_"
            error_occurred = True
        elif timed_out:
            if not got_any_output:
                final_chunk += "\n\n———\n⏱️ _timed out (no output at all — Gemini may be stuck)_"
                error_occurred = True
            elif file_changes:
                # Gemini did tool work then went quiet — not a real error, work was done
                final_chunk += "\n\n———\n✓ _complete (stale timeout after tool work)_"
            else:
                final_chunk += "\n\n———\n⏱️ _timed out (no output for 5 min)_"
                error_occurred = True
        elif process.returncode and process.returncode != 0:
            stderr_hint = f": {stderr_lines[-1][:150]}" if stderr_lines else ""
            final_chunk += f"\n\n———\n⚠️ _exited with code {process.returncode}{stderr_hint}_"
            error_occurred = True
        elif gemini_errors:
            final_chunk += f"\n\n———\n⚠️ _complete with errors:_ {gemini_errors[-1][:150]}"
        else:
            final_chunk += "\n\n———\n✓ _complete_"

        # WS stream: send done event with file changes for diff viewer
        _ws_session = getattr(_ws_session_override, 'name', None) or (get_active_session(chat_id) if chat_id else "")
        _ws_stream(chat_id, "done", message_ids[0] if message_ids else message_id,
                   session=_ws_session or "",
                   text=accumulated_text.strip(),
                   cancelled=cancelled,
                   file_changes=file_changes)

        if final_chunk and len(final_chunk) <= 4000:
            edit_message(chat_id, message_id, final_chunk, force=True)
        elif final_chunk:
            edit_message(chat_id, message_id, final_chunk[:3950] + "\n\n_(...truncated)_", force=True)

        return accumulated_text, new_session_id, error_occurred, bool(file_changes)

    except FileNotFoundError:
        if message_id:
            edit_message(chat_id, message_id, "❌ Gemini CLI not found.", force=True)
        else:
            send_message(chat_id, "❌ Gemini CLI not found.")
        return "", None, True, False
    except Exception as e:
        print(f"[Gemini-stream] Exception: {e}", flush=True)
        error_text = accumulated_text + f"\n\n———\n❌ Gemini error: {str(e)[:200]}"
        if message_id:
            edit_message(chat_id, message_id, error_text[:4000], force=True)
        else:
            send_message(chat_id, error_text[:4000])
        return accumulated_text, new_session_id, True, bool(file_changes)
    finally:
        try:
            watchdog_stop.set()
        except UnboundLocalError:
            pass
        active_processes.pop(process_key, None)
        _ws_broadcast(chat_id, "status", {"mode": "busy", "active": False})
        # Ensure subprocess is cleaned up
        if process is not None:
            try:
                if process.stdout:
                    process.stdout.close()
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            except Exception:
                pass


def perform_proactive_compaction(chat_id, session, cli_name):
    """Perform proactive compaction for any CLI by using that tool to summarize the state."""
    if not session:
        return None

    session_id = get_session_id(session)
    send_message(chat_id, f"📦 *Proactive compaction ({cli_name})* - summarizing context...")

    summary_prompt = """Summarize this session for context continuity (max 500 words). Focus on ACTIONABLE STATE:
1. Files being edited — exact paths and what changed
2. Current task — what's in progress, what's done, what's left
3. Key decisions — architectural choices, approaches chosen and WHY
4. Bugs/issues — any errors encountered and their status (fixed/open)
5. Code snippets — any critical code patterns or values needed to continue

Omit: greetings, abandoned approaches, resolved debugging back-and-forth.
Format as a compact bullet list. This summary will be used to restore context after a session reset."""

    try:
        summary = ""
        # Use the tool that has the conversation context to summarize itself
        if cli_name == "Codex":
            summary = run_codex(summary_prompt, cwd=session["cwd"], session=session)
        elif cli_name == "Gemini":
            summary = run_gemini(summary_prompt, cwd=session["cwd"], session=session)
        else:
            # Fallback/Default to Claude
            summary_response, _, _, _, _ = run_claude_streaming(
                summary_prompt, chat_id, cwd=session["cwd"], continue_session=True,
                session_id=session_id, session=session
            )
            summary = summary_response.split("———")[0].strip() if summary_response else ""
    except Exception as e:
        print(f"Compaction error for {cli_name}: {e}")
        summary = ""

    if summary and len(summary) > 50:
        save_session_summary(chat_id, session, summary)
        # Reset the specific CLI session ID
        update_cli_session_id(chat_id, session, cli_name, None)
        reset_message_count(chat_id, session, cli_name)
        return summary
    
    return None


def run_codex_task(chat_id, task, cwd, session=None):
    """Run a Codex task on the project in background thread. Resumes session if available."""
    session_id = get_session_id(session) if session else str(chat_id)

    def codex_thread():
        process = None
        message_id = None
        accumulated_text = ""
        current_chunk_text = ""
        message_ids = []
        file_changes = []
        processed_item_ids = set()
        try:
            if session:
                needs_compaction = increment_message_count(chat_id, session, "Codex")
                if needs_compaction:
                    perform_proactive_compaction(chat_id, session, "Codex")

            codex_sid = session.get("codex_session_id") if session else None
            mode = "Resuming" if codex_sid else "Starting"
            
            # Inject bridge to provide awareness of other CLI actions since this tool was last used
            current_task = task
            if session:
                bridge = get_context_bridge(session, "Codex")
                if bridge:
                    current_task = bridge + "[NEW TASK]\n" + task
            
            # Update session with the latest action
            if session:
                update_session_state(chat_id, session, task, "Codex")

            send_message(chat_id, f"🔍 *{mode} Codex*\nModel: `{CODEX_MODEL}`\nTask: _{task[:100]}_")

            # Build command — resume existing session or start new
            if codex_sid:
                cmd = [
                    "codex", "exec",
                    "-m", CODEX_MODEL,
                    "-c", 'model_reasoning_effort="xhigh"',
                    "--dangerously-bypass-approvals-and-sandbox", "--json",
                    "resume", codex_sid,
                    current_task
                ]
            else:
                cmd = [
                    "codex", "exec",
                    "-m", CODEX_MODEL,
                    "-c", 'model_reasoning_effort="xhigh"',
                    "--dangerously-bypass-approvals-and-sandbox", "--json",
                    current_task
                ]

            process = subprocess.Popen(
                cmd, cwd=cwd,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True
            )

            # Track as active so messages get queued
            active_processes[session_id] = process
            _ws_broadcast(chat_id, "status", {"mode": "busy", "active": True})

            # Drain stderr in background so errors are logged instead of silently lost
            codex_stderr_lines = []
            def _drain_codex_stderr():
                try:
                    for raw_line in process.stderr:
                        line = raw_line.decode("utf-8", errors="replace").strip() if isinstance(raw_line, bytes) else raw_line.strip()
                        if line:
                            codex_stderr_lines.append(line[:500])
                            print(f"[Codex stderr] {line[:300]}", flush=True)
                except Exception:
                    pass
            stderr_thread = threading.Thread(target=_drain_codex_stderr, daemon=True)
            stderr_thread.start()

            # Mark active for crash recovery
            session_name = session.get("name", "default") if session else "default"
            mark_session_active(chat_id, session_name, session_id, task)

            new_thread_id = None
            max_chunk_len = 3500
            update_interval = 1.0
            message_id = send_message(chat_id, "⏳ _Codex working..._")
            message_ids.append(message_id)
            # WS-native streaming: app renders one continuous message
            _codex_stream_session = session.get("name", "") if session else ""
            _ws_stream(chat_id, "start", message_id, session=_codex_stream_session)
            # Suppress legacy WS message/edit broadcasts — stream events replace them
            _ws_suppress.active = True
            # Force the first streaming update to be visible immediately.
            last_update = 0
            current_tool = None

            import io
            stdout_reader = io.TextIOWrapper(process.stdout, encoding='utf-8', errors='replace')
            # Track per-item accumulated text length so item.updated deltas can be extracted
            item_text_lengths = {}  # item_id -> length of text already appended
            seen_file_changes = set()  # (type, path) to avoid duplicate rows

            def _read_file_preview(path, limit=3000):
                """Best-effort preview for newly created files (for app diff viewer)."""
                if not path:
                    return ""
                try:
                    abs_path = path if os.path.isabs(path) else os.path.join(cwd or os.getcwd(), path)
                    if not os.path.isfile(abs_path):
                        return ""
                    with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                        return f.read(limit)
                except Exception:
                    return ""

            def _append_file_change(change_type, path, old="", new="", content=""):
                key = (change_type, path)
                if key in seen_file_changes:
                    return
                seen_file_changes.add(key)
                entry = {"type": change_type, "path": (path or "")[:100]}
                if old:
                    entry["old"] = old[:3000]
                if new:
                    entry["new"] = new[:3000]
                if content:
                    entry["content"] = content[:3000]
                file_changes.append(entry)

            for line in stdout_reader:
                line = line.strip()
                if not line:
                    continue

                line_len = len(line)
                try:
                    event = json.loads(line)
                    etype = event.get("type", "")

                    if etype == "thread.started":
                        new_thread_id = event.get("thread_id")

                    elif etype in ["item.started", "item.updated", "item.completed"]:
                        item = event.get("item", {})
                        itype = item.get("type")
                        item_id = item.get("id")

                        if itype == "agent_message":
                            text = item.get("text", "")
                            if text and item_id:
                                if etype == "item.completed":
                                    # Final text — append only the portion not yet seen
                                    prev_len = item_text_lengths.get(item_id, 0)
                                    new_text = text[prev_len:]
                                    item_text_lengths.pop(item_id, None)
                                    processed_item_ids.add(item_id)
                                    current_tool = None
                                elif etype == "item.updated":
                                    # Streaming delta — text field is cumulative, extract new portion
                                    prev_len = item_text_lengths.get(item_id, 0)
                                    new_text = text[prev_len:]
                                    item_text_lengths[item_id] = len(text)
                                else:
                                    new_text = ""

                                if new_text:
                                    # Strip leading newlines from very first text
                                    if not accumulated_text:
                                        new_text = new_text.lstrip('\n')
                                    if not new_text:
                                        continue
                                    # Add spacing between separate agent messages
                                    spacing = ""
                                    if accumulated_text and not accumulated_text.endswith('\n') and not new_text.startswith('\n'):
                                        # Only add spacing at the start of a NEW item, not mid-stream
                                        if item_id not in item_text_lengths or item_text_lengths.get(item_id, 0) == len(new_text):
                                            if accumulated_text.endswith(('.', '!', '?', ':')):
                                                spacing = "\n\n"
                                            elif not accumulated_text.endswith(' '):
                                                spacing = " "
                                    accumulated_text += spacing + new_text
                                    current_chunk_text += spacing + new_text
                                    _ws_stream(chat_id, "append", message_ids[0], text=spacing + new_text)

                        elif itype == "command_execution":
                            cmd_str = item.get("command", "")
                            if etype == "item.started":
                                if item_id and item_id not in processed_item_ids:
                                    _append_file_change("bash", cmd_str)
                                    if item_id:
                                        processed_item_ids.add(item_id)
                                current_tool = "Bash"
                                _ws_stream(chat_id, "tool", message_ids[0], tool="bash", path=cmd_str[:100])
                                now = time.time()
                                if now - last_update >= update_interval:
                                    display_text = current_chunk_text if current_chunk_text.strip() else "⏳"
                                    status = format_tool_status("bash", cmd_str)
                                    edit_message(chat_id, message_id, display_text + status)
                                    last_update = now
                            elif etype == "item.completed":
                                current_tool = None
                        elif itype == "file_change" and etype == "item.completed":
                            changes = item.get("changes", [])
                            if isinstance(changes, list):
                                for ch in changes:
                                    if not isinstance(ch, dict):
                                        continue
                                    kind = str(ch.get("kind", "")).lower()
                                    path = ch.get("path") or ch.get("new_path") or ch.get("to") or ""

                                    if kind in ("add", "create", "write", "new"):
                                        content = _read_file_preview(path)
                                        _append_file_change("write", path, content=content)
                                    elif kind in ("update", "modify", "edit", "change"):
                                        _append_file_change("edit", path)
                                    elif kind in ("delete", "remove"):
                                        _append_file_change("delete", path)
                                    elif kind in ("rename", "move"):
                                        src = ch.get("old_path") or ch.get("from") or ch.get("src") or path
                                        dst = ch.get("new_path") or ch.get("to") or ch.get("dst") or path
                                        move_path = f"{src} -> {dst}" if src and dst and src != dst else (dst or src)
                                        _append_file_change("move", move_path)
                                    else:
                                        _append_file_change(kind or "file", path)

                    # Stream update: chunk overflow
                    while len(current_chunk_text) > max_chunk_len:
                        send_part = current_chunk_text[:max_chunk_len]
                        carry_over = current_chunk_text[max_chunk_len:]
                        edit_message(chat_id, message_id, send_part.strip() + "\n\n———\n_continued..._", force=True)
                        message_id = send_message(chat_id, "⏳ _continuing..._")
                        message_ids.append(message_id)
                        current_chunk_text = carry_over
                        last_update = time.time()

                    # Stream update: periodic edit
                    now = time.time()
                    if now - last_update >= update_interval and current_chunk_text.strip():
                        suffix = f"\n\n———\n🔧 _{current_tool}_" if current_tool else "\n\n———\n⏳ _generating..._"
                        edit_message(chat_id, message_id, current_chunk_text + suffix)
                        last_update = now

                    # Memory management
                    if line_len > 50_000:
                        event = None
                        line = None
                        _malloc_trim()

                except json.JSONDecodeError:
                    pass

            process.wait()
            # Check if explicitly cancelled via /cancel (explicit flag, no race condition)
            cancelled = session_id in cancelled_sessions
            if cancelled:
                cancelled_sessions.discard(session_id)

            # Save codex session ID and resolved log path for resume
            if new_thread_id and session:
                chat_key = str(chat_id)
                for s in user_sessions.get(chat_key, {}).get("sessions", []):
                    if get_session_id(s) == session_id:
                        s["codex_session_id"] = new_thread_id
                        try:
                            import glob as glob_mod
                            home = os.path.expanduser("~")
                            matches = glob_mod.glob(f"{home}/.codex/sessions/**/*{new_thread_id}*.jsonl", recursive=True)
                            if matches:
                                s["codex_session_path"] = matches[0]
                        except Exception:
                            pass
                        save_sessions(force=True)
                        break

            # Final update
            final_chunk = current_chunk_text.strip()
            if not final_chunk:
                if len(message_ids) == 1 and accumulated_text.strip():
                    final_chunk = accumulated_text.strip()[-max_chunk_len:]
                else:
                    final_chunk = ""

            # Wait for stderr drain
            try:
                stderr_thread.join(timeout=5)
            except Exception:
                pass

            if cancelled:
                final_chunk += "\n\n———\n⚠️ _cancelled_"
            elif not accumulated_text.strip() and codex_stderr_lines:
                final_chunk += f"\n\n———\n❌ _No output:_ {codex_stderr_lines[-1][:200]}"
            else:
                final_chunk += "\n\n———\n✓ _complete_"

            # WS stream: send done event with file changes for diff viewer
            _ws_stream(chat_id, "done", message_ids[0] if message_ids else (message_id or 0),
                       session=_codex_stream_session,
                       text=accumulated_text.strip(),
                       cancelled=cancelled,
                       file_changes=file_changes)

            if len(final_chunk) <= 4000:
                if message_id:
                    edit_message(chat_id, message_id, final_chunk, force=True)
                else:
                    send_message(chat_id, final_chunk)
            else:
                # Split if too long
                max_len = 3900
                chunks = [final_chunk[i:i + max_len] for i in range(0, len(final_chunk), max_len)]
                for chunk in chunks:
                    send_message(chat_id, chunk)
                    time.sleep(0.2)

        except FileNotFoundError:
            _ws_suppress.active = False
            if message_id:
                edit_message(chat_id, message_id, "❌ Codex CLI not found.", force=True)
            else:
                send_message(chat_id, "❌ Codex CLI not found.")
        except Exception as e:
            _ws_suppress.active = False
            error_text = accumulated_text + f"\n\n———\n❌ Codex error: {str(e)[:200]}"
            if message_id:
                edit_message(chat_id, message_id, error_text[:4000], force=True)
            else:
                send_message(chat_id, error_text[:4000])
        finally:
            _ws_suppress.active = False
            mark_session_done(session_id)
            active_processes.pop(session_id, None)
            _ws_broadcast(chat_id, "status", {"mode": "busy", "active": False})
            process_message_queue(chat_id, session)

    # Mark active under lock to prevent race with incoming messages
    lock = get_session_lock(session_id)
    with lock:
        active_processes[session_id] = None
        _ws_broadcast(chat_id, "status", {"mode": "busy", "active": True})
    thread = threading.Thread(target=codex_thread, daemon=True)
    thread.start()
    return thread


def run_gemini_task(chat_id, task, cwd, session=None):
    """Run a Gemini task on the project in background thread. Resumes session if available.

    Returns (thread, result_dict) where result_dict is populated after thread completes:
        - "output": accumulated assistant text
        - "stderr": list of stderr lines
        - "exit_code": process return code
        - "error": exception message if any
    """
    session_id = get_session_id(session) if session else str(chat_id)
    result = {"output": "", "stderr": [], "exit_code": None, "error": None}

    def gemini_thread():
        process = None
        message_id = None
        accumulated_text = ""
        current_chunk_text = ""
        message_ids = []
        file_changes = []
        processed_tool_ids = set()
        try:
            if session:
                needs_compaction = increment_message_count(chat_id, session, "Gemini")
                if needs_compaction:
                    perform_proactive_compaction(chat_id, session, "Gemini")

            gemini_sid = session.get("gemini_session_id") if session else None
            mode = "Resuming" if gemini_sid else "Starting"
            
            # Inject bridge to provide awareness of other CLI actions since this tool was last used
            current_task = task
            if session:
                bridge = get_context_bridge(session, "Gemini")
                if bridge:
                    current_task = bridge + "[NEW TASK]\n" + task
            
            # Update session with the latest action
            if session:
                update_session_state(chat_id, session, task, "Gemini")

            send_message(chat_id, f"♊️ *{mode} Gemini*\nModel: `{GEMINI_MODEL}`\nTask: _{task[:100]}_")

            # Build command — resume existing session or start new
            cmd = ["gemini", "--prompt", current_task, "--output-format", "stream-json", "--yolo"]
            if gemini_sid:
                cmd.extend(["--resume", gemini_sid])

            if GEMINI_MODEL:
                cmd.extend(["-m", GEMINI_MODEL])

            process = subprocess.Popen(
                cmd, cwd=cwd,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True
            )

            # Track as active so messages get queued
            active_processes[session_id] = process
            _ws_broadcast(chat_id, "status", {"mode": "busy", "active": True})

            # Drain stderr in background so errors are logged instead of silently lost
            gemini_stderr_lines = []
            def _drain_gemini_stderr():
                try:
                    for raw_line in process.stderr:
                        line = raw_line.decode("utf-8", errors="replace").strip() if isinstance(raw_line, bytes) else raw_line.strip()
                        if line:
                            gemini_stderr_lines.append(line[:500])
                            print(f"[Gemini stderr] {line[:300]}", flush=True)
                except Exception:
                    pass
            stderr_thread = threading.Thread(target=_drain_gemini_stderr, daemon=True)
            stderr_thread.start()

            # Mark active for crash recovery
            session_name = session.get("name", "default") if session else "default"
            mark_session_active(chat_id, session_name, session_id, task)

            new_session_id = None
            max_chunk_len = 3500
            update_interval = 1.0
            gemini_stale_timeout = 300  # Kill if no output for 5 minutes
            gemini_errors = []  # Collect error events from Gemini CLI
            message_id = send_message(chat_id, "⏳ _Gemini working..._")
            message_ids.append(message_id)
            last_output_time = time.time()
            # Force the first streaming update to be visible immediately.
            last_update = 0
            current_tool = None

            # Watchdog thread: kills Gemini if no stdout activity for gemini_stale_timeout seconds
            watchdog_stop = threading.Event()
            def _gemini_watchdog():
                while not watchdog_stop.is_set():
                    watchdog_stop.wait(30)  # Check every 30s
                    if watchdog_stop.is_set():
                        break
                    elapsed = time.time() - last_output_time
                    if elapsed > gemini_stale_timeout:
                        print(f"[Gemini] Watchdog: no output for {elapsed:.0f}s, killing process", flush=True)
                        try:
                            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                            time.sleep(5)
                            if process.poll() is None:
                                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                        except Exception:
                            pass
                        break
            import signal
            watchdog_thread = threading.Thread(target=_gemini_watchdog, daemon=True)
            watchdog_thread.start()

            import io
            stdout_reader = io.TextIOWrapper(process.stdout, encoding='utf-8', errors='replace')

            for line in stdout_reader:
                line = line.strip()
                if not line:
                    continue

                last_output_time = time.time()
                line_len = len(line)
                try:
                    event = json.loads(line)
                    etype = event.get("type", "")

                    if etype == "init":
                        new_session_id = event.get("session_id")

                    elif etype == "message":
                        role = event.get("role")
                        if role == "assistant":
                            content = event.get("content", "")
                            if not isinstance(content, str):
                                content = str(content) if content is not None else ""
                            is_delta = bool(event.get("delta"))
                            append_text = content
                            if content and not is_delta:
                                if content.startswith(accumulated_text):
                                    append_text = content[len(accumulated_text):]
                                elif accumulated_text.startswith(content):
                                    append_text = ""
                            if append_text:
                                # Strip leading newlines from very first text
                                if not accumulated_text:
                                    append_text = append_text.lstrip('\n')
                                if not append_text:
                                    continue
                                print(f"[Gemini] text: +{len(append_text)} chars (total: {len(accumulated_text)}): {append_text[:80]}", flush=True)
                                spacing = ""
                                if accumulated_text and not accumulated_text.endswith('\n') and not append_text.startswith('\n'):
                                    if accumulated_text.endswith(('.', '!', '?', ':')):
                                        spacing = "\n\n"
                                    elif not accumulated_text.endswith(' '):
                                        spacing = " "
                                accumulated_text += spacing + append_text
                                current_chunk_text += spacing + append_text
                                current_tool = None

                    elif etype == "tool_use":
                        tool_id = event.get("tool_id")
                        if tool_id and tool_id in processed_tool_ids:
                            continue
                        if tool_id:
                            processed_tool_ids.add(tool_id)

                        tool_name = event.get("tool_name") or "tool"
                        params = event.get("parameters", {})
                        path = params.get("file_path") or params.get("command") or params.get("pattern") or params.get("dir_path") or ""
                        file_changes.append({"type": tool_name.lower(), "path": path[:100]})
                        current_tool = tool_name
                        print(f"[Gemini] tool_use: {tool_name}", flush=True)
                        # Mirror Claude-style visibility: show tool activity even before text arrives.
                        now = time.time()
                        if now - last_update >= update_interval:
                            display_text = current_chunk_text if current_chunk_text.strip() else "⏳"
                            status = format_tool_status(tool_name, path)
                            edit_message(chat_id, message_id, display_text + status)
                            last_update = now

                    elif etype == "tool_result":
                        print(f"[Gemini] tool_result", flush=True)
                        current_tool = None

                    elif etype == "result":
                        stats = event.get("stats", {})
                        print(f"[Gemini] result: status={event.get('status')}, tokens={stats.get('total_tokens')}, tool_calls={stats.get('tool_calls')}, accumulated_text={len(accumulated_text)}", flush=True)

                    elif etype == "error":
                        error_msg = event.get("message") or event.get("error") or str(event)
                        gemini_errors.append(error_msg[:300])
                        print(f"[Gemini] Error event: {error_msg[:300]}", flush=True)

                    else:
                        print(f"[Gemini] Unknown event type: {etype} (keys: {list(event.keys())[:8]})", flush=True)

                    # Stream update: chunk overflow
                    while len(current_chunk_text) > max_chunk_len:
                        send_part = current_chunk_text[:max_chunk_len]
                        carry_over = current_chunk_text[max_chunk_len:]
                        edit_message(chat_id, message_id, send_part.strip() + "\n\n———\n_continued..._", force=True)
                        message_id = send_message(chat_id, "⏳ _continuing..._")
                        message_ids.append(message_id)
                        current_chunk_text = carry_over
                        last_update = time.time()

                    # Stream update: periodic edit
                    now = time.time()
                    if now - last_update >= update_interval:
                        display_text = current_chunk_text if current_chunk_text.strip() else "⏳"
                        suffix = f"\n\n———\n🔧 _{current_tool}_" if current_tool else ("" if not current_chunk_text.strip() else "\n\n———\n⏳ _generating..._")
                        print(f"[Gemini] Streaming edit: {len(current_chunk_text)} chars, msg_id={message_id}", flush=True)
                        edit_message(chat_id, message_id, display_text + suffix)
                        last_update = now

                    # Memory management
                    if line_len > 50_000:
                        event = None
                        line = None
                        _malloc_trim()

                except json.JSONDecodeError:
                    pass

            watchdog_stop.set()
            process.wait()
            # Check if explicitly cancelled via /cancel (explicit flag, no race condition)
            cancelled = session_id in cancelled_sessions
            if cancelled:
                cancelled_sessions.discard(session_id)

            # Populate result for callers that join the thread
            result["output"] = accumulated_text
            result["stderr"] = gemini_stderr_lines
            result["exit_code"] = process.returncode

            # Save gemini session ID for resume
            if new_session_id and session:
                chat_key = str(chat_id)
                for s in user_sessions.get(chat_key, {}).get("sessions", []):
                    if get_session_id(s) == session_id:
                        s["gemini_session_id"] = new_session_id
                        save_sessions(force=True)
                        break

            # Final update
            final_chunk = current_chunk_text.strip()
            if not final_chunk:
                if len(message_ids) == 1 and accumulated_text.strip():
                    final_chunk = accumulated_text.strip()[-max_chunk_len:]
                else:
                    final_chunk = ""

            if file_changes:
                final_chunk += "\n\n📁 *File Operations:*"
                for change in file_changes:
                    ctype = change["type"]
                    path = change["path"]
                    if ctype in ["write", "write_file"]:
                        final_chunk += f"\n  ✅ Created: `{shorten_path(path)}`"
                    elif ctype in ["edit", "replace"]:
                        final_chunk += f"\n  ✅ Edited: `{shorten_path(path)}`"
                    elif ctype in ["bash", "run_shell_command"]:
                        final_chunk += f"\n  ✅ Ran: `{path[:80]}{'...' if len(path) > 80 else ''}`"
                    elif ctype in ["read", "read_file"]:
                        final_chunk += f"\n  📖 Read: `{shorten_path(path)}`"
                    elif ctype in ["glob", "grep", "grep_search"]:
                        final_chunk += f"\n  🔍 Search: `{path[:60]}{'...' if len(path) > 60 else ''}`"
                    else:
                        final_chunk += f"\n  🔧 {ctype}: `{shorten_path(path)}`"

            # Determine exit status
            timed_out = (time.time() - last_output_time) > gemini_stale_timeout - 10
            exit_code = process.returncode

            # Wait for stderr drain to finish
            try:
                stderr_thread.join(timeout=5)
            except Exception:
                pass

            if cancelled:
                final_chunk += "\n\n———\n⚠️ _cancelled_"
            elif timed_out:
                final_chunk += "\n\n———\n⏱️ _timed out (no output for 5 min)_"
            elif exit_code and exit_code != 0:
                stderr_hint = f": {gemini_stderr_lines[-1][:150]}" if gemini_stderr_lines else ""
                final_chunk += f"\n\n———\n⚠️ _exited with code {exit_code}{stderr_hint}_"
            elif gemini_errors:
                final_chunk += f"\n\n———\n⚠️ _complete with errors:_ {gemini_errors[-1][:150]}"
            else:
                final_chunk += "\n\n———\n✓ _complete_"

            if len(final_chunk) <= 4000:
                if message_id:
                    edit_message(chat_id, message_id, final_chunk, force=True)
                else:
                    send_message(chat_id, final_chunk)
            else:
                # Split if too long
                max_len = 3900
                chunks = [final_chunk[i:i + max_len] for i in range(0, len(final_chunk), max_len)]
                for chunk in chunks:
                    send_message(chat_id, chunk)
                    time.sleep(0.2)

        except FileNotFoundError:
            result["error"] = "Gemini CLI not found"
            if message_id:
                edit_message(chat_id, message_id, "❌ Gemini CLI not found.", force=True)
            else:
                send_message(chat_id, "❌ Gemini CLI not found.")
        except Exception as e:
            result["error"] = str(e)[:300]
            error_text = accumulated_text + f"\n\n———\n❌ Gemini error: {str(e)[:200]}"
            if message_id:
                edit_message(chat_id, message_id, error_text[:4000], force=True)
            else:
                send_message(chat_id, error_text[:4000])
        finally:
            # Stop watchdog if it was started
            try:
                watchdog_stop.set()
            except UnboundLocalError:
                pass
            mark_session_done(session_id)
            active_processes.pop(session_id, None)
            _ws_broadcast(chat_id, "status", {"mode": "busy", "active": False})
            process_message_queue(chat_id, session)

    # Mark active under lock to prevent race with incoming messages
    lock = get_session_lock(session_id)
    with lock:
        active_processes[session_id] = None
        _ws_broadcast(chat_id, "status", {"mode": "busy", "active": True})
    thread = threading.Thread(target=gemini_thread, daemon=True)
    thread.start()
    return thread, result


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
    r'|resource ?exhausted|usage limit|token limit exceeded'
    r"|out of (?:extra )?usage|usage (?:cap|reset))\b"
    r'|(?:^|\s)429(?:\s|$|[,.\-:])'  # 429 only as standalone number
    r'|\berror.*(?:overloaded|over capacity)\b',
    re.IGNORECASE
)

QUOTA_WAIT_SECONDS = 3600  # 1 hour fallback

# Regex to extract reset time from quota error messages.
# Covers Codex ("Try again at 3:45 PM"), Claude ("resets at 3:45 PM"), etc.
_RESET_TIME_RE = re.compile(
    r'(?:[Tt]ry again (?:at|after|later\.? or try again at)|[Rr]esets? at)\s+(.+?)\.?\s*$',
    re.MULTILINE,
)


def _parse_reset_wait(error_msg):
    """Parse an error message for reset time and return seconds to wait.

    Works for both Codex ("Try again at 3:45 PM") and Claude ("resets at 3:45 PM") messages.
    Returns (wait_seconds, reset_time_str) or (QUOTA_WAIT_SECONDS, None) if unparseable.
    """
    m = _RESET_TIME_RE.search(error_msg)
    if not m:
        return QUOTA_WAIT_SECONDS, None

    time_str = m.group(1).strip()
    now = datetime.now()

    # Try time-only format first: "3:45 PM"
    for fmt in ("%I:%M %p", "%b %d, %Y %I:%M %p", "%b %-d, %Y %-I:%M %p",
                "%B %d, %Y %I:%M %p"):
        try:
            parsed = datetime.strptime(time_str, fmt)
            # If only time was parsed (no date component), set to today
            if parsed.year == 1900:
                parsed = parsed.replace(year=now.year, month=now.month, day=now.day)
                # If the time is in the past, it means tomorrow
                if parsed < now:
                    parsed += timedelta(days=1)
            wait = int((parsed - now).total_seconds())
            if wait < 60:
                wait = 60  # Minimum 1 minute
            return wait, time_str
        except ValueError:
            continue

    return QUOTA_WAIT_SECONDS, time_str



def drain_user_feedback(chat_key):
    """Drain and format any queued user feedback for a justdoit/omni session."""
    messages = user_feedback_queue.pop(chat_key, [])
    if not messages:
        return ""
    feedback = "\n".join(f"- {m[:500]}" for m in messages[-10:])  # Last 10, truncated
    formatted = f"\n\n⚠️ USER FEEDBACK (sent during execution — address these):\n{feedback}"
    print(f"[Feedback] Drained {len(messages)} message(s) for {chat_key}: {feedback[:200]}", flush=True)
    return formatted


def run_codex_review(original_task, claude_output, step, history_summary, cwd, phase="implementing", pending_transition=None, stale_warning=None, claude_plan=None, user_feedback=""):
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
Your goal is to drive the implementation to completion across ALL plan items, not just the current one.

HOW TO CHECK IF IMPLEMENTATION IS COMPLETE:
Look at the plan checkboxes. If ALL items show - [x] (checked), or if Claude's output
confirms all items are implemented, then implementation IS complete — move to verification.
If ANY items still show - [ ] (unchecked), implementation is NOT complete.

- First, check if the work Claude just did is complete and correct. If not, tell Claude to finish or fix it.
- CRITICAL: Examine Claude's output for design and architecture problems BEFORE moving on.
  Look for: poor abstractions, god functions/classes, tight coupling between modules, patterns
  that won't scale, inconsistency with the existing codebase, hardcoded values that should be
  configurable, race conditions, or structural decisions you disagree with. If you spot any of
  these, INTERVENE IMMEDIATELY — include specific architectural feedback in your next prompt
  telling Claude what to restructure and why. It's much cheaper to fix design issues during
  implementation than to catch them in review.
- Once the current item is done AND architecturally sound, check the plan for the next unchecked item (- [ ]) and direct Claude to it by name.
- If unchecked items remain, give Claude the next specific implementation step based on the plan.
- If ALL plan items are checked (- [x]) or Claude's output indicates everything is implemented,
  DO NOT transition yet. Instead, ask Claude to verify its work: craft a prompt telling Claude
  to re-read the PLAN.md and the files it changed, then confirm that EVERY item from the plan
  has been implemented. Claude must explicitly list each plan item and state whether it is done
  or missing. Also check for TODOs, placeholder code, missed requirements, or incomplete sections.
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

    plan_section = ""
    if claude_plan:
        plan_section = f"""
CLAUDE'S IMPLEMENTATION PLAN:
{claude_plan}

IMPORTANT: This plan is your source of truth. Track progress against ALL items — look at
the checkboxes: - [ ] means not done, - [x] means done. If ALL items are - [x], the plan
IS complete — proceed to verification/transition. Don't let Claude get stuck polishing one
item while other plan items remain unstarted. If unchecked items remain, direct Claude to
the NEXT unchecked (- [ ]) item in the plan by name.
"""

    codex_prompt = f"""You are a senior engineering project manager overseeing an autonomous coding session.
You are responsible for driving the work through three phases: implementation → code review → testing.

ORIGINAL TASK:
{original_task}
{plan_section}
YOUR PRIMARY REFERENCE IS THE PLAN ABOVE. Use it to maintain big-picture awareness:
1. First, check whether the work Claude just did is actually complete and correct.
2. Then, check which plan items are still unchecked (- [ ]) to decide what's next.
3. If ALL items are checked (- [x]), the plan is COMPLETE — proceed to verification.
Don't tunnel-vision on the current item — but also don't skip ahead until it's done right.

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
7. DESIGN GUARDIAN ROLE: You are the architectural gatekeeper. Every time you read Claude's output,
   actively evaluate the design and architecture choices: separation of concerns, abstraction quality,
   coupling between components, naming conventions, consistency with existing codebase patterns,
   scalability, and maintainability. If something looks wrong or suboptimal, DO NOT just move on to
   the next task — intervene and tell Claude to fix the structural issue first. Be specific: name
   the problem, explain why it's wrong, and suggest how to restructure. Catching bad architecture
   early saves expensive rework later.
8. If Claude entered plan mode or is asking for plan approval, tell it to exit plan mode immediately and just implement directly. Plan mode wastes steps in autonomous execution.

RESPOND WITH ONE OF:
- "QUOTA:<wait_minutes>\\n<details>" if Claude's output indicates it hit a rate limit, quota exceeded,
  usage cap, or is out of usage. Extract the reset time from Claude's message and calculate how many
  minutes until the reset. Put that number after QUOTA: (e.g. "QUOTA:45" means wait 45 minutes).
  If you cannot determine the reset time, use "QUOTA:60". On the next line, include the raw reset
  info from Claude's output (e.g. "Resets at 3:45 PM").
- "VERIFY:<next_phase>\\n<verification prompt for Claude>" to ask Claude to verify before transitioning
- "PHASE:<next_phase>\\n<prompt for Claude>" to transition (ONLY when reviewing a verification result)
- "DONE\\n<summary>" to finish (ONLY when reviewing a verification result where all tests pass)
- Or the exact next prompt to send to Claude (nothing else, no meta-commentary)"""

    if stale_warning:
        codex_prompt += f"\n\n⚠️ STALE PROGRESS WARNING:\n{stale_warning}"

    if user_feedback:
        codex_prompt += user_feedback

    print(f"[Codex] Calling Codex. Step: {step}, phase: {phase}, pending_transition: {pending_transition}", flush=True)
    print(f"[Codex] Prompt length: {len(codex_prompt)}, Claude output length: {len(claude_output)}", flush=True)

    try:
        process = subprocess.Popen(
            [
                "codex", "exec",
                "-m", CODEX_MODEL,
                "-c", 'model_reasoning_effort="xhigh"',
                "--dangerously-bypass-approvals-and-sandbox",
                codex_prompt
            ],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        stdout, stderr = process.communicate(timeout=300)

        output = (stdout or "").strip()
        error_output = (stderr or "").strip()
        print(f"[Codex] Raw output ({len(output)} chars): {output[:300]}...", flush=True)
        if error_output:
            print(f"[Codex] Stderr: {error_output[:200]}", flush=True)

        # Check for ERROR: lines in stderr (quota, auth, model errors).
        # This is the most reliable detection — Codex CLI prefixes fatal errors with "ERROR:"
        stderr_error_lines = [l for l in error_output.split("\n") if l.startswith("ERROR:")]
        if stderr_error_lines:
            error_msg = stderr_error_lines[-1]
            wait_secs, _ = _parse_reset_wait(error_msg)
            wait_min = max(1, wait_secs // 60)
            print(f"[Codex] Fatal error detected: {error_msg}", flush=True)
            return None, False, f"QUOTA:{wait_min} Codex error — {error_msg[:200]}"

        if not output:
            return None, False, "Codex produced no output"

        if output.startswith("DONE"):
            summary = output[4:].strip().lstrip("\n")
            print(f"[Codex] Decision: DONE. Summary: {summary[:200]}", flush=True)
            return None, True, summary

        # Check if Codex detected Claude hit a quota/rate-limit
        # Format: "QUOTA:<wait_minutes>\n<details>"
        if output.startswith("QUOTA:"):
            first_line, _, rest = output.partition("\n")
            wait_str = first_line[6:].strip()
            details = rest.strip() or "no details"
            try:
                wait_min = max(1, int(wait_str))
            except (ValueError, TypeError):
                wait_min = 60
            print(f"[Codex] Decision: Claude quota detected. Wait: {wait_min}min. Details: {details[:200]}", flush=True)
            return None, False, f"QUOTA:{wait_min} {details[:200]}"

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
        print(f"[Codex] TIMEOUT after 300s (phase: {phase})", flush=True)
        # Phase-aware fallback prompts so we don't send nonsensical "continue implementing" during review/test
        timeout_fallbacks = {
            "implementing": "Continue implementing the next unfinished item from the plan.",
            "reviewing": "Continue the code review. Check for bugs, edge cases, design flaws, and anything that needs fixing.",
            "testing": "Continue writing and running tests. Focus on integration tests for the key workflows.",
        }
        fallback = timeout_fallbacks.get(phase, timeout_fallbacks["implementing"])
        return fallback, False, "Codex timed out"
    except FileNotFoundError:
        print(f"[Codex] ERROR: codex binary not found", flush=True)
        return None, False, "Codex not found"
    except Exception as e:
        print(f"[Codex] EXCEPTION: {e}", flush=True)
        err_str = str(e)
        if QUOTA_REGEX.search(err_str):
            return None, False, f"QUOTA:60 Codex exception — {err_str[:200]}"
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


def run_omni_loop(chat_id, task, session):
    """Main autonomous execution loop for /omni: Claude (Architect) -> Gemini (Execute, Claude fallback) -> Codex (Audit)."""
    session_id = get_session_id(session)
    chat_key = f"{chat_id}:{session_id}"
    cwd = session["cwd"]
    log_prefix = f"[Omni {chat_id}:{session.get('name', 'unknown')}]"
    original_task = task  # Preserve original task — don't mutate
    _ws_session_override.name = session.get("name", "")

    print(f"{log_prefix} Starting. Task: {task[:200]}", flush=True)
    print(f"{log_prefix} Session ID: {session_id}, CWD: {cwd}", flush=True)

    omni_active[chat_key] = {
        "active": True,
        "paused": False,
        "resume_event": threading.Event(),
        "task": task,
        "step": 0,
        "phase": "architecting",
        "chat_id": str(chat_id),
        "session_name": session.get("name", "unknown"),
        "started": time.time(),
    }
    omni_active[chat_key]["resume_event"].set()  # Not paused initially
    save_active_tasks()
    _ws_broadcast_status(chat_id, "omni", "starting", 0, active=True, task=task, started=omni_active[chat_key]["started"])

    step = 0
    phase = "architecting"  # architecting -> executing -> auditing
    audit_feedback = ""  # Carries Codex feedback into next execute cycle
    notified_exit = False
    preferred_executor = "gemini"  # Default; Codex can override with EXECUTOR: CLAUDE/GEMINI
    # Stale-loop guards: if Codex keeps returning effectively the same rejection,
    # stop Omni instead of thrashing between architect/audit phases forever.
    plan_reject_streak = 0
    last_plan_reject_sig = ""
    audit_reject_streak = 0
    last_audit_reject_sig = ""
    STALE_REJECT_LIMIT = 4

    def _feedback_signature(text):
        """Normalize model feedback to detect semantic repeats across timestamps/IDs."""
        if not text:
            return ""
        norm = text[:1500].upper()  # Truncate first to avoid regex work on discarded text
        norm = re.sub(r"`[^`]*`", "`X`", norm)  # Strip volatile inline values
        norm = re.sub(r"\b[0-9A-F]{7,40}\b", "#HASH", norm)  # commit-ish IDs
        norm = re.sub(r"\d+", "#", norm)  # step numbers, times, counters
        norm = re.sub(r"\s+", " ", norm).strip()
        return norm

    def _check_open_blockers(cwd):
        """Return list of unchecked BLOCKER lines from PLAN.md, or empty list."""
        try:
            with open(os.path.join(cwd, "PLAN.md"), "r") as f:
                return re.findall(r'^- \[ \] BLOCKER:.*', f.read(), re.MULTILINE)
        except FileNotFoundError:
            return []
        except Exception as e:
            print(f"[Omni] Error checking blockers: {e}", flush=True)
            return []

    try:
        send_message(chat_id, f"""🚀 *Omni Task Started* on `{session.get('name', 'unknown')}`

Task: _{task[:200]}_

_Claude (Architect) → Gemini (Execute) → Codex (Audit)_
_Claude as fallback if Gemini fails._
_Use /cancel to stop at any time._""")

        while omni_active.get(chat_key, {}).get("active"):
            step += 1
            omni_active[chat_key]["step"] = step
            omni_active[chat_key]["phase"] = phase
            save_active_tasks()
            _ws_broadcast_status(chat_id, "omni", phase, step)

            # Stop if we hit a runaway limit
            if step > 200:
                send_message(chat_id, "⚠️ *Omni limit reached* (200 steps). Stopping to prevent loop.")
                break

            print(f"{log_prefix} === Step {step} === Phase: {phase}", flush=True)

            # --- Phase 1: Architect (Claude) ---
            if phase == "architecting":
                send_message(chat_id, f"🏛️ *Step {step}: Architecting* (Claude)\nUpdating PLAN.md...")

                # Snapshot dirty files before architect runs (for phase enforcement)
                try:
                    _pre = subprocess.run(
                        ["git", "diff", "--name-only"], capture_output=True, text=True, cwd=cwd, timeout=10
                    ).stdout.strip()
                    pre_arch_dirty = set(_pre.split('\n')) if _pre else set()
                except Exception:
                    pre_arch_dirty = set()

                arch_prompt = (
                    f"Update PLAN.md in the root directory to reflect the implementation plan for the following task:\n\n"
                    f"{original_task}\n\n"
                    f"Use markdown checkboxes: - [ ] for pending, - [x] for done.\n"
                    f"Ensure architecture is solid and testing is planned.\n"
                    f"IMPORTANT: Do NOT enter plan mode (EnterPlanMode). Write PLAN.md directly.\n"
                    f"IMPORTANT: Do NOT modify any code files — only PLAN.md. You are the architect, not the executor.\n"
                    f"IMPORTANT: If PLAN.md has a '## Blockers' section, preserve it exactly. Do not remove or uncheck blocker lines."
                )
                if audit_feedback:
                    arch_prompt += f"\n\nPrevious audit feedback to incorporate:\n{audit_feedback}"

                response, questions, _, claude_sid, context_overflow = run_claude_streaming(
                    arch_prompt, chat_id, cwd=cwd, continue_session=True,
                    session_id=session_id, session=session
                )
                _ws_broadcast_status(chat_id, "omni", phase, step)  # Re-assert after Claude exits
                if not _check_pause(omni_active, chat_key, chat_id, "omni", phase, step):
                    break

                # Persist Claude session ID
                if claude_sid:
                    update_claude_session_id(chat_id, session, claude_sid)
                    session = get_session_by_id(chat_id, session_id) or session

                # Handle context overflow
                if context_overflow:
                    print(f"{log_prefix} Step {step}: Context overflow, resetting Claude session", flush=True)
                    send_message(chat_id, "⚠️ Context overflow — resetting Claude session...")
                    update_claude_session_id(chat_id, session, None)
                    reset_message_count(chat_id, session, "Claude")

                # Auto-answer any questions
                if questions:
                    auto_answer = handle_justdoit_questions(questions)
                    print(f"{log_prefix} Step {step}: Auto-answering {len(questions)} questions", flush=True)
                    send_message(chat_id, f"🤖 *Auto-answering:* _{auto_answer[:100]}_")
                    _, _, _, claude_sid2, _ = run_claude_streaming(
                        auto_answer, chat_id, cwd=cwd, continue_session=True,
                        session_id=session_id, session=session
                    )
                    if not omni_active.get(chat_key, {}).get("active"):
                        break
                    if claude_sid2:
                        update_claude_session_id(chat_id, session, claude_sid2)
                        session = get_session_by_id(chat_id, session_id) or session

                if response:
                    print(f"{log_prefix} Step {step}: Claude architect response: {response[:300]}...", flush=True)

                # Phase enforcement: revert any code changes made during architecting
                # Compare current dirty files against pre-architect snapshot to find new changes
                try:
                    diff_out = subprocess.run(
                        ["git", "diff", "--name-only"], capture_output=True, text=True, cwd=cwd, timeout=10
                    ).stdout.strip()
                    current_dirty = set(diff_out.split('\n')) if diff_out else set()
                    new_changes = current_dirty - pre_arch_dirty
                    non_plan = [f for f in new_changes if f and f != 'PLAN.md']
                    if non_plan:
                        print(f"{log_prefix} Step {step}: Architect modified non-plan files: {non_plan}", flush=True)
                        subprocess.run(["git", "checkout", "--"] + non_plan, cwd=cwd, timeout=10)
                        short_list = ", ".join(non_plan[:5])
                        if len(non_plan) > 5:
                            short_list += f" (+{len(non_plan) - 5} more)"
                        send_message(chat_id, f"⚠️ Architect touched code files (reverted): {short_list}")
                except Exception as e:
                    print(f"{log_prefix} Step {step}: Phase enforcement error: {e}", flush=True)

                # Drain any user feedback sent during architecting
                feedback = drain_user_feedback(chat_key)
                if feedback:
                    print(f"{log_prefix} Step {step}: Including user feedback in plan review", flush=True)

                # Codex reviews the plan before execution
                omni_active[chat_key]["phase"] = "reviewing"
                _ws_broadcast_status(chat_id, "omni", "reviewing", step)
                send_message(chat_id, f"📋 *Step {step}: Plan Review* (Codex)\nReviewing PLAN.md...")
                plan_review_prompt = (
                    f"Review PLAN.md against the original task:\n\n{original_task}\n\n"
                    f"Check that the plan is complete, feasible, well-structured, and covers testing.\n\n"
                    f"BLOCKER LEDGER: Check PLAN.md for a '## Blockers' section. If it exists, review each blocker.\n"
                    f"- If a blocker is already resolved in the code, mark it [x].\n"
                    f"- If you find new issues with the plan, add them as: - [ ] BLOCKER: <description> (files: <relevant files>)\n"
                    f"- Do NOT remove blocker lines — only check/uncheck them.\n"
                    f"- You may NOT sign off if any blocker lines show [ ] (unchecked).\n\n"
                    f"If the plan is solid and ready for execution, respond with:\n"
                    f"SIGN-OFF\n"
                    f"- Blockers resolved: <count or 'none found'>\n"
                    f"- Key files: <main files the plan targets>\n"
                    f"EXECUTOR: GEMINI or EXECUTOR: CLAUDE\n\n"
                    f"Choose CLAUDE for complex multi-file refactors, subtle bug fixes, or tasks requiring deep reasoning.\n"
                    f"Choose GEMINI for straightforward implementation, file creation, running tests, or mechanical changes.\n"
                    f"Otherwise, provide specific feedback on what needs to change."
                )
                if feedback:
                    plan_review_prompt += feedback
                if session:
                    bridge = get_context_bridge(session, "Codex")
                    if bridge:
                        plan_review_prompt = bridge + "[NEW TASK]\n" + plan_review_prompt

                if not omni_active.get(chat_key, {}).get("active"):
                    break
                plan_review = run_codex(plan_review_prompt, cwd=cwd, session=session, stale_timeout=300)
                update_session_state(chat_id, session, original_task, "Codex")
                if not omni_active.get(chat_key, {}).get("active"):
                    break

                if plan_review:
                    print(f"{log_prefix} Step {step}: Codex plan review: {plan_review[:500]}...", flush=True)
                    send_message(chat_id, f"📋 *Plan Review:*\n_{plan_review[:1000]}_")

                has_signoff = any(line.strip().upper().startswith("SIGN-OFF") for line in plan_review.strip().split("\n")) if plan_review else False
                # Parse executor recommendation from Codex (e.g. "EXECUTOR: CLAUDE")
                if plan_review:
                    for line in plan_review.strip().split("\n"):
                        stripped = line.strip().upper()
                        if stripped.startswith("EXECUTOR:"):
                            rec = stripped.split(":", 1)[1].strip()
                            if "CLAUDE" in rec:
                                preferred_executor = "claude"
                            elif "GEMINI" in rec:
                                preferred_executor = "gemini"
                            break

                # Contradiction gate: reject plan sign-off if open blockers remain
                if has_signoff:
                    open_blockers = _check_open_blockers(cwd)
                    if open_blockers:
                        has_signoff = False
                        print(f"{log_prefix} Step {step}: Plan sign-off REJECTED — {len(open_blockers)} open blocker(s)", flush=True)
                        send_message(chat_id, f"🚫 *Plan sign-off rejected* — {len(open_blockers)} open blocker(s) in PLAN.md")

                if has_signoff:
                    print(f"{log_prefix} Step {step}: Plan approved by Codex, executor={preferred_executor}", flush=True)
                    send_message(chat_id, f"✅ Plan approved by Codex. Executing with *{preferred_executor.capitalize()}*.")
                    audit_feedback = ""  # Clear so execution doesn't inherit stale plan-review feedback
                    plan_reject_streak = 0
                    last_plan_reject_sig = ""
                    phase = "executing"
                    _ws_broadcast_status(chat_id, "omni", phase, step)
                else:
                    # Codex rejected the plan — feed back to Claude
                    audit_feedback = plan_review[:6000] if plan_review else "Plan review returned no feedback."
                    sig = _feedback_signature(plan_review)
                    if sig and sig == last_plan_reject_sig:
                        plan_reject_streak += 1
                    else:
                        plan_reject_streak = 1
                        last_plan_reject_sig = sig
                    print(f"{log_prefix} Step {step}: Plan reject streak={plan_reject_streak}", flush=True)
                    if plan_reject_streak >= STALE_REJECT_LIMIT:
                        send_message(chat_id, f"""🛑 *Omni stopped: stale plan-review loop detected* (step {step})

Codex returned effectively the same plan rejection *{plan_reject_streak}* times in a row.
No new actionable code change was identified, so Omni stopped to prevent churn.

Use `/omni` again with an explicit human override (for example: accept current ops-blocked status, or provide one concrete code change to force).""")
                        notified_exit = True
                        break
                    print(f"{log_prefix} Step {step}: Plan rejected by Codex, looping back", flush=True)
                    phase = "architecting"
                    _ws_broadcast_status(chat_id, "omni", phase, step)

                time.sleep(2)
                continue

            # --- Phase 2: Execute (Codex picks executor, with fallback) ---
            if phase == "executing":
                # Check cancellation/pause
                if not _check_pause(omni_active, chat_key, chat_id, "omni", phase, step):
                    break

                exec_prompt = f"Original task:\n{original_task}\n\nReview the current PLAN.md and project state. Implement the next pending step of the plan. Verify your work with tests where applicable."
                if audit_feedback:
                    exec_prompt = f"Original task:\n{original_task}\n\nFix the issues identified in the recent audit:\n{audit_feedback}\n\nThen proceed with the next pending step from PLAN.md. Verify your work with tests where applicable."

                # Use Codex's recommended executor
                use_executor = preferred_executor

                if use_executor == "gemini":
                    send_message(chat_id, f"⚒️ *Step {step}: Executing* (Gemini)\n_{exec_prompt[:150]}_")
                    exec_response, gemini_sid, gemini_error, gemini_did_work = run_gemini_streaming(
                        exec_prompt, chat_id, cwd=cwd, session=session,
                        session_id=session_id
                    )
                    session = get_session_by_id(chat_id, session_id) or session
                    if not omni_active.get(chat_key, {}).get("active"):
                        break

                    # Fallback to Claude if Gemini actually failed
                    if gemini_error or (not exec_response.strip() and not gemini_did_work):
                        print(f"{log_prefix} Step {step}: Gemini {'errored' if gemini_error else 'returned empty'}, falling back to Claude", flush=True)
                        send_message(chat_id, f"🔄 *Gemini {'failed' if gemini_error else 'returned empty'}* — falling back to Claude...")
                        use_executor = "claude"  # Fall through to Claude below

                if use_executor == "claude":
                    send_message(chat_id, f"⚒️ *Step {step}: Executing* (Claude)\n_{exec_prompt[:150]}_")
                    exec_response, exec_questions, _, claude_sid, context_overflow = run_claude_streaming(
                        exec_prompt, chat_id, cwd=cwd, continue_session=True,
                        session_id=session_id, session=session
                    )
                    _ws_broadcast_status(chat_id, "omni", phase, step)  # Re-assert after Claude exits
                    if not omni_active.get(chat_key, {}).get("active"):
                        break

                    if claude_sid:
                        update_claude_session_id(chat_id, session, claude_sid)
                        session = get_session_by_id(chat_id, session_id) or session

                    if context_overflow:
                        print(f"{log_prefix} Step {step}: Context overflow, resetting Claude session", flush=True)
                        send_message(chat_id, "⚠️ Context overflow — resetting Claude session...")
                        update_claude_session_id(chat_id, session, None)
                        reset_message_count(chat_id, session, "Claude")

                    if exec_questions:
                        auto_answer = handle_justdoit_questions(exec_questions)
                        print(f"{log_prefix} Step {step}: Auto-answering {len(exec_questions)} questions", flush=True)
                        send_message(chat_id, f"🤖 *Auto-answering:* _{auto_answer[:100]}_")
                        _, _, _, claude_sid2, _ = run_claude_streaming(
                            auto_answer, chat_id, cwd=cwd, continue_session=True,
                            session_id=session_id, session=session
                        )
                        if not omni_active.get(chat_key, {}).get("active"):
                            break
                        if claude_sid2:
                            update_claude_session_id(chat_id, session, claude_sid2)
                            session = get_session_by_id(chat_id, session_id) or session


                if exec_response:
                    print(f"{log_prefix} Step {step}: Execute response: {exec_response[:300]}...", flush=True)

                phase = "auditing"
                _ws_broadcast_status(chat_id, "omni", phase, step)
                time.sleep(2)
                continue

            # --- Phase 3: Audit (Codex) ---
            if phase == "auditing":
                # Check cancellation/pause
                if not _check_pause(omni_active, chat_key, chat_id, "omni", phase, step):
                    break

                send_message(chat_id, f"🕵️ *Step {step}: Auditing* (Codex)\nReviewing implementation...")

                # Drain any user feedback sent during execution
                feedback = drain_user_feedback(chat_key)
                if feedback:
                    print(f"{log_prefix} Step {step}: Including user feedback in audit", flush=True)

                # Get git diff summary for concrete evidence of what changed
                try:
                    diff_stat = subprocess.run(
                        ["git", "diff", "--stat"], capture_output=True, text=True, cwd=cwd, timeout=10
                    ).stdout.strip()
                except Exception:
                    diff_stat = ""
                diff_section = (
                    f"\n\nFILES CHANGED SINCE LAST AUDIT:\n```\n{diff_stat[:2000]}\n```"
                    if diff_stat else "\n\n⚠️ No files were modified during this execution step."
                )

                codex_prompt = (
                    f"Review the recent changes against PLAN.md and the original task:\n\n"
                    f"{original_task}\n"
                    f"{diff_section}\n\n"
                    f"Check for bugs, security issues, or deviations from the plan.\n\n"
                    f"BLOCKER LEDGER: Maintain a '## Blockers' section at the bottom of PLAN.md.\n"
                    f"- For each issue you find, add: - [ ] BLOCKER: <description> (files: <relevant files>)\n"
                    f"- For issues that are now fixed in the code, mark them: - [x] BLOCKER: <description>\n"
                    f"- Do NOT remove blocker lines — only check/uncheck them.\n"
                    f"- CRITICAL: If you previously raised blockers, you must verify each one is actually fixed\n"
                    f"  in the code before marking [x]. Do not assume they are fixed without checking.\n\n"
                    f"To sign off, ALL blocker lines must show [x] (or no blockers exist). Use this format:\n"
                    f"SIGN-OFF\n"
                    f"- Blockers resolved: <count>\n"
                    f"- Files verified: <list of key files checked>\n"
                    f"- Tests: <test results or 'N/A'>\n\n"
                    f"If issues remain, provide precise, actionable feedback.\n"
                    f"Also recommend who should fix it: 'EXECUTOR: GEMINI' or 'EXECUTOR: CLAUDE'.\n"
                    f"Prefer CLAUDE for issues requiring careful reasoning, complex edits, or when repeated attempts have failed."
                )
                if feedback:
                    codex_prompt += feedback
                if session:
                    bridge = get_context_bridge(session, "Codex")
                    if bridge:
                        codex_prompt = bridge + "[NEW TASK]\n" + codex_prompt

                # Run Codex with stale-output watchdog (kills only if no output for 5 min)
                audit_result = run_codex(codex_prompt, cwd=cwd, session=session, stale_timeout=300)
                update_session_state(chat_id, session, original_task, "Codex")
                if not omni_active.get(chat_key, {}).get("active"):
                    break

                if not audit_result:
                    print(f"{log_prefix} Step {step}: Codex returned empty result", flush=True)
                    send_message(chat_id, f"⚠️ *Step {step}:* Codex returned no output. Retrying...")
                    time.sleep(5)
                    if not omni_active.get(chat_key, {}).get("active"):
                        break
                    audit_result = run_codex(codex_prompt, cwd=cwd, session=session, stale_timeout=300)
                    if not omni_active.get(chat_key, {}).get("active"):
                        break

                if audit_result:
                    print(f"{log_prefix} Step {step}: Codex audit result: {audit_result[:500]}...", flush=True)
                    # Show audit result to user
                    send_message(chat_id, f"🔍 *Audit Result (Step {step}):*\n_{audit_result[:1000]}_")

                # Check for sign-off: any line starting with SIGN-OFF counts
                # (Codex often adds preamble text before the SIGN-OFF verdict)
                has_signoff = any(line.strip().upper().startswith("SIGN-OFF") for line in audit_result.strip().split("\n")) if audit_result else False

                # Contradiction gate: reject sign-off if open blockers remain in PLAN.md
                if has_signoff:
                    open_blockers = _check_open_blockers(cwd)
                    if open_blockers:
                        has_signoff = False
                        blocker_list = "\n".join(open_blockers[:10])
                        print(f"{log_prefix} Step {step}: Sign-off REJECTED — {len(open_blockers)} open blocker(s)", flush=True)
                        send_message(chat_id, f"🚫 *Sign-off rejected* — {len(open_blockers)} open blocker(s) in PLAN.md:\n```\n{blocker_list}\n```")
                        audit_feedback = (
                            f"SIGN-OFF REJECTED: {len(open_blockers)} open blocker(s) remain in PLAN.md:\n"
                            f"{blocker_list}\n\n"
                            f"You must fix these before sign-off is accepted.\n"
                            + (audit_result[:4000] if audit_result else "")
                        )

                if has_signoff:
                    send_message(chat_id, f"""✅ *Omni Task Complete!* (Step {step})

Codex provided final sign-off.

_Session preserved. You can continue chatting in this session._""")
                    notified_exit = True
                    break
                else:
                    audit_feedback = audit_result[:6000] if audit_result else "Previous audit returned no feedback."
                    sig = _feedback_signature(audit_result)
                    if sig and sig == last_audit_reject_sig:
                        audit_reject_streak += 1
                    else:
                        audit_reject_streak = 1
                        last_audit_reject_sig = sig
                    print(f"{log_prefix} Step {step}: Audit reject streak={audit_reject_streak}", flush=True)
                    if audit_reject_streak >= STALE_REJECT_LIMIT:
                        send_message(chat_id, f"""🛑 *Omni stopped: stale audit loop detected* (step {step})

Codex audit feedback was effectively unchanged for *{audit_reject_streak}* consecutive rounds.
No new code-level action emerged, so Omni stopped to avoid endless architect/audit cycling.

Use `/omni` again with an explicit decision (accept ops-blocked state, or provide one concrete fix target).""")
                        notified_exit = True
                        break
                    # Parse executor recommendation for next cycle
                    if audit_result:
                        for line in audit_result.strip().split("\n"):
                            stripped = line.strip().upper()
                            if stripped.startswith("EXECUTOR:"):
                                rec = stripped.split(":", 1)[1].strip()
                                if "CLAUDE" in rec:
                                    preferred_executor = "claude"
                                elif "GEMINI" in rec:
                                    preferred_executor = "gemini"
                                break
                    # Loop back: architect incorporates feedback, then execute fixes
                    phase = "architecting"
                    _ws_broadcast_status(chat_id, "omni", phase, step)

                time.sleep(2)

        if not notified_exit:
            send_message(chat_id, f"🏁 *Omni process finished* for `{session.get('name', 'unknown')}`.")

    except Exception as e:
        import traceback
        print(f"{log_prefix} EXCEPTION: {e}", flush=True)
        print(f"{log_prefix} Traceback:\n{traceback.format_exc()}", flush=True)
        try:
            send_message(chat_id, f"❌ *Omni error:* {str(e)[:300]}")
        except Exception:
            pass
    finally:
        omni_active.pop(chat_key, None)
        user_feedback_queue.pop(chat_key, None)
        save_active_tasks()
        _ws_broadcast_status(chat_id, "omni", "", 0, active=False)
        _ws_session_override.name = None


def run_justdoit_loop(chat_id, task, session):
    """Main autonomous execution loop for /justdoit."""
    session_id = get_session_id(session)
    chat_key = f"{chat_id}:{session_id}"
    cwd = session["cwd"]
    log_prefix = f"[JustDoIt {chat_id}:{session.get('name', 'unknown')}]"
    # Pin WS session label to the originating session for all send_message calls on this thread
    _ws_session_override.name = session.get("name", "")

    print(f"{log_prefix} Starting. Task: {task[:200]}", flush=True)
    print(f"{log_prefix} Session ID: {session_id}, CWD: {cwd}", flush=True)

    justdoit_active[chat_key] = {
        "active": True,
        "paused": False,
        "resume_event": threading.Event(),
        "task": task,
        "step": 0,
        "phase": "implementing",
        "chat_id": str(chat_id),
        "session_name": session.get("name", "unknown"),
        "started": time.time(),
    }
    justdoit_active[chat_key]["resume_event"].set()  # Not paused initially
    save_active_tasks()
    _ws_broadcast_status(chat_id, "justdoit", "starting", 0, active=True, task=task, started=justdoit_active[chat_key]["started"])

    step = 0
    phase = "implementing"
    history_summary = ""
    plan_file = os.path.join(cwd, "PLAN.md")
    claude_plan = ""  # Read from plan file to give Codex full plan visibility
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

        # Step 0: Ask Claude to consolidate/create a plan file
        # Claude knows its own session context — it knows if it already created a plan somewhere
        print(f"{log_prefix} Step 0: Asking Claude for plan file", flush=True)
        plan_setup_prompt = (
            "Before we begin autonomous implementation, I need a plan file.\n"
            "IMPORTANT: If you are currently in plan mode, exit plan mode FIRST (use ExitPlanMode), then proceed.\n"
            "Do NOT use EnterPlanMode at any point during this autonomous session.\n"
            "1. If you already created a plan/todo file in this project, copy its content to PLAN.md in the project root.\n"
            "2. If no plan exists yet, create PLAN.md with a structured checklist for the task.\n"
            "Use markdown checkboxes: - [ ] for pending, - [x] for done.\n"
            "Then reply with ONLY the text: PLAN_READY"
        )
        plan_response, _, _, plan_sid, _ = run_claude_streaming(
            plan_setup_prompt, chat_id, cwd=cwd, continue_session=True,
            session_id=session_id, session=session
        )
        if plan_sid:
            update_claude_session_id(chat_id, session, plan_sid)
            session = get_session_by_id(chat_id, session_id) or session

        # Read the plan file Claude just created/updated
        try:
            if os.path.exists(plan_file):
                with open(plan_file, "r") as f:
                    claude_plan = f.read()[:5000]
                print(f"{log_prefix} Step 0: PLAN.md loaded ({len(claude_plan)} chars)", flush=True)
            else:
                print(f"{log_prefix} Step 0: PLAN.md not found after setup", flush=True)
        except Exception:
            pass

        current_prompt = task + (
            "\n\nRemember to update PLAN.md checkboxes (- [ ] → - [x]) as you complete each item."
            "\n\nIMPORTANT: Do NOT enter plan mode (EnterPlanMode) during this session. "
            "Just implement directly — the plan is already in PLAN.md."
        )

        while True:
            # Check cancellation/pause
            if not _check_pause(justdoit_active, chat_key, chat_id, "justdoit", phase, step):
                send_message(chat_id, f"⚠️ *JustDoIt cancelled* at step {step}.")
                notified_exit = True
                break

            step += 1
            justdoit_active[chat_key]["step"] = step
            justdoit_active[chat_key]["phase"] = phase
            save_active_tasks()
            _ws_broadcast_status(chat_id, "justdoit", phase, step)

            print(f"{log_prefix} === Step {step} === Phase: {phase}, Pending transition: {pending_transition}", flush=True)

            # --- Phase 1: Send prompt to Claude ---
            print(f"{log_prefix} Step {step}: Sending to Claude. Prompt: {current_prompt[:200]}...", flush=True)
            send_message(chat_id, f"🔄 *Step {step}* — Sending to Claude...")

            # Handle compaction
            needs_compaction = increment_message_count(chat_id, session, "Claude")

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
                except Exception:
                    summary = ""

                # Persist summary before clearing session (survives crashes)
                if summary and len(summary) > 50:
                    save_session_summary(chat_id, session, summary)

                update_claude_session_id(chat_id, session, None)
                reset_message_count(chat_id, session, "Claude")

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

            # Run Claude
            response, questions, _, claude_sid, context_overflow = run_claude_streaming(
                current_prompt, chat_id, cwd=cwd, continue_session=True,
                session_id=session_id, session=session
            )

            # Re-assert busy status — run_claude_streaming broadcasts busy:False on exit
            _ws_broadcast_status(chat_id, "justdoit", phase, step)

            print(f"{log_prefix} Step {step}: Claude response length: {len(response) if response else 0}, questions: {bool(questions)}, context_overflow: {context_overflow}", flush=True)
            if response:
                print(f"{log_prefix} Step {step}: Claude response preview: {response[:300]}...", flush=True)

            # NOTE: Claude quota/rate-limit detection is handled by Codex.
            # Codex sees Claude's output, detects quota errors, and responds with QUOTA:<minutes>.
            # The QUOTA handler below (after run_codex_review) handles the wait.

            # Update session ID
            if claude_sid:
                update_claude_session_id(chat_id, session, claude_sid)
                session = get_session_by_id(chat_id, session_id) or session

            # Handle context overflow
            if context_overflow:
                print(f"{log_prefix} Step {step}: Context overflow detected, compacting.", flush=True)
                send_message(chat_id, "⚠️ Context overflow — compacting...")
                update_claude_session_id(chat_id, session, None)
                reset_message_count(chat_id, session, "Claude")

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

            # Re-read PLAN.md after each step (Claude may have updated checkboxes)
            try:
                if os.path.exists(plan_file):
                    with open(plan_file, "r") as f:
                        claude_plan = f.read()[:5000]
            except Exception:
                pass

            # Update rolling history — no cap, Codex models have large context windows
            step_summary = clean_response[:1500]
            history_summary += f"\n\nStep {step}: {step_summary}"

            # --- Phase 2: Pause (human-like pacing) ---
            time.sleep(3)

            # Check cancellation/pause before Codex
            if not _check_pause(justdoit_active, chat_key, chat_id, "justdoit", phase, step):
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

            # Drain any user feedback sent during execution
            feedback = drain_user_feedback(chat_key)
            if feedback:
                print(f"{log_prefix} Step {step}: Including user feedback in Codex review", flush=True)

            print(f"{log_prefix} Step {step}: Calling Codex review. Phase: {phase}, pending_transition: {pending_transition}", flush=True)
            next_prompt, is_done, reasoning = run_codex_review(
                task, clean_response, step, history_summary, cwd, phase=phase,
                pending_transition=pending_transition, stale_warning=stale_warning,
                claude_plan=claude_plan, user_feedback=feedback
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
                    _ws_broadcast_status(chat_id, "justdoit", phase, step)
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
                        _ws_broadcast_status(chat_id, "justdoit", phase, step)
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

            # Handle quota errors — wait and retry
            # Format: "QUOTA:<minutes> <details>" from both Codex errors and Codex-detected Claude errors
            if next_prompt is None and reasoning and reasoning.startswith("QUOTA:"):
                # Parse "QUOTA:<minutes> <details>"
                quota_rest = reasoning[6:].strip()
                parts = quota_rest.split(" ", 1)
                try:
                    wait_min = max(1, int(parts[0]))
                except (ValueError, IndexError):
                    wait_min = 60
                details = parts[1] if len(parts) > 1 else ""
                wait_secs = wait_min * 60
                resume_time = (datetime.now() + timedelta(seconds=wait_secs)).strftime('%H:%M')
                print(f"{log_prefix} Step {step}: Rate limited. Wait: {wait_min}min. {details[:200]}", flush=True)
                send_message(chat_id,
                    f"⏳ *Rate limited* at step {step}.\n"
                    f"{details[:200]}\n"
                    f"_Waiting ~{wait_min}min... (resume ~{resume_time})_\n"
                    f"_Use /cancel to abort._")
                if not _justdoit_wait(chat_key, wait_secs):
                    send_message(chat_id, f"⚠️ *JustDoIt cancelled* during rate-limit wait.")
                    break
                send_message(chat_id, "🔄 *Resuming after rate-limit wait...*")
                next_prompt, is_done, reasoning = run_codex_review(
                    task, clean_response, step, history_summary, cwd, phase=phase,
                    pending_transition=pending_transition, claude_plan=claude_plan
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
                        _ws_broadcast_status(chat_id, "justdoit", phase, step)
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
                            _ws_broadcast_status(chat_id, "justdoit", phase, step)
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
                next_prompt = "Continue implementing the next unfinished item from the plan."
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
        _ws_broadcast_status(chat_id, "justdoit", "", 0, active=False)
        _ws_session_override.name = None  # Clear thread-local override


def run_codex_deepreview(claude_output, review_history, step, cwd, phase):
    """Call Codex to review Claude's review output during deepreview.

    Returns: (next_prompt: str or None, is_clean: bool, reasoning: str)
    - next_prompt: prompt to send to Claude for fixes, or None
    - is_clean: True if Codex found no issues
    - reasoning: explanation of Codex's decision (starts with "QUOTA:" if rate-limited)
    """
    max_output_len = 8000
    if len(claude_output) > max_output_len:
        claude_output = claude_output[:max_output_len] + "\n\n... (output truncated)"

    max_history_len = 6000
    if len(review_history) > max_history_len:
        review_history = review_history[-max_history_len:]

    if phase == "codex_reviews_claude":
        codex_prompt = f"""You are a ruthless senior staff engineer doing a deep code review.

You are reviewing Claude's detailed review output. Your job is to catch things Claude missed or got wrong:

1. DESIGN/ARCHITECTURE FLAWS: Poor abstractions, god functions, tight coupling, wrong patterns
2. BANDAIDS/HACKS: Quick fixes that don't address root causes, workarounds due to laziness
3. DEGRADING FALLBACKS: New fallback paths that silently degrade the product instead of failing properly
4. MISSED ISSUES: Bugs, race conditions, security issues Claude didn't catch
5. OVER-ENGINEERING: Unnecessary abstractions, premature optimization, gold-plating

REVIEW HISTORY SO FAR:
{review_history}

CLAUDE'S LATEST REVIEW OUTPUT:
{claude_output}

If you find ANY of the above issues, respond with a SPECIFIC prompt to give to Claude telling it exactly what to fix and why. Be direct and technical — name the exact function, file, pattern, or line that's wrong.

If Claude's review and fixes are solid — no design flaws, no bandaids, no degrading fallbacks, no hacks — respond with exactly:
CLEAN

Do NOT be lenient. Do NOT say CLEAN if there are real issues. But also do NOT nitpick style or cosmetic issues — focus on correctness, design, and architecture."""

    elif phase == "codex_final_signoff":
        codex_prompt = f"""You are a ruthless senior staff engineer doing a FINAL review of a deep code review session.

Throughout this session, Claude has been reviewing and fixing code. Now you must do a final comprehensive check.

FULL REVIEW HISTORY:
{review_history}

CLAUDE'S LATEST OUTPUT:
{claude_output}

Check for:
1. Did Claude actually fix the issues it found, or just describe them?
2. Are there any design/architecture flaws remaining?
3. Any bandaids, hacks, or lazy shortcuts that slipped through?
4. Any new fallbacks that degrade the product?
5. Any regressions — did fixing one thing break another?

If you find issues, respond with a SPECIFIC prompt to give to Claude to fix them.

If everything is solid and the code is clean, respond with exactly:
CLEAN

This is the final gate. Be thorough but fair."""

    else:
        return None, False, f"Unknown phase: {phase}"

    print(f"[DeepReview Codex] Step {step}, phase: {phase}, prompt length: {len(codex_prompt)}", flush=True)

    try:
        process = subprocess.Popen(
            [
                "codex", "exec",
                "-m", CODEX_MODEL,
                "-c", 'model_reasoning_effort="xhigh"',
                "--dangerously-bypass-approvals-and-sandbox",
                codex_prompt
            ],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        stdout, stderr = process.communicate(timeout=600)
        output = (stdout or "").strip()
        error_output = (stderr or "").strip()

        print(f"[DeepReview Codex] Raw output ({len(output)} chars): {output[:300]}...", flush=True)
        if error_output:
            print(f"[DeepReview Codex] Stderr: {error_output[:200]}", flush=True)

        # Check for fatal errors in stderr
        stderr_error_lines = [l for l in error_output.split("\n") if l.startswith("ERROR:")]
        if stderr_error_lines:
            error_msg = stderr_error_lines[-1]
            wait_secs, _ = _parse_reset_wait(error_msg)
            wait_min = max(1, wait_secs // 60)
            return None, False, f"QUOTA:{wait_min} Codex error — {error_msg[:200]}"

        if not output:
            return None, False, "Codex produced no output"

        if output.strip().startswith("CLEAN"):
            return None, True, "No issues found"

        if output.startswith("QUOTA:"):
            first_line, _, rest = output.partition("\n")
            wait_str = first_line[6:].strip()
            details = rest.strip() or "no details"
            try:
                wait_min = max(1, int(wait_str))
            except (ValueError, TypeError):
                wait_min = 60
            return None, False, f"QUOTA:{wait_min} {details[:200]}"

        # Codex found issues — output is the prompt for Claude
        return output, False, "Issues found"

    except subprocess.TimeoutExpired:
        process.kill()
        return None, False, "Codex timed out"
    except FileNotFoundError:
        return None, False, "Codex not found"
    except Exception as e:
        err_str = str(e)
        if QUOTA_REGEX.search(err_str):
            return None, False, f"QUOTA:60 Codex exception — {err_str[:200]}"
        return None, False, f"Codex error: {e}"


def run_codex_deepreview_fix(review_history, step, cwd, is_followup=False, claude_feedback=None):
    """Call Codex to review AND fix code directly (Phase 3).

    Codex runs with --full-auto so it can edit files.
    Returns: (output: str or None, is_clean: bool, reasoning: str)
    - output: Codex's report of what it reviewed/fixed, or None on error
    - is_clean: True if Codex found no issues
    - reasoning: explanation (starts with "QUOTA:" if rate-limited)
    """
    max_history_len = 6000
    if len(review_history) > max_history_len:
        review_history = review_history[-max_history_len:]

    if is_followup and claude_feedback:
        codex_prompt = f"""You are a ruthless senior staff engineer doing a deep code review AND fixing issues directly.

Claude (another AI) reviewed your previous fixes and found problems. Here's Claude's critique:

CLAUDE'S CRITIQUE:
{claude_feedback[:4000]}

REVIEW HISTORY SO FAR:
{review_history}

Your job:
1. Read Claude's critique carefully
2. Review the actual code files to verify Claude's claims
3. If Claude is right, fix the issues directly in the files
4. If Claude is wrong, explain why (but still check for other issues)
5. Look for anything BOTH you and Claude may have missed

After reviewing and fixing, report exactly what you found and changed.

If the code is solid and you found nothing to fix, respond with exactly:
ALL_CLEAN

Focus on correctness, design, and architecture — not cosmetics."""
    else:
        codex_prompt = f"""You are a ruthless senior staff engineer doing a deep code review AND fixing issues directly.

Claude (another AI) has already done {step} rounds of self-review and fixes. Your job is to find what Claude missed and FIX it yourself.

IMPORTANT: Focus ONLY on the files and code areas mentioned in the review history below. Do NOT review the entire project — only the files that were worked on in this session.

REVIEW HISTORY SO FAR:
{review_history}

Your job:
1. Read the actual code files mentioned in the review history
2. Look for issues Claude missed or got wrong:
   - BUGS: Logic errors, race conditions, null access, off-by-one
   - DESIGN FLAWS: Poor abstractions, god functions, tight coupling
   - BANDAIDS/HACKS: Quick fixes that don't address root causes
   - SECURITY: Injection, XSS, auth bypasses, secret leaks
   - OVER-ENGINEERING: Unnecessary abstractions, premature optimization
3. FIX every issue you find directly in the code files
4. Report what you found and fixed

After reviewing and fixing, report exactly what you found and changed.

If the code is solid and you found nothing to fix, respond with exactly:
ALL_CLEAN

Focus on correctness, design, and architecture — not cosmetics."""

    print(f"[DeepReview Codex Fix] Step {step}, is_followup: {is_followup}, prompt length: {len(codex_prompt)}", flush=True)

    try:
        process = subprocess.Popen(
            [
                "codex", "exec",
                "-m", CODEX_MODEL,
                "-c", 'model_reasoning_effort="xhigh"',
                "--dangerously-bypass-approvals-and-sandbox",
                codex_prompt
            ],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        stdout, stderr = process.communicate(timeout=600)
        output = (stdout or "").strip()
        error_output = (stderr or "").strip()

        print(f"[DeepReview Codex Fix] Raw output ({len(output)} chars): {output[:300]}...", flush=True)
        if error_output:
            print(f"[DeepReview Codex Fix] Stderr: {error_output[:200]}", flush=True)

        # Check for fatal errors in stderr
        stderr_error_lines = [l for l in error_output.split("\n") if l.startswith("ERROR:")]
        if stderr_error_lines:
            error_msg = stderr_error_lines[-1]
            wait_secs, _ = _parse_reset_wait(error_msg)
            wait_min = max(1, wait_secs // 60)
            return None, False, f"QUOTA:{wait_min} Codex error — {error_msg[:200]}"

        if not output:
            return None, False, "Codex produced no output"

        if "ALL_CLEAN" in output.upper():
            return output, True, "No issues found"

        if output.startswith("QUOTA:"):
            first_line, _, rest = output.partition("\n")
            wait_str = first_line[6:].strip()
            details = rest.strip() or "no details"
            try:
                wait_min = max(1, int(wait_str))
            except (ValueError, TypeError):
                wait_min = 60
            return None, False, f"QUOTA:{wait_min} {details[:200]}"

        # Codex found and fixed issues — output is its report
        return output, False, "Issues found and fixed"

    except subprocess.TimeoutExpired:
        process.kill()
        return None, False, "Codex timed out"
    except FileNotFoundError:
        return None, False, "Codex not found"
    except Exception as e:
        err_str = str(e)
        if QUOTA_REGEX.search(err_str):
            return None, False, f"QUOTA:60 Codex exception — {err_str[:200]}"
        return None, False, f"Codex error: {e}"


def _deepreview_wait(chat_key, seconds):
    """Sleep for `seconds` while checking deepreview cancellation every 30s."""
    elapsed = 0
    interval = 30
    while elapsed < seconds:
        state = deepreview_active.get(chat_key, {})
        if not state.get("active", False):
            return False
        chunk = min(interval, seconds - elapsed)
        time.sleep(chunk)
        elapsed += chunk
    return deepreview_active.get(chat_key, {}).get("active", False)


def run_deepreview_loop(chat_id, session):
    """Main deep review loop for /deepreview."""
    session_id = get_session_id(session)
    chat_key = f"{chat_id}:{session_id}"
    cwd = session["cwd"]
    log_prefix = f"[DeepReview {chat_id}:{session.get('name', 'unknown')}]"
    _ws_session_override.name = session.get("name", "")

    print(f"{log_prefix} Starting deep review", flush=True)

    deepreview_active[chat_key] = {
        "active": True,
        "paused": False,
        "resume_event": threading.Event(),
        "phase": "claude_self_review",
        "step": 0,
        "chat_id": str(chat_id),
        "session_name": session.get("name", "unknown"),
        "task": "Deep code review",
        "started": time.time(),
    }
    deepreview_active[chat_key]["resume_event"].set()  # Not paused initially
    _ws_broadcast_status(chat_id, "deepreview", "starting", 0, active=True, task="Deep code review", started=deepreview_active[chat_key]["started"])

    step = 0
    review_history = ""
    all_review_history = ""  # Accumulates everything across all phases
    codex_fail_streak = 0
    notified_exit = False

    try:
        send_message(chat_id, """🔬 *Deep Review Mode Activated*

_Phases 1+2: Claude fixes ↔ Codex reviews (loop until Codex satisfied)_
_Phases 3+4: Codex fixes ↔ Claude reviews (loop until Claude satisfied)_

_Use /cancel to stop at any time._""")

        # ============================================================
        # MEGA-LOOP 1: Phases 1+2 (up to 20 bounces)
        # Phase 1: Claude reviews+fixes (single pass)
        # Phase 2: Codex cross-reviews → if issues, back to Phase 1
        # ============================================================
        max_iterations_12 = 20
        iteration_12 = 0
        codex_satisfied = False

        while iteration_12 < max_iterations_12 and not codex_satisfied:
            iteration_12 += 1

            # Check cancellation/pause
            if not _check_pause(deepreview_active, chat_key, chat_id, "deepreview", phase, step):
                if not notified_exit:
                    send_message(chat_id, f"⚠️ *Deep review cancelled* at step {step}.")
                    notified_exit = True
                break

            # --- PHASE 1: Claude reviews and fixes (single pass) ---
            phase = "claude_self_review"
            deepreview_active[chat_key]["phase"] = phase
            step += 1
            deepreview_active[chat_key]["step"] = step
            _ws_broadcast_status(chat_id, "deepreview", phase, step)

            if iteration_12 == 1:
                send_message(chat_id, f"🔍 *Step {step}* — Phase 1: Claude reviewing & fixing...")

                # Build session-scoped prompt
                session_context = ""
                if session.get("last_summary"):
                    session_context = f"\n\nSESSION CONTEXT (what we've been working on):\n{session['last_summary'][:2000]}\n"
                elif session.get("last_prompt"):
                    session_context = f"\n\nLAST TASK: {session['last_prompt']}\n"

                prompt = f"""Do a deep, thorough review of the code you've been working on in this session. Focus on the files and areas we've touched or discussed — NOT the entire project.{session_context}
Be ruthlessly critical. Look for:
1. BUGS: Logic errors, off-by-one, null/undefined access, race conditions
2. DESIGN FLAWS: Poor abstractions, god functions, tight coupling, wrong patterns
3. SECURITY: Injection, XSS, auth bypasses, secret leaks
4. ERROR HANDLING: Silent failures, swallowed exceptions, missing error paths
5. EDGE CASES: Empty inputs, large inputs, concurrent access, network failures
6. PERFORMANCE: N+1 queries, unnecessary allocations, blocking operations in async code

For each issue found:
- State the exact file and location
- Explain why it's a problem
- Fix it immediately

After fixing everything you find, report what you fixed and what looks clean."""
            else:
                # Codex sent us back with feedback
                codex_feedback = review_history.split("=== Codex cross-review")[-1][:3000] if "=== Codex cross-review" in review_history else review_history[-2000:]
                send_message(chat_id, f"🔍 *Step {step}* — Phase 1 (iteration {iteration_12}): Claude fixing Codex's findings...")
                prompt = f"""A senior engineer (Codex) reviewed your code and found these issues. Fix them ALL:

{codex_feedback}

After fixing, do another pass to make sure you didn't introduce regressions. Report exactly what you changed. If you disagree with any feedback, explain why."""

            # Handle compaction
            needs_compaction = increment_message_count(chat_id, session, "Claude")
            if needs_compaction:
                send_message(chat_id, "📦 *Auto-compacting* session context...")
                try:
                    summary_response, _, _, _, _ = run_claude_streaming(
                        "Summarize this session for context continuity (max 500 words). Focus on files changed, issues found and fixed, and current state.",
                        chat_id, cwd=cwd, continue_session=True,
                        session_id=session_id, session=session
                    )
                    summary = summary_response.split("———")[0].strip() if summary_response else ""
                except Exception:
                    summary = ""
                if summary and len(summary) > 50:
                    save_session_summary(chat_id, session, summary)
                update_claude_session_id(chat_id, session, None)
                reset_message_count(chat_id, session, "Claude")
                if summary and len(summary) > 50:
                    prompt = f"[Session compacted - Previous context summary:]\n{summary}\n\n[Continuing task:]\n{prompt}"
                send_message(chat_id, "🔄 Context preserved. Continuing...")

            response, questions, _, claude_sid, context_overflow = run_claude_streaming(
                prompt, chat_id, cwd=cwd, continue_session=True,
                session_id=session_id, session=session
            )
            _ws_broadcast_status(chat_id, "deepreview", phase, step)  # Re-assert after Claude exits

            if claude_sid:
                update_claude_session_id(chat_id, session, claude_sid)
                session = get_session_by_id(chat_id, session_id) or session

            if context_overflow:
                send_message(chat_id, "⚠️ Context overflow — compacting...")
                update_claude_session_id(chat_id, session, None)
                reset_message_count(chat_id, session, "Claude")
                response, questions, _, claude_sid, _ = run_claude_streaming(
                    prompt, chat_id, cwd=cwd, continue_session=True,
                    session_id=session_id, session=session
                )
                _ws_broadcast_status(chat_id, "deepreview", phase, step)
                if claude_sid:
                    update_claude_session_id(chat_id, session, claude_sid)
                    session = get_session_by_id(chat_id, session_id) or session

            if questions:
                auto_answer = handle_justdoit_questions(questions)
                send_message(chat_id, f"🤖 *Auto-answering:* _{auto_answer[:100]}_")
                response2, _, _, claude_sid2, _ = run_claude_streaming(
                    auto_answer, chat_id, cwd=cwd, continue_session=True,
                    session_id=session_id, session=session
                )
                if claude_sid2:
                    update_claude_session_id(chat_id, session, claude_sid2)
                    session = get_session_by_id(chat_id, session_id) or session
                if response2:
                    response = (response or "") + "\n\n[After auto-answer:]\n" + response2

            clean_response = response.split("———")[0].strip() if response else "No output"
            review_history += f"\n\nClaude review+fix (iteration {iteration_12}):\n{clean_response[:2000]}"
            all_review_history += f"\n\n=== Claude review+fix (iteration {iteration_12}) ===\n{clean_response[:2000]}"

            print(f"{log_prefix} Step {step}: Claude review+fix iteration {iteration_12}, response length: {len(clean_response)}", flush=True)

            time.sleep(2)

            # Check cancellation/pause before phase 2
            if not _check_pause(deepreview_active, chat_key, chat_id, "deepreview", phase, step):
                if not notified_exit:
                    send_message(chat_id, f"⚠️ *Deep review cancelled* at step {step}.")
                    notified_exit = True
                break

            # --- PHASE 2: Codex cross-reviews Claude's work ---
            phase = "codex_reviews_claude"
            deepreview_active[chat_key]["phase"] = phase
            step += 1
            deepreview_active[chat_key]["step"] = step
            _ws_broadcast_status(chat_id, "deepreview", phase, step)

            send_message(chat_id, f"🧠 *Step {step}* — Phase 2 (iteration {iteration_12}): Codex cross-reviewing...")

            # Retry loop for Codex (handles timeouts/errors without re-running Claude)
            codex_retry = 0
            next_prompt = None
            is_clean = False
            reasoning = ""
            codex_abort = False
            while codex_retry < 3:
                next_prompt, is_clean, reasoning = run_codex_deepreview(
                    clean_response, review_history, step, cwd, phase="codex_reviews_claude"
                )
                print(f"{log_prefix} Step {step}: Codex cross-review iteration {iteration_12} (try {codex_retry + 1}) — clean: {is_clean}, reasoning: {reasoning[:200]}", flush=True)

                # Handle quota
                if reasoning and reasoning.startswith("QUOTA:"):
                    parts = reasoning[6:].strip().split(" ", 1)
                    try:
                        wait_min = max(1, int(parts[0]))
                    except (ValueError, IndexError):
                        wait_min = 60
                    details = parts[1] if len(parts) > 1 else ""
                    wait_secs = wait_min * 60
                    resume_time = (datetime.now() + timedelta(seconds=wait_secs)).strftime('%H:%M')
                    send_message(chat_id, f"⏳ *Rate limited.* _{details[:200]}_\n_Waiting ~{wait_min}min... (resume ~{resume_time})_")
                    if not _deepreview_wait(chat_key, wait_secs):
                        send_message(chat_id, f"⚠️ *Deep review cancelled* during wait.")
                        notified_exit = True
                        codex_abort = True
                        break
                    send_message(chat_id, "🔄 *Resuming...*")
                    continue  # Retry Codex directly after quota wait

                if is_clean or next_prompt is not None:
                    break  # Got a real result

                # Codex failed (timeout, error, no output)
                codex_retry += 1
                send_message(chat_id, f"⚠️ Codex failed ({reasoning[:100]}). Retry {codex_retry}/3...")
                time.sleep(5)

            if codex_abort:
                break

            if is_clean:
                send_message(chat_id, f"✅ Codex is satisfied with Claude's work after {iteration_12} iterations.")
                codex_satisfied = True
                break

            if next_prompt is None:
                send_message(chat_id, "⚠️ Codex failed 3 times. Moving to Codex's turn.")
                break

            all_review_history += f"\n\n=== Codex cross-review (iteration {iteration_12}) ===\n{next_prompt[:3000]}"
            review_history += f"\n\n=== Codex cross-review (iteration {iteration_12}) ===\n{next_prompt[:3000]}"

            send_message(chat_id, f"📋 *Codex feedback for Claude:*\n\n{next_prompt[:3500]}")
            send_message(chat_id, "🔄 Sending Claude back to fix...")

            time.sleep(2)

        if not codex_satisfied and not notified_exit:
            send_message(chat_id, f"⚠️ Hit max Phase 1↔2 iterations ({max_iterations_12}). Moving to Codex's turn.")

        # Check cancellation before mega-loop 2
        if not deepreview_active.get(chat_key, {}).get("active", False):
            if not notified_exit:
                send_message(chat_id, f"⚠️ *Deep review cancelled* at step {step}.")
            return

        # ============================================================
        # MEGA-LOOP 2: Phases 3+4 (up to 20 bounces)
        # Phase 3: Codex reviews+fixes (single pass)
        # Phase 4: Claude cross-reviews → if issues, back to Phase 3
        # ============================================================
        max_iterations_34 = 20
        iteration_34 = 0
        claude_satisfied = False
        codex_fail_streak = 0

        while iteration_34 < max_iterations_34 and not claude_satisfied:
            iteration_34 += 1

            # Check cancellation/pause
            if not _check_pause(deepreview_active, chat_key, chat_id, "deepreview", phase, step):
                if not notified_exit:
                    send_message(chat_id, f"⚠️ *Deep review cancelled* at step {step}.")
                    notified_exit = True
                break

            # --- PHASE 3: Codex reviews and fixes (single pass) ---
            phase = "codex_self_review"
            deepreview_active[chat_key]["phase"] = phase
            step += 1
            deepreview_active[chat_key]["step"] = step
            _ws_broadcast_status(chat_id, "deepreview", phase, step)

            # On iteration > 1, pass Claude's feedback from Phase 4
            is_followup = iteration_34 > 1
            claude_feedback_for_codex = None
            if is_followup and "=== Claude cross-review of Codex" in all_review_history:
                claude_feedback_for_codex = all_review_history.split("=== Claude cross-review of Codex")[-1][:3000]

            if iteration_34 == 1:
                send_message(chat_id, f"🔨 *Step {step}* — Phase 3: Codex reviewing & fixing...")
            else:
                send_message(chat_id, f"🔨 *Step {step}* — Phase 3 (iteration {iteration_34}): Codex fixing Claude's findings...")

            codex_output, is_clean, reasoning = run_codex_deepreview_fix(
                all_review_history, step, cwd,
                is_followup=is_followup,
                claude_feedback=claude_feedback_for_codex
            )

            print(f"{log_prefix} Step {step}: Codex review+fix iteration {iteration_34} — clean: {is_clean}, reasoning: {reasoning[:200]}", flush=True)

            # Handle quota
            if reasoning and reasoning.startswith("QUOTA:"):
                parts = reasoning[6:].strip().split(" ", 1)
                try:
                    wait_min = max(1, int(parts[0]))
                except (ValueError, IndexError):
                    wait_min = 60
                details = parts[1] if len(parts) > 1 else ""
                wait_secs = wait_min * 60
                resume_time = (datetime.now() + timedelta(seconds=wait_secs)).strftime('%H:%M')
                send_message(chat_id, f"⏳ *Rate limited.* _{details[:200]}_\n_Waiting ~{wait_min}min... (resume ~{resume_time})_")
                if not _deepreview_wait(chat_key, wait_secs):
                    send_message(chat_id, f"⚠️ *Deep review cancelled* during wait.")
                    notified_exit = True
                    break
                send_message(chat_id, "🔄 *Resuming...*")
                iteration_34 -= 1  # Retry
                continue

            if is_clean:
                send_message(chat_id, f"✅ Codex found no issues (iteration {iteration_34}).")

            if codex_output is None:
                codex_fail_streak += 1
                send_message(chat_id, f"⚠️ Codex failed ({reasoning[:100]}). Retry {codex_fail_streak}/3...")
                if codex_fail_streak >= 3:
                    send_message(chat_id, "⚠️ Codex failed 3 times. Moving to Claude cross-review.")
                else:
                    time.sleep(5)
                    iteration_34 -= 1  # Retry Phase 3 directly
                    continue
            else:
                codex_fail_streak = 0
                if not is_clean:
                    all_review_history += f"\n\n=== Codex review+fix (iteration {iteration_34}) ===\n{codex_output[:2000]}"
                    send_message(chat_id, f"🔨 *Codex review & fixes:*\n\n{codex_output[:3500]}")

            time.sleep(2)

            # Check cancellation/pause before phase 4
            if not _check_pause(deepreview_active, chat_key, chat_id, "deepreview", phase, step):
                if not notified_exit:
                    send_message(chat_id, f"⚠️ *Deep review cancelled* at step {step}.")
                    notified_exit = True
                break

            # --- PHASE 4: Claude cross-reviews Codex's work ---
            phase = "claude_reviews_codex"
            deepreview_active[chat_key]["phase"] = phase
            step += 1
            deepreview_active[chat_key]["step"] = step
            _ws_broadcast_status(chat_id, "deepreview", phase, step)

            send_message(chat_id, f"⚔️ *Step {step}* — Phase 4 (iteration {iteration_34}): Claude cross-reviewing Codex's work...")

            critique_prompt = f"""Another AI (Codex) just did a deep code review and made direct fixes to the codebase.

REVIEW HISTORY:
{all_review_history[-4000:]}

Your job is to cross-review Codex's work with fresh eyes:

1. Read the actual code files — did Codex's fixes actually improve things?
2. Did Codex introduce any regressions or new bugs?
3. Did Codex use bandaids/hacks instead of proper fixes?
4. Did Codex miss important issues that are still in the code?
5. Did Codex over-engineer or add unnecessary complexity?
6. Are there design/architecture concerns Codex overlooked?

If you find problems, fix them immediately and report what you changed.
If Codex's work is solid and the code is clean, say exactly: ALL_CLEAN"""

            # Handle compaction
            needs_compaction = increment_message_count(chat_id, session, "Claude")
            if needs_compaction:
                send_message(chat_id, "📦 *Auto-compacting* session context...")
                try:
                    summary_response, _, _, _, _ = run_claude_streaming(
                        "Summarize this session for context continuity (max 500 words). Focus on files changed, issues found and fixed, and current state.",
                        chat_id, cwd=cwd, continue_session=True,
                        session_id=session_id, session=session
                    )
                    summary = summary_response.split("———")[0].strip() if summary_response else ""
                except Exception:
                    summary = ""
                if summary and len(summary) > 50:
                    save_session_summary(chat_id, session, summary)
                update_claude_session_id(chat_id, session, None)
                reset_message_count(chat_id, session, "Claude")
                if summary and len(summary) > 50:
                    critique_prompt = f"[Session compacted - Previous context summary:]\n{summary}\n\n[Continuing task:]\n{critique_prompt}"
                send_message(chat_id, "🔄 Context preserved. Continuing...")

            response, questions, _, claude_sid, context_overflow = run_claude_streaming(
                critique_prompt, chat_id, cwd=cwd, continue_session=True,
                session_id=session_id, session=session
            )
            _ws_broadcast_status(chat_id, "deepreview", phase, step)  # Re-assert after Claude exits
            if claude_sid:
                update_claude_session_id(chat_id, session, claude_sid)
                session = get_session_by_id(chat_id, session_id) or session
            if context_overflow:
                update_claude_session_id(chat_id, session, None)
                reset_message_count(chat_id, session, "Claude")
                response, _, _, claude_sid, _ = run_claude_streaming(
                    critique_prompt, chat_id, cwd=cwd, continue_session=True,
                    session_id=session_id, session=session
                )
                _ws_broadcast_status(chat_id, "deepreview", phase, step)
                if claude_sid:
                    update_claude_session_id(chat_id, session, claude_sid)
                    session = get_session_by_id(chat_id, session_id) or session
            if questions:
                auto_answer = handle_justdoit_questions(questions)
                response2, _, _, sid2, _ = run_claude_streaming(
                    auto_answer, chat_id, cwd=cwd, continue_session=True,
                    session_id=session_id, session=session
                )
                if sid2:
                    update_claude_session_id(chat_id, session, sid2)
                    session = get_session_by_id(chat_id, session_id) or session
                if response2:
                    response = (response or "") + "\n\n[After auto-answer:]\n" + response2

            clean_response = response.split("———")[0].strip() if response else "No output"
            all_review_history += f"\n\n=== Claude cross-review of Codex (iteration {iteration_34}) ===\n{clean_response[:2000]}"

            print(f"{log_prefix} Step {step}: Claude critique iteration {iteration_34}, response length: {len(clean_response)}", flush=True)

            if "ALL_CLEAN" in clean_response.upper():
                print(f"{log_prefix} Claude reports ALL_CLEAN on Codex's work after iteration {iteration_34}", flush=True)
                send_message(chat_id, f"✅ Claude is satisfied with Codex's work after {iteration_34} iterations.")
                claude_satisfied = True
                break

            # Claude found issues — loop back to Phase 3
            send_message(chat_id, f"📋 *Claude feedback for Codex:*\n\n{clean_response[:3500]}")
            send_message(chat_id, "🔄 Sending Codex back to fix...")

            time.sleep(2)

        if not claude_satisfied and not notified_exit:
            send_message(chat_id, f"⚠️ Hit max Phase 3↔4 iterations ({max_iterations_34}). Ending review.")

        if not notified_exit:
            if codex_satisfied and claude_satisfied:
                send_message(chat_id, f"""🔬 *Deep Review Complete!*

Finished in *{step}* steps across all phases.
Both Claude and Codex agree the code is clean.

_Session preserved. You can continue chatting._""")
            else:
                send_message(chat_id, f"""🔬 *Deep Review Finished*

Completed in *{step}* steps.
_Session preserved. You can continue chatting._""")

    except Exception as e:
        import traceback
        print(f"{log_prefix} EXCEPTION: {e}", flush=True)
        print(f"{log_prefix} Traceback:\n{traceback.format_exc()}", flush=True)
        try:
            send_message(chat_id, f"❌ *Deep review error:* {str(e)[:300]}")
        except Exception:
            pass

    finally:
        print(f"{log_prefix} Loop ended. Total steps: {step}", flush=True)
        try:
            state = deepreview_active.get(chat_key, {})
            if state.get("active", False) and not notified_exit:
                send_message(chat_id, f"⚠️ *Deep review stopped* at step {step}.\n_Session preserved._")
        except Exception:
            pass
        deepreview_active.pop(chat_key, None)
        save_active_tasks()
        _ws_broadcast_status(chat_id, "deepreview", "", 0, active=False)
        _ws_session_override.name = None


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
• `/deepreview` - Deep multi-phase code review
• `/status` - Show current session
• `/help` - Show this help

Send any message to chat with Claude!""")
        return True

    if cmd == "/schedule":
        if not args:
            send_message(chat_id, """*Schedule a task:*
`/schedule daily HH:MM | session | prompt`
`/schedule weekly DAY HH:MM | session | prompt`
`/schedule hourly | session | prompt`
`/schedule cron EXPR | session | prompt`
`/schedule once YYYY-MM-DD HH:MM | session | prompt`

Add `remind` before the spec to get a reminder instead of auto-execution:
`/schedule remind daily 09:00 | myproject | Check test results`""")
            return True

        # Parse: /schedule [remind] <spec> | <session> | <prompt>
        parts = args.split("|", 2)
        if len(parts) < 3:
            send_message(chat_id, "❌ Format: `/schedule <spec> | <session> | <prompt>`")
            return True

        spec_raw = parts[0].strip()
        session_name = parts[1].strip()
        prompt = parts[2].strip()

        if not session_name or not prompt:
            send_message(chat_id, "❌ Session name and prompt are required.")
            return True

        # Check session exists
        user_data = user_sessions.get(str(chat_id), {})
        found = any(s.get("name") == session_name for s in user_data.get("sessions", []))
        if not found:
            send_message(chat_id, f"❌ Session `{session_name}` not found.")
            return True

        # Parse mode
        mode = "justdoit"
        if spec_raw.lower().startswith("remind "):
            mode = "remind"
            spec_raw = spec_raw[7:].strip()

        # Parse schedule spec
        try:
            spec_lower = spec_raw.lower()
            if spec_lower.startswith("daily "):
                hm = spec_raw[6:].strip()
                h, m = map(int, hm.split(":"))
                cron_expr = f"{m} {h} * * *"
                schedule_type, run_at = "cron", None
            elif spec_lower.startswith("weekly "):
                rest = spec_raw[7:].strip().split()
                day_name = rest[0].lower()[:3]
                if day_name not in _DOW_NAMES:
                    send_message(chat_id, f"❌ Invalid day: `{rest[0]}`. Use Mon, Tue, Wed, etc.")
                    return True
                hm = rest[1] if len(rest) > 1 else "09:00"
                h, m = map(int, hm.split(":"))
                cron_expr = f"{m} {h} * * {day_name}"
                schedule_type, run_at = "cron", None
            elif spec_lower == "hourly":
                cron_expr = "0 * * * *"
                schedule_type, run_at = "cron", None
            elif spec_lower.startswith("cron "):
                cron_expr = spec_raw[5:].strip()
                schedule_type, run_at = "cron", None
            elif spec_lower.startswith("once "):
                run_at = spec_raw[5:].strip()
                schedule_type, cron_expr = "once", None
            else:
                send_message(chat_id, f"❌ Unknown schedule spec: `{spec_raw}`\nUse `daily`, `weekly`, `hourly`, `cron`, or `once`.")
                return True

            task_id, task = create_scheduled_task(
                chat_id, session_name, prompt, schedule_type,
                cron_expr=cron_expr, run_at=run_at, mode=mode,
            )
            next_dt = datetime.fromtimestamp(task["next_run"]).strftime("%Y-%m-%d %H:%M") if task.get("next_run") else "?"
            mode_label = "🤖 Auto-execute" if mode == "justdoit" else "🔔 Remind"
            send_message(chat_id, f"✅ *Scheduled task created*\nID: `{task_id}`\nSession: `{session_name}`\nMode: {mode_label}\nNext run: {next_dt}\n\nTask: _{prompt[:200]}_")
        except ValueError as e:
            send_message(chat_id, f"❌ {e}")
        return True

    if cmd == "/schedules":
        with _scheduled_tasks_lock:
            tasks = [(tid, t) for tid, t in scheduled_tasks.items()
                     if str(t.get("chat_id")) == str(chat_id)]

        if not tasks:
            send_message(chat_id, "No scheduled tasks. Use `/schedule` to create one.")
            return True

        lines = ["*Scheduled Tasks:*\n"]
        for tid, t in sorted(tasks, key=lambda x: x[1].get("next_run") or float("inf")):
            status = "✅" if t["enabled"] else "⏸"
            mode_icon = "🤖" if t["mode"] == "justdoit" else "🔔"
            if t["schedule_type"] == "cron":
                sched_desc = t.get("cron_expr", "?")
            else:
                sched_desc = f"once {t.get('run_at', '?')}"
            next_dt = datetime.fromtimestamp(t["next_run"]).strftime("%m/%d %H:%M") if t.get("next_run") else "—"
            lines.append(f"{status} {mode_icon} `{tid}`\n   {t['session_name']} • {sched_desc}\n   Next: {next_dt} • Runs: {t.get('run_count', 0)}\n   _{t['prompt'][:80]}_\n")

        send_message(chat_id, "\n".join(lines))
        return True

    if cmd == "/unschedule":
        if not args:
            send_message(chat_id, "Usage: `/unschedule <task_id>`")
            return True

        task_id = args.strip()
        with _scheduled_tasks_lock:
            task = scheduled_tasks.get(task_id)
            if not task or str(task.get("chat_id")) != str(chat_id):
                send_message(chat_id, f"❌ Task `{task_id}` not found.")
                return True
            del scheduled_tasks[task_id]
        save_scheduled_tasks()
        _ws_broadcast_schedule(chat_id, "deleted", task_id, task)
        send_message(chat_id, f"🗑 Scheduled task `{task_id}` deleted.")
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
• `/claude [task]` - Run Claude task (session persists per project)
• `/codex [task]` - Run Codex task (session persists per project)
• `/gemini [task]` - Run Gemini task (session persists per project)
  Uses configured Gemini model (default `gemini-3.1-pro-preview`), auto-resumes previous session

*Autonomous Mode:*
• `/justdoit [task]` - Start autonomous implementation
  Claude implements, Codex reviews, loops until done.
  Use without args to continue current plan.
  _Use /cancel to stop._
• `/deepreview` - Deep multi-phase code review
  Phases 1↔2: Claude fixes ↔ Codex reviews (loop until Codex satisfied)
  Phases 3↔4: Codex fixes ↔ Claude reviews (loop until Claude satisfied)
  _Use /cancel to stop._
• `/omni [task]` - Unified Engineering Team Task
  Architect (Claude) -> Execute (Gemini) -> Audit (Codex).
  Loops until the task is complete and signed off by Codex.
  _Use /cancel to stop._

*Scheduling:*
• `/schedule <spec> | <session> | <prompt>` - Schedule a task
  Specs: `daily HH:MM`, `weekly DAY HH:MM`, `hourly`, `cron EXPR`, `once YYYY-MM-DD HH:MM`
  Add `remind` before spec for reminder only
• `/schedules` - List all scheduled tasks
• `/unschedule <id>` - Delete a scheduled task

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
                _ws_broadcast(chat_id, "active_session", {"session": s["name"]})
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
            save_sessions(force=True)
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
                    save_sessions(force=True)
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

            default_cli = session.get("last_cli", "Claude")
            send_message(chat_id, f"""*Current Session:*
• Project: `{session['name']}`
• Directory: `{session['cwd']}`
• Default CLI: `{default_cli}`
• Status: {status}
• Created: {session['created_at'][:16]}""")
        else:
            send_message(chat_id, "No active session. Use `/new <project>` to start one.")
        return True

    if cmd == "/end":
        chat_key = str(chat_id)
        if chat_key in user_sessions:
            user_sessions[chat_key]["active"] = None
            save_sessions(force=True)
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

    if cmd == "/reload":
        global _reload_requested
        _reload_requested = True
        send_message(chat_id, "🔄 *Hot reload requested.* New code will be loaded on next poll cycle.")
        return True

    if cmd == "/cancel":
        session = get_active_session(chat_id)

        # Cancel justdoit or deepreview mode if active on the current session
        justdoit_was_active = False
        deepreview_was_active = False
        omni_was_active = False
        if session:
            session_id = get_session_id(session)
            jdi_key = f"{chat_id}:{session_id}"
            if justdoit_active.get(jdi_key, {}).get("active"):
                justdoit_active[jdi_key]["active"] = False
                justdoit_was_active = True
                _ws_broadcast_status(chat_id, "justdoit", "", 0, active=False)
            if deepreview_active.get(jdi_key, {}).get("active"):
                deepreview_active[jdi_key]["active"] = False
                deepreview_was_active = True
                _ws_broadcast_status(chat_id, "deepreview", "", 0, active=False)
            if omni_active.get(jdi_key, {}).get("active"):
                omni_active[jdi_key]["active"] = False
                omni_was_active = True
                _ws_broadcast_status(chat_id, "omni", "", 0, active=False)
            # Clear any queued user feedback
            user_feedback_queue.pop(jdi_key, None)

        if session:
            session_id = get_session_id(session)
            process = active_processes.get(session_id)
            if process:
                # Only mark as cancelled if there's an active process — otherwise the flag
                # lingers and falsely marks the NEXT run as cancelled
                cancelled_sessions.add(session_id)
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
                    _ws_broadcast(chat_id, "status", {"mode": "busy", "active": False})
                    if justdoit_was_active:
                        send_message(chat_id, f"⚠️ *JustDoIt cancelled* for `{session['name']}`.\n_Session preserved. You can continue manually._")
                    elif deepreview_was_active:
                        send_message(chat_id, f"⚠️ *Deep review cancelled* for `{session['name']}`.\n_Session preserved._")
                    elif omni_was_active:
                        send_message(chat_id, f"⚠️ *Omni cancelled* for `{session['name']}`.\n_Session preserved._")
                    else:
                        send_message(chat_id, f"⚠️ Cancelled operation for `{session['name']}`.")
                except ProcessLookupError:
                    # Process already exited
                    active_processes.pop(session_id, None)
                    _ws_broadcast(chat_id, "status", {"mode": "busy", "active": False})
                    send_message(chat_id, f"⚠️ Cancelled (process already finished).")
                except Exception as e:
                    print(f"Cancel error: {e}", flush=True)
                    # Fallback: try regular kill
                    try:
                        process.kill()
                        active_processes.pop(session_id, None)
                        _ws_broadcast(chat_id, "status", {"mode": "busy", "active": False})
                    except Exception:
                        pass
                    send_message(chat_id, f"⚠️ Cancelled operation for `{session['name']}`.")
            else:
                if justdoit_was_active:
                    send_message(chat_id, f"⚠️ *JustDoIt cancelled* for `{session['name']}`.\n_No active subprocess was running._")
                elif deepreview_was_active:
                    send_message(chat_id, f"⚠️ *Deep review cancelled* for `{session['name']}`.\n_No active subprocess was running._")
                elif omni_was_active:
                    send_message(chat_id, f"⚠️ *Omni cancelled* for `{session['name']}`.\n_No active subprocess was running._")
                else:
                    send_message(chat_id, f"No active task for session `{session['name']}`.")
        else:
            if justdoit_was_active:
                send_message(chat_id, "⚠️ JustDoIt cancelled.")
            elif deepreview_was_active:
                send_message(chat_id, "⚠️ Deep review cancelled.")
            elif omni_was_active:
                send_message(chat_id, "⚠️ Omni cancelled.")
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

    if cmd in ("/omni", "/o"):
        session = get_active_session(chat_id)
        if not session:
            send_message(chat_id, "No active session. Use `/new <project>` first.")
            return True
        session_id = get_session_id(session)
        omni_key = f"{chat_id}:{session_id}"
        if omni_active.get(omni_key, {}).get("active"):
            send_message(chat_id, "⚠️ Omni is already running on this session. Use `/cancel` to stop it first.")
            return True
        if justdoit_active.get(omni_key, {}).get("active"):
            send_message(chat_id, "⚠️ JustDoIt is running on this session. Use `/cancel` to stop it first.")
            return True
        if deepreview_active.get(omni_key, {}).get("active"):
            send_message(chat_id, "⚠️ Deep review is running on this session. Use `/cancel` to stop it first.")
            return True
        if session_id in active_processes:
            send_message(chat_id, "⚠️ Session is busy. Wait for it to finish or `/cancel` first.")
            return True

        task = args.strip() if args else "Review the project and identify improvements"
        
        # Run Omni in a background thread
        thread = threading.Thread(
            target=run_omni_loop,
            args=(chat_id, task, session),
            daemon=True
        )
        thread.start()
        return True

    if cmd in ("/claude", "/c", "/cl"):
        session = get_active_session(chat_id)
        if not session:
            send_message(chat_id, "No active session. Use `/new <project>` first.")
            return True

        task = args.strip() if args else "Review the code and identify any issues, bugs, or improvements"
        sid = get_session_id(session)
        lock = get_session_lock(sid)
        with lock:
            if sid in active_processes:
                if sid not in message_queue:
                    message_queue[sid] = []
                message_queue[sid].append(task)
                queue_pos = len(message_queue[sid])
                send_message(chat_id, f"📋 _Message queued (#{queue_pos}) for `{session.get('name', 'default')}`. Will process after current task._")
                return True
            active_processes[sid] = None
            _ws_broadcast(chat_id, "status", {"mode": "busy", "active": True})
        session["last_cli"] = "Claude"
        run_claude_in_thread(chat_id, task, session=session)
        return True

    if cmd == "/codex":
        session = get_active_session(chat_id)
        if not session:
            send_message(chat_id, "No active session. Use `/new <project>` first.")
            return True

        task = args.strip() if args else "Review the code and identify any issues, bugs, or improvements"
        sid = get_session_id(session)
        lock = get_session_lock(sid)
        with lock:
            if sid in active_processes:
                if sid not in message_queue:
                    message_queue[sid] = []
                message_queue[sid].append(task)
                queue_pos = len(message_queue[sid])
                send_message(chat_id, f"📋 _Message queued (#{queue_pos}) for `{session.get('name', 'default')}`. Will process after current task._")
                return True
            active_processes[sid] = None
            _ws_broadcast(chat_id, "status", {"mode": "busy", "active": True})
        session["last_cli"] = "Codex"
        run_codex_task(chat_id, task, session["cwd"], session=session)
        return True

    if cmd in ("/gemini", "/gem", "/g"):
        session = get_active_session(chat_id)
        if not session:
            send_message(chat_id, "No active session. Use `/new <project>` first.")
            return True

        task = args.strip() if args else "Review the code and identify any issues, bugs, or improvements"
        sid = get_session_id(session)
        lock = get_session_lock(sid)
        with lock:
            if sid in active_processes:
                if sid not in message_queue:
                    message_queue[sid] = []
                message_queue[sid].append(task)
                queue_pos = len(message_queue[sid])
                send_message(chat_id, f"📋 _Message queued (#{queue_pos}) for `{session.get('name', 'default')}`. Will process after current task._")
                return True
            active_processes[sid] = None
            _ws_broadcast(chat_id, "status", {"mode": "busy", "active": True})
        session["last_cli"] = "Gemini"
        run_gemini_task(chat_id, task, session["cwd"], session=session)
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

    if cmd == "/deepreview":
        session = get_active_session(chat_id)
        if not session:
            send_message(chat_id, "No active session. Use `/new <project>` first.")
            return True

        session_id = get_session_id(session)
        dr_key = f"{chat_id}:{session_id}"

        if deepreview_active.get(dr_key, {}).get("active"):
            send_message(chat_id, "⚠️ Deep review is already running on this session. Use `/cancel` to stop it first.")
            return True

        jdi_key = dr_key
        if justdoit_active.get(jdi_key, {}).get("active"):
            send_message(chat_id, "⚠️ JustDoIt is running on this session. Use `/cancel` to stop it first.")
            return True

        if session_id in active_processes:
            send_message(chat_id, "⚠️ Session is busy. Wait for it to finish or `/cancel` first.")
            return True

        thread = threading.Thread(
            target=run_deepreview_loop,
            args=(chat_id, session),
            daemon=True
        )
        thread.start()
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
                _ws_broadcast(chat_id, "active_session", {"session": s["name"]})
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
                save_sessions(force=True)
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
                save_sessions(force=True)
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
                                _ws_broadcast(chat_id, "status", {"mode": "busy", "active": True})
                            run_claude_in_thread(chat_id, answer_text, session)
                    return
        except (ValueError, IndexError):
            pass

    pending_questions.pop(chat_key, None)


def run_claude_in_thread(chat_id, text, session=None):
    """Run Claude in a background thread."""
    chat_key = str(chat_id)
    session_id = get_session_id(session) if session else None

    def claude_task():
        try:
            if session:
                # Check if proactive compaction is needed BEFORE sending to Claude
                needs_compaction = increment_message_count(chat_id, session, "Claude")

                if needs_compaction:
                    perform_proactive_compaction(chat_id, session, "Claude")

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
                    except Exception:
                        summary = ""

                    # Persist summary before clearing session (survives crashes)
                    if summary and len(summary) > 50:
                        save_session_summary(chat_id, session, summary)

                    # Reset the session
                    update_claude_session_id(chat_id, session, None)
                    reset_message_count(chat_id, session, "Claude")

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
                _ws_broadcast(chat_id, "status", {"mode": "busy", "active": False})

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
            _ws_broadcast(chat_id, "status", {"mode": "busy", "active": True})
        else:
            return

    # Dispatch to the appropriate CLI based on session's last_cli (sticky routing)
    last_cli = session.get("last_cli", "Claude")
    if last_cli == "Codex":
        run_codex_task(chat_id, queued_text, session["cwd"], session=session)
    elif last_cli == "Gemini":
        run_gemini_task(chat_id, queued_text, session["cwd"], session=session)
    else:
        run_claude_in_thread(chat_id, queued_text, session)


def handle_message(chat_id, text):
    """Handle a regular message."""
    chat_key = str(chat_id)

    # Collect user feedback during justdoit/omni — queued for next audit/review step
    session = get_active_session(chat_id)
    if session:
        jdi_key = f"{chat_id}:{get_session_id(session)}"
        jdi_state = justdoit_active.get(jdi_key, {})
        omni_state = omni_active.get(jdi_key, {})
        if jdi_state.get("active") or omni_state.get("active"):
            mode = "JustDoIt" if jdi_state.get("active") else "Omni"
            if jdi_key not in user_feedback_queue:
                user_feedback_queue[jdi_key] = []
            user_feedback_queue[jdi_key].append(text)
            n = len(user_feedback_queue[jdi_key])
            send_message(chat_id,
                f"📝 *Noted* (feedback #{n}) — will include in next {mode} review step.\n"
                f"_Use /cancel to stop {mode}._")
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
                    _ws_broadcast(chat_id, "status", {"mode": "busy", "active": True})
                run_claude_in_thread(chat_id, answer_text, session)
        return

    # Get active session
    session = get_active_session(chat_id)
    session_id = get_session_id(session) if session else str(chat_id)
    print(f"[handle_message] session={session.get('name') if session else None}, last_cli={session.get('last_cli') if session else None}, id={id(session) if session else None}", flush=True)

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
            # Send notification outside the lock to avoid blocking other threads on slow TG API
            threading.Thread(target=send_message, args=(chat_id,
                f"📋 _Message queued (#{queue_pos}) for session `{session_name}`. Will process after current task._"),
                daemon=True).start()
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
        _ws_broadcast(chat_id, "status", {"mode": "busy", "active": True})

    # Dispatch to the appropriate CLI runner based on session state
    last_cli = session.get("last_cli", "Claude") if session else "Claude"
    print(f"[DISPATCH] last_cli={last_cli}, session_name={session.get('name') if session else None}, id={id(session)}", flush=True)

    if last_cli == "Codex":
        run_codex_task(chat_id, text, session["cwd"], session=session)
    elif last_cli == "Gemini":
        run_gemini_task(chat_id, text, session["cwd"], session=session)
    else:
        # Default to Claude
        run_claude_in_thread(chat_id, text, session)


def _reinit_api_refs():
    """Re-bind api.py function references after hot reload."""
    global _api_module
    if _api_module:
        _api_module.init_refs(
            handle_command=handle_command,
            handle_message=handle_message,
            handle_callback_query=handle_callback_query,
            is_allowed=is_allowed,
            get_active_session=get_active_session,
            get_session_id=get_session_id,
            user_sessions=user_sessions,
            active_processes=active_processes,
            justdoit_active=justdoit_active,
            omni_active=omni_active,
            deepreview_active=deepreview_active,
            send_message=send_message,
            send_message_no_ws=send_message_no_ws,
            default_chat_id=int(ALLOWED_CHAT_IDS[0]) if ALLOWED_CHAT_IDS and ALLOWED_CHAT_IDS[0] else None,
            cancelled_sessions=cancelled_sessions,
            ws_broadcast_status=_ws_broadcast_status,
            save_active_tasks=save_active_tasks,
            user_feedback_queue=user_feedback_queue,
            get_active_sessions_data=get_active_sessions_data,
            scheduled_tasks=scheduled_tasks,
            scheduled_tasks_lock=_scheduled_tasks_lock,
            save_scheduled_tasks=save_scheduled_tasks,
            create_scheduled_task=create_scheduled_task,
            trigger_scheduled_task=_trigger_scheduled_task,
            next_cron_run_fn=_next_cron_run,
            ws_broadcast_schedule=_ws_broadcast_schedule,
        )
        print("[Hot-reload] API refs re-bound.", flush=True)
    _start_scheduler()  # Restart scheduler with new generation


# Flag checked by loader.py to trigger hot reload from /reload command
_reload_requested = False


def startup():
    """Initialize the bot: load state, register commands, start API server.

    Called once on first boot. The polling loop lives in loader.py.
    """
    load_sessions()
    load_scheduled_tasks()
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
            {"command": "omni", "description": "Unified Engineering Task"},
            {"command": "claude", "description": "Run Claude task"},
            {"command": "codex", "description": "Run Codex task"},
            {"command": "gemini", "description": "Run Gemini task"},
            {"command": "schedule", "description": "Schedule a task"},
            {"command": "schedules", "description": "List scheduled tasks"},
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
            # Flush any debounced session saves
            try:
                _flush_sessions_if_dirty()
            except Exception:
                pass
            time.sleep(30)

    threading.Thread(target=memory_monitor, daemon=True).start()

    # Start HTTP API + WebSocket server on Tailscale interface
    global _api_module
    try:
        import api as api_server
        api_server.init_refs(
            handle_command=handle_command,
            handle_message=handle_message,
            handle_callback_query=handle_callback_query,
            is_allowed=is_allowed,
            get_active_session=get_active_session,
            get_session_id=get_session_id,
            user_sessions=user_sessions,
            active_processes=active_processes,
            justdoit_active=justdoit_active,
            omni_active=omni_active,
            deepreview_active=deepreview_active,
            send_message=send_message,
            send_message_no_ws=send_message_no_ws,
            default_chat_id=int(ALLOWED_CHAT_IDS[0]) if ALLOWED_CHAT_IDS and ALLOWED_CHAT_IDS[0] else None,
            cancelled_sessions=cancelled_sessions,
            ws_broadcast_status=_ws_broadcast_status,
            save_active_tasks=save_active_tasks,
            user_feedback_queue=user_feedback_queue,
            get_active_sessions_data=get_active_sessions_data,
            scheduled_tasks=scheduled_tasks,
            scheduled_tasks_lock=_scheduled_tasks_lock,
            save_scheduled_tasks=save_scheduled_tasks,
            create_scheduled_task=create_scheduled_task,
            trigger_scheduled_task=_trigger_scheduled_task,
            next_cron_run_fn=_next_cron_run,
            ws_broadcast_schedule=_ws_broadcast_schedule,
        )
        api_host = os.environ.get("API_HOST", "100.118.238.103")
        api_port = int(os.environ.get("API_PORT", "8642"))
        api_server.start(api_host, api_port)
        _api_module = api_server  # Enable WS broadcast from send_message/edit_message
    except Exception as e:
        print(f"API server failed to start: {e}", flush=True)
        import traceback
        traceback.print_exc()

    # Start scheduled task checker
    _start_scheduler()



# Legacy entry point — use loader.py for hot-reload support
if __name__ == "__main__":
    startup()
    print("WARNING: Running bot.py directly. Use loader.py for hot-reload support.", flush=True)

    signal.signal(signal.SIGTERM, lambda s, f: (save_sessions(force=True), os._exit(0)))
    signal.signal(signal.SIGINT, lambda s, f: (save_sessions(force=True), os._exit(0)))

    while True:
        updates = get_updates(last_update_id + 1)
        for update in updates:
            last_update_id = update["update_id"]
            try:
                if "callback_query" in update:
                    cb = update["callback_query"]
                    if is_allowed(cb["message"]["chat"]["id"]):
                        handle_callback_query(cb)
                    continue
                message = update.get("message", {})
                chat_id = message.get("chat", {}).get("id")
                if not chat_id or not is_allowed(chat_id):
                    continue
                text = message.get("text", "") or message.get("caption", "")
                if message.get("photo"):
                    photo = message["photo"][-1]
                    local_path = download_telegram_file(photo["file_id"], "image.jpg")
                    if local_path:
                        handle_message(chat_id, f"[User uploaded an image: {local_path}]\n\n{text or 'Please analyze this image.'}")
                    continue
                if message.get("document"):
                    doc = message["document"]
                    if doc.get("file_size", 0) > 50 * 1024 * 1024:
                        continue
                    local_path = download_telegram_file(doc["file_id"], doc.get("file_name", "file"))
                    if local_path:
                        handle_message(chat_id, f"[User uploaded a file: {local_path}]\n\n{text or 'Please analyze this file.'}")
                    continue
                if not text:
                    continue
                if text.startswith("/") and handle_command(chat_id, text):
                    continue
                handle_message(chat_id, text)
            except Exception as e:
                print(f"Error processing update: {e}", flush=True)
        time.sleep(1)
