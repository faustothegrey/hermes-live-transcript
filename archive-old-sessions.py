#!/usr/bin/env python3
"""Archive Hermes sessions older than N days from state.db.

Safely copies old sessions out of the main state.db into an archive db.
Uses VACUUM INTO for clean separation.

Usage:
    python3 archive-old-sessions.py [--days 6] [--dry-run]
"""

import argparse
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

STATE_DB = Path.home() / ".hermes" / "state.db"
ARCHIVE_DIR = Path.home() / ".hermes" / "archived"


def get_old_session_ids(db, cutoff: float) -> list[str]:
    cur = db.execute(
        "SELECT id FROM sessions WHERE started_at < ? ORDER BY started_at",
        (cutoff,),
    )
    return [row[0] for row in cur.fetchall()]


def get_session_summary(db) -> list[tuple]:
    cur = db.execute(
        """SELECT id, title, started_at, message_count
           FROM sessions ORDER BY started_at"""
    )
    return cur.fetchall()


def main():
    parser = argparse.ArgumentParser(description="Archive old Hermes sessions")
    parser.add_argument("--days", type=int, default=6)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cutoff = time.time() - (args.days * 86400)
    cutoff_date = datetime.fromtimestamp(cutoff, tz=timezone.utc).strftime("%Y-%m-%d")

    if not STATE_DB.exists():
        print(f"state.db not found at {STATE_DB}")
        sys.exit(1)

    db = sqlite3.connect(str(STATE_DB))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=OFF")

    old_ids = get_old_session_ids(db, cutoff)

    if not old_ids:
        print(f"No sessions older than {args.days} days (before {cutoff_date}).")
        db.close()
        return

    print(f"Sessions older than {args.days} days (before {cutoff_date}):")
    all_summary = get_session_summary(db)
    total_msgs = 0
    for sid, title, started_at, msg_count in all_summary:
        if sid not in old_ids:
            continue
        sdate = datetime.fromtimestamp(started_at, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M"
        )
        t = (title or "(untitled)")[:60]
        print(f"  {sdate}  {msg_count:>4} msgs  {t}")
        total_msgs += msg_count

    print(f"\nTotal: {len(old_ids)} sessions, {total_msgs} messages")

    if args.dry_run:
        db.close()
        print("\nDry run — no changes made.")
        return

    # Confirm with warning
    print(f"\nProceeding to archive {len(old_ids)} sessions...")
    print(f"  Source: {STATE_DB} ({STATE_DB.stat().st_size / 1024 / 1024:.0f} MB)")

    # Step 1: Copy the entire DB to archive (preserves all schema, triggers, FTS)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    archive_name = f"archive-pre-{cutoff_date}.db"
    archive_path = ARCHIVE_DIR / archive_name
    suffix = 1
    while archive_path.exists():
        suffix += 1
        archive_path = ARCHIVE_DIR / f"archive-pre-{cutoff_date}-{suffix}.db"

    print(f"  Copying to archive: {archive_path}")
    shutil.copy2(str(STATE_DB), str(archive_path))

    # Step 2: Delete old sessions from main DB
    print(f"  Removing {len(old_ids)} old sessions from main DB...")
    placeholders = ",".join("?" for _ in old_ids)

    # Delete messages first
    db.execute(
        f"DELETE FROM messages WHERE session_id IN ({placeholders})", old_ids
    )
    # Then delete from FTS tables
    db.execute(
        f"DELETE FROM messages_fts WHERE rowid IN (SELECT id FROM messages WHERE session_id IN ({placeholders}))",
        old_ids,
    )
    db.execute(
        f"DELETE FROM messages_fts_trigram WHERE rowid IN (SELECT id FROM messages WHERE session_id IN ({placeholders}))",
        old_ids,
    )
    # Then sessions
    db.execute(
        f"DELETE FROM sessions WHERE id IN ({placeholders})", old_ids
    )

    # Step 3: Commit deletions first, then vacuum
    db.commit()
    print(f"  Vacuuming main DB to reclaim space...")
    db.execute("VACUUM")
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.close()

    archive_size = archive_path.stat().st_size
    new_size = STATE_DB.stat().st_size
    print(f"\nDone.")
    print(f"  Archived to: {archive_path} ({archive_size / 1024 / 1024:.0f} MB)")
    print(f"  Main DB now: {STATE_DB} ({new_size / 1024 / 1024:.0f} MB)")
    print(f"  Freed: {(archive_size - new_size) / 1024 / 1024:.0f} MB")
    print(f"\nTo query archive:")
    print(f"  sqlite3 {archive_path}")
    print(f"  >> .tables")
    print(f"  >> SELECT id, title, datetime(started_at,'unixepoch') FROM sessions;")


if __name__ == "__main__":
    main()
