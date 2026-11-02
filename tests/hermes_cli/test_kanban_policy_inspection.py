"""Policy diagnostics and previews must not mutate or depend on board health."""
import contextlib
from pathlib import Path
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli import test_kanban_board_verification_policy as policy_tests

board = policy_tests.board
_cli = policy_tests._cli


def test_corrupt_board_diagnostic_does_not_break_config_load(board, monkeypatch, tmp_path, caplog):
    from hermes_cli.config import load_config

    path = tmp_path / "corrupt.db"
    path.write_bytes(b"not a SQLite database")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(path))
    assert isinstance(load_config(), dict)
    assert "Unable to inspect" in caplog.text
    assert path.read_bytes() == b"not a SQLite database"


def test_reassignment_preview_does_not_upgrade_legacy_board(board, monkeypatch):
    conn, home = board
    tid = kb.create_task(conn, title="Retired owner", assignee="builder")
    (home / "profiles" / "builder" / "profile.yaml").write_text("kanban_dispatch: false\n")
    (home / "profiles" / "builder" / "config.yaml").write_text("kanban:\n  default_assignee: alternate\n")
    conn.execute("DROP TABLE board_config")
    path = kb.kanban_db_path()
    source = conn.execute("PRAGMA database_list").fetchone()[2]
    conn.close()
    monkeypatch.setenv("HERMES_KANBAN_DB", source)
    before = {str(p): p.read_bytes() for p in home.rglob("*") if p.is_file()}
    db = Path(source)
    before_db = {p.name: p.read_bytes() for p in db.parent.iterdir() if p.is_file()}
    assert _cli(["reassign-ineligible", "--dry-run"]) == 0
    assert {str(p): p.read_bytes() for p in home.rglob("*") if p.is_file()} == before
    assert {p.name: p.read_bytes() for p in db.parent.iterdir() if p.is_file()} == before_db
    with contextlib.closing(sqlite3.connect(db)) as reader:
        assert reader.execute("SELECT assignee FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "builder"
        assert reader.execute("SELECT 1 FROM sqlite_master WHERE name='board_config'").fetchone() is None
    assert not path.exists()


@pytest.mark.parametrize("argv", [
    ["config", "set", "verify_profile", "alternate"],
    ["reassign-ineligible"],
    ["migrate-verification", "--boards", "default", "--verify-profile", "alternate", "--apply"],
])
def test_policy_commands_preserve_delegated_child_mutation_fence(board, monkeypatch, capsys, argv):
    conn, _home = board
    monkeypatch.setenv("HERMES_KANBAN_DB", conn.execute("PRAGMA database_list").fetchone()[2])
    monkeypatch.setenv("HERMES_DELEGATED_CHILD_CONTEXT", "1")
    assert _cli(argv) != 0
    assert "delegate_task" in capsys.readouterr().err
