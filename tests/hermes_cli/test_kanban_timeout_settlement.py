"""Failure settlement fences terminal handoffs without suppressing real timeouts."""

from pathlib import Path
import signal
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(kb, "_pid_alive", lambda _: False)
    with kbc.connect(tmp_path / "kanban.db") as conn:
        yield conn


def _snapshot(conn, tid):
    return {
        table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} WHERE {key}=? ORDER BY id", (tid,))]
        for table, key in (("tasks", "id"), ("task_events", "task_id"), ("task_runs", "task_id"))
    }


@pytest.mark.parametrize("release_claim,terminal", [
    (False, "done"), (False, "archived"),
    (True, "done"), (True, "archived"), (True, "blocked"), (True, "review"),
])
@pytest.mark.parametrize("failure_limit", [1, 2])
def test_failure_accounting_preserves_terminal_handoff(board, terminal, release_claim, failure_limit):
    tid = kb.create_task(board, title="finished attempt", assignee="worker")
    assert kb.claim_task(board, tid)
    if terminal in {"done", "archived"}:
        assert kb.complete_task(board, tid, summary="finished")
        if terminal == "archived":
            assert kb.archive_task(board, tid)
    elif terminal == "blocked":
        assert kb.block_task(board, tid, reason="needs operator", kind="needs_input")
    else:
        assert kb.request_review(board, tid, summary="review me", reviewer="reviewer")
    assert kb.get_task(board, tid).status == terminal
    before = _snapshot(board, tid)

    assert not kbd._record_task_failure(
        board, tid, "late timeout", outcome="timed_out", failure_limit=failure_limit,
        release_claim=release_claim, end_run=release_claim,
    )

    assert _snapshot(board, tid) == before


def _overdue_attempt(conn, tid):
    assert kb.claim_task(conn, tid)
    kbd._set_worker_pid(conn, tid, 999999)
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET started_at=? WHERE task_id=? AND ended_at IS NULL",
                     (int(time.time()) - 30, tid))


def test_running_runtime_timeout_retries_then_trips_once_per_attempt(board):
    tid = kb.create_task(board, title="slow worker", assignee="worker", max_runtime_seconds=1, max_retries=2)
    signals = []
    for attempt, status in ((1, "ready"), (2, "blocked")):
        _overdue_attempt(board, tid)
        assert kbd.enforce_max_runtime(board, signal_fn=lambda pid, sig: signals.append((pid, sig))) == [tid]
        task = kb.get_task(board, tid)
        assert task.status == status
        assert task.consecutive_failures == attempt
        runs = board.execute("SELECT outcome, ended_at FROM task_runs WHERE task_id=? ORDER BY id", (tid,)).fetchall()
        assert len(runs) == attempt
        assert all(row["outcome"] == "timed_out" and row["ended_at"] is not None for row in runs)
        kinds = [event.kind for event in kb.list_events(board, tid)]
        assert kinds.count("timed_out") == attempt
        assert kinds.count("gave_up") == (attempt == 2)
        before = _snapshot(board, tid)
        assert kbd.enforce_max_runtime(board, signal_fn=lambda *_: pytest.fail("settled attempt signalled")) == []
        assert _snapshot(board, tid) == before
    assert signals == [(999999, signal.SIGTERM), (999999, signal.SIGTERM)]


def test_completion_during_timeout_signal_wins_settlement(board):
    tid = kb.create_task(board, title="finishes during SIGTERM", assignee="worker", max_runtime_seconds=1)
    _overdue_attempt(board, tid)
    completed = []

    def complete_during_signal(pid, sig):
        assert (pid, sig) == (999999, signal.SIGTERM)
        # A separate connection models the worker committing during the dispatcher's kill wait.
        with kbc.connect(Path(board.execute("PRAGMA database_list").fetchone()[2])) as worker_conn:
            assert kb.complete_task(worker_conn, tid, summary="finished during shutdown")
            completed.append(_snapshot(worker_conn, tid))

    assert kbd.enforce_max_runtime(board, signal_fn=complete_during_signal) == []
    assert len(completed) == 1
    assert kb.get_task(board, tid).status == "done"
    assert _snapshot(board, tid) == completed[0]
    assert not any(event.kind in {"timed_out", "gave_up"} for event in kb.list_events(board, tid))
