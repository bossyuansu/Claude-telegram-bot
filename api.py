"""
HTTP API server for Claude Telegram Bot.
Runs alongside the Telegram polling loop in the same process,
sharing all in-memory state. Listens on the Tailscale IP.

WebSocket endpoint at /ws streams all bot messages in real time.
"""
import asyncio
import contextlib
import json
import os
import sys
import threading
import time
import uuid

from fastapi import FastAPI, HTTPException, Depends, Header, WebSocket, WebSocketDisconnect, Query, Request
from pydantic import BaseModel, ConfigDict, field_validator
from typing import Optional

app = FastAPI(title="Claude Bot API", docs_url="/docs")

# Module-level refs (populated by init_refs from bot.py)
_handle_command = None
_handle_command_for_session = None
_handle_message = None
_handle_callback_query = None
_is_allowed = None
_get_active_session = None
_get_session_id = None
_user_sessions = None
_active_processes = None
_justdoit_active = None
_goal_state = None
_omni_active = None
_deepreview_active = None
_ralph_active = None
_send_message = None
_send_message_no_ws = None
_cancelled_sessions = None
_ws_broadcast_status = None
_save_active_tasks = None
_user_feedback_queue = None
_get_active_sessions_data = None
_scheduled_tasks = None
_scheduled_tasks_lock = None
_save_scheduled_tasks = None
_create_scheduled_task = None
_trigger_scheduled_task = None
_next_cron_run_fn = None
_ws_broadcast_schedule = None
_cron_bg_sessions = {}
_message_queue = {}
_save_sessions = None
_goal_active = {}
_goal_lock = None
_load_goal = None
_save_goal = None
_load_goal_index = None
_create_goal = None
_list_goals = None
_delete_goal = None
_replan_goal = None
_decompose_goal = None
_run_goal_loop = None
_ws_broadcast_goal = None
_schedule_goal_checkin = None
_cancel_goal_checkin = None
_get_session_busy_reason = None
_reserve_goal_session = None
_release_goal_session = None
_cancel_goal_session = None

_MESSAGE_REQUEST_TTL_SECONDS = 600
_message_request_lock = threading.Lock()
_message_requests_seen = {}

API_SECRET = ""
_default_chat_id = None

# --- WebSocket client registry ---
_ws_clients: set[WebSocket] = set()
_ws_lock = threading.Lock()

# --- WS message buffer with sequence numbers ---
_ws_seq = 0  # Monotonic sequence counter
_ws_buffer: list[tuple[int, str]] = []  # (seq, JSON payload)
_WS_BUFFER_MAX = 1500  # replay window; transient stream chunks no longer consume it, so this
                       # now holds ~1500 meaningful events (messages/status/goal/start/done)
_server_id = str(uuid.uuid4())[:8]  # Unique ID per server boot

# In-progress stream snapshots: message_id -> {"chat_id","session","text"}. Transient append
# chunks aren't buffered (they'd flood the replay window), so a client that drops mid-stream — e.g.
# a phone on a flaky Tailscale DERP relay — loses every append in the gap and reconnects with
# nothing to replay, freezing the live view until the far-off 'done'. We keep the running text per
# active stream (bounded: one entry per stream, popped on 'done') and replay it on reconnect so the
# app always catches up to the current in-progress content.
_active_streams: dict[int, dict] = {}
_ACTIVE_STREAM_TEXT_CAP = 200_000  # guard against unbounded growth on a runaway stream


def _with_replay_flag(payload: str, is_replay: bool) -> str:
    """Return payload JSON with explicit replay flag for client-side UX decisions."""
    try:
        obj = json.loads(payload)
        obj["is_replay"] = bool(is_replay)
        return json.dumps(obj)
    except Exception:
        return payload


_TRANSIENT_STREAM_OPS = {"append", "tool", "skip"}


def _is_transient_stream(event_type: str, data: dict) -> bool:
    """Live-view-only stream chunks: delivered to connected clients but never given a monotonic
    seq or buffered. Keeps the replay buffer small and contiguous so high-volume token streaming
    (especially autonomous goal execution) can't blow past the window and gap-lock reconnecting
    clients. Final content still arrives via the buffered 'done'/message events.
    """
    return event_type == "stream" and str(data.get("op", "")).lower() in _TRANSIENT_STREAM_OPS


def _should_buffer_event(event_type: str, data: dict, has_clients: bool) -> bool:
    """Decide whether this (non-transient) event should be retained in the replay buffer.

    Transient stream chunks are handled separately (see _is_transient_stream); everything else
    — including stream 'start'/'done' continuity markers, messages, status, and goal events —
    is buffered for replay on reconnect.
    """
    return True


def init_refs(**kwargs):
    """Receive references to bot.py functions and shared dicts."""
    global _handle_command, _handle_command_for_session, _handle_message, _handle_callback_query
    global _is_allowed, _get_active_session, _get_session_id
    global _user_sessions, _active_processes
    global _justdoit_active, _goal_state, _omni_active, _deepreview_active, _ralph_active
    global _send_message, _send_message_no_ws
    global _cancelled_sessions, _ws_broadcast_status, _save_active_tasks, _user_feedback_queue, _get_active_sessions_data
    global _scheduled_tasks, _scheduled_tasks_lock, _save_scheduled_tasks, _create_scheduled_task, _trigger_scheduled_task, _next_cron_run_fn, _ws_broadcast_schedule
    global _cron_bg_sessions, _message_queue, _save_sessions
    global _goal_active, _goal_lock, _load_goal, _save_goal, _load_goal_index, _create_goal, _list_goals
    global _delete_goal, _replan_goal, _decompose_goal, _run_goal_loop
    global _ws_broadcast_goal, _schedule_goal_checkin, _cancel_goal_checkin
    global _get_session_busy_reason, _reserve_goal_session, _release_goal_session, _cancel_goal_session
    global API_SECRET, _default_chat_id

    _handle_command = kwargs["handle_command"]
    _handle_command_for_session = kwargs.get("handle_command_for_session")
    _handle_message = kwargs["handle_message"]
    _handle_callback_query = kwargs["handle_callback_query"]
    _is_allowed = kwargs["is_allowed"]
    _get_active_session = kwargs["get_active_session"]
    _get_session_id = kwargs["get_session_id"]
    _user_sessions = kwargs["user_sessions"]
    _active_processes = kwargs["active_processes"]
    _justdoit_active = kwargs["justdoit_active"]
    _goal_state = kwargs.get("goal_state", {})
    _omni_active = kwargs["omni_active"]
    _deepreview_active = kwargs["deepreview_active"]
    _ralph_active = kwargs.get("ralph_active", {})
    _send_message = kwargs.get("send_message")
    _send_message_no_ws = kwargs.get("send_message_no_ws")
    _cancelled_sessions = kwargs.get("cancelled_sessions")
    _ws_broadcast_status = kwargs.get("ws_broadcast_status")
    _save_active_tasks = kwargs.get("save_active_tasks")
    _user_feedback_queue = kwargs.get("user_feedback_queue")
    _get_active_sessions_data = kwargs.get("get_active_sessions_data")
    _scheduled_tasks = kwargs.get("scheduled_tasks")
    _scheduled_tasks_lock = kwargs.get("scheduled_tasks_lock")
    _save_scheduled_tasks = kwargs.get("save_scheduled_tasks")
    _create_scheduled_task = kwargs.get("create_scheduled_task")
    _trigger_scheduled_task = kwargs.get("trigger_scheduled_task")
    _next_cron_run_fn = kwargs.get("next_cron_run_fn")
    _ws_broadcast_schedule = kwargs.get("ws_broadcast_schedule")
    _cron_bg_sessions = kwargs.get("cron_bg_sessions", {})
    _message_queue = kwargs.get("message_queue", {})
    _save_sessions = kwargs.get("save_sessions")
    _goal_active = kwargs.get("goal_active", {})
    _goal_lock = kwargs.get("goal_lock")
    _load_goal = kwargs.get("load_goal")
    _save_goal = kwargs.get("save_goal")
    _load_goal_index = kwargs.get("load_goal_index")
    _create_goal = kwargs.get("create_goal")
    _list_goals = kwargs.get("list_goals")
    _delete_goal = kwargs.get("delete_goal")
    _replan_goal = kwargs.get("replan_goal")
    _decompose_goal = kwargs.get("decompose_goal")
    _run_goal_loop = kwargs.get("run_goal_loop")
    _ws_broadcast_goal = kwargs.get("ws_broadcast_goal")
    _schedule_goal_checkin = kwargs.get("schedule_goal_checkin")
    _cancel_goal_checkin = kwargs.get("cancel_goal_checkin")
    _get_session_busy_reason = kwargs.get("get_session_busy_reason")
    _reserve_goal_session = kwargs.get("reserve_goal_session")
    _release_goal_session = kwargs.get("release_goal_session")
    _cancel_goal_session = kwargs.get("cancel_goal_session")
    API_SECRET = os.environ.get("API_SECRET", "")
    _default_chat_id = kwargs.get("default_chat_id")


