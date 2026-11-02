"""Board-owned verification, frozen claims, and explicit policy migration."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    for name in ("builder", "reviewer", "alternate"):
        profile = home / "profiles" / name
        profile.mkdir(parents=True)
        (profile / "profile.yaml").write_text("kanban_dispatch: true\n")
        (profile / "config.yaml").write_text("{}\n")
    (home / "config.yaml").write_text("kanban:\n  verify_profile: reviewer\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home / "profiles" / "builder"))
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD",
                "HERMES_KANBAN_HOME", "HERMES_KANBAN_WORKSPACES_ROOT"):
        monkeypatch.delenv(key, raising=False)
    with kbc.connect_closing(tmp_path / "board.db") as conn:
        yield conn, home


def test_named_owner_does_not_inherit_root_verifier(board):
    conn, _home = board
    tid = kb.create_task(conn, title="Owned implementation", assignee="builder")
    run = kb.claim_task(conn, tid)
    assert kb.complete_task(conn, tid, summary="Tests pass", expected_run_id=run.current_run_id)
    assert kb.get_task(conn, tid).status == "done"


@pytest.mark.parametrize("launch,owner_policy,expected", [
    ("", "", "done"), ("alternate", "reviewer", "review"),
])
def test_claim_uses_explicit_run_owner_not_launch_profile(board, monkeypatch, launch, owner_policy, expected):
    conn, home = board
    (home / "profiles" / "builder" / "config.yaml").write_text(f"kanban:\n  verify_profile: '{owner_policy}'\n")
    monkeypatch.setenv("HERMES_HOME", str(home / "profiles" / launch if launch else home))
    tid = kb.create_task(conn, title="Dispatcher-owned claim", assignee="builder")
    run = kb.claim_task(conn, tid)
    assert kb.complete_task(conn, tid, summary="Tested", expected_run_id=run.current_run_id)
    assert kb.get_task(conn, tid).status == expected


def test_unclaimed_named_completion_does_not_inherit_launch_policy(board, monkeypatch):
    conn, home = board
    monkeypatch.setenv("HERMES_HOME", str(home))
    tid = kb.create_task(conn, title="Operator completion", assignee="builder")
    assert kb.complete_task(conn, tid, summary="Operator checked it")
    assert kb.get_task(conn, tid).status == "done"


def _reject(conn, tid, *, manual=False):
    run = kb.claim_task(conn, tid)
    assert run is not None
    if manual:
        assert kb.request_review(conn, tid, summary="Review this", reviewer="reviewer", expected_run_id=run.current_run_id)
    else:
        assert kb.complete_task(conn, tid, summary="Tested", expected_run_id=run.current_run_id)
    review = kb.claim_review_task(conn, tid)
    assert review is not None
    assert kb.request_changes(conn, tid, reason="Add a regression", expected_run_id=review.current_run_id)[0]


def test_manual_history_never_trips_automatic_sequence_breaker(board):
    from hermes_cli.kanban_dispatch_policy import set_board_verifier
    conn, _home = board
    tid = kb.create_task(conn, title="Manual then automatic", assignee="builder")
    for _ in range(4):
        _reject(conn, tid, manual=True)
        assert kb.get_task(conn, tid).status == "ready"
    set_board_verifier(conn, "reviewer")
    _reject(conn, tid)
    assert kb.get_task(conn, tid).status == "ready"


def test_human_unblock_starts_fresh_automatic_revision_sequence(board):
    from hermes_cli.kanban_dispatch_policy import set_board_verifier
    conn, _home = board
    set_board_verifier(conn, "reviewer")
    tid = kb.create_task(conn, title="Bounded revision", assignee="builder")
    for _ in range(3):
        _reject(conn, tid)
    assert kb.get_task(conn, tid).status == "blocked"
    assert kb.unblock_task(conn, tid)
    _reject(conn, tid)
    assert kb.get_task(conn, tid).status == "ready"


def test_warning_on_config_load_and_dispatch_without_board_override(board, monkeypatch, caplog):
    from hermes_cli.config import load_config
    from hermes_cli import kanban_db_dispatch as dispatch
    conn, _home = board
    monkeypatch.setenv("HERMES_KANBAN_DB", conn.execute("PRAGMA database_list").fetchone()[2])
    load_config()
    assert "does not inherit" in caplog.text
    caplog.clear()
    dispatch.dispatch_once(conn, dry_run=True)
    assert "does not inherit" in caplog.text


@pytest.mark.parametrize("root_config", ["[]\n", "kanban: invalid\n", "kanban: [invalid\n"])
def test_root_diagnostic_never_breaks_named_config_load(board, root_config):
    from hermes_cli.config import load_config
    _conn, home = board
    (home / "config.yaml").write_text(root_config)
    assert isinstance(load_config(), dict)


def test_explicit_migration_previews_readonly_and_preserves_active_claims(board, monkeypatch, capsys):
    from hermes_cli.kanban_dispatch_policy import board_verifier
    _conn, _home = board
    kb.create_board("selected")
    kb.create_board("untouched")
    with kbc.connect_closing(board="selected") as selected:
        tid = kb.create_task(selected, title="Active", assignee="builder")
        run = kb.claim_task(selected, tid)
        before = {table: [tuple(r) for r in selected.execute(f"SELECT * FROM {table}")]
                  for table in ("tasks", "task_runs", "task_events")}
    db_path = kb.kanban_db_path(board="selected")
    before_files = {p.name: p.read_bytes() for p in db_path.parent.iterdir() if p.is_file()}
    assert _cli(["migrate-verification", "--boards", "selected", "--verify-profile", "reviewer", "--dry-run"]) == 0
    assert {p.name: p.read_bytes() for p in db_path.parent.iterdir() if p.is_file()} == before_files
    assert "reviewer" in capsys.readouterr().out
    assert _cli(["migrate-verification", "--boards", "selected", "--verify-profile", "reviewer", "--apply"]) == 0
    with kbc.connect_closing(board="selected") as selected:
        assert board_verifier(selected) == "reviewer"
        assert {table: [tuple(r) for r in selected.execute(f"SELECT * FROM {table}")] for table in before} == before
        assert kb.complete_task(selected, tid, summary="Old snapshot", expected_run_id=run.current_run_id)
        assert kb.get_task(selected, tid).status == "done"
        new = kb.create_task(selected, title="Next claim", assignee="builder")
        claimed = kb.claim_task(selected, new)
        assert kb.complete_task(selected, new, summary="New policy", expected_run_id=claimed.current_run_id)
        assert kb.get_task(selected, new).status == "review"
    with kbc.connect_closing(board="untouched") as other:
        assert board_verifier(other) is None


def test_blocked_review_migration_keeps_reviewer_role_and_pr_credentials(board, monkeypatch):
    from hermes_cli import kanban_dispatch_policy as policy, kanban_pr_acceptance_store as store
    conn, home = board
    (home / "profiles" / "builder" / "config.yaml").write_text("kanban:\n  default_assignee: builder\n")
    policy.set_board_verifier(conn, "reviewer")
    tid = kb.create_task(conn, title="Published work", assignee="builder", completion_contract="org/project")
    owners = []
    def collect(_contract, _pr, *, assignee):
        owners.append(assignee)
        return {"ok": True}
    monkeypatch.setattr(store, "collect_acceptance", collect)
    run = kb.claim_task(conn, tid)
    assert kb.complete_task(conn, tid, summary="Published", expected_run_id=run.current_run_id,
                            metadata={"published_pr": "https://github.com/org/project/pull/1"})
    review = kb.claim_review_task(conn, tid)
    assert kb.block_task(conn, tid, reason="Need access", kind="needs_input", expected_run_id=review.current_run_id)
    (home / "profiles" / "reviewer" / "profile.yaml").write_text("kanban_dispatch: false\n")
    policy.set_board_verifier(conn, "alternate")
    before = [tuple(row) for row in conn.execute("SELECT * FROM task_runs")]
    assert policy.reassign_ineligible(conn)[0]["to"] == "alternate"
    assert [tuple(row) for row in conn.execute("SELECT * FROM task_runs")] == before
    assert kb.complete_task(conn, tid, summary="Human approval")
    assert owners == ["builder", "builder"]
    assert kb.get_task(conn, tid).status == "done"


def _cli(argv):
    import argparse
    from hermes_cli import kanban as cli
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers())
    return cli.kanban_command(parser.parse_args(["kanban", *argv]))


def test_board_policy_cli_overrides_owner_and_empty_disables(board, monkeypatch):
    conn, home = board
    monkeypatch.setenv("HERMES_KANBAN_DB", str(conn.execute("PRAGMA database_list").fetchone()[2]))
    assert _cli(["config", "set", "verify_profile", "alternate"]) == 0
    tid = kb.create_task(conn, title="Board override", assignee="builder")
    run = kb.claim_task(conn, tid)
    assert kb.complete_task(conn, tid, summary="Tested", expected_run_id=run.current_run_id)
    assert (kb.get_task(conn, tid).status, kb.get_task(conn, tid).assignee) == ("review", "alternate")
    (home / "profiles" / "builder" / "config.yaml").write_text("kanban:\n  verify_profile: reviewer\n")
    assert _cli(["config", "set", "verify_profile", ""]) == 0
    tid = kb.create_task(conn, title="Disabled", assignee="builder")
    run = kb.claim_task(conn, tid)
    assert kb.complete_task(conn, tid, summary="Tested", expected_run_id=run.current_run_id)
    assert kb.get_task(conn, tid).status == "done"


@pytest.mark.parametrize("initial,changed", [("reviewer", "alternate"), ("", "reviewer")])
def test_claim_freezes_policy_across_worker_scope_and_config_edits(board, monkeypatch, initial, changed):
    from hermes_cli.kanban_dispatch_policy import set_board_verifier
    conn, home = board
    set_board_verifier(conn, initial)
    tid = kb.create_task(conn, title="Frozen implementation", assignee="builder")
    run = kb.claim_task(conn, tid)
    set_board_verifier(conn, changed)
    (home / "profiles" / "builder" / "config.yaml").write_text("kanban:\n  verify_profile: alternate\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert kb.complete_task(conn, tid, summary="Tested", expected_run_id=run.current_run_id)
    task = kb.get_task(conn, tid)
    assert (task.status, task.assignee) == (("review", initial) if initial else ("done", "builder"))
    if initial:
        event = next(e for e in kb.list_events(conn, tid) if e.kind == "review_requested")
        assert event.payload["automatic_verification"] is True


@pytest.mark.parametrize("owner_policy,expected", [("", "done"), ("reviewer", "review")])
def test_legacy_claim_falls_back_to_run_owner_not_root_or_board(board, monkeypatch, owner_policy, expected):
    from hermes_cli.kanban_dispatch_policy import set_board_verifier
    conn, home = board
    tid = kb.create_task(conn, title="Legacy implementation", assignee="builder")
    run = kb.claim_task(conn, tid)
    conn.execute("UPDATE task_events SET payload='{}' WHERE task_id=? AND kind='claimed'", (tid,))
    (home / "profiles" / "builder" / "config.yaml").write_text(f"kanban:\n  verify_profile: '{owner_policy}'\n")
    set_board_verifier(conn, "alternate")
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert kb.complete_task(conn, tid, summary="Tested", expected_run_id=run.current_run_id)
    assert kb.get_task(conn, tid).status == expected


def test_selected_board_config_roundtrip_and_unset_owner_fallback(board, capsys):
    from hermes_cli.kanban_dispatch_policy import board_verifier
    _conn, home = board
    (home / "profiles" / "builder" / "config.yaml").write_text("kanban:\n  verify_profile: reviewer\n")
    kb.create_board("project")
    prefix = ["--board", "project", "config"]
    assert _cli([*prefix, "set", "verify_profile", "alternate"]) == 0
    assert _cli([*prefix, "get", "verify_profile"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "alternate"
    assert _cli([*prefix, "unset", "verify_profile"]) == 0
    with kbc.connect_closing(board="project") as selected:
        assert board_verifier(selected) is None
        tid = kb.create_task(selected, title="Owner fallback", assignee="builder")
        run = kb.claim_task(selected, tid)
        assert kb.complete_task(selected, tid, summary="Tested", expected_run_id=run.current_run_id)
        assert kb.get_task(selected, tid).assignee == "reviewer"
    assert board_verifier(_conn) is None


def test_default_migration_preview_never_initializes_legacy_schema_or_missing_board(board):
    _conn, _home = board
    kb.create_board("legacy")
    with kbc.connect_closing(board="legacy") as legacy:
        legacy.execute("DROP TABLE board_config")
    path = kb.kanban_db_path(board="legacy")
    before = {p.name: p.read_bytes() for p in path.parent.iterdir() if p.is_file()}
    assert _cli(["migrate-verification", "--boards", "legacy", "--verify-profile", "reviewer"]) == 0
    assert {p.name: p.read_bytes() for p in path.parent.iterdir() if p.is_file()} == before
    missing = kb.kanban_db_path(board="absent")
    assert _cli(["migrate-verification", "--boards", "absent", "--verify-profile", "reviewer", "--apply"]) != 0
    assert not missing.exists()


def test_policy_preview_never_writes_profile_backups(board):
    _conn, home = board
    kb.create_board("preview")
    (home / "profiles" / "builder" / "config.yaml").write_text("kanban:\n  dispatch_profiles: [builder, reviewer]\n")
    for backup in home.rglob("*.good.*"):
        backup.unlink()
    before = {str(p.relative_to(home)): p.read_bytes() for p in home.rglob("*") if p.is_file()}
    assert _cli(["migrate-verification", "--boards", "preview", "--verify-profile", "reviewer"]) == 0
    assert {str(p.relative_to(home)): p.read_bytes() for p in home.rglob("*") if p.is_file()} == before
    from hermes_cli.config_effective import load_user_config_effective
    load_user_config_effective(fail_closed=True)
    assert list((home / "profiles" / "builder").rglob("*.good.*"))


def test_migration_preview_reads_live_wal_without_touching_source_files(board, capsys):
    import subprocess
    import sys
    _conn, _home = board
    kb.create_board("wal-board")
    path = kb.kanban_db_path(board="wal-board")
    code = """
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute('PRAGMA journal_mode=WAL')
conn.execute("INSERT INTO board_config VALUES ('verify_profile', 'alternate')")
conn.commit()
print('ready', flush=True)
sys.stdin.readline()
conn.close()
"""
    with subprocess.Popen([sys.executable, "-c", code, str(path)], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as writer:
        try:
            assert writer.stdout.readline().strip() == "ready"
            assert path.with_name(path.name + "-wal").is_file()
            before = {p.name: p.read_bytes() for p in path.parent.iterdir() if p.is_file()}
            assert _cli(["migrate-verification", "--boards", "wal-board", "--verify-profile", "reviewer"]) == 0
            assert "'alternate' -> 'reviewer' (preview)" in capsys.readouterr().out
            assert {p.name: p.read_bytes() for p in path.parent.iterdir() if p.is_file()} == before
        finally:
            writer.communicate("close\n", timeout=10)
        assert writer.returncode == 0


def test_frozen_handoff_survives_verifier_optout_and_warning_stops_for_override(board, monkeypatch, caplog):
    from hermes_cli.config import load_config
    from hermes_cli.kanban_dispatch_policy import set_board_verifier
    from hermes_cli import kanban_db_dispatch as dispatch
    conn, home = board
    monkeypatch.setenv("HERMES_KANBAN_DB", conn.execute("PRAGMA database_list").fetchone()[2])
    set_board_verifier(conn, "reviewer")
    tid = kb.create_task(conn, title="Frozen eligible choice", assignee="builder")
    run = kb.claim_task(conn, tid)
    (home / "profiles" / "reviewer" / "profile.yaml").write_text("kanban_dispatch: false\n")
    assert kb.complete_task(conn, tid, summary="Tested", expected_run_id=run.current_run_id)
    assert kb.get_task(conn, tid).assignee == "reviewer"
    set_board_verifier(conn, "")
    caplog.clear()
    load_config()
    dispatch.dispatch_once(conn, dry_run=True)
    assert "does not inherit" not in caplog.text
