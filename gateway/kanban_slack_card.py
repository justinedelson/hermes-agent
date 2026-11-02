"""Durable Slack task cards; subscription metadata owns the root, not process caches."""

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from functools import partial
from types import SimpleNamespace

from agent.i18n import t
from gateway.kanban_notify_summarizer import event_audience
from gateway.kanban_ping_slack import _escape, _headline, _safe, format_slack_ping

logger = logging.getLogger(__name__)

_STATE_KINDS = {
    "claimed": "running", "promoted": "ready", "unblocked": "ready",
    "completed": "done", "review_requested": "review", "blocked": "blocked",
    "changes_requested": "ready", "block_loop_detected": "triage", "archived": "archived",
    "gave_up": "blocked", "scheduled": "scheduled", "specified": "todo",
    "review_reopened": "ready",
}
CARD_KINDS = (*_STATE_KINDS, "status", "created", "crashed", "timed_out", "reclaimed")
_STATUS_HEADLINE_KINDS = {"claimed", "promoted", "created", "scheduled", "specified", "review_reopened", "reclaimed"}
_RETRY_KINDS = {"crashed", "timed_out", "gave_up", "reclaimed"}


def _valid_state_events(events):
    """Ignore legacy settlement after a terminal state, until a real reopen event."""
    previous = None
    for ev in events:
        if previous in {"done", "archived"} and ev.kind in _RETRY_KINDS:
            continue
        state = _event_status(ev)
        if state:
            previous = state
        yield ev


def filter_settled_retries(n, events):
    """Fence queued phantom retries by event order, not today's task status.

    A genuine timeout before completion (or after an explicit reopen) still
    belongs in history. Keep the claim cursor unchanged so skipped events settle.
    """
    if not any(ev.kind in _RETRY_KINDS for ev in events):
        return events
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc

    candidates = {ev.id for ev in events if ev.kind in _RETRY_KINDS}
    with kb.pin_first_board_resolution(), kbc.connect_closing(board=n.board_slug) as conn:
        rows = conn.execute(
            "SELECT * FROM task_events WHERE task_id=? AND id<=? AND kind IN ("
            + ",".join("?" for _ in CARD_KINDS) + ") ORDER BY id",
            (n.task_id, max(candidates), *CARD_KINDS),
        )
        valid = {ev.id for ev in _valid_state_events(kb.Event.from_row(row) for row in rows)
                 if ev.id in candidates}
    return [ev for ev in events if ev.id not in candidates or ev.id in valid]


def uses_slack_card(sub):
    """Conversation threads and explicit opt-outs never edit somebody else's root."""
    metadata = sub.get("delivery_metadata") or {}
    return (str(sub.get("platform") or "").lower() == "slack" and not sub.get("thread_id")
            and not metadata.get("thread_id") and metadata.get("kanban_card") is not False)