# --- Auth ---

def verify_auth(authorization: str = Header(None)):
    if not API_SECRET:
        return
    if not authorization or authorization != f"Bearer {API_SECRET}":
        raise HTTPException(status_code=401, detail="Unauthorized")


def _goal_rate_limit_resume_delay(goal):
    if not goal:
        return 0, None
    until = goal.get("rate_limited_until")
    if not until:
        return 0, None
    try:
        from datetime import datetime
        resume_at = datetime.fromisoformat(str(until))
        delay = int((resume_at - datetime.now()).total_seconds())
    except (TypeError, ValueError):
        return 0, None
    if delay <= 0:
        return 0, resume_at
    return delay, resume_at


def _goal_rate_limit_resume_detail(wait_seconds, resume_at):
    wait_min = max(1, (int(wait_seconds) + 59) // 60)
    resume_text = resume_at.isoformat(timespec="minutes") if resume_at else "provider reset"
    return f"Goal is rate-limited; retry resume in about {wait_min} minutes ({resume_text})"


def _goal_clear_expired_rate_limit(goal):
    wait_seconds, _ = _goal_rate_limit_resume_delay(goal)
    if wait_seconds > 0:
        return False
    changed = False
    for key in ("rate_limited_until", "rate_limit_wait_seconds", "rate_limit_reset_hint"):
        if key in goal:
            goal.pop(key, None)
            changed = True
    if goal.get("pause_reason") == "rate_limited":
        goal.pop("pause_reason", None)
        goal.pop("pause_details", None)
        changed = True
    return changed


# --- Models ---

class MessageRequest(BaseModel):
    chat_id: Optional[int] = None
    session: Optional[str] = None
    client_request_id: Optional[str] = None
    text: str

class CallbackRequest(BaseModel):
    chat_id: Optional[int] = None
    data: str
    message_id: int

class TaskActionRequest(BaseModel):
    chat_id: Optional[int] = None
    session: str

class ScheduleTaskRequest(BaseModel):
    chat_id: Optional[int] = None
    session_name: Optional[str] = None  # Resolved to cwd server-side
    prompt: str
    schedule_type: str  # "cron" | "once"
    cron_expr: Optional[str] = None
    run_at: Optional[str] = None
    cwd: Optional[str] = None  # Explicit cwd (takes priority over session_name)

class ScheduleTaskUpdate(BaseModel):
    enabled: Optional[bool] = None
    prompt: Optional[str] = None
    cron_expr: Optional[str] = None
    run_at: Optional[str] = None

class GoalCreateRequest(BaseModel):
    chat_id: Optional[int] = None
    session_name: Optional[str] = None
    description: str
    config: Optional[dict] = None

_VALID_EXECUTION_MODES = {
    "auto",
    "claude",
    "claude-only",
    "justdoit",
    "codex",
    "omni",
    "codex_reviewed",
    "codex-reviewed",
}
_VALID_MODELS = {"opus", "sonnet", "haiku"}

class GoalConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_iterations: Optional[int] = None
    max_consecutive_failures: Optional[int] = None
    execution_mode: Optional[str] = None
    auto_replan_threshold: Optional[int] = None
    max_total_time: Optional[int] = None
    model_call_timeout: Optional[int] = None
    execution_stale_timeout: Optional[int] = None
    rate_limit_max_wait: Optional[int] = None
    transient_max_retries: Optional[int] = None
    transient_retry_base_delay: Optional[int] = None
    verification_commands: Optional[list] = None
    pause_between_iterations: Optional[bool] = None
    model: Optional[str] = None
    checkin_schedule: Optional[str] = None

    @field_validator("max_iterations")
    @classmethod
    def validate_max_iterations(cls, v):
        if v is not None and (v < 1 or v > 1000):
            raise ValueError("max_iterations must be between 1 and 1000")
        return v

    @field_validator("max_consecutive_failures")
    @classmethod
    def validate_max_consecutive_failures(cls, v):
        if v is not None and (v < 1 or v > 100):
            raise ValueError("max_consecutive_failures must be between 1 and 100")
        return v

    @field_validator("auto_replan_threshold")
    @classmethod
    def validate_auto_replan_threshold(cls, v):
        if v is not None and (v < 1 or v > 50):
            raise ValueError("auto_replan_threshold must be between 1 and 50")
        return v

    @field_validator("max_total_time")
    @classmethod
    def validate_max_total_time(cls, v):
        if v is not None and (v < 60 or v > 86400):
            raise ValueError("max_total_time must be between 60 and 86400 seconds")
        return v

    @field_validator("model_call_timeout", "execution_stale_timeout")
    @classmethod
    def validate_goal_timeout(cls, v):
        if v is not None and (v < 30 or v > 7200):
            raise ValueError("timeouts must be between 30 and 7200 seconds")
        return v

    @field_validator("rate_limit_max_wait")
    @classmethod
    def validate_rate_limit_max_wait(cls, v):
        if v is not None and (v < 60 or v > 86400):
            raise ValueError("rate_limit_max_wait must be between 60 and 86400 seconds")
        return v

    @field_validator("transient_max_retries")
    @classmethod
    def validate_transient_max_retries(cls, v):
        if v is not None and (v < 0 or v > 20):
            raise ValueError("transient_max_retries must be between 0 and 20")
        return v

    @field_validator("transient_retry_base_delay")
    @classmethod
    def validate_transient_retry_base_delay(cls, v):
        if v is not None and (v < 1 or v > 3600):
            raise ValueError("transient_retry_base_delay must be between 1 and 3600 seconds")
        return v

    @field_validator("execution_mode")
    @classmethod
    def validate_execution_mode(cls, v):
        if v is not None and v not in _VALID_EXECUTION_MODES:
            raise ValueError(f"execution_mode must be one of: {', '.join(sorted(_VALID_EXECUTION_MODES))}")
        return v

    @field_validator("model")
    @classmethod
    def validate_model(cls, v):
        if v is not None and v not in _VALID_MODELS:
            raise ValueError(f"model must be one of: {', '.join(sorted(_VALID_MODELS))}")
        return v

    @field_validator("verification_commands")
    @classmethod
    def validate_verification_commands(cls, v):
        if v is not None:
            if not all(isinstance(item, str) for item in v):
                raise ValueError("verification_commands must be a list of strings")
        return v

    @field_validator("checkin_schedule")
    @classmethod
    def validate_checkin_schedule(cls, v):
        if v is not None:
            # Validate cron expression syntax (5 fields)
            parts = v.strip().split()
            if len(parts) != 5:
                raise ValueError("checkin_schedule must be a 5-field cron expression (e.g. '0 9 * * *')")
        return v


# --- Task helpers ---

_AUTONOMOUS_MODES = [
    ("goal", "_goal_state", "Goal"),
    ("justdoit", "_justdoit_active", "JustDoIt"),
    ("omni", "_omni_active", "Omni"),
    ("deepreview", "_deepreview_active", "Deep review"),
    ("ralph", "_ralph_active", "Ralph"),
]

def _get_mode_states():
    """Return [(state_dict, mode_key, label)] resolving current global refs."""
    return [
        (_goal_state or {}, "goal", "Goal"),
        (_justdoit_active or {}, "justdoit", "JustDoIt"),
        (_omni_active or {}, "omni", "Omni"),
        (_deepreview_active or {}, "deepreview", "Deep review"),
        (_ralph_active or {}, "ralph", "Ralph"),
    ]

def _resolve_task_session(req):
    """Auth + session lookup shared by cancel/pause/resume. Returns (chat_id, target_session, session_id, jdi_key)."""
    chat_id = req.chat_id or _default_chat_id
    if not chat_id or not _is_allowed(chat_id):
        raise HTTPException(status_code=403, detail="Chat ID not allowed")
    user_data = _user_sessions.get(str(chat_id), {})
    for s in user_data.get("sessions", []):
        if s.get("name") == req.session:
            session_id = _get_session_id(s)
            return chat_id, s, session_id, f"{chat_id}:{session_id}"
    raise HTTPException(status_code=404, detail="Session not found")


def _resolve_message_session(chat_id, session_name):
    """Resolve optional per-message session targeting for app filtered views."""
    if not session_name:
        return None
    user_data = _user_sessions.get(str(chat_id), {})
    for s in user_data.get("sessions", []):
        if s.get("name") == session_name:
            return s
    raise HTTPException(status_code=404, detail="Session not found")


def _is_duplicate_message_request(req: MessageRequest, chat_id: int) -> bool:
    """Return True when the app retries a request the server already accepted."""
    request_id = (req.client_request_id or "").strip()
    if not request_id:
        return False

    now = time.time()
    session_key = (req.session or "").strip()
    dedupe_key = f"{chat_id}:{session_key}:{request_id}"
    with _message_request_lock:
        expired = [
            key for key, seen_at in _message_requests_seen.items()
            if now - seen_at > _MESSAGE_REQUEST_TTL_SECONDS
        ]
        for key in expired:
            _message_requests_seen.pop(key, None)
        if dedupe_key in _message_requests_seen:
            return True
        _message_requests_seen[dedupe_key] = now
    return False


def _goal_id_for_key(goal_key):
    state = (_goal_state or {}).get(goal_key, {})
    return state.get("goal_id") or (_goal_active or {}).get(goal_key)


def _goal_is_running(goal, goal_key):
    if goal.get("status") not in ("planning", "active", "paused"):
        return False
    state = (_goal_state or {}).get(goal_key, {})
    return goal_key in (_goal_active or {}) and bool(state.get("active", True))


def _session_name_for_id(chat_id, session_id):
    user_data = (_user_sessions or {}).get(str(chat_id), {})
    for s in user_data.get("sessions", []):
        if _get_session_id and _get_session_id(s) == session_id:
            return s.get("name", "")
    return ""


# --- WebSocket broadcast (called from bot.py threads) ---

def broadcast_ws(chat_id, event_type, data):
    """Send a message to all connected WebSocket clients.
    Every broadcast gets a monotonic seq number for ordering guarantees.
    If no clients are connected, buffer for delivery on reconnect.
    """
    global _ws_seq

    # Creation time (epoch ms), stamped into the payload now — this is when the message/event was
    # created, not when the app receives it. Because the stamped payload is what gets buffered and
    # later replayed, replayed messages keep their ORIGINAL creation time instead of "now".
    _created_ms = int(time.time() * 1000)

    with _ws_lock:
        clients = list(_ws_clients)
        has_clients = bool(clients)

        # Maintain per-stream running-text snapshots for reconnect catch-up. Done before the
        # transient/no-clients early-return below so appends are captured even during a client gap.
        if event_type == "stream":
            op = str(data.get("op", "")).lower()
            mid = data.get("message_id")
            if mid is not None:
                if op == "start":
                    _active_streams[mid] = {
                        "chat_id": int(chat_id),
                        "session": data.get("session", ""),
                        "text": "",
                        "created_at": _created_ms,
                    }
                elif op == "append":
                    snap = _active_streams.get(mid)
                    if snap is not None and len(snap["text"]) < _ACTIVE_STREAM_TEXT_CAP:
                        snap["text"] += data.get("text", "")
                elif op == "done":
                    _active_streams.pop(mid, None)

        # High-volume live-view stream chunks (append/tool/skip) get NO monotonic seq and are
        # never buffered. Otherwise, streaming an autonomous goal's executor token-by-token fills
        # the 500-entry replay window in seconds; any brief app disconnect then lands past the
        # window, and the client gap-locks (waits forever for evicted seqs). Delivered live to
        # connected clients (seq=0 → client renders immediately); the final content still arrives
        # via the buffered 'done'/message events, so reconnects stay contiguous and recover.
        if _is_transient_stream(event_type, data):
            if not has_clients:
                return  # nobody watching a live-only chunk — nothing to replay
            seq = 0
            payload = json.dumps({
                "seq": 0,
                "type": event_type,
                "chat_id": int(chat_id),
                "is_replay": False,
                "created_at": _created_ms,
                **data,
            })
        else:
            should_buffer = _should_buffer_event(event_type, data, has_clients)
            # No connected clients and this is low-value stream noise: drop it instead
            # of storing replay clutter that can surface later.
            if not has_clients and not should_buffer:
                op = data.get("op", "")
                print(f"[WS] No clients — dropped noise event type={event_type} op={op}", flush=True)
                return

            _ws_seq += 1
            seq = _ws_seq
            payload = json.dumps({
                "seq": seq,
                "type": event_type,
                "chat_id": int(chat_id),
                "is_replay": False,
                "created_at": _created_ms,
                **data,
            })

            if should_buffer:
                _ws_buffer.append((seq, payload))
                if len(_ws_buffer) > _WS_BUFFER_MAX:
                    _ws_buffer.pop(0)

    if not has_clients:
        op = data.get("op", "")
        print(f"[WS] No clients — buffered seq={seq} type={event_type} op={op} ({len(_ws_buffer)} queued)", flush=True)
        return

    print(f"[WS] Broadcasting {event_type} seq={seq} to {len(clients)} client(s)", flush=True)
    for ws in clients:
        try:
            loop = _ws_event_loop
            if loop and loop.is_running():
                asyncio.run_coroutine_threadsafe(_send_or_evict(ws, payload, event_type, seq), loop)
        except Exception:
            pass


async def _send_or_evict(ws, payload: str, event_type: str, seq: int):
    """Send to one client; on failure evict it so it reconnects and replays.

    Sends were previously fire-and-forget: run_coroutine_threadsafe returns a Future nobody
    awaits, so a failed send was invisible AND the client stayed registered. If the failed frame
    was the LAST of a burst (typically stream 'done'), the app never saw a higher seq, so its
    gap detector never fired, it never reconnected, and it sat on partial text forever. Evicting
    on failure turns a silent loss into a reconnect, which replays the buffer and the in-flight
    stream catch-up.
    """
    try:
        await ws.send_text(payload)
    except Exception as e:
        with _ws_lock:
            _ws_clients.discard(ws)
        print(f"[WS] Send failed (type={event_type} seq={seq}): {type(e).__name__}: {e} "
              f"— client evicted so it reconnects and replays", flush=True)
        try:
            await ws.close()
        except Exception:
            pass


# Captured reference to uvicorn's event loop (set in start())
_ws_event_loop: Optional[asyncio.AbstractEventLoop] = None


# --- Routes ---

@contextlib.contextmanager
def _app_request_origin():
    """Tag the calling thread as serving an APP (HTTP API) request while a command runs.

    Command handlers in bot.py can then behave differently for app-originated commands — e.g.
    `/file` skips the Telegram upload, since the app fetches the bytes itself via /api/download
    and routing sensitive files through Telegram is undesirable. FastAPI runs these sync
    endpoints on a worker thread and the handler executes inline on that same thread, so a
    thread-local is visible to it; it is always cleared so a pooled thread can't leak the flag.
    """
    bot_mod = sys.modules.get("bot")
    origin = getattr(bot_mod, "_request_origin", None) if bot_mod else None
    previous = getattr(origin, "source", None) if origin is not None else None
    if origin is not None:
        origin.source = "api"
    try:
        yield
    finally:
        if origin is not None:
            origin.source = previous


@app.post("/api/crash")
async def post_crash(request: Request):
    """Receive crash reports from the Android app."""
    body = await request.body()
    print(f"[CRASH] Android app crash:\n{body.decode('utf-8', errors='replace')}", flush=True)
    return {"ok": True}

@app.post("/api/message")
def post_message(req: MessageRequest, _=Depends(verify_auth)):
    """Send a message or command as if typed in Telegram."""
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty message")

    chat_id = req.chat_id or _default_chat_id
    if not chat_id:
        raise HTTPException(status_code=400, detail="No chat_id provided and no default configured")
    if not _is_allowed(chat_id):
        raise HTTPException(status_code=403, detail="Chat ID not allowed")

    target_session = _resolve_message_session(chat_id, req.session)

    if _is_duplicate_message_request(req, chat_id):
        print(f"[API] duplicate message ignored from {chat_id}: {text[:80]}...", flush=True)
        return {"ok": True, "duplicate": True, "type": "deduped"}

    print(f"[API] message from {chat_id}: {text[:80]}...", flush=True)

    # Echo user message to TG chat so it appears in the conversation
    # Skip echo for slash commands — the command handler sends its own response
    # Use _send_message_no_ws to avoid WS echo back to the app (app already shows it locally)
    # Fire-and-forget via thread to avoid blocking the API on slow TG responses
    if not text.startswith("/"):
        echo_fn = _send_message_no_ws or _send_message
        if echo_fn:
            import threading
            threading.Thread(target=echo_fn, args=(chat_id, f"\U0001F4F1 {text}"),
                           kwargs={"parse_mode": None}, daemon=True).start()

    with _app_request_origin():
        if text.startswith("/"):
            if req.session:
                if text.strip().lower() == "/cancel":
                    cancel_task(TaskActionRequest(chat_id=chat_id, session=req.session))
                    return {"ok": True, "type": "command", "command": "/cancel", "session": req.session}
                if _handle_command_for_session:
                    handled = _handle_command_for_session(chat_id, text, target_session)
                    if not handled:
                        raise HTTPException(status_code=400, detail=f"Unknown command: {text.split()[0]}")
                    return {"ok": True, "type": "command", "command": text.split()[0], "session": req.session}
                raise HTTPException(status_code=400, detail=f"Targeted command not supported: {text.split()[0]}")
            handled = _handle_command(chat_id, text)
            if not handled:
                raise HTTPException(status_code=400, detail=f"Unknown command: {text.split()[0]}")
            return {"ok": True, "type": "command", "command": text.split()[0]}
        else:
            _handle_message(chat_id, text, session=target_session)
            return {"ok": True, "type": "message"}


@app.post("/api/message-targeted")
def post_message_targeted(req: MessageRequest, _=Depends(verify_auth)):
    """Session-aware message endpoint used by app filtered-session views.

    Kept separate so hot reload can graft it onto older live FastAPI apps that
    already have /api/message registered.
    """
    return post_message(req)


@app.post("/api/callback")
def post_callback(req: CallbackRequest, _=Depends(verify_auth)):
    """Simulate a button press (callback query)."""
    chat_id = req.chat_id or _default_chat_id
    if not chat_id:
        raise HTTPException(status_code=400, detail="No chat_id provided and no default configured")
    if not _is_allowed(chat_id):
        raise HTTPException(status_code=403, detail="Chat ID not allowed")

    fake_query = {
        "id": "api_0",
        "message": {
            "chat": {"id": chat_id},
            "message_id": req.message_id,
        },
        "data": req.data,
    }
    _handle_callback_query(fake_query)
    return {"ok": True, "data": req.data}


@app.get("/api/status/{chat_id}")
def get_status(chat_id: int, _=Depends(verify_auth)):
    """Get current session status."""
    if not _is_allowed(chat_id):
        raise HTTPException(status_code=403, detail="Chat ID not allowed")

    session = _get_active_session(chat_id)
    if not session:
        return {"chat_id": chat_id, "active_session": None, "busy": False}

    sid = _get_session_id(session)
    jdi_key = f"{chat_id}:{sid}"

    return {
        "chat_id": chat_id,
        "active_session": session.get("name"),
        "last_cli": session.get("last_cli", "Claude"),
        "busy": sid in _active_processes,
        "goal": (_goal_state or {}).get(jdi_key, {}).get("active", False),
        "justdoit": _justdoit_active.get(jdi_key, {}).get("active", False),
        "omni": _omni_active.get(jdi_key, {}).get("active", False),
        "deepreview": _deepreview_active.get(jdi_key, {}).get("active", False),
        "ralph": (_ralph_active or {}).get(jdi_key, {}).get("active", False),
    }


@app.get("/api/sessions/{chat_id}")
def get_sessions(chat_id: int = 0, _=Depends(verify_auth)):
    """List all sessions for a chat ID."""
    chat_id = chat_id or _default_chat_id
    if not chat_id or not _is_allowed(chat_id):
        raise HTTPException(status_code=403, detail="Chat ID not allowed")

    user_data = _user_sessions.get(str(chat_id), {})
    sessions = user_data.get("sessions", [])
    active_id = user_data.get("active")

    def _is_busy(sid):
        if sid in _active_processes:
            return True
        if f"cron:{sid}" in _active_processes:
            return True
        jdi_key = f"{chat_id}:{sid}"
        if _justdoit_active.get(jdi_key, {}).get("active", False):
            return True
        if (_goal_state or {}).get(jdi_key, {}).get("active", False):
            return True
        if (_goal_active or {}).get(jdi_key):
            return True
        if _omni_active.get(jdi_key, {}).get("active", False):
            return True
        if _deepreview_active.get(jdi_key, {}).get("active", False):
            return True
        if (_ralph_active or {}).get(jdi_key, {}).get("active", False):
            return True
        return False

    return {
        "chat_id": chat_id,
        "active": active_id,
        "sessions": [
            {
                "name": s.get("name"),
                "id": _get_session_id(s),
                "cwd": s.get("cwd"),
                "last_cli": s.get("last_cli", "Claude"),
                "busy": _is_busy(_get_session_id(s)),
                "is_active": _get_session_id(s) == active_id,
                "queue_count": len(_message_queue.get(_get_session_id(s), [])),
            }
            for s in sessions
        ],
    }


@app.patch("/api/sessions/{chat_id}/{session_name:path}")
def patch_session(chat_id: int, session_name: str, body: dict, _=Depends(verify_auth)):
    """Update properties of a session (e.g. cwd)."""
    chat_id = chat_id or _default_chat_id
    if not chat_id or not _is_allowed(chat_id):
        raise HTTPException(status_code=403, detail="Chat ID not allowed")
    user_data = _user_sessions.get(str(chat_id), {})
    for s in user_data.get("sessions", []):
        if s.get("name") == session_name:
            for key in ("cwd", "claude_session_id", "codex_session_id", "codex_session_path"):
                if key in body:
                    s[key] = body[key]
            if _save_sessions:
                _save_sessions(force=True)
            return {"ok": True, "session": s}
    raise HTTPException(status_code=404, detail=f"Session '{session_name}' not found")


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "active_processes": len(_active_processes),
        "ws_clients": len(_ws_clients),
        "threads": threading.active_count(),
    }


