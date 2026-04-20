"""Settings Console — SQLite persistence layer.

All durable state lives in ``data/settings.db`` (single-writer file model
under the edge process). The schema is intentionally tiny — six tables —
and is bootstrapped by a one-shot migration runner at module import time.

Tables:
    * ``migrations``         — applied schema versions.
    * ``templates``          — operator-named presets (soft delete).
    * ``template_revisions`` — immutable history per template.
    * ``apply_log``          — every settings apply (success or failure).
    * ``baselines``          — captured baseline windows for impact compare.
    * ``impact_sessions``    — active + archived impact monitoring sessions.

The module exposes a small set of free functions plus a context-managed
:func:`connect` so callers do not have to think about cursor lifecycle.
SQLite is opened with ``check_same_thread=False`` because the FastAPI
worker thread and the impact-engine background task both write.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from road_safety.config import DATA_DIR


_DB_PATH: Path = DATA_DIR / "settings.db"


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------
# A single shared connection (and a lock) is the simplest reliable pattern
# for SQLite under the edge process. Multiple connections to the same file
# are also fine, but they make WAL bookkeeping noisier; one connection +
# a lock keeps writes serialized and reads cheap.
_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _open() -> sqlite3.Connection:
    """Open (and memoize) the singleton connection, ensuring the dir exists."""
    global _conn
    if _conn is not None:
        return _conn
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        _DB_PATH,
        check_same_thread=False,
        isolation_level=None,  # autocommit; we wrap in explicit transactions when needed
        timeout=5.0,
    )
    # Row factory so cursor results behave like dicts.
    conn.row_factory = sqlite3.Row
    # WAL gives much better read concurrency vs the default rollback journal.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _conn = conn
    _migrate(conn)
    return conn


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Context-managed access to the shared connection.

    Holds the module-level lock for the duration of the ``with`` block —
    fine for our write volumes (handfuls of apply rows per day plus the
    impact-engine's 15 s tick).
    """
    with _lock:
        yield _open()


# ---------------------------------------------------------------------------
# Schema migrations
# ---------------------------------------------------------------------------
def _migrate(conn: sqlite3.Connection) -> None:
    """Apply any unapplied migrations.

    Migrations are tiny and inline; bumping the schema version means adding
    a new ``if`` arm here and a single CREATE / ALTER block.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS migrations (
            version INTEGER PRIMARY KEY,
            applied_at REAL NOT NULL
        )
        """
    )
    cur = conn.execute("SELECT MAX(version) AS v FROM migrations")
    current = cur.fetchone()["v"] or 0

    if current < 1:
        conn.executescript(
            """
            CREATE TABLE templates (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                system INTEGER NOT NULL DEFAULT 0,
                soft_deleted_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE template_revisions (
                id TEXT PRIMARY KEY,
                template_id TEXT NOT NULL,
                revision_no INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                created_at REAL NOT NULL,
                created_by_label TEXT,
                FOREIGN KEY (template_id) REFERENCES templates(id) ON DELETE CASCADE
            );

            CREATE INDEX idx_revisions_template ON template_revisions(template_id, revision_no);

            CREATE TABLE apply_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                actor_label TEXT NOT NULL,
                revision_hash_before TEXT NOT NULL,
                revision_hash_after TEXT NOT NULL,
                result TEXT NOT NULL,
                warnings_json TEXT NOT NULL DEFAULT '[]',
                audit_id TEXT,
                payload_json TEXT NOT NULL
            );

            CREATE INDEX idx_apply_log_ts ON apply_log(ts DESC);
            CREATE INDEX idx_apply_log_audit ON apply_log(audit_id);

            CREATE TABLE baselines (
                id TEXT PRIMARY KEY,
                audit_id TEXT NOT NULL,
                settings_hash TEXT NOT NULL,
                captured_start REAL NOT NULL,
                captured_end REAL NOT NULL,
                sample_count INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE INDEX idx_baselines_audit ON baselines(audit_id);

            CREATE TABLE impact_sessions (
                id TEXT PRIMARY KEY,
                audit_id TEXT NOT NULL UNIQUE,
                change_ts REAL NOT NULL,
                actor_label TEXT NOT NULL,
                before_json TEXT NOT NULL,
                after_json TEXT NOT NULL,
                baseline_id TEXT,
                last_payload_json TEXT,
                state TEXT NOT NULL DEFAULT 'monitoring',
                archived_at REAL,
                FOREIGN KEY (baseline_id) REFERENCES baselines(id)
            );

            CREATE INDEX idx_impact_state ON impact_sessions(state, change_ts DESC);
            """
        )
        conn.execute(
            "INSERT INTO migrations(version, applied_at) VALUES (?, ?)",
            (1, time.time()),
        )


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
def insert_template(
    *,
    template_id: str,
    name: str,
    description: str,
    payload: dict[str, Any],
    schema_version: int,
    payload_hash: str,
    actor_label: str,
    system: bool = False,
) -> dict[str, Any]:
    """Insert a brand-new template plus its initial revision (rev 1)."""
    now = time.time()
    with connect() as conn:
        conn.execute("BEGIN")
        try:
            conn.execute(
                """
                INSERT INTO templates(id, name, description, system, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (template_id, name, description, 1 if system else 0, now, now),
            )
            rev_id = f"{template_id}:rev1"
            conn.execute(
                """
                INSERT INTO template_revisions(
                    id, template_id, revision_no, payload_json, payload_hash,
                    schema_version, created_at, created_by_label
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rev_id,
                    template_id,
                    1,
                    json.dumps(payload, sort_keys=True),
                    payload_hash,
                    schema_version,
                    now,
                    actor_label,
                ),
            )
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
    return get_template(template_id)


