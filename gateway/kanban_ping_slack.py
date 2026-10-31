"""Compact Slack presentation; wake payloads and other platforms stay independent."""

import json
import re

from agent.i18n import t
from gateway.kanban_notify_summarizer import event_audience

_PR = re.compile(r"(?:https?://)?github\.com/([\w.-]+/[\w.-]+)(?:/pull/|#)(\d+)\b")
_JIRA = re.compile(r"https?://[^\s<>\"'|]+/browse/([A-Z][A-Z0-9]+-\d+)\b")
_CONFLICT = re.compile(r"\bCONFLICTING\b|\bmerge conflicts?\b", re.IGNORECASE)


def _safe(value, limit=160):
    from gateway.kanban_watchers_notifier import _safe_review_reason
    return _safe_review_reason(value, limit)


def _escape(value):
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _links(text):
    """Extract only recognizable PR/Jira references, never invent URLs for bare issue keys."""
    links = {}
    for match in _PR.finditer(text):
        repo, number = match.groups()
        links[f"https://github.com/{repo}/pull/{number}"] = f"PR #{number}"
    for match in _JIRA.finditer(text):
        links[match.group()] = match.group(1)
    return [f"<{_escape(url)}|{label}>" for url, label in list(links.items())[:4]]


def _handoff_headline(payload, role, kind):
    owner = _safe(payload.get(role), 48)
    return kind + ("_agent" if owner else "_human"), {role: _escape(owner)}


def _changes_headline(payload):
    key, values = _handoff_headline(payload, "implementer", "changes_requested")
    reason = _safe(payload.get("reason"), 0)
    pr = _PR.search(reason)
    if values["implementer"] and pr and _CONFLICT.search(reason):
        return "conflict", {**values, "number": pr.group(2)}
    return key, values


def _status_headline(payload):
    status = _safe(payload.get("status"), 48) or "unknown"
    key = "status_attention" if status in {"blocked", "triage"} else "status_fyi"
    return key, {"status": _escape(status)}


_HEADLINE_VARIANTS = {
    "blocked": lambda p: ("blocked" + {"needs_input": "_input", "dependency": "_dependency"}.get(str(p.get("kind")), "_attention"), {}),
    "block_loop_detected": lambda p: ("block_loop_detected_input" if p.get("kind") == "needs_input" else "block_loop_detected_attention", {}),
    "review_requested": lambda p: _handoff_headline(p, "reviewer", "review_requested"),
    "changes_requested": _changes_headline,
    "status": _status_headline,
}


def _headline(ev):
    """An assigned reviewer/implementer owns handoffs; only input gates ask the human."""
    payload = ev.payload or {}
    if event_audience(ev) == "agent" and payload.get("ask"):
        action = t("gateway.kanban.ping.slack.agent_action", value=_escape(_safe(payload["ask"], 200)))
        if ev.kind in {"crashed", "timed_out", "gave_up"}:
            return t("gateway.kanban.ping.slack." + ev.kind) + " · " + action
        return action
    if payload.get("audience") == "human" and ev.kind in {"review_requested", "changes_requested"}:
        return t("gateway.kanban.ping.slack." + ev.kind + "_human")
    variant = _HEADLINE_VARIANTS.get(ev.kind)
    key, values = variant(ev.payload or {}) if variant else (ev.kind, {})
    return t("gateway.kanban.ping.slack." + key, **values)


def format_slack_ping(ev, n):
    """Render outcome, title, context, then a clipped worker handoff."""
    payload = ev.payload or {}
    details = payload.get("why") or payload.get("reason") or payload.get("summary") or payload.get("error")
    if not details and ev.kind == "completed":
        details = getattr(n.task, "result", "")
    # Discover links before clipping prose, including published PRs in structured handoff metadata.
    references = _safe(json.dumps(payload, ensure_ascii=False, default=str), 0)
    references += " " + _safe(getattr(n.task, "body", ""), 0)
    references += " " + _safe(n.title, 0)
    if ev.kind == "completed":
        references += " " + _safe(getattr(n.task, "result", ""), 0)
    context = [_escape(_safe(v, 120)) for v in [n.board_slug, getattr(n.task, "assignee", ""), n.task_id] if v]
    context += _links(references)
    # send() accepts standard Markdown; the Slack adapter converts bold to mrkdwn.
    text = f"{_headline(ev)}\n**{_escape(_safe(n.title, 120))}**\n{' · '.join(context)}"
    if payload.get("ask") and event_audience(ev) == "human":
        text += "\n" + t("gateway.kanban.ping.slack.ask", value=_escape(_safe(payload["ask"], 200)))
    if details:
        # Mobile pings stay short even when notify_reason_max_chars disables clipping for wake detail.
        text += "\n" + t("gateway.kanban.ping.slack.details", value=_escape(_safe(details)))
    return text
