"""Only actionable human asks belong on the live card; status is not repeated."""

from types import SimpleNamespace

from gateway.kanban_slack_card import format_card


def render(status, payload):
    task = SimpleNamespace(status=status, title="Task", body="", assignee="builder")
    n = SimpleNamespace(task=task, title="Task", task_id="t_example", board_slug="test")
    ev = SimpleNamespace(id=1, task_id=n.task_id, kind="status", payload={"status": status, **payload}, created_at=0)
    return format_card(n, task, [ev])


def test_no_filler_ask_or_duplicate_running_status():
    text = render("running", {})
    assert "Ask:" not in text
    assert "additional action specified" not in text
    assert text.lower().count("running") == 2  # one chip, one historical trail entry
    assert "Status →" not in text


def test_agent_fallback_does_not_request_human_action():
    text = render("blocked", {"ask": "Rebase the branch", "audience": "agent"})
    assert "Ask:" not in text
    assert "No action needed" in text


def test_human_action_is_displayed_without_raw_evidence():
    text = render("blocked", {"reason": "Many technical details", "ask": "Choose a region", "audience": "human"})
    assert "Ask:** Choose a region" in text
    assert "Many technical details" not in text


def test_live_card_displays_normalized_reason_and_evidence():
    text = render("blocked", {"ask": "Review the change", "audience": "human",
                              "why": "Manual verification remains", "evidence": ["CI green"],
                              "links": ["https://github.com/example/repo/pull/1"]})
    assert "Manual verification remains" in text
    assert "CI green" in text
    assert "<https://github.com/example/repo/pull/1|PR #1>" in text
