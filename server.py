#!/usr/bin/env python3
"""Hermes Live Transcript — local web viewer for the current conversation.

Displays the Hermes session transcript in real-time on a local web page.
Reads directly from the Hermes state.db SQLite database.

Usage:
    python3 live-transcript.py [port]
    Open http://127.0.0.1:PORT in your browser
"""

import json
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import urllib.error
import urllib.request

PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8800
STATE_DB = Path.home() / ".hermes" / "state.db"
BUS_LOG_DB = Path.home() / ".hermes" / "agent-bus-log.db"
ARCHIVE_DIR = Path.home() / ".hermes" / "live-transcript-archives"
HERMES_API_BASE_URL = os.getenv("HERMES_API_BASE_URL", "http://127.0.0.1:8642")
HERMES_API_KEY_FILE = Path.home() / ".hermes" / "live-transcript-api-key"
HERMES_CONFIG_FILE = Path.home() / ".hermes" / "config.yaml"
HERMES_ENV_FILE = Path.home() / ".hermes" / ".env"
AGENTTALK_API_BASE_URL = os.getenv("AGENTTALK_API_BASE_URL", "http://127.0.0.1:3741")
PROJECT_PATH = Path(os.getenv("HERMES_LIVE_PROJECT_PATH") or os.getenv("HERMES_PROJECT_PATH") or Path.cwd()).expanduser().resolve()
PROJECT_NAME = (
    os.getenv("HERMES_LIVE_PROJECT_NAME")
    or os.getenv("HERMES_PROJECT_NAME")
    or PROJECT_PATH.name
    or "project"
).strip()
if not PROJECT_NAME:
    PROJECT_NAME = "project"
LINCHPIN_DOCS = [
    "AGENT.md",
    "design/collaboration-workflow.md",
]
MAX_SEND_CHARS = 20000
MAX_ARCHIVE_MESSAGES = 200
MAX_ARCHIVE_BODY_BYTES = 1024 * 1024
LIVE_MESSAGE_TTL_SECONDS = 20 * 60
MAX_LIVE_MESSAGES_PER_SESSION = 20
MAX_SESSION_TITLE_LENGTH = 100

# Dev mode: shorter poll, verbose logging
DEV_MODE = "--dev" in sys.argv
POLL_INTERVAL = 1000 if DEV_MODE else 3000  # ms
DEAD_AGENT_HIDE_AFTER_SECONDS = 20 * 60

if DEV_MODE:
    print(f"  ⚠ DEV MODE — poll every {POLL_INTERVAL}ms")

# Cache: pinned session ID, updated on initial poll only
_pinned_session_id: str | None = None
_reported_errors: set[str] = set()
_live_lock = threading.Lock()
_live_messages_by_session: dict[str, dict[str, dict]] = {}


def today_midnight_ts() -> float:
    """Return Unix timestamp for the start of today in the local timezone."""
    now = datetime.now().astimezone()
    return datetime(now.year, now.month, now.day, tzinfo=now.tzinfo).timestamp()


def log_exception(context: str):
    if context in _reported_errors:
        return
    _reported_errors.add(context)
    print(f"[Hermes Live] {context}", file=sys.stderr)
    traceback.print_exc()


def normalize_message_content(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def message_content_matches(live_content: object, committed_content: object, live_completed: bool = False) -> bool:
    live = normalize_message_content(live_content)
    committed = normalize_message_content(committed_content)
    if not live or not committed:
        return False
    if live == committed:
        return True

    shorter = min(len(live), len(committed))
    longer = max(len(live), len(committed))
    if shorter < 24:
        return False

    if committed.startswith(live):
        return not live_completed or len(live) >= 48 or shorter / longer >= 0.65
    if live.startswith(committed):
        return shorter / longer >= 0.65
    return False


def archive_timestamp(value: object) -> float | None:
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    return ts


def format_archive_timestamp(value: object, for_filename: bool = False) -> str:
    ts = archive_timestamp(value)
    if ts is None:
        return "unknown"
    dt = datetime.fromtimestamp(ts).astimezone()
    if for_filename:
        return dt.strftime("%Y%m%d-%H%M%S")
    return dt.isoformat(timespec="seconds")


def human_timestamp(dt: datetime | None = None) -> str:
    return (dt or datetime.now().astimezone()).strftime("%Y-%m-%d %H:%M:%S")


def compact_project_name(max_len: int) -> str:
    name = re.sub(r"\s+", " ", PROJECT_NAME).strip() or "project"
    if len(name) <= max_len:
        return name
    return name[:max_len].rstrip(" .-_") or "project"


def project_session_title(dt: datetime | None = None) -> str:
    stamp = human_timestamp(dt)
    separator = " - "
    max_name_len = MAX_SESSION_TITLE_LENGTH - len(separator) - len(stamp)
    return f"{compact_project_name(max_name_len)}{separator}{stamp}"


def project_context_prompt() -> str:
    docs = "\n".join(f"- {doc}: {PROJECT_PATH / doc}" for doc in LINCHPIN_DOCS)
    return (
        "Current development project context:\n"
        f"- Project name: {PROJECT_NAME}\n"
        f"- Project path: {PROJECT_PATH}\n"
        "- Linchpin docs to read at startup:\n"
        f"{docs}\n\n"
        "Use this as the working project context for this development session."
    )


def safe_archive_filename(value: object) -> str:
    name = str(value or "").strip()
    name = name.replace("/", "-").replace("\\", "-")
    name = re.sub(r"[^A-Za-z0-9._ -]+", "-", name)
    name = re.sub(r"\s+", " ", name).strip(" .-_")
    if not name:
        name = "hermes-transcript-archive"
    if not name.lower().endswith(".md"):
        name = f"{name}.md"
    return name[:180]


def default_archive_filename(dt: datetime | None = None) -> str:
    return safe_archive_filename(project_session_title(dt))


def render_archive_markdown(archive_id: str, session_id: str, messages: list[dict]) -> str:
    first_ts = messages[0].get("timestamp") if messages else None
    last_ts = messages[-1].get("timestamp") if messages else None
    title = (
        f"Hermes transcript archive "
        f"{format_archive_timestamp(first_ts)} to {format_archive_timestamp(last_ts)}"
    )
    if session_id:
        title += f" ({session_id})"

    lines = [
        f"# {title}",
        "",
        f"- Archive id: `{archive_id}`",
        f"- Session id: `{session_id or 'unknown'}`",
        f"- Project name: `{PROJECT_NAME}`",
        f"- Project path: `{PROJECT_PATH}`",
        f"- Linchpin docs: `{', '.join(LINCHPIN_DOCS)}`",
        f"- First message: `{format_archive_timestamp(first_ts)}`",
        f"- Last message: `{format_archive_timestamp(last_ts)}`",
        f"- Message count: `{len(messages)}`",
        f"- Created at: `{datetime.now().astimezone().isoformat(timespec='seconds')}`",
        "",
        "## Messages",
        "",
    ]

    for msg in messages:
        display = str(msg.get("display") or msg.get("role") or "message")
        timestamp = format_archive_timestamp(msg.get("timestamp"))
        message_id = str(msg.get("id") or msg.get("bus_id") or "")
        status = []
        if msg.get("live"):
            status.append("live")
        if msg.get("pending"):
            status.append("pending")
        suffix = f" [{' / '.join(status)}]" if status else ""
        id_part = f" `{message_id}`" if message_id else ""
        lines.extend([
            f"### {display}{suffix} - {timestamp}{id_part}",
            "",
            "```text",
            str(msg.get("content") or ""),
            "```",
            "",
        ])
    return "\n".join(lines)


def archive_visible_messages(data: dict) -> dict:
    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("no messages to archive")
    if len(messages) > MAX_ARCHIVE_MESSAGES:
        raise ValueError(f"too many messages to archive; max {MAX_ARCHIVE_MESSAGES}")

    cleaned = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "")
        if not content:
            continue
        cleaned.append({
            "id": item.get("id"),
            "bus_id": item.get("bus_id"),
            "role": str(item.get("role") or ""),
            "display": str(item.get("display") or item.get("role") or "message"),
            "content": content[:MAX_SEND_CHARS],
            "timestamp": archive_timestamp(item.get("timestamp")),
            "live": bool(item.get("live")),
            "pending": bool(item.get("pending")),
        })
    if not cleaned:
        raise ValueError("no non-empty messages to archive")

    session_id = str(data.get("session_id") or "")
    archive_id = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    filename = safe_archive_filename(data.get("filename") or default_archive_filename())
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    path = ARCHIVE_DIR / filename
    if path.exists():
        stem = path.stem
        suffix = path.suffix or ".md"
        path = ARCHIVE_DIR / f"{stem}-{archive_id}{suffix}"

    content = render_archive_markdown(archive_id, session_id, cleaned)
    path.write_text(content, encoding="utf-8")
    return {
        "archive_id": archive_id,
        "path": str(path),
        "filename": path.name,
        "message_count": len(cleaned),
    }


def prune_live_messages_locked(now: float | None = None):
    now = now or time.time()
    empty_sessions = []
    for session_id, messages in _live_messages_by_session.items():
        stale_keys = [
            key for key, msg in messages.items()
            if now - float(msg.get("updated_at") or msg.get("timestamp") or 0) > LIVE_MESSAGE_TTL_SECONDS
        ]
        for key in stale_keys:
            messages.pop(key, None)
        if not messages:
            empty_sessions.append(session_id)
    for session_id in empty_sessions:
        _live_messages_by_session.pop(session_id, None)


