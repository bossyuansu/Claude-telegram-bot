#!/usr/bin/env python3
"""
Telegram userbot that auto-responds in a specific chat using Ollama (Qwen3-VL).
Messages appear as sent by YOU, not a bot.

Setup:
1. Get api_id and api_hash from https://my.telegram.org
2. Add to .env file: TG_API_ID, TG_API_HASH, TARGET_CHAT_ID
3. Run: python userbot.py
4. First run will ask for phone number and code
"""

import os
import re
import glob
import json
import asyncio
import base64
import httpx
from pathlib import Path
from telethon import TelegramClient, events
from telethon.tl.types import PeerUser, PeerChat, PeerChannel

# Directory for downloaded media
UPLOADS_DIR = Path(__file__).parent / "data" / "userbot_uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB limit

# Load .env file
ENV_FILE = Path(__file__).parent / ".env"
if ENV_FILE.exists():
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())

# Configuration
API_ID = os.environ.get("TG_API_ID", "")
API_HASH = os.environ.get("TG_API_HASH", "")
TARGET_CHAT_ID = int(os.environ.get("TARGET_CHAT_ID", "0"))  # Chat to auto-respond in
SESSION_NAME = os.environ.get("TG_SESSION", "userbot_session")

# Ollama API configuration
OLLAMA_API_URL = os.environ.get("OLLAMA_API_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma3:12b")

# Working directory for code analysis
WORKING_DIR = Path(os.environ.get("CLAUDE_WORKING_DIR", os.getcwd()))

# System prompt
SYSTEM_PROMPT = os.environ.get("OLLAMA_SYSTEM_PROMPT", """You are 袁蘇 (Yuan Su), a software engineer and founder of NuOrbit. You're chatting with your girlfriend on Telegram. You have a daughter named 圓圓.

You will receive:
1. The latest message from your girlfriend
2. [Recent chat context:] - History of your conversation on various random topics (daily life, 圓圓, work frustrations, jokes, etc.)
3. [Project code context:] - Only included when discussing NuOrbit code

Your task: Respond naturally to the LATEST MESSAGE. Chat history is just for reference.

Your personality:
- Loving and supportive boyfriend/dad
- Busy with work but always make time to chat
- Playful, sometimes teasing, but always caring
- Tech-savvy but don't lecture unless asked

Response style:
- SHORT replies (1-3 sentences) like normal texting
- Match her language (Chinese/English)
- Match her mood - if she's venting, listen and empathize; if joking, joke back
- NO formatting (no headers, bullets, lists) for casual chat
- Emojis sparingly and naturally

Important:
- Respond to what she's CURRENTLY saying, not old topics
- Simple messages get simple responses ("😡" → "怎么啦？" not an essay)
- If she changes subject, follow naturally""")

# Bot response indicator
BOT_INDICATOR = os.environ.get("BOT_INDICATOR", "🤖")

# Optional: Only respond to specific users (leave empty to respond to all)
RESPOND_TO_USERS = os.environ.get("RESPOND_TO_USERS", "").split(",")  # Comma-separated user IDs

# Create client
client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

# State for message handling
is_processing = False  # Track if we're currently processing
pending_messages = []  # Queue of messages received while processing
thinking_msg = None  # Current "thinking" message to edit
recent_context = []  # Track recent messages (yours + theirs) for context
MAX_CONTEXT_MESSAGES = 20  # How many recent messages to include

# Session persistence
SESSION_FILE = Path(__file__).parent / "data" / "ollama_conversation.json"
SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
MAX_HISTORY_SIZE = 20  # Max messages to keep in history (smaller = less old pattern influence)


def load_conversation_history():
    """Load conversation history from file."""
    try:
        if SESSION_FILE.exists():
            data = json.loads(SESSION_FILE.read_text())
            return data.get("history", [])
    except Exception as e:
        print(f"Error loading conversation history: {e}")
    return []


