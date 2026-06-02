"""Import repo-local ``Kanban.md`` files into Hermes Kanban boards.

This module is intentionally one-way: markdown is the project-management
source of truth, and the SQLite board is a synced execution surface.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import quote

from hermes_cli import kanban_db as kb


LANE_HEADINGS: dict[str, str] = {
    "triage": "triage",
    "todo": "todo",
    "scheduled": "scheduled",
    "ready": "ready",
    "running": "running",
    "blocked": "blocked",
    "review": "review",
    "done": "done",
}

_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
_CHECKBOX_RE = re.compile(r"^(?P<indent>\s*)[-*+]\s+\[(?P<mark>[ xX])\]\s+(?P<body>.+?)\s*$")
_HIDDEN_ID_RE = re.compile(r"<!--\s*hkb:([A-Za-z0-9_.:-]+)\s*-->")
_META_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)\s*:\s*(.*)$")
_SAFE_TASK_ID_RE = re.compile(r"^t_[A-Za-z0-9][A-Za-z0-9_.:-]{0,126}$")
_BODY_HEADER = "Imported from repo Kanban.md metadata:"


@dataclass
class MarkdownKanbanCard:
    """A checkbox card parsed from a recognized markdown lane."""

    title: str
    status: str
    hidden_id: Optional[str]
    metadata: dict[str, str | list[str]]
    line_number: int
    checked: bool = False


@dataclass
class SyncOperation:
    action: str
    line_number: int
    task_id: Optional[str]
    title: str
    status: str
    fields: list[str] = field(default_factory=list)
    message: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "line_number": self.line_number,
            "task_id": self.task_id,
            "title": self.title,
            "status": self.status,
            "fields": list(self.fields),
            "message": self.message,
        }


@dataclass
class SyncResult:
    board: str
    kanban_path: str
    dry_run: bool
    operations: list[SyncOperation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def created(self) -> int:
        return sum(1 for op in self.operations if op.action == "create")

    @property
    def updated(self) -> int:
        return sum(1 for op in self.operations if op.action == "update")

    @property
    def unchanged(self) -> int:
        return sum(1 for op in self.operations if op.action == "unchanged")

    @property
    def skipped(self) -> int:
        return sum(1 for op in self.operations if op.action == "skip")

    def as_dict(self) -> dict[str, Any]:
        return {
            "board": self.board,
            "kanban_path": self.kanban_path,
            "dry_run": self.dry_run,
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
            "warnings": list(self.warnings),
            "operations": [op.as_dict() for op in self.operations],
        }


def parse_markdown_kanban_text(text: str) -> list[MarkdownKanbanCard]:
    """Parse checkbox cards under recognized ``##`` Kanban headings."""

    cards: list[MarkdownKanbanCard] = []
    current_status: Optional[str] = None
    current_card: Optional[MarkdownKanbanCard] = None
    current_meta_key: Optional[str] = None

    for line_number, line in enumerate(text.splitlines(), start=1):
        heading = _HEADING_RE.match(line.strip())
        if heading:
            current_status = LANE_HEADINGS.get(heading.group(1).strip().casefold())
            current_card = None
            current_meta_key = None
            continue

        if current_status is None:
            continue

        checkbox = _CHECKBOX_RE.match(line)
        if checkbox:
            body = checkbox.group("body").strip()
            hidden = _extract_hidden_id(body)
            title = _HIDDEN_ID_RE.sub("", body).strip()
            current_card = MarkdownKanbanCard(
                title=title,
                status=current_status,
                hidden_id=hidden,
                metadata={},
                line_number=line_number,
                checked=checkbox.group("mark").lower() == "x",
            )
            cards.append(current_card)
            current_meta_key = None
            continue

        if current_card is None:
            continue
        if not line.startswith((" ", "\t")) or not line.strip():
            current_meta_key = None
            continue

        meta_text = _strip_list_marker(line.strip())
        if not meta_text:
            continue

        meta = _META_RE.match(meta_text)
        if meta:
            key = meta.group(1).casefold().replace("-", "_")
            value = meta.group(2).strip()
            if value:
                current_card.metadata[key] = value
            else:
                current_card.metadata[key] = []
            current_meta_key = key
            continue

        if current_meta_key:
            existing = current_card.metadata.get(current_meta_key)
            if isinstance(existing, list):
                existing.append(meta_text)
            elif isinstance(existing, str):
                current_card.metadata[current_meta_key] = (
                    existing + "\n" + meta_text
                )

    return cards


