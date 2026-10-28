"""网关核心：把检索到的记忆拼进 messages，再把回复交回调用方。"""

from __future__ import annotations

import copy
from typing import Any

from .llm import content_of, extract_delta_content, last_user_text  # noqa: F401  (对外复用)

MEMORY_BLOCK_POSITION = "system"


def inject_block(payload: dict[str, Any], block: str) -> dict[str, Any]:
    """把记忆块并入 system 提示；没有 system 就新建一条。"""
    if not block:
        return payload
    out = copy.deepcopy(payload)
    messages = out.get("messages")
    if not isinstance(messages, list):
        messages = []
    if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
        first = dict(messages[0])
        content = first.get("content")
        if isinstance(content, list):
            first["content"] = [*content, {"type": "text", "text": "\n\n" + block}]
        else:
            first["content"] = ((content or "").rstrip() + "\n\n" + block).strip()
        messages[0] = first
    else:
        messages = [{"role": "system", "content": block}, *messages]
    out["messages"] = messages
    return out


def request_query(messages: list[dict[str, Any]]) -> str:
    """用来检索的查询：最后一条用户消息。"""
    return last_user_text(messages)[:500]


def assistant_text_from_response(data: dict[str, Any]) -> str:
    choices = data.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return content_of(message)
