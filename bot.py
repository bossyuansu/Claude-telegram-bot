#!/usr/bin/env python3
"""
Telegram bot that forwards messages to Claude CLI with session support.
Supports interactive prompts, plan mode, and multiple working directories.
"""

import os
import subprocess
import requests
import time
import json
import threading
import uuid
from pathlib import Path
from datetime import datetime

# Configuration
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN_HERE")
ALLOWED_CHAT_IDS = os.environ.get("ALLOWED_CHAT_IDS", "").split(",")
BASE_PROJECTS_DIR = os.environ.get("PROJECTS_DIR", os.path.expanduser("~"))

# Pre-approved tools for Claude CLI (Option A: avoid permission prompts)
CLAUDE_ALLOWED_TOOLS = os.environ.get(
    "CLAUDE_ALLOWED_TOOLS",
    "Write,Edit,Bash,Read,Glob,Grep,Task,WebFetch,WebSearch,NotebookEdit,TodoWrite"
)

API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
DATA_DIR = Path(__file__).parent / "data"
SESSIONS_FILE = DATA_DIR / "sessions.json"

last_update_id = 0

# In-memory state
user_sessions = {}  # chat_id -> {sessions: [], active: session_id}
pending_questions = {}  # chat_id -> {questions, session, awaiting_text}
active_processes = {}  # session_id -> subprocess.Popen (allows parallel sessions)
message_queue = {}  # session_id -> [queued messages]


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


def get_updates(offset=0):
    """Poll for new messages and callback queries."""
    try:
        resp = requests.get(
            f"{API_URL}/getUpdates",
            params={"offset": offset, "timeout": 30},
            timeout=35
        )
        return resp.json().get("result", [])
    except Exception as e:
        print(f"Error getting updates: {e}")
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


def edit_message(chat_id, message_id, text, parse_mode="Markdown"):
    """Edit an existing message."""
    if not message_id:
        return

    # Truncate if too long
    if len(text) > 4000:
        text = text[:3997] + "..."

    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        resp = requests.post(f"{API_URL}/editMessageText", json=payload, timeout=10)
        result = resp.json()
        if not result.get("ok") and parse_mode:
            # Retry without markdown if parsing fails
            payload.pop("parse_mode", None)
            requests.post(f"{API_URL}/editMessageText", json=payload, timeout=10)
    except Exception as e:
        # Ignore edit errors (message not modified, etc.)
        pass


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
    cmd = ["claude", "-p", "--verbose", "--output-format", "stream-json"]

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

    cmd = ["claude", "-p", "--verbose", "--output-format", "stream-json"]

    # Add pre-approved tools to avoid permission prompts
    if CLAUDE_ALLOWED_TOOLS:
        cmd.extend(["--allowedTools", CLAUDE_ALLOWED_TOOLS])

    # Resume with Claude's session ID if available
    # Only use --resume with a valid session ID, never use --continue
    # (--continue resumes the global last conversation, which breaks new sessions)
    claude_session_id = session.get("claude_session_id") if session else None
    if claude_session_id:
        cmd.extend(["--resume", claude_session_id])

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
            stderr=subprocess.PIPE,
            text=True,
            cwd=work_dir,
            bufsize=1
        )

        # Track active process for cancellation (by session_id for parallel support)
        active_processes[process_key] = process

        for line in process.stdout:
            if not line.strip():
                continue

            try:
                data = json.loads(line)
                msg_type = data.get("type")

                # Capture Claude's session_id from init message
                if msg_type == "system" and data.get("subtype") == "init":
                    new_claude_session_id = data.get("session_id")

                if msg_type == "assistant":
                    content = data.get("message", {}).get("content", [])
                    for block in content:
                        if block.get("type") == "text":
                            text = block.get("text", "")
                            if text:
                                # Add spacing between text chunks if needed
                                spacing = ""
                                if accumulated_text and not accumulated_text.endswith('\n') and not text.startswith('\n'):
                                    # Check if we need a newline (new paragraph) or space
                                    if accumulated_text.endswith(('.', '!', '?', ':')):
                                        spacing = "\n\n"
                                    elif not accumulated_text.endswith(' '):
                                        spacing = " "
                                accumulated_text += spacing + text
                                current_chunk_text += spacing + text
                                current_tool = None

                                # Check if current chunk is getting too long - start new message
                                if len(current_chunk_text) > max_chunk_len:
                                    # Finalize current message (remove generating indicator)
                                    edit_message(chat_id, message_id, current_chunk_text.strip() + "\n\n———\n_continued..._")
                                    # Start new message for continuation
                                    message_id = send_message(chat_id, "⏳ _continuing..._")
                                    message_ids.append(message_id)
                                    current_chunk_text = ""
                                    last_update = time.time()

                                # Update message periodically
                                now = time.time()
                                if now - last_update >= update_interval and current_chunk_text.strip():
                                    edit_message(chat_id, message_id, current_chunk_text + "\n\n———\n⏳ _generating..._")
                                    last_update = now

                        elif block.get("type") == "tool_use":
                            tool_id = block.get("id")
                            tool_name = block.get("name")
                            tool_input = block.get("input", {})

                            # Skip if we've already processed this tool_use (prevents duplicates in streaming)
                            if tool_id and tool_id in processed_tool_ids:
                                continue
                            if tool_id:
                                processed_tool_ids.add(tool_id)

                            if tool_name == "AskUserQuestion":
                                questions.extend(tool_input.get("questions", []))
                            elif tool_name == "ExitPlanMode":
                                questions.append({
                                    "question": "Plan is ready. Do you approve this plan?",
                                    "header": "Plan Approval",
                                    "options": [
                                        {"label": "✅ Approve", "description": "Proceed with implementation"},
                                        {"label": "❌ Reject", "description": "Revise the plan"},
                                    ]
                                })
                            elif tool_name in ["Write", "Edit", "Bash", "Read", "Glob", "Grep"]:
                                path = tool_input.get("file_path") or tool_input.get("command") or tool_input.get("pattern") or ""
                                file_changes.append({
                                    "type": tool_name.lower(),
                                    "path": path[:100]
                                })
                                current_tool = tool_name
                                # Show tool activity
                                display_text = current_chunk_text or ""
                                status = f"\n\n🔧 _Running {tool_name}..._"
                                edit_message(chat_id, message_id, display_text + status)
                                last_update = time.time()

                elif msg_type == "result":
                    result_text = data.get("result", "")
                    # Use result text as it's properly formatted
                    if result_text:
                        accumulated_text = result_text
                        # For final result, put it in current chunk for display
                        current_chunk_text = result_text

            except json.JSONDecodeError:
                if line.strip() and not accumulated_text:
                    accumulated_text += line

        process.wait()

        # Check if cancelled
        cancelled = process_key not in active_processes

        # Clean up process tracking
        active_processes.pop(process_key, None)

        # Final update - no cursor, indicates completion
        # Use current_chunk_text for the last message (already chunked during streaming)
        final_chunk = current_chunk_text.strip() or "Done."

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
            edit_message(chat_id, message_id, final_chunk)
        else:
            # Final chunk is too long, need to split it
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
        edit_message(chat_id, message_id, "❌ _Error: Claude CLI not found_")
        return "Error: Claude CLI not found", [], message_id, None, False
    except Exception as e:
        active_processes.pop(process_key, None)
        error_text = accumulated_text + f"\n\n———\n❌ _Error: {e}_"
        context_overflow = ("prompt is too long" in str(e).lower() or
                           "context length" in str(e).lower() or
                           "too much media" in str(e).lower())
        if len(error_text) <= 4000:
            edit_message(chat_id, message_id, error_text)
        else:
            edit_message(chat_id, message_id, error_text[:3950] + "\n\n_(...truncated)_")
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
            save_sessions()
            break


