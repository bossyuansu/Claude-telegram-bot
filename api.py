"""
HTTP API server for Claude Telegram Bot.
Runs alongside the Telegram polling loop in the same process,
sharing all in-memory state. Listens on the Tailscale IP.

WebSocket endpoint at /ws streams all bot messages in real time.
"""
import asyncio
import json
import os
import threading
import time
import uuid

from fastapi import FastAPI, HTTPException, Depends, Header, WebSocket, WebSocketDisconnect, Query, Request
from pydantic import BaseModel
from typing import Optional

app = FastAPI(title="Claude Bot API", docs_url="/docs")

# Module-level refs (populated by init_refs from bot.py)
_handle_command = None
_handle_message = None
_handle_callback_query = None
_is_allowed = None
_get_active_session = None
_get_session_id = None
_user_sessions = None
_active_processes = None
_justdoit_active = None
_omni_active = None
_deepreview_active = None
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
_next_cron_run_fn = None
_ws_broadcast_schedule = None

API_SECRET = ""
_default_chat_id = None

# --- WebSocket client registry ---
_ws_clients: set[WebSocket] = set()
_ws_lock = threading.Lock()

# --- WS message buffer with sequence numbers ---
_ws_seq = 0  # Monotonic sequence counter
_ws_buffer: list[tuple[int, str]] = []  # (seq, JSON payload)
_WS_BUFFER_MAX = 500
_server_id = str(uuid.uuid4())[:8]  # Unique ID per server boot


def _with_replay_flag(payload: str, is_replay: bool) -> str:
    """Return payload JSON with explicit replay flag for client-side UX decisions."""
    try:
        obj = json.loads(payload)
        obj["is_replay"] = bool(is_replay)
        return json.dumps(obj)
    except Exception:
        return payload


def _should_buffer_event(event_type: str, data: dict, has_clients: bool) -> bool:
    """Decide whether this event should be retained in replay buffer.

    Preserve stream continuity (`start/append/done`) across reconnects.
    Only low-value stream noise (`tool/skip`) is dropped while offline.
    """
    if event_type != "stream":
        return True

    op = str(data.get("op", "")).lower()
    if op in {"tool", "skip"} and not has_clients:
        return False
    return True


def init_refs(**kwargs):
    """Receive references to bot.py functions and shared dicts."""
    global _handle_command, _handle_message, _handle_callback_query
    global _is_allowed, _get_active_session, _get_session_id
    global _user_sessions, _active_processes
    global _justdoit_active, _omni_active, _deepreview_active
    global _send_message, _send_message_no_ws
    global _cancelled_sessions, _ws_broadcast_status, _save_active_tasks, _user_feedback_queue, _get_active_sessions_data
    global _scheduled_tasks, _scheduled_tasks_lock, _save_scheduled_tasks, _create_scheduled_task, _next_cron_run_fn, _ws_broadcast_schedule
    global API_SECRET, _default_chat_id

    _handle_command = kwargs["handle_command"]
    _handle_message = kwargs["handle_message"]
    _handle_callback_query = kwargs["handle_callback_query"]
    _is_allowed = kwargs["is_allowed"]
    _get_active_session = kwargs["get_active_session"]
    _get_session_id = kwargs["get_session_id"]
    _user_sessions = kwargs["user_sessions"]
    _active_processes = kwargs["active_processes"]
    _justdoit_active = kwargs["justdoit_active"]
    _omni_active = kwargs["omni_active"]
    _deepreview_active = kwargs["deepreview_active"]
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
    _next_cron_run_fn = kwargs.get("next_cron_run_fn")
    _ws_broadcast_schedule = kwargs.get("ws_broadcast_schedule")
    API_SECRET = os.environ.get("API_SECRET", "")
    _default_chat_id = kwargs.get("default_chat_id")


# --- Auth ---

def verify_auth(authorization: str = Header(None)):
    if not API_SECRET:
        return
    if not authorization or authorization != f"Bearer {API_SECRET}":
        raise HTTPException(status_code=401, detail="Unauthorized")


