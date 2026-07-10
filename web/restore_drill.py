#!/usr/bin/env python3
"""Non-destructive restore drill for SQLite backups.

Usage:
  BACKUP_FILE=/var/data/backups/bridgemd_YYYYMMDD_HHMMSS.db \
  /Users/harshils/GraphMD/matcher/.venv/bin/python restore_drill.py
"""
from __future__ import annotations

import os
import pathlib
import sqlite3
import sys


def _count(con, table):
    return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def main():
    raw = os.environ.get("BACKUP_FILE", "").strip()
    if not raw:
        raise RuntimeError("Set BACKUP_FILE=/path/to/backup.db")
    p = pathlib.Path(raw)
    if not p.exists():
        raise RuntimeError(f"backup file missing: {p}")

    con = sqlite3.connect(p)
    check = con.execute("PRAGMA integrity_check").fetchone()[0]
    if check.lower() != "ok":
        raise RuntimeError(f"integrity_check failed: {check}")

    # Core entities expected for a viable restore.
    counts = {
        "users": _count(con, "users"),
        "patient_users": _count(con, "patient_users"),
        "leads": _count(con, "leads"),
        "lead_events": _count(con, "lead_events"),
    }
    con.close()
    print("PASS: restore drill ok")
    for k, v in counts.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FAIL: {exc}")
        sys.exit(1)

