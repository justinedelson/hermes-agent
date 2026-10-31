"""Action-first Slack pings keep human decisions separate from worker handoffs."""

from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.kanban_watchers_notifier import _KanbanNotification


def notification(platform="slack", **task_fields):
    task = SimpleNamespace(title="Ship the service", assignee="backend-engineer", result="", **task_fields)
    return _KanbanNotification(None, {
        "sub": {"task_id": "task-example", "platform": platform, "chat_id": "channel"},
        "task": task, "board": "engineering",
    }, platform_cls=Platform, sub_fail_counts={})


def event(kind, /, **payload):
    return SimpleNamespace(kind=kind, payload=payload)


def render(n, ev):
    from plugins.platforms.slack.adapter import SlackAdapter

    text = n.format_event(ev)
    return SlackAdapter.format_message(SlackAdapter.__new__(SlackAdapter), text) if text else text


def test_completion_puts_outcome_before_title_and_context():
    n = notification()
    text = render(n, event("completed", summary="Deployed and verified."))
    assert text is not None
    lines = text.splitlines()
    assert lines[0] == "✔ *Completed — no action needed*"
    assert lines[1] == "*Ship the service*"
    assert lines[2] == "engineering · backend-engineer · task-example"
    assert lines[3] == "*Details*: Deployed and verified."
    assert n.wake_handoff == "Deployed and verified."


@pytest.mark.parametrize("kind,payload,headline", [
    ("blocked", {"kind": "needs_input"}, "⏸ *Blocked — your input needed*"),
    ("blocked", {"kind": "dependency"}, "⏸ *Blocked — waiting on a dependency; no action needed*"),
    ("blocked", {"kind": "capability"}, "⏸ *Blocked — operator attention needed*"),
    ("blocked", {"kind": "transient"}, "⏸ *Blocked — operator attention needed*"),
    ("blocked", {}, "⏸ *Blocked — operator attention needed*"),
    ("gave_up", {"failures": 3}, "⛔ *Retries exhausted — operator must fix the cause and unblock*"),
    ("crashed", {}, "✖ *Worker crashed — automatic retry; no action needed*"),
    ("timed_out", {"limit_seconds": 120}, "⏱ *Worker timed out — automatic retry; no action needed*"),
    ("status", {"status": "running"}, "🔄 *Status → running — no action needed*"),
    ("status", {"status": "blocked"}, "🔄 *Status → blocked — operator attention needed*"),
    ("review_requested", {}, "👀 *Ready for review — your review needed*"),
    ("review_requested", {"reviewer": "qa-engineer"}, "👀 *Ready for review — qa-engineer reviewing; no action needed*"),
    ("changes_requested", {"implementer": "backend-engineer"}, "↩️ *Changes requested — sent back to backend-engineer; no action needed*"),
    ("changes_requested", {}, "↩️ *Changes requested — operator must assign an implementer*"),
    ("block_loop_detected", {"kind": "needs_input"}, "🛑 *Repeated blocks → triage — your decision needed*"),
    ("block_loop_detected", {}, "🛑 *Repeated blocks → triage — operator attention needed*"),
])
def test_event_states_who_acts_before_task_context(kind, payload, headline):
    text = render(notification(), event(kind, **payload))
    assert text is not None
    lines = text.splitlines()
    assert lines[0] == headline
    assert lines[1] == "*Ship the service*"
    assert lines[2] == "engineering · backend-engineer · task-example"


@pytest.mark.parametrize("kind", ["archived", "unblocked", "claimed"])
def test_bookkeeping_remains_silent(kind):
    assert notification().format_event(event(kind)) is None


