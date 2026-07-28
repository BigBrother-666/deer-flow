"""Human-in-the-loop hard gate for Pterodactyl mutating tools.

A mutating tool (see ``mutations.MUTATING_TOOLS``) only executes when the
message history contains a *recent, affirmative* ``ask_clarification`` reply
whose question embedded this exact operation's confirmation marker, and no
mutating tool has executed since that reply (single-consume). Otherwise the
call is blocked with a recoverable ToolMessage that instructs the model to ask
for confirmation first — that ``ask_clarification`` call is the real interrupt
point (handled by ``ClarificationMiddleware``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

from deerflow.agents.human_input import read_human_input_response

from .mutations import confirm_marker, confirmation_token, is_mutating

logger = logging.getLogger(__name__)

# Guard-generated block messages carry this marker so they are never mistaken
# for an executed mutation when enforcing single-consume.
GUARD_BLOCK_KEY = "pterodactyl_guard_block"

_AFFIRMATIVE = ("确认", "确定", "同意", "批准", "允许", "执行", "confirm", "yes", "approve", "proceed", "ok")
_NEGATIVE = ("取消", "拒绝", "不要", "否", "停止", "cancel", "no", "reject", "deny", "abort", "stop")


def _is_affirmative(value: str) -> bool:
    """Heuristic affirmative check; negatives take precedence over affirmatives."""
    text = value.strip().lower()
    if not text:
        return False
    if any(word in text for word in _NEGATIVE):
        return False
    return any(word in text for word in _AFFIRMATIVE)


def _messages(state: Any) -> list:
    if isinstance(state, dict):
        return state.get("messages") or []
    return getattr(state, "messages", None) or []


def _find_clarification_question(messages: list) -> str | None:
    """Return the text of the most recent ask_clarification in ``messages``.

    ``messages`` is the history up to and including the reply, so the nearest
    preceding ask_clarification is the question the human actually answered.
    Its question/context is where the confirmation marker lives.
    """
    for msg in reversed(messages):
        if not isinstance(msg, AIMessage):
            continue
        for call in msg.tool_calls or []:
            if call.get("name") != "ask_clarification":
                continue
            args = call.get("args") or {}
            parts = [str(args.get("question") or ""), str(args.get("context") or "")]
            return "\n".join(parts)
    return None


def _has_valid_approval(messages: list, token: str) -> bool:
    """True if a recent affirmative reply authorizes this exact operation token.

    Walks history newest-first. Finds the latest affirmative human_input_response
    from ask_clarification whose paired question embedded this token's marker.
    Enforces single-consume: if any mutating tool executed *after* that reply,
    the approval is spent and no longer valid.
    """
    marker = confirm_marker(token)

    # Index of the authorizing reply, if any.
    reply_index: int | None = None
    for idx in range(len(messages) - 1, -1, -1):
        msg = messages[idx]
        if not isinstance(msg, HumanMessage):
            continue
        response = read_human_input_response(getattr(msg, "additional_kwargs", None))
        if response is None or response.get("source") != "ask_clarification":
            continue
        if not _is_affirmative(str(response.get("value") or "")):
            continue
        question = _find_clarification_question(messages[: idx + 1])
        if question and marker in question:
            reply_index = idx
            break

    if reply_index is None:
        return False

    # Single-consume: reject if a real mutating tool ran after the approval.
    for msg in messages[reply_index + 1 :]:
        if not isinstance(msg, ToolMessage):
            continue
        if (msg.additional_kwargs or {}).get(GUARD_BLOCK_KEY):
            continue
        if is_mutating(msg.name):
            return False
    return True


class PterodactylGuardMiddleware(AgentMiddleware):
    """Blocks mutating Pterodactyl tools until a human confirms the exact op."""

    def _build_block(self, request: ToolCallRequest) -> ToolMessage:
        name = request.tool_call.get("name", "")
        args = request.tool_call.get("args") or {}
        token = confirmation_token(name, args)
        marker = confirm_marker(token)
        summary = ", ".join(f"{k}={v!r}" for k, v in args.items()) or "(no arguments)"
        content = (
            f"BLOCKED: '{name}' is a state-changing operation and requires explicit "
            f"human confirmation before it can run.\n"
            f"Operation: {name}({summary})\n\n"
            f"To proceed, call ask_clarification with "
            f'clarification_type="risk_confirmation", describe this exact operation, '
            f"and include this marker verbatim in your question so the confirmation "
            f"is bound to it: {marker}\n"
            f"After the user confirms, re-issue '{name}' with the SAME arguments. "
            f"If the user declines, do not retry."
        )
        return ToolMessage(
            content=content,
            tool_call_id=request.tool_call.get("id", ""),
            name=name,
            status="error",
            additional_kwargs={GUARD_BLOCK_KEY: True},
        )

    def _gate(self, request: ToolCallRequest) -> ToolMessage | None:
        name = request.tool_call.get("name")
        if not is_mutating(name):
            return None
        token = confirmation_token(name, request.tool_call.get("args") or {})
        if _has_valid_approval(_messages(request.state), token):
            logger.info("Pterodactyl guard: confirmed '%s', allowing execution", name)
            return None
        logger.info("Pterodactyl guard: blocked unconfirmed '%s'", name)
        return self._build_block(request)

    def wrap_tool_call(self, request: ToolCallRequest, handler: Callable) -> Any:
        block = self._gate(request)
        return block if block is not None else handler(request)

    async def awrap_tool_call(self, request: ToolCallRequest, handler: Callable) -> Any:
        block = self._gate(request)
        if block is not None:
            return block
        return await handler(request)
