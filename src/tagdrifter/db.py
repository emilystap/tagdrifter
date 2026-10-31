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
    # 2: whether an issue's track/album is MBID-tagged (1/0; NULL for artist/library scope)
    """
    ALTER TABLE issues ADD COLUMN mbid_tagged INTEGER;
    """,
    # 3: the album folder an issue belongs to (NULL for artist/library scope, and for issues
    # recorded before this migration until the next scan updates them)
    """
    ALTER TABLE issues ADD COLUMN album TEXT;
    CREATE INDEX issues_album ON issues(album);
    """,
    # 4: rename journal (renamer.py): one batch per applied album, its moves in order
    """
    CREATE TABLE rename_batches (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        album       TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        finished_at TEXT,
        status      TEXT NOT NULL,      -- running | applied | failed | partial | undone
        error       TEXT
    );
    CREATE TABLE rename_moves (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id INTEGER NOT NULL REFERENCES rename_batches(id),
        kind     TEXT NOT NULL,         -- folder | file | sidecar | mkdir | rmdir
        src      TEXT NOT NULL,         -- library-relative; '' for mkdir
        dst      TEXT NOT NULL,         -- library-relative; '' for rmdir
        undone   INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX rename_moves_batch ON rename_moves(batch_id);
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
    mbid_tagged: bool | None
    album: str | None


@dataclass(frozen=True)
class IssueGroup:
    """Open issues of one album folder (or, without one, of one subject)."""

    key: str  # the album folder, else the subject
    count: int
    worst_severity: str
    checks: dict[str, int]  # check id -> issues
    untagged: int  # issues on files without valid MBIDs


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@dataclass(frozen=True)
class IssueFilter:
    """Filters for open issues. Empty values mean "any"."""

    check_id: str | None = None
    severity: str | None = None
    path_prefix: str | None = None  # library-relative folder or file, e.g. an artist folder
    # False also matches artist/library-scope issues, which have no MBID status.
    mbid_tagged: bool | None = None
    text: str | None = None  # substring of subject or message

    def where(self) -> tuple[str, list[Any]]:
        sql = "resolved_scan IS NULL"
        params: list[Any] = []
        if self.check_id:
            sql += " AND check_id = ?"
            params.append(self.check_id)
        if self.severity:
            sql += " AND severity = ?"
            params.append(self.severity)
        if self.mbid_tagged is not None:
            sql += (
                " AND mbid_tagged = 1" if self.mbid_tagged else " AND COALESCE(mbid_tagged, 0) = 0"
            )
        if self.path_prefix:
            prefix = self.path_prefix.rstrip("/")
            sql += " AND (subject = ? OR subject LIKE ? ESCAPE '\\')"
            params += [prefix, _like_escape(prefix) + "/%"]
        if self.text:
            sql += " AND (subject LIKE ? ESCAPE '\\' OR message LIKE ? ESCAPE '\\')"
            pattern = f"%{_like_escape(self.text)}%"
            params += [pattern, pattern]
        return sql, params


# Issues group by album folder; issues without one (artist/library scope) stand alone.
GROUP_KEY = "COALESCE(album, subject)"


def _issue_row(r: sqlite3.Row) -> IssueRow:
    return IssueRow(
        id=r["id"],
        check_id=r["check_id"],
        subject=r["subject"],
        severity=r["severity"],
        message=r["message"],
        details=json.loads(r["details_json"]),
        first_seen_scan=r["first_seen_scan"],
        last_seen_scan=r["last_seen_scan"],
        resolved_scan=r["resolved_scan"],
        mbid_tagged=None if r["mbid_tagged"] is None else bool(r["mbid_tagged"]),
        album=r["album"],
    )


class Store:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the web app opens one Store per request, and FastAPI may run
        # a request's dependency and handler on different threadpool threads.
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
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
        return self._cached_files("SELECT path, size, mtime_ns, tags_json, error FROM files")

    def _cached_files(self, sql: str, params: Sequence[Any] = ()) -> dict[str, CachedFile]:
        out = {}
        for row in self.conn.execute(sql, params):
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

    def files_under(self, folder: str) -> dict[str, CachedFile]:
        """Cached files anywhere below a library-relative folder."""
        prefix = folder.rstrip("/") + "/"
        # substr, not LIKE: LIKE ignores ASCII letter case.
        return self._cached_files(
            "SELECT path, size, mtime_ns, tags_json, error FROM files WHERE substr(path, 1, ?) = ?",
            (len(prefix), prefix),
        )

    def files_in_folder(self, folder: str) -> dict[str, CachedFile]:
        """Cached files directly in a library-relative folder."""
        prefix = folder.rstrip("/") + "/"
        return {
            path: entry
            for path, entry in self.files_under(folder).items()
            if "/" not in path[len(prefix) :]
        }

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
                    issue.mbid_tagged,
                    issue.album,
                    scan_id,
                )
                if key in existing:
                    self.conn.execute(
                        "UPDATE issues SET severity=?, message=?, details_json=?, mbid_tagged=?,"
                        " album=?, last_seen_scan=? WHERE id=?",
                        (*params, existing[key]),
                    )
                else:
                    new += 1
                    self.conn.execute(
                        "INSERT INTO issues (check_id, subject, severity, message, details_json,"
                        " mbid_tagged, album, first_seen_scan, last_seen_scan)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (issue.check_id, issue.subject, *params[:5], scan_id, scan_id),
                    )
            gone = [issue_id for key, issue_id in existing.items() if key not in current]
            self.conn.executemany(
                "UPDATE issues SET resolved_scan = ? WHERE id = ?", [(scan_id, i) for i in gone]
            )
        return len(current), new, len(gone)

    def open_issues(
        self,
        filters: IssueFilter | None = None,
        *,
        limit: int | None = None,
        offset: int = 0,
        **filter_kwargs: Any,
    ) -> list[IssueRow]:
        """Open issues ordered by check and subject. Filter with an IssueFilter or its fields
        as keywords, e.g. `open_issues(check_id="tags.required")`."""
        where, params = (filters or IssueFilter(**filter_kwargs)).where()
        sql = f"SELECT * FROM issues WHERE {where} ORDER BY check_id, subject"
        if limit:
            sql += " LIMIT ? OFFSET ?"
            params += [limit, offset]
        return [_issue_row(r) for r in self.conn.execute(sql, params)]

    def open_issue_groups(
        self, filters: IssueFilter | None = None, *, limit: int | None = None, offset: int = 0
    ) -> list[IssueGroup]:
        """Open issues grouped by album folder, ordered by folder name (case-insensitive)."""
        where, params = (filters or IssueFilter()).where()
        sql = (
            f"SELECT {GROUP_KEY} AS grp, COUNT(*) AS n,"
            " MIN(CASE severity WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END) AS sev,"
            " SUM(CASE WHEN mbid_tagged = 0 THEN 1 ELSE 0 END) AS untagged,"
            " group_concat(check_id, char(31)) AS checks"
            f" FROM issues WHERE {where} GROUP BY grp ORDER BY grp COLLATE NOCASE, grp"
        )
        if limit:
            sql += " LIMIT ? OFFSET ?"
            params += [limit, offset]
        groups = []
        for r in self.conn.execute(sql, params):
            checks: dict[str, int] = {}
            for check_id in sorted(r["checks"].split(chr(31))):
                checks[check_id] = checks.get(check_id, 0) + 1
            groups.append(
                IssueGroup(
                    key=r["grp"],
                    count=r["n"],
                    worst_severity=("error", "warning", "info")[r["sev"]],
                    checks=checks,
                    untagged=r["untagged"],
                )
            )
        return groups

    def count_open_issue_groups(self, filters: IssueFilter | None = None) -> int:
        where, params = (filters or IssueFilter()).where()
        return self.conn.execute(
            f"SELECT COUNT(DISTINCT {GROUP_KEY}) FROM issues WHERE {where}", params
        ).fetchone()[0]

    def open_issues_in_groups(
        self, keys: Sequence[str], filters: IssueFilter | None = None
    ) -> dict[str, list[IssueRow]]:
        """The open issues (matching `filters`) of the given groups, by group key."""
        if not keys:
            return {}
        where, params = (filters or IssueFilter()).where()
        marks = ",".join("?" * len(keys))
        rows = self.conn.execute(
            f"SELECT * FROM issues WHERE {where} AND {GROUP_KEY} IN ({marks})"
            " ORDER BY check_id, subject",
            [*params, *keys],
        )
        out: dict[str, list[IssueRow]] = {key: [] for key in keys}
        for r in rows:
            out[r["album"] or r["subject"]].append(_issue_row(r))
        return out

    def count_open_issues(self, filters: IssueFilter | None = None, **filter_kwargs: Any) -> int:
        where, params = (filters or IssueFilter(**filter_kwargs)).where()
        return self.conn.execute(f"SELECT COUNT(*) FROM issues WHERE {where}", params).fetchone()[0]

    def open_issue_counts(self) -> dict[tuple[str, str], int]:
        """{(check_id, severity): count} for open issues."""
        return {
            (r["check_id"], r["severity"]): r["n"]
            for r in self.conn.execute(
                "SELECT check_id, severity, COUNT(*) AS n FROM issues"
                " WHERE resolved_scan IS NULL GROUP BY check_id, severity"
            )
        }

    def open_issue_artists(self) -> list[tuple[str, int]]:
        """(artist folder, open issue count) pairs, by folder name. Library-level subjects
        (no '/') count as their own name, e.g. a loose file."""
        return [
            (r["artist"], r["n"])
            for r in self.conn.execute(
                "SELECT substr(subject, 1, instr(subject || '/', '/') - 1) AS artist,"
                " COUNT(*) AS n FROM issues WHERE resolved_scan IS NULL"
                " GROUP BY artist ORDER BY artist COLLATE NOCASE"
            )
        ]

    def file_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]

    # --- Rename journal -----------------------------------------------------------------

    def start_rename_batch(self, album: str) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO rename_batches (album, created_at, status) VALUES (?, ?, 'running')",
                (album, utcnow()),
            )
        return int(cur.lastrowid)

    def record_rename(self, batch_id: int, kind: str, src: str, dst: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO rename_moves (batch_id, kind, src, dst) VALUES (?, ?, ?, ?)",
                (batch_id, kind, src, dst),
            )

    def mark_rename_undone(self, move_id: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE rename_moves SET undone = 1 WHERE id = ?", (move_id,))

    def finish_rename_batch(self, batch_id: int, status: str, error: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE rename_batches SET status = ?, error = ?, finished_at = ? WHERE id = ?",
                (status, error, utcnow(), batch_id),
            )

    def rename_batch(self, batch_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM rename_batches WHERE id = ?", (batch_id,)
        ).fetchone()

    def rename_batches(self, limit: int = 50) -> list[sqlite3.Row]:
        """Recent batches, newest first, with how many tracks each moved."""
        return self.conn.execute(
            "SELECT b.*, (SELECT COUNT(*) FROM rename_moves m WHERE m.batch_id = b.id"
            " AND m.kind = 'file') AS tracks FROM rename_batches b ORDER BY b.id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def rename_moves(self, batch_id: int, *, pending_only: bool = False) -> list[sqlite3.Row]:
        """A batch's moves in the order they were made."""
        sql = "SELECT * FROM rename_moves WHERE batch_id = ?"
        if pending_only:
            sql += " AND undone = 0"
        return self.conn.execute(sql + " ORDER BY id", (batch_id,)).fetchall()
