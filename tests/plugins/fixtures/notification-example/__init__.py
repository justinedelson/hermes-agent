"""Minimal presentation consumer: no board mutations, actions or external service."""


def prepare(platform, event_payload):
    if platform == "slack" and event_payload.get("audience") == "human":
        return {"attention_text": "Human input needed:"}


def render(text):
    return {"blocks": [{
        "type": "section", "text": {"type": "plain_text", "text": text[:2900]},
    }]}


def register(ctx):
    ctx.register_hook("kanban_notify_prepare", prepare)
    ctx.register_hook("kanban_card_render", render)