@pytest.mark.parametrize("reference", [
    "github.com/example/services#958",
    "https://github.com/example/services/pull/958",
])
def test_conflict_bounce_is_fyi_with_pr_link(reference):
    n = notification()
    reason = f"WAKE (pr-watch, round 1): PR {reference} is CONFLICTING against main @ a6fdf6c3474c — resolve per the WAKE comment on the card."
    text = render(n, event("changes_requested", reason=reason,
                                reviewer="platform-engineer", implementer="backend-engineer"))
    assert text is not None
    assert text.splitlines()[0] == "↩️ *Merge conflict on PR #958 — sent back to backend-engineer to rebase; no action needed*"
    assert "<https://github.com/example/services/pull/958|PR #958>" in text.splitlines()[2]
    assert "review requested changes/BLOCK" not in text
    assert n.wake_review_detail.startswith("WAKE (pr-watch")


def test_context_extracts_links_before_details_clip_and_escapes_input():
    n = notification()
    n.title = "Deploy <production> & verify"
    text = render(n, event("blocked", kind="needs_input", reason="Approve " + "x" * 200,
        summary="https://github.com/example/services/pull/42 https://issues.example.com/browse/OPS-12",
        error="See /home/alice/private/log token=«redacted:sk-…»"))
    assert text is not None
    lines = text.splitlines()
    assert lines[1] == "*Deploy &lt;production&gt; &amp; verify*"
    assert "<https://github.com/example/services/pull/42|PR #42>" in lines[2]
    assert "<https://issues.example.com/browse/OPS-12|OPS-12>" in lines[2]
    assert lines[3].startswith("*Details*: Approve ")
    assert lines[3].endswith("…")
    assert len(lines[3]) <= 172


def test_details_are_path_safe_and_no_stale_result_for_failure():
    n = notification()
    n.task.result = "Old completion"
    text = render(n, event("crashed", error="See /Users/alice/private/log <@U123> & token=«redacted:sk-…»"))
    assert text is not None
    assert "[local path]" in text
    assert "&lt;@U123&gt; &amp;" in text
    assert "/Users/alice" not in text
    assert "Old completion" not in text


def test_slack_adapter_preserves_bold_and_links_in_ping():
    from plugins.platforms.slack.adapter import SlackAdapter

    msg = notification().format_event(event("completed", summary="https://github.com/example/services/pull/42"))
    assert msg is not None
    rendered = SlackAdapter.format_message(SlackAdapter.__new__(SlackAdapter), msg)
    assert rendered.startswith("✔ *Completed — no action needed*\n*Ship the service*\n")
    assert "<https://github.com/example/services/pull/42|PR #42>" in rendered


def test_context_includes_task_links_when_event_has_no_links():
    n = notification(body="Track https://issues.example.com/browse/OPS-12 and https://github.com/example/services/pull/42")
    text = render(n, event("status", status="review"))
    assert text is not None
    assert "<https://github.com/example/services/pull/42|PR #42>" in text.splitlines()[2]
    assert "<https://issues.example.com/browse/OPS-12|OPS-12>" in text.splitlines()[2]


def test_link_context_is_deduplicated_and_bounded():
    refs = " ".join(f"https://github.com/example/services/pull/{i}" for i in range(10))
    text = render(notification(), event("completed", summary=refs, metadata={"published_pr": refs}))
    assert text is not None
    assert text.splitlines()[2].count("|PR #") == 4


def test_completion_legacy_result_and_unclipped_wake_do_not_expand_details(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"kanban": {"notify_reason_max_chars": 0}})
    n = notification()
    n.task.result = "Verified legacy deployment."
    assert "Verified legacy deployment." in (render(n, event("completed")) or "")
    text = render(n, event("changes_requested", reason="x" * 1000, implementer="backend-engineer"))
    assert text is not None
    assert len(n.wake_review_detail) == 1000
    assert len(text.splitlines()[3]) < 180


def test_external_ping_redacts_secrets_and_url_credentials():
    token = "sk-" + "a1b2c3d4" * 6
    text = render(notification(), event("blocked", kind="needs_input",
        reason=f"token={token} https://alice:password123@issues.example.com/browse/OPS-12"))
    assert text is not None
    assert token not in text
    assert "password123" not in text
