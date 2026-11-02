"""Automatic same-card verification through real dispatch and mutations."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch


@pytest.fixture
def verification_board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    for name in ("implementer", "verifier"):
        profile = home / "profiles" / name
        profile.mkdir(parents=True)
        (profile / "profile.yaml").write_text("kanban_dispatch: true\n")
    (home / "config.yaml").write_text(
        "kanban:\n  verify_profile: verifier\n  review_dispatch: true\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD",
                "HERMES_KANBAN_HOME", "HERMES_KANBAN_WORKSPACES_ROOT"):
        monkeypatch.delenv(key, raising=False)
    with kbc.connect_closing(tmp_path / "kanban.db") as conn:
        from hermes_cli.kanban_dispatch_policy import set_board_verifier
        set_board_verifier(conn, "verifier")
        yield conn, home


def _handoff(conn):
    task_id = kb.create_task(conn, title="Implement feature", assignee="implementer")
    child = kb.create_task(conn, title="Use feature", assignee="implementer", parents=[task_id])
    run = kb.claim_task(conn, task_id)
    assert kb.complete_task(conn, task_id, summary="Feature tested", metadata={"tests_passed": 4},
                            expected_run_id=run.current_run_id)
    assert kb.get_task(conn, task_id).status == "review"
    assert kb.get_task(conn, task_id).assignee == "verifier"
    assert kb.get_task(conn, child).status == "todo"
    return task_id, child


def _dispatch_review(conn, task_id):
    spawned = []
    result = dispatch.dispatch_once(conn, spawn_fn=lambda task, _workspace: spawned.append(task))
    assert [row[0] for row in result.spawned] == [task_id]
    assert [(task.id, task.assignee) for task in spawned] == [(task_id, "verifier")]
    assert "sdlc-review" in spawned[0].skills
    run = kb.latest_run(conn, task_id)
    assert run.profile == "verifier"
    assert kb.get_task(conn, task_id).status == "running"
    return run.id


def test_automatic_verifier_approval_closes_same_card_and_releases_children(verification_board):
    conn, _home = verification_board
    task_id, child = _handoff(conn)
    run_id = _dispatch_review(conn, task_id)
    assert kb.complete_task(conn, task_id, summary="Independently approved", expected_run_id=run_id)
    assert kb.get_task(conn, task_id).status == "done"
    assert kb.get_task(conn, child).status == "ready"
    assert {task.id for task in kb.list_tasks(conn)} == {task_id, child}
    runs = kb.list_runs(conn, task_id)
    implementation = next(run for run in runs if run.profile == "implementer")
    assert implementation.summary == "Feature tested"
    assert implementation.metadata["tests_passed"] == 4
    assert kb.latest_run(conn, task_id).outcome == "completed"


def test_automatic_verifier_changes_return_to_implementer_with_notes(verification_board):
    conn, _home = verification_board
    task_id, child = _handoff(conn)
    run_id = _dispatch_review(conn, task_id)
    reason = "Add coverage for an empty export before approval."
    assert kb.request_changes(conn, task_id, reason=reason, expected_run_id=run_id,
                              ask="Add the empty-export regression.", audience="agent") == (True, "implementer")
    task = kb.get_task(conn, task_id)
    assert (task.status, task.assignee, task.current_run_id) == ("ready", "implementer", None)
    assert kb.get_task(conn, child).status == "todo"
    event = next(event for event in reversed(kb.list_events(conn, task_id))
                 if event.kind == "changes_requested")
    assert event.payload["reason"] == reason
    assert event.payload["reviewer"] == "verifier"
    assert event.payload["ask"] == "Add the empty-export regression."
    assert kb.latest_run(conn, task_id).summary == reason
    assert reason in kb.build_worker_context(conn, task_id)
    rework = kb.claim_task(conn, task_id)
    assert kb.complete_task(conn, task_id, summary="Empty-export regression passes",
                            expected_run_id=rework.current_run_id)
    assert kb.get_task(conn, task_id).assignee == "verifier"
    assert kb.get_task(conn, task_id).status == "review"
    assert kb.get_task(conn, child).status == "todo"


@pytest.mark.parametrize("exclusion", ["flag", "allowlist"])
def test_automatic_verification_never_routes_to_an_ineligible_verifier(verification_board, exclusion, caplog):
    conn, home = verification_board
    if exclusion == "flag":
        (home / "profiles" / "verifier" / "profile.yaml").write_text("kanban_dispatch: false\n")
    else:
        (home / "profiles" / "implementer" / "config.yaml").write_text(
            "kanban:\n  dispatch_profiles: [implementer]\n")
    task_id = kb.create_task(conn, title="No eligible verifier", assignee="implementer")
    run = kb.claim_task(conn, task_id)
    assert kb.complete_task(conn, task_id, summary="Tests pass", expected_run_id=run.current_run_id)
    assert kb.get_task(conn, task_id).status == "done"
    assert kb.get_task(conn, task_id).assignee == "implementer"
    assert not any(event.kind == "review_requested" for event in kb.list_events(conn, task_id))
    assert "automatic verification disabled" in caplog.text