def update_template(
    template_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    payload: dict[str, Any] | None = None,
    schema_version: int | None = None,
    payload_hash: str | None = None,
    actor_label: str = "system",
) -> dict[str, Any]:
    """Patch a template's metadata and/or append a new immutable revision.

    A new revision is only appended when ``payload`` is provided.
    """
    now = time.time()
    with connect() as conn:
        cur = conn.execute("SELECT system FROM templates WHERE id=?", (template_id,))
        row = cur.fetchone()
        if row is None:
            raise KeyError(template_id)
        if row["system"]:
            raise PermissionError(f"template {template_id} is system-owned and read-only")

        conn.execute("BEGIN")
        try:
            if name is not None or description is not None:
                conn.execute(
                    """
                    UPDATE templates SET
                        name = COALESCE(?, name),
                        description = COALESCE(?, description),
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (name, description, now, template_id),
                )
            if payload is not None:
                cur = conn.execute(
                    "SELECT MAX(revision_no) AS rn FROM template_revisions WHERE template_id=?",
                    (template_id,),
                )
                next_no = (cur.fetchone()["rn"] or 0) + 1
                conn.execute(
                    """
                    INSERT INTO template_revisions(
                        id, template_id, revision_no, payload_json, payload_hash,
                        schema_version, created_at, created_by_label
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"{template_id}:rev{next_no}",
                        template_id,
                        next_no,
                        json.dumps(payload, sort_keys=True),
                        payload_hash or "",
                        schema_version or 0,
                        now,
                        actor_label,
                    ),
                )
                conn.execute(
                    "UPDATE templates SET updated_at=? WHERE id=?",
                    (now, template_id),
                )
            conn.execute("COMMIT")
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
    return get_template(template_id)


def soft_delete_template(template_id: str) -> bool:
    """Mark a template as deleted; raises ``PermissionError`` for system rows.

    Returns True if the row was modified, False if it was already deleted.
    """
    now = time.time()
    with connect() as conn:
        cur = conn.execute("SELECT system, soft_deleted_at FROM templates WHERE id=?", (template_id,))
        row = cur.fetchone()
        if row is None:
            raise KeyError(template_id)
        if row["system"]:
            raise PermissionError("system templates cannot be deleted")
        if row["soft_deleted_at"] is not None:
            return False
        conn.execute(
            "UPDATE templates SET soft_deleted_at=?, updated_at=? WHERE id=?",
            (now, now, template_id),
        )
    return True


