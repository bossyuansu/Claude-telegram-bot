"""
HTTP API server for Claude Telegram Bot.
Runs alongside the Telegram polling loop in the same process,
sharing all in-memory state. Listens on the Tailscale IP.

WebSocket endpoint at /ws/{chat_id} streams all bot messages in real time.
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

API_SECRET = ""

# --- WebSocket client registry ---
# chat_id (str) -> set of WebSocket connections
_ws_clients: dict[str, set[WebSocket]] = {}
_ws_lock = threading.Lock()


def init_refs(**kwargs):
    """Receive references to bot.py functions and shared dicts."""
    global _handle_command, _handle_message, _handle_callback_query
    global _is_allowed, _get_active_session, _get_session_id
    global _user_sessions, _active_processes
    global _justdoit_active, _omni_active, _deepreview_active
    global API_SECRET

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
    API_SECRET = os.environ.get("API_SECRET", "")


# --- Auth ---

def verify_auth(authorization: str = Header(None)):
    if not API_SECRET:
        return
    if not authorization or authorization != f"Bearer {API_SECRET}":
        raise HTTPException(status_code=401, detail="Unauthorized")


# --- Models ---

class MessageRequest(BaseModel):
    chat_id: int
    text: str

class CallbackRequest(BaseModel):
    chat_id: int
    data: str
    message_id: int


# --- WebSocket broadcast (called from bot.py threads) ---

def broadcast_ws(chat_id, event_type, data):
    """Send a message to all WebSocket clients subscribed to this chat_id.
    Called from bot.py's send_message/edit_message (sync threads).
    Failures on individual clients are silently ignored — WS and TG are independent.
    """
    key = str(chat_id)
    with _ws_lock:
        clients = list(_ws_clients.get(key, set()))
    if not clients:
        return

    payload = json.dumps({"type": event_type, "chat_id": int(chat_id), **data})

    for ws in clients:
        try:
            # Send from sync context into the async WS — use the event loop
            loop = _ws_event_loop
            if loop and loop.is_running():
                asyncio.run_coroutine_threadsafe(ws.send_text(payload), loop)
        except Exception:
            pass  # Independent channel — don't let WS errors affect bot


# Captured reference to uvicorn's event loop (set in start())
_ws_event_loop: Optional[asyncio.AbstractEventLoop] = None


# --- Routes ---

@app.post("/api/message")
def post_message(req: MessageRequest, _=Depends(verify_auth)):
    """Send a message or command as if typed in Telegram."""
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty message")

    if not _is_allowed(req.chat_id):
        raise HTTPException(status_code=403, detail="Chat ID not allowed")

    print(f"[API] message from {req.chat_id}: {text[:80]}...", flush=True)

    if text.startswith("/"):
        handled = _handle_command(req.chat_id, text)
        if not handled:
            raise HTTPException(status_code=400, detail=f"Unknown command: {text.split()[0]}")
        return {"ok": True, "type": "command", "command": text.split()[0]}
    else:
        _handle_message(req.chat_id, text)
        return {"ok": True, "type": "message"}


@app.post("/api/callback")
def post_callback(req: CallbackRequest, _=Depends(verify_auth)):
    """Simulate a button press (callback query)."""
    if not _is_allowed(req.chat_id):
        raise HTTPException(status_code=403, detail="Chat ID not allowed")

    fake_query = {
        "id": "api_0",
        "message": {
            "chat": {"id": req.chat_id},
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
def get_sessions(chat_id: int, _=Depends(verify_auth)):
    """List all sessions for a chat ID."""
    if not _is_allowed(chat_id):
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
        "ws_clients": sum(len(v) for v in _ws_clients.values()),
        "threads": threading.active_count(),
    }


# --- WebSocket endpoint ---

@app.websocket("/ws/{chat_id}")
async def ws_endpoint(websocket: WebSocket, chat_id: int, token: str = Query(default="")):
    """WebSocket stream for a chat_id. Receives all bot messages in real time.
    Connect: ws://host:port/ws/{chat_id}?token=YOUR_SECRET
    """
    # Auth check
    if API_SECRET and token != API_SECRET:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    if not _is_allowed(chat_id):
        await websocket.close(code=4003, reason="Chat ID not allowed")
        return

    await websocket.accept()
    key = str(chat_id)

    # Register
    with _ws_lock:
        if key not in _ws_clients:
            _ws_clients[key] = set()
        _ws_clients[key].add(websocket)
    print(f"[WS] Client connected for chat_id={chat_id}", flush=True)

    try:
        # Keep alive — read incoming messages (allows client to send commands too)
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                text = msg.get("text", "").strip()
                if text:
                    print(f"[WS] message from {chat_id}: {text[:80]}...", flush=True)
                    if text.startswith("/"):
                        _handle_command(chat_id, text)
                    else:
                        _handle_message(chat_id, text)
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps({"type": "error", "detail": "Invalid JSON"}))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[WS] Error for chat_id={chat_id}: {e}", flush=True)
    finally:
        with _ws_lock:
            clients = _ws_clients.get(key, set())
            clients.discard(websocket)
            if not clients:
                _ws_clients.pop(key, None)
        print(f"[WS] Client disconnected for chat_id={chat_id}", flush=True)


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
    print(f"  WebSocket: ws://{host}:{port}/ws/{{chat_id}}", flush=True)
    print(f"  API docs:  http://{host}:{port}/docs", flush=True)
