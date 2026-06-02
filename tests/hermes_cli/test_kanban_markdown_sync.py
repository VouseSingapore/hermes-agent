from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_markdown_sync as kms


@pytest.fixture
def tmp_path(request):
    root = Path.cwd() / ".test-tmp"
    root.mkdir(exist_ok=True)
    path = root / f"{request.node.name}-{uuid.uuid4().hex}"
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def scratch_dir(tmp_path):
    return tmp_path


@pytest.fixture
def kanban_home(scratch_dir, monkeypatch):
    home = scratch_dir / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: scratch_dir)
    return home


def test_parser_handles_lanes_ids_and_metadata():
    cards = kms.parse_markdown_kanban_text(
        """# Kanban

## Todo

- [ ] Build sync <!-- hkb:t_repo_kanban_sync_v1 -->
  - assignee: codex
  - priority: 1
  - workspace: dir:C:/repo
  - goal: Keep Markdown canonical.
  - acceptance:
    - Parse lanes.
    - Preserve IDs.

## Review

- [x] Check docs <!-- hkb:t_docs_review -->

## Ideas

- [ ] Ignored card <!-- hkb:t_ignored -->
"""
    )

    assert [card.status for card in cards] == ["todo", "review"]
    first = cards[0]
    assert first.hidden_id == "t_repo_kanban_sync_v1"
    assert first.title == "Build sync"
    assert first.metadata["assignee"] == "codex"
    assert first.metadata["priority"] == "1"
    assert first.metadata["workspace"] == "dir:C:/repo"
    assert first.metadata["goal"] == "Keep Markdown canonical."
    assert first.metadata["acceptance"] == ["Parse lanes.", "Preserve IDs."]
    assert cards[1].checked is True


def test_dry_run_does_not_create_or_mutate_db(kanban_home, scratch_dir):
    kanban_md = scratch_dir / "Kanban.md"
    kanban_md.write_text(
        """# Kanban

## Todo

- [ ] Build sync <!-- hkb:t_repo_kanban_sync_v1 -->
  - assignee: codex
""",
        encoding="utf-8",
    )

    db_path = kanban_home / "kanban.db"
    assert not db_path.exists()

    result = kms.sync_markdown_file(kanban_md, dry_run=True)

    assert result.created == 1
    assert result.updated == 0
    assert not db_path.exists()


def test_cli_dry_run_missing_non_default_board_exits_nonzero(
    kanban_home, scratch_dir
):
    kanban_md = scratch_dir / "Kanban.md"
    kanban_md.write_text(
        """# Kanban

## Todo

- [ ] Build sync <!-- hkb:t_repo_kanban_sync_v1 -->
""",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["HERMES_HOME"] = str(kanban_home)
    env["PYTHONPATH"] = str(Path.cwd())
    env.pop("HERMES_KANBAN_DB", None)
    env.pop("HERMES_KANBAN_HOME", None)
    env.pop("HERMES_KANBAN_BOARD", None)

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "hermes_cli.main",
            "kanban",
            "sync-markdown",
            str(kanban_md),
            "--dry-run",
            "--board",
            "missing-board",
        ],
        cwd=Path.cwd(),
        env=env,
        text=True,
        capture_output=True,
    )

    assert proc.returncode != 0
    assert "board 'missing-board' does not exist" in proc.stderr
    assert not (kanban_home / "kanban" / "boards" / "missing-board").exists()


def test_sync_creates_once_and_updates_by_hidden_id(kanban_home, scratch_dir):
    project_root = scratch_dir / "project"
    project_root.mkdir()
    kanban_md = project_root / "Kanban.md"
    kanban_md.write_text(
        """# Kanban

## Todo

- [ ] Build sync <!-- hkb:t_repo_kanban_sync_v1 -->
  - assignee: Codex
  - priority: 1
  - goal: Initial goal.
""",
        encoding="utf-8",
    )

    first = kms.sync_markdown_file(kanban_md, project_root=project_root)
    second = kms.sync_markdown_file(kanban_md, project_root=project_root)

    assert first.created == 1
    assert second.unchanged == 1

    with kb.connect() as conn:
        tasks = kb.list_tasks(conn)
        task = kb.get_task(conn, "t_repo_kanban_sync_v1")

    assert len(tasks) == 1
    assert task is not None
    assert task.title == "Build sync"
    assert task.status == "todo"
    assert task.assignee == "codex"
    assert task.priority == 1
    assert task.workspace_kind == "dir"
    assert task.workspace_path == str(project_root)
    assert "Initial goal." in (task.body or "")

    kanban_md.write_text(
        """# Kanban

## Blocked

- [ ] Build sync adapter <!-- hkb:t_repo_kanban_sync_v1 -->
  - assignee: codex
  - priority: 3
  - goal: Updated goal.
""",
        encoding="utf-8",
    )

    third = kms.sync_markdown_file(kanban_md, project_root=project_root)

    assert third.updated == 1
    with kb.connect() as conn:
        tasks = kb.list_tasks(conn)
        task = kb.get_task(conn, "t_repo_kanban_sync_v1")

    assert len(tasks) == 1
    assert task is not None
    assert task.title == "Build sync adapter"
    assert task.status == "blocked"
    assert task.priority == 3
    assert "Updated goal." in (task.body or "")


def test_sync_skips_cards_without_hidden_ids(kanban_home, scratch_dir):
    kanban_md = scratch_dir / "Kanban.md"
    kanban_md.write_text(
        """# Kanban

## Todo

- [ ] Missing id
""",
        encoding="utf-8",
    )

    result = kms.sync_markdown_file(kanban_md)

    assert result.created == 0
    assert result.skipped == 1
    assert "missing hidden hkb id" in result.warnings[0]
    with kb.connect() as conn:
        assert kb.list_tasks(conn) == []
