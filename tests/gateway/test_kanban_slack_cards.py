"""Slack task cards edit one durable root and keep per-event thread history."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.kanban_watchers_notifier import _KanbanNotification, _notifier_collect
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_notify as kbn
from plugins.platforms.slack.adapter import SlackAdapter


class FakeSlackClient:
    def __init__(self):
        self.calls = []

    async def chat_postMessage(self, **kwargs):
        self.calls.append(("post", kwargs))
        return {"ok": True, "ts": f"100.{len(self.calls):06d}"}

    async def chat_update(self, **kwargs):
        self.calls.append(("update", kwargs))
        return {"ok": True, "ts": kwargs["ts"]}


def setup(tmp_path, monkeypatch, metadata=None, thread_id=""):
    from agent import auxiliary_client
    monkeypatch.setattr(auxiliary_client, "call_llm", Mock(side_effect=RuntimeError("offline")))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    client = FakeSlackClient()
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-fake"))
    adapter._app = SimpleNamespace(client=client)
    adapter._get_client = lambda chat, team_id="": client
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {Platform.SLACK: adapter}
    runner._profile_adapters = {}
    runner._primary_profile_name = "default"
    runner._kanban_notifier_profile = "default"
    runner._kanban_dispatcher_lock_handle = object()
    runner.config = GatewayConfig(multiplex_profiles=False)
    runner._deliver_kanban_artifacts = AsyncMock()
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="Old title", assignee="worker")
        kbn.add_notify_sub(conn, task_id=task_id, platform="slack", chat_id="C123",
                           thread_id=thread_id, chat_type="channel", delivery_metadata=metadata)
    return runner, client, task_id


def change(task_id, status, **payload):
    with kbc.connect() as conn:
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))
        kb._append_event(conn, task_id, "status", {"status": status, **payload})
        conn.commit()


def collect(runner):
    return _notifier_collect(runner, kb, notifier_profile="default", gc_due=False, gc_retention_days=30)


async def deliver(runner, rows):
    for row in rows:
        await _KanbanNotification(runner, row, platform_cls=Platform, sub_fail_counts={}).deliver()


def test_card_times_are_slack_local_tokens_with_pacific_fallback(tmp_path, monkeypatch):
    runner, client, task_id = setup(tmp_path, monkeypatch, {"thread_ts": "100.000001"})
    change(task_id, "blocked", ask="Choose a region", audience="human")
    with kbc.connect() as conn:
        conn.execute("UPDATE task_events SET created_at=1793468577 WHERE task_id=?", (task_id,))
        conn.commit()
    asyncio.run(deliver(runner, collect(runner)))
    for _, kwargs in client.calls:
        assert "<!date^1793468577^{time}|2026-10-31 10:42 PDT>" in kwargs["text"]
        assert "UTC" not in kwargs["text"]


def test_card_labels_and_punctuated_titles_have_no_invisible_chars(tmp_path, monkeypatch):
    runner, client, task_id = setup(tmp_path, monkeypatch, {"thread_ts": "100.000001"})
    with kbc.connect() as conn:
        conn.execute("UPDATE tasks SET title='Build (proposal E)' WHERE id=?", (task_id,))
        conn.commit()
    change(task_id, "blocked", ask="Choose a region?", audience="human", why="Needs input.")
    asyncio.run(deliver(runner, collect(runner)))
    card = client.calls[0][1]["text"]
    assert "\u200b" not in card
    assert "*Ask*: Choose a region?" in card
    assert "*Details*: Needs input." in card
    assert "*Status trail*:" in card
    assert card.startswith("*Task*: Build (proposal E)\n")


def test_trail_collapses_churn_and_reports_continuous_time_in_state(tmp_path, monkeypatch):
    runner, client, task_id = setup(tmp_path, monkeypatch, {"thread_ts": "100.000001"})
    monkeypatch.setattr("time.time", lambda: 1793468577 + 6000)
    with kbc.connect() as conn:
        conn.execute("UPDATE tasks SET created_at=1793468577 WHERE id=?", (task_id,))
        conn.execute("UPDATE task_events SET created_at=1793468577 WHERE task_id=?", (task_id,))
        for i, state in enumerate(["running", "blocked", "ready"] * 10 + ["running", "review", "done", "done"]):
            kb._append_event(conn, task_id, "status", {"status": state})
            conn.execute("UPDATE task_events SET created_at=? WHERE id=(SELECT MAX(id) FROM task_events)",
                         (1793468577 + (i + 1) * 100,))
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (task_id,))
        conn.commit()
    asyncio.run(deliver(runner, collect(runner)))
    card = client.calls[0][1]["text"]
    assert "*In state*: 45m · since <!date^1793471877^{time}|" in card
    trail = card.split("*Status trail*: ", 1)[1]
    assert [entry.split(" ", 1)[0] for entry in trail.split(" → ")] == ["created", "running", "blocked", "review", "done"]
    assert "<!date^1793468577^{time}|" in trail


@pytest.mark.parametrize("metadata,thread_id", [({}, ""), ({"kanban_card": False}, ""), ({}, "100.123456")])
def test_claim_and_promotion_refresh_cards_once_without_waking_or_legacy_noise(tmp_path, monkeypatch, metadata, thread_id):
    runner, client, task_id = setup(tmp_path, monkeypatch, metadata, thread_id)
    change(task_id, "blocked")
    asyncio.run(deliver(runner, collect(runner)))
    client.calls.clear()
    with kbc.connect() as conn:
        kb.unblock_task(conn, task_id)
        kb.claim_task(conn, task_id, claimer="test-claim", ttl_seconds=300)
    asyncio.run(deliver(runner, collect(runner)))
    is_card = not thread_id and metadata.get("kanban_card") is not False
    assert [method for method, _ in client.calls] == (["update", "post", "update", "post"] if is_card else [])
    if is_card:
        assert "Unblocked" in client.calls[1][1]["text"]
        assert "Status → running" in client.calls[3][1]["text"]
        assert "[RUNNING]" in client.calls[2][1]["text"]
    assert not collect(runner)
    if is_card:
        with kbc.connect() as conn:
            conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (task_id,))
            kb.recompute_ready(conn)
        client.calls.clear()
        asyncio.run(deliver(runner, collect(runner)))
        assert [method for method, _ in client.calls] == ["update", "post"]
        assert "Status → ready" in client.calls[1][1]["text"]


@pytest.mark.parametrize("kind,payload,status", [
    ("unblocked", {"status": "review", "resume_status": "review"}, "review"),
    ("changes_requested", {"status": "todo", "implementer": "worker"}, "todo"),
    ("scheduled", {}, "scheduled"), ("specified", {}, "todo"),
    ("review_reopened", {}, "ready"), ("reclaimed", {"retry_status": "review"}, "review"),
])
def test_other_state_changes_refresh_once_and_use_persisted_landing_state(tmp_path, monkeypatch, kind, payload, status):
    runner, client, task_id = setup(tmp_path, monkeypatch, {"thread_ts": "100.000001"})
    change(task_id, "running")
    asyncio.run(deliver(runner, collect(runner)))
    client.calls.clear()
    with kbc.connect() as conn:
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))
        kb._append_event(conn, task_id, kind, payload)
        conn.execute("UPDATE task_events SET created_at=1793468577 WHERE task_id=? AND kind=?", (task_id, kind))
        conn.commit()
    asyncio.run(deliver(runner, collect(runner)))
    assert [method for method, _ in client.calls] == ["update", "post"]
    card = client.calls[0][1]["text"]
    assert f"[{status.upper()}]" in card
    assert "since <!date^1793468577^{time}|" in card
    assert "gateway.kanban" not in client.calls[1][1]["text"]
    assert not collect(runner)


@pytest.mark.parametrize("public_url", ["https://dashboard.example.test/hermes/", ""])
def test_card_links_use_configured_dashboard_and_keep_pr_jira_references(tmp_path, monkeypatch, public_url):
    from hermes_constants import get_hermes_home
    runner, client, task_id = setup(tmp_path, monkeypatch)
    monkeypatch.delenv("HERMES_DASHBOARD_PUBLIC_URL", raising=False)
    (get_hermes_home() / "config.yaml").write_text(f"dashboard:\n  public_url: '{public_url}'\n", encoding="utf-8")
    with kbc.connect() as conn:
        conn.execute("UPDATE tasks SET body=? WHERE id=?", (
            "Work https://github.com/example/services/pull/42 and https://jira.example.test/browse/APP-123", task_id))
        conn.commit()
    change(task_id, "running")
    asyncio.run(deliver(runner, collect(runner)))
    card = client.calls[0][1]["text"]
    if public_url:
        assert f"<https://dashboard.example.test/hermes/kanban?board=default&amp;task={task_id}|{task_id}>" in card
    else:
        assert f"worker · {task_id}" in card
        assert "kanban?" not in card
    assert "<https://github.com/example/services/pull/42|PR #42>" in card
    assert "<https://jira.example.test/browse/APP-123|APP-123>" in card


def test_batch_posts_one_card_then_edits_and_replies_from_current_state(tmp_path, monkeypatch):
    runner, client, task_id = setup(tmp_path, monkeypatch, {"scope_id": "T123"})
    change(task_id, "review")
    change(task_id, "blocked", reason="Choose a region")
    change(task_id, "done")
    rows = collect(runner)
    with kbc.connect() as conn:
        conn.execute("UPDATE tasks SET title='Current title' WHERE id=?", (task_id,))
        conn.commit()
    asyncio.run(deliver(runner, rows))
    assert [method for method, _ in client.calls] == ["post", "update", "post", "update", "post"]
    root = "100.000001"
    first = client.calls[0][1]
    assert "thread_ts" not in first
    assert first["text"].startswith("*Task*: Current title\n")
    assert "[DONE]" in first["text"]
    assert "Choose a region" not in first["text"]
    for method, kwargs in client.calls[1:]:
        if method == "update":
            assert kwargs["ts"] == root
            assert "Current title" in kwargs["text"]
            assert "[DONE]" in kwargs["text"]
        else:
            assert kwargs["thread_ts"] == root
            assert "\n" not in kwargs["text"]
    with kbc.connect() as conn:
        sub = kbn.list_notify_subs(conn, task_id)[0]
    assert sub["delivery_metadata"]["thread_ts"] == root
    assert sub["delivery_metadata"]["scope_id"] == "T123"
    assert not collect(runner)
    # A new delivery object/tick must reuse the persisted root, without any plugin cache.
    change(task_id, "running")
    asyncio.run(deliver(runner, collect(runner)))
    assert [method for method, _ in client.calls[-2:]] == ["update", "post"]
    assert client.calls[-1][1]["thread_ts"] == root
    assert "[RUNNING]" in client.calls[-2][1]["text"]


@pytest.mark.parametrize("failure", ["message_not_found", "cant_update_message", "adapter_exception"])
def test_update_failure_still_delivers_history_without_replacing_root(tmp_path, monkeypatch, failure):
    runner, client, task_id = setup(tmp_path, monkeypatch, {"thread_ts": "100.000001"})
    if failure == "adapter_exception":
        runner.adapters[Platform.SLACK].edit_message = AsyncMock(side_effect=RuntimeError("edit failed"))
    else:
        async def failed_update(**kwargs):
            client.calls.append(("update", kwargs))
            error = RuntimeError(failure)
            error.response = SimpleNamespace(data={"ok": False, "error": failure}, status_code=200)
            raise error
        client.chat_update = failed_update
    change(task_id, "blocked", reason="Choose a region")
    asyncio.run(deliver(runner, collect(runner)))
    posts = [kwargs for method, kwargs in client.calls if method == "post"]
    assert len(posts) == 1
    assert posts[0]["thread_ts"] == "100.000001"
    assert "Choose a region" in posts[0]["text"]
    assert "\n" not in posts[0]["text"]
    assert not collect(runner)
    with kbc.connect() as conn:
        assert kbn.list_notify_subs(conn, task_id)[0]["delivery_metadata"]["thread_ts"] == "100.000001"


@pytest.mark.parametrize("kind,status", [("unblocked", "ready"), ("archived", "archived")])
def test_bookkeeping_refreshes_existing_card_and_records_history(tmp_path, monkeypatch, kind, status):
    runner, client, task_id = setup(tmp_path, monkeypatch, {"thread_ts": "100.000001"})
    with kbc.connect() as conn:
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))
        kb._append_event(conn, task_id, kind)
        conn.commit()
    asyncio.run(deliver(runner, collect(runner)))
    assert [method for method, _ in client.calls] == ["update", "post"]
    assert f"[{status.upper()}]" in client.calls[0][1]["text"]
    assert "\n" not in client.calls[1][1]["text"]
    assert "gateway.kanban" not in client.calls[1][1]["text"]
    with kbc.connect() as conn:
        subs = kbn.list_notify_subs(conn, task_id)
    assert bool(subs) is (kind != "archived")


@pytest.mark.parametrize("kind,status,headline", [("gave_up", "blocked", "Retries exhausted"),
                                                 ("crashed", "ready", "Worker crashed"),
                                                 ("timed_out", "review", "Worker timed out")])
def test_retry_failure_uses_latest_saved_error_not_old_ask(tmp_path, monkeypatch, kind, status, headline):
    runner, client, task_id = setup(tmp_path, monkeypatch)
    with kbc.connect() as conn:
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))
        kb._append_event(conn, task_id, "status", {"status": status, "reason": "Old decision"})
        kb._append_event(conn, task_id, kind, {"error": "Repair the build environment", "retry_status": status})
        conn.commit()
    asyncio.run(deliver(runner, collect(runner)))
    card = client.calls[0][1]["text"]
    assert headline in card
    assert "Repair the build environment" in card
    assert "Old decision" not in card


@pytest.mark.parametrize("metadata,thread_id", [({"kanban_card": False}, ""), ({}, "100.123456"),
                                               ({"thread_id": "100.123456"}, "")])
def test_explicit_legacy_delivery_does_not_edit_conversation_root(tmp_path, monkeypatch, metadata, thread_id):
    runner, client, task_id = setup(tmp_path, monkeypatch, metadata, thread_id)
    change(task_id, "review")
    change(task_id, "done")
    asyncio.run(deliver(runner, collect(runner)))
    assert [method for method, _ in client.calls] == ["post", "post"]
    for _, kwargs in client.calls:
        assert kwargs.get("thread_ts") == (thread_id or metadata.get("thread_id"))
    with kbc.connect() as conn:
        assert not kbn.list_notify_subs(conn, task_id)[0]["delivery_metadata"].get("thread_ts")


def test_card_ask_and_history_are_safe_and_artifacts_stay_in_card_thread(tmp_path, monkeypatch):
    runner, client, task_id = setup(tmp_path, monkeypatch)
    with kbc.connect() as conn:
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (task_id,))
        kb._append_event(conn, task_id, "review_requested", {
            "reviewer": "qa", "ask": "Review <@U123>\nSee /home/alice/private/log",
            "audience": "human",
            "summary": "Verified https://github.com/example/services/pull/42", "artifacts": [],
        })
        conn.commit()
    asyncio.run(deliver(runner, collect(runner)))
    card = client.calls[0][1]["text"]
    assert "Review &lt;@U123&gt; See [local path]" in card
    assert "<https://github.com/example/services/pull/42|PR #42>" in card
    assert "Your review needed" in card
    kwargs = runner._deliver_kanban_artifacts.await_args.kwargs
    assert kwargs["metadata"]["thread_ts"] == "100.000001"


def test_failed_history_retries_keep_checkpointed_root(tmp_path, monkeypatch):
    runner, client, task_id = setup(tmp_path, monkeypatch)
    original = client.chat_postMessage
    fail_history = True

    async def post(**kwargs):
        nonlocal fail_history
        if kwargs.get("thread_ts") and fail_history:
            fail_history = False
            raise RuntimeError("temporary send failure")
        return await original(**kwargs)

    client.chat_postMessage = post
    change(task_id, "review")
    change(task_id, "done")
    asyncio.run(deliver(runner, collect(runner)))
    assert [method for method, _ in client.calls] == ["post", "update"]
    retry = collect(runner)
    assert retry
    asyncio.run(deliver(runner, retry))
    posts = [kwargs for method, kwargs in client.calls if method == "post"]
    assert len(posts) == 2
    assert "thread_ts" not in posts[0]
    assert posts[1]["thread_ts"] == "100.000001"
    assert not collect(runner)


def test_claimed_current_state_has_a_rendered_headline(tmp_path, monkeypatch):
    runner, client, task_id = setup(tmp_path, monkeypatch)
    change(task_id, "blocked", reason="Old question")
    with kbc.connect() as conn:
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        kb._append_event(conn, task_id, "claimed", {"assignee": "worker"})
        conn.commit()
    asyncio.run(deliver(runner, collect(runner)))
    card = client.calls[0][1]["text"]
    assert "[RUNNING]" in card
    assert "No action needed" in card
    assert "Status → running" not in card
    assert "Ask:" not in card
    assert "gateway.kanban" not in card
    assert "Old question" not in card


def test_live_card_consumes_auxiliary_action_and_preserves_raw_history(tmp_path, monkeypatch):
    import json
    from agent import auxiliary_client
    runner, client, task_id = setup(tmp_path, monkeypatch)
    change(task_id, "running")
    asyncio.run(deliver(runner, collect(runner)))
    response = {"ask": "Log in and run manual Tier tests", "audience": "human",
                "why": "Manual verification remains", "evidence": [], "links": []}
    aux = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(response)))]))
    monkeypatch.setattr(auxiliary_client, "call_llm", aux)
    change(task_id, "blocked", reason="Raw worker evidence: CI green; manual verification remains")
    asyncio.run(deliver(runner, collect(runner)))
    update = [kwargs for method, kwargs in client.calls if method == "update"][-1]
    assert "Log in and run manual Tier tests" in update["text"]
    assert "Raw worker evidence" not in update["text"]
    reply = client.calls[-1][1]
    assert reply["thread_ts"] == "100.000001"
    assert "Raw worker evidence" in reply["text"]
    assert aux.call_count == 1


def test_summarizer_timeout_delivers_raw_card_and_checkpoints_event(tmp_path, monkeypatch):
    import threading
    from agent import auxiliary_client
    from gateway import kanban_notify_summarizer as summaries
    runner, client, task_id = setup(tmp_path, monkeypatch)
    release = threading.Event()
    finished = threading.Event()

    def stalled(**kwargs):
        try:
            release.wait(10)
            raise RuntimeError("provider ignored timeout")
        finally:
            finished.set()

    aux = Mock(side_effect=stalled)
    monkeypatch.setattr(auxiliary_client, "call_llm", aux)
    monkeypatch.setattr(summaries, "TIMEOUT_SECONDS", 0.05)
    change(task_id, "blocked", reason="Choose a region; keep Draft, do not merge")
    try:
        asyncio.run(deliver(runner, collect(runner)))
        assert len(client.calls) == 1
        assert "Choose a region; keep Draft, do not merge" in client.calls[0][1]["text"]
        assert not collect(runner)
        assert aux.call_count == 1
    finally:
        release.set()
        assert finished.wait(2)
