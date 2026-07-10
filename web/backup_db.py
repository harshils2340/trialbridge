#!/usr/bin/env python3
"""Create a timestamped SQLite backup and verify integrity.

Run:
  DB_PATH=/var/data/bridgemd.db \
  /Users/harshils/GraphMD/matcher/.venv/bin/python backup_db.py
"""
from __future__ import annotations

import datetime as dt
import os
import pathlib
import shutil
import sqlite3
import sys


def main():
    db_path = pathlib.Path(
        os.environ.get("DB_PATH")
        or (pathlib.Path(__file__).resolve().parent / "bridgemd.db")
    )
    if not db_path.exists():
        raise RuntimeError(f"DB_PATH does not exist: {db_path}")
    out_dir = pathlib.Path(os.environ.get("DB_BACKUP_DIR", db_path.parent / "backups"))
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out_file = out_dir / f"bridgemd_{stamp}.db"
    shutil.copy2(db_path, out_file)

    con = sqlite3.connect(out_file)
    ok = con.execute("PRAGMA integrity_check").fetchone()[0]
    con.close()
    if ok.lower() != "ok":
        raise RuntimeError(f"integrity_check failed: {ok}")
    print(f"PASS: backup created {out_file}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FAIL: {exc}")
        sys.exit(1)