def save_conversation_history(history):
    """Save conversation history to file."""
    try:
        SESSION_FILE.write_text(json.dumps({"history": history}, indent=2))
    except Exception as e:
        print(f"Error saving conversation history: {e}")


def clear_conversation_history():
    """Clear the conversation history."""
    global conversation_history
    conversation_history = []
    save_conversation_history([])


conversation_history = load_conversation_history()  # Load on startup


def encode_image_to_base64(image_path):
    """Encode an image file to base64 string."""
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def read_file(filepath):
    """Read a file from the working directory."""
    try:
        # Resolve path relative to working dir
        if not os.path.isabs(filepath):
            filepath = WORKING_DIR / filepath
        else:
            filepath = Path(filepath)

        # Security: ensure file is within working directory
        try:
            filepath.resolve().relative_to(WORKING_DIR.resolve())
        except ValueError:
            return None, "File is outside the project directory"

        # Check sensitive files
        name_lower = filepath.name.lower()
        if name_lower == '.env' or 'secret' in name_lower or 'credential' in name_lower:
            return None, "Cannot read sensitive files"

        if not filepath.exists():
            return None, f"File not found: {filepath}"

        if filepath.stat().st_size > 100000:  # 100KB limit
            return None, "File too large (>100KB)"

        content = filepath.read_text()
        return content, None
    except Exception as e:
        return None, str(e)


def find_files(pattern):
    """Find files matching a glob pattern in working directory."""
    try:
        matches = list(WORKING_DIR.glob(pattern))
        # Also try recursive
        if not matches:
            matches = list(WORKING_DIR.glob(f"**/{pattern}"))
        # Filter out hidden and node_modules
        matches = [m for m in matches if not any(p.startswith('.') or p == 'node_modules' for p in m.parts)]
        return [str(m.relative_to(WORKING_DIR)) for m in matches[:20]]  # Limit to 20
    except Exception:
        return []


def search_code(pattern, file_pattern="*.py"):
    """Search for a pattern in code files."""
    results = []
    try:
        for filepath in WORKING_DIR.glob(f"**/{file_pattern}"):
            if any(p.startswith('.') or p == 'node_modules' for p in filepath.parts):
                continue
            try:
                content = filepath.read_text()
                for i, line in enumerate(content.split('\n'), 1):
                    if pattern.lower() in line.lower():
                        results.append(f"{filepath.relative_to(WORKING_DIR)}:{i}: {line.strip()[:100]}")
                        if len(results) >= 20:
                            return results
            except:
                continue
    except:
        pass
    return results


def extract_file_requests(text):
    """Extract file read/search requests from user message."""
    text_lower = text.lower()

    # Patterns that suggest user wants to see code
    read_patterns = [
        r'(?:read|show|open|cat|view|look at|check|see)\s+(?:the\s+)?(?:file\s+)?["\']?([^\s"\']+\.\w+)["\']?',
        r'(?:what\'s in|contents? of)\s+["\']?([^\s"\']+\.\w+)["\']?',
        r'["\']([^\s"\']+\.\w+)["\']',  # Quoted filename
    ]

    files_to_read = []
    for pattern in read_patterns:
        matches = re.findall(pattern, text_lower)
        files_to_read.extend(matches)

    # Search patterns
    search_match = re.search(r'(?:search|find|grep|look for)\s+["\']?([^"\']+)["\']?\s+(?:in|across)', text_lower)
    search_term = search_match.group(1) if search_match else None

    # List files patterns
    list_match = re.search(r'(?:list|show)\s+(?:all\s+)?(?:files|\.(\w+)\s+files)', text_lower)
    list_pattern = f"*.{list_match.group(1)}" if list_match and list_match.group(1) else None

    return files_to_read, search_term, list_pattern


