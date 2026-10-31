"""Durable Slack task cards; subscription metadata owns the root, not process caches."""

import logging
from datetime import UTC, datetime
from functools import partial
from types import SimpleNamespace

from agent.i18n import t
from gateway.kanban_ping_slack import _escape, _headline, _safe, format_slack_ping

logger = logging.getLogger(__name__)

_STATE_KINDS = {
    "claimed": "running", "promoted": "ready", "unblocked": "ready",
    "completed": "done", "review_requested": "review", "blocked": "blocked",
    "changes_requested": "ready", "block_loop_detected": "triage", "archived": "archived",
    "gave_up": "blocked",
}


def _event_status(ev):
    payload = ev.payload or {}
    if ev.kind in {"crashed", "timed_out"}:
        return payload.get("retry_status")
    return payload.get("status") if ev.kind in {"status", "created"} else _STATE_KINDS.get(ev.kind)


def _snapshot(n):
    """Read current task and a bounded state trail from the same pinned board as the claim."""
    from hermes_cli import kanban_db as kb, kanban_db_connect as kbc

    kinds = (*_STATE_KINDS, "status", "created", "crashed", "timed_out", "gave_up")
    with kb.pin_first_board_resolution(), kbc.connect_closing(board=n.board_slug) as conn:
        task = kb.get_task(conn, n.task_id)
        rows = conn.execute(
            "SELECT * FROM task_events WHERE task_id=? AND kind IN ("
            + ",".join("?" for _ in kinds) + ") ORDER BY id DESC LIMIT 16",
            (n.task_id, *kinds),
        ).fetchall()
        return task, [kb.Event.from_row(row) for row in reversed(rows)]


def format_card(n, task, events):
    """Use saved current state for the ask; old queued events belong only in history."""
    status = task.status if task else "unknown"
    current = next((ev for ev in reversed(events) if _event_status(ev) == status),
                   SimpleNamespace(kind="status", payload={"status": status}))
    if current.kind in {"claimed", "promoted", "created"}:
        current = SimpleNamespace(kind="status", payload={"status": status})
    context = SimpleNamespace(task=task, title=task.title if task else n.task_id,
                              task_id=n.task_id, board_slug=n.board_slug)
    lines = format_slack_ping(current, context).splitlines()
    payload = current.payload or {}
    ask = payload.get("ask") or payload.get("reason") or payload.get("summary") or payload.get("error")
    if not ask and status == "done":
        ask = getattr(task, "result", "")
    ask = _escape(_safe(ask, 200)) if ask else t("gateway.kanban.ping.slack.no_ask")
    trail = []
    last_status = None
    for ev in events:
        state = _event_status(ev)
        if not state or state == last_status:
            continue
        timestamp = datetime.fromtimestamp(ev.created_at, UTC).strftime("%H:%M")
        trail.append(f"{_escape(_safe(state, 24))} {timestamp}")
        last_status = state
    return "\n".join([
        lines[1], f"[{_escape(_safe(status, 24)).upper()}] {lines[0]}",
        t("gateway.kanban.ping.slack.ask", value=ask), lines[2],
        t("gateway.kanban.ping.slack.trail", value=" → ".join(trail[-8:])),
    ])


def format_history(ev):
    """One line per delivered event, without repeating the whole card."""
    payload = ev.payload or {}
    detail = payload.get("ask") or payload.get("reason") or payload.get("summary") or payload.get("error")
    timestamp = datetime.fromtimestamp(ev.created_at, UTC).strftime("%H:%M")
    text = f"{timestamp} UTC · {_headline(ev)}"
    if detail:
        text += " · " + _escape(_safe(detail))
    return text


async def send_card_event(n, ev, metadata):
    """Post the first root, then edit + reply; persist the root before the next event in a batch."""
    from gateway.kanban_watchers_common import _to_thread_process_service

    task, events = await _to_thread_process_service(_snapshot, n)
    card = format_card(n, task, events)
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
