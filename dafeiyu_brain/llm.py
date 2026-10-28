"""上游模型客户端：只用标准库的 http.client，支持流式与非流式。"""

from __future__ import annotations

import http.client
import json
import ssl
from typing import Any
from urllib.parse import urlsplit


class UpstreamError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"upstream {status}: {body[:400]}")
        self.status = status
        self.body = body


class Upstream:
    def __init__(self, cfg: dict[str, Any]) -> None:
        up = cfg.get("upstream") or {}
        self.base_url = (up.get("base_url") or "").rstrip("/")
        self.chat_path = up.get("chat_path") or "/v1/chat/completions"
        self.model = up.get("model") or ""
        self.api_key = up.get("api_key") or ""
        self.timeout = float(up.get("timeout") or 90.0)
        self.extra_body = up.get("extra_body") or {}
        if not self.base_url:
            raise ValueError("config.upstream.base_url 不能为空")

    @property
    def url(self) -> str:
        return self.base_url + self.chat_path

    def _target(self) -> str:
        parts = urlsplit(self.url)
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        return target

    def _connection(self, timeout: float) -> http.client.HTTPConnection:
        parts = urlsplit(self.url)
        if parts.scheme == "https":
            return http.client.HTTPSConnection(
                parts.hostname, parts.port or 443, timeout=timeout, context=ssl.create_default_context()
            )
        return http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def header_value(self, name: str) -> str:
        return self._headers().get(name, "")

    def open_chat(
        self, payload: dict[str, Any], *, timeout: float | None = None
    ) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        """发起请求并返回（连接, 响应）。调用方负责 close。"""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        conn = self._connection(timeout or self.timeout)
        try:
            conn.request("POST", self._target(), body=body, headers=self._headers())
            resp = conn.getresponse()
        except Exception:
            conn.close()
            raise
        return conn, resp

    def post_chat(self, payload: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        """非流式请求，返回解析后的 JSON。"""
        conn, resp = self.open_chat(payload, timeout=timeout)
        try:
            raw = resp.read()
        finally:
            conn.close()
        text = raw.decode("utf-8", "replace")
        if resp.status >= 400:
            raise UpstreamError(resp.status, text)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise UpstreamError(resp.status, text) from exc
        if not isinstance(data, dict):
            raise UpstreamError(resp.status, text)
        return data

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> str:
        """简化调用：返回助手回复的纯文本。"""
        payload: dict[str, Any] = dict(self.extra_body)
        payload.update(
            {
                "model": model or self.model,
                "messages": messages,
                "stream": False,
            }
        )
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        data = self.post_chat(payload, timeout=timeout)
        choices = data.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return str(content)


def content_of(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"
        )
    return str(content or "")


def last_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages or []):
        if message.get("role") == "user":
            return content_of(message)
    return ""


def extract_delta_content(chunk: str) -> str:
    """从一行 SSE data 里取出增量文本。"""
    chunk = chunk.strip()
    if not chunk.startswith("data:"):
        return ""
    data = chunk[5:].strip()
    if not data or data == "[DONE]":
        return ""
    try:
        obj = json.loads(data)
    except json.JSONDecodeError:
        return ""
    choices = obj.get("choices") or []
    if not choices:
        return ""
    delta = choices[0].get("delta") or {}
    content = delta.get("content")
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"
        )
    return str(content or "")
