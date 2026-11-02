"""Card extensions reach the real adapter without decorating history or durable routes."""
import asyncio

from hermes_cli import lifecycle, kanban_db_connect as kbc
from tests.gateway.test_kanban_slack_cards import setup, change, collect, deliver


BLOCKS = [{"type": "actions", "elements": [{
    "type": "button", "text": {"type": "plain_text", "text": "Comment…"},
    "action_id": "example_comment", "value": "opaque",
}]}]


def test_card_blocks_reach_root_and_edit_but_not_history_or_subscription(tmp_path, monkeypatch):
    runner, client, task_id = setup(tmp_path, monkeypatch)

    async def hook(name, **kwargs):
        if name == "kanban_card_render":
            assert kwargs["adapter"] is runner.adapters[next(iter(runner.adapters))]
            assert kwargs["subscription"]["task_id"] == task_id
            assert kwargs["db_path"] == str((tmp_path / "kanban.db").resolve())
            assert kwargs["task"].id == task_id
            assert kwargs["events"]
            return [{"blocks": BLOCKS}]
        return []

    monkeypatch.setattr(lifecycle, "ainvoke_hook", hook)
    change(task_id, "running")
    asyncio.run(deliver(runner, collect(runner)))
    assert client.calls[0][1].get("blocks") == BLOCKS
    change(task_id, "blocked")
    asyncio.run(deliver(runner, collect(runner)))
    assert client.calls[1][1].get("blocks") == BLOCKS
    assert "blocks" not in client.calls[2][1]
    with kbc.connect() as conn:
        metadata = conn.execute("SELECT delivery_metadata FROM kanban_notify_subs").fetchone()[0]
    assert "blocks" not in metadata
    assert not collect(runner)


def test_comment_refreshes_card_once_without_publishing_comment_body(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb

    runner, client, task_id = setup(tmp_path, monkeypatch, {"thread_ts": "100.000001"})
    with kbc.connect() as conn:
        kb._append_event(conn, task_id, "commented", {"author": "reviewer", "body": "Board-only comment"})
        conn.commit()
    asyncio.run(deliver(runner, collect(runner)))
    assert [method for method, _ in client.calls] == ["update", "post"]
    assert "Comment added by reviewer" in client.calls[1][1]["text"]
    assert all("Board-only comment" not in kwargs["text"] for _, kwargs in client.calls)
    assert not collect(runner)


def test_broken_card_extension_preserves_delivery(tmp_path, monkeypatch):
    runner, client, task_id = setup(tmp_path, monkeypatch)

    async def hook(name, **kwargs):
        if name == "kanban_card_render":
            raise RuntimeError("broken extension")
        return []

    monkeypatch.setattr(lifecycle, "ainvoke_hook", hook)
    change(task_id, "running")
    asyncio.run(deliver(runner, collect(runner)))
    assert len(client.calls) == 1
    assert not collect(runner)