@app.get("/api/active-tasks/{chat_id}")
def get_active_tasks(chat_id: int = 0, _=Depends(verify_auth)):
    """Return all currently active autonomous tasks across all sessions."""
    chat_id = chat_id or _default_chat_id
    if not chat_id or not _is_allowed(chat_id):
        raise HTTPException(status_code=403, detail="Chat ID not allowed")

    tasks = []
    autonomous_sids = set()
    for state_dict, mode, _label in _get_mode_states():
        for key, state in list(state_dict.items()):
            if state.get("active") and str(state.get("chat_id")) == str(chat_id):
                tasks.append({
                    "mode": mode,
                    "session": state.get("session_name", ""),
                    "task": (state.get("task", "") or "")[:200],
                    "phase": state.get("phase", ""),
                    "step": state.get("step", 0),
                    "started": state.get("started", 0),
                    "paused": state.get("paused", False),
                })
                # Track session_id to exclude from CLI runs below
                parts = key.split(":", 1)
                if len(parts) == 2:
                    autonomous_sids.add(parts[1])

    # Add active CLI processes (Claude, Codex, Gemini) not already tracked as autonomous tasks
    active_data = _get_active_sessions_data() if _get_active_sessions_data else {}
    user_data = _user_sessions.get(str(chat_id), {}) if _user_sessions else {}
    for s in user_data.get("sessions", []):
        sid = _get_session_id(s) if _get_session_id else None
        if sid and sid in (_active_processes or {}) and sid not in autonomous_sids:
            info = active_data.get(sid, {})
            cli = s.get("last_cli", "Claude")
            tasks.append({
                "mode": cli.lower(),
                "session": s.get("name", ""),
                "task": (info.get("prompt", "") or "")[:200],
                "phase": "",
                "step": 0,
                "started": int(info.get("started", 0)),
                "paused": False,
            })

    return {"tasks": tasks}