def get_template(template_id: str, *, include_deleted: bool = False) -> dict[str, Any] | None:
    """Return the template metadata dict (without revisions) or ``None``."""
    with connect() as conn:
        cur = conn.execute(
            "SELECT * FROM templates WHERE id=?",
            (template_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    if row["soft_deleted_at"] is not None and not include_deleted:
        return None
    return _row_to_template(row)


def list_templates(*, include_deleted: bool = False) -> list[dict[str, Any]]:
    """Return all templates ordered by created_at ascending.

    Includes the latest revision payload inline for the UI's quick "apply" flow.
    """
    with connect() as conn:
        if include_deleted:
            cur = conn.execute("SELECT * FROM templates ORDER BY created_at ASC")
        else:
            cur = conn.execute(
                "SELECT * FROM templates WHERE soft_deleted_at IS NULL ORDER BY created_at ASC"
            )
        rows = cur.fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        tmpl = _row_to_template(row)
        latest = latest_revision(tmpl["id"])
        if latest is not None:
            tmpl["payload"] = latest["payload"]
            tmpl["latest_revision_no"] = latest["revision_no"]
            tmpl["payload_hash"] = latest["payload_hash"]
        out.append(tmpl)
    return out


def latest_revision(template_id: str) -> dict[str, Any] | None:
    """Return the most recent revision dict for ``template_id`` or ``None``."""
    with connect() as conn:
        cur = conn.execute(
            """
            SELECT * FROM template_revisions
            WHERE template_id=? ORDER BY revision_no DESC LIMIT 1
            """,
            (template_id,),
        )
        row = cur.fetchone()
    return _row_to_revision(row) if row else None


def list_revisions(template_id: str) -> list[dict[str, Any]]:
    """All revisions for ``template_id``, oldest first."""
    with connect() as conn:
        cur = conn.execute(
            "SELECT * FROM template_revisions WHERE template_id=? ORDER BY revision_no ASC",
            (template_id,),
        )
        rows = cur.fetchall()
    return [_row_to_revision(r) for r in rows]


# ---------------------------------------------------------------------------
# Apply log
# ---------------------------------------------------------------------------
def insert_apply_log(
    *,
    actor_label: str,
    revision_hash_before: str,
    revision_hash_after: str,
    result: str,
    warnings: list[str],
    payload: dict[str, Any],
    audit_id: str | None = None,
) -> int:
    """Append one row to the apply log; returns the new row id."""
    now = time.time()
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO apply_log(
                ts, actor_label, revision_hash_before, revision_hash_after,
                result, warnings_json, audit_id, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                actor_label,
                revision_hash_before,
                revision_hash_after,
                result,
                json.dumps(warnings),
                audit_id,
                json.dumps(payload, sort_keys=True, default=str),
            ),
        )
        return cur.lastrowid


def list_apply_log(limit: int = 50) -> list[dict[str, Any]]:
    with connect() as conn:
        cur = conn.execute(
            "SELECT * FROM apply_log ORDER BY ts DESC LIMIT ?",
            (limit,),
        )
        rows = cur.fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["warnings"] = json.loads(d.pop("warnings_json") or "[]")
        d["payload"] = json.loads(d.pop("payload_json") or "{}")
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------
def insert_baseline(
    *,
    baseline_id: str,
    audit_id: str,
    settings_hash: str,
    captured_start: float,
    captured_end: float,
    sample_count: int,
    payload: dict[str, Any],
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO baselines(
                id, audit_id, settings_hash, captured_start, captured_end,
                sample_count, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                baseline_id,
                audit_id,
                settings_hash,
                captured_start,
                captured_end,
                sample_count,
                json.dumps(payload, default=str),
                time.time(),
            ),
        )


def get_baseline(baseline_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        cur = conn.execute("SELECT * FROM baselines WHERE id=?", (baseline_id,))
        row = cur.fetchone()
    if row is None:
        return None
    out = dict(row)
    out["payload"] = json.loads(out.pop("payload_json") or "{}")
    return out


def baseline_for_audit(audit_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        cur = conn.execute(
            "SELECT * FROM baselines WHERE audit_id=? ORDER BY created_at DESC LIMIT 1",
            (audit_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    out = dict(row)
    out["payload"] = json.loads(out.pop("payload_json") or "{}")
    return out


# ---------------------------------------------------------------------------
# Impact sessions
# ---------------------------------------------------------------------------
def upsert_impact_session(
    *,
    session_id: str,
    audit_id: str,
    change_ts: float,
    actor_label: str,
    before: dict[str, Any],
    after: dict[str, Any],
    baseline_id: str | None,
    last_payload: dict[str, Any] | None,
    state: str,
    archived_at: float | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO impact_sessions(
                id, audit_id, change_ts, actor_label, before_json, after_json,
                baseline_id, last_payload_json, state, archived_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(audit_id) DO UPDATE SET
                last_payload_json = excluded.last_payload_json,
                state = excluded.state,
                archived_at = excluded.archived_at,
                after_json = excluded.after_json
            """,
            (
                session_id,
                audit_id,
                change_ts,
                actor_label,
                json.dumps(before, default=str),
                json.dumps(after, default=str),
                baseline_id,
                json.dumps(last_payload, default=str) if last_payload else None,
                state,
                archived_at,
            ),
        )


def get_active_impact_session() -> dict[str, Any] | None:
    """Return the most-recent ``state='monitoring'`` session, if any."""
    with connect() as conn:
        cur = conn.execute(
            """
            SELECT * FROM impact_sessions
            WHERE state IN ('monitoring','monitoring_unattended')
            ORDER BY change_ts DESC LIMIT 1
            """
        )
        row = cur.fetchone()
    return _row_to_session(row) if row else None


def get_impact_session(audit_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        cur = conn.execute("SELECT * FROM impact_sessions WHERE audit_id=?", (audit_id,))
        row = cur.fetchone()
    return _row_to_session(row) if row else None


def list_archived_sessions(limit: int = 20) -> list[dict[str, Any]]:
    with connect() as conn:
        cur = conn.execute(
            """
            SELECT * FROM impact_sessions WHERE state='archived'
            ORDER BY archived_at DESC LIMIT ?
            """,
            (limit,),
        )
        rows = cur.fetchall()
    return [_row_to_session(r) for r in rows]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _row_to_template(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "system": bool(row["system"]),
        "soft_deleted_at": row["soft_deleted_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _row_to_revision(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "template_id": row["template_id"],
        "revision_no": row["revision_no"],
        "payload": json.loads(row["payload_json"]),
        "payload_hash": row["payload_hash"],
        "schema_version": row["schema_version"],
        "created_at": row["created_at"],
        "created_by_label": row["created_by_label"],
    }


def _row_to_session(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "audit_id": row["audit_id"],
        "change_ts": row["change_ts"],
        "actor_label": row["actor_label"],
        "before": json.loads(row["before_json"]),
        "after": json.loads(row["after_json"]),
        "baseline_id": row["baseline_id"],
        "last_payload": json.loads(row["last_payload_json"]) if row["last_payload_json"] else None,
        "state": row["state"],
        "archived_at": row["archived_at"],
    }


# ---------------------------------------------------------------------------
# Test helper — never call from production code
# ---------------------------------------------------------------------------
def _reset_for_tests(path: Path | None = None) -> None:
    """Re-point the singleton at a fresh DB. Used by the pytest fixtures.

    The tests pass a tmp_path-derived ``Path`` so each test gets isolation.
    Production code never calls this.
    """
    global _conn, _DB_PATH
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except sqlite3.Error:
                pass
        _conn = None
        if path is not None:
            _DB_PATH = path
