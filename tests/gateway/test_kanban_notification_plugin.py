"""The documented minimal consumer loads normally and decorates real Slack delivery."""

import asyncio
import shutil
from pathlib import Path

from hermes_cli.plugins import get_plugin_manager
from hermes_constants import get_hermes_home
from tests.gateway.test_kanban_slack_cards import change, collect, deliver, setup


def test_discovered_presentation_plugin_keeps_attention_out_of_root_edits(tmp_path, monkeypatch):
    runner, client, task_id = setup(tmp_path, monkeypatch)
    home = get_hermes_home()
    source = Path(__file__).parents[1] / "plugins" / "fixtures" / "notification-example"
    shutil.copytree(source, home / "plugins" / "notification-example", ignore=shutil.ignore_patterns("__pycache__"))
    (home / "config.yaml").write_text("plugins: {enabled: [notification-example], isolation: in_process}\n")
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(tmp_path / "empty"))
    monkeypatch.delenv("HERMES_ENABLE_PROJECT_PLUGINS", raising=False)
    monkeypatch.chdir(home)
    manager = get_plugin_manager()
    try:
        manager.discover_and_load(force=True)
        change(task_id, "blocked", kind="needs_input", audience="human", ask="Choose a region")
        asyncio.run(deliver(runner, collect(runner)))
        first = client.calls[0][1]
        assert first["text"].startswith("Human input needed:\n")
        block_text = first["blocks"][0]["text"]["text"]
        assert next(iter(runner.adapters.values())).format_message(block_text) == first["text"]
        change(task_id, "blocked", kind="needs_input", audience="human", ask="Choose another region")
        asyncio.run(deliver(runner, collect(runner)))
        edited, history = client.calls[1][1], client.calls[2][1]
        assert "Human input needed:" not in edited["text"]
        assert "Human input needed:" not in edited["blocks"][0]["text"]["text"]
        assert history["text"].startswith("Human input needed: ")
        assert "blocks" not in history
        assert not collect(runner)
    finally:
        manager.unload()