def is_allowed(chat_id):
    """Check if the chat ID is allowed."""
    if not ALLOWED_CHAT_IDS or ALLOWED_CHAT_IDS == [""]:
        print("Warning: No ALLOWED_CHAT_IDS set. Allowing all users.")
        return True
    return str(chat_id) in ALLOWED_CHAT_IDS


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

*Other:*
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
        if not args:
            send_message(chat_id, "Usage: `/delete <session_name>`\nOr `/delete all` to clear all sessions.")
            return True

        target = args.strip().lower()
        chat_key = str(chat_id)
        user_data = user_sessions.get(chat_key, {})
        sessions = user_data.get("sessions", [])

        if target == "all":
            # Clear all sessions
            user_sessions[chat_key] = {"sessions": [], "active": None}
            save_sessions()
            send_message(chat_id, "🗑️ All sessions deleted.")
            return True

        # Find and delete matching session
        for i, s in enumerate(sessions):
            if s["name"].lower() == target or s["name"].lower().startswith(target):
                deleted_name = s["name"]
                sessions.pop(i)
                # Clear active if it was the deleted session
                if user_data.get("active") == get_session_id(s):
                    user_data["active"] = None
                save_sessions()
                send_message(chat_id, f"🗑️ Deleted session `{deleted_name}`")
                return True

        send_message(chat_id, f"❌ Session `{target}` not found. Use `/sessions` to list.")
        return True

    if cmd == "/status":
        session = get_active_session(chat_id)
        if session:
            session_id = get_session_id(session)
            is_busy = session_id in active_processes
            status = "🔄 Running" if is_busy else "✅ Idle"
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
        if session:
            session_id = get_session_id(session)
            process = active_processes.get(session_id)
            if process:
                try:
                    process.terminate()
                    active_processes.pop(session_id, None)
                    send_message(chat_id, f"⚠️ Cancelled operation for `{session['name']}`.")
                except:
                    send_message(chat_id, "Failed to cancel.")
            else:
                send_message(chat_id, f"No active task for session `{session['name']}`.")
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
            pending_questions[str(chat_id)] = {
                "questions": questions,
                "session": session
            }
            for q in questions:
                keyboard = create_inline_keyboard(q.get("options", []))
                send_message(chat_id, f"*{q.get('header', 'Question')}*\n\n{q['question']}",
                           reply_markup=keyboard)
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

    pending = pending_questions.get(chat_key)

    if not pending:
        send_message(chat_id, "This question has expired. Please try again.")
        return

    session = pending.get("session") or get_active_session(chat_id)

    if data == "opt_other":
        send_message(chat_id, "Please type your response:")
        pending_questions[chat_key]["awaiting_text"] = True
        return

    if data.startswith("opt_"):
        try:
            idx = int(data.split("_")[1])
            questions = pending.get("questions", [])
            if questions:
                options = questions[0].get("options", [])
                if idx < len(options):
                    selected = options[idx]
                    label = selected.get("label", selected) if isinstance(selected, dict) else str(selected)

                    send_message(chat_id, f"Selected: *{label}*")
                    send_typing(chat_id)

                    # Send response to Claude
                    if session:
                        response, new_questions = run_claude(
                            label,
                            cwd=session["cwd"],
                            continue_session=True
                        )

                        if new_questions:
                            pending_questions[chat_key] = {"questions": new_questions, "session": session}
                            for q in new_questions:
                                keyboard = create_inline_keyboard(q.get("options", []))
                                send_message(chat_id, f"*{q.get('header', 'Question')}*\n\n{q['question']}",
                                           reply_markup=keyboard)
                        else:
                            pending_questions.pop(chat_key, None)
                            if response:
                                send_message(chat_id, response)
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
                response, questions, _, claude_sid, context_overflow = run_claude_streaming(
                    text, chat_id, cwd=session["cwd"], continue_session=True,
                    session_id=session_id, session=session
                )

                # Smart compaction on context overflow: get summary, reset, retry with context
                if context_overflow:
                    send_message(chat_id, "⚠️ *Context too long* - compacting session...")

                    # First, ask Claude to summarize the conversation context (using old session)
                    summary_prompt = """Before we reset, provide a brief summary (max 500 words) of:
1. What we've been working on
2. Key decisions made
3. Current state/progress
4. Any important context for continuing

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

                    # Reset the session
                    update_claude_session_id(chat_id, session, None)

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
                pending_questions[chat_key] = {"questions": questions, "session": session}
                for q in questions:
                    keyboard = create_inline_keyboard(q.get("options", []))
                    send_message(chat_id, f"*{q.get('header', 'Question')}*\n\n{q['question']}",
                               reply_markup=keyboard)

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
    if message_queue.get(session_id):
        queued_text = message_queue[session_id].pop(0)
        # Run next queued message in thread
        run_claude_in_thread(chat_id, queued_text, session)


def handle_message(chat_id, text):
    """Handle a regular message."""
    chat_key = str(chat_id)

    # Check if awaiting text response for "Other" option
    pending = pending_questions.get(chat_key)
    if pending and pending.get("awaiting_text"):
        session = pending.get("session") or get_active_session(chat_id)
        pending_questions.pop(chat_key, None)

        if session:
            run_claude_in_thread(chat_id, text, session)
        return

    # Get active session
    session = get_active_session(chat_id)
    session_id = get_session_id(session) if session else str(chat_id)

    # Check if Claude is already running for THIS session (not all sessions)
    if session_id in active_processes:
        # Queue the message for this session
        if session_id not in message_queue:
            message_queue[session_id] = []
        message_queue[session_id].append(text)
        queue_pos = len(message_queue[session_id])
        session_name = session.get("name", "default") if session else "default"
        send_message(chat_id, f"📋 _Message queued (#{queue_pos}) for session `{session_name}`. Will process after current task._")
        return

    # Regular message - send to Claude with streaming in background
    run_claude_in_thread(chat_id, text, session)


def main():
    global last_update_id

    load_sessions()
    print("Claude Telegram Bot started!")
    print(f"Allowed chat IDs: {ALLOWED_CHAT_IDS}")
    print(f"Projects directory: {BASE_PROJECTS_DIR}")

    while True:
        updates = get_updates(last_update_id + 1)

        for update in updates:
            last_update_id = update["update_id"]

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
            text = message.get("text", "")

            if not chat_id or not text:
                continue

            if not is_allowed(chat_id):
                print(f"Unauthorized access attempt from chat_id: {chat_id}")
                send_message(chat_id, "Unauthorized. Your chat ID is not in the allowed list.")
                continue

            print(f"Received from {chat_id}: {text[:50]}...")

            # Handle commands
            if text.startswith("/"):
                if handle_command(chat_id, text):
                    continue

            # Handle regular messages
            handle_message(chat_id, text)

        time.sleep(1)


if __name__ == "__main__":
    main()
