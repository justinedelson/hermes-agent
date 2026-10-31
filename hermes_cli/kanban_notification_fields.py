"""Optional, validated action ownership for durable Kanban event payloads."""


def notification_fields(ask=None, audience=None):
    """Validate before mutation; redact action text before durable storage."""
    from agent.redact import redact_sensitive_text

    fields = {}
    if ask is not None:
        if not isinstance(ask, str) or not ask.strip() or len(ask) > 200:
            raise ValueError("ask must be a nonblank string of at most 200 characters")
        fields["ask"] = redact_sensitive_text(ask.strip(), force=True, redact_url_credentials=True)[:200]
    if audience is not None:
        if audience not in ("human", "agent"):
            raise ValueError("audience must be human or agent")
        fields["audience"] = audience
    return fields


def route_block(kind, reason, source_status, *, prev_kind, prev_recurrences):
    """Choose the lane and audit payload without weakening dependency or loop gating.

    Dependencies wait in todo, never in the human blocked bucket. A repeated
    same-kind block after unblock counts a recurrence; the limit routes to
    triage. This is distinct from dispatcher consecutive failure accounting.
    """
    from hermes_cli.kanban_db import BLOCK_RECURRENCE_LIMIT

    payload = {"reason": reason, "kind": kind, "source_status": source_status}
    if kind == "dependency":
        return "todo", "dependency_wait", "block_kind    = ?", (kind,), payload
    recurrences = prev_recurrences + 1 if prev_kind == kind else 1
    set_sql = "block_kind    = ?,\n                       block_recurrences = ?"
    payload = {"reason": reason, "kind": kind, "recurrences": recurrences, "source_status": source_status}
    if recurrences >= BLOCK_RECURRENCE_LIMIT:
        payload["limit"] = BLOCK_RECURRENCE_LIMIT
        return "triage", "block_loop_detected", set_sql, (kind, recurrences), payload
    return "blocked", "blocked", set_sql, (kind, recurrences), payload