def detect_code_question(text):
    """Detect if user is asking about code/product features."""
    text_lower = text.lower()

    # Strong indicators - definitely about code/project
    strong_code_keywords = [
        'function', 'class', 'method', 'api', 'endpoint', 'route', 'handler',
        'controller', 'service', 'model', 'schema', 'database', 'query',
        'authentication', 'auth', 'login', 'payment', 'transaction',
        'error', 'bug', 'issue', 'fix', 'debug', 'logic', 'flow', 'process',
        'config', 'setting', 'module', 'component', 'feature', 'code',
        'implement', 'refactor', 'deploy', 'build', 'test', 'compile',
        'repository', 'repo', 'commit', 'branch', 'merge', 'pull request',
        'nuorbit', 'backend', 'frontend', 'server', 'client', 'sdk',
        '.py', '.ts', '.js', '.go', '.rs', '.java', '.tsx', '.jsx'
    ]

    # Check strong keywords - these definitely indicate code discussion
    for keyword in strong_code_keywords:
        if keyword in text_lower:
            return True

    # Weak indicators - only count if combined with question patterns
    weak_code_keywords = [
        'user', 'work', 'works', 'working'
    ]

    question_patterns = [
        'how does', 'how do', 'where is', 'where does', 'what is', 'what does',
        'explain the', 'show me the', 'find the', 'search for', 'look at the',
        'can you check', 'analyze the', 'review the'
    ]

    # Check if has question pattern + weak keyword
    has_question = any(p in text_lower for p in question_patterns)
    has_weak_keyword = any(k in text_lower for k in weak_code_keywords)

    if has_question and has_weak_keyword:
        # Extra check: avoid casual questions like "how does this look?" "what is this?"
        casual_patterns = [
            'how are you', 'what is this', 'what do you think', 'how does this look',
            'what should', 'how should', 'do you', 'can you help', 'are you',
            'what time', 'when', 'who', 'whose'
        ]
        if any(p in text_lower for p in casual_patterns):
            return False
        return True

    return False


def extract_topic_keywords(text):
    """Extract potential topic keywords from user question."""
    text_lower = text.lower()

    # Remove common words
    stop_words = {
        'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
        'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
        'should', 'may', 'might', 'must', 'shall', 'can', 'need', 'dare',
        'ought', 'used', 'to', 'of', 'in', 'for', 'on', 'with', 'at', 'by',
        'from', 'as', 'into', 'through', 'during', 'before', 'after', 'above',
        'below', 'between', 'under', 'again', 'further', 'then', 'once', 'here',
        'there', 'when', 'where', 'why', 'how', 'all', 'each', 'few', 'more',
        'most', 'other', 'some', 'such', 'no', 'nor', 'not', 'only', 'own',
        'same', 'so', 'than', 'too', 'very', 'just', 'and', 'but', 'if', 'or',
        'because', 'until', 'while', 'this', 'that', 'these', 'those', 'what',
        'which', 'who', 'whom', 'i', 'me', 'my', 'we', 'our', 'you', 'your',
        'he', 'him', 'his', 'she', 'her', 'it', 'its', 'they', 'them', 'their',
        'show', 'tell', 'explain', 'find', 'look', 'work', 'works', 'about'
    }

    # Extract words
    words = re.findall(r'\b[a-z_][a-z0-9_]*\b', text_lower)
    keywords = [w for w in words if w not in stop_words and len(w) > 2]

    return keywords[:5]  # Top 5 keywords


def auto_find_relevant_files(keywords):
    """Automatically find files relevant to the keywords."""
    relevant_files = []

    # Common file extensions to search
    extensions = ['*.py', '*.ts', '*.js', '*.go', '*.rs', '*.java', '*.tsx', '*.jsx']

    for keyword in keywords:
        # Search in filenames
        for ext in extensions:
            matches = find_files(f"*{keyword}*{ext[1:]}")  # e.g., *payment*.py
            relevant_files.extend(matches)

        # Search in code content
        for ext in extensions:
            results = search_code(keyword, ext)
            for result in results[:3]:
                # Extract filename from result
                filename = result.split(':')[0]
                if filename not in relevant_files:
                    relevant_files.append(filename)

        if len(relevant_files) >= 5:
            break

    return list(dict.fromkeys(relevant_files))[:3]  # Dedupe, limit to 3


