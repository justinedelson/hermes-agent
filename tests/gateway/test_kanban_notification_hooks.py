"""Notification presentation hooks preserve durable card and delivery semantics."""
import asyncio

from hermes_cli import lifecycle
from tests.gateway.test_kanban_slack_cards import setup, change, collect, deliver


def test_attention_hook_mentions_only_new_history_not_edited_card(tmp_path, monkeypatch):
    runner, client, task_id = setup(tmp_path, monkeypatch, {"thread_ts": "100.000001"})
    observed = []

    async def hook(name, **kwargs):
        if name == "kanban_notify_prepare":
            observed.append(kwargs)
            return [{"attention_text": "<@W4RPPHL3V>"}]
        return []

    monkeypatch.setattr(lifecycle, "ainvoke_hook", hook)
    change(task_id, "blocked", kind="needs_input", audience="human", ask="Choose a region")
    asyncio.run(deliver(runner, collect(runner)))
    assert "<@W4RPPHL3V>" not in client.calls[0][1]["text"]
    assert client.calls[1][1]["text"].startswith("<@W4RPPHL3V> ")
    assert observed[0]["event_payload"]["audience"] == "human"
    assert observed[0]["subscription"]["task_id"] == task_id
    assert not collect(runner)


def test_attention_hook_failure_does_not_lose_delivery(tmp_path, monkeypatch):
    runner, client, task_id = setup(tmp_path, monkeypatch)

    async def hook(name, **kwargs):
        raise RuntimeError("broken presentation plugin")

    monkeypatch.setattr(lifecycle, "ainvoke_hook", hook)
    change(task_id, "running")
    asyncio.run(deliver(runner, collect(runner)))
    assert len(client.calls) == 1
    assert not collect(runner)


def test_idle_notifier_ticks_background_consumers(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from gateway import kanban_notification_hooks, kanban_watchers
    runner, _, _ = setup(tmp_path, monkeypatch)
    runner._running = True
    tick = AsyncMock()
    monkeypatch.setattr(kanban_notification_hooks, "notification_tick", tick)
    monkeypatch.setattr(kanban_watchers.asyncio, "sleep", AsyncMock())

    async def stop(_interval):
        runner._running = False

    runner._sleep_between_ticks = stop
    asyncio.run(runner._kanban_notifier_watcher())
    tick.assert_awaited_once_with(runner)


def test_presentation_consumer_can_refresh_cards_when_diagnostics_are_muted(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
    from hermes_constants import get_hermes_home
    runner, client, task_id = setup(tmp_path, monkeypatch, {"thread_ts": "100.000001"})
    (get_hermes_home() / "config.yaml").write_text("display: {suppress_warning_notifications: true}")

    async def hook(name, **kwargs):
        return [{"attention_text": "<@W4RPPHL3V>", "diagnostic": False}] if name == "kanban_notify_prepare" else []

    monkeypatch.setattr(lifecycle, "ainvoke_hook", hook)
    with kbc.connect() as conn:
        kb._append_event(conn, task_id, "gave_up", {"failures": 2})
        conn.commit()
    asyncio.run(deliver(runner, collect(runner)))
    assert [method for method, _ in client.calls] == ["update", "post"]
    assert client.calls[-1][1]["text"].startswith("<@W4RPPHL3V>")


def test_tick_binds_every_served_profile_and_refuses_foreign_owner(tmp_path, monkeypatch):
    from gateway.kanban_notification_hooks import notification_tick
    from hermes_constants import get_hermes_home
    runner, _, _ = setup(tmp_path, monkeypatch)
    homes = [(name, tmp_path / name) for name in ("alpha", "beta")]
    for _, home in homes:
        home.mkdir()
    runner.config.multiplex_profiles = True
    runner.config._runtime_profile_homes = homes
    monkeypatch.setattr(lifecycle, "has_hook", lambda name: True)
    calls = []

    async def hook(name, **kwargs):
        calls.append(get_hermes_home())
        assert kwargs["resolve_adapter"]({"notifier_profile": "foreign", "platform": "slack"}) is None
        return []

    monkeypatch.setattr(lifecycle, "ainvoke_hook", hook)
    asyncio.run(notification_tick(runner))
    assert calls == [home for _, home in homes]