def live_message_key(message_id: str | None) -> str:
    return f"live:{message_id or 'assistant'}"


def upsert_live_message(session_id: str, message_id: str | None, **updates):
    if not session_id:
        return
    now = time.time()
    key = live_message_key(message_id)
    with _live_lock:
        prune_live_messages_locked(now)
        session_messages = _live_messages_by_session.setdefault(session_id, {})
        msg = session_messages.get(key)
        if msg is None:
            msg = {
                "id": key,
                "session_id": session_id,
                "role": "assistant",
                "display": "hermes",
                "content": "",
                "timestamp": now,
                "live": True,
                "completed": False,
                "source": "hermes_sse",
            }
            session_messages[key] = msg
        msg.update(updates)
        msg["updated_at"] = now

        while len(session_messages) > MAX_LIVE_MESSAGES_PER_SESSION:
            oldest_key = min(
                session_messages,
                key=lambda k: float(session_messages[k].get("updated_at") or session_messages[k].get("timestamp") or 0),
            )
            session_messages.pop(oldest_key, None)


def append_live_delta(session_id: str, message_id: str | None, delta: str):
    if not delta:
        return
    key = live_message_key(message_id)
    with _live_lock:
        existing = _live_messages_by_session.get(session_id, {}).get(key, {})
        content = str(existing.get("content") or "") + delta
    upsert_live_message(session_id, message_id, content=content, completed=False)


def reconcile_live_messages_locked(session_id: str, committed_messages: list[dict]):
    session_messages = _live_messages_by_session.get(session_id)
    if not session_messages:
        return

    committed = []
    for msg in committed_messages:
        if msg.get("is_bus") or msg.get("live"):
            continue
        role = str(msg.get("role") or "").lower()
        display = str(msg.get("display") or "").lower()
        if role not in ("user", "assistant") and display not in ("human", "hermes"):
            continue
        content = normalize_message_content(msg.get("content"))
        if content:
            committed.append((role, display, content))

    if not committed:
        return

    matched_keys = []
    for key, live in session_messages.items():
        if not normalize_message_content(live.get("content")):
            continue
        live_role = str(live.get("role") or "").lower()
        live_display = str(live.get("display") or "").lower()
        live_completed = bool(live.get("completed"))
        for role, display, content in committed:
            same_role = (
                role == live_role
                or display == live_display
                or (role == "assistant" and live_display == "hermes")
                or (role == "user" and live_display == "human")
            )
            if not same_role:
                continue
            if message_content_matches(live.get("content"), content, live_completed):
                matched_keys.append(key)
                break

    for key in matched_keys:
        session_messages.pop(key, None)
    if not session_messages:
        _live_messages_by_session.pop(session_id, None)


def get_live_messages(session_id: str, committed_messages: list[dict] | None = None) -> list[dict]:
    with _live_lock:
        prune_live_messages_locked()
        if committed_messages:
            reconcile_live_messages_locked(session_id, committed_messages)
        live = []
        for msg in _live_messages_by_session.get(session_id, {}).values():
            if not normalize_message_content(msg.get("content")):
                continue
            copy = {k: v for k, v in msg.items() if k != "updated_at"}
            live.append(copy)
    live.sort(key=lambda m: float(m.get("timestamp") or 0))
    return live


def safe_int(value: str | None, default: int = 0, minimum: int | None = None) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    if minimum is not None and parsed < minimum:
        return minimum
    return parsed


def parse_activity_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return None


def should_show_agent(data: dict) -> bool:
    if data.get("alive", False):
        return True

    last_activity = parse_activity_ts(data.get("last_activity"))
    if last_activity is None:
        return True

    return datetime.now().timestamp() - last_activity <= DEAD_AGENT_HIDE_AFTER_SECONDS


def get_hermes_api_key() -> str:
    key = os.getenv("HERMES_LIVE_TRANSCRIPT_API_KEY", "").strip()
    if key:
        return key
    key = os.getenv("API_SERVER_KEY", "").strip()
    if key:
        return key
    key = get_hermes_api_key_from_env_file()
    if key:
        return key
    key = get_hermes_api_key_from_config()
    if key:
        return key
    try:
        return HERMES_API_KEY_FILE.read_text().strip()
    except FileNotFoundError:
        return ""
    except Exception:
        log_exception("failed to read Hermes API key")
        return ""


def get_hermes_api_key_from_env_file() -> str:
    try:
        lines = HERMES_ENV_FILE.read_text().splitlines()
    except FileNotFoundError:
        return ""
    except Exception:
        log_exception("failed to read Hermes env file")
        return ""

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == "API_SERVER_KEY":
            return value.strip().strip("'\"")
    return ""


def get_hermes_api_key_from_config() -> str:
    try:
        lines = HERMES_CONFIG_FILE.read_text().splitlines()
    except FileNotFoundError:
        return ""
    except Exception:
        log_exception("failed to read Hermes config")
        return ""

    in_gateway = False
    in_platforms = False
    in_api_server = False
    in_extra = False
    gateway_indent = platforms_indent = api_indent = extra_indent = -1
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))

        if stripped == "gateway:":
            in_gateway = True
            in_platforms = in_api_server = in_extra = False
            gateway_indent = indent
            continue
        if in_gateway and indent <= gateway_indent and stripped != "gateway:":
            in_gateway = in_platforms = in_api_server = in_extra = False

        if in_gateway and stripped == "platforms:":
            in_platforms = True
            in_api_server = in_extra = False
            platforms_indent = indent
            continue
        if in_platforms and indent <= platforms_indent and stripped != "platforms:":
            in_platforms = in_api_server = in_extra = False

        if in_gateway and in_platforms and stripped == "api_server:":
            in_api_server = True
            in_extra = False
            api_indent = indent
            extra_indent = -1
            continue
        if in_api_server and indent <= api_indent and stripped != "api_server:":
            in_api_server = False
            in_extra = False
        if in_api_server and stripped == "extra:":
            in_extra = True
            extra_indent = indent
            continue
        if in_extra and indent <= extra_indent and stripped != "extra:":
            in_extra = False
        if in_api_server and in_extra and stripped.startswith("key:"):
            return stripped.split(":", 1)[1].strip().strip("'\"")
    return ""


def call_hermes_api(path: str, payload: dict, method: str = "POST", timeout: int = 600) -> dict:
    api_key = get_hermes_api_key()
    if not api_key:
        raise RuntimeError(f"Missing Hermes API key. Set HERMES_API_KEY or create {HERMES_API_KEY_FILE}")

    url = f"{HERMES_API_BASE_URL.rstrip('/')}{path}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def create_hermes_api_session() -> tuple[str, str]:
    title = project_session_title()
    result = call_hermes_api(
        "/api/sessions",
        {"title": title, "system_prompt": project_context_prompt()},
        timeout=30,
    )
    session = result.get("session") if isinstance(result, dict) else None
    session_id = session.get("id") if isinstance(session, dict) else None
    if not session_id:
        raise RuntimeError("Hermes API did not return a session id")
    return session_id, title


def call_hermes_session_chat(session_id: str, message: str, instructions: str | None = None) -> dict:
    payload = {"message": message}
    if instructions:
        payload["instructions"] = instructions
    return call_hermes_api(f"/api/sessions/{session_id}/chat", payload)


def iter_sse_events(response):
    event_name = None
    data_lines: list[str] = []
    for raw_line in response:
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            if data_lines:
                data_raw = "\n".join(data_lines)
                try:
                    data = json.loads(data_raw)
                except json.JSONDecodeError:
                    data = {"raw": data_raw}
                yield event_name or "message", data
            event_name = None
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].lstrip())
    if data_lines:
        data_raw = "\n".join(data_lines)
        try:
            data = json.loads(data_raw)
        except json.JSONDecodeError:
            data = {"raw": data_raw}
        yield event_name or "message", data


def handle_hermes_stream_event(default_session_id: str, event_name: str, payload: dict):
    if not isinstance(payload, dict):
        return
    session_id = str(payload.get("session_id") or default_session_id or "")
    if not session_id:
        return

    if event_name == "message.started":
        message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
        message_id = str(message.get("id") or payload.get("message_id") or "assistant")
        role = str(message.get("role") or "assistant")
        upsert_live_message(session_id, message_id, role=role, display="hermes" if role == "assistant" else role)
    elif event_name == "assistant.delta":
        message_id = str(payload.get("message_id") or "assistant")
        append_live_delta(session_id, message_id, str(payload.get("delta") or ""))
    elif event_name == "assistant.completed":
        message_id = str(payload.get("message_id") or "assistant")
        content = payload.get("content")
        updates = {"completed": True}
        if isinstance(content, str) and content:
            updates["content"] = content
        upsert_live_message(session_id, message_id, **updates)
    elif event_name in ("error", "done"):
        return


def call_hermes_session_chat_stream(session_id: str, message: str, instructions: str | None = None):
    api_key = get_hermes_api_key()
    if not api_key:
        raise RuntimeError(f"Missing Hermes API key. Set HERMES_LIVE_TRANSCRIPT_API_KEY or create {HERMES_API_KEY_FILE}")

    url = f"{HERMES_API_BASE_URL.rstrip('/')}/api/sessions/{session_id}/chat/stream"
    payload = {"message": message}
    if instructions:
        payload["instructions"] = instructions
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
    )
    with urllib.request.urlopen(req, timeout=600) as response:
        for event_name, payload in iter_sse_events(response):
            handle_hermes_stream_event(session_id, event_name, payload)