def get_project_overview():
    """Get project overview from CLAUDE.md or claude.md."""
    for filename in ['CLAUDE.md', 'claude.md', 'README.md']:
        content, error = read_file(filename)
        if content:
            # Truncate if too long
            if len(content) > 3000:
                content = content[:3000] + "\n... (truncated)"
            return content
    return None


def augment_prompt_with_code(prompt, chat_context=None):
    """Augment the prompt with relevant code if user asks about files.

    Separates:
    - chat_context: Recent conversation history (always included if provided)
    - project_context: Code files and project info (only for code questions)
    """
    files_to_read, search_term, list_pattern = extract_file_requests(prompt)

    augmented = prompt
    project_context = []
    is_code_question = detect_code_question(prompt) or files_to_read or search_term or list_pattern

    # Only include project context for code-related questions
    if is_code_question:
        # Include project overview for code questions
        overview = get_project_overview()
        if overview:
            project_context.append(f"=== Project Overview (CLAUDE.md) ===\n{overview}")

        # Read explicitly requested files
        for filename in files_to_read[:3]:  # Limit to 3 files
            content, error = read_file(filename)
            if content:
                project_context.append(f"=== File: {filename} ===\n```\n{content[:8000]}\n```")
            elif error:
                project_context.append(f"=== Could not read {filename}: {error} ===")

        # Search for code
        if search_term:
            results = search_code(search_term)
            if results:
                project_context.append(f"=== Search results for '{search_term}' ===\n" + "\n".join(results))

        # List files
        if list_pattern:
            files = find_files(list_pattern)
            if files:
                project_context.append(f"=== Files matching {list_pattern} ===\n" + "\n".join(files))

        # If no explicit file request but asking about code/product, auto-find relevant files
        if len(project_context) <= 1:  # Only has overview or nothing
            keywords = extract_topic_keywords(prompt)
            if keywords:
                relevant_files = auto_find_relevant_files(keywords)
                for filename in relevant_files:
                    content, error = read_file(filename)
                    if content:
                        # Truncate to keep context manageable
                        truncated = content[:4000] + "\n... (truncated)" if len(content) > 4000 else content
                        project_context.append(f"=== Relevant file: {filename} ===\n```\n{truncated}\n```")

    # Build the augmented prompt with clear separation
    parts = [prompt]

    # Add chat context (recent conversation) - always included if provided
    if chat_context:
        parts.append(f"\n\n[Recent chat context:]\n{chat_context}")

    # Add project context (code/files) - only for code questions
    if project_context:
        parts.append(f"\n\n[Project code context:]\n" + "\n\n".join(project_context))

    return "".join(parts)


