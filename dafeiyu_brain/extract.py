"""记忆抽取：把一轮对话压成 0~3 条长期记忆。

原则：宁可少记，不要记错。抽取失败只写日志，绝不影响正常回复。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any

from . import retrieval
from .llm import Upstream, UpstreamError
from .store import Store

log = logging.getLogger("brain.extract")

PROMPT = """你在为一个聊天角色整理长期记忆。下面是一轮对话，请抽取「关于用户本人、以后还用得上」的信息。

要记的（任何一条命中就记）：
- 用户的身份、称呼、住哪、做什么、养了什么
- 用户的偏好、习惯、口味、忌讳
- 用户经历的具体事件：谁做了什么、结果如何、什么时候
- 用户的计划、约定、待办
- 用户要求角色做的事（发动态、改签名、查东西、记东西），哪怕一句带过也要记

不要记的：
- 寒暄、客套、语气词、纯情绪感叹
- 角色自己说的话、角色的自夸
- 没有具体信息的附和（「嗯」「好」「行」）

规则：
1. 每条独立成句、第三人称、不超过 40 字，脱离上下文也能看懂（用「他/她」或用户称呼，不要用「你」）。
2. 最多 {max_items} 条。实在没有值得记的才返回空数组，不要编造。
3. importance 取 0~1：随口一提 0.3，明确偏好 0.6，重要计划/身份信息 0.9。

用户说：
{user}

角色回：
{assistant}

只输出 JSON，不要解释：
{{"memories":[{{"kind":"fact|pref|event|plan|relation","text":"...","importance":0.6}}]}}"""

KINDS = {"fact", "pref", "event", "plan", "relation", "emotion"}

_sem: threading.Semaphore | None = None
_sem_size = 0


def _semaphore(size: int) -> threading.Semaphore:
    global _sem, _sem_size
    if _sem is None or _sem_size != size:
        _sem = threading.Semaphore(max(1, size))
        _sem_size = size
    return _sem


def parse_memories(raw: str, max_items: int) -> list[dict[str, Any]]:
    if not raw:
        return []
    text = raw.strip()
    if "```" in text:
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text, flags=re.S).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    items = data.get("memories") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []

    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text_value = str(item.get("text") or "").strip()
        if len(text_value) < 4:
            continue
        kind = str(item.get("kind") or "fact").strip().lower()
        if kind not in KINDS:
            kind = "fact"
        try:
            importance = float(item.get("importance", 0.5))
        except (TypeError, ValueError):
            importance = 0.5
        out.append(
            {
                "text": text_value[:200],
                "kind": kind,
                "importance": max(0.05, min(1.0, importance)),
            }
        )
        if len(out) >= max_items:
            break
    return out


def extract(
    upstream: Upstream,
    user_text: str,
    assistant_text: str,
    *,
    max_items: int = 3,
    model: str = "",
    timeout: float = 45.0,
    temperature: float = 0.1,
    max_tokens: int = 400,
) -> list[dict[str, Any]]:
    prompt = PROMPT.format(
        max_items=max_items, user=user_text[:2000], assistant=assistant_text[:2000]
    )
    raw = upstream.chat(
        [{"role": "user", "content": prompt}],
        model=model or None,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )
    return parse_memories(raw, max_items)


def run(
    store: Store,
    upstream: Upstream,
    cfg: dict[str, Any],
    *,
    person_id: str,
    scene: str,
    source: str,
    user_text: str,
    assistant_text: str,
) -> list[dict[str, Any]]:
    """完整的一轮落库：原文 + 抽取出的原子。失败只记日志。"""
    conf = cfg.get("extraction") or {}
    store.add_turn(person_id, role="user", text=user_text, source=source, scene=scene)
    store.add_turn(person_id, role="assistant", text=assistant_text, source=source, scene=scene)

    if not conf.get("enabled", True):
        return []
    if len(user_text.strip()) < int(conf.get("min_user_chars", 3)):
        return []
    if not assistant_text.strip():
        return []

    sem = _semaphore(int(conf.get("max_concurrency", 2)))
    if not sem.acquire(blocking=False):
        log.warning("抽取并发已满，跳过这一轮")
        return []
    try:
        items = extract(
            upstream,
            user_text,
            assistant_text,
            max_items=int(conf.get("max_items", 3)),
            model=conf.get("model") or "",
            timeout=float(conf.get("timeout", 45.0)),
            temperature=float(conf.get("temperature", 0.1)),
            max_tokens=int(conf.get("max_tokens", 400)),
        )
    except UpstreamError as exc:
        log.warning("抽取失败（上游 %s）：%s", exc.status, exc.body[:200])
        return []
    except Exception as exc:
        log.warning("抽取失败：%s", exc)
        return []
    finally:
        sem.release()

    created: list[dict[str, Any]] = []
    threshold = float((cfg.get("memory") or {}).get("dedupe_jaccard", 0.86))
    for item in items:
        atom = store.add_atom(
            person_id,
            item["text"],
            kind=item["kind"],
            importance=item["importance"],
            source=source,
            scene=scene,
            dedupe_threshold=threshold,
        )
        if atom:
            created.append(atom)
    if created:
        log.info("新记忆 %d 条：%s", len(created), " / ".join(a["text"][:20] for a in created))
    return created


def recall_block(store: Store, cfg: dict[str, Any], person_id: str, query: str) -> tuple[list[dict], str]:
    mem = cfg.get("memory") or {}
    if not mem.get("enabled", True):
        return [], ""
    items = retrieval.select(
        store,
        person_id,
        query,
        k=int(mem.get("top_k", 5)),
        recent_k=int(mem.get("recent_k", 2)),
    )
    block = retrieval.format_block(
        items,
        header=mem.get("inject_header") or "【你记得关于这个人的事】",
        max_chars=int(mem.get("inject_max_chars", 900)),
    )
    if items:
        store.bump_atoms([int(a["id"]) for a in items])
    return items, block
