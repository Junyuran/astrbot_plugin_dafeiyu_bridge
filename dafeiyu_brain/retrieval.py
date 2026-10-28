"""检索：BM25（字符二元组）+ 重要度 + 时间衰减，纯 Python 实现。

记忆规模在几千条以内时，直接全量打分比维护倒排索引更简单也更快；
超过这个量再换向量库也不迟（接口保持不变）。
"""

from __future__ import annotations

import math
import threading
import time
from collections import Counter
from typing import Any

from .textutil import clip, tokens

K1 = 1.2
B = 0.75
HALF_LIFE_DAYS = 120.0

_cache: dict[str, tuple[int, "BM25"]] = {}
_cache_lock = threading.Lock()


class BM25:
    def __init__(self, atoms: list[dict[str, Any]]) -> None:
        self.ids: list[int] = []
        self.tfs: list[Counter] = []
        self.df: Counter = Counter()
        self.lengths: list[int] = []
        for atom in atoms:
            toks = tokens(atom["text"])
            tf = Counter(toks)
            self.ids.append(int(atom["id"]))
            self.tfs.append(tf)
            self.lengths.append(max(1, len(toks)))
            self.df.update(set(tf))
        self.n = len(atoms)
        self.avgdl = (sum(self.lengths) / self.n) if self.n else 1.0

    def scores(self, query_tokens: list[str]) -> dict[int, float]:
        if not self.n or not query_tokens:
            return {}
        unique = set(query_tokens)
        idf = {
            term: math.log(1 + (self.n - self.df.get(term, 0) + 0.5) / (self.df.get(term, 0) + 0.5))
            for term in unique
        }
        out: dict[int, float] = {}
        for i in range(self.n):
            tf = self.tfs[i]
            dl = self.lengths[i]
            total = 0.0
            for term in unique:
                freq = tf.get(term)
                if not freq:
                    continue
                denom = freq + K1 * (1 - B + B * dl / self.avgdl)
                total += idf[term] * (freq * (K1 + 1)) / denom
            if total:
                out[self.ids[i]] = total
        return out


def _semaphore_index(atoms: list[dict[str, Any]]) -> BM25:
    return BM25(atoms)


def _index_for(store: Any, person_id: str) -> BM25:
    version = store.version
    with _cache_lock:
        cached = _cache.get(person_id)
        if cached and cached[0] == version:
            return cached[1]
    atoms = store.all_active_atoms(person_id)
    index = _semaphore_index(atoms)
    with _cache_lock:
        _cache[person_id] = (version, index)
    return index


def _recency(created_at: float, now: float) -> float:
    days = max(0.0, (now - float(created_at or now)) / 86400.0)
    return math.exp(-days / HALF_LIFE_DAYS)


def select(
    store: Any,
    person_id: str,
    query: str,
    *,
    k: int = 5,
    recent_k: int = 2,
) -> list[dict[str, Any]]:
    """挑出要注入 prompt 的记忆。query 为空时退化为「最近几条」。"""
    atoms = store.all_active_atoms(person_id)
    if not atoms:
        return []

    now = time.time()
    by_id = {int(a["id"]): a for a in atoms}
    query_tokens = tokens(query or "")
    raw: dict[int, float] = {}
    if query_tokens:
        raw = _index_for(store, person_id).scores(query_tokens)
    top = max(raw.values()) if raw else 0.0

    ranked: list[tuple[float, dict[str, Any]]] = []
    for atom in atoms:
        importance = float(atom.get("importance") or 0.5)
        rec = _recency(atom.get("created_at"), now)
        if raw and top > 0:
            relevance = raw.get(int(atom["id"]), 0.0) / top
            score = 0.6 * relevance + 0.25 * importance + 0.15 * rec
        else:
            score = 0.5 * importance + 0.5 * rec
        ranked.append((score, atom))

    ranked.sort(key=lambda item: item[0], reverse=True)
    picked: list[dict[str, Any]] = []
    seen: set[int] = set()

    for score, atom in ranked:
        if len(picked) >= k:
            break
        # 语义完全无关的旧记忆不要塞进来，宁缺毋滥
        if raw and raw.get(int(atom["id"]), 0.0) <= 0 and float(atom.get("importance") or 0) < 0.6:
            continue
        atom = dict(atom)
        atom["_score"] = round(score, 4)
        picked.append(atom)
        seen.add(int(atom["id"]))

    if recent_k > 0:
        for atom in store.recent_atoms(person_id, recent_k):
            atom_id = int(atom["id"])
            if atom_id in seen or atom_id not in by_id:
                continue
            atom = dict(atom)
            atom["_score"] = 0.0
            picked.append(atom)
            seen.add(atom_id)

    return picked


def format_block(
    memories: list[dict[str, Any]],
    *,
    header: str = "【你记得关于这个人的事】",
    max_chars: int = 900,
) -> str:
    if not memories:
        return ""
    lines: list[str] = []
    used = 0
    for atom in sorted(memories, key=lambda a: float(a.get("created_at") or 0)):
        text = clip(atom.get("text", ""), 160)
        if not text:
            continue
        day = time.strftime("%m-%d", time.localtime(float(atom.get("created_at") or 0)))
        line = f"- {day}｜{text}"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
    if not lines:
        return ""
    tail = "\n（这些是你以前自己记下的，别生硬复述，合适的时候自然带出来就行）"
    return header + "\n" + "\n".join(lines) + tail
