"""Structured notification ownership survives tool and CLI transitions."""

import json

import pytest

from hermes_cli import kanban as kc, kanban_db as kb, kanban_db_connect as kbc
from tools import kanban_tools
from tools.registry import registry


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    with kbc.connect() as conn:
        yield conn


@pytest.mark.parametrize("surface", ["tool", "cli"])
def test_block_preserves_explicit_action_and_owner(board, surface):
    tid = kb.create_task(board, title="Choose region", assignee="builder")
    if surface == "tool":
        result = json.loads(registry.dispatch("kanban_block", {
            "task_id": tid, "reason": "The deployment needs a region", "kind": "needs_input",
            "ask": "Choose the deployment region", "audience": "human",
        }))
        assert result.get("ok"), result
    else:
        result = kc.run_slash(f"block {tid} 'The deployment needs a region' --kind needs_input "
                              "--ask 'Choose the deployment region' --audience human")
        assert "Blocked" in result
    ev = [ev for ev in kb.list_events(board, tid) if ev.kind == "blocked"][-1]
    assert ev.payload["ask"] == "Choose the deployment region"
    assert ev.payload["audience"] == "human"


@pytest.mark.parametrize("surface", ["tool", "cli"])
def test_review_round_trip_preserves_action_ownership(board, surface, monkeypatch):
    tid = kb.create_task(board, title="Review change", assignee="builder")
    if surface == "tool":
        result = json.loads(registry.dispatch("kanban_request_review", {
            "task_id": tid, "summary": "Tests pass", "ask": "Review the change; do not merge",
            "audience": "human",
        }))
        assert result.get("ok"), result
    else:
        assert "Requested review" in kc.run_slash(
            f"request-review {tid} --summary 'Tests pass' "
            "--ask 'Review the change; do not merge' --audience human")
    event = [ev for ev in kb.list_events(board, tid) if ev.kind == "review_requested"][-1]
    assert event.payload["ask"] == "Review the change; do not merge"
    assert event.payload["audience"] == "human"
    review = kb.claim_review_task(board, tid, claimer="reviewer:1")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(review.current_run_id))
    if surface == "tool":
        result = json.loads(registry.dispatch("kanban_request_changes", {
            "reason": "Missing boundary test", "ask": "Add the boundary test", "audience": "agent",
        }))
        assert result.get("ok"), result
    else:
        assert "Requested changes" in kc.run_slash(
            f"request-changes {tid} 'Missing boundary test' --ask 'Add the boundary test' --audience agent")
    event = [ev for ev in kb.list_events(board, tid) if ev.kind == "changes_requested"][-1]
    assert event.payload["ask"] == "Add the boundary test"
    assert event.payload["audience"] == "agent"


@pytest.mark.parametrize("fields", [{"ask": "x" * 201}, {"ask": " "}, {"ask": 1}, {"audience": "everyone"}])
def test_invalid_action_does_not_mutate_task(board, fields):
    tid = kb.create_task(board, title="Action validation", assignee="builder")
    with pytest.raises(ValueError):
        kb.block_task(board, tid, reason="Needs input", **fields)
    assert kb.get_task(board, tid).status == "ready"
    assert not [ev for ev in kb.list_events(board, tid) if ev.kind == "blocked"]


def test_action_is_redacted_at_domain_boundary(board):
    tid = kb.create_task(board, title="Action redaction", assignee="builder")
    secret = "ghp_" + "A" * 40
    assert kb.block_task(board, tid, reason="Reauthorize", ask=f"Replace token {secret}", audience="human")
    ev = [ev for ev in kb.list_events(board, tid) if ev.kind == "blocked"][-1]
    assert secret not in ev.payload["ask"]
