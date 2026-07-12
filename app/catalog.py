"""SQLite catalog for long-lived RadarVault frame archives.

The catalog is deliberately independent of the collector and filesystem
layout.  It can be rebuilt from an existing cache and is safe to use from
multiple worker threads or processes.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class FrameRecord:
    radar_id: str
    filename: str
    path: str
    preview_path: str | None
    product: str
    observed_at: str | None
    fetched_at: str
    width: int
    height: int
    media_type: str
    source_sha256: str
    stored_sha256: str
    bytes: int
    pinned: bool = False

    def __post_init__(self) -> None:
        if not self.radar_id.strip() or not self.filename.strip():
            raise ValueError("radar_id and filename are required")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("frame dimensions must be positive")
        if self.bytes < 0:
            raise ValueError("frame bytes must be >= 0")


SCHEMA = """
CREATE TABLE IF NOT EXISTS frames (
    radar_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    path TEXT NOT NULL,
    preview_path TEXT,
    product TEXT NOT NULL,
    observed_at TEXT,
    fetched_at TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    stored_sha256 TEXT NOT NULL,
    bytes INTEGER NOT NULL CHECK (bytes >= 0),
    pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
    PRIMARY KEY (radar_id, filename)
);
CREATE INDEX IF NOT EXISTS idx_frames_radar_observed
    ON frames (radar_id, observed_at, filename);
CREATE INDEX IF NOT EXISTS idx_frames_radar_fetched
    ON frames (radar_id, fetched_at, filename);
CREATE INDEX IF NOT EXISTS idx_frames_source_hash
    ON frames (source_sha256);
CREATE INDEX IF NOT EXISTS idx_frames_stored_hash
    ON frames (stored_sha256);
CREATE INDEX IF NOT EXISTS idx_frames_pinned
    ON frames (pinned, radar_id, fetched_at);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _record_from_row(row: sqlite3.Row) -> FrameRecord:
    values = dict(row)
    values["pinned"] = bool(values["pinned"])
    return FrameRecord(**values)