async def query_ollama(prompt, images=None, stream_callback=None):
    """Query Ollama API with streaming."""
    global conversation_history

    # Build messages for chat endpoint
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Add conversation history
    messages.extend(conversation_history)

    # Build current message content
    if images:
        # Ollama vision model format - images as base64 in the message
        img_list = []
        for img_path in images:
            if img_path and os.path.exists(img_path):
                b64_image = encode_image_to_base64(img_path)
                img_list.append(b64_image)
        messages.append({"role": "user", "content": prompt, "images": img_list})
    else:
        messages.append({"role": "user", "content": prompt})

    payload = {
        "model": OLLAMA_MODEL,
        "messages": messages,
        "stream": True,
    }

    accumulated_text = ""

    try:
        async with httpx.AsyncClient(timeout=300.0) as http_client:
            async with http_client.stream(
                "POST",
                f"{OLLAMA_API_URL}/api/chat",
                json=payload,
            ) as response:
                if response.status_code != 200:
                    error_text = await response.aread()
                    return f"Error from Ollama: {response.status_code} - {error_text.decode()}"

                async for line in response.aiter_lines():
                    if not line:
                        continue

                    try:
                        data = json.loads(line)
                        if "message" in data and "content" in data["message"]:
                            chunk = data["message"]["content"]
                            if chunk:
                                accumulated_text += chunk

                                if stream_callback:
                                    await stream_callback(accumulated_text)

                        if data.get("done", False):
                            break
                    except json.JSONDecodeError:
                        continue

        # Clean up thinking tags if present (Qwen3 uses these)
        if "<think>" in accumulated_text:
            # Remove thinking content
            import re
            accumulated_text = re.sub(r'<think>.*?</think>', '', accumulated_text, flags=re.DOTALL).strip()

        # Update conversation history (store without code context to save space)
        clean_prompt = prompt.split("\n\n[Recent chat context:]")[0]
        conversation_history.append({"role": "user", "content": clean_prompt})
        conversation_history.append({"role": "assistant", "content": accumulated_text})

        # Keep history manageable
        if len(conversation_history) > MAX_HISTORY_SIZE:
            conversation_history = conversation_history[-MAX_HISTORY_SIZE:]

        # Persist to disk
        save_conversation_history(conversation_history)

        return accumulated_text.strip() or "No response from model."

    except httpx.TimeoutException:
        return "Error: Request timed out."
    except httpx.ConnectError:
        return "Error: Could not connect to Ollama. Is it running?"
    except Exception as e:
        return f"Error: {e}"


@client.on(events.NewMessage(chats=TARGET_CHAT_ID if TARGET_CHAT_ID else None, outgoing=True))
async def outgoing_handler(event):
    """Track your own messages for context."""
    global recent_context

    text = event.text
    if not text:
        return

    # Skip bot responses (they have the indicator)
    if text.startswith(BOT_INDICATOR):
        return

    # Add to context
    recent_context.append(f"You: {text}")
    if len(recent_context) > MAX_CONTEXT_MESSAGES:
        recent_context = recent_context[-MAX_CONTEXT_MESSAGES:]

    print(f"Tracked your message: {text[:50]}...")


