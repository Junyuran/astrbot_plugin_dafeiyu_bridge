"""SQLite 存储层：记忆原子 + 对话原文 + 身份映射。

设计要点：
- 单文件 SQLite + WAL，支持多线程（每个线程一条连接）。
- 只追加、软删除，不覆盖历史，方便以后回溯和合并。
- 写入时做「精确 hash + 字符二元组 Jaccard」两层去重，避免同一件事记十遍。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from typing import Any, Iterable

from .textutil import jaccard, normalize, token_set

SCHEMA = """
CREATE TABLE IF NOT EXISTS atoms (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id     TEXT    NOT NULL,
    kind          TEXT    NOT NULL DEFAULT 'fact',
    text          TEXT    NOT NULL,
    text_hash     TEXT    NOT NULL,
    importance    REAL    NOT NULL DEFAULT 0.5,
    visibility    TEXT    NOT NULL DEFAULT 'private',
    source        TEXT    NOT NULL DEFAULT 'desktop',
    scene         TEXT    NOT NULL DEFAULT 'desktop',
    created_at    REAL    NOT NULL,
    last_access_at REAL   NOT NULL,
    access_count  INTEGER NOT NULL DEFAULT 0,
    status        TEXT    NOT NULL DEFAULT 'active',
    meta          TEXT    NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_atoms_person ON atoms(person_id, status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_atoms_hash   ON atoms(person_id, text_hash);

CREATE TABLE IF NOT EXISTS turns (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id  TEXT NOT NULL,
    scene      TEXT NOT NULL DEFAULT 'desktop',
    source     TEXT NOT NULL DEFAULT 'desktop',
    role       TEXT NOT NULL,
    text       TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_person ON turns(person_id, created_at DESC);

CREATE TABLE IF NOT EXISTS identities (
    platform    TEXT NOT NULL,
    external_id TEXT NOT NULL,
    person_id   TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (platform, external_id)
);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS diag (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     REAL NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    name   TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_diag_ts ON diag(ts DESC);
"""


def _hash(text: str) -> str:
    import hashlib

    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:20]


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._local = threading.local()
        self._version = 0
        self._lock = threading.RLock()
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    # ---------- 连接 ----------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    @property
    def version(self) -> int:
        return self._version

    def _bump(self) -> None:
        with self._lock:
            self._version += 1

    # ---------- 记忆原子 ----------

    def add_atom(
        self,
        person_id: str,
        text: str,
        *,
        kind: str = "fact",
        importance: float = 0.5,
        source: str = "desktop",
        scene: str = "desktop",
        visibility: str = "private",
        dedupe_threshold: float = 0.86,
        created_at: float | None = None,
    ) -> dict[str, Any] | None:
        """写入一条记忆；重复或太短则返回 None。"""
        text = normalize(text)
        if len(text) < 4:
            return None
        person_id = person_id or "owner"
        digest = _hash(text)

        with self._lock:
            conn = self.conn
            exists = conn.execute(
                "SELECT id FROM atoms WHERE person_id=? AND text_hash=? AND status='active'",
                (person_id, digest),
            ).fetchone()
            if exists:
                conn.execute(
                    "UPDATE atoms SET access_count=access_count+1, last_access_at=? WHERE id=?",
                    (time.time(), exists["id"]),
                )
                conn.commit()
                return None

            if dedupe_threshold > 0:
                new_tokens = token_set(text)
                recent = conn.execute(
                    "SELECT id, text FROM atoms WHERE person_id=? AND status='active' "
                    "ORDER BY created_at DESC LIMIT 300",
                    (person_id,),
                ).fetchall()
                for row in recent:
                    if jaccard(new_tokens, token_set(row["text"])) >= dedupe_threshold:
                        conn.execute(
                            "UPDATE atoms SET access_count=access_count+1, last_access_at=? WHERE id=?",
                            (time.time(), row["id"]),
                        )
                        conn.commit()
                        return None

            now = time.time()
            stamp = float(created_at) if created_at else now
            cur = conn.execute(
                "INSERT INTO atoms (person_id, kind, text, text_hash, importance, visibility, "
                "source, scene, created_at, last_access_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    person_id,
                    kind or "fact",
                    text,
                    digest,
                    max(0.0, min(1.0, float(importance))),
                    visibility or "private",
                    source or "desktop",
                    scene or "desktop",
                    stamp,
                    now,
                ),
            )
            conn.commit()
            atom_id = cur.lastrowid
        self._bump()
        return self.get_atom(int(atom_id))

    def get_atom(self, atom_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM atoms WHERE id=?", (atom_id,)).fetchone()
        return _row_to_dict(row) if row else None

    def all_active_atoms(self, person_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM atoms WHERE person_id=? AND status='active'", (person_id,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def recent_atoms(self, person_id: str, limit: int = 5) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM atoms WHERE person_id=? AND status='active' "
            "ORDER BY created_at DESC LIMIT ?",
            (person_id, max(0, limit)),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def bump_atoms(self, ids: Iterable[int]) -> None:
        ids = [int(i) for i in ids]
        if not ids:
            return
        marks = ",".join("?" for _ in ids)
        with self._lock:
            conn = self.conn
            conn.execute(
                f"UPDATE atoms SET access_count=access_count+1, last_access_at=? WHERE id IN ({marks})",
                [time.time(), *ids],
            )
            conn.commit()

    def delete_atom(self, atom_id: int) -> bool:
        with self._lock:
            conn = self.conn
            cur = conn.execute(
                "UPDATE atoms SET status='deleted' WHERE id=? AND status='active'", (atom_id,)
            )
            conn.commit()
        if cur.rowcount:
            self._bump()
            return True
        return False

    def list_atoms(
        self, *, person_id: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        if person_id:
            rows = self.conn.execute(
                "SELECT * FROM atoms WHERE status='active' AND person_id=? "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (person_id, limit, offset),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM atoms WHERE status='active' "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ---------- 对话原文 ----------

    def add_turn(
        self, person_id: str, *, role: str, text: str, source: str = "desktop", scene: str = "desktop"
    ) -> None:
        text = normalize(text)
        if not text:
            return
        with self._lock:
            conn = self.conn
            conn.execute(
                "INSERT INTO turns (person_id, scene, source, role, text, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (person_id or "owner", scene, source, role, text[:8000], time.time()),
            )
            conn.commit()

    def recent_turns(self, person_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM turns WHERE person_id=? ORDER BY created_at DESC LIMIT ?",
            (person_id, max(0, limit)),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ---------- 身份映射 ----------

    def link_identity(self, platform: str, external_id: str, person_id: str, note: str = "") -> None:
        with self._lock:
            conn = self.conn
            conn.execute(
                "INSERT INTO identities (platform, external_id, person_id, note) VALUES (?,?,?,?) "
                "ON CONFLICT(platform, external_id) DO UPDATE SET person_id=excluded.person_id, note=excluded.note",
                (platform, external_id, person_id, note),
            )
            conn.commit()

    def resolve_person(self, platform: str, external_id: str, default: str = "owner") -> str:
        row = self.conn.execute(
            "SELECT person_id FROM identities WHERE platform=? AND external_id=?",
            (platform, external_id),
        ).fetchone()
        return row["person_id"] if row else default

    # ---------- 统计 ----------

    def add_diag(self, name: str, detail: str = "", source: str = "") -> None:
        """一条自检记录（插件侧钩子是否触发、为什么跳过）。"""
        with self._lock:
            conn = self.conn
            conn.execute(
                "INSERT INTO diag (ts, source, name, detail) VALUES (?,?,?,?)",
                (time.time(), source[:40], str(name)[:80], str(detail)[:500]),
            )
            conn.commit()

    def recent_diag(self, limit: int = 60) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM diag ORDER BY id DESC LIMIT ?", (max(1, min(500, limit)),)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def clear_diag(self) -> int:
        with self._lock:
            conn = self.conn
            cur = conn.execute("DELETE FROM diag")
            conn.commit()
        return cur.rowcount or 0

    def stats(self) -> dict[str, Any]:
        conn = self.conn
        total = conn.execute("SELECT COUNT(*) c FROM atoms WHERE status='active'").fetchone()["c"]
        turns = conn.execute("SELECT COUNT(*) c FROM turns").fetchone()["c"]
        per_person = [
            {"person_id": r["person_id"], "count": r["c"]}
            for r in conn.execute(
                "SELECT person_id, COUNT(*) c FROM atoms WHERE status='active' GROUP BY person_id"
            ).fetchall()
        ]
        last = conn.execute("SELECT MAX(created_at) m FROM atoms").fetchone()["m"]
        try:
            size = os.path.getsize(self.path)
        except OSError:
            size = 0
        return {
            "atoms": total,
            "turns": turns,
            "persons": per_person,
            "db_bytes": size,
            "last_write": last,
        }