class Catalog:
    """A small SQLite index over archived frames.

    One connection is kept per Catalog instance and guarded by an RLock.  A
    separate instance in another process gets its own connection, while WAL
    mode keeps readers responsive during writes.
    """

    def __init__(self, database: str | Path) -> None:
        self.database = Path(database).expanduser()
        if str(self.database) != ":memory:":
            self.database.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.database), timeout=30.0, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._configure()

    def _configure(self) -> None:
        with self._lock:
            # WAL is unavailable for a pure in-memory database; ignore that
            # one expected limitation while preserving all other pragmas.
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    @property
    def connection(self) -> sqlite3.Connection:
        """Return the connection for read-only maintenance helpers.

        Callers should prefer the methods on :class:`Catalog`; this property
        exists for diagnostics and migration tooling and is deliberately not
        exposed as a mutable public API in the application layer.
        """
        return self._conn

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "Catalog":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def record_frame(self, record: FrameRecord) -> None:
        """Insert or refresh a frame record atomically.

        A previously pinned frame remains pinned when a collector refreshes
        its metadata.  This prevents an incidental rescan from unpinning a
        user's protected archive.
        """
        rid = record.radar_id.strip().upper()
        with self._lock:
            try:
                self._conn.execute("BEGIN")
                self._upsert(record, radar_id=rid)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def record_frames(self, records: Iterable[FrameRecord]) -> int:
        """Insert many records in one transaction and return the row count.

        Rebuilds and long-running collectors use this method to avoid one
        fsync/commit per frame.  The same upsert semantics as ``record_frame``
        apply, including preservation of an existing pinned bit.
        """
        items = list(records)
        if not items:
            return 0
        with self._lock:
            try:
                self._conn.execute("BEGIN")
                for record in items:
                    self._upsert(record, radar_id=record.radar_id.strip().upper())
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return len(items)

    def _upsert(self, record: FrameRecord, *, radar_id: str) -> None:
        self._conn.execute(
            """
            INSERT INTO frames (
                radar_id, filename, path, preview_path, product,
                observed_at, fetched_at, width, height, media_type,
                source_sha256, stored_sha256, bytes, pinned
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(radar_id, filename) DO UPDATE SET
                path=excluded.path,
                preview_path=excluded.preview_path,
                product=excluded.product,
                observed_at=excluded.observed_at,
                fetched_at=excluded.fetched_at,
                width=excluded.width,
                height=excluded.height,
                media_type=excluded.media_type,
                source_sha256=excluded.source_sha256,
                stored_sha256=excluded.stored_sha256,
                bytes=excluded.bytes,
                pinned=CASE WHEN frames.pinned = 1 THEN 1 ELSE excluded.pinned END
            """,
            (
                radar_id,
                record.filename,
                record.path,
                record.preview_path,
                record.product,
                record.observed_at,
                record.fetched_at,
                record.width,
                record.height,
                record.media_type,
                record.source_sha256,
                record.stored_sha256,
                int(record.bytes),
                int(record.pinned),
            ),
        )

    def get_frame(self, radar_id: str, filename: str) -> FrameRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM frames WHERE radar_id = ? AND filename = ?",
                (radar_id.strip().upper(), filename),
            ).fetchone()
        return _record_from_row(row) if row else None

    def list_frames(
        self,
        radar_id: str,
        *,
        start: str | datetime | None = None,
        end: str | datetime | None = None,
        after: str | None = None,
        limit: int = 500,
    ) -> list[FrameRecord]:
        """Return a bounded page ordered by observed/fetched time and name.

        ``after`` is an opaque filename cursor.  Filenames in the legacy
        cache are timestamp ordered, and using the primary-key filename keeps
        pagination stable even when observation timestamps are unknown.
        """
        if limit <= 0:
            return []
        limit = min(int(limit), 10_000)
        clauses = ["radar_id = ?"]
        params: list[Any] = [radar_id.strip().upper()]
        if start is not None:
            clauses.append("COALESCE(observed_at, fetched_at) >= ?")
            params.append(_as_iso(start))
        if end is not None:
            clauses.append("COALESCE(observed_at, fetched_at) <= ?")
            params.append(_as_iso(end))
        cursor_time: str | None = None
        if after:
            # The public cursor is normally a filename.  Resolve its sort
            # key so pagination remains correct when observed_at differs
            # from filename order.  Unknown cursors retain a useful fallback.
            with self._lock:
                cursor = self._conn.execute(
                    "SELECT COALESCE(observed_at, fetched_at) AS sort_time "
                    "FROM frames WHERE radar_id = ? AND filename = ?",
                    (radar_id.strip().upper(), after),
                ).fetchone()
            if cursor:
                cursor_time = str(cursor["sort_time"])
                clauses.append(
                    "(COALESCE(observed_at, fetched_at) > ? OR "
                    "(COALESCE(observed_at, fetched_at) = ? AND filename > ?))"
                )
                params.extend([cursor_time, cursor_time, after])
            else:
                clauses.append("filename > ?")
                params.append(after)
        params.append(limit)
        query = (
            "SELECT * FROM frames WHERE "
            + " AND ".join(clauses)
            + " ORDER BY COALESCE(observed_at, fetched_at), filename LIMIT ?"
        )
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [_record_from_row(row) for row in rows]

    def all_frames(self, *, limit: int | None = None) -> list[FrameRecord]:
        """Return catalog records for maintenance tools.

        Retention uses this bounded optional API rather than walking every
        frame file.  ``limit=None`` is intentional for local maintenance,
        while normal API callers should use :meth:`list_frames`.
        """
        query = "SELECT * FROM frames ORDER BY COALESCE(observed_at, fetched_at), radar_id, filename"
        params: tuple[Any, ...] = ()
        if limit is not None:
            query += " LIMIT ?"
            params = (max(0, int(limit)),)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [_record_from_row(row) for row in rows]

    def radar_ids(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT radar_id FROM frames ORDER BY radar_id"
            ).fetchall()
        return [str(row[0]) for row in rows]

    def latest_frame(self, radar_id: str) -> FrameRecord | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM frames WHERE radar_id = ?
                ORDER BY COALESCE(observed_at, fetched_at) DESC, filename DESC
                LIMIT 1
                """,
                (radar_id.strip().upper(),),
            ).fetchone()
        return _record_from_row(row) if row else None

    def radar_stats(self, radar_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS frame_count,
                       COALESCE(SUM(bytes), 0) AS bytes,
                       SUM(CASE WHEN pinned = 1 THEN 1 ELSE 0 END) AS pinned_count,
                       COALESCE(SUM(CASE WHEN pinned = 1 THEN bytes ELSE 0 END), 0) AS pinned_bytes,
                       MIN(COALESCE(observed_at, fetched_at)) AS first_utc,
                       MAX(COALESCE(observed_at, fetched_at)) AS last_utc
                FROM frames WHERE radar_id = ?
                """,
                (radar_id.strip().upper(),),
            ).fetchone()
        result = dict(row)
        result["radar_id"] = radar_id.strip().upper()
        result["pinned_count"] = int(result["pinned_count"] or 0)
        return result

    def global_stats(self) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS frame_count,
                       COUNT(DISTINCT radar_id) AS radar_count,
                       COALESCE(SUM(bytes), 0) AS bytes,
                       SUM(CASE WHEN pinned = 1 THEN 1 ELSE 0 END) AS pinned_count,
                       COALESCE(SUM(CASE WHEN pinned = 1 THEN bytes ELSE 0 END), 0) AS pinned_bytes,
                       MIN(COALESCE(observed_at, fetched_at)) AS first_utc,
                       MAX(COALESCE(observed_at, fetched_at)) AS last_utc
                FROM frames
                """
            ).fetchone()
        result = dict(row)
        result["pinned_count"] = int(result["pinned_count"] or 0)
        return result

    def set_pinned(self, radar_id: str, filename: str, pinned: bool) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE frames SET pinned = ? WHERE radar_id = ? AND filename = ?",
                (int(bool(pinned)), radar_id.strip().upper(), filename),
            )
            self._conn.commit()

    def count(self, radar_id: str | None = None) -> int:
        """Return a catalog row count without materializing records."""
        with self._lock:
            if radar_id is None:
                row = self._conn.execute("SELECT COUNT(*) FROM frames").fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM frames WHERE radar_id = ?",
                    (radar_id.strip().upper(),),
                ).fetchone()
        return int(row[0])

    def delete_frame_record(self, radar_id: str, filename: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM frames WHERE radar_id = ? AND filename = ?",
                (radar_id.strip().upper(), filename),
            )
            self._conn.commit()

    def verify(self) -> dict[str, Any]:
        with self._lock:
            integrity = str(self._conn.execute("PRAGMA integrity_check").fetchone()[0])
            table = self._conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='frames'"
            ).fetchone()[0]
            journal = str(self._conn.execute("PRAGMA journal_mode").fetchone()[0])
        return {"ok": integrity == "ok" and bool(table), "integrity": integrity, "journal_mode": journal}


def _as_iso(value: str | datetime) -> str:
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return value


def record_from_mapping(data: dict[str, Any]) -> FrameRecord:
    """Construct a record from JSON/CLI input with conservative defaults."""
    fields = {key: data.get(key) for key in FrameRecord.__dataclass_fields__}
    fields["preview_path"] = fields["preview_path"] or None
    fields["pinned"] = bool(fields["pinned"])
    return FrameRecord(**fields)  # type: ignore[arg-type]


__all__ = ["Catalog", "FrameRecord", "record_from_mapping"]
