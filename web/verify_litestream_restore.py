#!/usr/bin/env python3
"""Prove a Litestream replica restores to the same data the live database holds, before
anything depends on it. Same shape as restore_drill.py (integrity check, then row counts
on the entities that matter), run against a restore pulled from the bucket rather than a
local backup file, and diffed against the live database instead of just printed.

Usage (from a shell with the `litestream` binary and this service's env vars available,
for example `render ssh` into the web service, or locally with the same LITESTREAM_* and
DB_PATH vars exported):

  python verify_litestream_restore.py

Exits non-zero and prints exactly what disagrees if the restore is missing a table, fails
its integrity check, or has a different row count than the live database in any of the
core tables. A clean pass is the gate before phase 2 (dropping the disk) in LITESTREAM.md.
"""
from __future__ import annotations

import os
import pathlib
import sqlite3
import subprocess
import sys
import tempfile

CORE_TABLES = ["users", "patient_users", "leads", "lead_events"]


def _counts(db_path: pathlib.Path) -> dict[str, int]:
    con = sqlite3.connect(db_path)
    try:
        check = con.execute("PRAGMA integrity_check").fetchone()[0]
        if check.lower() != "ok":
            raise RuntimeError(f"integrity_check failed on {db_path}: {check}")
        return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in CORE_TABLES}
    finally:
        con.close()


def main() -> None:
    live_path = pathlib.Path(os.environ.get("DB_PATH", "").strip() or sys.exit("Set DB_PATH to the live database."))
    if not live_path.exists():
        raise RuntimeError(f"live DB_PATH does not exist: {live_path}")

    with tempfile.TemporaryDirectory() as tmp:
        restored = pathlib.Path(tmp) / "restored.db"
        # -config is required: without it Litestream looks for /etc/litestream.yml and finds
        # no replica. Run from web/, where the build puts the litestream binary.
        subprocess.run([os.environ.get("LITESTREAM_BIN", "./litestream"), "restore", "-config", "litestream.yml", "-o", str(restored), str(live_path)], check=True)
        restored_counts = _counts(restored)

    live_counts = _counts(live_path)
    mismatches = {t: (live_counts[t], restored_counts[t]) for t in CORE_TABLES if live_counts[t] != restored_counts[t]}
    if mismatches:
        print("FAIL: restore disagrees with the live database")
        for t, (live_n, restored_n) in mismatches.items():
            print(f"  {t}: live={live_n} restored={restored_n}")
        sys.exit(1)

    print("PASS: Litestream restore matches the live database")
    for t, n in live_counts.items():
        print(f"  {t}: {n}")


if __name__ == "__main__":
    main()