def send_hermes_session_chat_background(session_id: str, message: str, instructions: str | None = None):
    def worker():
        try:
            call_hermes_session_chat_stream(session_id, message, instructions=instructions)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                try:
                    call_hermes_session_chat(session_id, message, instructions=instructions)
                    return
                except Exception:
                    pass
            log_exception(f"failed to stream background message to Hermes session {session_id}")
        except Exception:
            log_exception(f"failed to stream background message to Hermes session {session_id}")

    threading.Thread(target=worker, daemon=True).start()


def get_current_session_id() -> str | None:
    """Return the most recent non-cron session started today."""
    try:
        with sqlite3.connect(str(STATE_DB)) as db:
            cur = db.execute(
                "SELECT id FROM sessions WHERE started_at >= ? AND id NOT LIKE 'cron_%' ORDER BY started_at DESC LIMIT 1",
                (today_midnight_ts(),),
            )
            row = cur.fetchone()
            return row[0] if row else None
    except Exception:
        log_exception("failed to read current session")
        return None


def get_messages(session_id: str, after_id: int = 0) -> list[dict]:
    """Return messages for a session, optionally only those after a given id.
    Excludes internal tool results and session metadata — only conversation messages."""
    try:
        with sqlite3.connect(str(STATE_DB)) as db:
            cur = db.execute(
                """SELECT id, role, content, tool_name, tool_calls, timestamp
                   FROM messages
                   WHERE session_id = ? AND id > ? AND role NOT IN ('tool', 'session_meta')
                     AND NOT (role = 'assistant' AND (content IS NULL OR content = ''))
                   ORDER BY id ASC""",
                (session_id, after_id),
            )
            rows = cur.fetchall()
        result = []
        for row in rows:
            msg = {
                "id": row[0],
                "role": row[1],
                "display": {"user": "human", "assistant": "hermes", "tool": "system", "session_meta": "meta"}.get(row[1], row[1]),
                "content": row[2] or "",
                "tool_name": row[3] or "",
                "tool_calls": row[4] or "",
                "timestamp": row[5],
            }
            result.append(msg)
        return result
    except Exception:
        log_exception(f"failed to read messages for session {session_id}")
        return []


def get_session_info(session_id: str) -> dict | None:
    try:
        with sqlite3.connect(str(STATE_DB)) as db:
            cur = db.execute(
                "SELECT title, message_count, started_at, ended_at, end_reason, source FROM sessions WHERE id = ?",
                (session_id,),
            )
            row = cur.fetchone()
    except Exception:
        log_exception(f"failed to read session info for {session_id}")
        return None

    if not row:
        return None
    return {
        "title": row[0] or "",
        "message_count": row[1] or 0,
        "started_at": row[2] or 0,
        "ended_at": row[3] or 0,
        "end_reason": row[4] or "",
        "source": row[5] or "",
    }


def get_bus_messages(limit: int = 30, after_id: int = 0, min_timestamp: float = 0) -> list[dict]:
    """Return recent agent bus messages, newest first. Optionally only after a given id."""
    if not BUS_LOG_DB.exists():
        return []
    try:
        with sqlite3.connect(str(BUS_LOG_DB)) as db:
            if after_id > 0:
                cur = db.execute(
                    """SELECT id, agent, direction, content, msg_type, created_at
                       FROM bus_messages
                       WHERE id > ? AND created_at >= ?
                       ORDER BY id ASC""",
                    (after_id, min_timestamp),
                )
            else:
                cur = db.execute(
                    """SELECT id, agent, direction, content, msg_type, created_at
                       FROM bus_messages
                       WHERE created_at >= ?
                       ORDER BY id DESC LIMIT ?""",
                    (min_timestamp, limit),
                )
            rows = cur.fetchall()
        result = []
        for row in rows:
            bus_id = row[0]
            agent = row[1]
            if agent == "agy":
                agent = "gemini"
            direction = row[2]
            content = row[3]
            arrow = "→ to" if direction == "to" else "← from"
            result.append({
                "id": -bus_id,
                "bus_id": bus_id,
                "display": agent,
                "content": f"{arrow} {content}",
                "timestamp": row[5] or 0,
                "is_bus": True,
            })
        # If after_id, return chronological; if initial, return newest-first
        if after_id == 0:
            return result  # newest first
        return result  # chronological
    except Exception:
        log_exception("failed to read bus messages")
        return []


class TranscriptHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/":
            self._serve_html()
        elif path == "/api/status":
            self._serve_status()
        elif path == "/api/current":
            self._serve_current()
        elif path == "/api/bus/status":
            self._serve_bus_status()
        elif path == "/api/agenttalk/backlog":
            self._serve_agenttalk_backlog()
        else:
            self._json(404, json.dumps({"error": "not found"}).encode("utf-8"))

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/api/send":
            self._serve_send()
        elif path == "/api/archive":
            self._serve_archive()
        else:
            self._json(404, json.dumps({"error": "not found"}).encode("utf-8"))

    def do_HEAD(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path in ("/", "/api/status", "/api/current", "/api/bus/status", "/api/archive", "/api/agenttalk/backlog"):
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
        else:
            self.send_response(404)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

    def _serve_current(self):
        global _pinned_session_id

        after = 0
        bus_after = 0
        limit = 60
        requested_sid = None
        params = parse_qs(urlparse(self.path).query)
        if params:
            after = safe_int(params.get("after", ["0"])[0], default=0, minimum=0)
            bus_after = safe_int(params.get("bus_after", ["0"])[0], default=0, minimum=0)
            limit = safe_int(params.get("limit", ["60"])[0], default=60, minimum=1)
            requested_sid = params.get("session_id", [None])[0]

        # Use explicit session_id if provided; else auto-detect
        if requested_sid:
            sid = requested_sid
        elif after == 0:
            # Initial poll — refresh the pin
            sid = get_current_session_id()
            _pinned_session_id = sid
        else:
            # Incremental poll — use cached pin
            sid = _pinned_session_id

        if not sid:
            self._json(200, json.dumps({"session_id": None}).encode("utf-8"))
            return

        session_info = get_session_info(sid)
        if session_info is None:
            self._json(200, json.dumps({"session_id": None}).encode("utf-8"))
            return

        msgs = get_messages(sid, after_id=after)
        session_started_at = float(session_info.get("started_at") or 0)

        # Merge bus messages on every poll (incremental via bus_after)
        if after == 0:
            bus_msgs = get_bus_messages(limit, min_timestamp=session_started_at)
        elif bus_after > 0:
            bus_msgs = get_bus_messages(after_id=bus_after, min_timestamp=session_started_at)
        else:
            # The page may have opened before any bus message existed. Keep looking
            # until the client receives a last_bus_id and switches to id polling.
            bus_msgs = get_bus_messages(limit, min_timestamp=session_started_at)

        if bus_msgs:
            if bus_after == 0:
                bus_msgs.reverse()  # initial: newest-first → chronological for merge
            all_msgs = []
            ti, bi = 0, 0
            while ti < len(msgs) and bi < len(bus_msgs):
                if msgs[ti]["timestamp"] <= bus_msgs[bi]["timestamp"]:
                    all_msgs.append(msgs[ti])
                    ti += 1
                else:
                    all_msgs.append(bus_msgs[bi])
                    bi += 1
            all_msgs.extend(msgs[ti:])
            all_msgs.extend(bus_msgs[bi:])
            msgs = all_msgs

        live_msgs = get_live_messages(sid, committed_messages=msgs)
        if live_msgs:
            msgs = sorted(
                [*msgs, *live_msgs],
                key=lambda m: (float(m.get("timestamp") or 0), 1 if m.get("live") else 0),
            )

        # Extract last_id values BEFORE trim so they never regress
        last_transcript_id = None
        last_bus_id = None
        last_id = None
        if msgs:
            for m in reversed(msgs):
                tid = m.get("id", 0)
                if isinstance(tid, int) and tid > 0 and last_transcript_id is None:
                    last_transcript_id = tid
                if m.get("is_bus") and m.get("bus_id", 0) > 0 and last_bus_id is None:
                    last_bus_id = m["bus_id"]
                if last_transcript_id is not None and last_bus_id is not None:
                    break
            last_id = last_transcript_id or (-(last_bus_id or 0) if last_bus_id else None)

        # Cap on initial poll only
        if after == 0 and len(msgs) > limit:
            msgs = msgs[-limit:]

        data = {
            "session_id": sid,
            "session": session_info,
            "messages": msgs,
        }
        if last_id is not None:
            data["last_id"] = last_id
        if last_transcript_id is not None:
            data["last_transcript_id"] = last_transcript_id
        if last_bus_id is not None:
            data["last_bus_id"] = last_bus_id
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self._json(200, body)

    def _read_json_body(self, max_bytes: int = 65536) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(400, json.dumps({"error": "invalid content length"}).encode("utf-8"))
            return None
        if length <= 0:
            self._json(400, json.dumps({"error": "empty request body"}).encode("utf-8"))
            return None
        if length > max_bytes:
            self._json(413, json.dumps({"error": "request body too large"}).encode("utf-8"))
            return None
        try:
            raw = self.rfile.read(length).decode("utf-8")
            data = json.loads(raw)
        except Exception:
            self._json(400, json.dumps({"error": "invalid JSON body"}).encode("utf-8"))
            return None
        if not isinstance(data, dict):
            self._json(400, json.dumps({"error": "JSON body must be an object"}).encode("utf-8"))
            return None
        return data

    def _serve_send(self):
        data = self._read_json_body()
        if data is None:
            return

        message = data.get("message", "")
        if not isinstance(message, str):
            self._json(400, json.dumps({"error": "message must be a string"}).encode("utf-8"))
            return
        message = message.strip()
        if not message:
            self._json(400, json.dumps({"error": "message is required"}).encode("utf-8"))
            return
        if len(message) > MAX_SEND_CHARS:
            self._json(400, json.dumps({"error": f"message is too long; max {MAX_SEND_CHARS} chars"}).encode("utf-8"))
            return

        requested_sid = data.get("session_id") or _pinned_session_id or get_current_session_id()
        if not isinstance(requested_sid, str) or not requested_sid:
            requested_sid = ""

        session_info = get_session_info(requested_sid) if requested_sid else None
        if requested_sid and session_info is None:
            self._json(404, json.dumps({"error": "Hermes session not found"}).encode("utf-8"))
            return
        created_session = False
        created_session_title = None
        if not requested_sid or session_info.get("ended_at", 0):
            try:
                requested_sid, created_session_title = create_hermes_api_session()
                created_session = True
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                self._json(exc.code, json.dumps({"error": "Hermes API could not create a session", "detail": detail}).encode("utf-8"))
                return
            except Exception as exc:
                log_exception("failed to create Hermes API session")
                self._json(502, json.dumps({"error": "Hermes API server unavailable", "detail": str(exc)}).encode("utf-8"))
                return

        instructions = project_context_prompt() if created_session else None
        send_hermes_session_chat_background(requested_sid, message, instructions=instructions)
        response = {
            "ok": True,
            "queued": True,
            "streaming": True,
            "session_id": requested_sid,
            "session_title": created_session_title,
        }
        self._json(200, json.dumps(response, ensure_ascii=False).encode("utf-8"))

    def _serve_archive(self):
        data = self._read_json_body(max_bytes=MAX_ARCHIVE_BODY_BYTES)
        if data is None:
            return
        try:
            result = archive_visible_messages(data)
        except ValueError as exc:
            self._json(400, json.dumps({"error": str(exc)}).encode("utf-8"))
            return
        except Exception:
            log_exception("failed to archive visible messages")
            self._json(500, json.dumps({"error": "failed to archive messages"}).encode("utf-8"))
            return
        body = json.dumps({"ok": True, **result}, ensure_ascii=False).encode("utf-8")
        self._json(200, body)

    def _serve_status(self):
        sid = get_current_session_id()
        data = {"session_id": sid, "ok": sid is not None}
        body = json.dumps(data).encode("utf-8")
        self._json(200, body)

    def _serve_bus_status(self):
        """Return agent liveness from Agent Telemetry (port 9900)."""
        try:
            import urllib.request
            with urllib.request.urlopen("http://127.0.0.1:9900/agents", timeout=3) as r:
                telemetry = json.loads(r.read())
        except Exception:
            log_exception("failed to read agent telemetry")
            telemetry = {}

        # Read agent sessions for tmux attach info
        sessions = {}
        try:
            with open(Path.home() / ".hermes" / "agent-sessions.json") as f:
                sessions = json.load(f)
        except FileNotFoundError:
            pass
        except Exception:
            log_exception("failed to read agent sessions")
            pass

        agents = []
        for name, data in sorted(telemetry.items()):
            if name in ("agenttest", "smtest"):
                continue
            if not should_show_agent(data):
                continue
            entry = {
                "name": name,
                "type": "gemini" if name == "agy" else name,
                "alive": bool(data.get("alive", False)),
            }
            session_info = sessions.get(name)
            if session_info and session_info.get("session"):
                entry["tmux_attach"] = f"tmux attach -t {session_info['session']}"
            agents.append(entry)

        body = json.dumps({"agents": agents}).encode("utf-8")
        self._json(200, body)

    def _serve_agenttalk_backlog(self):
        params = parse_qs(urlparse(self.path).query)
        query = "?all=true" if params.get("all", ["false"])[0].lower() == "true" else ""
        url = f"{AGENTTALK_API_BASE_URL.rstrip('/')}/api/backlog{query}"
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                raw = r.read()
            data = json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            self._json(
                exc.code,
                json.dumps({
                    "ok": False,
                    "error": "AgentTalk backlog API returned an error",
                    "detail": detail,
                    "agenttalk_url": url,
                }).encode("utf-8"),
            )
            return
        except Exception as exc:
            self._json(
                502,
                json.dumps({
                    "ok": False,
                    "error": "AgentTalk backlog API unavailable",
                    "detail": str(exc),
                    "agenttalk_url": url,
                }).encode("utf-8"),
            )
            return

        body = json.dumps({"ok": True, "agenttalk_url": url, **data}, ensure_ascii=False).encode("utf-8")
        self._json(200, body)

    def _serve_html(self):
        html = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Hermes Live Transcript</title>
<style>
  :root {
    --bg: #0d1117;
    --card: #161b22;
    --border: #30363d;
    --human: #d29922;
    --hermes: #58a6ff;
    --system: #8b949e;
    --claude: #d2a8ff;
    --codex: #3fb950;
    --gemini: #ffa657;
    --text: #e6edf3;
    --muted: #8b949e;
    --mono: 'SF Mono', 'Cascadia Code', 'JetBrains Mono', monospace;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
    padding: 20px;
    max-width: 1240px;
    margin: 0 auto;
  }
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 0;
    border-bottom: 1px solid var(--border);
    margin-bottom: 20px;
  }
  h1 { font-size: 18px; font-weight: 600; }
  #status {
    font-size: 12px;
    color: var(--muted);
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    display: inline-block;
  }
  .dot.live { background: #3fb950; }
  .dot.paused { background: #d29922; }
  .controls {
    display: flex;
    gap: 8px;
    margin-bottom: 16px;
  }
  .controls button {
    background: var(--card);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 6px 14px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 13px;
  }
  .controls button:hover { background: #21262d; }
  .controls button.active { border-color: var(--hermes); }
  .view-tabs {
    display: flex;
    gap: 6px;
    margin-bottom: 12px;
    border-bottom: 1px solid var(--border);
  }
  .view-tabs button {
    background: transparent;
    border: 0;
    border-bottom: 2px solid transparent;
    color: var(--muted);
    cursor: pointer;
    padding: 8px 10px;
    font-size: 13px;
  }
  .view-tabs button.active {
    border-bottom-color: var(--hermes);
    color: var(--text);
  }
  .view { display: none; }
  .view.active { display: block; }
  .layout {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 320px;
    gap: 18px;
    align-items: start;
  }
  .main-pane {
    min-width: 0;
  }
  .send-panel {
    position: sticky;
    top: 16px;
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px;
  }
  .send-panel h2 {
    font-size: 13px;
    font-weight: 600;
    margin-bottom: 10px;
  }
  #send-message {
    width: 100%;
    min-height: 180px;
    resize: vertical;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: #0d1117;
    color: var(--text);
    padding: 10px;
    font-family: var(--mono);
    font-size: 13px;
    line-height: 1.45;
  }
  #send-message:focus {
    outline: none;
    border-color: var(--hermes);
  }
  .send-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-top: 10px;
  }
  .send-actions button {
    background: var(--hermes);
    border: 1px solid var(--hermes);
    color: #0d1117;
    padding: 7px 12px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 13px;
    font-weight: 600;
  }
  .send-actions button.secondary {
    background: var(--card);
    color: var(--text);
  }
  .send-actions button:disabled {
    opacity: 0.6;
    cursor: wait;
  }
  #send-status {
    color: var(--muted);
    font-size: 12px;
    line-height: 1.35;
  }
  #send-status.error { color: #ff7b72; }
  #send-status.ok { color: #3fb950; }
  #msg-count {
    font-size: 12px;
    color: var(--muted);
    margin-left: auto;
    align-self: center;
  }
  #archive-status {
    color: var(--muted);
    font-size: 12px;
    align-self: center;
  }
  #archive-status.ok { color: #3fb950; }
  #archive-status.error { color: #ff7b72; }
  #backlog-status {
    color: var(--muted);
    font-size: 12px;
    align-self: center;
  }
  #backlog-status.error { color: #ff7b72; }
  #backlog-status.ok { color: #3fb950; }
  .backlog-source {
    color: var(--muted);
    font-size: 12px;
    margin-bottom: 12px;
  }
  .backlog-source code {
    color: var(--text);
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 2px 5px;
  }
  .backlog-summary {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 12px;
    font-size: 12px;
    color: var(--muted);
  }
  .backlog-pill {
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 3px 8px;
    background: var(--card);
  }
  .backlog-list {
    display: grid;
    gap: 16px;
  }
  .backlog-section {
    display: grid;
    gap: 8px;
  }
  .backlog-section-header {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    color: var(--muted);
    font-size: 12px;
    margin-top: 2px;
  }
  .backlog-section-title {
    color: var(--text);
    font-size: 13px;
    font-weight: 700;
  }
  .backlog-section-count {
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 2px 7px;
    background: var(--card);
  }
  .backlog-item {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px 14px;
  }
  .backlog-item.current {
    border-color: #f2cc60;
    box-shadow: inset 3px 0 0 #f2cc60;
  }
  .backlog-item-header {
    display: flex;
    align-items: baseline;
    gap: 8px;
    flex-wrap: wrap;
    margin-bottom: 6px;
  }
  .backlog-title {
    color: var(--text);
    font-weight: 600;
  }
  .backlog-meta {
    color: var(--muted);
    font-size: 12px;
  }
  .backlog-current-label {
    color: #f2cc60;
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
  }
  .backlog-tags {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
    margin-top: 8px;
  }
  .backlog-tag {
    color: var(--muted);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 1px 5px;
    font-size: 11px;
  }
  .backlog-body {
    color: var(--text);
    font-size: 13px;
    line-height: 1.45;
  }
  .backlog-body summary {
    color: var(--muted);
    cursor: pointer;
    font-size: 12px;
    margin: 4px 0;
    user-select: none;
  }
  .backlog-body-content {
    margin-top: 6px;
    white-space: pre-wrap;
  }
  #transcript {
    display: flex;
    flex-direction: column-reverse;
    gap: 8px;
  }
  .msg {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px 16px;
    font-size: 14px;
    line-height: 1.5;
  }
  .msg.pending {
    border-style: dashed;
    opacity: 0.78;
  }
  .msg.live {
    border-color: #58a6ff;
  }
  .msg-header {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 6px;
    font-size: 12px;
  }
  .msg-role {
    font-weight: 600;
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    padding: 2px 8px;
    border-radius: 4px;
  }
  .msg-role.human { background: var(--human); color: #0d1117; }
  .msg-role.hermes { background: var(--hermes); color: #0d1117; }
  .msg-role.system { background: var(--system); color: #0d1117; }
  .msg-role.claude { background: var(--claude); color: #0d1117; }
  .msg-role.codex { background: var(--codex); color: #0d1117; }
  .msg-role.gemini { background: var(--gemini); color: #0d1117; }
  .msg-role.agy { background: var(--gemini); color: #0d1117; }
  .msg-role.pending { background: var(--muted); color: #0d1117; }
  .msg-time { color: var(--muted); font-size: 11px; }
  .msg-content {
    white-space: pre-wrap;
    word-break: break-word;
    font-family: var(--mono);
    font-size: 13px;
  }
  .msg-content.collapsed {
    max-height: 200px;
    overflow: hidden;
    position: relative;
  }
  .msg-content.collapsed::after {
    content: '... (click to expand)';
    position: absolute;
    bottom: 0; right: 0;
    background: var(--card);
    padding: 2px 8px;
    font-size: 11px;
    color: var(--muted);
    cursor: pointer;
  }
  .tool-detail {
    margin-top: 6px;
    padding: 6px 10px;
    background: #1c2128;
    border-radius: 6px;
    font-size: 12px;
    color: #d2a8ff;
    font-family: var(--mono);
  }
  .empty {
    text-align: center;
    color: var(--muted);
    padding: 40px;
    font-style: italic;
  }
  .session-info {
    font-size: 12px;
    color: var(--muted);
    margin-bottom: 16px;
    padding: 8px 12px;
    background: var(--card);
    border-radius: 6px;
    border: 1px solid var(--border);
  }
  .session-info .session-row + .session-row { margin-top: 4px; }
  .session-info .project-name { color: var(--text); font-weight: 600; }
  #agent-bar {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin-bottom: 12px;
    padding: 6px 12px;
    background: var(--card);
    border-radius: 6px;
    border: 1px solid var(--border);
    font-size: 12px;
    min-height: 30px;
    align-items: center;
  }
  .agent-badge {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.3px;
  }
  .agent-badge .dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
  }
  .agent-badge .dot.alive { background: #3fb950; }
  .agent-badge .dot.dead { background: #da3633; }
  #agent-tooltip {
    display: none;
    position: fixed;
    z-index: 999;
    background: #1c2128;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 14px;
    font-size: 15px;
    font-family: var(--mono);
    color: #e6edf3;
    box-shadow: 0 8px 24px rgba(0,0,0,0.4);
    cursor: pointer;
    pointer-events: auto;
    max-width: 500px;
    white-space: nowrap;
    line-height: 1.5;
  }
  #agent-tooltip .tt-cmd {
    font-weight: 600;
    letter-spacing: 0.3px;
  }
  #agent-tooltip .tt-hint {
    font-size: 11px;
    color: var(--muted);
    font-family: sans-serif;
    display: block;
    margin-top: 4px;
    text-align: center;
  }
  @media (max-width: 900px) {
    body { padding: 14px; }
    .layout { grid-template-columns: 1fr; }
    .send-panel { position: static; }
  }
</style>
</head>
<body>

<header>
  <h1>Hermes Transcript</h1>
  <div id="status"><span class="dot live" id="dot"></span> <span id="status-text">live</span></div>
</header>

<div class="session-info" id="session-info">Loading session...</div>

<div id="agent-bar"><span style="color:var(--muted)">agents:</span></div>
<div id="agent-tooltip"></div>

<div class="layout">
  <main class="main-pane">
    <div class="view-tabs">
      <button id="tab-transcript" class="active" onclick="showView('transcript')">Transcript</button>
      <button id="tab-backlog" onclick="showView('backlog')">Backlog</button>
    </div>

    <div id="view-transcript" class="view active">
      <div class="controls">
        <button id="btn-scroll" class="active" onclick="toggleScroll()">Auto-scroll</button>
        <button onclick="clearTranscript()">Clear</button>
        <button id="btn-archive" onclick="archiveTranscript()">Archive</button>
        <span id="archive-status"></span>
        <span id="msg-count">0 messages</span>
      </div>

      <div id="transcript">
        <div class="empty">Waiting for messages...</div>
      </div>
    </div>

    <div id="view-backlog" class="view">
      <div class="controls">
        <button id="btn-backlog-refresh" onclick="pollBacklog({force: true})">Refresh</button>
        <button id="btn-backlog-all" onclick="toggleBacklogAll()">Show all</button>
        <span id="backlog-status">Not loaded</span>
      </div>
      <div class="backlog-source">AgentTalk API: <code id="backlog-source">loading...</code></div>
      <div id="backlog-summary" class="backlog-summary"></div>
      <div id="backlog-list" class="backlog-list">
        <div class="empty">Open the Backlog tab to load AgentTalk backlog items.</div>
      </div>
    </div>
  </main>

  <aside class="send-panel">
    <h2>Send to Hermes</h2>
    <textarea id="send-message" placeholder="Type a message..."></textarea>
    <div class="send-actions">
      <button id="btn-send" onclick="sendToHermes()">Send</button>
      <button id="btn-start" class="secondary" onclick="sendToHermes({includeProjectInfo: true})">Start</button>
      <span id="send-status">Ready</span>
    </div>
  </aside>
</div>

<script>
let lastId = 0;
let lastBusId = 0;
let sessionId = null;
let autoScroll = true;
let polling = false;
let initialized = false;
let currentSessionTitle = null;
let sending = false;
let activeView = 'transcript';
let backlogLoaded = false;
let backlogPolling = false;
let showAllBacklog = false;
const PROJECT_NAME = __PROJECT_NAME_JSON__;
const PROJECT_PATH = __PROJECT_PATH_JSON__;
const LINCHPIN_DOCS = __LINCHPIN_DOCS_JSON__;

function renderSessionInfo(sessionTextNodes) {
  const info = document.getElementById('session-info');
  const sessionRow = document.createElement('div');
  sessionRow.className = 'session-row';
  sessionRow.append(...sessionTextNodes);
  const projectRow = document.createElement('div');
  projectRow.className = 'session-row project-row';
  const projectName = document.createElement('span');
  projectName.className = 'project-name';
  projectName.textContent = PROJECT_NAME || 'project';
  const projectPath = document.createElement('code');
  projectPath.textContent = PROJECT_PATH || 'unknown';
  projectRow.append(
    document.createTextNode('project: '),
    projectName,
    document.createTextNode(' · '),
    projectPath
  );
  const docsRow = document.createElement('div');
  docsRow.className = 'session-row project-row';
  docsRow.append(
    document.createTextNode('linchpin docs: '),
    document.createTextNode(LINCHPIN_DOCS.join(', '))
  );
  info.replaceChildren(sessionRow, projectRow, docsRow);
}

function updateSendButtons() {
  const sendButton = document.getElementById('btn-send');
  const startButton = document.getElementById('btn-start');
  if (!sendButton || !startButton) return;
  sendButton.disabled = sending;
  startButton.disabled = sending || Boolean(sessionId);
}

function showView(name) {
  activeView = name;
  document.getElementById('tab-transcript').className = name === 'transcript' ? 'active' : '';
  document.getElementById('tab-backlog').className = name === 'backlog' ? 'active' : '';
  document.getElementById('view-transcript').className = name === 'transcript' ? 'view active' : 'view';
  document.getElementById('view-backlog').className = name === 'backlog' ? 'view active' : 'view';
  if (name === 'backlog') pollBacklog();
}

function setBacklogStatus(text, state) {
  const el = document.getElementById('backlog-status');
  el.textContent = text;
  el.className = state || '';
}

function setBacklogSource(url) {
  const el = document.getElementById('backlog-source');
  if (el) el.textContent = url || 'AgentTalk';
}

function backlogStatusCounts(items) {
  const counts = new Map();
  for (const item of items) {
    const status = String(item.status || 'unknown');
    counts.set(status, (counts.get(status) || 0) + 1);
  }
  return Array.from(counts.entries()).sort((a, b) => a[0].localeCompare(b[0]));
}

function renderBacklog(data) {
  const items = Array.isArray(data.items) ? data.items : [];
  updateBacklogAllToggle();
  renderBacklogSummary(data, items);
  renderBacklogItems(items);
}

function updateBacklogAllToggle() {
  const button = document.getElementById('btn-backlog-all');
  if (!button) return;
  button.className = showAllBacklog ? 'active' : '';
  button.textContent = showAllBacklog ? 'Show active' : 'Show all';
}

function renderBacklogSummary(data, items) {
  const summary = document.getElementById('backlog-summary');
  const nodes = [];
  const parsedTotal = Number(data.total);
  const responseTotal = Number.isFinite(parsedTotal) ? parsedTotal : null;
  const totalCount = responseTotal === null ? items.length : responseTotal;
  const total = document.createElement('span');
  total.className = 'backlog-pill';
  total.textContent = responseTotal === null || responseTotal === items.length
    ? `${items.length} items`
    : `${items.length} of ${responseTotal} items`;
  nodes.push(total);

  const mode = document.createElement('span');
  mode.className = 'backlog-pill';
  mode.textContent = showAllBacklog ? 'all items' : 'active only';
  nodes.push(mode);

  const hiddenCount = totalCount - items.length;
  if (!showAllBacklog && hiddenCount > 0) {
    const hidden = document.createElement('span');
    hidden.className = 'backlog-pill';
    hidden.textContent = `${hiddenCount} inactive hidden`;
    nodes.push(hidden);
  }
  for (const [status, count] of backlogStatusCounts(items)) {
    const pill = document.createElement('span');
    pill.className = 'backlog-pill';
    pill.textContent = `${status}: ${count}`;
    nodes.push(pill);
  }
  if (data.generatedAt) {
    const generated = document.createElement('span');
    generated.className = 'backlog-pill';
    generated.textContent = `generated ${new Date(data.generatedAt).toLocaleString()}`;
    nodes.push(generated);
  }
  if (Array.isArray(data.warnings) && data.warnings.length > 0) {
    const warnings = document.createElement('span');
    warnings.className = 'backlog-pill';
    warnings.textContent = `${data.warnings.length} warnings`;
    nodes.push(warnings);
  }
  summary.replaceChildren(...nodes);
}

function toggleBacklogAll() {
  showAllBacklog = !showAllBacklog;
  pollBacklog({force: true});
}

function isActiveBacklogItem(item) {
  const status = String(item.status || '').toLowerCase();
  return status === 'doing' || status === 'todo';
}

function backlogPriorityRank(item) {
  const status = String(item.status || '').toLowerCase();
  if (status === 'doing') return 0;
  if (status === 'todo') return 1;
  if (!['done', 'dropped', 'promoted', 'absorbed', 'deferred', 'parked'].includes(status)) return 2;
  if (status === 'parked' || status === 'deferred') return 3;
  return 4;
}

function orderBacklogItems(items) {
  return items
    .map((item, index) => ({ item, index }))
    .sort((a, b) => {
      const byRank = backlogPriorityRank(a.item) - backlogPriorityRank(b.item);
      return byRank || a.index - b.index;
    })
    .map(({ item }) => item);
}

function backlogGroupTitle(item) {
  const epic = String(item.epic || '').trim();
  const promotedTo = String(item.promotedTo || '').trim();
  if (epic) return `Epic ${epic}`;
  if (promotedTo && promotedTo.toLowerCase().includes('spike')) return `Spike ${promotedTo}`;
  if (promotedTo) return `Promoted to ${promotedTo}`;
  return 'No epic or spike';
}

function buildBacklogGroups(items) {
  const activeGroupTitles = new Set(
    items
      .filter(isActiveBacklogItem)
      .map(backlogGroupTitle)
  );
  const groups = new Map();
  for (const item of items) {
    const itemGroupTitle = backlogGroupTitle(item);
    const title = isActiveBacklogItem(item) || activeGroupTitles.has(itemGroupTitle)
      ? itemGroupTitle
      : 'Inactive backlog';
    if (!groups.has(title)) groups.set(title, []);
    groups.get(title).push(item);
  }
  return groups;
}

function renderBacklogSectionHeader(title, items) {
  const header = document.createElement('div');
  header.className = 'backlog-section-header';

  const name = document.createElement('span');
  name.className = 'backlog-section-title';
  name.textContent = title;
  header.appendChild(name);

  const total = document.createElement('span');
  total.className = 'backlog-section-count';
  total.textContent = `${items.length} item${items.length === 1 ? '' : 's'}`;
  header.appendChild(total);

  for (const [status, count] of backlogStatusCounts(items)) {
    const statusEl = document.createElement('span');
    statusEl.className = 'backlog-section-count';
    statusEl.textContent = `${status}: ${count}`;
    header.appendChild(statusEl);
  }

  return header;
}

function renderBacklogDescription(item, isCurrent) {
  const body = document.createElement('details');
  body.className = 'backlog-body';
  const text = item.bodyMarkdown || '';
  body.open = isCurrent || text.length <= 700;

  const summary = document.createElement('summary');
  summary.textContent = body.open ? 'Description' : `Description (${text.length.toLocaleString()} chars)`;

  const content = document.createElement('div');
  content.className = 'backlog-body-content';
  content.textContent = text;

  body.append(summary, content);
  return body;
}

function renderBacklogCard(item, isCurrent) {
  const card = document.createElement('article');
  card.className = isCurrent ? 'backlog-item current' : 'backlog-item';

  const header = document.createElement('div');
  header.className = 'backlog-item-header';
  const title = document.createElement('span');
  title.className = 'backlog-title';
  title.textContent = item.title || '(untitled)';
  const meta = document.createElement('span');
  meta.className = 'backlog-meta';
  const metaParts = [item.id, item.status, item.epic, item.date].filter(Boolean);
  meta.textContent = metaParts.join(' / ');
  header.append(title, meta);
  if (isCurrent) {
    const current = document.createElement('span');
    current.className = 'backlog-current-label';
    current.textContent = String(item.status || '').toLowerCase() === 'doing' ? 'Current' : 'Next';
    header.appendChild(current);
  }

  card.append(header, renderBacklogDescription(item, isCurrent));
  if (Array.isArray(item.tags) && item.tags.length > 0) {
    const tags = document.createElement('div');
    tags.className = 'backlog-tags';
    for (const tag of item.tags) {
      const tagEl = document.createElement('span');
      tagEl.className = 'backlog-tag';
      tagEl.textContent = tag;
      tags.appendChild(tagEl);
    }
    card.appendChild(tags);
  }
  return card;
}

function renderBacklogItems(items) {
  const list = document.getElementById('backlog-list');
  if (!items.length) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = showAllBacklog
      ? 'No backlog items returned by AgentTalk.'
      : 'No active backlog items returned by AgentTalk.';
    list.replaceChildren(empty);
    return;
  }

  const ordered = orderBacklogItems(items);
  const currentActive = ordered.find(isActiveBacklogItem);
  const groups = buildBacklogGroups(ordered);

  const nodes = [];
  for (const [title, groupItems] of groups) {
    const section = document.createElement('section');
    section.className = 'backlog-section';
    section.appendChild(renderBacklogSectionHeader(title, groupItems));
    for (const item of groupItems) {
      section.appendChild(renderBacklogCard(item, currentActive && item.id === currentActive.id));
    }
    nodes.push(section);
  }
  list.replaceChildren(...nodes);
}

async function pollBacklog(options = {}) {
  if (backlogPolling) return;
  if (backlogLoaded && activeView !== 'backlog' && !options.force) return;
  backlogPolling = true;
  const refresh = document.getElementById('btn-backlog-refresh');
  if (refresh) refresh.disabled = true;
  setBacklogStatus('Loading...', '');
  try {
    const r = await fetch(showAllBacklog ? '/api/agenttalk/backlog?all=true' : '/api/agenttalk/backlog');
    const data = await r.json().catch(() => ({}));
    setBacklogSource(data.agenttalk_url);
    if (!r.ok || !data.ok) {
      throw new Error(data.error || `Backlog failed: ${r.status}`);
    }
    renderBacklog(data);
    backlogLoaded = true;
    setBacklogStatus('Loaded', 'ok');
  } catch (e) {
    updateBacklogAllToggle();
    document.getElementById('backlog-summary').replaceChildren();
    const list = document.getElementById('backlog-list');
    const error = document.createElement('div');
    error.className = 'empty';
    error.textContent = e.message || 'Backlog unavailable';
    list.replaceChildren(error);
    setBacklogStatus(e.message || 'Backlog unavailable', 'error');
  } finally {
    backlogPolling = false;
    if (refresh) refresh.disabled = false;
  }
}

function toggleScroll() {
  autoScroll = !autoScroll;
  document.getElementById('btn-scroll').className = autoScroll ? 'active' : '';
}

const MAX_VISIBLE = 60;

function updateMessageCount() {
  const container = document.getElementById('transcript');
  const count = container.querySelectorAll('.msg').length;
  document.getElementById('msg-count').textContent = count + ' messages';
}

function trimTranscript() {
  const container = document.getElementById('transcript');
  while (container.children.length > MAX_VISIBLE + 5) {
    container.removeChild(container.firstChild);
  }
  updateMessageCount();
}

async function poll() {
  if (polling) return;
  polling = true;
  try {
    const isInitial = !initialized;
    const url = isInitial
      ? '/api/current?limit=' + MAX_VISIBLE
      : `/api/current?after=${lastId}&bus_after=${lastBusId}&session_id=${encodeURIComponent(sessionId)}`;
    const r = await fetch(url);
    if (!r.ok) throw new Error(`poll failed: ${r.status}`);
    const data = await r.json();

    if (!data.session_id) {
      renderSessionInfo([document.createTextNode('No active session.')]);
      document.getElementById('status-text').textContent = 'idle';
      document.getElementById('dot').className = 'dot paused';
      sessionId = null;
      currentSessionTitle = null;
      updateSendButtons();
      polling = false;
      return;
    }

    if (data.session_id !== sessionId) {
      sessionId = data.session_id;
      lastId = 0;
      lastBusId = 0;
      initialized = false;
      document.getElementById('transcript').innerHTML = '';
      document.getElementById('msg-count').textContent = '0 messages';
      document.getElementById('dot').className = 'dot live';
      document.getElementById('status-text').textContent = 'live';
      updateSendButtons();
    }
    document.getElementById('dot').className = 'dot live';
    document.getElementById('status-text').textContent = 'live';
    initialized = true;

    if (data.session) {
      const s = data.session;
      const title = s.title || '(untitled)';
      currentSessionTitle = s.title || null;
      const count = s.message_count || 0;
      const date = s.started_at ? new Date(s.started_at * 1000).toLocaleString() : '?';
      const titleEl = document.createElement('strong');
      titleEl.textContent = title;
      const sidEl = document.createElement('code');
      sidEl.textContent = sessionId.slice(0, 20) + '...';
      renderSessionInfo([
        titleEl,
        document.createTextNode(` · ${count} msgs · started ${date} · `),
        sidEl
      ]);
    }

    if (data.messages && data.messages.length > 0) {
      upsertMessages(data.messages);
      // Use transcript id for incremental polling (bus ids are negative)
      if (data.last_transcript_id) lastId = data.last_transcript_id;
      else if (data.last_id && data.last_id > 0) lastId = data.last_id;
      if (data.last_bus_id) lastBusId = data.last_bus_id;
      trimTranscript();
    }
  } catch (e) {
    document.getElementById('status-text').textContent = 'error';
    document.getElementById('dot').className = 'dot paused';
  }
  polling = false;
}

function messageKey(msg) {
  if (msg.live) return String(msg.id);
  if (msg.is_bus) return `bus:${msg.bus_id}`;
  return `db:${msg.id}`;
}

function normalizedContent(value) {
  return String(value || '').replace(/\s+/g, ' ').trim();
}

function messageContentMatches(liveContent, committedContent, liveCompleted = false) {
  const live = normalizedContent(liveContent);
  const committed = normalizedContent(committedContent);
  if (!live || !committed) return false;
  if (live === committed) return true;

  const shorter = Math.min(live.length, committed.length);
  const longer = Math.max(live.length, committed.length);
  if (shorter < 24) return false;

  if (committed.startsWith(live)) {
    return !liveCompleted || live.length >= 48 || shorter / longer >= 0.65;
  }
  if (live.startsWith(committed)) {
    return shorter / longer >= 0.65;
  }
  return false;
}

function sameConversationRole(a, b) {
  const roleA = String(a.role || '').toLowerCase();
  const roleB = String(b.role || '').toLowerCase();
  const displayA = String(a.display || '').toLowerCase();
  const displayB = String(b.display || '').toLowerCase();
  return (
    roleA === roleB ||
    displayA === displayB ||
    (roleA === 'assistant' && displayB === 'hermes') ||
    (roleB === 'assistant' && displayA === 'hermes') ||
    (roleA === 'user' && displayB === 'human') ||
    (roleB === 'user' && displayA === 'human')
  );
}

function filterReconciledLiveMessages(messages) {
  const committed = messages.filter(msg => !msg.live && !msg.is_bus);
  if (committed.length === 0) return messages;

  return messages.filter(msg => {
    if (!msg.live) return true;
    return !committed.some(dbMsg =>
      sameConversationRole(msg, dbMsg) &&
      messageContentMatches(msg.content, dbMsg.content, Boolean(msg.completed))
    );
  });
}

function upsertMessages(messages) {
  messages = filterReconciledLiveMessages(messages);
  const container = document.getElementById('transcript');
  const empty = container.querySelector('.empty');
  if (empty) empty.remove();
  removeMatchedPendingMessages(messages);
  removeMatchedLiveMessages(messages);

  for (const msg of messages) {
    const key = messageKey(msg);
    const existing = container.querySelector(`.msg[data-message-key="${CSS.escape(key)}"]`);
    const next = buildMessageElement(msg);
    next.dataset.messageKey = key;
    if (existing) {
      existing.replaceWith(next);
    } else {
      container.appendChild(next);
    }
  }

  updateMessageCount();

  if (autoScroll) {
    window.scrollTo(0, 0);
  }
}

function buildMessageElement(msg, opts = {}) {
  const el = document.createElement('div');
  const classes = ['msg'];
  if (opts.pending || (msg.live && !msg.completed)) classes.push('pending');
  if (msg.live) classes.push('live');
  el.className = classes.join(' ');

  const roleLabel = String(msg.display || msg.role || 'message');
  const roleClass = opts.pending ? 'pending' : roleLabel.replace(/[^a-z0-9_-]/gi, '-').toLowerCase();
  const time = msg.timestamp ? new Date(msg.timestamp * 1000).toLocaleTimeString() : '';
  const content = String(msg.content || '');
  el.dataset.messageId = msg.id == null ? '' : String(msg.id);
  el.dataset.busId = msg.bus_id == null ? '' : String(msg.bus_id);
  el.dataset.role = String(msg.role || '');
  el.dataset.display = roleLabel;
  el.dataset.timestamp = msg.timestamp == null ? '' : String(msg.timestamp);
  el.dataset.content = content;
  if (msg.live) el.dataset.live = '1';
  if (msg.completed) el.dataset.completed = '1';
  if (opts.pending) el.dataset.pending = '1';

  const needsCollapse = content.length > 500;
  const header = document.createElement('div');
  header.className = 'msg-header';
  const role = document.createElement('span');
  role.className = `msg-role ${roleClass}`;
  role.textContent = roleLabel;
  const timeEl = document.createElement('span');
  timeEl.className = 'msg-time';
  if (opts.pending) {
    timeEl.textContent = `${time} · queued`;
  } else if (msg.live && msg.completed) {
    timeEl.textContent = `${time} · pending db`;
  } else if (msg.live) {
    timeEl.textContent = `${time} · live`;
  } else {
    timeEl.textContent = time;
  }
  header.append(role, timeEl);

  const contentEl = document.createElement('div');
  contentEl.className = `msg-content ${needsCollapse ? 'collapsed' : ''}`;
  contentEl.textContent = content;
  contentEl.addEventListener('click', () => contentEl.classList.toggle('collapsed'));

  el.append(header, contentEl);
  if (msg.tool_name || opts.detail) {
    const toolInfo = document.createElement('div');
    toolInfo.className = 'tool-detail';
    toolInfo.textContent = String(msg.tool_name || opts.detail);
    el.appendChild(toolInfo);
  }
  return el;
}

function addPendingMessage(content) {
  const container = document.getElementById('transcript');
  const empty = container.querySelector('.empty');
  if (empty) empty.remove();

  const el = buildMessageElement({
    display: 'human',
    content,
    timestamp: Date.now() / 1000
  }, {pending: true, detail: 'waiting for Hermes to persist this turn'});
  el.dataset.pendingContent = content;
  container.appendChild(el);
  updateMessageCount();
  if (autoScroll) {
    window.scrollTo(0, 0);
  }
}

function removeMatchedPendingMessages(messages) {
  const pending = Array.from(document.querySelectorAll('#transcript .msg[data-pending-content]'));
  if (pending.length === 0) return;

  for (const msg of messages) {
    const role = String(msg.role || '').toLowerCase();
    const display = String(msg.display || '').toLowerCase();
    if (role !== 'user' && display !== 'human') continue;

    const content = String(msg.content || '');
    const match = pending.find(el => el.dataset.pendingContent === content);
    if (match) {
      match.remove();
      pending.splice(pending.indexOf(match), 1);
    }
  }
}

function removeMatchedLiveMessages(messages) {
  const liveNodes = Array.from(document.querySelectorAll('#transcript .msg[data-live="1"]'));
  if (liveNodes.length === 0) return;

  for (const msg of messages) {
    if (msg.live || msg.is_bus) continue;

    const match = liveNodes.find(el => sameConversationRole({
      role: el.dataset.role || '',
      display: el.dataset.display || ''
    }, msg) && messageContentMatches(
      el.dataset.content || '',
      msg.content || '',
      el.dataset.completed === '1'
    ));
    if (match) {
      match.remove();
      liveNodes.splice(liveNodes.indexOf(match), 1);
    }
  }
}

function clearTranscript() {
  document.getElementById('transcript').innerHTML = '<div class="empty">Cleared — waiting for new messages...</div>';
  document.getElementById('msg-count').textContent = '0 messages';
  setArchiveStatus('', '');
}

function setArchiveStatus(text, state) {
  const el = document.getElementById('archive-status');
  el.textContent = text;
  el.className = state || '';
}

function archiveFilenamePart(value) {
  return String(value || 'unknown')
    .replace(/[^A-Za-z0-9._ -]+/g, '-')
    .replace(/^[ ._-]+|[ ._-]+$/g, '')
    .slice(0, 80) || 'unknown';
}

function archiveTimestampForName(date = new Date()) {
  const pad = value => String(value).padStart(2, '0');
  return [
    date.getFullYear(),
    pad(date.getMonth() + 1),
    pad(date.getDate())
  ].join('-') + ' ' + [
    pad(date.getHours()),
    pad(date.getMinutes()),
    pad(date.getSeconds())
  ].join('-');
}

function visibleArchiveMessages() {
  return Array.from(document.querySelectorAll('#transcript .msg')).map(el => ({
    id: el.dataset.messageId || undefined,
    bus_id: el.dataset.busId || undefined,
    role: el.dataset.role || '',
    display: el.dataset.display || 'message',
    content: el.dataset.content || '',
    timestamp: el.dataset.timestamp ? Number(el.dataset.timestamp) : undefined,
    live: el.dataset.live === '1',
    pending: el.dataset.pending === '1'
  })).filter(msg => msg.content.trim());
}

function defaultArchiveFilename(messages) {
  const title = currentSessionTitle || `${PROJECT_NAME || 'project'} - ${archiveTimestampForName()}`;
  return archiveFilenamePart(title) + '.md';
}

async function archiveTranscript() {
  const messages = visibleArchiveMessages();
  if (messages.length === 0) {
    setArchiveStatus('Nothing to archive', 'error');
    return;
  }

  const suggested = defaultArchiveFilename(messages);
  const filename = window.prompt('Archive filename', suggested);
  if (filename === null) return;
  const button = document.getElementById('btn-archive');
  button.disabled = true;
  setArchiveStatus('Archiving...', '');
  try {
    const response = await fetch('/api/archive', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: sessionId, filename, messages})
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) {
      throw new Error(data.error || `Archive failed: ${response.status}`);
    }
    setArchiveStatus(`Saved ${data.message_count} to ${data.filename}`, 'ok');
  } catch (e) {
    setArchiveStatus(e.message || 'Archive failed', 'error');
  } finally {
    button.disabled = false;
  }
}

function setSendStatus(text, state) {
  const el = document.getElementById('send-status');
  el.textContent = text;
  el.className = state || '';
}

function projectInfoMessage(message) {
  const userMessage = message.trim() || 'Start a development session for this project.';
  const docs = LINCHPIN_DOCS.map(doc => `- ${doc}: ${PROJECT_PATH || 'unknown'}/${doc}`);
  return [
    'Current development project:',
    `- Project name: ${PROJECT_NAME || 'project'}`,
    `- Project path: ${PROJECT_PATH || 'unknown'}`,
    '- Linchpin docs to read at startup:',
    ...docs,
    '',
    userMessage
  ].join('\n');
}

async function sendToHermes(options = {}) {
  const textarea = document.getElementById('send-message');
  const rawMessage = textarea.value.trim();
  const message = options.includeProjectInfo ? projectInfoMessage(rawMessage) : rawMessage;
  if (!message) {
    setSendStatus('Message is empty', 'error');
    textarea.focus();
    return;
  }
  sending = true;
  updateSendButtons();
  setSendStatus('Sending...', '');
  try {
    const payload = {message};
    if (sessionId) payload.session_id = sessionId;
    const response = await fetch('/api/send', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) {
      throw new Error(data.error || `Send failed: ${response.status}`);
    }
    textarea.value = '';
    setSendStatus(data.streaming ? 'Queued · streaming' : 'Queued', 'ok');
    if (data.session_id && data.session_id !== sessionId) {
      sessionId = data.session_id;
      currentSessionTitle = data.session_title || currentSessionTitle;
      lastId = 0;
      lastBusId = 0;
      initialized = true;
      document.getElementById('transcript').innerHTML = '';
      document.getElementById('msg-count').textContent = '0 messages';
      updateSendButtons();
    }
    addPendingMessage(message);
    poll();
  } catch (e) {
    setSendStatus(e.message || 'Send failed', 'error');
  } finally {
    sending = false;
    updateSendButtons();
  }
}

document.getElementById('send-message').addEventListener('keydown', (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
    event.preventDefault();
    sendToHermes();
  }
});

function copyTmux(cmd, el) {
  navigator.clipboard.writeText(cmd).then(() => {
    el.style.outline = '2px solid #3fb950';
    el.style.outlineOffset = '2px';
    setTimeout(() => { el.style.outline = ''; el.style.outlineOffset = ''; }, 1200);
  }).catch(() => {});
}

const tooltipEl = document.getElementById('agent-tooltip');
let tooltipHideTimer = null;

function showTooltip(cmd, x, y) {
  if (tooltipHideTimer) { clearTimeout(tooltipHideTimer); tooltipHideTimer = null; }
  const cmdEl = document.createElement('span');
  cmdEl.className = 'tt-cmd';
  cmdEl.textContent = cmd;
  const hintEl = document.createElement('span');
  hintEl.className = 'tt-hint';
  hintEl.textContent = 'Click to copy';
  tooltipEl.replaceChildren(cmdEl, hintEl);
  tooltipEl.style.display = 'block';
  tooltipEl.style.left = Math.min(x, window.innerWidth - tooltipEl.offsetWidth - 20) + 'px';
  tooltipEl.style.top = (y + 16) + 'px';
  tooltipEl.dataset.cmd = cmd;
}

function hideTooltip(delay) {
  if (tooltipHideTimer) clearTimeout(tooltipHideTimer);
  tooltipHideTimer = setTimeout(() => {
    tooltipEl.style.display = 'none';
    tooltipHideTimer = null;
  }, delay || 0);
}

tooltipEl.addEventListener('mouseenter', () => {
  if (tooltipHideTimer) { clearTimeout(tooltipHideTimer); tooltipHideTimer = null; }
});
tooltipEl.addEventListener('mouseleave', () => hideTooltip(100));
tooltipEl.addEventListener('click', () => {
  const cmd = tooltipEl.dataset.cmd;
  if (cmd) copyTmux(cmd, tooltipEl);
});

setInterval(poll, __POLL_INTERVAL__);
updateSendButtons();
poll();

setInterval(() => {
  if (activeView === 'backlog') pollBacklog();
}, 30000);

// Agent liveness bar
setInterval(pollAgentBar, 5000);
pollAgentBar();

const agentColors = {claude:'#d2a8ff', codex:'#3fb950', gemini:'#ffa657', agy:'#ffa657'};

function pollAgentBar() {
  fetch('/api/bus/status')
    .then(r => r.json())
    .then(data => {
      const bar = document.getElementById('agent-bar');
      const prefix = document.createElement('span');
      prefix.style.color = 'var(--muted)';
      prefix.textContent = 'agents:';
      if (!data.agents || data.agents.length === 0) {
        const none = document.createElement('em');
        none.textContent = ' none connected';
        prefix.appendChild(none);
        bar.replaceChildren(prefix);
        return;
      }
      const nodes = [prefix];
      for (const a of data.agents) {
        const displayName = a.name === 'agy' ? 'gemini' : a.name;
        const color = agentColors[displayName] || 'var(--text)';
        const label = a.type ? `${displayName} (${a.type})` : displayName;
        const badge = document.createElement('span');
        badge.className = 'agent-badge';
        badge.style.color = color;
        badge.style.cursor = 'pointer';
        if (a.tmux_attach) {
          badge.dataset.tmux = a.tmux_attach;
        }
        const dot = document.createElement('span');
        dot.className = `dot ${a.alive ? 'alive' : 'dead'}`;
        badge.append(dot, document.createTextNode(label));
        nodes.push(badge);
      }
      bar.replaceChildren(...nodes);

      // Attach hover events for tooltip badges via delegation
      bar.querySelectorAll('.agent-badge[data-tmux]').forEach(el => {
        el.addEventListener('mouseenter', (e) => {
          const cmd = el.dataset.tmux;
          const rect = el.getBoundingClientRect();
          showTooltip(cmd, rect.left, rect.bottom);
        });
        el.addEventListener('mouseleave', () => hideTooltip(200));
      });
    })
    .catch(() => {});
}
</script>
</body>
</html>"""
        html = html.replace("__POLL_INTERVAL__", str(POLL_INTERVAL))
        html = html.replace("__PROJECT_NAME_JSON__", json.dumps(PROJECT_NAME))
        html = html.replace("__PROJECT_PATH_JSON__", json.dumps(str(PROJECT_PATH)))
        html = html.replace("__LINCHPIN_DOCS_JSON__", json.dumps(LINCHPIN_DOCS))
        if DEV_MODE:
            html = html.replace("</head>",
                "<script>console.log('[Hermes Live] DEV MODE — poll interval %sms')</script></head>" % POLL_INTERVAL)
        self._html(html)

    def _html(self, content: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(content.encode("utf-8"))

    def _json(self, status: int, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), TranscriptHandler)
    print(f"Hermes Live Transcript: http://127.0.0.1:{PORT}")
    print(f"  Reading from: {STATE_DB}")
    print(f"  Project: {PROJECT_NAME} ({PROJECT_PATH})")
    print(f"  AgentTalk API: {AGENTTALK_API_BASE_URL}")
    sid = get_current_session_id()
    print(f"  Today session: {sid}")
    print("  Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
