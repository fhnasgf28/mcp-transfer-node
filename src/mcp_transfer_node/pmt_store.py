from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

from mcp_transfer_node.pmt_reports import (
    MAX_REPORT_ITEMS_PER_SECTION,
    MAX_REPORT_ACTOR_LENGTH,
    MAX_REPORT_OWNER_LENGTH,
    MAX_REPORT_VERSION,
    clean_text,
    local_date,
    parse_report_date,
    render_report,
    utc_date_window,
    validate_overrides,
    validate_period,
)

TASK_STATUSES = {
    "inbox",
    "todo",
    "claimed",
    "in_progress",
    "ready_for_review",
    "blocked",
    "done",
    "cancelled",
}
TASK_PRIORITIES = {"low", "normal", "high", "urgent"}
EVIDENCE_TYPES = {"commit", "merge_request", "pipeline", "screenshot", "video", "test", "note"}
AGENT_MODES = {"active", "draining", "disabled"}
SCHEDULE_JOB_TYPES = {"google_sheet_sync", "lease_recovery", "internal_status_generate"}
APPROVAL_ACTION_TYPES = {
    "sheet_writeback",
    "git_push",
    "gitlab_merge_request",
    "gitlab_pipeline_retry",
    "chat_message",
    "deployment",
}
APPROVAL_STATUSES = {
    "pending",
    "approved",
    "executing",
    "succeeded",
    "failed",
    "rejected",
    "cancelled",
    "expired",
}
SENSITIVE_PAYLOAD_KEYS = {
    "authorization",
    "cookie",
    "credential",
    "credentials",
    "password",
    "private_key",
    "secret",
    "token",
}
AGENT_STATUS_TRANSITIONS = {
    "claimed": {"in_progress", "blocked", "todo"},
    "in_progress": {"in_progress", "blocked", "ready_for_review", "todo"},
    "blocked": {"in_progress", "todo"},
}
MAX_CONTEXT_DOCUMENTS_PER_TASK = 5
MAX_CREATE_IDEMPOTENCY_KEY_LENGTH = 240
MAX_DRIVE_EVENT_ATTEMPTS = 8
MAX_DRIVE_CLEANUP_ATTEMPTS = 8
SHEET_SYNC_LEASE_SECONDS = 90
GOOGLE_DOC_TASK_DESCRIPTION = (
    "Task dibuat dari Google Docs context. Gunakan snapshot terlampir sebagai requirement utama."
)


class LeaseExpiredError(PermissionError):
    """Raised when an owner presents a claim whose lease is no longer active."""


