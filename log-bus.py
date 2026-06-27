#!/usr/bin/env python3
"""Log an Agent Bus exchange to the persistent bus log.

Usage:
    python3 log-bus.py to claude "message text"
    python3 log-bus.py from codex '{"text":"response text","type":"status"}'

The bus log is read by the Hermes Live Transcript server to display
agent communications alongside the Hermes conversation.
"""

import json
import sqlite3
import sys
import time
from pathlib import Path

LOG_DB = Path.home() / ".hermes" / "agent-bus-log.db"


def log(agent: str, direction: str, content: str, msg_type: str = "chat"):
    db = sqlite3.connect(str(LOG_DB))
    db.execute(
        "INSERT INTO bus_messages (agent, direction, content, msg_type, created_at) VALUES (?, ?, ?, ?, ?)",
        (agent, direction, content, msg_type, time.time()),
    )
    db.commit()
    db.close()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    direction = sys.argv[1]
    if direction not in ("to", "from"):
        print(f"Direction must be 'to' or 'from', got '{direction}'")
        sys.exit(1)

    agent = sys.argv[2]
    content = " ".join(sys.argv[3:]) if len(sys.argv) > 3 else ""

    if not content:
        # Read from stdin
        content = sys.stdin.read().strip()

    log(agent, direction, content)
    print(f"✅ Logged {direction} {agent}: {content[:60]}...")
