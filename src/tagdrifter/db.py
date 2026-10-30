"""SQLite persistence: file fingerprint/tag cache, scan history, and issue lifecycle.

Plain `sqlite3` keeps the dependency list short. Schema changes go in MIGRATIONS (append
only); `PRAGMA user_version` records which have run.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .checks import Issue

MIGRATIONS: list[str] = [
    # 1: initial schema
    """
    CREATE TABLE files (
        path      TEXT PRIMARY KEY,     -- library-relative, '/'-separated
        size      INTEGER NOT NULL,
        mtime_ns  INTEGER NOT NULL,
        tags_json TEXT,                 -- NULL when unreadable
        error     TEXT,
        read_at   TEXT NOT NULL
    );
    CREATE TABLE scans (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at      TEXT NOT NULL,
        finished_at     TEXT,
        status          TEXT NOT NULL,  -- running | completed | failed | cancelled
        library_path    TEXT NOT NULL,
        files_total     INTEGER NOT NULL DEFAULT 0,
        files_read      INTEGER NOT NULL DEFAULT 0,
        files_removed   INTEGER NOT NULL DEFAULT 0,
        issues_open     INTEGER NOT NULL DEFAULT 0,
        issues_new      INTEGER NOT NULL DEFAULT 0,
        issues_resolved INTEGER NOT NULL DEFAULT 0,
        error           TEXT
    );
    CREATE TABLE issues (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        check_id        TEXT NOT NULL,
        subject         TEXT NOT NULL,
        severity        TEXT NOT NULL,
        message         TEXT NOT NULL,
        details_json    TEXT NOT NULL,
        first_seen_scan INTEGER NOT NULL REFERENCES scans(id),
        last_seen_scan  INTEGER NOT NULL REFERENCES scans(id),
        resolved_scan   INTEGER REFERENCES scans(id)
    );
    CREATE UNIQUE INDEX issues_open_key ON issues(check_id, subject) WHERE resolved_scan IS NULL;
    CREATE INDEX issues_subject ON issues(subject);
    """,
]


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class CachedFile:
    size: int
    mtime_ns: int
    tags: dict[str, tuple[str, ...]] | None
    error: str | None


@dataclass(frozen=True)
class IssueRow:
    id: int
    check_id: str
    subject: str
    severity: str
    message: str
    details: dict[str, Any]
    first_seen_scan: int
    last_seen_scan: int
    resolved_scan: int | None


class Store:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for index, script in enumerate(MIGRATIONS[version:], start=version + 1):
            with self.conn:
                self.conn.executescript(script)
                self.conn.execute(f"PRAGMA user_version = {index}")

    # --- File cache ---------------------------------------------------------------------

    def load_file_cache(self) -> dict[str, CachedFile]:
        out = {}
        for row in self.conn.execute("SELECT path, size, mtime_ns, tags_json, error FROM files"):
            tags = (
                {k: tuple(v) for k, v in json.loads(row["tags_json"]).items()}
                if row["tags_json"] is not None
                else None
            )
            out[row["path"]] = CachedFile(row["size"], row["mtime_ns"], tags, row["error"])
        return out

    def save_files(
        self, rows: Iterable[tuple[str, int, int, Mapping[str, Sequence[str]] | None, str | None]]
    ) -> None:
        """Upsert (path, size, mtime_ns, tags | None, error | None) rows."""
        now = utcnow()
        with self.conn:
            self.conn.executemany(
                """INSERT INTO files (path, size, mtime_ns, tags_json, error, read_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET
                     size=excluded.size, mtime_ns=excluded.mtime_ns,
                     tags_json=excluded.tags_json, error=excluded.error,
                     read_at=excluded.read_at""",
                [
                    (
                        path,
                        size,
                        mtime,
                        json.dumps(tags, ensure_ascii=False) if tags is not None else None,
                        error,
                        now,
                    )
                    for path, size, mtime, tags, error in rows
                ],
            )

    def delete_files(self, paths: Iterable[str]) -> None:
        with self.conn:
            self.conn.executemany("DELETE FROM files WHERE path = ?", [(p,) for p in paths])

    # --- Scans --------------------------------------------------------------------------

    def start_scan(self, library_path: str) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO scans (started_at, status, library_path) VALUES (?, 'running', ?)",
                (utcnow(), library_path),
            )
        return int(cur.lastrowid)

    def finish_scan(
        self, scan_id: int, status: str, error: str | None = None, **counts: int
    ) -> None:
        allowed = {
            "files_total",
            "files_read",
            "files_removed",
            "issues_open",
            "issues_new",
            "issues_resolved",
        }
        if unknown := counts.keys() - allowed:
            raise ValueError(f"unknown scan counters: {unknown}")
        sets = ", ".join(f"{k} = :{k}" for k in counts)
        with self.conn:
            self.conn.execute(
                f"UPDATE scans SET finished_at = :now, status = :status, error = :error"
                f"{', ' + sets if sets else ''} WHERE id = :id",
                {"now": utcnow(), "status": status, "error": error, "id": scan_id, **counts},
            )

    def recent_scans(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM scans ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    # --- Issues -------------------------------------------------------------------------

    def sync_issues(self, scan_id: int, issues: Iterable[Issue]) -> tuple[int, int, int]:
        """Record this scan's issues. Returns (open, new, resolved).

        Issues are identified by (check_id, subject). Ones seen before stay open with their
        original first_seen_scan; open ones not seen this scan are marked resolved.
        """
        current = {i.key: i for i in issues}  # last one wins if a check repeats a subject
        new = 0
        with self.conn:
            existing = {
                (r["check_id"], r["subject"]): r["id"]
                for r in self.conn.execute(
                    "SELECT id, check_id, subject FROM issues WHERE resolved_scan IS NULL"
                )
            }
            for key, issue in current.items():
                params = (
                    issue.severity.value,
                    issue.message,
                    json.dumps(issue.details, ensure_ascii=False, default=str),
                    scan_id,
                )
                if key in existing:
                    self.conn.execute(
                        "UPDATE issues SET severity=?, message=?, details_json=?, last_seen_scan=?"
                        " WHERE id=?",
                        (*params, existing[key]),
                    )
                else:
                    new += 1
                    self.conn.execute(
                        "INSERT INTO issues (check_id, subject, severity, message, details_json,"
                        " first_seen_scan, last_seen_scan) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (issue.check_id, issue.subject, *params[:3], scan_id, scan_id),
                    )
            gone = [issue_id for key, issue_id in existing.items() if key not in current]
            self.conn.executemany(
                "UPDATE issues SET resolved_scan = ? WHERE id = ?", [(scan_id, i) for i in gone]
            )
        return len(current), new, len(gone)

    def open_issues(
        self,
        *,
        check_id: str | None = None,
        path_prefix: str | None = None,
        severity: str | None = None,
        limit: int | None = None,
    ) -> list[IssueRow]:
        sql = "SELECT * FROM issues WHERE resolved_scan IS NULL"
        params: list[Any] = []
        if check_id:
            sql += " AND check_id = ?"
            params.append(check_id)
        if severity:
            sql += " AND severity = ?"
            params.append(severity)
        if path_prefix:
            sql += " AND (subject = ? OR subject LIKE ? ESCAPE '\\')"
            escaped = path_prefix.rstrip("/").replace("\\", "\\\\").replace("%", "\\%")
            escaped = escaped.replace("_", "\\_")
            params += [path_prefix.rstrip("/"), escaped + "/%"]
        sql += " ORDER BY check_id, subject"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [
            IssueRow(
                id=r["id"],
                check_id=r["check_id"],
                subject=r["subject"],
                severity=r["severity"],
                message=r["message"],
                details=json.loads(r["details_json"]),
                first_seen_scan=r["first_seen_scan"],
                last_seen_scan=r["last_seen_scan"],
                resolved_scan=r["resolved_scan"],
            )
            for r in self.conn.execute(sql, params)
        ]

    def open_issue_counts(self) -> dict[tuple[str, str], int]:
        """{(check_id, severity): count} for open issues."""
        return {
            (r["check_id"], r["severity"]): r["n"]
            for r in self.conn.execute(
                "SELECT check_id, severity, COUNT(*) AS n FROM issues"
                " WHERE resolved_scan IS NULL GROUP BY check_id, severity"
            )
        }