def _slack_time(epoch):
    """Slack localizes the token; legacy clients get an unambiguous Pacific fallback."""
    fallback = datetime.fromtimestamp(epoch, ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d %H:%M %Z")
    return f"<!date^{int(epoch)}^{{time}}|{fallback}>"


def _event_status(ev):
    payload = ev.payload or {}
    if ev.kind in {"crashed", "timed_out", "reclaimed"}:
        return payload.get("retry_status") or payload.get("status")
    return payload.get("status") or _STATE_KINDS.get(ev.kind)


def _snapshot(n):
    """Read current task and a bounded state trail from the same pinned board as the claim."""
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc

    kinds = CARD_KINDS
    with kb.pin_first_board_resolution(), kbc.connect_closing(board=n.board_slug) as conn:
        task = kb.get_task(conn, n.task_id)
        rows = conn.execute(
            "SELECT * FROM task_events WHERE task_id=? AND kind IN ("
            + ",".join("?" for _ in kinds) + ") ORDER BY id",
            (n.task_id, *kinds),
        )
        return task, _compact_events(kb.Event.from_row(row) for row in rows)


def _compact_events(events):
    """Stream state-only rows; keep first milestones, latest payloads and latest entry times.

    A fixed tail drops creation/long-lived state entry after enough retry churn.
    Retention here scales with distinct states, not the number of runs or heartbeats.
    """
    first, latest, entries = {}, {}, {}
    previous = None
    for ev in _valid_state_events(events):
        state = _event_status(ev)
        if not state:
            continue
        first.setdefault("created" if ev.kind == "created" else state, ev)
        latest[state] = ev
        if state != previous:
            entries[state] = ev
        previous = state
    retained = {ev.id: ev for group in (first, latest, entries) for ev in group.values()}
    return [retained[key] for key in sorted(retained)]


def _state_timing(task, events, now):
    """Repeated same-state events do not reset the clock; re-entry does."""
    since = getattr(task, "created_at", now)
    for ev in reversed(events):
        state = _event_status(ev)
        if state and state != task.status:
            break
        if state:
            since = ev.created_at
    minutes = max(0, int(now - since)) // 60
    if minutes < 60:
        duration = f"{minutes}m"
    elif minutes < 1440:
        duration = f"{minutes // 60}h {minutes % 60}m"
    else:
        duration = f"{minutes // 1440}d {minutes % 1440 // 60}h"
    return t("gateway.kanban.ping.slack.in_state", duration=duration, since=_slack_time(since))


def _milestones(events):
    """First visits only; scheduling churn is not a milestone. Keep creation + last four."""
    milestones = {}
    for ev in events:
        state = "created" if ev.kind == "created" else _event_status(ev)
        if state and state not in {"ready", "todo", "scheduled"}:
            milestones.setdefault(state, ev.created_at)
    entries = list(milestones.items())
    if len(entries) > 5:
        entries = entries[:1] + entries[-4:]
    return " → ".join(f"{_escape(_safe(state, 24))} {_slack_time(epoch)}" for state, epoch in entries)


def current_event(task, events):
    """Select the current-state event, never a stale queued ask."""
    status = task.status if task else "unknown"
    current = next((ev for ev in reversed(events) if _event_status(ev) == status),
                   SimpleNamespace(kind="status", payload={"status": status}))
    if current.kind in _STATUS_HEADLINE_KINDS:
        current = SimpleNamespace(kind="status", payload={"status": status})
    return current


def _task_link(n):
    """Use the subscriber's declared public deployment, never guess a loopback URL."""
    from urllib.parse import urlencode, urlsplit
    from gateway.platforms._shared import get_scoped_secret
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_cli.dashboard_auth.prefix import _normalise_public_url

    config = load_user_config_effective()
    dashboard = config.get("dashboard") or {}
    raw = get_scoped_secret("HERMES_DASHBOARD_PUBLIC_URL", "") or dashboard.get("public_url", "")
    base = _normalise_public_url(raw)
    if not base:
        return _escape(n.task_id)
    parsed = urlsplit(base)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or "|" in base:
        return _escape(n.task_id)
    query = urlencode({"board": n.board_slug or "default", "task": n.task_id})
    return f"<{_escape(base + '/kanban?' + query)}|{_escape(n.task_id)}>"


def format_card(n, task, events, current=None):
    """Use saved current state for the ask; old queued events belong only in history."""
    status = task.status if task else "unknown"
    current = current if current is not None else current_event(task, events)
    context = SimpleNamespace(task=task, title=task.title if task else n.task_id,
                              task_id=n.task_id, board_slug=n.board_slug)
    lines = format_slack_ping(current, context).splitlines()
    payload = current.payload or {}
    ask = payload.get("ask") if event_audience(current) == "human" else None
    ask_line = [t("gateway.kanban.ping.slack.ask", value=_escape(_safe(ask, 200)))] if ask else []
    facts = [payload.get("why"), *(payload.get("evidence") or [])]
    details = _escape(_safe(" · ".join(_safe(value, 200) for value in facts if value), 200))
    detail_line = [t("gateway.kanban.ping.slack.details", value=details)] if details else []

    key = {"blocked": "card_attention", "triage": "card_attention", "review": "card_review"}.get(status, "card_fyi")
    if event_audience(current) == "agent":
        key = "card_fyi"
    headline = t("gateway.kanban.ping.slack." + key)
    if current.kind in {"crashed", "timed_out", "gave_up"} or (event_audience(current) == "agent" and payload.get("ask")):
        headline = _headline(current)
    return "\n".join([
        t("gateway.kanban.ping.slack.card_title", value=_escape(_safe(context.title, 120))),
        f"[{_escape(_safe(status, 24)).upper()}] {headline}",
        *([_state_timing(task, events, time.time())] if task else []),
        *ask_line, lines[2].replace(_escape(n.task_id), _task_link(n), 1), *detail_line,
        t("gateway.kanban.ping.slack.trail", value=_milestones(events)),
    ])


def format_history(ev):
    """One line per delivered event, without repeating the whole card."""
    payload = ev.payload or {}
    detail = payload.get("reason") or payload.get("summary") or payload.get("error") or payload.get("ask")
    timestamp = _slack_time(ev.created_at)
    headline_event = ev
    if ev.kind in _STATUS_HEADLINE_KINDS:
        headline_event = SimpleNamespace(kind="status", payload={"status": _event_status(ev)})
    text = f"{timestamp} · {_headline(headline_event)}"
    if detail:
        text += " · " + _escape(_safe(detail))
    return text


async def send_card_event(n, ev, metadata):
    """Post the first root, then edit + reply; persist the root before the next event in a batch."""
    from gateway.kanban_watchers_common import _to_thread_process_service
    from gateway.kanban_notify_summarizer import enrich_event

    task, events = await _to_thread_process_service(_snapshot, n)
    current = await enrich_event(current_event(task, events), board=n.board_slug)
    card = format_card(n, task, events, current=current)
    root = metadata.get("thread_ts")
    if root:
        try:
            edited = await n.adapter.edit_message(n.sub["chat_id"], str(root), card, finalize=True, metadata=metadata)
            if getattr(edited, "success", True) is False:
                logger.debug("kanban Slack card update failed for %s: %s; delivering history",
                             n.task_id, getattr(edited, "error", None))
        except Exception as exc:
            logger.exception("kanban Slack card update raised for %s: %s; delivering history", n.task_id, exc)
        return await n.adapter.send(n.sub["chat_id"], format_history(ev), metadata=metadata)
    result = await n.adapter.send(n.sub["chat_id"], card, metadata=metadata)
    if getattr(result, "success", True) is not False and getattr(result, "message_id", None):
        metadata["thread_ts"] = result.message_id
        n.sub["delivery_metadata"] = dict(metadata)
        await _to_thread_process_service(partial(
            n.runner._kanban_sub_op, n.board_slug, "add_notify_sub", n.sub,
            delivery_metadata={"thread_ts": result.message_id},
        ))
    return result