ADMIN_STATUS_TRANSITIONS = {
    current_status: TASK_STATUSES - {current_status} for current_status in TASK_STATUSES
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    return (value or utcnow()).isoformat()


@dataclass(frozen=True, slots=True)
class TaskInput:
    title: str
    description: str = ""
    project: str = "HMX"
    module: str = ""
    menu: str = ""
    source: str = "manual"
    external_id: str = ""
    assignee: str = ""
    priority: str = "normal"
    target_branch: str = "Human-Resources"
    acceptance_criteria: tuple[str, ...] = ()
    required_checks: tuple[str, ...] = ()


def derive_google_doc_task_title(snapshot: dict[str, Any], override: str = "") -> str:
    """Build the bounded task title used by Google Docs task creation."""
    normalized_override = re.sub(r"\s+", " ", override.strip())
    if normalized_override:
        return normalized_override[:300].rstrip()
    document_title = re.sub(r"\s+", " ", str(snapshot.get("title", "")).strip())
    selected_id = str(snapshot.get("selected_tab_id", ""))
    selected_title = ""
    for tab in snapshot.get("tabs", []):
        if isinstance(tab, dict) and str(tab.get("tab_id", "")) == selected_id:
            selected_title = re.sub(r"\s+", " ", str(tab.get("title", "")).strip())
            break
    if document_title and selected_title:
        title = f"{document_title} — {selected_title}"
    else:
        title = document_title or selected_title or "Google Docs requirement"
    return title[:300].rstrip()


class PmtStore:
    """Small SQLite-backed PMT store with transactional agent claims."""

    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(f"{self.path.name}.initialize.lock")
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                self._initialize_locked()
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _initialize_locked(self) -> None:
        """Run schema setup while holding the cross-process initialization lock."""
        with self._connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS counters (
                    name TEXT PRIMARY KEY,
                    value INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    task_key TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    project TEXT NOT NULL DEFAULT 'HMX',
                    module TEXT NOT NULL DEFAULT '',
                    menu TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'manual',
                    external_id TEXT NOT NULL DEFAULT '',
                    assignee TEXT NOT NULL DEFAULT '',
                    priority TEXT NOT NULL DEFAULT 'normal',
                    status TEXT NOT NULL DEFAULT 'inbox',
                    target_branch TEXT NOT NULL DEFAULT 'Human-Resources',
                    acceptance_criteria TEXT NOT NULL DEFAULT '[]',
                    required_checks TEXT NOT NULL DEFAULT '[]',
                    claimed_by TEXT,
                    lease_expires_at TEXT,
                    progress_note TEXT NOT NULL DEFAULT '',
                    blocker TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_external_source
                    ON tasks(source, external_id) WHERE external_id != '';
                CREATE INDEX IF NOT EXISTS idx_tasks_status_priority
                    ON tasks(status, priority, created_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_claim
                    ON tasks(claimed_by, lease_expires_at);
                CREATE TABLE IF NOT EXISTS task_runs (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    agent_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_task_events_task
                    ON task_events(task_id, id);
                CREATE TABLE IF NOT EXISTS task_evidence (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    evidence_type TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    url TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_task_evidence_task
                    ON task_evidence(task_id, created_at);
                CREATE TABLE IF NOT EXISTS internal_status_reports (
                    id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    report_date TEXT NOT NULL,
                    period TEXT NOT NULL CHECK(period IN ('morning','evening')),
                    report_version INTEGER NOT NULL CHECK(report_version >= 1),
                    state TEXT NOT NULL DEFAULT 'draft'
                        CHECK(state IN ('draft','approved','sent')),
                    timezone TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    generated_by TEXT NOT NULL DEFAULT '',
                    rendered_text TEXT NOT NULL,
                    sections TEXT NOT NULL DEFAULT '{}',
                    overrides TEXT NOT NULL DEFAULT '{}',
                    approved_by TEXT,
                    approved_at TEXT,
                    sent_by TEXT,
                    sent_at TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(owner,report_date,period,report_version)
                );
                CREATE INDEX IF NOT EXISTS idx_internal_status_report_latest
                    ON internal_status_reports(owner,report_date,period,report_version DESC);
                CREATE TABLE IF NOT EXISTS task_context_documents (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    provider TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    selected_tab_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    revision_id TEXT NOT NULL DEFAULT '',
                    content_sha256 TEXT NOT NULL,
                    context_version INTEGER NOT NULL DEFAULT 1,
                    tab_count INTEGER NOT NULL,
                    char_count INTEGER NOT NULL,
                    fetched_at TEXT NOT NULL,
                    last_checked_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(task_id,provider,external_id)
                );
                CREATE INDEX IF NOT EXISTS idx_task_context_documents_task
                    ON task_context_documents(task_id,created_at);
                CREATE TABLE IF NOT EXISTS task_context_tabs (
                    context_document_id TEXT NOT NULL REFERENCES task_context_documents(id)
                        ON DELETE CASCADE,
                    tab_id TEXT NOT NULL,
                    parent_tab_id TEXT,
                    depth INTEGER NOT NULL,
                    position INTEGER NOT NULL,
                    order_index INTEGER NOT NULL,
                    position_path TEXT NOT NULL,
                    path TEXT NOT NULL,
                    title TEXT NOT NULL,
                    text TEXT NOT NULL,
                    char_count INTEGER NOT NULL,
                    snapshot TEXT NOT NULL,
                    PRIMARY KEY(context_document_id,tab_id)
                );
                CREATE INDEX IF NOT EXISTS idx_task_context_tabs_order
                    ON task_context_tabs(context_document_id,order_index);
                CREATE TABLE IF NOT EXISTS google_doc_task_creations (
                    idempotency_key TEXT PRIMARY KEY,
                    request_sha256 TEXT NOT NULL,
                    task_id TEXT NOT NULL REFERENCES tasks(id),
                    context_id TEXT NOT NULL REFERENCES task_context_documents(id),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agents (
                    agent_id TEXT PRIMARY KEY,
                    server_name TEXT NOT NULL,
                    capabilities TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'online',
                    current_task_id TEXT,
                    last_heartbeat_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_events_agent
                    ON agent_events(agent_id, id);
                CREATE TABLE IF NOT EXISTS schedules (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    job_type TEXT NOT NULL,
                    interval_seconds INTEGER NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    next_run_at TEXT NOT NULL,
                    locked_by TEXT,
                    lock_expires_at TEXT,
                    current_run_id TEXT,
                    last_run_at TEXT,
                    last_status TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS schedule_runs (
                    id TEXT PRIMARY KEY,
                    schedule_id TEXT NOT NULL REFERENCES schedules(id),
                    worker_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    result TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_schedule_runs_schedule_started
                    ON schedule_runs(schedule_id, started_at);
                CREATE INDEX IF NOT EXISTS idx_schedules_due
                    ON schedules(enabled, next_run_at);
                CREATE TABLE IF NOT EXISTS drive_watch_channels (
                    channel_id TEXT PRIMARY KEY,
                    file_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    resource_id TEXT,
                    resource_uri TEXT,
                    expiration_at TEXT NOT NULL,
                    state TEXT NOT NULL,
                    replaced_by TEXT,
                    last_message_number INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_drive_channels_file_state
                    ON drive_watch_channels(file_id,state,expiration_at);
                CREATE TABLE IF NOT EXISTS drive_watch_leases (
                    file_id TEXT PRIMARY KEY,
                    locked_by TEXT NOT NULL,
                    lock_expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS drive_watch_maintenance (
                    file_id TEXT PRIMARY KEY,
                    desired_active INTEGER NOT NULL DEFAULT 0,
                    renewal_attempts INTEGER NOT NULL DEFAULT 0,
                    renewal_next_attempt_at TEXT,
                    last_status TEXT,
                    last_error_type TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS drive_notification_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id TEXT NOT NULL REFERENCES drive_watch_channels(channel_id),
                    message_number INTEGER NOT NULL,
                    resource_state TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    run_id TEXT,
                    locked_by TEXT,
                    lock_expires_at TEXT,
                    finished_at TEXT,
                    result TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(channel_id,message_number)
                );
                CREATE INDEX IF NOT EXISTS idx_drive_events_due
                    ON drive_notification_events(status,next_attempt_at,available_at);
                CREATE TABLE IF NOT EXISTS sheet_sync_leases (
                    source_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    locked_by TEXT NOT NULL,
                    lock_expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approval_requests (
                    id TEXT PRIMARY KEY,
                    approval_key TEXT NOT NULL UNIQUE,
                    task_id TEXT REFERENCES tasks(id),
                    action_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL DEFAULT '{}',
                    payload_sha256 TEXT NOT NULL,
                    requested_by TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    version INTEGER NOT NULL DEFAULT 1,
                    decided_by TEXT,
                    decision_note TEXT NOT NULL DEFAULT '',
                    decided_at TEXT,
                    expires_at TEXT,
                    claimed_by TEXT,
                    lease_expires_at TEXT,
                    current_run_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_request_idempotency
                    ON approval_requests(requested_by,idempotency_key)
                    WHERE idempotency_key != '';
                CREATE INDEX IF NOT EXISTS idx_approval_status_created
                    ON approval_requests(status,created_at);
                CREATE INDEX IF NOT EXISTS idx_approval_task
                    ON approval_requests(task_id,created_at);
                CREATE TABLE IF NOT EXISTS approval_runs (
                    id TEXT PRIMARY KEY,
                    approval_id TEXT NOT NULL REFERENCES approval_requests(id),
                    executor_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    provider_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    finished_at TEXT,
                    result TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_approval_runs_request
                    ON approval_runs(approval_id,started_at);
                CREATE TABLE IF NOT EXISTS approval_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    approval_id TEXT NOT NULL REFERENCES approval_requests(id),
                    event_type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    payload TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_approval_events_request
                    ON approval_events(approval_id,id);
                """
            )
            db.execute("BEGIN IMMEDIATE")
            try:
                self._ensure_column(db, "tasks", "source_branch", "TEXT NOT NULL DEFAULT ''")
                self._ensure_column(db, "tasks", "commit_ref", "TEXT NOT NULL DEFAULT ''")
                self._ensure_column(db, "tasks", "mr_url", "TEXT NOT NULL DEFAULT ''")
                self._ensure_column(db, "tasks", "pipeline_url", "TEXT NOT NULL DEFAULT ''")
                self._ensure_column(db, "tasks", "current_run_id", "TEXT")
                self._ensure_column(db, "agents", "mode", "TEXT NOT NULL DEFAULT 'active'")
                self._ensure_column(db, "schedules", "current_run_id", "TEXT")
                self._ensure_column(db, "approval_runs", "provider_key", "TEXT NOT NULL DEFAULT ''")
                self._ensure_column(
                    db,
                    "internal_status_reports",
                    "generated_by",
                    "TEXT NOT NULL DEFAULT ''",
                )
                self._ensure_column(
                    db, "drive_watch_maintenance", "desired_active", "INTEGER NOT NULL DEFAULT 0"
                )
                self._ensure_column(
                    db, "drive_watch_channels", "cleanup_status", "TEXT NOT NULL DEFAULT 'none'"
                )
                self._ensure_column(
                    db, "drive_watch_channels", "cleanup_attempts", "INTEGER NOT NULL DEFAULT 0"
                )
                self._ensure_column(db, "drive_watch_channels", "cleanup_next_attempt_at", "TEXT")
                self._ensure_column(db, "drive_watch_channels", "cleanup_last_error_type", "TEXT")
                self._migrate_active_task_runs(db)
            except Exception:
                db.rollback()
                raise
            else:
                db.commit()

    @staticmethod
    def _ensure_column(db: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _migrate_active_task_runs(self, db: sqlite3.Connection) -> None:
        active_claims = db.execute(
            """SELECT id,task_key,claimed_by FROM tasks
                WHERE claimed_by IS NOT NULL AND current_run_id IS NULL"""
        ).fetchall()
        now = iso()
        for task in active_claims:
            runs = db.execute(
                """SELECT id FROM task_runs WHERE task_id=? AND agent_id=?
                    AND finished_at IS NULL ORDER BY started_at DESC""",
                (task["id"], task["claimed_by"]),
            ).fetchall()
            if len(runs) == 1:
                db.execute(
                    "UPDATE tasks SET current_run_id=? WHERE id=?",
                    (runs[0]["id"], task["id"]),
                )
                continue
            db.execute(
                """UPDATE task_runs SET status='lease_expired',finished_at=?
                    WHERE task_id=? AND finished_at IS NULL""",
                (now, task["id"]),
            )
            db.execute(
                """UPDATE agents SET current_task_id=NULL,status='online',updated_at=?
                    WHERE agent_id=? AND current_task_id=?""",
                (now, task["claimed_by"], task["id"]),
            )
            db.execute(
                """UPDATE tasks SET status='todo',claimed_by=NULL,lease_expires_at=NULL,
                    current_run_id=NULL,version=version+1,updated_at=? WHERE id=?""",
                (now, task["id"]),
            )
            self._event(
                db,
                task["id"],
                "task.legacy_claim_reconciled",
                "schema-migration",
                {"prior_agent_id": task["claimed_by"], "unfinished_runs": len(runs)},
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=15000")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except Exception:
                db.rollback()
                raise
            else:
                db.commit()

    @staticmethod
    def _task(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["acceptance_criteria"] = PmtStore._criteria(
            json.loads(result["acceptance_criteria"])
        )
        result["required_checks"] = json.loads(result["required_checks"])
        return result

    @staticmethod
    def _criteria(items: list[Any]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            if isinstance(item, str):
                text = item.strip()
                if text:
                    normalized.append({"id": f"criterion-{index + 1}", "text": text, "done": False})
                continue
            if isinstance(item, dict) and str(item.get("text", "")).strip():
                normalized.append(
                    {
                        "id": str(item.get("id") or f"criterion-{index + 1}"),
                        "text": str(item["text"]).strip(),
                        "done": bool(item.get("done", False)),
                    }
                )
        return normalized

    @staticmethod
    def _validated_url(value: str) -> str:
        url = value.strip()
        if not url:
            return ""
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("evidence URL must use http or https")
        return url

    @staticmethod
    def _require_active_owner(
        row: sqlite3.Row,
        expected_owner: str | None,
        expected_run_id: str | None = None,
    ) -> None:
        if expected_owner is None:
            return
        lease = row["lease_expires_at"]
        if row["claimed_by"] != expected_owner:
            raise PermissionError("task detail writes require the active task owner")
        if not expected_run_id or row["current_run_id"] != expected_run_id:
            raise PermissionError("task run fencing token is stale")
        if not lease or datetime.fromisoformat(lease) <= utcnow():
            raise LeaseExpiredError("task lease expired; reclaim before continuing")

    @staticmethod
    def _schedule(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        result["enabled"] = bool(result["enabled"])
        return result

    @staticmethod
    def _approval(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    @staticmethod
    def _safe_json_object(value: dict[str, Any], field_name: str = "payload") -> tuple[str, str]:
        def inspect(item: Any, depth: int = 0) -> None:
            if depth > 8:
                raise ValueError(f"{field_name} nesting is too deep")
            if isinstance(item, dict):
                if len(item) > 100:
                    raise ValueError(f"{field_name} contains too many fields")
                for key, nested in item.items():
                    normalized = str(key).strip().lower().replace("-", "_")
                    if any(marker in normalized for marker in SENSITIVE_PAYLOAD_KEYS):
                        raise ValueError(f"{field_name} must not contain secrets or credentials")
                    inspect(nested, depth + 1)
            elif isinstance(item, list):
                if len(item) > 200:
                    raise ValueError(f"{field_name} contains too many items")
                for nested in item:
                    inspect(nested, depth + 1)
            elif item is not None and not isinstance(item, (str, int, float, bool)):
                raise ValueError(f"{field_name} must be JSON-compatible")

        if not isinstance(value, dict):
            raise ValueError(f"{field_name} must be an object")
        inspect(value)
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} must be valid JSON") from exc
        if len(encoded.encode("utf-8")) > 20_000:
            raise ValueError(f"{field_name} exceeds 20 KB")
        return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_approval_payload(action_type: str, payload: dict[str, Any]) -> None:
        schemas: dict[str, tuple[set[str], set[str]]] = {
            "sheet_writeback": (
                {"connector_id", "task_key", "stable_row_key", "updates", "preconditions"},
                {"connector_id", "task_key", "stable_row_key", "updates", "preconditions"},
            ),
            "git_push": (
                {"repository", "remote", "source_branch", "target_branch", "commit_sha"},
                {"repository", "remote", "source_branch", "target_branch", "commit_sha"},
            ),
            "gitlab_merge_request": (
                {"project_path", "source_branch", "target_branch", "title", "description"},
                {"project_path", "source_branch", "target_branch", "title", "description"},
            ),
            "gitlab_pipeline_retry": (
                {"project_path", "pipeline_id"},
                {"project_path", "pipeline_id", "job_id"},
            ),
            "chat_message": (
                {"connector_id", "target", "message"},
                {"connector_id", "target", "message", "thread_id"},
            ),
            "deployment": (
                {"environment", "artifact_ref", "version"},
                {"environment", "artifact_ref", "version", "notes"},
            ),
        }
        required, allowed = schemas[action_type]
        keys = set(payload)
        missing = sorted(required - keys)
        unexpected = sorted(keys - allowed)
        if missing:
            raise ValueError(f"approval payload missing fields: {', '.join(missing)}")
        if unexpected:
            raise ValueError(
                f"approval payload contains unsupported fields: {', '.join(unexpected)}"
            )
        if action_type == "sheet_writeback":
            stable_key = payload["stable_row_key"]
            if not isinstance(stable_key, dict) or set(stable_key) != {"column", "value"}:
                raise ValueError("sheet write-back requires a stable row key column and value")
            if not all(str(stable_key[item]).strip() for item in ("column", "value")):
                raise ValueError("sheet stable row key cannot be blank")
            updates = payload["updates"]
            preconditions = payload["preconditions"]
            writable_columns = {
                "Dev Status",
                "Internal Status",
                "Internal Status Date",
                "Internal Status Note",
            }
            if not isinstance(updates, dict) or not updates:
                raise ValueError("sheet write-back requires at least one update")
            if not isinstance(preconditions, dict) or not preconditions:
                raise ValueError("sheet write-back requires preconditions")
            if not set(updates) <= writable_columns or not set(preconditions) <= writable_columns:
                raise ValueError("sheet write-back contains a non-allowlisted column")
            if "row" in payload or "row_number" in payload:
                raise ValueError("row-number-only Sheet write-back is forbidden")
        for key, value in payload.items():
            if key in {"pipeline_id", "job_id"}:
                if not isinstance(value, int) or value < 1:
                    raise ValueError(f"{key} must be a positive integer")
            elif key not in {"stable_row_key", "updates", "preconditions"}:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"approval payload field {key} must be a non-empty string")
                if len(value) > 20_000:
                    raise ValueError(f"approval payload field {key} exceeds allowed length")

    def _event(
        self,
        db: sqlite3.Connection,
        task_id: str,
        event_type: str,
        actor: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        db.execute(
            "INSERT INTO task_events(task_id,event_type,actor,payload,created_at) VALUES(?,?,?,?,?)",
            (task_id, event_type, actor, json.dumps(payload or {}), iso()),
        )

    def _agent_event(
        self,
        db: sqlite3.Connection,
        agent_id: str,
        event_type: str,
        actor: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        db.execute(
            "INSERT INTO agent_events(agent_id,event_type,actor,payload,created_at) VALUES(?,?,?,?,?)",
            (agent_id, event_type, actor, json.dumps(payload or {}), iso()),
        )

    def _approval_event(
        self,
        db: sqlite3.Connection,
        approval_id: str,
        event_type: str,
        actor: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        db.execute(
            "INSERT INTO approval_events(approval_id,event_type,actor,payload,created_at) VALUES(?,?,?,?,?)",
            (approval_id, event_type, actor, json.dumps(payload or {}), iso()),
        )

    def _next_key(self, db: sqlite3.Connection, prefix: str = "PMT") -> str:
        counter_name = "task" if prefix == "PMT" else prefix.lower()
        db.execute(
            "INSERT INTO counters(name,value) VALUES(?,0) ON CONFLICT(name) DO NOTHING",
            (counter_name,),
        )
        row = db.execute(
            "UPDATE counters SET value=value+1 WHERE name=? RETURNING value",
            (counter_name,),
        ).fetchone()
        return f"{prefix}-{int(row['value']):04d}"

    @staticmethod
    def _validate_task_input(data: TaskInput) -> str:
        title = data.title.strip()
        if not title:
            raise ValueError("title is required")
        if data.priority not in TASK_PRIORITIES:
            raise ValueError(f"invalid priority: {data.priority}")
        return title

    def _insert_task_in_transaction(
        self, db: sqlite3.Connection, data: TaskInput, actor: str
    ) -> tuple[dict[str, Any], bool]:
        title = self._validate_task_input(data)
        if data.external_id:
            existing = db.execute(
                "SELECT * FROM tasks WHERE source=? AND external_id=?",
                (data.source, data.external_id),
            ).fetchone()
            if existing is not None:
                return self._task(existing), False
        task_id = f"task_{uuid.uuid4().hex}"
        now = iso()
        task_key = self._next_key(db)
        try:
            db.execute(
                """
                INSERT INTO tasks(
                    id,task_key,title,description,project,module,menu,source,external_id,
                    assignee,priority,status,target_branch,acceptance_criteria,required_checks,
                    created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    task_id,
                    task_key,
                    title,
                    data.description.strip(),
                    data.project.strip() or "HMX",
                    data.module.strip(),
                    data.menu.strip(),
                    data.source.strip() or "manual",
                    data.external_id.strip(),
                    data.assignee.strip(),
                    data.priority,
                    "todo",
                    data.target_branch.strip() or "Human-Resources",
                    json.dumps(
                        [
                            {
                                "id": f"criterion_{uuid.uuid4().hex}",
                                "text": text.strip(),
                                "done": False,
                            }
                            for text in data.acceptance_criteria
                            if text.strip()
                        ]
                    ),
                    json.dumps(list(data.required_checks)),
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            if data.external_id:
                existing = db.execute(
                    "SELECT * FROM tasks WHERE source=? AND external_id=?",
                    (data.source, data.external_id),
                ).fetchone()
                if existing is not None:
                    return self._task(existing), False
            raise ValueError("task key or external source already exists") from exc
        self._event(db, task_id, "task.created", actor, {"task_key": task_key})
        row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._task(row), True

    def create_task(self, data: TaskInput, actor: str = "human") -> dict[str, Any]:
        with self._transaction() as db:
            task, _created = self._insert_task_in_transaction(db, data, actor)
            return task

    def list_tasks(
        self,
        *,
        status: str | None = None,
        assignee: str | None = None,
        claimed_by: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if status:
            if status not in TASK_STATUSES:
                raise ValueError(f"invalid status: {status}")
            clauses.append("status=?")
            values.append(status)
        if assignee:
            clauses.append("assignee=?")
            values.append(assignee)
        if claimed_by:
            clauses.append("claimed_by=?")
            values.append(claimed_by)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(max(1, min(limit, 200)))
        with self._connect() as db:
            rows = db.execute(
                f"""SELECT * FROM tasks {where}
                ORDER BY created_at DESC, id DESC LIMIT ?""",
                values,
            ).fetchall()
        return [self._task(row) for row in rows]

    def get_task(self, task_ref: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM tasks WHERE id=? OR task_key=?", (task_ref, task_ref)
            ).fetchone()
        return self._task(row) if row else None

    def get_external_task(self, source: str, external_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM tasks WHERE source=? AND external_id=?", (source, external_id)
            ).fetchone()
        return self._task(row) if row else None

    def task_events(self, task_ref: str, limit: int = 100) -> list[dict[str, Any]]:
        task = self.get_task(task_ref)
        if task is None:
            raise KeyError(task_ref)
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT ?",
                (task["id"], max(1, min(limit, 500))),
            ).fetchall()
        result = []
        for row in rows:
            event = dict(row)
            event["payload"] = json.loads(event["payload"])
            result.append(event)
        return result

    @staticmethod
    def _context_document(
        row: sqlite3.Row, tabs: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        result = dict(row)
        if tabs is not None:
            result["tabs"] = tabs
        return result

    @staticmethod
    def _context_tab(row: sqlite3.Row) -> dict[str, Any]:
        snapshot = json.loads(row["snapshot"])
        # The deterministic parser snapshot is authoritative; relational
        # columns support hierarchy queries and migration inspection.
        return snapshot

    def _task_row(self, db: sqlite3.Connection, task_ref: str) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM tasks WHERE id=? OR task_key=?", (task_ref, task_ref)
        ).fetchone()
        if row is None:
            raise KeyError(task_ref)
        return row

    @staticmethod
    def _require_task_version(row: sqlite3.Row, expected_version: int) -> None:
        if row["version"] != expected_version:
            raise PermissionError("task changed since it was loaded; refresh and retry")

    def check_context_write_access(
        self,
        task_ref: str,
        *,
        expected_version: int,
        expected_owner: str | None = None,
        expected_run_id: str | None = None,
        context_ref: str | None = None,
        external_id: str | None = None,
        source_url: str | None = None,
    ) -> dict[str, Any]:
        """Preflight a fetch without holding SQLite open across the network."""
        with self._connect() as db:
            task = self._task_row(db, task_ref)
            self._require_active_owner(task, expected_owner, expected_run_id)
            self._require_task_version(task, expected_version)
            if context_ref is None:
                existing = None
                if external_id is not None:
                    existing = db.execute(
                        """SELECT * FROM task_context_documents
                           WHERE task_id=? AND provider='google_docs' AND external_id=?""",
                        (task["id"], external_id),
                    ).fetchone()
                    if existing is not None and existing["source_url"] != source_url:
                        raise PermissionError(
                            "Google Docs document is already attached with a different tab selection"
                        )
                count = db.execute(
                    "SELECT COUNT(*) AS count FROM task_context_documents WHERE task_id=?",
                    (task["id"],),
                ).fetchone()["count"]
                if existing is None and count >= MAX_CONTEXT_DOCUMENTS_PER_TASK:
                    raise ValueError("task exceeds the 5 external context document limit")
                return {
                    "task": self._task(task),
                    "context": self._context_document(existing) if existing is not None else None,
                }
            context = db.execute(
                """SELECT * FROM task_context_documents
                   WHERE task_id=? AND (id=? OR external_id=?)""",
                (task["id"], context_ref, context_ref),
            ).fetchone()
            if context is None:
                raise KeyError(context_ref)
            return {"task": self._task(task), "context": self._context_document(context)}

    def list_task_context_documents(self, task_ref: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            task = self._task_row(db, task_ref)
            rows = db.execute(
                "SELECT * FROM task_context_documents WHERE task_id=? ORDER BY created_at,id",
                (task["id"],),
            ).fetchall()
        return [self._context_document(row) for row in rows]

    def get_task_context_document(self, task_ref: str, context_ref: str) -> dict[str, Any]:
        with self._connect() as db:
            task = self._task_row(db, task_ref)
            row = db.execute(
                """SELECT * FROM task_context_documents
                   WHERE task_id=? AND (id=? OR external_id=?)""",
                (task["id"], context_ref, context_ref),
            ).fetchone()
            if row is None:
                raise KeyError(context_ref)
            tab_rows = db.execute(
                """SELECT * FROM task_context_tabs WHERE context_document_id=?
                   ORDER BY order_index""",
                (row["id"],),
            ).fetchall()
        return self._context_document(row, [self._context_tab(tab) for tab in tab_rows])

    @staticmethod
    def _validated_context_snapshot(snapshot: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
        required = {
            "document_id",
            "title",
            "revision_id",
            "tabs",
            "content_sha256",
            "char_count",
            "selected_tab_id",
        }
        if not isinstance(snapshot, dict) or not required <= set(snapshot):
            raise ValueError("Google Docs snapshot is incomplete")
        tabs = snapshot["tabs"]
        digest = snapshot["content_sha256"]
        if (
            not isinstance(tabs, list)
            or not 1 <= len(tabs) <= 100
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            raise ValueError("Google Docs snapshot is invalid")
        try:
            int(digest, 16)
        except ValueError as exc:
            raise ValueError("Google Docs snapshot hash is invalid") from exc
        return tabs, digest

    @staticmethod
    def _creation_request_hash(data: TaskInput, source_url: str, snapshot: dict[str, Any]) -> str:
        request = {
            "source_url": source_url,
            "document_id": snapshot["document_id"],
            "selected_tab_id": snapshot["selected_tab_id"],
            "content_sha256": snapshot["content_sha256"],
            "title": data.title.strip(),
            "description": data.description.strip(),
            "project": data.project.strip() or "HMX",
            "module": data.module.strip(),
            "menu": data.menu.strip(),
            "source": data.source.strip() or "manual",
            "external_id": data.external_id.strip(),
            "assignee": data.assignee.strip(),
            "priority": data.priority,
            "target_branch": data.target_branch.strip() or "Human-Resources",
            "acceptance_criteria": list(data.acceptance_criteria),
            "required_checks": list(data.required_checks),
        }
        encoded = json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _insert_context_rows(
        self,
        db: sqlite3.Connection,
        task_id: str,
        context_id: str,
        source_url: str,
        snapshot: dict[str, Any],
        tabs: list[dict[str, Any]],
        digest: str,
        context_version: int,
        created_at: str,
        now: str,
    ) -> dict[str, Any]:
        db.execute(
            """INSERT INTO task_context_documents(
                id,task_id,provider,source_url,external_id,selected_tab_id,title,revision_id,
                content_sha256,context_version,tab_count,char_count,fetched_at,last_checked_at,
                created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                context_id,
                task_id,
                "google_docs",
                source_url,
                snapshot["document_id"],
                snapshot["selected_tab_id"],
                snapshot["title"],
                snapshot["revision_id"],
                digest,
                context_version,
                len(tabs),
                snapshot["char_count"],
                now,
                now,
                created_at,
                now,
            ),
        )
        for order_index, tab in enumerate(tabs):
            encoded = json.dumps(
                tab, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
            db.execute(
                """INSERT INTO task_context_tabs(
                    context_document_id,tab_id,parent_tab_id,depth,position,order_index,
                    position_path,path,title,text,char_count,snapshot
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    context_id,
                    tab["tab_id"],
                    tab["parent_tab_id"],
                    tab["depth"],
                    tab["position"],
                    order_index,
                    json.dumps(tab["position_path"]),
                    tab["path"],
                    tab["title"],
                    tab["text"],
                    tab["char_count"],
                    encoded,
                ),
            )
        row = db.execute(
            "SELECT * FROM task_context_documents WHERE id=?", (context_id,)
        ).fetchone()
        return self._context_document(row)

    def _insert_initial_context(
        self,
        db: sqlite3.Connection,
        task_id: str,
        source_url: str,
        snapshot: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        tabs, digest = self._validated_context_snapshot(snapshot)
        now = iso()
        context_id = f"context_{uuid.uuid4().hex}"
        context = self._insert_context_rows(
            db,
            task_id,
            context_id,
            source_url,
            snapshot,
            tabs,
            digest,
            1,
            now,
            now,
        )
        self._event(
            db,
            task_id,
            "task.context_attached",
            actor,
            {
                "context_id": context_id,
                "provider": "google_docs",
                "external_id": snapshot["document_id"],
                "context_version": 1,
                "content_sha256": digest,
                "tab_count": len(tabs),
                "char_count": snapshot["char_count"],
            },
        )
        return context

    def create_task_from_google_doc(
        self,
        data: TaskInput,
        *,
        source_url: str,
        snapshot: dict[str, Any],
        actor: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Atomically create a task and its already-fetched Google Docs snapshot."""
        key = idempotency_key.strip()
        if not key:
            raise ValueError("idempotency_key is required")
        if len(key) > MAX_CREATE_IDEMPOTENCY_KEY_LENGTH:
            raise ValueError("idempotency_key exceeds allowed length")
        data = replace(
            data,
            description=data.description.strip() or GOOGLE_DOC_TASK_DESCRIPTION,
            source="google_docs",
        )
        self._validated_context_snapshot(snapshot)
        request_hash = self._creation_request_hash(data, source_url, snapshot)
        with self._transaction() as db:
            existing = db.execute(
                "SELECT * FROM google_doc_task_creations WHERE idempotency_key=?", (key,)
            ).fetchone()
            if existing is not None:
                if existing["request_sha256"] != request_hash:
                    raise ValueError("idempotency key was reused with different content")
                task_row = db.execute(
                    "SELECT * FROM tasks WHERE id=?", (existing["task_id"],)
                ).fetchone()
                context_row = db.execute(
                    "SELECT * FROM task_context_documents WHERE id=?", (existing["context_id"],)
                ).fetchone()
                if task_row is None or context_row is None:
                    raise RuntimeError("Google Docs task creation record is incomplete")
                return {
                    "task": self._task(task_row),
                    "context": self._context_document(context_row),
                    "created": False,
                }
            task, _created = self._insert_task_in_transaction(db, data, actor)
            context = self._insert_initial_context(db, task["id"], source_url, snapshot, actor)
            db.execute(
                """INSERT INTO google_doc_task_creations(
                    idempotency_key,request_sha256,task_id,context_id,created_at
                ) VALUES(?,?,?,?,?)""",
                (key, request_hash, task["id"], context["id"], iso()),
            )
            return {"task": task, "context": context, "created": True}

    def get_google_doc_task_creation(
        self, idempotency_key: str, source_url: str
    ) -> dict[str, Any] | None:
        key = idempotency_key.strip()
        if not key:
            return None
        with self._connect() as db:
            record = db.execute(
                "SELECT task_id,context_id FROM google_doc_task_creations WHERE idempotency_key=?",
                (key,),
            ).fetchone()
            if record is None:
                return None
            task_row = db.execute("SELECT * FROM tasks WHERE id=?", (record["task_id"],)).fetchone()
            context_row = db.execute(
                "SELECT * FROM task_context_documents WHERE id=?", (record["context_id"],)
            ).fetchone()
        if task_row is None or context_row is None:
            raise RuntimeError("Google Docs task creation record is incomplete")
        if context_row["source_url"] != source_url.strip():
            raise ValueError("idempotency key was reused with a different source URL")
        task = self._task(task_row)
        context = self._context_document(context_row)
        return {"task": task, "context": context, "created": False}

    def save_task_context_snapshot(
        self,
        task_ref: str,
        *,
        source_url: str,
        snapshot: dict[str, Any],
        actor: str,
        operation: str,
        expected_version: int,
        expected_context_version: int | None = None,
        context_ref: str | None = None,
        expected_owner: str | None = None,
        expected_run_id: str | None = None,
    ) -> dict[str, Any]:
        """Atomically attach/refresh a normalized snapshot and its tabs."""
        if operation not in {"attach", "refresh"}:
            raise ValueError("invalid context operation")
        tabs, digest = self._validated_context_snapshot(snapshot)
        now = iso()
        with self._transaction() as db:
            task = self._task_row(db, task_ref)
            self._require_active_owner(task, expected_owner, expected_run_id)
            self._require_task_version(task, expected_version)
            if operation == "refresh":
                current = db.execute(
                    """SELECT * FROM task_context_documents
                       WHERE task_id=? AND (id=? OR external_id=?)""",
                    (task["id"], context_ref, context_ref),
                ).fetchone()
                if current is None:
                    raise KeyError(context_ref or "")
            else:
                current = db.execute(
                    """SELECT * FROM task_context_documents
                       WHERE task_id=? AND provider='google_docs' AND external_id=?""",
                    (task["id"], snapshot["document_id"]),
                ).fetchone()
                if current is None:
                    count = db.execute(
                        "SELECT COUNT(*) AS count FROM task_context_documents WHERE task_id=?",
                        (task["id"],),
                    ).fetchone()["count"]
                    if count >= MAX_CONTEXT_DOCUMENTS_PER_TASK:
                        raise ValueError("task exceeds the 5 external context document limit")

            if current is not None:
                compatible_identity = (
                    current["provider"] == "google_docs"
                    and current["external_id"] == snapshot["document_id"]
                    and current["source_url"] == source_url
                    and current["selected_tab_id"] == snapshot["selected_tab_id"]
                )
                unchanged = compatible_identity and current["content_sha256"] == digest
                if unchanged:
                    db.execute(
                        """UPDATE task_context_documents
                           SET revision_id=?,fetched_at=?,last_checked_at=?,updated_at=?
                           WHERE id=?""",
                        (snapshot["revision_id"], now, now, now, current["id"]),
                    )
                    row = db.execute(
                        "SELECT * FROM task_context_documents WHERE id=?", (current["id"],)
                    ).fetchone()
                    result = self._context_document(row)
                    result["changed"] = False
                    return result
                if operation == "attach" and expected_context_version is None:
                    raise PermissionError(
                        "Google Docs context is already attached; refresh the existing snapshot"
                    )
                if (
                    expected_context_version is None
                    or current["context_version"] != expected_context_version
                ):
                    raise PermissionError("context changed since it was loaded; refresh and retry")
                context_id = current["id"]
                context_version = current["context_version"] + 1
                created_at = current["created_at"]
                db.execute(
                    "DELETE FROM task_context_tabs WHERE context_document_id=?", (context_id,)
                )
                db.execute("DELETE FROM task_context_documents WHERE id=?", (context_id,))
            else:
                if operation == "refresh":
                    raise KeyError(context_ref or "")
                context_id = f"context_{uuid.uuid4().hex}"
                context_version = 1
                created_at = now

            self._insert_context_rows(
                db,
                task["id"],
                context_id,
                source_url,
                snapshot,
                tabs,
                digest,
                context_version,
                created_at,
                now,
            )
            event_type = (
                "task.context_attached" if context_version == 1 else "task.context_refreshed"
            )
            self._event(
                db,
                task["id"],
                event_type,
                actor,
                {
                    "context_id": context_id,
                    "provider": "google_docs",
                    "external_id": snapshot["document_id"],
                    "context_version": context_version,
                    "content_sha256": digest,
                    "tab_count": len(tabs),
                    "char_count": snapshot["char_count"],
                },
            )
            row = db.execute(
                "SELECT * FROM task_context_documents WHERE id=?", (context_id,)
            ).fetchone()
        result = self._context_document(row)
        result["changed"] = True
        return result

    def remove_task_context_document(
        self,
        task_ref: str,
        context_ref: str,
        *,
        actor: str,
        expected_version: int,
        expected_context_version: int,
        expected_owner: str | None = None,
        expected_run_id: str | None = None,
    ) -> dict[str, Any]:
        with self._transaction() as db:
            task = self._task_row(db, task_ref)
            self._require_active_owner(task, expected_owner, expected_run_id)
            self._require_task_version(task, expected_version)
            row = db.execute(
                """SELECT * FROM task_context_documents
                   WHERE task_id=? AND (id=? OR external_id=?)""",
                (task["id"], context_ref, context_ref),
            ).fetchone()
            if row is None:
                raise KeyError(context_ref)
            if row["context_version"] != expected_context_version:
                raise PermissionError("context changed since it was loaded; refresh and retry")
            metadata = self._context_document(row)
            db.execute("DELETE FROM google_doc_task_creations WHERE context_id=?", (row["id"],))
            db.execute("DELETE FROM task_context_tabs WHERE context_document_id=?", (row["id"],))
            db.execute("DELETE FROM task_context_documents WHERE id=?", (row["id"],))
            self._event(
                db,
                task["id"],
                "task.context_removed",
                actor,
                {
                    "context_id": row["id"],
                    "provider": row["provider"],
                    "external_id": row["external_id"],
                    "context_version": row["context_version"],
                    "content_sha256": row["content_sha256"],
                },
            )
        return metadata

    def remove_task(
        self,
        task_ref: str,
        *,
        actor: str,
        expected_version: int,
    ) -> dict[str, Any]:
        """Remove an inactive task and its task-owned records atomically."""
        with self._transaction() as db:
            task = self._task_row(db, task_ref)
            self._require_task_version(task, expected_version)
            if (
                task["claimed_by"]
                or task["current_run_id"]
                or task["status"]
                in {
                    "claimed",
                    "in_progress",
                }
            ):
                raise PermissionError(
                    "task sedang dikerjakan agent; pindahkan task ke status nonaktif sebelum dihapus"
                )
            unfinished_run = db.execute(
                "SELECT 1 FROM task_runs WHERE task_id=? AND finished_at IS NULL LIMIT 1",
                (task["id"],),
            ).fetchone()
            if unfinished_run is not None:
                raise PermissionError(
                    "task masih memiliki run aktif; selesaikan atau release run sebelum dihapus"
                )
            active_approval = db.execute(
                """SELECT 1 FROM approval_requests
                   WHERE task_id=? AND status IN ('pending','approved','executing') LIMIT 1""",
                (task["id"],),
            ).fetchone()
            if active_approval is not None:
                raise PermissionError(
                    "task masih memiliki approval aktif; selesaikan atau batalkan approval sebelum dihapus"
                )

            metadata = self._task(task)
            context_ids = db.execute(
                "SELECT id FROM task_context_documents WHERE task_id=?", (task["id"],)
            ).fetchall()
            for context in context_ids:
                db.execute(
                    "DELETE FROM task_context_tabs WHERE context_document_id=?",
                    (context["id"],),
                )
            db.execute("DELETE FROM google_doc_task_creations WHERE task_id=?", (task["id"],))
            db.execute("DELETE FROM task_context_documents WHERE task_id=?", (task["id"],))
            db.execute("DELETE FROM task_evidence WHERE task_id=?", (task["id"],))
            db.execute("DELETE FROM task_events WHERE task_id=?", (task["id"],))
            db.execute("DELETE FROM task_runs WHERE task_id=?", (task["id"],))
            db.execute("UPDATE approval_requests SET task_id=NULL WHERE task_id=?", (task["id"],))
            db.execute(
                """UPDATE agents SET current_task_id=NULL,status='online',updated_at=?
                   WHERE current_task_id=?""",
                (iso(), task["id"]),
            )
            db.execute("DELETE FROM tasks WHERE id=?", (task["id"],))
            metadata["removed_by"] = actor
            return metadata

    def update_task(
        self,
        task_ref: str,
        *,
        actor: str,
        title: str,
        description: str = "",
        project: str = "HMX",
        module: str = "",
        menu: str = "",
        assignee: str = "",
        priority: str = "normal",
        required_checks: list[str] | tuple[str, ...] | None = None,
        target_branch: str = "Human-Resources",
        source_branch: str = "",
        commit_ref: str = "",
        mr_url: str = "",
        pipeline_url: str = "",
        expected_owner: str | None = None,
        expected_run_id: str | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        if not title.strip():
            raise ValueError("title is required")
        if priority not in TASK_PRIORITIES:
            raise ValueError(f"invalid priority: {priority}")
        mr_url = self._validated_url(mr_url)
        pipeline_url = self._validated_url(pipeline_url)
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM tasks WHERE id=? OR task_key=?", (task_ref, task_ref)
            ).fetchone()
            if row is None:
                raise KeyError(task_ref)
            self._require_active_owner(row, expected_owner, expected_run_id)
            if expected_version is not None and row["version"] != expected_version:
                raise PermissionError("task changed since it was loaded; refresh and retry")
            values = {
                "title": title.strip(),
                "description": description.strip(),
                "project": project.strip() or "HMX",
                "module": module.strip(),
                "menu": menu.strip(),
                "assignee": assignee.strip(),
                "priority": priority,
                "required_checks": (
                    row["required_checks"]
                    if required_checks is None
                    else json.dumps([check.strip() for check in required_checks if check.strip()])
                ),
                "target_branch": target_branch.strip() or "Human-Resources",
                "source_branch": source_branch.strip(),
                "commit_ref": commit_ref.strip(),
                "mr_url": mr_url,
                "pipeline_url": pipeline_url,
            }
            changed = {key: value for key, value in values.items() if value != row[key]}
            if changed:
                db.execute(
                    """UPDATE tasks SET title=?,description=?,project=?,module=?,menu=?,assignee=?,
                        priority=?,required_checks=?,target_branch=?,source_branch=?,commit_ref=?,mr_url=?,pipeline_url=?,
                        version=version+1,updated_at=? WHERE id=?""",
                    (*values.values(), iso(), row["id"]),
                )
                self._event(db, row["id"], "task.updated", actor, {"changed": changed})
            updated = db.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone()
            return self._task(updated)

    def add_acceptance_criterion(
        self,
        task_ref: str,
        text: str,
        actor: str,
        expected_owner: str | None = None,
        expected_run_id: str | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        if not text.strip():
            raise ValueError("criterion text is required")
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM tasks WHERE id=? OR task_key=?", (task_ref, task_ref)
            ).fetchone()
            if row is None:
                raise KeyError(task_ref)
            self._require_active_owner(row, expected_owner, expected_run_id)
            if expected_version is not None and row["version"] != expected_version:
                raise PermissionError("task changed since it was loaded; refresh and retry")
            criteria = self._criteria(json.loads(row["acceptance_criteria"]))
            criterion = {
                "id": f"criterion_{uuid.uuid4().hex}",
                "text": text.strip(),
                "done": False,
            }
            criteria.append(criterion)
            db.execute(
                """UPDATE tasks SET acceptance_criteria=?,version=version+1,updated_at=?
                    WHERE id=?""",
                (json.dumps(criteria), iso(), row["id"]),
            )
            self._event(db, row["id"], "criterion.added", actor, criterion)
            updated = db.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone()
            return self._task(updated)

    def toggle_acceptance_criterion(
        self,
        task_ref: str,
        criterion_id: str,
        actor: str,
        expected_owner: str | None = None,
        expected_run_id: str | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM tasks WHERE id=? OR task_key=?", (task_ref, task_ref)
            ).fetchone()
            if row is None:
                raise KeyError(task_ref)
            self._require_active_owner(row, expected_owner, expected_run_id)
            if expected_version is not None and row["version"] != expected_version:
                raise PermissionError("task changed since it was loaded; refresh and retry")
            criteria = self._criteria(json.loads(row["acceptance_criteria"]))
            criterion = next((item for item in criteria if item["id"] == criterion_id), None)
            if criterion is None:
                raise KeyError(criterion_id)
            criterion["done"] = not criterion["done"]
            db.execute(
                """UPDATE tasks SET acceptance_criteria=?,version=version+1,updated_at=?
                    WHERE id=?""",
                (json.dumps(criteria), iso(), row["id"]),
            )
            self._event(db, row["id"], "criterion.toggled", actor, criterion)
            updated = db.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone()
            return self._task(updated)

    def admin_transition_task(
        self,
        task_ref: str,
        target_status: str,
        actor: str,
        note: str = "",
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        if target_status not in TASK_STATUSES:
            raise ValueError(f"invalid status: {target_status}")
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM tasks WHERE id=? OR task_key=?", (task_ref, task_ref)
            ).fetchone()
            if row is None:
                raise KeyError(task_ref)
            if expected_version is not None and row["version"] != expected_version:
                raise PermissionError("task changed since it was loaded; refresh and retry")
            if target_status == row["status"]:
                return self._task(row)
            allowed = ADMIN_STATUS_TRANSITIONS.get(row["status"], set())
            if target_status not in allowed:
                raise ValueError(f"invalid manual transition: {row['status']} -> {target_status}")
            if target_status in {"claimed", "in_progress"} and not row["claimed_by"]:
                raise ValueError(f"{target_status} status requires an agent owner")
            now = iso()
            releases = target_status in {
                "inbox",
                "todo",
                "ready_for_review",
                "done",
                "cancelled",
            }
            claimed_by = None if releases else row["claimed_by"]
            lease = None if releases else row["lease_expires_at"]
            db.execute(
                """UPDATE tasks SET status=?,claimed_by=?,lease_expires_at=?,current_run_id=?,progress_note=?,
                    blocker=?,version=version+1,updated_at=? WHERE id=?""",
                (
                    target_status,
                    claimed_by,
                    lease,
                    None if releases else row["current_run_id"],
                    note.strip(),
                    note.strip() if target_status == "blocked" else "",
                    now,
                    row["id"],
                ),
            )
            if releases and row["claimed_by"]:
                db.execute(
                    """UPDATE task_runs SET status=?,finished_at=? WHERE task_id=?
                        AND finished_at IS NULL""",
                    (target_status, now, row["id"]),
                )
                db.execute(
                    """UPDATE agents SET current_task_id=NULL,status='online',updated_at=?
                        WHERE agent_id=?""",
                    (now, row["claimed_by"]),
                )
            self._event(
                db,
                row["id"],
                f"task.{target_status}",
                actor,
                {"note": note, "manual": True},
            )
            updated = db.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone()
            return self._task(updated)

    def add_evidence(
        self,
        task_ref: str,
        *,
        evidence_type: str,
        label: str,
        url: str,
        note: str,
        actor: str,
        expected_owner: str | None = None,
        expected_run_id: str | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        if evidence_type not in EVIDENCE_TYPES:
            raise ValueError(f"invalid evidence type: {evidence_type}")
        url = self._validated_url(url)
        if not label.strip() and not url and not note.strip():
            raise ValueError("evidence requires a label, URL, or note")
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM tasks WHERE id=? OR task_key=?", (task_ref, task_ref)
            ).fetchone()
            if row is None:
                raise KeyError(task_ref)
            self._require_active_owner(row, expected_owner, expected_run_id)
            if expected_version is not None and row["version"] != expected_version:
                raise PermissionError("task changed since it was loaded; refresh and retry")
            evidence_id = f"evidence_{uuid.uuid4().hex}"
            created_at = iso()
            db.execute(
                """INSERT INTO task_evidence(
                    id,task_id,evidence_type,label,url,note,actor,created_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    evidence_id,
                    row["id"],
                    evidence_type,
                    label.strip(),
                    url,
                    note.strip(),
                    actor,
                    created_at,
                ),
            )
            db.execute(
                "UPDATE tasks SET version=version+1,updated_at=? WHERE id=?",
                (created_at, row["id"]),
            )
            payload = {
                "id": evidence_id,
                "evidence_type": evidence_type,
                "label": label.strip(),
                "url": url,
                "note": note.strip(),
            }
            self._event(db, row["id"], "evidence.added", actor, payload)
            return {**payload, "actor": actor, "created_at": created_at}

    def list_evidence(self, task_ref: str) -> list[dict[str, Any]]:
        task = self.get_task(task_ref)
        if task is None:
            raise KeyError(task_ref)
        with self._connect() as db:
            rows = db.execute(
                """SELECT id,evidence_type,label,url,note,actor,created_at
                    FROM task_evidence WHERE task_id=? ORDER BY created_at DESC""",
                (task["id"],),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _internal_status_report(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["sections"] = json.loads(result["sections"])
        result["overrides"] = json.loads(result["overrides"])
        return result

    @staticmethod
    def _report_owner(value: Any) -> str:
        owner = clean_text(value, MAX_REPORT_OWNER_LENGTH)
        if not owner:
            raise ValueError("owner is required")
        return owner

    @staticmethod
    def _report_actor(value: Any) -> str:
        actor = clean_text(value, MAX_REPORT_ACTOR_LENGTH)
        if not actor:
            raise ValueError("report actor identity is required")
        return actor

    @staticmethod
    def _report_version(value: int | None) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not 1 <= value <= MAX_REPORT_VERSION:
            raise ValueError(f"report version must be between 1 and {MAX_REPORT_VERSION}")
        return value

    @staticmethod
    def _report_task_item(
        task: sqlite3.Row,
        *,
        source_event_id: int | None = None,
        source_evidence_id: str | None = None,
        note: str = "",
        url: str = "",
        carry_over: bool = False,
    ) -> dict[str, Any]:
        item: dict[str, Any] = {
            "task_id": task["id"],
            "task_key": clean_text(task["task_key"], 80),
            "title": clean_text(task["title"]),
            "status": task["status"],
            "source": clean_text(task["source"], 80),
            "external_id": clean_text(task["external_id"], 240),
            "carry_over": carry_over,
        }
        if source_event_id is not None:
            item["source_event_id"] = source_event_id
        if source_evidence_id:
            item["source_evidence_id"] = source_evidence_id
        if note:
            item["note"] = clean_text(note)
        if url:
            item["url"] = clean_text(url, 2_000)
        return item

    def _build_internal_status_sections(
        self,
        db: sqlite3.Connection,
        *,
        owner: str,
        report_date: str,
        period: str,
        timezone_name: str,
        overrides: dict[str, list[dict[str, str]]],
    ) -> dict[str, list[dict[str, Any]]]:
        parsed_date = parse_report_date(report_date, timezone_name)
        done_date = parsed_date - timedelta(days=1) if period == "morning" else parsed_date
        done_start, done_end = utc_date_window(done_date, timezone_name)
        day_start, day_end = utc_date_window(parsed_date, timezone_name)
        tasks = db.execute(
            "SELECT * FROM tasks WHERE lower(assignee)=lower(?) ORDER BY task_key",
            (owner,),
        ).fetchall()
        by_ref: dict[str, sqlite3.Row] = {}
        for task in tasks:
            by_ref[task["id"]] = task
            by_ref[task["task_key"]] = task

        sections: dict[str, list[dict[str, Any]]] = {
            "done": [],
            "plan": [],
            "in_progress": [],
            "blocker": [],
            "merge_requests": [],
        }
        done_rows = db.execute(
            """SELECT task_events.id AS event_id,tasks.* FROM task_events
                JOIN tasks ON tasks.id=task_events.task_id
                WHERE lower(tasks.assignee)=lower(?) AND task_events.event_type='task.done'
                  AND task_events.created_at>=? AND task_events.created_at<?
                ORDER BY tasks.task_key,task_events.id""",
            (owner, done_start.isoformat(), done_end.isoformat()),
        ).fetchall()
        seen_done: set[str] = set()
        for task in done_rows:
            if task["id"] in seen_done:
                continue
            seen_done.add(task["id"])
            sections["done"].append(
                self._report_task_item(task, source_event_id=int(task["event_id"]))
            )

        if period == "morning":
            event_rows = db.execute(
                """SELECT task_events.id AS event_id,task_events.event_type,task_events.payload,
                           task_events.created_at,tasks.*
                    FROM task_events JOIN tasks ON tasks.id=task_events.task_id
                    WHERE lower(tasks.assignee)=lower(?)
                      AND task_events.created_at>=? AND task_events.created_at<?
                    ORDER BY tasks.task_key,task_events.id""",
                (owner, day_start.isoformat(), day_end.isoformat()),
            ).fetchall()
            plan_sources: dict[str, int] = {}
            for event in event_rows:
                if event["status"] != "todo":
                    continue
                if event["event_type"] == "task.created":
                    plan_sources.setdefault(event["id"], int(event["event_id"]))
                elif event["event_type"] == "task.updated":
                    payload = json.loads(event["payload"])
                    changed = payload.get("changed", {}) if isinstance(payload, dict) else {}
                    if str(changed.get("assignee", "")).casefold() == owner.casefold():
                        plan_sources.setdefault(event["id"], int(event["event_id"]))
            for task in tasks:
                if task["id"] in plan_sources:
                    sections["plan"].append(
                        self._report_task_item(
                            task, source_event_id=plan_sources[task["id"]], carry_over=False
                        )
                    )

        state_event_rows = db.execute(
            """SELECT max(task_events.id) AS id,task_events.task_id FROM task_events
                JOIN tasks ON tasks.id=task_events.task_id
                WHERE lower(tasks.assignee)=lower(?)
                  AND task_events.event_type IN ('task.claimed','task.in_progress',
                                                 'task.ready_for_review','task.blocked')
                GROUP BY task_events.task_id""",
            (owner,),
        ).fetchall()
        state_event_ids: dict[str, int] = {}
        for event in state_event_rows:
            if event["task_id"] in by_ref:
                state_event_ids.setdefault(event["task_id"], int(event["id"]))
        for task in tasks:
            if task["status"] in {"claimed", "in_progress", "ready_for_review"}:
                sections["in_progress"].append(
                    self._report_task_item(task, source_event_id=state_event_ids.get(task["id"]))
                )
            elif task["status"] == "blocked":
                sections["blocker"].append(
                    self._report_task_item(
                        task,
                        source_event_id=state_event_ids.get(task["id"]),
                        note=task["blocker"] or task["progress_note"],
                    )
                )

        if period == "evening":
            evidence_rows = db.execute(
                """SELECT task_evidence.id AS evidence_id,task_evidence.label,
                           task_evidence.url,task_evidence.note,tasks.*
                    FROM task_evidence JOIN tasks ON tasks.id=task_evidence.task_id
                    WHERE lower(tasks.assignee)=lower(?)
                      AND task_evidence.evidence_type='merge_request'
                      AND task_evidence.created_at>=? AND task_evidence.created_at<?
                    ORDER BY tasks.task_key,task_evidence.created_at,task_evidence.id""",
                (owner, day_start.isoformat(), day_end.isoformat()),
            ).fetchall()
            seen_mr: set[tuple[str, str]] = set()
            for task in evidence_rows:
                marker = (task["id"], task["url"])
                if marker in seen_mr:
                    continue
                seen_mr.add(marker)
                sections["merge_requests"].append(
                    self._report_task_item(
                        task,
                        source_evidence_id=task["evidence_id"],
                        note=task["label"] or task["note"] or task["title"],
                        url=task["url"],
                    )
                )
            mr_events = db.execute(
                """SELECT task_events.id AS event_id,task_events.payload,tasks.*
                    FROM task_events JOIN tasks ON tasks.id=task_events.task_id
                    WHERE lower(tasks.assignee)=lower(?) AND task_events.event_type='task.updated'
                      AND task_events.created_at>=? AND task_events.created_at<?
                    ORDER BY tasks.task_key,task_events.id""",
                (owner, day_start.isoformat(), day_end.isoformat()),
            ).fetchall()
            for task in mr_events:
                payload = json.loads(task["payload"])
                changed = payload.get("changed", {}) if isinstance(payload, dict) else {}
                url = str(changed.get("mr_url", ""))
                marker = (task["id"], url)
                if url and marker not in seen_mr:
                    seen_mr.add(marker)
                    sections["merge_requests"].append(
                        self._report_task_item(task, source_event_id=int(task["event_id"]), url=url)
                    )

        for exclusion in overrides["exclude"]:
            section = exclusion["section"]
            task = by_ref.get(exclusion["task_ref"])
            if task:
                sections[section] = [
                    item for item in sections[section] if item["task_id"] != task["id"]
                ]
        allowed_sections = {"done", "in_progress", "blocker"}
        allowed_sections.add("plan" if period == "morning" else "merge_requests")
        for inclusion in overrides["include"]:
            section = inclusion["section"]
            if section not in allowed_sections:
                raise ValueError(f"section {section} is not available for {period} reports")
            task = by_ref.get(inclusion["task_ref"])
            if task is None:
                raise KeyError(inclusion["task_ref"])
            existing = next(
                (item for item in sections[section] if item["task_id"] == task["id"]),
                None,
            )
            if existing is not None:
                if inclusion.get("note"):
                    existing["note"] = inclusion["note"]
                continue
            sections[section].append(
                self._report_task_item(
                    task,
                    note=inclusion.get("note", ""),
                    carry_over=section == "plan"
                    and local_date(task["created_at"], timezone_name) < parsed_date,
                )
            )
        for key in sections:
            sections[key] = sorted(
                sections[key], key=lambda item: (item["task_key"], item.get("url", ""))
            )[:MAX_REPORT_ITEMS_PER_SECTION]
        return sections

    def generate_internal_status_report(
        self,
        *,
        owner: str,
        report_date: str | None,
        period: str,
        timezone_name: str = "Asia/Jakarta",
        actor: str,
        regenerate: bool = False,
        overrides: dict[str, Any] | None = None,
        expected_version: int | None = None,
        require_draft: bool = False,
    ) -> dict[str, Any]:
        owner = self._report_owner(owner)
        actor = self._report_actor(actor)
        expected_version = self._report_version(expected_version)
        period = validate_period(period)
        parsed_date = parse_report_date(report_date, timezone_name)
        normalized_overrides = validate_overrides(overrides)
        with self._transaction() as db:
            latest = db.execute(
                """SELECT * FROM internal_status_reports
                    WHERE lower(owner)=lower(?) AND report_date=? AND period=?
                    ORDER BY report_version DESC LIMIT 1""",
                (owner, parsed_date.isoformat(), period),
            ).fetchone()
            if latest is not None and not regenerate:
                return self._internal_status_report(latest)
            if regenerate:
                if latest is not None and expected_version is None:
                    raise PermissionError(
                        "regeneration requires expected_version for the latest report"
                    )
                if expected_version is not None and (
                    latest is None or int(latest["report_version"]) != expected_version
                ):
                    raise PermissionError("report changed since it was loaded; refresh and retry")
            if require_draft and latest is not None and latest["state"] != "draft":
                raise PermissionError("approved or sent report snapshots are immutable")
            if latest is not None:
                owner = latest["owner"]
            report_version = int(latest["report_version"]) + 1 if latest else 1
            sections = self._build_internal_status_sections(
                db,
                owner=owner,
                report_date=parsed_date.isoformat(),
                period=period,
                timezone_name=timezone_name,
                overrides=normalized_overrides,
            )
            rendered = render_report(owner, parsed_date, period, sections)
            report_id = f"report_{uuid.uuid4().hex}"
            now = iso()
            db.execute(
                """INSERT INTO internal_status_reports(
                    id,owner,report_date,period,report_version,state,timezone,generated_at,
                    generated_by,rendered_text,sections,overrides,created_at
                ) VALUES(?,?,?,?,?,'draft',?,?,?,?,?,?,?)""",
                (
                    report_id,
                    owner,
                    parsed_date.isoformat(),
                    period,
                    report_version,
                    timezone_name,
                    now,
                    actor,
                    rendered,
                    json.dumps(sections, ensure_ascii=False, sort_keys=True),
                    json.dumps(normalized_overrides, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            row = db.execute(
                "SELECT * FROM internal_status_reports WHERE id=?", (report_id,)
            ).fetchone()
            return self._internal_status_report(row)

    def get_internal_status_report(
        self, owner: str, report_date: str, period: str, version: int | None = None
    ) -> dict[str, Any] | None:
        owner = self._report_owner(owner)
        version = self._report_version(version)
        period = validate_period(period)
        parsed_date = parse_report_date(report_date, "Asia/Jakarta")
        with self._connect() as db:
            if version is None:
                row = db.execute(
                    """SELECT * FROM internal_status_reports
                        WHERE lower(owner)=lower(?) AND report_date=? AND period=?
                        ORDER BY report_version DESC LIMIT 1""",
                    (owner, parsed_date.isoformat(), period),
                ).fetchone()
            else:
                row = db.execute(
                    """SELECT * FROM internal_status_reports
                        WHERE lower(owner)=lower(?) AND report_date=? AND period=?
                          AND report_version=?""",
                    (owner, parsed_date.isoformat(), period, version),
                ).fetchone()
        return self._internal_status_report(row) if row else None

    def list_internal_status_reports(
        self, *, owner: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        values: list[Any] = []
        clauses = ""
        if owner is not None:
            normalized_owner = clean_text(owner, MAX_REPORT_OWNER_LENGTH)
            if not normalized_owner:
                return []
            clauses = "WHERE lower(owner)=lower(?)"
            values.append(normalized_owner)
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("limit must be an integer")
        values.append(max(1, min(limit, 200)))
        with self._connect() as db:
            rows = db.execute(
                f"""SELECT id,owner,report_date,period,report_version,state,timezone,
                           generated_at,generated_by,approved_by,approved_at,sent_by,sent_at,
                           created_at
                    FROM internal_status_reports {clauses}
                    ORDER BY report_date DESC,period,report_version DESC LIMIT ?""",
                values,
            ).fetchall()
        return [dict(row) for row in rows]

    def revise_internal_status_report(
        self,
        *,
        owner: str,
        report_date: str,
        period: str,
        expected_version: int,
        overrides: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        latest = self.get_internal_status_report(owner, report_date, period)
        if latest is None:
            raise KeyError(f"{owner}:{report_date}:{period}")
        return self.generate_internal_status_report(
            owner=owner,
            report_date=report_date,
            period=period,
            timezone_name=latest["timezone"],
            actor=actor,
            regenerate=True,
            overrides=overrides,
            expected_version=expected_version,
            require_draft=True,
        )

    def transition_internal_status_report(
        self,
        *,
        owner: str,
        report_date: str,
        period: str,
        expected_version: int,
        target_state: str,
        actor: str,
    ) -> dict[str, Any]:
        if target_state not in {"approved", "sent"}:
            raise ValueError("invalid report state transition")
        owner = self._report_owner(owner)
        actor = self._report_actor(actor)
        expected_version = self._report_version(expected_version)
        parsed_date = parse_report_date(report_date, "Asia/Jakarta")
        with self._transaction() as db:
            row = db.execute(
                """SELECT * FROM internal_status_reports
                    WHERE lower(owner)=lower(?) AND report_date=? AND period=?
                    ORDER BY report_version DESC LIMIT 1""",
                (owner, parsed_date.isoformat(), validate_period(period)),
            ).fetchone()
            if row is None:
                raise KeyError(f"{owner}:{report_date}:{period}")
            if int(row["report_version"]) != expected_version:
                raise PermissionError("report changed since it was loaded; refresh and retry")
            if row["state"] == target_state or (row["state"] == "sent" and target_state == "sent"):
                return self._internal_status_report(row)
            expected_state = "draft" if target_state == "approved" else "approved"
            if row["state"] != expected_state:
                raise PermissionError(f"report must be {expected_state} before {target_state}")
            now = iso()
            if target_state == "approved":
                db.execute(
                    """UPDATE internal_status_reports SET state='approved',approved_by=?,approved_at=?
                        WHERE id=? AND state='draft'""",
                    (actor, now, row["id"]),
                )
            else:
                db.execute(
                    """UPDATE internal_status_reports SET state='sent',sent_by=?,sent_at=?
                        WHERE id=? AND state='approved'""",
                    (actor, now, row["id"]),
                )
            updated = db.execute(
                "SELECT * FROM internal_status_reports WHERE id=?", (row["id"],)
            ).fetchone()
            return self._internal_status_report(updated)

    def register_agent(
        self, agent_id: str, server_name: str, capabilities: list[str] | None = None
    ) -> dict[str, Any]:
        if not agent_id.strip() or not server_name.strip():
            raise ValueError("agent_id and server_name are required")
        now = iso()
        with self._transaction() as db:
            existing = db.execute(
                "SELECT agent_id FROM agents WHERE agent_id=?", (agent_id,)
            ).fetchone()
            db.execute(
                """
                INSERT INTO agents(agent_id,server_name,capabilities,last_heartbeat_at,created_at,updated_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    server_name=excluded.server_name,
                    capabilities=excluded.capabilities,
                    status='online',
                    last_heartbeat_at=excluded.last_heartbeat_at,
                    updated_at=excluded.updated_at
                """,
                (agent_id, server_name, json.dumps(capabilities or []), now, now, now),
            )
            self._agent_event(
                db,
                agent_id,
                "agent.heartbeat" if existing else "agent.registered",
                agent_id,
                {"server_name": server_name, "capabilities": capabilities or []},
            )
            row = db.execute("SELECT * FROM agents WHERE agent_id=?", (agent_id,)).fetchone()
        result = dict(row)
        result["capabilities"] = json.loads(result["capabilities"])
        return result

    def heartbeat_agent(self, agent_id: str) -> dict[str, Any]:
        now = iso()
        with self._transaction() as db:
            row = db.execute("SELECT * FROM agents WHERE agent_id=?", (agent_id,)).fetchone()
            if row is None:
                raise KeyError(agent_id)
            status = "busy" if row["current_task_id"] else "online"
            db.execute(
                "UPDATE agents SET status=?,last_heartbeat_at=?,updated_at=? WHERE agent_id=?",
                (status, now, now, agent_id),
            )
            self._agent_event(db, agent_id, "agent.heartbeat", agent_id)
            updated = db.execute("SELECT * FROM agents WHERE agent_id=?", (agent_id,)).fetchone()
        result = dict(updated)
        result["capabilities"] = json.loads(result["capabilities"])
        return result

    def list_agents(self, offline_after_seconds: int = 180) -> list[dict[str, Any]]:
        offline_after_seconds = max(30, min(offline_after_seconds, 3600))
        now = utcnow()
        with self._connect() as db:
            rows = db.execute(
                """SELECT agents.*,tasks.task_key,tasks.title AS task_title,
                          tasks.status AS task_status,tasks.lease_expires_at
                    FROM agents LEFT JOIN tasks ON tasks.id=agents.current_task_id
                    ORDER BY agents.server_name,agents.agent_id"""
            ).fetchall()
        agents: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["capabilities"] = json.loads(item["capabilities"])
            last_seen = datetime.fromisoformat(item["last_heartbeat_at"])
            item["last_seen_seconds"] = max(0, int((now - last_seen).total_seconds()))
            lease = item["lease_expires_at"]
            item["lease_remaining_seconds"] = (
                max(0, int((datetime.fromisoformat(lease) - now).total_seconds()))
                if lease
                else None
            )
            item["current_task"] = (
                {
                    "task_key": item.pop("task_key"),
                    "title": item.pop("task_title"),
                    "status": item.pop("task_status"),
                    "lease_expires_at": lease,
                }
                if item["current_task_id"]
                else None
            )
            if item["mode"] == "disabled":
                item["effective_status"] = "disabled"
            elif item["last_seen_seconds"] > offline_after_seconds:
                item["effective_status"] = "offline"
            elif item["current_task_id"]:
                item["effective_status"] = "busy"
            elif item["mode"] == "draining":
                item["effective_status"] = "draining"
            else:
                item["effective_status"] = "online"
            agents.append(item)
        return agents

    def agent_events(self, agent_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM agent_events WHERE agent_id=? ORDER BY id DESC LIMIT ?",
                (agent_id, max(1, min(limit, 200))),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            event = dict(row)
            event["payload"] = json.loads(event["payload"])
            result.append(event)
        return result

    def set_agent_mode(self, agent_id: str, mode: str, actor: str) -> dict[str, Any]:
        if mode not in AGENT_MODES:
            raise ValueError(f"invalid agent mode: {mode}")
        with self._transaction() as db:
            row = db.execute("SELECT * FROM agents WHERE agent_id=?", (agent_id,)).fetchone()
            if row is None:
                raise KeyError(agent_id)
            if mode == "disabled" and row["current_task_id"]:
                raise ValueError("busy agent must release its task before being disabled")
            db.execute(
                "UPDATE agents SET mode=?,updated_at=? WHERE agent_id=?",
                (mode, iso(), agent_id),
            )
            self._agent_event(
                db,
                agent_id,
                "agent.mode_changed",
                actor,
                {"from": row["mode"], "to": mode},
            )
            updated = db.execute("SELECT * FROM agents WHERE agent_id=?", (agent_id,)).fetchone()
        result = dict(updated)
        result["capabilities"] = json.loads(result["capabilities"])
        return result

    def claim_task(
        self,
        task_ref: str,
        agent_id: str,
        idempotency_key: str,
        lease_seconds: int = 1800,
    ) -> dict[str, Any]:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        lease_seconds = max(60, min(lease_seconds, 7200))
        now = utcnow()
        expires = now + timedelta(seconds=lease_seconds)
        with self._transaction() as db:
            agent = db.execute("SELECT * FROM agents WHERE agent_id=?", (agent_id,)).fetchone()
            if agent is None:
                raise PermissionError("agent must register before claiming a task")
            if agent["mode"] != "active":
                raise PermissionError(f"agent cannot claim tasks while mode is {agent['mode']}")
            prior = db.execute(
                "SELECT task_id,agent_id FROM task_runs WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if prior:
                row = db.execute("SELECT * FROM tasks WHERE id=?", (prior["task_id"],)).fetchone()
                if prior["agent_id"] != agent_id or task_ref not in {row["id"], row["task_key"]}:
                    raise PermissionError("idempotency key belongs to another claim")
                return self._task(row)
            if agent["current_task_id"]:
                current = db.execute(
                    "SELECT id,task_key,lease_expires_at FROM tasks WHERE id=?",
                    (agent["current_task_id"],),
                ).fetchone()
                if (
                    current
                    and current["lease_expires_at"]
                    and current["lease_expires_at"] > iso(now)
                ):
                    raise PermissionError(f"agent already owns active task {current['task_key']}")
                raise PermissionError("agent has an expired task awaiting lease reconciliation")
            row = db.execute(
                "SELECT * FROM tasks WHERE id=? OR task_key=?", (task_ref, task_ref)
            ).fetchone()
            if row is None:
                raise KeyError(task_ref)
            task = self._task(row)
            lease_expired = bool(
                task["lease_expires_at"] and task["lease_expires_at"] <= now.isoformat()
            )
            if task["claimed_by"] and task["claimed_by"] != agent_id and not lease_expired:
                raise PermissionError(f"task already claimed by {task['claimed_by']}")
            if task["claimed_by"] and lease_expired:
                previous_agent = task["claimed_by"]
                db.execute(
                    """UPDATE task_runs SET status='lease_expired',finished_at=?
                        WHERE id=? AND finished_at IS NULL""",
                    (now.isoformat(), task["current_run_id"]),
                )
                db.execute(
                    """UPDATE agents SET current_task_id=NULL,status='online',updated_at=?
                        WHERE agent_id=? AND current_task_id=?""",
                    (now.isoformat(), previous_agent, task["id"]),
                )
                self._event(
                    db,
                    task["id"],
                    "task.lease_expired",
                    "claim-recovery",
                    {"agent_id": previous_agent, "expired_at": task["lease_expires_at"]},
                )
                self._agent_event(
                    db,
                    previous_agent,
                    "agent.lease_expired",
                    "claim-recovery",
                    {"task_key": task["task_key"]},
                )
            if task["status"] in {"done", "cancelled", "ready_for_review"}:
                raise ValueError(f"task cannot be claimed from status {task['status']}")
            run_id = f"run_{uuid.uuid4().hex}"
            db.execute(
                """UPDATE tasks SET status='claimed',claimed_by=?,lease_expires_at=?,current_run_id=?,
                    version=version+1,updated_at=? WHERE id=?""",
                (agent_id, expires.isoformat(), run_id, now.isoformat(), task["id"]),
            )
            db.execute(
                """INSERT INTO task_runs(id,task_id,agent_id,idempotency_key,status,started_at,heartbeat_at)
                    VALUES(?,?,?,?,?,?,?)""",
                (
                    run_id,
                    task["id"],
                    agent_id,
                    idempotency_key,
                    "claimed",
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            db.execute(
                """UPDATE agents SET current_task_id=?,status='busy',last_heartbeat_at=?,updated_at=?
                    WHERE agent_id=?""",
                (task["id"], now.isoformat(), now.isoformat(), agent_id),
            )
            self._event(
                db,
                task["id"],
                "task.claimed",
                agent_id,
                {"run_id": run_id, "lease_expires_at": expires.isoformat()},
            )
            self._agent_event(
                db,
                agent_id,
                "agent.task_claimed",
                agent_id,
                {"task_key": task["task_key"], "lease_expires_at": expires.isoformat()},
            )
            updated = db.execute("SELECT * FROM tasks WHERE id=?", (task["id"],)).fetchone()
            return self._task(updated)

    def heartbeat(
        self,
        task_ref: str,
        agent_id: str,
        run_id: str,
        lease_seconds: int = 1800,
    ) -> dict[str, Any]:
        lease_seconds = max(60, min(lease_seconds, 7200))
        now = utcnow()
        expires = now + timedelta(seconds=lease_seconds)
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM tasks WHERE id=? OR task_key=?", (task_ref, task_ref)
            ).fetchone()
            if row is None:
                raise KeyError(task_ref)
            if row["claimed_by"] != agent_id:
                raise PermissionError("agent does not own this task")
            if not run_id or row["current_run_id"] != run_id:
                raise PermissionError("task run fencing token is stale")
            self._require_active_owner(row, agent_id, run_id)
            db.execute(
                "UPDATE tasks SET lease_expires_at=?,updated_at=? WHERE id=?",
                (expires.isoformat(), now.isoformat(), row["id"]),
            )
            db.execute(
                """UPDATE task_runs SET heartbeat_at=? WHERE id=? AND task_id=? AND agent_id=?
                    AND finished_at IS NULL""",
                (now.isoformat(), run_id, row["id"], agent_id),
            )
            db.execute(
                "UPDATE agents SET last_heartbeat_at=?,updated_at=? WHERE agent_id=?",
                (now.isoformat(), now.isoformat(), agent_id),
            )
            self._agent_event(
                db,
                agent_id,
                "agent.task_heartbeat",
                agent_id,
                {"task_key": row["task_key"], "run_id": run_id},
            )
            updated = db.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone()
            return self._task(updated)

    def transition_task(
        self,
        task_ref: str,
        agent_id: str,
        run_id: str,
        target_status: str,
        *,
        note: str = "",
        blocker: str = "",
    ) -> dict[str, Any]:
        if target_status not in TASK_STATUSES:
            raise ValueError(f"invalid status: {target_status}")
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM tasks WHERE id=? OR task_key=?", (task_ref, task_ref)
            ).fetchone()
            if row is None:
                raise KeyError(task_ref)
            if row["claimed_by"] != agent_id:
                raise PermissionError("agent does not own this task")
            if not run_id or row["current_run_id"] != run_id:
                raise PermissionError("task run fencing token is stale")
            self._require_active_owner(row, agent_id, run_id)
            allowed = AGENT_STATUS_TRANSITIONS.get(row["status"], set())
            if target_status not in allowed:
                raise ValueError(f"invalid agent transition: {row['status']} -> {target_status}")
            now = iso()
            releases = target_status in {"ready_for_review", "done", "cancelled", "todo"}
            db.execute(
                """UPDATE tasks SET status=?,progress_note=?,blocker=?,claimed_by=?,
                    lease_expires_at=?,current_run_id=?,version=version+1,updated_at=? WHERE id=?""",
                (
                    target_status,
                    note.strip(),
                    blocker.strip(),
                    None if releases else agent_id,
                    None if releases else row["lease_expires_at"],
                    None if releases else run_id,
                    now,
                    row["id"],
                ),
            )
            if releases:
                db.execute(
                    """UPDATE task_runs SET status=?,finished_at=? WHERE id=? AND task_id=?
                        AND agent_id=? AND finished_at IS NULL""",
                    (target_status, now, run_id, row["id"], agent_id),
                )
                db.execute(
                    """UPDATE agents SET current_task_id=NULL,status='online',updated_at=?
                        WHERE agent_id=?""",
                    (now, agent_id),
                )
            self._event(
                db,
                row["id"],
                f"task.{target_status}",
                agent_id,
                {"note": note, "blocker": blocker},
            )
            self._agent_event(
                db,
                agent_id,
                f"agent.task_{target_status}",
                agent_id,
                {"task_key": row["task_key"]},
            )
            updated = db.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone()
            return self._task(updated)

    def reconcile_expired_leases(self, actor: str = "lease-monitor") -> dict[str, Any]:
        now = utcnow()
        released: list[dict[str, str]] = []
        with self._transaction() as db:
            rows = db.execute(
                """SELECT * FROM tasks WHERE claimed_by IS NOT NULL
                    AND lease_expires_at IS NOT NULL AND lease_expires_at<=?""",
                (now.isoformat(),),
            ).fetchall()
            for row in rows:
                agent_id = row["claimed_by"]
                db.execute(
                    """UPDATE tasks SET status='todo',claimed_by=NULL,lease_expires_at=NULL,current_run_id=NULL,
                        blocker='',version=version+1,updated_at=? WHERE id=?""",
                    (now.isoformat(), row["id"]),
                )
                db.execute(
                    """UPDATE task_runs SET status='lease_expired',finished_at=? WHERE id=?
                        AND task_id=? AND finished_at IS NULL""",
                    (now.isoformat(), row["current_run_id"], row["id"]),
                )
                db.execute(
                    """UPDATE agents SET current_task_id=NULL,status='online',updated_at=?
                        WHERE agent_id=? AND current_task_id=?""",
                    (now.isoformat(), agent_id, row["id"]),
                )
                payload = {"task_key": row["task_key"], "expired_at": row["lease_expires_at"]}
                self._event(db, row["id"], "task.lease_expired", actor, payload)
                self._agent_event(db, agent_id, "agent.lease_expired", actor, payload)
                released.append({"task_key": row["task_key"], "agent_id": agent_id})
        return {"released": released, "count": len(released), "checked_at": now.isoformat()}

    def create_approval_request(
        self,
        *,
        action_type: str,
        title: str,
        reason: str,
        payload: dict[str, Any],
        requested_by: str,
        idempotency_key: str,
        task_ref: str | None = None,
        task_run_id: str | None = None,
        admin_request: bool = False,
    ) -> dict[str, Any]:
        action_type = action_type.strip()
        title = title.strip()
        reason = reason.strip()
        requested_by = requested_by.strip()
        idempotency_key = idempotency_key.strip()
        if action_type not in APPROVAL_ACTION_TYPES:
            raise ValueError("unsupported approval action type")
        if not title or not requested_by or not idempotency_key:
            raise ValueError("title, requested_by, and idempotency_key are required")
        if len(title) > 300 or len(reason) > 20_000 or len(idempotency_key) > 240:
            raise ValueError("approval request fields exceed allowed length")
        self._validate_approval_payload(action_type, payload)
        encoded_payload, payload_sha256 = self._safe_json_object(payload)
        now = iso()
        with self._transaction() as db:
            task = None
            if task_ref:
                task = db.execute(
                    "SELECT * FROM tasks WHERE id=? OR task_key=?", (task_ref, task_ref)
                ).fetchone()
                if task is None:
                    raise KeyError(task_ref)
                if not admin_request:
                    self._require_active_owner(task, requested_by, task_run_id)

            prior = db.execute(
                "SELECT * FROM approval_requests WHERE requested_by=? AND idempotency_key=?",
                (requested_by, idempotency_key),
            ).fetchone()
            if prior is not None:
                if (
                    prior["action_type"] != action_type
                    or prior["payload_sha256"] != payload_sha256
                    or prior["title"] != title
                    or prior["reason"] != reason
                    or prior["task_id"] != (task["id"] if task is not None else None)
                ):
                    raise PermissionError(
                        "approval idempotency key was reused with different content"
                    )
                return self._approval(prior)

            approval_id = f"approval_{uuid.uuid4().hex}"
            approval_key = self._next_key(db, "APR")
            db.execute(
                """INSERT INTO approval_requests(
                    id,approval_key,task_id,action_type,title,reason,payload,payload_sha256,
                    requested_by,idempotency_key,status,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    approval_id,
                    approval_key,
                    task["id"] if task is not None else None,
                    action_type,
                    title,
                    reason,
                    encoded_payload,
                    payload_sha256,
                    requested_by,
                    idempotency_key,
                    "pending",
                    now,
                    now,
                ),
            )
            event_payload = {
                "approval_key": approval_key,
                "action_type": action_type,
                "payload_sha256": payload_sha256,
            }
            self._approval_event(db, approval_id, "approval.requested", requested_by, event_payload)
            if task is not None:
                self._event(db, task["id"], "approval.requested", requested_by, event_payload)
            row = db.execute(
                "SELECT * FROM approval_requests WHERE id=?", (approval_id,)
            ).fetchone()
            return self._approval(row)

    def list_approvals(
        self,
        *,
        status: str | None = None,
        task_ref: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if status is not None and status not in APPROVAL_STATUSES:
            raise ValueError("invalid approval status")
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("approval_requests.status=?")
            params.append(status)
        if task_ref:
            clauses.append("(tasks.id=? OR tasks.task_key=?)")
            params.extend([task_ref, task_ref])
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(limit, 200)))
        with self._connect() as db:
            rows = db.execute(
                f"""SELECT approval_requests.*,tasks.task_key
                    FROM approval_requests LEFT JOIN tasks ON tasks.id=approval_requests.task_id
                    {where} ORDER BY approval_requests.created_at DESC LIMIT ?""",
                params,
            ).fetchall()
        return [self._approval(row) for row in rows]

    def get_approval(self, approval_ref: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT approval_requests.*,tasks.task_key
                    FROM approval_requests LEFT JOIN tasks ON tasks.id=approval_requests.task_id
                    WHERE approval_requests.id=? OR approval_requests.approval_key=?""",
                (approval_ref, approval_ref),
            ).fetchone()
        return self._approval(row) if row is not None else None

    def approval_events(self, approval_ref: str, limit: int = 100) -> list[dict[str, Any]]:
        approval = self.get_approval(approval_ref)
        if approval is None:
            raise KeyError(approval_ref)
        with self._connect() as db:
            rows = db.execute(
                """SELECT * FROM approval_events WHERE approval_id=?
                    ORDER BY id DESC LIMIT ?""",
                (approval["id"], max(1, min(limit, 200))),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            result.append(item)
        return result

    def decide_approval(
        self,
        approval_ref: str,
        decision: str,
        actor: str,
        note: str,
        expected_version: int,
        approval_ttl_seconds: int = 3600,
    ) -> dict[str, Any]:
        decision = decision.strip()
        actor = actor.strip()
        note = note.strip()
        transitions = {
            "approve": ({"pending", "failed", "expired"}, "approved"),
            "reject": ({"pending", "approved"}, "rejected"),
            "cancel": ({"pending", "approved"}, "cancelled"),
        }
        if decision not in transitions:
            raise ValueError("invalid approval decision")
        if not actor:
            raise ValueError("decision actor is required")
        if len(note) > 20_000:
            raise ValueError("decision note exceeds allowed length")
        allowed_from, target = transitions[decision]
        now = utcnow()
        ttl = max(300, min(approval_ttl_seconds, 24 * 3600))
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM approval_requests WHERE id=? OR approval_key=?",
                (approval_ref, approval_ref),
            ).fetchone()
            if row is None:
                raise KeyError(approval_ref)
            if row["version"] != expected_version:
                raise PermissionError("approval changed since it was loaded")
            if decision == "approve" and actor == row["requested_by"]:
                raise PermissionError("approval requester cannot approve their own request")
            if row["status"] not in allowed_from:
                raise ValueError(f"cannot {decision} approval from {row['status']}")
            expires_at = (
                (now + timedelta(seconds=ttl)).isoformat() if target == "approved" else None
            )
            cursor = db.execute(
                """UPDATE approval_requests SET status=?,version=version+1,decided_by=?,
                    decision_note=?,decided_at=?,expires_at=?,claimed_by=NULL,
                    lease_expires_at=NULL,current_run_id=NULL,updated_at=?
                    WHERE id=? AND version=?""",
                (
                    target,
                    actor,
                    note,
                    now.isoformat(),
                    expires_at,
                    now.isoformat(),
                    row["id"],
                    expected_version,
                ),
            )
            if cursor.rowcount != 1:
                raise PermissionError("approval changed since it was loaded")
            event_payload = {"from": row["status"], "to": target, "note": note}
            self._approval_event(db, row["id"], f"approval.{target}", actor, event_payload)
            if row["task_id"]:
                self._event(db, row["task_id"], f"approval.{target}", actor, event_payload)
            updated = db.execute(
                "SELECT * FROM approval_requests WHERE id=?", (row["id"],)
            ).fetchone()
            return self._approval(updated)

    def claim_approval(
        self,
        approval_ref: str,
        executor_id: str,
        idempotency_key: str,
        lease_seconds: int = 900,
    ) -> dict[str, Any]:
        executor_id = executor_id.strip()
        idempotency_key = idempotency_key.strip()
        if not executor_id or not idempotency_key:
            raise ValueError("executor_id and idempotency_key are required")
        if len(idempotency_key) > 240:
            raise ValueError("idempotency_key exceeds allowed length")
        now = utcnow()
        lease_seconds = max(60, min(lease_seconds, 3600))
        expired = False
        result: dict[str, Any] | None = None
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM approval_requests WHERE id=? OR approval_key=?",
                (approval_ref, approval_ref),
            ).fetchone()
            if row is None:
                raise KeyError(approval_ref)
            prior_run = db.execute(
                "SELECT * FROM approval_runs WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if prior_run is not None:
                if prior_run["approval_id"] != row["id"] or prior_run["executor_id"] != executor_id:
                    raise PermissionError("approval execution idempotency key is already in use")
                if prior_run["status"] == "timed_out":
                    raise PermissionError(
                        "approval execution attempt timed out; use a new idempotency key"
                    )
                result = self._approval(row)
                result["run_id"] = prior_run["id"]
                result["provider_key"] = prior_run["provider_key"]
                return result

            agent = db.execute("SELECT * FROM agents WHERE agent_id=?", (executor_id,)).fetchone()
            if agent is None:
                raise PermissionError("executor must register before claiming an approval")
            if agent["mode"] != "active":
                raise PermissionError(f"executor is {agent['mode']} and cannot claim approvals")
            capabilities = set(json.loads(agent["capabilities"]))
            if not (
                "approval.execute" in capabilities
                or f"approval.execute:{row['action_type']}" in capabilities
            ):
                raise PermissionError("executor lacks approval execution capability")
            if row["status"] not in {"approved", "failed"}:
                raise PermissionError(f"approval is {row['status']} and cannot be executed")
            if not row["expires_at"] or datetime.fromisoformat(row["expires_at"]) <= now:
                db.execute(
                    """UPDATE approval_requests SET status='expired',version=version+1,
                        updated_at=? WHERE id=? AND status IN ('approved','failed')""",
                    (now.isoformat(), row["id"]),
                )
                self._approval_event(
                    db, row["id"], "approval.expired", "approval-lease-monitor", {}
                )
                expired = True
            else:
                run_id = f"approval_run_{uuid.uuid4().hex}"
                provider_key = hashlib.sha256(
                    f"{row['id']}:{row['action_type']}:{row['payload_sha256']}".encode()
                ).hexdigest()
                lease_expires_at = (now + timedelta(seconds=lease_seconds)).isoformat()
                cursor = db.execute(
                    """UPDATE approval_requests SET status='executing',claimed_by=?,
                        lease_expires_at=?,current_run_id=?,version=version+1,updated_at=?
                        WHERE id=? AND status IN ('approved','failed')""",
                    (executor_id, lease_expires_at, run_id, now.isoformat(), row["id"]),
                )
                if cursor.rowcount != 1:
                    raise PermissionError("approval was claimed concurrently")
                db.execute(
                    """INSERT INTO approval_runs(
                        id,approval_id,executor_id,idempotency_key,provider_key,status,
                        started_at,heartbeat_at
                    ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        run_id,
                        row["id"],
                        executor_id,
                        idempotency_key,
                        provider_key,
                        "running",
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                self._approval_event(
                    db,
                    row["id"],
                    "approval.execution_claimed",
                    executor_id,
                    {"run_id": run_id, "lease_expires_at": lease_expires_at},
                )
                updated = db.execute(
                    "SELECT * FROM approval_requests WHERE id=?", (row["id"],)
                ).fetchone()
                result = self._approval(updated)
                result["run_id"] = run_id
                result["provider_key"] = provider_key
        if expired:
            raise PermissionError("approval expired before execution")
        if result is None:
            raise PermissionError("approval could not be claimed")
        return result

    def heartbeat_approval(
        self,
        approval_ref: str,
        executor_id: str,
        run_id: str,
        lease_seconds: int = 900,
    ) -> dict[str, Any]:
        now = utcnow()
        lease_seconds = max(60, min(lease_seconds, 3600))
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM approval_requests WHERE id=? OR approval_key=?",
                (approval_ref, approval_ref),
            ).fetchone()
            if row is None:
                raise KeyError(approval_ref)
            if (
                row["status"] != "executing"
                or row["claimed_by"] != executor_id
                or row["current_run_id"] != run_id
            ):
                raise PermissionError("approval execution fencing token is stale")
            if (
                not row["lease_expires_at"]
                or datetime.fromisoformat(row["lease_expires_at"]) <= now
            ):
                raise LeaseExpiredError("approval execution lease expired")
            lease_expires_at = (now + timedelta(seconds=lease_seconds)).isoformat()
            db.execute(
                """UPDATE approval_requests SET lease_expires_at=?,updated_at=?
                    WHERE id=? AND current_run_id=? AND claimed_by=?""",
                (lease_expires_at, now.isoformat(), row["id"], run_id, executor_id),
            )
            db.execute(
                """UPDATE approval_runs SET heartbeat_at=?
                    WHERE id=? AND approval_id=? AND executor_id=? AND status='running'""",
                (now.isoformat(), run_id, row["id"], executor_id),
            )
            updated = db.execute(
                "SELECT * FROM approval_requests WHERE id=?", (row["id"],)
            ).fetchone()
            return self._approval(updated)

    def finish_approval(
        self,
        approval_ref: str,
        executor_id: str,
        run_id: str,
        status: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        if status not in {"succeeded", "failed"}:
            raise ValueError("invalid approval execution status")
        encoded_result, _ = self._safe_json_object(result, "result")
        now = utcnow()
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM approval_requests WHERE id=? OR approval_key=?",
                (approval_ref, approval_ref),
            ).fetchone()
            if row is None:
                raise KeyError(approval_ref)
            prior_run = db.execute(
                "SELECT * FROM approval_runs WHERE id=? AND approval_id=? AND executor_id=?",
                (run_id, row["id"], executor_id),
            ).fetchone()
            if prior_run is not None and prior_run["status"] != "running":
                if prior_run["status"] == "timed_out":
                    raise PermissionError("approval execution fencing token is stale")
                if prior_run["status"] == status and prior_run["result"] == encoded_result:
                    return self._approval(row)
                raise PermissionError("approval execution was already finished with another result")
            if (
                row["status"] != "executing"
                or row["claimed_by"] != executor_id
                or row["current_run_id"] != run_id
            ):
                raise PermissionError("approval execution fencing token is stale")
            if (
                not row["lease_expires_at"]
                or datetime.fromisoformat(row["lease_expires_at"]) <= now
            ):
                raise LeaseExpiredError("approval execution lease expired")
            cursor = db.execute(
                """UPDATE approval_runs SET status=?,finished_at=?,heartbeat_at=?,result=?
                    WHERE id=? AND approval_id=? AND executor_id=? AND status='running'""",
                (
                    status,
                    now.isoformat(),
                    now.isoformat(),
                    encoded_result,
                    run_id,
                    row["id"],
                    executor_id,
                ),
            )
            if cursor.rowcount != 1:
                raise PermissionError("approval execution is missing, stale, or already finished")
            cursor = db.execute(
                """UPDATE approval_requests SET status=?,claimed_by=NULL,lease_expires_at=NULL,
                    current_run_id=NULL,version=version+1,updated_at=?
                    WHERE id=? AND current_run_id=? AND claimed_by=?""",
                (status, now.isoformat(), row["id"], run_id, executor_id),
            )
            if cursor.rowcount != 1:
                raise PermissionError("approval execution fencing token is stale")
            event_payload = {"run_id": run_id, "result": json.loads(encoded_result)}
            self._approval_event(db, row["id"], f"approval.{status}", executor_id, event_payload)
            if row["task_id"]:
                self._event(db, row["task_id"], f"approval.{status}", executor_id, event_payload)
            updated = db.execute(
                "SELECT * FROM approval_requests WHERE id=?", (row["id"],)
            ).fetchone()
            return self._approval(updated)

    def list_approval_runs(
        self, approval_ref: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if approval_ref:
            approval = self.get_approval(approval_ref)
            if approval is None:
                raise KeyError(approval_ref)
            where = " WHERE approval_id=?"
            params.append(approval["id"])
        params.append(max(1, min(limit, 200)))
        with self._connect() as db:
            rows = db.execute(
                f"SELECT * FROM approval_runs{where} ORDER BY started_at DESC LIMIT ?", params
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["result"] = json.loads(item["result"])
            result.append(item)
        return result

    def reconcile_expired_approval_leases(
        self, actor: str = "approval-lease-monitor"
    ) -> dict[str, Any]:
        now = utcnow()
        released: list[dict[str, str]] = []
        with self._transaction() as db:
            rows = db.execute(
                """SELECT * FROM approval_requests WHERE status='executing'
                    AND lease_expires_at IS NOT NULL AND lease_expires_at<=?""",
                (now.isoformat(),),
            ).fetchall()
            for row in rows:
                next_status = (
                    "approved"
                    if row["expires_at"] and datetime.fromisoformat(row["expires_at"]) > now
                    else "expired"
                )
                db.execute(
                    """UPDATE approval_runs SET status='timed_out',finished_at=?,result=?
                        WHERE id=? AND approval_id=? AND status='running'""",
                    (
                        now.isoformat(),
                        json.dumps({"reason": "approval execution lease expired"}),
                        row["current_run_id"],
                        row["id"],
                    ),
                )
                db.execute(
                    """UPDATE approval_requests SET status=?,claimed_by=NULL,
                        lease_expires_at=NULL,current_run_id=NULL,version=version+1,updated_at=?
                        WHERE id=?""",
                    (next_status, now.isoformat(), row["id"]),
                )
                payload = {
                    "run_id": row["current_run_id"],
                    "executor_id": row["claimed_by"],
                    "next_status": next_status,
                }
                self._approval_event(db, row["id"], "approval.execution_timed_out", actor, payload)
                if row["task_id"]:
                    self._event(db, row["task_id"], "approval.execution_timed_out", actor, payload)
                released.append(
                    {"approval_key": row["approval_key"], "executor_id": row["claimed_by"]}
                )
            expired_rows = db.execute(
                """SELECT * FROM approval_requests WHERE status IN ('approved','failed')
                    AND expires_at IS NOT NULL AND expires_at<=?""",
                (now.isoformat(),),
            ).fetchall()
            for row in expired_rows:
                db.execute(
                    """UPDATE approval_requests SET status='expired',version=version+1,
                        updated_at=? WHERE id=? AND status IN ('approved','failed')""",
                    (now.isoformat(), row["id"]),
                )
                payload = {"expired_at": row["expires_at"], "from": row["status"]}
                self._approval_event(db, row["id"], "approval.expired", actor, payload)
                if row["task_id"]:
                    self._event(db, row["task_id"], "approval.expired", actor, payload)
                released.append({"approval_key": row["approval_key"], "executor_id": ""})
        return {"released": released, "count": len(released), "checked_at": now.isoformat()}

    def create_schedule(
        self,
        name: str,
        job_type: str,
        interval_seconds: int,
        payload: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        if not name.strip() or not job_type.strip():
            raise ValueError("name and job_type are required")
        job_type = job_type.strip()
        if job_type not in SCHEDULE_JOB_TYPES:
            raise ValueError("unsupported schedule job type")
        if job_type == "internal_status_generate":
            if not isinstance(payload, dict) or set(payload) - {
                "owner",
                "period",
                "timezone",
                "report_date",
            }:
                raise ValueError("invalid internal status schedule payload")
            owner = clean_text(payload.get("owner"), 120)
            if not owner:
                raise ValueError("internal status schedule owner is required")
            period = validate_period(str(payload.get("period", "")))
            timezone_name = clean_text(payload.get("timezone") or "Asia/Jakarta", 64)
            parsed_date = (
                parse_report_date(str(payload["report_date"]), timezone_name).isoformat()
                if payload.get("report_date")
                else None
            )
            payload = {"owner": owner, "period": period, "timezone": timezone_name}
            if parsed_date:
                payload["report_date"] = parsed_date
        else:
            self._safe_json_object(payload, "schedule payload")
        interval_seconds = max(60, min(interval_seconds, 31 * 24 * 3600))
        schedule_id = f"schedule_{uuid.uuid4().hex}"
        now = utcnow()
        with self._transaction() as db:
            db.execute(
                """INSERT INTO schedules(
                    id,name,job_type,interval_seconds,payload,next_run_at,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    schedule_id,
                    name.strip(),
                    job_type,
                    interval_seconds,
                    json.dumps(payload),
                    (now + timedelta(seconds=interval_seconds)).isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            row = db.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
        return self._schedule(row)

    def list_schedules(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM schedules ORDER BY created_at").fetchall()
        return [self._schedule(row) for row in rows]

    def list_schedule_runs(
        self, schedule_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        with self._connect() as db:
            if schedule_id:
                rows = db.execute(
                    """SELECT schedule_runs.*,schedules.name,schedules.job_type
                        FROM schedule_runs JOIN schedules ON schedules.id=schedule_runs.schedule_id
                        WHERE schedule_id=? ORDER BY started_at DESC LIMIT ?""",
                    (schedule_id, max(1, min(limit, 200))),
                ).fetchall()
            else:
                rows = db.execute(
                    """SELECT schedule_runs.*,schedules.name,schedules.job_type
                        FROM schedule_runs JOIN schedules ON schedules.id=schedule_runs.schedule_id
                        ORDER BY started_at DESC LIMIT ?""",
                    (max(1, min(limit, 200)),),
                ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["result"] = json.loads(item["result"])
            result.append(item)
        return result

    def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> dict[str, Any]:
        with self._transaction() as db:
            row = db.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
            if row is None:
                raise KeyError(schedule_id)
            if not enabled and row["locked_by"]:
                db.execute(
                    """UPDATE schedule_runs SET status='cancelled',finished_at=?,result=?
                        WHERE schedule_id=? AND status='running'""",
                    (
                        iso(),
                        json.dumps({"reason": "schedule disabled by administrator"}),
                        schedule_id,
                    ),
                )
            db.execute(
                """UPDATE schedules SET enabled=?,locked_by=NULL,lock_expires_at=NULL,
                    current_run_id=NULL,updated_at=? WHERE id=?""",
                (int(enabled), iso(), schedule_id),
            )
            updated = db.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
        return self._schedule(updated)

    def claim_due_schedule(self, worker_id: str, lease_seconds: int = 300) -> dict[str, Any] | None:
        now = utcnow()
        with self._transaction() as db:
            row = db.execute(
                """SELECT * FROM schedules WHERE enabled=1 AND next_run_at<=?
                    AND (locked_by IS NULL OR lock_expires_at<=?) ORDER BY next_run_at LIMIT 1""",
                (now.isoformat(), now.isoformat()),
            ).fetchone()
            if row is None:
                return None
            if row["locked_by"]:
                db.execute(
                    """UPDATE schedule_runs SET status='timed_out',finished_at=?,result=?
                        WHERE schedule_id=? AND status='running'""",
                    (
                        now.isoformat(),
                        json.dumps({"reason": "schedule worker lease expired"}),
                        row["id"],
                    ),
                )
            run_id = f"schedule_run_{uuid.uuid4().hex}"
            db.execute(
                """UPDATE schedules SET locked_by=?,lock_expires_at=?,current_run_id=?,
                    updated_at=? WHERE id=?""",
                (
                    worker_id,
                    (now + timedelta(seconds=max(60, lease_seconds))).isoformat(),
                    run_id,
                    now.isoformat(),
                    row["id"],
                ),
            )
            db.execute(
                """INSERT INTO schedule_runs(id,schedule_id,worker_id,status,started_at)
                    VALUES(?,?,?,?,?)""",
                (run_id, row["id"], worker_id, "running", now.isoformat()),
            )
            claimed = self._schedule(
                db.execute("SELECT * FROM schedules WHERE id=?", (row["id"],)).fetchone()
            )
            claimed["run_id"] = run_id
            return claimed

    def finish_schedule_run(
        self, schedule_id: str, run_id: str, worker_id: str, status: str, result: dict[str, Any]
    ) -> dict[str, Any]:
        if status not in {"succeeded", "failed", "skipped"}:
            raise ValueError("invalid schedule run status")
        now = utcnow()
        with self._transaction() as db:
            row = db.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
            if row is None:
                raise KeyError(schedule_id)
            if row["locked_by"] != worker_id:
                raise PermissionError("worker does not own this schedule lease")
            if row["current_run_id"] != run_id:
                raise PermissionError("schedule run fencing token is stale")
            cursor = db.execute(
                """UPDATE schedule_runs SET status=?,finished_at=?,result=?
                    WHERE id=? AND schedule_id=? AND worker_id=? AND status='running'""",
                (status, now.isoformat(), json.dumps(result), run_id, schedule_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise PermissionError("schedule run is missing, stale, or already finished")
            cursor = db.execute(
                """UPDATE schedules SET locked_by=NULL,lock_expires_at=NULL,last_run_at=?,
                    current_run_id=NULL,last_status=?,next_run_at=?,updated_at=?
                    WHERE id=? AND locked_by=? AND current_run_id=?""",
                (
                    now.isoformat(),
                    status,
                    (now + timedelta(seconds=row["interval_seconds"])).isoformat(),
                    now.isoformat(),
                    schedule_id,
                    worker_id,
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise PermissionError("schedule run fencing token is stale")
            updated = db.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
            return self._schedule(updated)

    def create_pending_drive_channel(
        self, channel_id: str, file_id: str, token_hash: str, expiration_at: datetime
    ) -> dict[str, Any]:
        now = utcnow()
        with self._transaction() as db:
            db.execute(
                """INSERT INTO drive_watch_channels(
                    channel_id,file_id,token_hash,expiration_at,state,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?)""",
                (
                    channel_id,
                    file_id,
                    token_hash,
                    expiration_at.isoformat(),
                    "active",
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            row = db.execute(
                "SELECT * FROM drive_watch_channels WHERE channel_id=?", (channel_id,)
            ).fetchone()
        return dict(row)

    def set_drive_watch_desired(self, file_id: str, desired: bool) -> None:
        """Persist the administrator's lifecycle intent independently of environment config."""
        now = iso()
        with self._transaction() as db:
            db.execute(
                """INSERT INTO drive_watch_maintenance(file_id,desired_active,updated_at)
                    VALUES(?,?,?) ON CONFLICT(file_id) DO UPDATE SET
                    desired_active=excluded.desired_active,updated_at=excluded.updated_at""",
                (file_id, int(desired), now),
            )

    def drive_watch_desired(self, file_id: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT desired_active FROM drive_watch_maintenance WHERE file_id=?", (file_id,)
            ).fetchone()
        # An enabled environment is capability only. A new database remains stopped until
        # an administrator explicitly registers a watch.
        return bool(row and row["desired_active"])

    def claim_drive_watch_lease(self, file_id: str, owner: str, lease_seconds: int = 300) -> bool:
        now = utcnow()
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM drive_watch_leases WHERE file_id=?", (file_id,)
            ).fetchone()
            if row is not None and row["lock_expires_at"] > now.isoformat():
                return False
            db.execute(
                """INSERT INTO drive_watch_leases(file_id,locked_by,lock_expires_at)
                    VALUES(?,?,?) ON CONFLICT(file_id) DO UPDATE SET
                    locked_by=excluded.locked_by,lock_expires_at=excluded.lock_expires_at""",
                (
                    file_id,
                    owner,
                    (now + timedelta(seconds=max(60, lease_seconds))).isoformat(),
                ),
            )
        return True

    def release_drive_watch_lease(self, file_id: str, owner: str) -> None:
        with self._transaction() as db:
            db.execute(
                "DELETE FROM drive_watch_leases WHERE file_id=? AND locked_by=?", (file_id, owner)
            )

    def bind_drive_channel(
        self,
        channel_id: str,
        resource_id: str,
        resource_uri: str,
        expiration_at: datetime,
    ) -> dict[str, Any]:
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM drive_watch_channels WHERE channel_id=?", (channel_id,)
            ).fetchone()
            if row is None or row["state"] != "active":
                raise KeyError(channel_id)
            if row["resource_id"] not in {None, resource_id}:
                raise ValueError("Drive channel resource binding changed")
            cursor = db.execute(
                """UPDATE drive_watch_channels SET resource_id=?,resource_uri=?,expiration_at=?,
                    updated_at=? WHERE channel_id=? AND state='active'""",
                (resource_id, resource_uri, expiration_at.isoformat(), iso(), channel_id),
            )
            if cursor.rowcount != 1:
                raise PermissionError("Drive channel binding was fenced")
            bound = db.execute(
                "SELECT * FROM drive_watch_channels WHERE channel_id=?", (channel_id,)
            ).fetchone()
        return dict(bound)

    def fail_pending_drive_channel(self, channel_id: str) -> None:
        with self._transaction() as db:
            db.execute(
                """UPDATE drive_watch_channels SET state='stopped',updated_at=?
                    WHERE channel_id=? AND resource_id IS NULL""",
                (iso(), channel_id),
            )

    def mark_drive_channel_cleanup_needed(
        self, channel_id: str, resource_id: str | None = None
    ) -> None:
        """Durably queue a remote channel stop without returning provider identifiers."""
        with self._transaction() as db:
            if resource_id is not None:
                db.execute(
                    """UPDATE drive_watch_channels SET resource_id=COALESCE(resource_id,?),
                        state='stopped',cleanup_status='pending',cleanup_next_attempt_at=?,updated_at=?
                        WHERE channel_id=?""",
                    (resource_id, iso(), iso(), channel_id),
                )
            else:
                db.execute(
                    """UPDATE drive_watch_channels SET state='stopped',cleanup_status='pending',
                        cleanup_next_attempt_at=?,updated_at=? WHERE channel_id=?
                        AND resource_id IS NOT NULL""",
                    (iso(), iso(), channel_id),
                )

    def due_drive_channel_cleanups(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT channel_id,resource_id FROM drive_watch_channels
                    WHERE cleanup_status IN ('pending','failed') AND resource_id IS NOT NULL
                    AND cleanup_attempts<? AND cleanup_next_attempt_at<=?
                    ORDER BY cleanup_next_attempt_at LIMIT ?""",
                (MAX_DRIVE_CLEANUP_ATTEMPTS, iso(), max(1, min(limit, 50))),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_drive_cleanup_result(
        self, channel_id: str, *, success: bool, error_type: str = ""
    ) -> None:
        now = utcnow()
        with self._transaction() as db:
            row = db.execute(
                "SELECT cleanup_attempts FROM drive_watch_channels WHERE channel_id=?",
                (channel_id,),
            ).fetchone()
            if row is None:
                return
            attempts = int(row["cleanup_attempts"]) + 1
            exhausted = not success and attempts >= MAX_DRIVE_CLEANUP_ATTEMPTS
            delay = min(3600, 60 * (2 ** min(max(attempts - 1, 0), 6)))
            db.execute(
                """UPDATE drive_watch_channels SET cleanup_status=?,cleanup_attempts=?,
                    cleanup_next_attempt_at=?,cleanup_last_error_type=?,updated_at=?
                    WHERE channel_id=?""",
                (
                    "succeeded" if success else ("exhausted" if exhausted else "failed"),
                    attempts,
                    None if success or exhausted else (now + timedelta(seconds=delay)).isoformat(),
                    "" if success else error_type[:120],
                    now.isoformat(),
                    channel_id,
                ),
            )

    def replace_drive_channels(self, file_id: str, new_channel_id: str) -> list[dict[str, Any]]:
        with self._transaction() as db:
            rows = db.execute(
                """SELECT * FROM drive_watch_channels WHERE file_id=? AND state='active'
                    AND channel_id<>? AND resource_id IS NOT NULL""",
                (file_id, new_channel_id),
            ).fetchall()
            db.execute(
                """UPDATE drive_watch_channels SET state='replaced',replaced_by=?,
                    cleanup_status='pending',cleanup_next_attempt_at=?,updated_at=?
                    WHERE file_id=? AND state='active' AND channel_id<>?
                    AND resource_id IS NOT NULL""",
                (new_channel_id, iso(), iso(), file_id, new_channel_id),
            )
        return [dict(row) for row in rows]

    def stop_drive_channel(self, channel_id: str) -> dict[str, Any]:
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM drive_watch_channels WHERE channel_id=?", (channel_id,)
            ).fetchone()
            if row is None:
                raise KeyError(channel_id)
            db.execute(
                """UPDATE drive_watch_channels SET state='stopped',cleanup_status=CASE
                    WHEN resource_id IS NULL THEN cleanup_status ELSE 'pending' END,
                    cleanup_next_attempt_at=CASE WHEN resource_id IS NULL THEN cleanup_next_attempt_at
                    ELSE ? END,updated_at=? WHERE channel_id=?""",
                (iso(), iso(), channel_id),
            )
        return dict(row)

    def drive_watch_status(self, file_id: str) -> dict[str, Any]:
        now = utcnow().isoformat()
        with self._transaction() as db:
            db.execute(
                """UPDATE drive_watch_channels SET state='expired',updated_at=?
                    WHERE file_id=? AND state='active' AND expiration_at<=?""",
                (now, file_id, now),
            )
            rows = db.execute(
                """SELECT channel_id,state,expiration_at,resource_id IS NOT NULL AS bound,
                    cleanup_status,last_message_number,created_at,updated_at FROM drive_watch_channels
                    WHERE file_id=? ORDER BY created_at DESC LIMIT 20""",
                (file_id,),
            ).fetchall()
            pending = db.execute(
                """SELECT e.status,COUNT(*) AS count FROM drive_notification_events e
                    JOIN drive_watch_channels c ON c.channel_id=e.channel_id
                    WHERE c.file_id=? GROUP BY e.status""",
                (file_id,),
            ).fetchall()
        return {
            "channels": [dict(row) for row in rows],
            "events": {row["status"]: row["count"] for row in pending},
            "desired_active": self.drive_watch_desired(file_id),
        }

    def get_drive_channel(self, channel_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM drive_watch_channels WHERE channel_id=?", (channel_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def record_drive_notification(
        self,
        channel_id: str,
        token_hash: str,
        resource_id: str,
        message_number: int,
        resource_state: str,
    ) -> str:
        now = utcnow()
        with self._transaction() as db:
            row = db.execute(
                "SELECT * FROM drive_watch_channels WHERE channel_id=?", (channel_id,)
            ).fetchone()
            if row is None or row["state"] != "active" or row["expiration_at"] <= now.isoformat():
                raise PermissionError("Drive channel is unknown or inactive")
            if not hmac.compare_digest(row["token_hash"], token_hash):
                raise PermissionError("Drive channel token is invalid")
            if row["resource_id"] is None:
                if resource_state != "sync" or message_number != 1:
                    raise PermissionError("Drive channel resource is not bound")
            elif not hmac.compare_digest(row["resource_id"], resource_id):
                raise PermissionError("Drive resource does not match the channel")
            existing = db.execute(
                """SELECT 1 FROM drive_notification_events
                    WHERE channel_id=? AND message_number=?""",
                (channel_id, message_number),
            ).fetchone()
            if existing:
                return "duplicate"
            last_number = row["last_message_number"]
            if last_number is not None and message_number <= last_number:
                raise PermissionError("Drive message number is out of order")
            event_status = "ignored" if resource_state == "sync" else "pending"
            available = now if event_status == "ignored" else now + timedelta(seconds=5)
            db.execute(
                """INSERT INTO drive_notification_events(
                    channel_id,message_number,resource_state,received_at,available_at,status,
                    next_attempt_at,finished_at,result
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    channel_id,
                    message_number,
                    resource_state,
                    now.isoformat(),
                    available.isoformat(),
                    event_status,
                    available.isoformat(),
                    now.isoformat() if event_status == "ignored" else None,
                    json.dumps({"action": "acknowledged"}) if event_status == "ignored" else "{}",
                ),
            )
            db.execute(
                """UPDATE drive_watch_channels SET last_message_number=?,updated_at=?
                    WHERE channel_id=?""",
                (message_number, now.isoformat(), channel_id),
            )
        return event_status

    def claim_drive_events(self, worker_id: str, lease_seconds: int = 300) -> dict[str, Any] | None:
        now = utcnow()
        with self._transaction() as db:
            db.execute(
                """UPDATE drive_notification_events SET status='failed',run_id=NULL,locked_by=NULL,
                    lock_expires_at=NULL,next_attempt_at=? WHERE status='running'
                    AND lock_expires_at<=?""",
                (now.isoformat(), now.isoformat()),
            )
            running = db.execute(
                "SELECT 1 FROM drive_notification_events WHERE status='running' LIMIT 1"
            ).fetchone()
            if running:
                return None
            rows = db.execute(
                """SELECT e.id,c.file_id FROM drive_notification_events e
                    JOIN drive_watch_channels c ON c.channel_id=e.channel_id
                    WHERE e.status IN ('pending','failed') AND e.available_at<=?
                    AND e.next_attempt_at<=? ORDER BY e.received_at,e.id""",
                (now.isoformat(), now.isoformat()),
            ).fetchall()
            if not rows:
                return None
            file_id = rows[0]["file_id"]
            ids = [row["id"] for row in rows if row["file_id"] == file_id]
            run_id = f"drive_run_{uuid.uuid4().hex}"
            placeholders = ",".join("?" for _ in ids)
            db.execute(
                f"""UPDATE drive_notification_events SET status='running',attempts=attempts+1,
                    run_id=?,locked_by=?,lock_expires_at=? WHERE id IN ({placeholders})""",
                (
                    run_id,
                    worker_id,
                    (now + timedelta(seconds=max(60, lease_seconds))).isoformat(),
                    *ids,
                ),
            )
        return {"run_id": run_id, "file_id": file_id, "event_ids": ids}

    def defer_drive_events_busy(self, run_id: str, worker_id: str, delay_seconds: int = 5) -> int:
        """Return a busy Drive batch to pending without consuming a failure attempt."""
        with self._transaction() as db:
            cursor = db.execute(
                """UPDATE drive_notification_events SET status='pending',attempts=MAX(attempts-1,0),
                    next_attempt_at=?,run_id=NULL,locked_by=NULL,lock_expires_at=NULL
                    WHERE status='running' AND run_id=? AND locked_by=?""",
                (
                    (utcnow() + timedelta(seconds=max(1, delay_seconds))).isoformat(),
                    run_id,
                    worker_id,
                ),
            )
        return cursor.rowcount

    def next_drive_event_delay(self, maximum_seconds: float = 5.0) -> float | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT MIN(CASE WHEN available_at>next_attempt_at THEN available_at
                    ELSE next_attempt_at END) AS due_at FROM drive_notification_events
                    WHERE status='pending'"""
            ).fetchone()
        if row is None or row["due_at"] is None:
            return None
        delay = (datetime.fromisoformat(row["due_at"]) - utcnow()).total_seconds()
        return max(0.05, min(maximum_seconds, delay))

    def finish_drive_events(
        self, run_id: str, worker_id: str, *, success: bool, result: dict[str, Any]
    ) -> int:
        now = utcnow()
        with self._transaction() as db:
            rows = db.execute(
                """SELECT id,attempts FROM drive_notification_events WHERE status='running'
                    AND run_id=? AND locked_by=?""",
                (run_id, worker_id),
            ).fetchall()
            if not rows:
                raise PermissionError("Drive event run is stale")
            for row in rows:
                if success:
                    db.execute(
                        """UPDATE drive_notification_events SET status='succeeded',finished_at=?,
                            result=?,run_id=NULL,locked_by=NULL,lock_expires_at=NULL WHERE id=?""",
                        (now.isoformat(), json.dumps(result), row["id"]),
                    )
                elif row["attempts"] >= MAX_DRIVE_EVENT_ATTEMPTS:
                    db.execute(
                        """UPDATE drive_notification_events SET status='exhausted',finished_at=?,
                            result=?,run_id=NULL,locked_by=NULL,lock_expires_at=NULL WHERE id=?""",
                        (now.isoformat(), json.dumps(result), row["id"]),
                    )
                else:
                    delay = min(3600, 60 * (2 ** min(row["attempts"] - 1, 6)))
                    db.execute(
                        """UPDATE drive_notification_events SET status='failed',next_attempt_at=?,
                            result=?,run_id=NULL,locked_by=NULL,lock_expires_at=NULL WHERE id=?""",
                        (
                            (now + timedelta(seconds=delay)).isoformat(),
                            json.dumps(result),
                            row["id"],
                        ),
                    )
        return len(rows)

    def due_drive_renewal(self, file_id: str, overlap_seconds: int = 600) -> bool:
        threshold = (utcnow() + timedelta(seconds=overlap_seconds)).isoformat()
        with self._connect() as db:
            row = db.execute(
                """SELECT 1 FROM drive_watch_channels WHERE file_id=? AND state='active'
                    AND resource_id IS NOT NULL AND expiration_at>? LIMIT 1""",
                (file_id, threshold),
            ).fetchone()
        return row is None

    def drive_renewal_retry_due(self, file_id: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT renewal_next_attempt_at FROM drive_watch_maintenance WHERE file_id=?",
                (file_id,),
            ).fetchone()
        return (
            row is None
            or row["renewal_next_attempt_at"] is None
            or row["renewal_next_attempt_at"] <= iso()
        )

    def record_drive_renewal_result(
        self, file_id: str, *, success: bool, error_type: str = ""
    ) -> None:
        now = utcnow()
        with self._transaction() as db:
            row = db.execute(
                "SELECT renewal_attempts FROM drive_watch_maintenance WHERE file_id=?",
                (file_id,),
            ).fetchone()
            attempts = 0 if success else (int(row["renewal_attempts"]) if row else 0) + 1
            delay = min(3600, 60 * (2 ** min(max(attempts - 1, 0), 6)))
            next_attempt = None if success else (now + timedelta(seconds=delay)).isoformat()
            db.execute(
                """INSERT INTO drive_watch_maintenance(
                    file_id,renewal_attempts,renewal_next_attempt_at,last_status,last_error_type,
                    updated_at
                ) VALUES(?,?,?,?,?,?) ON CONFLICT(file_id) DO UPDATE SET
                    renewal_attempts=excluded.renewal_attempts,
                    renewal_next_attempt_at=excluded.renewal_next_attempt_at,
                    last_status=excluded.last_status,last_error_type=excluded.last_error_type,
                    updated_at=excluded.updated_at""",
                (
                    file_id,
                    attempts,
                    next_attempt,
                    "succeeded" if success else "failed",
                    "" if success else error_type[:120],
                    now.isoformat(),
                ),
            )

    def claim_sheet_sync_lease(
        self, source_id: str, owner: str, run_id: str, lease_seconds: int = SHEET_SYNC_LEASE_SECONDS
    ) -> bool:
        now = utcnow()
        with self._transaction() as db:
            row = db.execute(
                "SELECT lock_expires_at FROM sheet_sync_leases WHERE source_id=?", (source_id,)
            ).fetchone()
            if row is not None and row["lock_expires_at"] > now.isoformat():
                return False
            db.execute(
                """INSERT INTO sheet_sync_leases(
                    source_id,run_id,locked_by,lock_expires_at,created_at,updated_at
                ) VALUES(?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET
                    run_id=excluded.run_id,locked_by=excluded.locked_by,
                    lock_expires_at=excluded.lock_expires_at,updated_at=excluded.updated_at""",
                (
                    source_id,
                    run_id,
                    owner,
                    (
                        now + timedelta(seconds=max(SHEET_SYNC_LEASE_SECONDS, lease_seconds))
                    ).isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
        return True

    def assert_sheet_sync_lease(self, source_id: str, owner: str, run_id: str) -> None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM sheet_sync_leases WHERE source_id=?", (source_id,)
            ).fetchone()
        if (
            row is None
            or row["locked_by"] != owner
            or row["run_id"] != run_id
            or row["lock_expires_at"] <= iso()
        ):
            raise LeaseExpiredError("Sheet sync lease is stale")

    def release_sheet_sync_lease(self, source_id: str, owner: str, run_id: str) -> None:
        with self._transaction() as db:
            db.execute(
                """DELETE FROM sheet_sync_leases WHERE source_id=? AND locked_by=? AND run_id=?""",
                (source_id, owner, run_id),
            )

    def prune_drive_history(
        self, retention_days: int = 30, keep_channels: int = 100
    ) -> dict[str, int]:
        """Bound terminal webhook history while retaining active/retriable state."""
        cutoff = (utcnow() - timedelta(days=max(1, retention_days))).isoformat()
        with self._transaction() as db:
            events = db.execute(
                """DELETE FROM drive_notification_events WHERE status IN
                    ('succeeded','ignored','exhausted') AND finished_at<?""",
                (cutoff,),
            ).rowcount
            stale_ids = db.execute(
                """SELECT channel_id FROM drive_watch_channels WHERE state IN
                    ('stopped','replaced','expired') AND cleanup_status IN ('none','succeeded','exhausted')
                    AND updated_at<? ORDER BY updated_at DESC LIMIT -1 OFFSET ?""",
                (cutoff, max(1, keep_channels)),
            ).fetchall()
            channels = 0
            if stale_ids:
                ids = [row["channel_id"] for row in stale_ids]
                marks = ",".join("?" for _ in ids)
                db.execute(
                    f"DELETE FROM drive_notification_events WHERE channel_id IN ({marks})", ids
                )
                channels = db.execute(
                    f"DELETE FROM drive_watch_channels WHERE channel_id IN ({marks})", ids
                ).rowcount
        return {"events": events, "channels": channels}
