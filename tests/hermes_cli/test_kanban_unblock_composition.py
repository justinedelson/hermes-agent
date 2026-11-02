"""Guarded UI operations compose with unblock without losing parent/review re-gating."""
import pytest
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc


def test_guarded_unblock_and_action_receipt_rollback_together(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    with kbc.connect_closing() as conn:
        task = kb.create_task(conn, title="Guarded", assignee="worker", initial_status="blocked")
        with pytest.raises(ValueError, match="abort receipt"):
            with kb.write_txn(conn):
                assert kb.unblock_task(conn, task, allow_nested=True)
                kb._append_event(conn, task, "interaction_receipt", {"request": "opaque"})
                raise ValueError("abort receipt")
        assert kb.get_task(conn, task).status == "blocked"
        assert not any(ev.kind in {"unblocked", "interaction_receipt"} for ev in kb.list_events(conn, task))
        with kb.write_txn(conn):
            assert kb.unblock_task(conn, task, allow_nested=True)
            kb._append_event(conn, task, "interaction_receipt", {"request": "opaque"})
        assert kb.get_task(conn, task).status == "ready"
        assert len([ev for ev in kb.list_events(conn, task) if ev.kind == "unblocked"]) == 1
