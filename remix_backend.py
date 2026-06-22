"""Persistence helpers for SmartGallery's remote ComfyUI dispatcher."""

from __future__ import annotations

import time
import uuid
import urllib.parse
import json


REMIX_JOB_STATUSES = (
    "pending",
    "dispatching",
    "submitted",
    "running",
    "downloading",
    "completed",
    "failed",
    "cancelled",
)

ACTIVE_REMIX_JOB_STATUSES = ("dispatching", "submitted", "running", "downloading")


class RemixBackendError(ValueError):
    pass


def normalize_backend_url(value: str) -> str:
    normalized = (value or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(normalized)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise RemixBackendError(
            "ComfyUI URL must be an absolute http or https URL."
        )
    if parsed.username or parsed.password:
        raise RemixBackendError("Credentials are not supported in ComfyUI URLs.")
    if parsed.query or parsed.fragment:
        raise RemixBackendError("ComfyUI URL cannot contain a query or fragment.")
    return normalized


def list_backends(conn):
    return conn.execute(
        """
        SELECT id, name, base_url, priority, enabled, health_status,
               last_checked, last_error, created_at, updated_at
        FROM remix_backends
        ORDER BY priority ASC, created_at ASC
        """
    ).fetchall()


def create_backend(conn, name: str, base_url: str, enabled: bool = True) -> str:
    clean_name = (name or "").strip()
    if not clean_name:
        raise RemixBackendError("Backend name is required.")
    clean_url = normalize_backend_url(base_url)
    now = time.time()
    backend_id = str(uuid.uuid4())
    next_priority = conn.execute(
        "SELECT COALESCE(MAX(priority), -1) + 1 FROM remix_backends"
    ).fetchone()[0]
    try:
        conn.execute(
            """
            INSERT INTO remix_backends
                (id, name, base_url, priority, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                backend_id,
                clean_name,
                clean_url,
                next_priority,
                1 if enabled else 0,
                now,
                now,
            ),
        )
    except Exception as exc:
        if "UNIQUE constraint failed: remix_backends.base_url" in str(exc):
            raise RemixBackendError("That ComfyUI URL is already configured.") from exc
        raise
    return backend_id


def update_backend(
    conn,
    backend_id: str,
    *,
    name: str,
    base_url: str,
    enabled: bool,
) -> bool:
    clean_name = (name or "").strip()
    if not clean_name:
        raise RemixBackendError("Backend name is required.")
    clean_url = normalize_backend_url(base_url)
    try:
        cursor = conn.execute(
            """
            UPDATE remix_backends
            SET name = ?, base_url = ?, enabled = ?, updated_at = ?,
                health_status = 'unknown', last_checked = NULL, last_error = NULL
            WHERE id = ?
            """,
            (
                clean_name,
                clean_url,
                1 if enabled else 0,
                time.time(),
                backend_id,
            ),
        )
    except Exception as exc:
        if "UNIQUE constraint failed: remix_backends.base_url" in str(exc):
            raise RemixBackendError("That ComfyUI URL is already configured.") from exc
        raise
    return cursor.rowcount > 0


def reorder_backends(conn, backend_ids) -> None:
    ordered_ids = [str(value) for value in backend_ids]
    if len(ordered_ids) != len(set(ordered_ids)):
        raise RemixBackendError("Backend order contains duplicate IDs.")
    existing_ids = {
        row[0] for row in conn.execute("SELECT id FROM remix_backends").fetchall()
    }
    if set(ordered_ids) != existing_ids:
        raise RemixBackendError("Backend order must include every configured backend.")
    now = time.time()
    conn.executemany(
        "UPDATE remix_backends SET priority = ?, updated_at = ? WHERE id = ?",
        [(priority, now, backend_id) for priority, backend_id in enumerate(ordered_ids)],
    )


def delete_backend(conn, backend_id: str) -> bool:
    placeholders = ",".join("?" for _ in ACTIVE_REMIX_JOB_STATUSES)
    active = conn.execute(
        f"""
        SELECT COUNT(*) FROM remix_jobs
        WHERE backend_id = ? AND status IN ({placeholders})
        """,
        (backend_id, *ACTIVE_REMIX_JOB_STATUSES),
    ).fetchone()[0]
    if active:
        raise RemixBackendError(
            "Backend cannot be deleted while it has an active Remix job."
        )
    cursor = conn.execute("DELETE FROM remix_backends WHERE id = ?", (backend_id,))
    return cursor.rowcount > 0


def create_remix_job(
    conn,
    workflow,
    *,
    source_file_id=None,
    input_files=None,
    job_id=None,
) -> str:
    now = time.time()
    remix_job_id = job_id or str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO remix_jobs
            (id, source_file_id, workflow_json, input_files_json,
             status, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'pending', ?, ?)
        """,
        (
            remix_job_id,
            source_file_id,
            json.dumps(workflow),
            json.dumps(input_files or []),
            now,
            now,
        ),
    )
    return remix_job_id


def recover_interrupted_jobs(conn) -> None:
    """Recover only states that are safe to resume after process restart."""
    now = time.time()
    conn.execute(
        """
        UPDATE remix_jobs
        SET status = 'pending', backend_id = NULL, error = NULL, updated_at = ?
        WHERE status = 'dispatching' AND comfy_prompt_id IS NULL
        """,
        (now,),
    )
    conn.execute(
        """
        UPDATE remix_jobs
        SET status = 'submitted', updated_at = ?
        WHERE status IN ('running', 'downloading')
          AND comfy_prompt_id IS NOT NULL
        """,
        (now,),
    )


def initialize_remix_schema(conn, default_comfy_url: str) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS remix_backends (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            base_url TEXT NOT NULL UNIQUE,
            priority INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            health_status TEXT NOT NULL DEFAULT 'unknown',
            last_checked REAL,
            last_error TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS remix_jobs (
            id TEXT PRIMARY KEY,
            source_file_id TEXT,
            backend_id TEXT,
            comfy_prompt_id TEXT,
            workflow_json TEXT NOT NULL,
            input_files_json TEXT NOT NULL DEFAULT '[]',
            output_files_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            started_at REAL,
            completed_at REAL,
            FOREIGN KEY (backend_id) REFERENCES remix_backends(id)
                ON DELETE SET NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_remix_backends_priority "
        "ON remix_backends(enabled, priority)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_remix_jobs_status_created "
        "ON remix_jobs(status, created_at)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_remix_jobs_remote_prompt "
        "ON remix_jobs(backend_id, comfy_prompt_id) "
        "WHERE comfy_prompt_id IS NOT NULL"
    )

    # Preserve existing installations: the old single URL becomes backend #1.
    existing = conn.execute("SELECT COUNT(*) FROM remix_backends").fetchone()[0]
    if existing == 0 and default_comfy_url and default_comfy_url.strip():
        now = time.time()
        conn.execute(
            """
            INSERT INTO remix_backends
                (id, name, base_url, priority, enabled, created_at, updated_at)
            VALUES (?, ?, ?, 0, 1, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                "Default ComfyUI",
                normalize_backend_url(default_comfy_url),
                now,
                now,
            ),
        )