def parse_markdown_kanban_file(path: str | os.PathLike[str]) -> list[MarkdownKanbanCard]:
    """Read and parse a repo-local ``Kanban.md`` file."""

    kanban_path = Path(path).expanduser()
    return parse_markdown_kanban_text(kanban_path.read_text(encoding="utf-8"))


def sync_markdown_file(
    path: str | os.PathLike[str],
    *,
    board: Optional[str] = None,
    project_root: Optional[str | os.PathLike[str]] = None,
    dry_run: bool = False,
) -> SyncResult:
    """Import parsed markdown cards into a Hermes Kanban board.

    Cards without hidden ``hkb`` IDs are always skipped in write mode. In
    dry-run mode they are reported as skipped so the markdown owner can add a
    stable ID before syncing.
    """

    kanban_path = Path(path).expanduser()
    cards = parse_markdown_kanban_file(kanban_path)
    board_slug = kb._normalize_board_slug(board) or kb.get_current_board()
    result = SyncResult(
        board=board_slug,
        kanban_path=str(kanban_path),
        dry_run=dry_run,
    )

    import_ids = [card.hidden_id for card in cards if card.hidden_id]

    if dry_run:
        with _readonly_existing_connection(board_slug) as conn:
            existing = _load_existing_tasks(conn, import_ids)
            _plan_cards(
                result,
                cards,
                existing=existing,
                project_root=project_root,
                apply=False,
            )
        return result

    with kb.connect_closing(board=board_slug) as conn:
        existing = _load_existing_tasks(conn, import_ids)
        _plan_cards(
            result,
            cards,
            existing=existing,
            project_root=project_root,
            apply=True,
            conn=conn,
        )
    return result


def _extract_hidden_id(text: str) -> Optional[str]:
    match = _HIDDEN_ID_RE.search(text)
    return match.group(1).strip() if match else None


def _strip_list_marker(text: str) -> str:
    if len(text) >= 2 and text[0] in "-*+" and text[1].isspace():
        return text[2:].strip()
    return text


@contextlib.contextmanager
def _readonly_existing_connection(board: str):
    """Open an existing board DB read-only, or yield None if it is absent."""

    db_path = kb.kanban_db_path(board=board)
    if not db_path.exists():
        yield None
        return

    resolved = str(db_path.resolve()).replace("\\", "/")
    uri = "file:" + quote(resolved, safe="/:") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _load_existing_tasks(
    conn: Optional[sqlite3.Connection],
    import_ids: Iterable[Optional[str]],
) -> dict[str, kb.Task]:
    ids = sorted({str(task_id) for task_id in import_ids if task_id})
    if not conn or not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    try:
        rows = conn.execute(
            f"""
            SELECT * FROM tasks
             WHERE id IN ({placeholders})
                OR idempotency_key IN ({placeholders})
            """,
            tuple(ids + ids),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}

    by_import_id: dict[str, kb.Task] = {}
    for row in rows:
        task = kb.Task.from_row(row)
        if task.id in ids:
            by_import_id[task.id] = task
        if task.idempotency_key in ids and task.idempotency_key not in by_import_id:
            by_import_id[str(task.idempotency_key)] = task
    return by_import_id


