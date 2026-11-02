"""Budget exhaustion cannot undo a durable worker handoff."""

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.turn_finalizer import _record_kanban_budget_exhausted, _resolve_budget_fallback
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def worker(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    monkeypatch.setattr("agent.turn_finalizer.is_dispatcher_owned_worker_context", lambda: True)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="last-turn handoff", assignee="worker")
        assert kb.claim_task(conn, tid, claimer="worker", ttl_seconds=300)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    return tid


def _snapshot(conn, tid):
    return {
        table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} WHERE {key}=? ORDER BY id", (tid,))]
        for table, key in (("tasks", "id"), ("task_events", "task_id"), ("task_runs", "task_id"))
    }


@pytest.mark.parametrize("terminal", ["done", "archived", "blocked", "review"])
@pytest.mark.parametrize("failure_limit", [1, 2])
def test_last_turn_handoff_survives_budget_fallback(worker, monkeypatch, terminal, failure_limit):
    monkeypatch.setattr(kbd, "DEFAULT_FAILURE_LIMIT", failure_limit)
    with kbc.connect() as conn:
        if terminal in {"done", "archived"}:
            assert kb.complete_task(conn, worker, summary="finished")
            if terminal == "archived":
                assert kb.archive_task(conn, worker)
        elif terminal == "blocked":
            assert kb.block_task(conn, worker, reason="needs operator", kind="needs_input")
        else:
            assert kb.request_review(conn, worker, summary="ready for review", reviewer="reviewer")
        assert kb.get_task(conn, worker).status == terminal
        before = _snapshot(conn, worker)

    result = _resolve_budget_fallback(
        SimpleNamespace(max_iterations=90, iteration_budget=SimpleNamespace(remaining=0)),
        final_response="handoff recorded", api_call_count=90, interrupted=False, failed=False,
        messages=[], _turn_exit_reason="text_response(90)",
        _pending_verification_response=None, _pending_verification_response_previewed=False,
        logger=logging.getLogger(__name__),
    )

    assert result == ("handoff recorded", "text_response(90)", False, False)
    with kbc.connect() as conn:
        assert _snapshot(conn, worker) == before


def test_repeated_unfinished_budget_settlement_counts_once(worker):
    _record_kanban_budget_exhausted(worker, 90, 90, logging.getLogger(__name__))
    with kbc.connect() as conn:
        task = kb.get_task(conn, worker)
        assert task.status == "ready"
        assert task.consecutive_failures == 1
        assert "Iteration budget exhausted" in task.last_failure_error
        assert [event.kind for event in kb.list_events(conn, worker)].count("timed_out") == 1
        run = conn.execute("SELECT * FROM task_runs WHERE task_id=?", (worker,)).fetchone()
        assert run["outcome"] == "timed_out"
        assert run["ended_at"] is not None
        before = _snapshot(conn, worker)
    _record_kanban_budget_exhausted(worker, 90, 90, logging.getLogger(__name__))
    with kbc.connect() as conn:
        assert _snapshot(conn, worker) == before
