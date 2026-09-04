"""SQLite store.

The database is a single file committed to the repo by the Actions runner. That
buys three things at once: state survives ephemeral runners, every change is
version-controlled and auditable via git history, and the commit doubles as the
repository activity that keeps GitHub from auto-disabling scheduled workflows
after 60 days of quiet.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from ff.logging_setup import get_logger

log = get_logger(__name__)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
SCHEMA_VERSION = "1"


def utcnow() -> str:
    """ISO-8601 UTC. Every timestamp in this system is UTC, no exceptions."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def content_hash(payload: Any) -> str:
    """Stable hash of a structure, for cheap change detection.

    sort_keys is load-bearing -- without it dict ordering makes identical data
    hash differently and every cycle looks like a change.
    """
    blob = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


class Store:
    """Thin wrapper over sqlite3 with the conveniences we actually use."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._ensure_schema()

    # -- lifecycle ---------------------------------------------------------

    def _ensure_schema(self) -> None:
        self.conn.executescript(SCHEMA_PATH.read_text())
        self.conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (SCHEMA_VERSION,),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- primitives --------------------------------------------------------

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        self.conn.executemany(sql, rows)

    def commit(self) -> None:
        self.conn.commit()

    # -- change detection --------------------------------------------------

    def global_changed(self, key: str, payload: Any) -> bool:
        """True if `payload` differs from what we last saw under `key`.

        This is the whole of stage 1: pure hashing, no model, no cost.
        """
        new = content_hash(payload)
        row = self.one("SELECT hash FROM global_state WHERE key = ?", (key,))
        if row and row["hash"] == new:
            return False
        self.execute(
            "INSERT INTO global_state(key, hash, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET hash=excluded.hash, updated_at=excluded.updated_at",
            (key, new, utcnow()),
        )
        return True

    # -- preferences (global scope; per-league lives on LeagueContext) ------

    def set_preference(self, key: str, value: str, league_id: int | None = None) -> None:
        self.execute(
            "INSERT INTO preferences(league_id, key, value, created_at) VALUES(?,?,?,?)",
            (league_id, key, value, utcnow()),
        )
        self.commit()

    def preferences(self, league_id: int | None = None) -> list[sqlite3.Row]:
        """Preferences applying to a league: its own plus the global ones."""
        if league_id is None:
            return self.query("SELECT * FROM preferences WHERE league_id IS NULL")
        return self.query(
            "SELECT * FROM preferences WHERE league_id IS NULL OR league_id = ? "
            "ORDER BY created_at",
            (league_id,),
        )

    def clear_preferences(self, key: str, league_id: int | None = None) -> int:
        cur = self.execute(
            "DELETE FROM preferences WHERE key = ? AND league_id IS ?", (key, league_id)
        )
        self.commit()
        return cur.rowcount