def _plan_cards(
    result: SyncResult,
    cards: Iterable[MarkdownKanbanCard],
    *,
    existing: dict[str, kb.Task],
    project_root: Optional[str | os.PathLike[str]],
    apply: bool,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    for card in cards:
        if not card.hidden_id:
            _skip_card(
                result,
                card,
                "missing hidden hkb id; add a comment like <!-- hkb:t_project_task -->",
            )
            continue
        if not _SAFE_TASK_ID_RE.match(card.hidden_id):
            _skip_card(
                result,
                card,
                f"unsafe hidden hkb id {card.hidden_id!r}; expected a t_... task id",
            )
            continue

        fields = _fields_for_card(result, card, project_root=project_root)
        task = existing.get(card.hidden_id)
        if task is None:
            result.operations.append(
                SyncOperation(
                    action="create",
                    line_number=card.line_number,
                    task_id=card.hidden_id,
                    title=card.title,
                    status=card.status,
                    fields=sorted(fields.keys()),
                )
            )
            if apply:
                if conn is None:
                    raise RuntimeError("sync apply requires a database connection")
                _insert_card(conn, card.hidden_id, fields)
                existing[card.hidden_id] = kb.get_task(conn, card.hidden_id)  # type: ignore[assignment]
            continue

        changed = _changed_fields(task, fields)
        if not changed:
            result.operations.append(
                SyncOperation(
                    action="unchanged",
                    line_number=card.line_number,
                    task_id=task.id,
                    title=card.title,
                    status=card.status,
                )
            )
            continue

        result.operations.append(
            SyncOperation(
                action="update",
                line_number=card.line_number,
                task_id=task.id,
                title=card.title,
                status=card.status,
                fields=changed,
            )
        )
        if apply:
            if conn is None:
                raise RuntimeError("sync apply requires a database connection")
            _update_card(conn, task, fields, changed)
            existing[card.hidden_id] = kb.get_task(conn, task.id)  # type: ignore[assignment]


def _skip_card(result: SyncResult, card: MarkdownKanbanCard, message: str) -> None:
    warning = f"line {card.line_number}: {message} ({card.title!r})"
    result.warnings.append(warning)
    result.operations.append(
        SyncOperation(
            action="skip",
            line_number=card.line_number,
            task_id=card.hidden_id,
            title=card.title,
            status=card.status,
            message=message,
        )
    )


def _fields_for_card(
    result: SyncResult,
    card: MarkdownKanbanCard,
    *,
    project_root: Optional[str | os.PathLike[str]],
) -> dict[str, Any]:
    workspace_kind, workspace_path = _workspace_for_card(
        result,
        card,
        project_root=project_root,
    )
    return {
        "title": card.title.strip(),
        "body": _body_from_metadata(card.metadata),
        "assignee": _assignee_from_metadata(card.metadata),
        "status": card.status,
        "priority": _priority_from_metadata(result, card),
        "workspace_kind": workspace_kind,
        "workspace_path": workspace_path,
        "idempotency_key": card.hidden_id,
    }


def _assignee_from_metadata(metadata: dict[str, str | list[str]]) -> Optional[str]:
    raw = metadata.get("assignee")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return kb._canonical_assignee(raw.strip())


def _priority_from_metadata(result: SyncResult, card: MarkdownKanbanCard) -> int:
    raw = card.metadata.get("priority")
    if raw is None:
        return 0
    if isinstance(raw, list):
        raw = " ".join(raw)
    try:
        return int(str(raw).strip())
    except ValueError:
        result.warnings.append(
            f"line {card.line_number}: ignored non-integer priority {raw!r}"
        )
        return 0


def _workspace_for_card(
    result: SyncResult,
    card: MarkdownKanbanCard,
    *,
    project_root: Optional[str | os.PathLike[str]],
) -> tuple[str, Optional[str]]:
    if project_root is not None:
        return "dir", os.path.expanduser(str(project_root))

    raw = card.metadata.get("workspace")
    if not isinstance(raw, str) or not raw.strip():
        return "scratch", None
    value = raw.strip()
    if value == "scratch":
        return "scratch", None
    if value == "worktree":
        return "worktree", None
    for prefix, kind in (("dir:", "dir"), ("worktree:", "worktree")):
        if value.startswith(prefix):
            path = value[len(prefix):].strip()
            if path:
                return kind, os.path.expanduser(path)
    result.warnings.append(
        f"line {card.line_number}: ignored unsupported workspace {value!r}"
    )
    return "scratch", None


def _body_from_metadata(metadata: dict[str, str | list[str]]) -> Optional[str]:
    if not metadata:
        return None
    lines = [_BODY_HEADER]
    for key, value in metadata.items():
        if isinstance(value, list):
            lines.append(f"- {key}:")
            for item in value:
                lines.append(f"  - {item}")
        else:
            lines.append(f"- {key}: {value}")
    return "\n".join(lines)


def _changed_fields(task: kb.Task, fields: dict[str, Any]) -> list[str]:
    changed: list[str] = []
    for name, value in fields.items():
        if name == "body" and value is None:
            if task.body and task.body.startswith(_BODY_HEADER):
                changed.append(name)
            continue
        if getattr(task, name) != value:
            changed.append(name)
    return changed


def _insert_card(
    conn: sqlite3.Connection,
    task_id: str,
    fields: dict[str, Any],
) -> None:
    if fields["status"] not in kb.VALID_STATUSES - {"archived"}:
        raise ValueError(f"unsupported Kanban status {fields['status']!r}")
    if fields["workspace_kind"] not in kb.VALID_WORKSPACE_KINDS:
        raise ValueError(f"unsupported workspace kind {fields['workspace_kind']!r}")
    now = int(time.time())
    started_at = now if fields["status"] == "running" else None
    completed_at = now if fields["status"] == "done" else None

    with kb.write_txn(conn):
        conn.execute(
            """
            INSERT INTO tasks (
                id, title, body, assignee, status, priority,
                created_by, created_at, started_at, completed_at,
                workspace_kind, workspace_path, idempotency_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                fields["title"],
                fields["body"],
                fields["assignee"],
                fields["status"],
                int(fields["priority"]),
                "kanban-markdown-sync",
                now,
                started_at,
                completed_at,
                fields["workspace_kind"],
                fields["workspace_path"],
                fields["idempotency_key"],
            ),
        )
        kb._append_event(
            conn,
            task_id,
            "created",
            {
                "source": "markdown_sync",
                "assignee": fields["assignee"],
                "status": fields["status"],
                "parents": [],
                "tenant": None,
            },
        )


def _update_card(
    conn: sqlite3.Connection,
    task: kb.Task,
    fields: dict[str, Any],
    changed: list[str],
) -> None:
    now = int(time.time())
    status = fields["status"]
    if status not in kb.VALID_STATUSES - {"archived"}:
        raise ValueError(f"unsupported Kanban status {status!r}")
    if fields["workspace_kind"] not in kb.VALID_WORKSPACE_KINDS:
        raise ValueError(f"unsupported workspace kind {fields['workspace_kind']!r}")

    body = fields["body"]
    if body is None and task.body and not task.body.startswith(_BODY_HEADER):
        body = task.body

    completed_at = task.completed_at
    if status == "done" and task.status != "done":
        completed_at = now
    elif status != "done":
        completed_at = None

    started_at = task.started_at
    if status == "running" and started_at is None:
        started_at = now

    clear_claim = status != "running"
    with kb.write_txn(conn):
        run_id = None
        if task.current_run_id and status != "running":
            run_id = kb._end_run(
                conn,
                task.id,
                outcome="reclaimed",
                status="reclaimed",
                summary=f"status changed to {status} (markdown sync)",
            )
        conn.execute(
            """
            UPDATE tasks
               SET title = ?,
                   body = ?,
                   assignee = ?,
                   status = ?,
                   priority = ?,
                   started_at = ?,
                   completed_at = ?,
                   workspace_kind = ?,
                   workspace_path = ?,
                   idempotency_key = ?,
                   claim_lock = CASE WHEN ? THEN NULL ELSE claim_lock END,
                   claim_expires = CASE WHEN ? THEN NULL ELSE claim_expires END,
                   worker_pid = CASE WHEN ? THEN NULL ELSE worker_pid END
             WHERE id = ?
            """,
            (
                fields["title"],
                body,
                fields["assignee"],
                status,
                int(fields["priority"]),
                started_at,
                completed_at,
                fields["workspace_kind"],
                fields["workspace_path"],
                fields["idempotency_key"],
                1 if clear_claim else 0,
                1 if clear_claim else 0,
                1 if clear_claim else 0,
                task.id,
            ),
        )
        if "status" in changed:
            kb._append_event(
                conn,
                task.id,
                "status",
                {"status": status, "source": "markdown_sync"},
                run_id=run_id,
            )
        if "priority" in changed:
            kb._append_event(
                conn,
                task.id,
                "reprioritized",
                {"priority": int(fields["priority"]), "source": "markdown_sync"},
            )
        edited_fields = [
            name
            for name in changed
            if name not in {"status", "priority", "idempotency_key"}
        ]
        if edited_fields:
            kb._append_event(
                conn,
                task.id,
                "edited",
                {"fields": edited_fields, "source": "markdown_sync"},
            )


def format_sync_result(result: SyncResult) -> str:
    """Return concise human-readable CLI output for a sync result."""

    mode = "dry-run" if result.dry_run else "sync"
    lines = [
        (
            f"Kanban markdown {mode}: board={result.board} "
            f"created={result.created} updated={result.updated} "
            f"unchanged={result.unchanged} skipped={result.skipped}"
        )
    ]
    for op in result.operations:
        ident = op.task_id or "-"
        suffix = f" fields={','.join(op.fields)}" if op.fields else ""
        msg = f" ({op.message})" if op.message else ""
        lines.append(
            f"  {op.action.upper():9s} line {op.line_number:<4d} "
            f"{ident:24s} {op.status:9s} {op.title}{suffix}{msg}"
        )
    if result.warnings:
        lines.append("")
        lines.append("Warnings:")
        lines.extend(f"  - {warning}" for warning in result.warnings)
    return "\n".join(lines)


def result_to_json(result: SyncResult) -> str:
    return json.dumps(result.as_dict(), indent=2, ensure_ascii=False)