@client.on(events.NewMessage(chats=TARGET_CHAT_ID if TARGET_CHAT_ID else None, incoming=True))
async def handler(event):
    """Handle incoming messages in target chat."""
    global pending_messages, thinking_msg, recent_context, is_processing

    # Skip if no target chat configured
    if not TARGET_CHAT_ID:
        return

    # Skip messages from yourself
    if event.out:
        return

    # Optional: Only respond to specific users
    if RESPOND_TO_USERS and RESPOND_TO_USERS != [""]:
        sender_id = str(event.sender_id)
        if sender_id not in RESPOND_TO_USERS:
            return

    # Get message text, sticker, or media
    text = event.text or event.raw_text or ""
    caption = event.message.message or ""  # Caption for media
    sticker = event.sticker
    media_path = None

    # Handle special commands
    if text.lower() in ['/reset', '/clear', '/new']:
        clear_conversation_history()
        await event.reply(f"{BOT_INDICATOR} 🔄 Conversation cleared. Starting fresh!")
        return

    if sticker:
        # Describe the sticker - handle different sticker types
        emoji = "?"
        if hasattr(sticker, 'alt') and sticker.alt:
            emoji = sticker.alt
        elif hasattr(event.message, 'media') and hasattr(event.message.media, 'emoticon'):
            emoji = event.message.media.emoticon or "?"
        text = f"[Sticker: {emoji}]"
    elif event.photo:
        # Download photo
        try:
            media_path = await event.download_media(file=UPLOADS_DIR)
            if media_path:
                text = caption or "What's in this image?"
                print(f"Downloaded photo to {media_path}")
        except Exception as e:
            print(f"Error downloading photo: {e}")
            text = f"[Photo received but failed to download: {e}]"
    elif event.document:
        # Handle documents (files, audio, video, etc.)
        doc = event.document
        file_size = doc.size if doc.size else 0

        # Get filename from attributes
        file_name = "file"
        for attr in doc.attributes:
            if hasattr(attr, 'file_name') and attr.file_name:
                file_name = attr.file_name
                break

        # Check file type
        mime_type = doc.mime_type or ""
        is_image = mime_type.startswith("image/")
        is_audio = mime_type.startswith("audio/") or event.voice or event.audio
        is_video = mime_type.startswith("video/") or event.video or event.video_note

        if file_size > MAX_FILE_SIZE:
            text = f"[File too large: {file_name} ({file_size / 1024 / 1024:.1f}MB > 50MB limit)]"
        elif is_image:
            try:
                media_path = await event.download_media(file=UPLOADS_DIR)
                if media_path:
                    text = caption or "What's in this image?"
                    print(f"Downloaded image {file_name} to {media_path}")
            except Exception as e:
                print(f"Error downloading image: {e}")
                text = f"[Image received but failed to download: {e}]"
        else:
            # Non-image files - just acknowledge
            if is_audio:
                text = f"[Audio file received: {file_name} - I can't process audio yet]"
            elif is_video:
                text = f"[Video file received: {file_name} - I can't process video yet]"
            else:
                text = f"[File received: {file_name} - I can only analyze images currently]"
    elif not text:
        return

    # Add their message to context
    context_text = text[:100] + "..." if len(text) > 100 else text
    recent_context.append(f"Them: {context_text}")
    if len(recent_context) > MAX_CONTEXT_MESSAGES:
        recent_context = recent_context[-MAX_CONTEXT_MESSAGES:]

    print(f"Received from {event.sender_id}: {text[:50]}...")

    # If already processing, queue this message
    if is_processing:
        pending_messages.append((text, media_path))
        print(f"Queued message (processing in progress)")
        return

    # Process messages
    await process_messages(event, text, media_path)