@app.post("/api/cancel-task")
def cancel_task(req: TaskActionRequest, _=Depends(verify_auth)):
    """Cancel an autonomous task by session name without switching the active session."""
    import signal as _signal

    chat_id, _target, session_id, jdi_key = _resolve_task_session(req)
    cancelled_mode = None

    for state_dict_ref, mode, mode_label in _get_mode_states():
        state = state_dict_ref.get(jdi_key) if state_dict_ref else None
        if state and state.get("active"):
            state["active"] = False
            # Unblock if paused so the loop thread can exit
            resume_event = state.get("resume_event")
            if resume_event:
                resume_event.set()
            cancelled_mode = mode
            if _ws_broadcast_status:
                _ws_broadcast_status(chat_id, mode, "", 0, active=False)

    if not cancelled_mode and (_goal_active or {}).get(jdi_key):
        cancelled_mode = "goal"

    if cancelled_mode == "goal":
        goal_id = None
        if _cancel_goal_session:
            goal_id = _cancel_goal_session(chat_id, session_id, reason="api_cancel_task")
        else:
            goal_id = (_goal_active or {}).pop(jdi_key, None)
            goal = _load_goal(goal_id) if goal_id and _load_goal else None
            if goal:
                if _cancel_goal_checkin:
                    _cancel_goal_checkin(goal)
                from datetime import datetime
                goal["status"] = "abandoned"
                goal["updated_at"] = datetime.now().isoformat()
                _save_goal(goal)
            if _ws_broadcast_goal and goal_id:
                _ws_broadcast_goal(chat_id, "cancelled", goal_id, {"reason": "api_cancel_task"})

    if _user_feedback_queue:
        _user_feedback_queue.pop(jdi_key, None)
    if _save_active_tasks:
        _save_active_tasks()

    # Kill the process
    process = _active_processes.get(session_id)
    if process:
        if _cancelled_sessions is not None:
            _cancelled_sessions.add(session_id)
        try:
            import os as _os
            _os.killpg(_os.getpgid(process.pid), _signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception:
            # Fallback (e.g. EPERM) — try direct kill
            try:
                process.kill()
            except Exception:
                pass
        # Clean up pipes and tracking
        for pipe in (process.stdout, process.stderr):
            try:
                if pipe:
                    pipe.close()
            except Exception:
                pass
        _active_processes.pop(session_id, None)
        broadcast_ws(chat_id, "status", {"mode": "busy", "active": False})

    if cancelled_mode and _send_message:
        label = dict((m, l) for m, _, l in _AUTONOMOUS_MODES).get(cancelled_mode, cancelled_mode)
        _send_message(chat_id, f"\u26A0\uFE0F *{label} cancelled* for `{req.session}`.\n_Session preserved._")

    if not cancelled_mode and not process:
        raise HTTPException(status_code=404, detail="No active task found for this session")

    return {"status": "cancelled", "session": req.session, "mode": cancelled_mode}


@app.post("/api/pause-task")
def pause_task(req: TaskActionRequest, _=Depends(verify_auth)):
    """Pause an autonomous task. The loop finishes its current step then blocks."""
    chat_id, _target, _sid, jdi_key = _resolve_task_session(req)
    paused_mode = None

    for state_dict_ref, mode, _label in _get_mode_states():
        state = state_dict_ref.get(jdi_key) if state_dict_ref else None
        if state and state.get("active") and not state.get("paused"):
            state["paused"] = True
            resume_event = state.get("resume_event")
            if resume_event:
                resume_event.clear()  # Block the loop thread at next checkpoint
            paused_mode = mode
            if _ws_broadcast_status:
                _ws_broadcast_status(chat_id, mode, state.get("phase", ""), state.get("step", 0), paused=True)

    if paused_mode == "goal":
        goal_id = _goal_id_for_key(jdi_key)
        goal = _load_goal(goal_id) if goal_id and _load_goal else None
        if goal:
            from datetime import datetime
            goal["status"] = "paused"
            goal["updated_at"] = datetime.now().isoformat()
            _save_goal(goal)
            if _schedule_goal_checkin:
                _schedule_goal_checkin(goal)
            if _ws_broadcast_goal:
                _ws_broadcast_goal(chat_id, "paused", goal_id, {"reason": "api_pause_task"})

    if _save_active_tasks:
        _save_active_tasks()

    if not paused_mode:
        raise HTTPException(status_code=404, detail="No active task found for this session")

    return {"status": "paused", "session": req.session, "mode": paused_mode}


@app.post("/api/resume-task")
def resume_task(req: TaskActionRequest, _=Depends(verify_auth)):
    """Resume a paused autonomous task."""
    chat_id, _target, _sid, jdi_key = _resolve_task_session(req)
    resumed_mode = None

    for state_dict_ref, mode, _label in _get_mode_states():
        state = state_dict_ref.get(jdi_key) if state_dict_ref else None
        if state and state.get("active") and state.get("paused"):
            state["paused"] = False
            resume_event = state.get("resume_event")
            if resume_event:
                resume_event.set()  # Unblock the loop thread
            resumed_mode = mode
            if _ws_broadcast_status:
                _ws_broadcast_status(chat_id, mode, state.get("phase", ""), state.get("step", 0), paused=False)

    if resumed_mode == "goal":
        goal_id = _goal_id_for_key(jdi_key)
        goal = _load_goal(goal_id) if goal_id and _load_goal else None
        if goal:
            if _cancel_goal_checkin:
                _cancel_goal_checkin(goal)
            from datetime import datetime
            goal["status"] = "active"
            goal["updated_at"] = datetime.now().isoformat()
            _save_goal(goal)
            if _ws_broadcast_goal:
                _ws_broadcast_goal(chat_id, "resumed", goal_id)

    if _save_active_tasks:
        _save_active_tasks()

    if not resumed_mode:
        raise HTTPException(status_code=404, detail="No paused task found for this session")

    return {"status": "resumed", "session": req.session, "mode": resumed_mode}


# --- Scheduled tasks endpoints ---

@app.get("/api/scheduled-tasks/{chat_id}")
def get_scheduled_tasks(chat_id: int = 0, _=Depends(verify_auth)):
    """List all scheduled tasks for a chat."""
    chat_id = chat_id or _default_chat_id
    if not chat_id or not _is_allowed(chat_id):
        raise HTTPException(status_code=403, detail="Chat ID not allowed")
    with _scheduled_tasks_lock:
        tasks = [t for t in (_scheduled_tasks or {}).values()
                 if str(t.get("chat_id")) == str(chat_id)]
    return sorted(tasks, key=lambda t: t.get("next_run") or float("inf"))


@app.post("/api/schedule-task")
def api_create_schedule_task(req: ScheduleTaskRequest, _=Depends(verify_auth)):
    """Create a new scheduled task."""
    chat_id = req.chat_id or _default_chat_id
    if not chat_id or not _is_allowed(chat_id):
        raise HTTPException(status_code=403, detail="Chat ID not allowed")

    # Resolve cwd: explicit cwd > session_name lookup > current directory
    task_cwd = req.cwd
    if not task_cwd and req.session_name:
        user_data = _user_sessions.get(str(chat_id), {})
        for s in user_data.get("sessions", []):
            if s.get("name") == req.session_name:
                task_cwd = s.get("cwd")
                break
    try:
        task_id, task = _create_scheduled_task(
            chat_id, req.prompt, req.schedule_type,
            cron_expr=req.cron_expr, run_at=req.run_at, cwd=task_cwd,
        )
        return {"status": "created", "task_id": task_id, "next_run": task.get("next_run")}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/api/schedule-task/{task_id}")
def api_update_schedule_task(task_id: str, req: ScheduleTaskUpdate, _=Depends(verify_auth)):
    """Update a scheduled task (enable/disable, edit prompt, change schedule)."""
    try:
        with _scheduled_tasks_lock:
            task = (_scheduled_tasks or {}).get(task_id)
            if not task:
                raise HTTPException(status_code=404, detail="Task not found")
            if req.enabled is not None:
                task["enabled"] = req.enabled
            if req.prompt is not None:
                task["prompt"] = req.prompt
            if req.cron_expr is not None:
                task["cron_expr"] = req.cron_expr
                task["schedule_type"] = "cron"
                if _next_cron_run_fn:
                    from datetime import datetime as _dt
                    nxt = _next_cron_run_fn(req.cron_expr, _dt.now())
                    task["next_run"] = nxt.timestamp() if nxt else None
            if req.run_at is not None:
                task["run_at"] = req.run_at
                task["schedule_type"] = "once"
                from datetime import datetime as _dt
                task["next_run"] = _dt.fromisoformat(req.run_at.replace(" ", "T", 1)).timestamp()
    except HTTPException:
        raise
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    if _save_scheduled_tasks:
        _save_scheduled_tasks()
    chat_id = int(task.get("chat_id", 0))
    if _ws_broadcast_schedule and chat_id:
        _ws_broadcast_schedule(chat_id, "updated", task_id, task)
    return {"status": "updated", "task_id": task_id}


@app.delete("/api/schedule-task/{task_id}")
def api_delete_schedule_task(task_id: str, _=Depends(verify_auth)):
    """Delete a scheduled task."""
    with _scheduled_tasks_lock:
        task = (_scheduled_tasks or {}).pop(task_id, None)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if _save_scheduled_tasks:
        _save_scheduled_tasks()
    chat_id = int(task.get("chat_id", 0))
    if _ws_broadcast_schedule and chat_id:
        _ws_broadcast_schedule(chat_id, "deleted", task_id, task)
    return {"status": "deleted", "task_id": task_id}


@app.post("/api/schedule-task/{task_id}/trigger")
def api_trigger_schedule_task(task_id: str, _=Depends(verify_auth)):
    """Trigger a scheduled task immediately (run now)."""
    with _scheduled_tasks_lock:
        task = (_scheduled_tasks or {}).get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if not _trigger_scheduled_task:
        raise HTTPException(status_code=500, detail="Trigger function not available")
    import threading
    threading.Thread(target=_trigger_scheduled_task, args=(task_id, task), daemon=True).start()
    return {"status": "triggered", "task_id": task_id}


# --- Cron background jobs ---

# --- Message queue management ---

@app.get("/api/queue/{session_id}")
def get_queue(session_id: str, _=Depends(verify_auth)):
    """List queued messages for a session."""
    items = _message_queue.get(session_id, [])
    return {"session_id": session_id, "queue": [
        {"index": i, "text": msg[:500]} for i, msg in enumerate(items)
    ]}


@app.put("/api/queue/{session_id}/{index}")
def edit_queue_item(session_id: str, index: int, body: dict, _=Depends(verify_auth)):
    """Edit a queued message by index."""
    items = _message_queue.get(session_id, [])
    if index < 0 or index >= len(items):
        raise HTTPException(status_code=404, detail="Queue index out of range")
    new_text = body.get("text")
    if not new_text:
        raise HTTPException(status_code=400, detail="Missing 'text' field")
    items[index] = new_text
    return {"status": "updated", "index": index}


@app.delete("/api/queue/{session_id}/{index}")
def delete_queue_item(session_id: str, index: int, _=Depends(verify_auth)):
    """Delete a queued message by index."""
    items = _message_queue.get(session_id, [])
    if index < 0 or index >= len(items):
        raise HTTPException(status_code=404, detail="Queue index out of range")
    removed = items.pop(index)
    if not items:
        _message_queue.pop(session_id, None)
    return {"status": "deleted", "text": removed[:200]}


@app.delete("/api/queue/{session_id}")
def clear_queue(session_id: str, _=Depends(verify_auth)):
    """Clear entire queue for a session."""
    count = len(_message_queue.pop(session_id, []))
    return {"status": "cleared", "count": count}


# --- File download endpoint ---

@app.get("/api/download")
async def download_file(path: str = Query(...), _=Depends(verify_auth)):
    """Serve a file by absolute path. Used by the Android app to receive /file results."""
    import mimetypes
    from fastapi.responses import FileResponse

    real = os.path.realpath(path)
    if not os.path.isfile(real):
        raise HTTPException(status_code=404, detail="File not found")
    mime, _ = mimetypes.guess_type(real)
    return FileResponse(path=real, filename=os.path.basename(real),
                        media_type=mime or "application/octet-stream")


# --- Goal endpoints ---

def _resolve_goal_session(chat_id, session_name=None):
    """Resolve chat_id and session_id for goal operations. Returns (chat_id, session_id, cwd).

    If session_name is provided but not found, raises 404 instead of silently
    falling back to the active session.
    """
    chat_id = chat_id or _default_chat_id
    if not chat_id or not _is_allowed(chat_id):
        raise HTTPException(status_code=403, detail="Chat ID not allowed")
    session_id = None
    cwd = None
    if session_name:
        user_data = _user_sessions.get(str(chat_id), {})
        for s in user_data.get("sessions", []):
            if s.get("name") == session_name:
                session_id = _get_session_id(s)
                cwd = s.get("cwd")
                break
        if not session_id:
            raise HTTPException(status_code=404, detail=f"Session '{session_name}' not found")
    else:
        # No session_name specified — fall back to active session
        active = _get_active_session(chat_id)
        if active:
            session_id = _get_session_id(active)
            cwd = active.get("cwd")
    return chat_id, session_id, cwd


@app.get("/api/goals/{chat_id}")
def api_list_goals(chat_id: int = 0, _=Depends(verify_auth)):
    """List all goals for a chat."""
    chat_id = chat_id or _default_chat_id
    if not chat_id or not _is_allowed(chat_id):
        raise HTTPException(status_code=403, detail="Chat ID not allowed")
    goals = _list_goals(chat_id)
    # Return summary, not full iterations/learnings
    summaries = []
    for g in goals:
        milestones = g.get("milestones", [])
        total = len(milestones)
        done = sum(1 for m in milestones if m.get("status") == "completed")
        goal_key = f"{g['chat_id']}:{g.get('session_id', '')}"
        state = (_goal_state or {}).get(goal_key, {})
        # Resolve session name
        session_name = ""
        sid = g.get("session_id", "")
        if sid and _user_sessions:
            for s in _user_sessions.get(str(g.get("chat_id", "")), {}).get("sessions", []):
                s_id = _get_session_id(s) if _get_session_id else s.get("id")
                if s_id == sid:
                    session_name = s.get("name", "")
                    break
        summaries.append({
            "id": g["id"],
            "title": g.get("title", ""),
            "description": g.get("description", ""),
            "status": g["status"],
            "session": session_name,
            "created_at": g.get("created_at"),
            "updated_at": g.get("updated_at"),
            "completed_at": g.get("completed_at"),
            "milestones_total": total,
            "milestones_done": done,
            "current_iteration": len(g.get("iterations", [])),
            "learnings_count": len(g.get("learnings", [])),
            "is_running": _goal_is_running(g, goal_key),
            "is_paused": bool(state.get("paused")),
            "milestones": [
                {
                    "id": m.get("id", ""),
                    "title": m.get("title", ""),
                    "status": m.get("status", "pending"),
                    "order": m.get("order", 0),
                    "attempts": m.get("attempts", 0),
                    "acceptance_criteria": m.get("acceptance_criteria", []),
                }
                for m in milestones
            ],
        })
    return {"goals": summaries}


@app.get("/api/goal/{goal_id}")
def api_get_goal(goal_id: str, _=Depends(verify_auth)):
    """Get full goal state including milestones and iterations."""
    goal = _load_goal(goal_id)
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")
    goal_key = f"{goal['chat_id']}:{goal.get('session_id', '')}"
    goal["is_running"] = _goal_is_running(goal, goal_key)
    state = (_goal_state or {}).get(goal_key, {})
    goal["is_paused"] = bool(state.get("paused"))
    return goal


@app.get("/api/goal/{goal_id}/journal")
def api_get_goal_journal(goal_id: str, _=Depends(verify_auth)):
    """Get goal learnings/journal only."""
    goal = _load_goal(goal_id)
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")
    return {
        "goal_id": goal_id,
        "title": goal.get("title", ""),
        "learnings": goal.get("learnings", []),
    }


@app.post("/api/goal")
def api_create_goal(req: GoalCreateRequest, _=Depends(verify_auth)):
    """Create a new goal, decompose it, and optionally start the loop."""
    chat_id, session_id, cwd = _resolve_goal_session(req.chat_id, req.session_name)
    if not session_id:
        raise HTTPException(status_code=400, detail="No active session found")
    if not cwd:
        cwd = os.getcwd()

    goal_key = f"{chat_id}:{session_id}"
    busy_reason = _get_session_busy_reason(chat_id, session_id) if _get_session_busy_reason else None
    if busy_reason:
        raise HTTPException(status_code=409, detail=busy_reason)
    if not _get_session_busy_reason:
        if (_goal_active or {}).get(goal_key):
            raise HTTPException(status_code=409, detail="A goal is already running on this session")
        if (_goal_state or {}).get(goal_key, {}).get("active"):
            raise HTTPException(status_code=409, detail="A goal is already running on this session")
        if (_justdoit_active or {}).get(goal_key, {}).get("active"):
            raise HTTPException(status_code=409, detail="Session is busy with another task")
        if session_id in (_active_processes or {}):
            raise HTTPException(status_code=409, detail="Session is busy with another task")

    # Validate config if provided
    validated_config = None
    if req.config:
        try:
            cfg_model = GoalConfigUpdate(**req.config)
            validated_config = cfg_model.model_dump(exclude_none=True)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid config: {e}")

    # Create goal
    goal = _create_goal(chat_id, session_id, cwd, req.description, validated_config)
    reserved = False
    if _reserve_goal_session:
        ok, busy_reason = _reserve_goal_session(
            chat_id,
            session_id,
            goal["id"],
            task=req.description[:200],
            session_name=_session_name_for_id(chat_id, session_id),
            phase="planning",
        )
        if not ok:
            _delete_goal(goal["id"])
            raise HTTPException(status_code=409, detail=busy_reason or "Session is busy")
        reserved = True

    # Resolve session for context bridge injection
    session = None
    user_data = (_user_sessions or {}).get(str(chat_id), {})
    for s in user_data.get("sessions", []):
        if _get_session_id and _get_session_id(s) == session_id:
            session = s
            break

    # Decompose
    try:
        title, milestones = _decompose_goal(req.description, cwd, session=session, chat_id=chat_id)
        goal["title"] = title
        goal["milestones"] = milestones
        goal["status"] = "active"
        from datetime import datetime
        goal["updated_at"] = datetime.now().isoformat()
        _save_goal(goal)
    except Exception as e:
        if reserved and _release_goal_session:
            _release_goal_session(chat_id, session_id, goal["id"])
        _delete_goal(goal["id"])
        exc_name = e.__class__.__name__
        if exc_name == "GoalRateLimitError":
            raise HTTPException(status_code=429, detail=f"Goal planning rate-limited: {e}")
        if exc_name == "GoalModelTimeoutError":
            raise HTTPException(status_code=504, detail=f"Goal planning timed out: {e}")
        raise HTTPException(status_code=500, detail=f"Goal decomposition failed: {e}")

    # Start the loop in a background thread
    import threading
    thread = threading.Thread(
        target=_run_goal_loop,
        args=(chat_id, session_id, goal["id"]),
        daemon=True,
    )
    thread.start()

    return {
        "status": "created",
        "goal_id": goal["id"],
        "title": goal.get("title", ""),
        "milestones": len(goal.get("milestones", [])),
    }


@app.post("/api/goal/{goal_id}/pause")
def api_pause_goal(goal_id: str, _=Depends(verify_auth)):
    """Pause a running goal."""
    goal = _load_goal(goal_id)
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")
    goal_key = f"{goal['chat_id']}:{goal.get('session_id', '')}"
    if goal_key not in (_goal_active or {}):
        raise HTTPException(status_code=400, detail="Goal is not running")
    state = (_goal_state or {}).get(goal_key)
    if not state or not state.get("active"):
        raise HTTPException(status_code=400, detail="Goal is not active")
    state["paused"] = True
    resume_event = state.get("resume_event")
    if resume_event:
        resume_event.clear()
    # Persist paused status to disk for crash recovery
    from datetime import datetime
    goal["status"] = "paused"
    goal["updated_at"] = datetime.now().isoformat()
    _save_goal(goal)
    if _schedule_goal_checkin:
        _schedule_goal_checkin(goal)
    if _save_active_tasks:
        _save_active_tasks()
    if _ws_broadcast_goal:
        _ws_broadcast_goal(int(goal["chat_id"]), "paused", goal_id, {"reason": "api_requested"})
    return {"status": "paused", "goal_id": goal_id}


@app.post("/api/goal/{goal_id}/resume")
def api_resume_goal(goal_id: str, _=Depends(verify_auth)):
    """Resume a paused goal."""
    goal = _load_goal(goal_id)
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")
    goal_key = f"{goal['chat_id']}:{goal.get('session_id', '')}"

    # Check if running but paused
    state = (_goal_state or {}).get(goal_key)
    if state and state.get("active") and state.get("paused"):
        wait_seconds, resume_at = _goal_rate_limit_resume_delay(goal)
        if wait_seconds > 0:
            raise HTTPException(status_code=429, detail=_goal_rate_limit_resume_detail(wait_seconds, resume_at))
        if _goal_clear_expired_rate_limit(goal):
            _save_goal(goal)
        state["paused"] = False
        resume_event = state.get("resume_event")
        if resume_event:
            resume_event.set()
        # Cancel any paused check-in and restore active status on disk
        if _cancel_goal_checkin:
            _cancel_goal_checkin(goal)
        from datetime import datetime
        goal["status"] = "active"
        goal["updated_at"] = datetime.now().isoformat()
        _save_goal(goal)
        if _save_active_tasks:
            _save_active_tasks()
        if _ws_broadcast_goal:
            _ws_broadcast_goal(int(goal["chat_id"]), "resumed", goal_id)
        if _ws_broadcast_status:
            _ws_broadcast_status(int(goal["chat_id"]), "goal", state.get("phase", ""), state.get("step", 0), paused=False)
        return {"status": "resumed", "goal_id": goal_id}

    # Check if there's a paused goal on disk to restart
    if (_goal_active or {}).get(goal_key):
        raise HTTPException(status_code=400, detail="Goal is already running")

    if goal["status"] != "paused":
        raise HTTPException(status_code=400, detail=f"Goal is not paused (status: {goal['status']})")

    wait_seconds, resume_at = _goal_rate_limit_resume_delay(goal)
    if wait_seconds > 0:
        raise HTTPException(status_code=429, detail=_goal_rate_limit_resume_detail(wait_seconds, resume_at))
    if _goal_clear_expired_rate_limit(goal):
        _save_goal(goal)

    session_id = goal.get("session_id", "")
    chat_id = int(goal["chat_id"])
    busy_reason = _get_session_busy_reason(chat_id, session_id) if _get_session_busy_reason else None
    if busy_reason:
        raise HTTPException(status_code=409, detail=busy_reason)
    if _reserve_goal_session:
        ok, busy_reason = _reserve_goal_session(
            chat_id,
            session_id,
            goal["id"],
            task=goal.get("title") or goal.get("description", "")[:200],
            session_name=_session_name_for_id(chat_id, session_id),
            phase="resuming",
        )
        if not ok:
            raise HTTPException(status_code=409, detail=busy_reason or "Session is busy")

    # Restart from paused state — cancel check-in
    if _cancel_goal_checkin:
        _cancel_goal_checkin(goal)
    from datetime import datetime
    goal["status"] = "active"
    goal["updated_at"] = datetime.now().isoformat()
    _save_goal(goal)

    import threading
    thread = threading.Thread(
        target=_run_goal_loop,
        args=(chat_id, session_id, goal["id"]),
        daemon=True,
    )
    thread.start()
    return {"status": "resumed", "goal_id": goal_id}


@app.post("/api/goal/{goal_id}/cancel")
def api_cancel_goal(goal_id: str, _=Depends(verify_auth)):
    """Cancel a running goal."""
    goal = _load_goal(goal_id)
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")
    goal_key = f"{goal['chat_id']}:{goal.get('session_id', '')}"
    if goal_key not in (_goal_active or {}):
        raise HTTPException(status_code=400, detail="Goal is not running")
    session_id = goal.get("session_id", "")
    if _cancel_goal_session:
        _cancel_goal_session(int(goal["chat_id"]), session_id, goal_id, reason="api_goal_cancel")
    else:
        state = (_goal_state or {}).get(goal_key)
        if state:
            state["active"] = False
            resume_event = state.get("resume_event")
            if resume_event:
                resume_event.set()  # Unblock if paused
        (_goal_active or {}).pop(goal_key, None)
    # Kill active subprocess
    if _active_processes and session_id:
        process = _active_processes.get(session_id)
        if process:
            if _cancelled_sessions is not None:
                _cancelled_sessions.add(session_id)
            try:
                import signal, os
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                try:
                    if process.stdout:
                        process.stdout.close()
                except Exception:
                    pass
            except (ProcessLookupError, OSError):
                pass
            _active_processes.pop(session_id, None)
    # Persist abandoned status immediately (cancel_goal_session already did this
    # in the normal bot-integrated path; repeat is harmless for fallback refs).
    fresh_goal = _load_goal(goal_id) or goal
    if _cancel_goal_checkin:
        _cancel_goal_checkin(fresh_goal)
    from datetime import datetime
    fresh_goal["status"] = "abandoned"
    fresh_goal["updated_at"] = datetime.now().isoformat()
    _save_goal(fresh_goal)
    if _save_active_tasks:
        _save_active_tasks()
    if _ws_broadcast_goal:
        _ws_broadcast_goal(int(goal["chat_id"]), "cancelled", goal_id)
    if _ws_broadcast_status:
        _ws_broadcast_status(int(goal["chat_id"]), "goal", "", 0, active=False)
    return {"status": "cancelled", "goal_id": goal_id}


@app.post("/api/goal/{goal_id}/replan")
def api_replan_goal(goal_id: str, _=Depends(verify_auth)):
    """Trigger replanning for a goal."""
    goal = _load_goal(goal_id)
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")
    if goal["status"] not in ("active", "paused"):
        raise HTTPException(status_code=400, detail=f"Cannot replan goal with status: {goal['status']}")
    # Resolve session so replan gets context bridge (Codex/Gemini session logs)
    session = None
    chat_id = None
    g_session_id = goal.get("session_id", "")
    g_chat_id = goal.get("chat_id")
    if g_chat_id and g_session_id:
        chat_id = int(g_chat_id)
        user_data = (_user_sessions or {}).get(str(chat_id), {})
        for s in user_data.get("sessions", []):
            if _get_session_id and _get_session_id(s) == g_session_id:
                session = s
                break

    try:
        new_milestones, rationale = _replan_goal(goal, session=session, chat_id=chat_id)
        from datetime import datetime
        goal["milestones"] = new_milestones
        goal["current_milestone_id"] = None
        goal["updated_at"] = datetime.now().isoformat()
        _save_goal(goal)
        return {"status": "replanned", "goal_id": goal_id, "rationale": rationale, "milestones": len(new_milestones)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Replan failed: {e}")


@app.patch("/api/goal/{goal_id}/config")
def api_update_goal_config(goal_id: str, req: GoalConfigUpdate, _=Depends(verify_auth)):
    """Update goal configuration."""
    goal = _load_goal(goal_id)
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")
    cfg = goal.get("config", {})
    updates = req.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail="No config fields provided")
    for key, value in updates.items():
        if key not in cfg:
            raise HTTPException(status_code=400, detail=f"Unknown config key: {key}")
        cfg[key] = value
    goal["config"] = cfg
    from datetime import datetime
    goal["updated_at"] = datetime.now().isoformat()
    _save_goal(goal)
    return {"status": "updated", "goal_id": goal_id, "config": cfg}


@app.delete("/api/goal/{goal_id}")
def api_delete_goal(goal_id: str, _=Depends(verify_auth)):
    """Delete a goal. Cannot delete running goals."""
    goal = _load_goal(goal_id)
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")
    # Block only if THIS goal is the one actively running on its session — not merely because
    # some (possibly newer) goal is active on the same session, and not for terminal-status goals.
    goal_key = f"{goal['chat_id']}:{goal.get('session_id', '')}"
    this_goal_active = (_goal_active or {}).get(goal_key) == goal_id
    if this_goal_active and goal.get("status") in ("active", "planning"):
        raise HTTPException(status_code=409, detail="Cannot delete a running goal. Cancel it first.")
    _delete_goal(goal_id)
    return {"status": "deleted", "goal_id": goal_id}


# --- WebSocket endpoint ---

@app.websocket("/ws")
async def ws_endpoint(
    websocket: WebSocket,
    token: str = Query(default=""),
    last_seq: int = Query(default=0),
):
    """WebSocket stream for all bot messages in real time.
    Connect: ws://host:port/ws?token=YOUR_SECRET&last_seq=N
    Messages after last_seq are replayed on connect.
    """
    # Auth check
    if API_SECRET and token != API_SECRET:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await websocket.accept()

    # Send server identity so the app can detect restarts
    hello = json.dumps({"type": "server_hello", "server_id": _server_id, "seq": 0})
    await websocket.send_text(hello)

    # Replay missed messages on reconnect (all event types — the app
    # handles stream/status events gracefully even when replayed).
    with _ws_lock:
        _ws_clients.add(websocket)
        if last_seq > 0 and last_seq <= _ws_seq:
            replay = [(s, p) for s, p in _ws_buffer if s > last_seq]
        elif last_seq > _ws_seq:
            # Client's seq is ahead — server was restarted, replay full buffer
            replay = list(_ws_buffer)
        else:
            # Fresh connect (last_seq=0) — replay full buffer so client
            # catches up on anything it missed (e.g. first-time connect).
            replay = list(_ws_buffer)
    print(f"[WS] Client connected (last_seq={last_seq}, replaying {len(replay)})", flush=True)

    # Replay missed messages (throttled to avoid flooding)
    for i, (_, payload) in enumerate(replay):
        try:
            await websocket.send_text(_with_replay_flag(payload, True))
            if (i + 1) % 10 == 0:
                await asyncio.sleep(0.05)
        except Exception:
            break

    # Catch up any in-progress streams whose appends were lost during the client's gap. The
    # buffered 'start' (above) reset the message text to ""; re-send 'start' (idempotent clear)
    # then one 'append' carrying the full running text so the live view resumes where it is.
    with _ws_lock:
        active_snapshot = [(m, dict(s)) for m, s in _active_streams.items()]
    for mid, snap in active_snapshot:
        if not snap["text"]:
            continue
        base = {"type": "stream", "chat_id": snap["chat_id"], "message_id": mid,
                "session": snap["session"], "seq": 0, "is_replay": True,
                "created_at": snap.get("created_at", 0)}
        try:
            await websocket.send_text(json.dumps({**base, "op": "start"}))
            await websocket.send_text(json.dumps({**base, "op": "append", "text": snap["text"]}))
        except Exception:
            break

    try:
        while True:
            # Use receive() to handle all frame types (text, bytes, ping, pong, close)
            msg = await websocket.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg["type"] == "websocket.receive":
                data = msg.get("text", "")
                if data:
                    try:
                        parsed = json.loads(data)
                        msg_type = parsed.get("type", "")

                        if msg_type == "resend":
                            from_seq = parsed.get("from_seq", 0)
                            with _ws_lock:
                                resend = [(s, p) for s, p in _ws_buffer if s >= from_seq]
                            print(f"[WS] Resend request from_seq={from_seq}, sending {len(resend)} messages", flush=True)
                            for _, p in resend:
                                try:
                                    await websocket.send_text(_with_replay_flag(p, True))
                                except Exception:
                                    break
                        else:
                            text = parsed.get("text", "").strip()
                            if text:
                                print(f"[WS] incoming: {text[:80]}...", flush=True)
                    except json.JSONDecodeError:
                        await websocket.send_text(json.dumps({"type": "error", "detail": "Invalid JSON"}))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[WS] Error: {e}", flush=True)
    finally:
        with _ws_lock:
            _ws_clients.discard(websocket)
        print("[WS] Client disconnected", flush=True)


def start(host: str, port: int):
    """Start the API server in a background daemon thread.
    If the host IP isn't available yet (e.g. Tailscale not ready at boot),
    retries every few seconds until it can bind."""
    global _ws_event_loop
    import uvicorn
    import socket

    def _wait_for_interface(addr):
        """Block until the given IP address is bindable. Retries indefinitely."""
        logged = False
        while True:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.bind((addr, 0))
                s.close()
                if logged:
                    print(f"[API] {addr} is now available.", flush=True)
                return
            except OSError:
                if not logged:
                    print(f"[API] Waiting for {addr} to become available (e.g. Tailscale)...", flush=True)
                    logged = True
                time.sleep(3)

    def _run():
        global _ws_event_loop
        # Wait for the host interface before starting uvicorn
        if host not in ("0.0.0.0", "127.0.0.1", "localhost", ""):
            _wait_for_interface(host)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _ws_event_loop = loop
        config = uvicorn.Config(app, host=host, port=port, log_level="warning", loop="asyncio")
        server = uvicorn.Server(config)
        print(f"API server listening on http://{host}:{port}", flush=True)
        print(f"  WebSocket: ws://{host}:{port}/ws", flush=True)
        print(f"  API docs:  http://{host}:{port}/docs", flush=True)
        loop.run_until_complete(server.serve())

    t = threading.Thread(target=_run, daemon=True, name="api-server")
    t.start()
