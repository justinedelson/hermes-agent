"""Generic notifier presentation hooks; policy and scheduling live in consumers."""
import copy
import logging
from functools import partial

logger = logging.getLogger(__name__)


async def prepare_notification(n, ev):
    """First valid attention_text wins; presentation failures never veto delivery."""
    from hermes_cli.lifecycle import ainvoke_hook
    try:
        results = await ainvoke_hook(
            "kanban_notify_prepare", task_id=n.task_id, board=n.board_slug,
            platform=n.platform_str, event_id=ev.id, event_kind=ev.kind,
            event_payload=copy.deepcopy(ev.payload or {}),
            task_title=n.title, subscription=copy.deepcopy(n.sub),
        )
        for result in results:
            if isinstance(result, dict) and isinstance(result.get("attention_text"), str):
                if isinstance(result.get("diagnostic"), bool):
                    n.notification_diagnostic = result["diagnostic"]
                return result["attention_text"][:200]
    except Exception:
        logger.exception("Kanban notification preparation hook failed for %s", n.task_id)
    return ""


def _scoped_adapter(runner, profile, subscription):
    """Revalidate the durable route and owner at send time, not only at enqueue."""
    from gateway.config import Platform
    from gateway.kanban_watchers_notifier import _adapter_for_subscription
    if (subscription.get("notifier_profile") or profile) != profile:
        return None
    try:
        platform = Platform(subscription.get("platform"))
    except ValueError:
        return None
    return _adapter_for_subscription(runner, platform, subscription, profile)


async def notification_tick(runner):
    """Run background consumers even on idle ticks, bound to every served profile."""
    from hermes_cli.lifecycle import ainvoke_hook, has_hook
    from gateway.run import _async_profile_runtime_scope, _multiplex_profile_homes
    from hermes_constants import get_hermes_home

    profile = runner._kanban_notifier_profile
    homes = (_multiplex_profile_homes(runner.config) if runner.config.multiplex_profiles
             else [(profile, get_hermes_home())])
    for owner, home in homes:
        try:
            async with _async_profile_runtime_scope(home):
                if has_hook("kanban_notify_tick"):
                    await ainvoke_hook("kanban_notify_tick", resolve_adapter=partial(_scoped_adapter, runner, owner))
        except Exception:
            logger.exception("Kanban notification tick hook failed for profile %s", owner)
