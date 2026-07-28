"""Tests for the Pterodactyl HITL hard gate."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from deerflow.community.pterodactyl.guard import GUARD_BLOCK_KEY, PterodactylGuardMiddleware
from deerflow.community.pterodactyl.mutations import confirm_marker, confirmation_token


def _tool_call(name, args, call_id="c1"):
    return {"name": name, "args": args, "id": call_id}


def _request(name, args):
    class _Req:
        tool_call = _tool_call(name, args)
        state: dict = {"messages": []}

    req = _Req()
    return req


def _ask_clarification_ai(question):
    return AIMessage(content="", tool_calls=[{"name": "ask_clarification", "args": {"question": question, "clarification_type": "risk_confirmation"}, "id": "ask1"}])


def _reply(value, request_id="req1", source="ask_clarification"):
    return HumanMessage(
        content=value,
        additional_kwargs={
            "human_input_response": {
                "version": 1,
                "kind": "human_input_response",
                "source": source,
                "request_id": request_id,
                "response_kind": "text",
                "value": value,
            }
        },
    )


def _executed_mutation(name):
    return ToolMessage(content="done", tool_call_id="x", name=name)


async def _run(guard, request):
    async def handler(_req):
        return ToolMessage(content="EXECUTED", tool_call_id="c1", name=request.tool_call["name"])

    return await guard.awrap_tool_call(request, handler)


GUARD = PterodactylGuardMiddleware()


@pytest.mark.anyio
async def test_read_only_tool_passes_through():
    req = _request("pterodactyl_get_resources", {"server_id": "a"})
    result = await _run(GUARD, req)
    assert result.content == "EXECUTED"


@pytest.mark.anyio
async def test_mutating_tool_blocked_without_confirmation():
    req = _request("pterodactyl_power_action", {"server_id": "a", "signal": "restart"})
    result = await _run(GUARD, req)
    assert result.status == "error"
    assert result.additional_kwargs[GUARD_BLOCK_KEY] is True
    # Block instructs the model with the exact bound marker.
    token = confirmation_token("pterodactyl_power_action", {"server_id": "a", "signal": "restart"})
    assert confirm_marker(token) in result.content


@pytest.mark.anyio
async def test_mutating_tool_allowed_after_affirmative_confirmation():
    args = {"server_id": "a", "signal": "restart"}
    token = confirmation_token("pterodactyl_power_action", args)
    req = _request("pterodactyl_power_action", args)
    req.state = {
        "messages": [
            _ask_clarification_ai(f"Restart server a? {confirm_marker(token)}"),
            _reply("确认执行"),
        ]
    }
    result = await _run(GUARD, req)
    assert result.content == "EXECUTED"


@pytest.mark.anyio
async def test_negative_reply_keeps_blocking():
    args = {"server_id": "a", "signal": "restart"}
    token = confirmation_token("pterodactyl_power_action", args)
    req = _request("pterodactyl_power_action", args)
    req.state = {"messages": [_ask_clarification_ai(f"Restart? {confirm_marker(token)}"), _reply("取消")]}
    result = await _run(GUARD, req)
    assert result.status == "error"


@pytest.mark.anyio
async def test_confirmation_for_different_operation_does_not_authorize():
    """A confirmation bound to restart must not unlock a delete."""
    restart_token = confirmation_token("pterodactyl_power_action", {"server_id": "a", "signal": "restart"})
    req = _request("pterodactyl_delete_file", {"server_id": "a", "file_path": "/x"})
    req.state = {
        "messages": [
            _ask_clarification_ai(f"Restart? {confirm_marker(restart_token)}"),
            _reply("确认"),
        ]
    }
    result = await _run(GUARD, req)
    assert result.status == "error"


@pytest.mark.anyio
async def test_single_consume_blocks_second_mutation_after_execution():
    """Once a mutation executed under an approval, that approval is spent."""
    args = {"server_id": "a", "signal": "restart"}
    token = confirmation_token("pterodactyl_power_action", args)
    req = _request("pterodactyl_power_action", args)
    req.state = {
        "messages": [
            _ask_clarification_ai(f"Restart? {confirm_marker(token)}"),
            _reply("确认"),
            _executed_mutation("pterodactyl_power_action"),  # already ran once
        ]
    }
    result = await _run(GUARD, req)
    assert result.status == "error"


@pytest.mark.anyio
async def test_guard_block_message_does_not_count_as_execution():
    """A prior guard-block ToolMessage must not spend the approval."""
    args = {"server_id": "a", "signal": "restart"}
    token = confirmation_token("pterodactyl_power_action", args)
    prior_block = ToolMessage(
        content="BLOCKED",
        tool_call_id="p",
        name="pterodactyl_power_action",
        status="error",
        additional_kwargs={GUARD_BLOCK_KEY: True},
    )
    req = _request("pterodactyl_power_action", args)
    req.state = {
        "messages": [
            prior_block,
            _ask_clarification_ai(f"Restart? {confirm_marker(token)}"),
            _reply("确认"),
        ]
    }
    result = await _run(GUARD, req)
    assert result.content == "EXECUTED"


@pytest.mark.anyio
async def test_uses_nearest_clarification_before_reply():
    """The reply binds to the most recent preceding clarification, not an earlier one."""
    args = {"server_id": "a", "signal": "restart"}
    token = confirmation_token("pterodactyl_power_action", args)
    other_token = confirmation_token("pterodactyl_delete_file", {"server_id": "a", "file_path": "/x"})
    req = _request("pterodactyl_power_action", args)
    req.state = {
        "messages": [
            _ask_clarification_ai(f"Delete file? {confirm_marker(other_token)}"),
            _reply("取消"),  # declined the delete
            _ask_clarification_ai(f"Restart? {confirm_marker(token)}"),
            _reply("确认"),  # approved the restart
        ]
    }
    result = await _run(GUARD, req)
    assert result.content == "EXECUTED"
