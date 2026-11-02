"""Explicit plugin layouts obey the same Slack payload and fallback boundaries."""

import copy

import pytest

from tests.gateway.test_slack_block_kit_adapter import _make_adapter, SlackRejectedBlocks


@pytest.mark.asyncio
async def test_explicit_layout_overrides_auto_render_and_is_sanitized_without_mutation():
    adapter, client = _make_adapter({"rich_blocks": True})
    blocks = [{"type": "section", "text": {"type": "plain_text", "text": "x" * 4000}}]
    metadata = {"blocks": blocks, "thread_ts": "100.000001"}
    original = copy.deepcopy(metadata)
    result = await adapter.send("C1", "Accessible fallback", metadata=metadata)
    assert result.success
    sent = client.chat_postMessage.await_args.kwargs
    assert sent["text"] == "Accessible fallback"
    assert sent["thread_ts"] == "100.000001"
    assert sent["blocks"][0]["text"]["type"] == "plain_text"
    assert len(sent["blocks"][0]["text"]["text"]) == 3000
    assert metadata == original


@pytest.mark.asyncio
@pytest.mark.parametrize("blocks", [[], [{"type": "section"}]])
async def test_empty_or_invalid_explicit_layout_clears_final_edit_only(blocks):
    adapter, client = _make_adapter({"rich_blocks": True})
    for finalize in (False, True):
        result = await adapter.edit_message("C1", "100.000001", "Fallback", finalize=finalize,
                                            metadata={"blocks": blocks})
        assert result.success
        edited = client.chat_update.await_args.kwargs
        assert edited["text"] == "Fallback"
        assert (edited.get("blocks") == []) if finalize else ("blocks" not in edited)


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", [False, True])
async def test_rejected_explicit_layout_retries_accessible_fallback(edit):
    adapter, client = _make_adapter()
    blocks = [{"type": "divider"}]
    method = client.chat_update if edit else client.chat_postMessage
    method.side_effect = [SlackRejectedBlocks(), {"ts": "100.000001"}]
    if edit:
        result = await adapter.edit_message("C1", "100.000001", "Fallback", finalize=True,
                                            metadata={"blocks": blocks})
    else:
        result = await adapter.send("C1", "Fallback", metadata={"blocks": blocks})
    assert result.success
    first, retry = [call.kwargs for call in method.await_args_list]
    assert first["blocks"] == blocks
    assert retry["text"] == "Fallback"
    assert (retry.get("blocks") == []) if edit else ("blocks" not in retry)


@pytest.mark.asyncio
async def test_split_sends_do_not_repeat_explicit_layout():
    adapter, client = _make_adapter()
    result = await adapter.send("C1", "x" * 41000, metadata={"blocks": [{"type": "divider"}]})
    assert result.success
    assert client.chat_postMessage.await_count > 1
    assert all("blocks" not in call.kwargs for call in client.chat_postMessage.await_args_list)
