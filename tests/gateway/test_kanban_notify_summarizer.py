"""Auxiliary notification summaries are optional and never hold delivery hostage."""

import asyncio
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent import auxiliary_client

from gateway import kanban_notify_summarizer as summaries


@pytest.fixture(autouse=True)
def empty_cache():
    summaries._CACHE.clear()


def event(event_id=1, **payload):
    return SimpleNamespace(id=event_id, task_id="t_example", kind="blocked", created_at=0,
                           payload={"reason": "Manual tests need IMS login. Keep Draft; do not merge.", **payload})


def test_legacy_reason_is_summarized_once_per_event(monkeypatch):
    from gateway import kanban_notify_summarizer as summaries

    summaries._CACHE.clear()
    client = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
        "ask": "Log in and run the manual tests; keep Draft, do not merge", "audience": "human",
        "why": "IMS login required", "evidence": ["CI green"], "links": [],
    })))]))
    monkeypatch.setattr(auxiliary_client, "call_llm", client)

    async def run():
        first = await summaries.enrich_event(event(), board="test")
        second = await summaries.enrich_event(event(), board="test")
        assert first.payload["ask"].startswith("Log in")
        assert second.payload == first.payload
        assert first.payload["reason"] == event().payload["reason"]
        assert client.call_count == 1
        assert client.call_args.kwargs["task"] == "kanban_notify_summarizer"
        assert client.call_args.kwargs["timeout"] == 8
    asyncio.run(run())


@pytest.mark.parametrize("reply", ["not json", "{}", '{"ask": 4}', None])
def test_invalid_aux_reply_keeps_clipped_raw_reason(monkeypatch, reply):
    client = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))]))
    monkeypatch.setattr(auxiliary_client, "call_llm", client)
    ev = event(reason="Need operator access " * 40)
    result = asyncio.run(summaries.enrich_event(ev, board="test"))
    assert len(result.payload["ask"]) <= 200
    assert result.payload["ask"].startswith("Need operator access")
    assert result.payload["audience"] == "human"
    assert ev.payload.get("ask") is None


def test_timeout_returns_without_waiting_for_provider_and_caches_failure(monkeypatch):
    release = threading.Event()
    finished = threading.Event()
    def stalled(**kwargs):
        try:
            release.wait(10)
            raise RuntimeError("late provider failure")
        finally:
            finished.set()
    monkeypatch.setattr(auxiliary_client, "call_llm", stalled)
    monkeypatch.setattr(summaries, "TIMEOUT_SECONDS", 0.05)
    started = time.monotonic()
    try:
        first = asyncio.run(summaries.enrich_event(event(), board="test"))
        assert time.monotonic() - started < 2
        second = asyncio.run(summaries.enrich_event(event(), board="test"))
        assert first.payload == second.payload
    finally:
        release.set()
        assert finished.wait(2)


@pytest.mark.parametrize("kind", ["gave_up", "block_loop_detected"])
@pytest.mark.parametrize("reply", [None, json.dumps({"ask": "Repair the environment", "audience": "agent",
                                                    "why": "Retries exhausted", "evidence": [], "links": []})])
def test_exhausted_retry_fallback_requires_human_even_with_old_retry_status(monkeypatch, kind, reply):
    client = Mock(side_effect=RuntimeError("offline")) if reply is None else Mock(
        return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply))]))
    monkeypatch.setattr(auxiliary_client, "call_llm", client)
    ev = event(retry_status="ready")
    ev.kind = kind
    result = asyncio.run(summaries.enrich_event(ev, board="test"))
    assert result.payload["audience"] == "human"


def test_saved_ask_skips_auxiliary_client(monkeypatch):
    client = Mock(side_effect=AssertionError("must not call aux"))
    monkeypatch.setattr(auxiliary_client, "call_llm", client)
    ev = event(ask="Choose a region", audience="human")
    assert asyncio.run(summaries.enrich_event(ev, board="test")) is ev
    client.assert_not_called()


def test_concurrent_subscriptions_share_one_auxiliary_call(monkeypatch):
    client = Mock(side_effect=RuntimeError("unavailable"))
    monkeypatch.setattr(auxiliary_client, "call_llm", client)
    async def run():
        results = await asyncio.gather(*(summaries.enrich_event(event(), board="test") for _ in range(3)))
        assert all(result.payload == results[0].payload for result in results)
    asyncio.run(run())
    assert client.call_count == 1


def test_cache_resolution_failure_never_prevents_delivery(monkeypatch):
    from hermes_cli import kanban_db as kb
    monkeypatch.setattr(kb, "kanban_db_path", Mock(side_effect=RuntimeError("cannot resolve board")))
    ev = event(reason="Need operator access " * 40)
    result = asyncio.run(summaries.enrich_event(ev, board="test"))
    assert result.payload["ask"].startswith("Need operator access")
    assert len(result.payload["ask"]) <= 200
    assert result.payload["reason"] == ev.payload["reason"]


def test_real_config_loader_exposes_new_auxiliary_route(tmp_path, monkeypatch):
    from hermes_cli.config import atomic_config_write
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_constants import get_hermes_home
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    atomic_config_write(home / "config.yaml", {"auxiliary": {"kanban_notify_summarizer": {
        "provider": "copilot", "model": "notification-model", "timeout": 3,
    }}})
    cfg = load_user_config_effective(get_hermes_home() / "config.yaml")
    assert cfg["auxiliary"]["kanban_notify_summarizer"]["model"] == "notification-model"
    assert auxiliary_client._get_auxiliary_task_config("kanban_notify_summarizer")["provider"] == "copilot"


def test_needs_input_kind_cannot_be_reclassified_as_agent(monkeypatch):
    payload = {"ask": "Choose a region", "audience": "agent", "why": "Decision needed", "evidence": [], "links": []}
    client = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))]))
    monkeypatch.setattr(auxiliary_client, "call_llm", client)
    result = asyncio.run(summaries.enrich_event(event(kind="needs_input"), board="test"))
    assert result.payload["audience"] == "human"


def test_same_event_id_isolated_by_profile_and_board(tmp_path, monkeypatch):
    from hermes_constants import get_hermes_home, set_hermes_home_override, reset_hermes_home_override
    seen = []
    def client(**kwargs):
        seen.append(str(get_hermes_home()))
        raise RuntimeError("offline")
    monkeypatch.setattr(auxiliary_client, "call_llm", client)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    for profile, board in [("one", "alpha"), ("one", "beta"), ("two", "alpha")]:
        token = set_hermes_home_override(tmp_path / profile)
        try:
            asyncio.run(summaries.enrich_event(event(), board=board))
        finally:
            reset_hermes_home_override(token)
    assert seen == [str(tmp_path / "one"), str(tmp_path / "one"), str(tmp_path / "two")]


def test_invented_links_are_discarded_and_secrets_redacted(monkeypatch):
    secret = "ghp_" + "A" * 40
    valid = "https://github.com/example/repo/pull/1"
    payload = {"ask": f"Rotate token {secret}", "audience": "human", "why": "Manual access needed",
               "evidence": [f"token={secret}"], "links": [valid, "https://invented.example/approval"]}
    client = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))]))
    monkeypatch.setattr(auxiliary_client, "call_llm", client)
    result = asyncio.run(summaries.enrich_event(event(reason=f"Review {valid}; token={secret}"), board="test"))
    assert result.payload["links"] == [valid]
    assert secret not in result.payload["ask"]
    assert secret not in json.dumps(result.payload["evidence"])
    assert secret not in client.call_args.kwargs["messages"][1]["content"]
