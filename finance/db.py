"""SQLite access. One file, migrations applied in order, read-only mode for Q&A."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: Path, ro: bool = False) -> sqlite3.Connection:
    if ro:
        if not db_path.exists():
            raise FileNotFoundError(f"database not found: {db_path}")
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply any migrations/NNN_*.sql not yet recorded in schema_version. Returns names applied."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    applied = {r[0] for r in conn.execute("SELECT version FROM schema_version")}
    done = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = int(path.name.split("_", 1)[0])
        if version in applied:
            continue
        conn.executescript(path.read_text())
        conn.execute("INSERT OR IGNORE INTO schema_version (version) VALUES (?)", (version,))
        conn.commit()
        done.append(path.name)
    return done


def open_db(db_path: Path, ro: bool = False) -> sqlite3.Connection:
    conn = connect(db_path, ro=ro)
    if not ro:
        migrate(conn)
    return conn


@contextmanager
def sync_run(conn: sqlite3.Connection, source: str):
    """Record a run in sync_runs. Usage: with sync_run(conn, 'plaid:mybank') as run: run['added'] += 1"""
    cur = conn.execute("INSERT INTO sync_runs (source, started) VALUES (?, ?)", (source, now_iso()))
    run_id = cur.lastrowid
    conn.commit()
    stats = {"added": 0, "modified": 0, "removed": 0}
    try:
        yield stats
    except Exception as e:
        conn.execute(
            "UPDATE sync_runs SET finished=?, added=?, modified=?, removed=?, error=? WHERE id=?",
            (now_iso(), stats["added"], stats["modified"], stats["removed"], f"{type(e).__name__}: {e}", run_id),
        )
        conn.commit()
        raise
    conn.execute(
        "UPDATE sync_runs SET finished=?, added=?, modified=?, removed=? WHERE id=?",
        (now_iso(), stats["added"], stats["modified"], stats["removed"], run_id),
    )
    conn.commit()


def upsert_account(conn: sqlite3.Connection, **row) -> None:
    cols = ", ".join(row)
    placeholders = ", ".join("?" for _ in row)
    updates = ", ".join(f"{k}=excluded.{k}" for k in row if k != "id")
    conn.execute(
        f"INSERT INTO accounts ({cols}) VALUES ({placeholders}) ON CONFLICT(id) DO UPDATE SET {updates}",
        tuple(row.values()),
    )