# --- Models ---

class MessageRequest(BaseModel):
    chat_id: Optional[int] = None
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
    session_name: str
    prompt: str
    schedule_type: str  # "cron" | "once"
    cron_expr: Optional[str] = None
    run_at: Optional[str] = None
    mode: str = "justdoit"  # "justdoit" | "remind"

class ScheduleTaskUpdate(BaseModel):
    enabled: Optional[bool] = None
    prompt: Optional[str] = None
    cron_expr: Optional[str] = None
    run_at: Optional[str] = None
    mode: Optional[str] = None


# --- Task helpers ---

_AUTONOMOUS_MODES = [
    ("justdoit", "_justdoit_active", "JustDoIt"),
    ("omni", "_omni_active", "Omni"),
    ("deepreview", "_deepreview_active", "Deep review"),
]

def _get_mode_states():
    """Return [(state_dict, mode_key, label)] resolving current global refs."""
    return [
        (_justdoit_active or {}, "justdoit", "JustDoIt"),
        (_omni_active or {}, "omni", "Omni"),
        (_deepreview_active or {}, "deepreview", "Deep review"),
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


# --- WebSocket broadcast (called from bot.py threads) ---

def broadcast_ws(chat_id, event_type, data):
    """Send a message to all connected WebSocket clients.
    Every broadcast gets a monotonic seq number for ordering guarantees.
    If no clients are connected, buffer for delivery on reconnect.
    """
    global _ws_seq

    with _ws_lock:
        clients = list(_ws_clients)
        has_clients = bool(clients)
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
                asyncio.run_coroutine_threadsafe(ws.send_text(payload), loop)
        except Exception:
            pass


# Captured reference to uvicorn's event loop (set in start())
_ws_event_loop: Optional[asyncio.AbstractEventLoop] = None


# --- Routes ---

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

    if text.startswith("/"):
        handled = _handle_command(chat_id, text)
        if not handled:
            raise HTTPException(status_code=400, detail=f"Unknown command: {text.split()[0]}")
        return {"ok": True, "type": "command", "command": text.split()[0]}
    else:
        _handle_message(chat_id, text)
        return {"ok": True, "type": "message"}


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
        "justdoit": _justdoit_active.get(jdi_key, {}).get("active", False),
        "omni": _omni_active.get(jdi_key, {}).get("active", False),
        "deepreview": _deepreview_active.get(jdi_key, {}).get("active", False),
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

    return {
        "chat_id": chat_id,
        "active": active_id,
        "sessions": [
            {
                "name": s.get("name"),
                "id": _get_session_id(s),
                "cwd": s.get("cwd"),
                "last_cli": s.get("last_cli", "Claude"),
                "busy": _get_session_id(s) in _active_processes,
                "is_active": _get_session_id(s) == active_id,
            }
            for s in sessions
        ],
    }


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
    try:
        task_id, task = _create_scheduled_task(
            chat_id, req.session_name, req.prompt, req.schedule_type,
            cron_expr=req.cron_expr, run_at=req.run_at, mode=req.mode,
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
            if req.mode is not None:
                if req.mode not in ("justdoit", "remind"):
                    raise HTTPException(status_code=400, detail="Invalid mode")
                task["mode"] = req.mode
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
    """Start the API server in a background daemon thread."""
    global _ws_event_loop
    import uvicorn

    def _run():
        global _ws_event_loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _ws_event_loop = loop
        config = uvicorn.Config(app, host=host, port=port, log_level="warning", loop="asyncio")
        server = uvicorn.Server(config)
        loop.run_until_complete(server.serve())

    t = threading.Thread(target=_run, daemon=True, name="api-server")
    t.start()
    # Give uvicorn a moment to create the event loop
    time.sleep(0.5)
    print(f"API server listening on http://{host}:{port}", flush=True)
    print(f"  WebSocket: ws://{host}:{port}/ws", flush=True)
    print(f"  API docs:  http://{host}:{port}/docs", flush=True)
