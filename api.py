"""
HTTP API server for Claude Telegram Bot.
Runs alongside the Telegram polling loop in the same process,
sharing all in-memory state. Listens on the Tailscale IP.
"""
import os
import threading

from fastapi import FastAPI, HTTPException, Depends, Header
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
        "threads": threading.active_count(),
    }


def start(host: str, port: int):
    """Start the API server in a background daemon thread."""
    import uvicorn

    def _run():
        uvicorn.run(app, host=host, port=port, log_level="warning")

    t = threading.Thread(target=_run, daemon=True, name="api-server")
    t.start()
    print(f"API server listening on http://{host}:{port}", flush=True)
    print(f"API docs: http://{host}:{port}/docs", flush=True)
