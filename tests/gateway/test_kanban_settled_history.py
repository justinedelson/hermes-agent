"""Preserve real retry history while discarding stale terminal settlement."""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from agent.turn_finalizer import _record_kanban_budget_exhausted
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from tests.gateway.test_kanban_slack_cards import collect, deliver, setup


@pytest.mark.parametrize("kind", ["timed_out", "crashed", "gave_up", "reclaimed"])
def test_legacy_retry_after_completion_is_not_delivered(tmp_path, monkeypatch, kind):
    runner, client, tid = setup(tmp_path, monkeypatch, {"thread_ts": "100.000001"})
    with kbc.connect() as conn:
        kb.claim_task(conn, tid, claimer="worker", ttl_seconds=300)
        assert kb.complete_task(conn, tid, summary="finished")
    asyncio.run(deliver(runner, collect(runner)))
    client.calls.clear()
    with kbc.connect() as conn:
        kb._append_event(conn, tid, kind, {"retry_status": "ready", "error": "stale settlement"})
    asyncio.run(deliver(runner, collect(runner)))
    assert client.calls == []
    assert not collect(runner)


def test_valid_retry_history_survives_later_completion(tmp_path, monkeypatch):
    runner, client, tid = setup(tmp_path, monkeypatch, {"thread_ts": "100.000001"})
    with kbc.connect() as conn:
        kb.claim_task(conn, tid, claimer="worker", ttl_seconds=300)
    _record_kanban_budget_exhausted(tid, 90, 90, logging.getLogger(__name__))
    with kbc.connect() as conn:
        kb.claim_task(conn, tid, claimer="worker", ttl_seconds=300)
        assert kb.complete_task(conn, tid, summary="finished")
    asyncio.run(deliver(runner, collect(runner)))
    assert sum("Worker timed out" in args["text"] for method, args in client.calls if method == "post") == 1


def test_terminal_trail_ignores_phantom_retry_but_allows_real_reopen(tmp_path, monkeypatch):
    from gateway.kanban_slack_card import _snapshot, _state_timing

    runner, client, tid = setup(tmp_path, monkeypatch, {"thread_ts": "100.000001"})
    with kbc.connect() as conn:
        kb.claim_task(conn, tid, claimer="worker", ttl_seconds=300)
        assert kb.complete_task(conn, tid, summary="finished")
        conn.execute("UPDATE task_events SET created_at=1000 WHERE task_id=? AND kind='completed'", (tid,))
        kb._append_event(conn, tid, "timed_out", {"retry_status": "ready"})
    n = SimpleNamespace(board_slug="default", task_id=tid)
    task, events = _snapshot(n)
    assert events[-1].kind == "completed"
    assert "since <!date^1000^" in _state_timing(task, events, 1600)
    with kbc.connect() as conn:
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        kb._append_event(conn, tid, "status", {"status": "ready"})
        kb.claim_task(conn, tid, claimer="worker", ttl_seconds=300)
    _record_kanban_budget_exhausted(tid, 90, 90, logging.getLogger(__name__))
    task, events = _snapshot(n)
    assert task.status == "ready"
    assert events[-1].kind == "timed_out"
    asyncio.run(deliver(runner, collect(runner)))
    assert sum("Worker timed out" in args["text"] for method, args in client.calls if method == "post") == 1
