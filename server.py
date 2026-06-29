#!/usr/bin/env python3
"""Hermes Live Transcript — local web viewer for the current conversation.

Displays the Hermes session transcript in real-time on a local web page.
Reads directly from the Hermes state.db SQLite database.

Usage:
    python3 live-transcript.py [port]
    Open http://127.0.0.1:PORT in your browser
"""

import json
import sqlite3
import sys
import traceback
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8800
STATE_DB = Path.home() / ".hermes" / "state.db"
BUS_LOG_DB = Path.home() / ".hermes" / "agent-bus-log.db"

# Dev mode: shorter poll, verbose logging
DEV_MODE = "--dev" in sys.argv
POLL_INTERVAL = 1000 if DEV_MODE else 3000  # ms

if DEV_MODE:
    print(f"  ⚠ DEV MODE — poll every {POLL_INTERVAL}ms")

# Cache: pinned session ID, updated on initial poll only
_pinned_session_id: str | None = None
_reported_errors: set[str] = set()


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


def safe_int(value: str | None, default: int = 0, minimum: int | None = None) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    if minimum is not None and parsed < minimum:
        return minimum
    return parsed


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
                "SELECT title, message_count, started_at FROM sessions WHERE id = ?",
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
    }


def get_bus_messages(limit: int = 30, after_id: int = 0) -> list[dict]:
    """Return recent agent bus messages, newest first. Optionally only after a given id."""
    if not BUS_LOG_DB.exists():
        return []
    try:
        with sqlite3.connect(str(BUS_LOG_DB)) as db:
            if after_id > 0:
                cur = db.execute(
                    """SELECT id, agent, direction, content, msg_type, created_at
                       FROM bus_messages WHERE id > ? ORDER BY id ASC""",
                    (after_id,),
                )
            else:
                cur = db.execute(
                    """SELECT id, agent, direction, content, msg_type, created_at
                       FROM bus_messages ORDER BY id DESC LIMIT ?""",
                    (limit,),
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
        else:
            self._json(404, json.dumps({"error": "not found"}).encode("utf-8"))

    def do_HEAD(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path in ("/", "/api/status", "/api/current", "/api/bus/status"):
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

        # Merge bus messages on every poll (incremental via bus_after)
        if after == 0:
            bus_msgs = get_bus_messages(limit)
        elif bus_after > 0:
            bus_msgs = get_bus_messages(after_id=bus_after)
        else:
            # The page may have opened before any bus message existed. Keep looking
            # until the client receives a last_bus_id and switches to id polling.
            bus_msgs = get_bus_messages(limit)

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

        # Extract last_id values BEFORE trim so they never regress
        last_transcript_id = None
        last_bus_id = None
        last_id = None
        if msgs:
            last = msgs[-1]
            last_id = last.get("id") or last.get("bus_id") or 0
            for m in reversed(msgs):
                tid = m.get("id", 0)
                if tid and tid > 0 and last_transcript_id is None:
                    last_transcript_id = tid
                if m.get("is_bus") and m.get("bus_id", 0) > 0 and last_bus_id is None:
                    last_bus_id = m["bus_id"]
                if last_transcript_id is not None and last_bus_id is not None:
                    break

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
    max-width: 900px;
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
  #msg-count {
    font-size: 12px;
    color: var(--muted);
    margin-left: auto;
    align-self: center;
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

<div class="controls">
  <button id="btn-scroll" class="active" onclick="toggleScroll()">Auto-scroll</button>
  <button onclick="clearTranscript()">Clear</button>
  <span id="msg-count">0 messages</span>
</div>

<div id="transcript">
  <div class="empty">Waiting for messages...</div>
</div>

<script>
let lastId = 0;
let lastBusId = 0;
let sessionId = null;
let autoScroll = true;
let polling = false;
let initialized = false;

function toggleScroll() {
  autoScroll = !autoScroll;
  document.getElementById('btn-scroll').className = autoScroll ? 'active' : '';
}

const MAX_VISIBLE = 60;

function trimTranscript() {
  const container = document.getElementById('transcript');
  while (container.children.length > MAX_VISIBLE + 5) {
    container.removeChild(container.firstChild);
  }
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
    const data = await r.json();

    if (!data.session_id) {
      document.getElementById('session-info').textContent = 'No active session.';
      document.getElementById('status-text').textContent = 'idle';
      document.getElementById('dot').className = 'dot paused';
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
    }
    initialized = true;

    if (data.session) {
      const s = data.session;
      const title = s.title || '(untitled)';
      const count = s.message_count || 0;
      const date = s.started_at ? new Date(s.started_at * 1000).toLocaleString() : '?';
      const info = document.getElementById('session-info');
      const titleEl = document.createElement('strong');
      titleEl.textContent = title;
      const sidEl = document.createElement('code');
      sidEl.textContent = sessionId.slice(0, 20) + '...';
      info.replaceChildren(
        titleEl,
        document.createTextNode(` · ${count} msgs · started ${date} · `),
        sidEl
      );
    }

    if (data.messages && data.messages.length > 0) {
      appendMessages(data.messages);
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

function appendMessages(messages) {
  const container = document.getElementById('transcript');
  const empty = container.querySelector('.empty');
  if (empty) empty.remove();

  for (const msg of messages) {
    const el = document.createElement('div');
    el.className = 'msg';

    const roleLabel = String(msg.display || msg.role || 'message');
    const roleClass = roleLabel.replace(/[^a-z0-9_-]/gi, '-').toLowerCase();
    const time = msg.timestamp ? new Date(msg.timestamp * 1000).toLocaleTimeString() : '';
    const content = String(msg.content || '');

    const needsCollapse = content.length > 500;
    const header = document.createElement('div');
    header.className = 'msg-header';
    const role = document.createElement('span');
    role.className = `msg-role ${roleClass}`;
    role.textContent = roleLabel;
    const timeEl = document.createElement('span');
    timeEl.className = 'msg-time';
    timeEl.textContent = time;
    header.append(role, timeEl);

    const contentEl = document.createElement('div');
    contentEl.className = `msg-content ${needsCollapse ? 'collapsed' : ''}`;
    contentEl.textContent = content;
    contentEl.addEventListener('click', () => contentEl.classList.toggle('collapsed'));

    el.append(header, contentEl);
    if (msg.tool_name) {
      const toolInfo = document.createElement('div');
      toolInfo.className = 'tool-detail';
      toolInfo.textContent = String(msg.tool_name);
      el.appendChild(toolInfo);
    }
    container.appendChild(el);
  }

  const count = container.children.length;
  document.getElementById('msg-count').textContent = count + ' messages';

  if (autoScroll) {
    window.scrollTo(0, 0);
  }
}

function clearTranscript() {
  document.getElementById('transcript').innerHTML = '<div class="empty">Cleared — waiting for new messages...</div>';
  document.getElementById('msg-count').textContent = '0 messages';
}

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
poll();

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
    server = HTTPServer(("127.0.0.1", PORT), TranscriptHandler)
    print(f"Hermes Live Transcript: http://127.0.0.1:{PORT}")
    print(f"  Reading from: {STATE_DB}")
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
