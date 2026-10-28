"""中文/英文混合文本的轻量分词与相似度工具。

只依赖标准库：中文按「二元组」切，英文按单词切。
二元组对短句检索的效果接近 jieba，但不需要装任何包、也不怕词典缺失。
"""

from __future__ import annotations

import re

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_ASCII = re.compile(r"[a-z0-9]+")

# 只由这些字组成的词（的/了/是……）对检索没有信息量，直接丢掉。
_STOP_CHARS = frozenset(
    "的了是我你他她它吗呢吧啊呀哦嗯哈就都和跟与在有个一下不没很也还这那什么"
)


def normalize(text: str) -> str:
    """压掉多余空白，去掉首尾空白。"""
    return " ".join((text or "").split()).strip()


def tokens(text: str, *, keep_single: bool = False) -> list[str]:
    """把一段文本切成检索用的 token 列表。

    - 连续中文 → 相邻两字组成一个 token（「想吃火锅」→ 想吃/吃火/火锅）
    - 单个汉字默认丢弃（除非 keep_single=True）
    - 英文数字串整体保留（「bge-m3」→ bge, m3）
    """
    lowered = (text or "").lower()
    out: list[str] = []
    for run in _CJK.findall(lowered):
        if len(run) == 1:
            if keep_single:
                out.append(run)
            continue
        out.extend(run[i : i + 2] for i in range(len(run) - 1))
    out.extend(_ASCII.findall(lowered))
    return [w for w in out if set(w) - _STOP_CHARS]


def token_set(text: str) -> set[str]:
    return set(tokens(text))


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / len(a | b)


def clip(text: str, limit: int) -> str:
    text = normalize(text)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"
