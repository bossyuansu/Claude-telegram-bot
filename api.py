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

from fastapi import FastAPI, HTTPException, Depends, Header, WebSocket, WebSocketDisconnect, Query
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

API_SECRET = ""
_default_chat_id = None

# --- WebSocket client registry ---
_ws_clients: set[WebSocket] = set()
_ws_lock = threading.Lock()

# --- WS message buffer with sequence numbers ---
_ws_seq = 0  # Monotonic sequence counter
_ws_buffer: list[tuple[int, str]] = []  # (seq, JSON payload)
_WS_BUFFER_MAX = 500


def init_refs(**kwargs):
    """Receive references to bot.py functions and shared dicts."""
    global _handle_command, _handle_message, _handle_callback_query
    global _is_allowed, _get_active_session, _get_session_id
    global _user_sessions, _active_processes
    global _justdoit_active, _omni_active, _deepreview_active
    global _send_message, _send_message_no_ws
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


# --- WebSocket broadcast (called from bot.py threads) ---

def broadcast_ws(chat_id, event_type, data):
    """Send a message to all connected WebSocket clients.
    Every broadcast gets a monotonic seq number for ordering guarantees.
    If no clients are connected, buffer for delivery on reconnect.
    """
    global _ws_seq

    with _ws_lock:
        _ws_seq += 1
        seq = _ws_seq
        payload = json.dumps({"seq": seq, "type": event_type, "chat_id": int(chat_id), **data})

        # Always buffer (for replay on reconnect)
        _ws_buffer.append((seq, payload))
        if len(_ws_buffer) > _WS_BUFFER_MAX:
            _ws_buffer.pop(0)

        clients = list(_ws_clients)

    if not clients:
        print(f"[WS] No clients — buffered seq={seq} ({len(_ws_buffer)} queued)", flush=True)
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
    if not text.startswith("/"):
        echo_fn = _send_message_no_ws or _send_message
        if echo_fn:
            echo_fn(chat_id, f"\U0001F4F1 {text}", parse_mode=None)

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

    # Register and find messages to replay
    with _ws_lock:
        _ws_clients.add(websocket)
        if last_seq > 0:
            # Replay everything after last_seq from the buffer
            replay = [(s, p) for s, p in _ws_buffer if s > last_seq]
        else:
            # No last_seq — flush entire buffer (legacy behavior)
            replay = list(_ws_buffer)
    print(f"[WS] Client connected (last_seq={last_seq}, replaying {len(replay)})", flush=True)

    # Replay missed messages
    for _, payload in replay:
        try:
            await websocket.send_text(payload)
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

                        # Handle resend request: client detected a seq gap
                        if msg_type == "resend":
                            from_seq = parsed.get("from_seq", 0)
                            with _ws_lock:
                                resend = [(s, p) for s, p in _ws_buffer if s >= from_seq]
                            print(f"[WS] Resend request from_seq={from_seq}, sending {len(resend)} messages", flush=True)
                            for _, payload in resend:
                                try:
                                    await websocket.send_text(payload)
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