async def process_messages(event, initial_text, media_path=None):
    """Process message(s) with Ollama."""
    global pending_messages, thinking_msg, recent_context, is_processing

    is_processing = True

    try:
        # Combine initial text with any pending messages
        all_texts = [initial_text]
        all_media = [media_path] if media_path else []

        for item in pending_messages:
            if isinstance(item, tuple):
                all_texts.append(item[0])
                if item[1]:
                    all_media.append(item[1])
            else:
                all_texts.append(item)

        pending_messages = []
        thinking_msg = None

        if len(all_texts) > 1:
            combined = "\n\n".join(all_texts)
            prompt = f"[Multiple messages:]\n{combined}"
        else:
            prompt = all_texts[0]

        # Build chat context from recent messages (separate from project context)
        # Let the LLM decide what's relevant from the context
        chat_context = None
        if recent_context:
            chat_context = "\n".join(recent_context[-10:])

        # Augment prompt with contexts (chat context always, project context only for code questions)
        prompt = augment_prompt_with_code(prompt, chat_context=chat_context)

        # Send immediate "thinking" message
        initial_msg = await event.reply(f"{BOT_INDICATOR} ⏳ _thinking..._")
        thinking_msg = initial_msg
        current_msg = [initial_msg]

        # Rate limit updates
        last_update = [0]
        last_text = [""]
        max_chunk_len = 3500

        async def update_message(text):
            now = asyncio.get_event_loop().time()

            # Only update every 1.5 seconds and if text changed
            if now - last_update[0] >= 1.5 and text != last_text[0]:
                try:
                    display_text = text
                    if len(text) > max_chunk_len:
                        display_text = text[-max_chunk_len:]

                    full_text = f"{BOT_INDICATOR} {display_text}\n\n———\n⏳ _generating..._"
                    if len(full_text) > 4000:
                        full_text = full_text[:3950] + "\n\n_(...generating)_"
                    await current_msg[0].edit(full_text)
                    last_update[0] = now
                    last_text[0] = text
                except Exception as e:
                    pass

        # Query Ollama with streaming
        response = await query_ollama(
            prompt,
            images=all_media if all_media else None,
            stream_callback=update_message
        )

        # Final update with response
        if response:
            full_response = f"{BOT_INDICATOR} {response}"

            # Chunk long responses
            max_len = 3900
            try:
                if len(full_response) <= max_len:
                    await current_msg[0].edit(full_response)
                else:
                    # For long responses, delete current msg and send fresh chunked messages
                    await current_msg[0].delete()
                    chunks = [full_response[i:i + max_len] for i in range(0, len(full_response), max_len)]
                    for i, chunk in enumerate(chunks):
                        if i == 0:
                            await event.reply(chunk)
                        else:
                            await client.send_message(event.chat_id, chunk)
                        await asyncio.sleep(0.5)
            except Exception as e:
                # Handle FloodWait or other errors - try sending new message instead
                error_str = str(e).lower()
                if 'flood' in error_str or 'wait' in error_str:
                    print(f"FloodWait error, waiting and sending new message...")
                    await asyncio.sleep(5)
                    try:
                        await current_msg[0].delete()
                    except:
                        pass
                    await event.reply(full_response[:max_len])
                else:
                    print(f"Error editing message: {e}")

            print(f"Replied: {response[:50]}... (total {len(full_response)} chars)")
        else:
            try:
                await current_msg[0].edit(f"{BOT_INDICATOR} No response")
            except:
                pass

    finally:
        is_processing = False

        # Process any queued messages
        if pending_messages:
            first_pending = pending_messages.pop(0)
            if isinstance(first_pending, tuple):
                await process_messages(event, first_pending[0], first_pending[1])
            else:
                await process_messages(event, first_pending)


async def main():
    """Main entry point."""
    if not API_ID or not API_HASH:
        print("Error: TG_API_ID and TG_API_HASH must be set")
        print("\nGet them from https://my.telegram.org:")
        print("1. Log in with your phone number")
        print("2. Go to 'API development tools'")
        print("3. Create an application")
        print("4. Copy api_id and api_hash")
        print("\nThen set environment variables:")
        print("  export TG_API_ID='your_api_id'")
        print("  export TG_API_HASH='your_api_hash'")
        print("  export TARGET_CHAT_ID='chat_id_to_monitor'")
        return

    if not TARGET_CHAT_ID:
        print("Error: TARGET_CHAT_ID must be set")
        print("\nTo find a chat ID:")
        print("1. Forward a message from that chat to @userinfobot")
        print("2. Or use the list_chats mode below")
        print("\nSet: export TARGET_CHAT_ID='123456789'")
        return

    print(f"Starting userbot with Ollama...")
    print(f"Target chat ID: {TARGET_CHAT_ID}")
    print(f"Model: {OLLAMA_MODEL}")
    print(f"Ollama URL: {OLLAMA_API_URL}")

    await client.start()
    print("Connected! Listening for messages...")
    print("Press Ctrl+C to stop")

    await client.run_until_disconnected()


if __name__ == "__main__":
    import sys

    # Helper mode to list chats
    if len(sys.argv) > 1 and sys.argv[1] == "list_chats":
        async def list_chats():
            if not API_ID or not API_HASH:
                print("Set TG_API_ID and TG_API_HASH first")
                return
            await client.start()
            print("\nYour recent chats:\n")
            async for dialog in client.iter_dialogs(limit=20):
                chat_type = "User" if dialog.is_user else ("Group" if dialog.is_group else "Channel")
                print(f"  {chat_type:8} | ID: {dialog.id:15} | {dialog.name}")
            print("\nUse the ID as TARGET_CHAT_ID")

        asyncio.run(list_chats())
    else:
        asyncio.run(main())
