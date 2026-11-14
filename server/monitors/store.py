"""SQLite storage for fact sheets, their fields and every monitor item received.

The default database is ``out/factsheets.db`` (git-ignored). Every write commits at once,
so a crash mid-poll keeps what was already received.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "out" / "factsheets.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sheets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company TEXT NOT NULL UNIQUE COLLATE NOCASE,
    telem_session_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fields (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sheet_id INTEGER NOT NULL REFERENCES sheets(id) ON DELETE CASCADE,
    field TEXT NOT NULL,
    label TEXT NOT NULL,
    query TEXT NOT NULL,
    goal TEXT NOT NULL,
    value TEXT,
    verdict TEXT NOT NULL,
    evidence TEXT,
    source_url TEXT,
    engines_tried TEXT NOT NULL DEFAULT '[]',
    cost_usd REAL NOT NULL DEFAULT 0,
    telem_session_id TEXT,
    search_error TEXT,
    verified_at TEXT,
    monitor_id TEXT,
    monitor_description TEXT,
    monitor_frequency TEXT,
    include_domains TEXT,
    webhook_secret TEXT,
    monitor_error TEXT,
    stale_evidence TEXT,
    stale_url TEXT,
    stale_why TEXT,
    stale_at TEXT,
    last_polled_at TEXT,
    UNIQUE (sheet_id, field)
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    field_id INTEGER NOT NULL REFERENCES fields(id) ON DELETE CASCADE,
    monitor_id TEXT,
    item_key TEXT NOT NULL,
    url TEXT,
    title TEXT,
    snippet TEXT,
    published TEXT,
    via TEXT NOT NULL,
    received_at TEXT NOT NULL,
    check_verdict TEXT,
    check_why TEXT,
    check_cost_usd REAL NOT NULL DEFAULT 0,
    check_attempts INTEGER NOT NULL DEFAULT 0,
    supersedes INTEGER NOT NULL DEFAULT 0,
    UNIQUE (field_id, item_key)
);
"""


def now_iso() -> str:
    """The current UTC time, ISO 8601, to the second."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class FactSheetStore:
    """A thin typed layer over the fact sheet database.

    Args:
        path: The SQLite file; its parent directory is created. ``":memory:"`` works.
    """

    def __init__(self, path: str | Path = DEFAULT_DB) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(items)")}
        if "check_attempts" not in columns:  # a database made before retries were recorded
            with self._tx() as conn:
                conn.execute("ALTER TABLE items ADD COLUMN check_attempts INTEGER NOT NULL DEFAULT 0")

    def close(self) -> None:
        """Close the connection."""
        self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._conn:
            yield self._conn

    # Sheets

    def create_sheet(self, company: str) -> int:
        """Create an empty sheet for ``company`` and return its id.

        Raises:
            sqlite3.IntegrityError: A sheet for this company already exists.
        """
        with self._tx() as conn:
            cur = conn.execute("INSERT INTO sheets (company, created_at) VALUES (?, ?)", (company, now_iso()))
            return int(cur.lastrowid)

    def get_sheet(self, company: str) -> dict[str, Any] | None:
        """The sheet row for ``company`` (case-insensitive), or None."""
        row = self._conn.execute("SELECT * FROM sheets WHERE company = ?", (company,)).fetchone()
        return dict(row) if row else None

    def sheets(self) -> list[dict[str, Any]]:
        """Every sheet, oldest first."""
        return [dict(row) for row in self._conn.execute("SELECT * FROM sheets ORDER BY id")]

    def set_sheet_session(self, sheet_id: int, session_id: str | None) -> None:
        """Record the Telem session the sheet's searches were grouped under."""
        with self._tx() as conn:
            conn.execute("UPDATE sheets SET telem_session_id = ? WHERE id = ?", (session_id, sheet_id))

    def delete_sheet(self, sheet_id: int) -> None:
        """Delete a sheet with its fields and items."""
        with self._tx() as conn:
            conn.execute("DELETE FROM sheets WHERE id = ?", (sheet_id,))

    # Fields

    def add_field(self, sheet_id: int, **values: Any) -> int:
        """Insert a field row; list values are stored as JSON. Returns its id."""
        values = {key: _encode(value) for key, value in values.items()}
        columns = ", ".join(["sheet_id", *values])
        marks = ", ".join("?" * (len(values) + 1))
        with self._tx() as conn:
            cur = conn.execute(f"INSERT INTO fields ({columns}) VALUES ({marks})", (sheet_id, *values.values()))
            return int(cur.lastrowid)

    def update_field(self, field_id: int, **values: Any) -> None:
        """Update named columns of one field."""
        if not values:
            return
        sets = ", ".join(f"{key} = ?" for key in values)
        with self._tx() as conn:
            conn.execute(f"UPDATE fields SET {sets} WHERE id = ?",
                         (*(_encode(value) for value in values.values()), field_id))

    def get_field(self, field_id: int) -> dict[str, Any] | None:
        """One field row with JSON columns decoded, or None."""
        row = self._conn.execute("SELECT * FROM fields WHERE id = ?", (field_id,)).fetchone()
        return _decode_field(row) if row else None

    def fields(self, sheet_id: int | None = None) -> list[dict[str, Any]]:
        """The fields of one sheet, or of every sheet, in insertion order."""
        if sheet_id is None:
            rows = self._conn.execute("SELECT * FROM fields ORDER BY id")
        else:
            rows = self._conn.execute("SELECT * FROM fields WHERE sheet_id = ? ORDER BY id", (sheet_id,))
        return [_decode_field(row) for row in rows]

    # Items

    def add_item(self, field_id: int, *, monitor_id: str | None, item_key: str, url: str | None,
                 title: str | None, snippet: str | None, published: str | None, via: str) -> int | None:
        """Store one received monitor item. Returns its id, or None if it was already stored."""
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO items (field_id, monitor_id, item_key, url, title, snippet, published, via,"
                " received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (field_id, monitor_id, item_key, url, title, snippet, published, via, now_iso()))
            return int(cur.lastrowid) if cur.rowcount else None

    def update_item(self, item_id: int, **values: Any) -> None:
        """Update named columns of one item."""
        if not values:
            return
        sets = ", ".join(f"{key} = ?" for key in values)
        with self._tx() as conn:
            conn.execute(f"UPDATE items SET {sets} WHERE id = ?", (*values.values(), item_id))

    def unchecked_items(self, field_id: int, max_attempts: int) -> list[dict[str, Any]]:
        """Items of one field with no verdict yet (new, or left by a crash) or whose check
        failed, that have had fewer than ``max_attempts`` checks, oldest first."""
        rows = self._conn.execute(
            "SELECT * FROM items WHERE field_id = ? AND (check_verdict IS NULL OR check_verdict = 'ERROR')"
            " AND check_attempts < ? ORDER BY id", (field_id, max_attempts))
        return [dict(row) for row in rows]

    def items(self, field_id: int) -> list[dict[str, Any]]:
        """Every item received for one field, oldest first."""
        rows = self._conn.execute("SELECT * FROM items WHERE field_id = ? ORDER BY id", (field_id,))
        return [dict(row) for row in rows]


def _encode(value: Any) -> Any:
    return json.dumps(list(value)) if isinstance(value, (list, tuple)) else value


def _decode_field(row: sqlite3.Row) -> dict[str, Any]:
    field = dict(row)
    field["engines_tried"] = json.loads(field.get("engines_tried") or "[]")
    domains = field.get("include_domains")
    field["include_domains"] = json.loads(domains) if domains else None
    return field
