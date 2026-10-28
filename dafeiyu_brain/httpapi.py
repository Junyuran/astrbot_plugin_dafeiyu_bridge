"""HTTP 服务：OpenAI 兼容网关 + 记忆接口 + 一个能看到记忆的网页。"""

from __future__ import annotations

import html
import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import extract, retrieval
from .gateway import assistant_text_from_response, inject_block, request_query
from .llm import Upstream, extract_delta_content
from .store import Store

log = logging.getLogger("brain.http")

MAX_BODY = 32 * 1024 * 1024
_EXTRACT_SLOTS = threading.Semaphore(4)


class BrainHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, *, cfg, store, upstream):
        super().__init__(addr, handler)
        self.cfg = cfg
        self.store = store
        self.upstream = upstream
        self.started_at = time.time()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "DafeiyuBrain/1.0"

    # ---------- 基础工具 ----------

    @property
    def cfg(self) -> dict[str, Any]:
        return self.server.cfg  # type: ignore[attr-defined]

    @property
    def store(self) -> Store:
        return self.server.store  # type: ignore[attr-defined]

    @property
    def upstream(self) -> Upstream:
        return self.server.upstream  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:  # 别往 stderr 刷
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _read_body(self) -> bytes:
        length = self.headers.get("Content-Length")
        if length:
            try:
                return self.rfile.read(min(int(length), MAX_BODY))
            except (ValueError, OSError):
                return b""
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            chunks: list[bytes] = []
            total = 0
            while True:
                line = self.rfile.readline(65537)
                if not line:
                    break
                try:
                    size = int(line.split(b";")[0].strip(), 16)
                except ValueError:
                    break
                if size == 0:
                    self.rfile.readline(65537)
                    break
                data = self.rfile.read(size)
                chunks.append(data)
                total += len(data)
                self.rfile.read(2)
                if total > MAX_BODY:
                    break
            return b"".join(chunks)
        return b""

    def _send_bytes(self, data: bytes, status: int, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:  # 客户端提前断开不算错误
            pass
        self.close_connection = True

    def _json(self, obj: Any, status: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send_bytes(data, status, "application/json; charset=utf-8")

    def _raw(self, data: bytes, status: int, content_type: str) -> None:
        self._send_bytes(data, status, content_type or "application/json")

    def _start_stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()

    def _write_chunk(self, data: bytes) -> None:
        if not data:
            return
        self.wfile.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")
        self.wfile.flush()

    def _end_stream(self) -> None:
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except OSError:  # 客户端提前断开（WinError 10053 之类）不算错误
            pass
        self.close_connection = True

    def _authorized(self) -> bool:
        token = (self.cfg.get("server") or {}).get("token") or ""
        if not token:
            return True
        header = self.headers.get("Authorization") or ""
        supplied = header[7:].strip() if header.lower().startswith("bearer ") else ""
        return supplied == token

    def _guard(self) -> bool:
        if self._authorized():
            return True
        self._json({"error": {"message": "unauthorized"}}, 401)
        return False

    def _person_scene(self) -> tuple[str, str]:
        memory = self.cfg.get("memory") or {}
        person = self.headers.get("X-Dafeiyu-Person") or memory.get("default_person") or "owner"
        scene = self.headers.get("X-Dafeiyu-Scene") or memory.get("default_scene") or "desktop"
        return person.strip(), scene.strip()

    # ---------- 路由 ----------

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        parsed = urlsplit(self.path)
        raw_path = parsed.path or "/"
        # 客户端把 base_url 写成 ".../8787/" 时会拼出 "//v1/..."，这里统一收掉
        while "//" in raw_path:
            raw_path = raw_path.replace("//", "/")
        path = raw_path.rstrip("/") or "/"
        self.query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        try:
            if path == "/health" and method == "GET":
                return self._health()
            if path in ("/", "/admin") and method == "GET":
                if self._guard():
                    self._admin()
                return
            if path == "/v1/chat/completions" and method == "POST":
                return self._chat()
            if path == "/diag":
                if not self._guard():
                    return
                if method == "POST":
                    return self._diag_post()
                if method == "GET":
                    return self._diag_get()
                if method == "DELETE":
                    self.store.clear_diag()
                    return self._json({"ok": True})
            if path.startswith("/memory"):
                if not self._guard():
                    return
                if path == "/memory/recall" and method in ("POST", "GET"):
                    return self._recall()
                if path == "/memory/ingest" and method == "POST":
                    return self._ingest()
                if path == "/memory/add" and method == "POST":
                    return self._add()
                if path == "/memory/list" and method == "GET":
                    return self._list()
                if path == "/memory/delete" and method == "POST":
                    return self._delete()
                if path == "/memory/stats" and method == "GET":
                    return self._stats()
                if path == "/memory/link" and method == "POST":
                    return self._link()
                if path == "/memory/import" and method == "POST":
                    return self._import()
                if path == "/memory/export" and method == "GET":
                    return self._export()
            self._json({"error": {"message": f"not found: {method} {path}"}}, 404)
        except OSError:
            log.debug("连接异常：%s %s", method, path)
        except Exception:
            log.exception("处理 %s %s 出错", method, path)
            try:
                self._json({"error": {"message": "internal error"}}, 500)
            except Exception:
                pass

    # ---------- 网关 ----------

    def _health(self) -> None:
        stats = self.store.stats()
        self._json(
            {
                "ok": True,
                "uptime_seconds": round(time.time() - self.server.started_at, 1),
                "atoms": stats["atoms"],
                "persons": stats["persons"],
                "upstream": self.upstream.base_url,
                "memory_enabled": bool((self.cfg.get("memory") or {}).get("enabled", True)),
            }
        )

    def _chat(self) -> None:
        body = self._read_body()
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return self._json({"error": {"message": "invalid json body"}}, 400)
        if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
            return self._json({"error": {"message": "messages is required"}}, 400)

        person, scene = self._person_scene()
        messages = payload["messages"]
        query = request_query(messages)

        block = ""
        try:
            _, block = extract.recall_block(self.store, self.cfg, person, query)
        except Exception:
            log.exception("检索失败，按无记忆转发")
            block = ""

        outbound = inject_block(payload, block)
        if not outbound.get("model"):
            outbound["model"] = self.upstream.model

        source = self.headers.get("X-Dafeiyu-Source") or scene
        try:
            conn, resp = self.upstream.open_chat(outbound)
        except Exception as exc:
            log.warning("上游连接失败：%s", exc)
            return self._json({"error": {"message": f"upstream unreachable: {exc}"}}, 502)

        if resp.status >= 400:
            raw = resp.read()
            conn.close()
            log.warning("上游返回 %s：%s", resp.status, raw[:200])
            return self._raw(raw, resp.status, resp.getheader("Content-Type") or "application/json")

        if payload.get("stream"):
            return self._stream_reply(conn, resp, person, scene, source, query)

        raw = resp.read()
        conn.close()
        assistant = ""
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
            assistant = assistant_text_from_response(data)
        except Exception:
            log.debug("非 JSON 回复，跳过记忆写入")
        self._raw(raw, 200, resp.getheader("Content-Type") or "application/json")
        if assistant:
            self._schedule_extraction(person, scene, source, query, assistant)

    def _stream_reply(self, conn, resp, person: str, scene: str, source: str, query: str) -> None:
        parts: list[str] = []
        self._start_stream()
        try:
            while True:
                line = resp.readline()
                if not line:
                    break
                delta = extract_delta_content(line.decode("utf-8", "replace"))
                if delta:
                    parts.append(delta)
                self._write_chunk(line)
        except OSError:
            log.debug("客户端中途断开流")
        except Exception:
            log.exception("流式转发出错")
        finally:
            try:
                conn.close()
            except Exception:
                pass
            self._end_stream()
        assistant = "".join(parts)
        if assistant:
            self._schedule_extraction(person, scene, source, query, assistant)

    def _schedule_extraction(
        self, person: str, scene: str, source: str, user_text: str, assistant: str
    ) -> None:
        if not (self.cfg.get("extraction") or {}).get("enabled", True):
            return
        if not _EXTRACT_SLOTS.acquire(blocking=False):
            log.info("抽取线程已满，跳过本轮")
            return

        store, upstream, cfg = self.store, self.upstream, self.cfg

        def worker() -> None:
            try:
                extract.run(
                    store,
                    upstream,
                    cfg,
                    person_id=person,
                    scene=scene,
                    source=source,
                    user_text=user_text,
                    assistant_text=assistant,
                )
            except Exception:
                log.exception("后台抽取异常")
            finally:
                _EXTRACT_SLOTS.release()

        threading.Thread(target=worker, name="extract", daemon=True).start()

    # ---------- 自检 ----------

    def _diag_post(self) -> None:
        try:
            payload = json.loads(self._read_body().decode("utf-8") or "{}")
        except Exception:
            return self._json({"error": {"message": "invalid json"}}, 400)
        self.store.add_diag(
            str(payload.get("name") or "?"),
            str(payload.get("detail") or ""),
            str(payload.get("source") or ""),
        )
        self._json({"ok": True})

    def _diag_get(self) -> None:
        limit = int(self.query.get("limit", 60))
        events = self.store.recent_diag(limit)
        for event in events:
            event["when"] = time.strftime(
                "%H:%M:%S", time.localtime(float(event.get("ts") or 0))
            )
        self._json({"count": len(events), "events": events})

    # ---------- 记忆接口 ----------

    def _recall(self) -> None:
        if self.command == "GET":
            payload: dict[str, Any] = dict(self.query)
        else:
            try:
                payload = json.loads(self._read_body().decode("utf-8") or "{}")
            except Exception:
                return self._json({"error": {"message": "invalid json"}}, 400)
        memory = self.cfg.get("memory") or {}
        person = str(payload.get("person_id") or memory.get("default_person") or "owner")
        query = str(payload.get("query") or "")
        k = int(payload.get("k") or memory.get("top_k", 5))
        items = retrieval.select(
            self.store, person, query, k=max(1, min(50, k)), recent_k=int(memory.get("recent_k", 2))
        )
        block = retrieval.format_block(
            items,
            header=memory.get("inject_header") or "【你记得关于这个人的事】",
            max_chars=int(memory.get("inject_max_chars", 900)),
        )
        self._json(
            {
                "person_id": person,
                "count": len(items),
                "block": block,
                "memories": [
                    {
                        "id": a["id"],
                        "text": a["text"],
                        "kind": a["kind"],
                        "importance": a["importance"],
                        "created_at": a["created_at"],
                        "source": a["source"],
                    }
                    for a in items
                ],
            }
        )

    def _ingest(self) -> None:
        try:
            payload = json.loads(self._read_body().decode("utf-8") or "{}")
        except Exception:
            return self._json({"error": {"message": "invalid json"}}, 400)
        person = str(
            payload.get("person_id") or (self.cfg.get("memory") or {}).get("default_person") or "owner"
        )
        scene = str(payload.get("scene") or "qq")
        source = str(payload.get("source") or scene)
        user_text = str(payload.get("user_text") or "")
        assistant_text = str(payload.get("assistant_text") or "")
        if not user_text and not assistant_text:
            return self._json({"error": {"message": "user_text/assistant_text 至少给一个"}}, 400)

        if str(payload.get("wait", "1")) in ("0", "false", "False"):
            self._schedule_extraction(person, scene, source, user_text, assistant_text)
            return self._json({"ok": True, "queued": True})

        try:
            created = extract.run(
                self.store,
                self.upstream,
                self.cfg,
                person_id=person,
                scene=scene,
                source=source,
                user_text=user_text,
                assistant_text=assistant_text,
            )
        except Exception as exc:
            log.exception("同步抽取失败")
            return self._json({"ok": False, "error": str(exc)}, 500)
        self._json({"ok": True, "created": len(created), "memories": [a["text"] for a in created]})

    def _add(self) -> None:
        try:
            payload = json.loads(self._read_body().decode("utf-8") or "{}")
        except Exception:
            return self._json({"error": {"message": "invalid json"}}, 400)
        person = str(
            payload.get("person_id") or (self.cfg.get("memory") or {}).get("default_person") or "owner"
        )
        text = str(payload.get("text") or "")
        if len(text.strip()) < 4:
            return self._json({"error": {"message": "text 太短"}}, 400)
        atom = self.store.add_atom(
            person,
            text,
            kind=str(payload.get("kind") or "fact"),
            importance=float(payload.get("importance", 0.6)),
            source=str(payload.get("source") or "manual"),
            scene=str(payload.get("scene") or "manual"),
            visibility=str(payload.get("visibility") or "private"),
            dedupe_threshold=float((self.cfg.get("memory") or {}).get("dedupe_jaccard", 0.86)),
        )
        if not atom:
            return self._json({"ok": True, "created": 0, "note": "重复或已存在"})
        self._json({"ok": True, "created": 1, "atom": atom})

    def _list(self) -> None:
        person = self.query.get("person_id") or None
        limit = max(1, min(500, int(self.query.get("limit", 100))))
        offset = max(0, int(self.query.get("offset", 0)))
        atoms = self.store.list_atoms(person_id=person, limit=limit, offset=offset)
        self._json({"count": len(atoms), "memories": atoms})

    def _delete(self) -> None:
        try:
            payload = json.loads(self._read_body().decode("utf-8") or "{}")
        except Exception:
            return self._json({"error": {"message": "invalid json"}}, 400)
        atom_id = int(payload.get("id") or 0)
        ok = self.store.delete_atom(atom_id)
        self._json({"ok": ok})

    def _stats(self) -> None:
        stats = self.store.stats()
        if stats.get("last_write"):
            stats["last_write_human"] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(stats["last_write"])
            )
        self._json(stats)

    def _link(self) -> None:
        """把某个平台账号绑到同一个人身上（qq:123 / wx:openid / desktop:owner）。"""
        try:
            payload = json.loads(self._read_body().decode("utf-8") or "{}")
        except Exception:
            return self._json({"error": {"message": "invalid json"}}, 400)
        platform = str(payload.get("platform") or "").strip()
        external_id = str(payload.get("external_id") or "").strip()
        person = str(payload.get("person_id") or "").strip()
        if not platform or not external_id or not person:
            return self._json({"error": {"message": "platform / external_id / person_id 都要给"}}, 400)
        self.store.link_identity(platform, external_id, person, str(payload.get("note") or ""))
        self._json({"ok": True, "platform": platform, "external_id": external_id, "person_id": person})

    def _import(self) -> None:
        """批量导入（迁移 livingmemory 历史时用）。重复的会被跳过。"""
        try:
            payload = json.loads(self._read_body().decode("utf-8") or "{}")
        except Exception:
            return self._json({"error": {"message": "invalid json"}}, 400)
        default_person = str(
            payload.get("person_id") or (self.cfg.get("memory") or {}).get("default_person") or "owner"
        )
        items = payload.get("atoms") or payload.get("memories") or []
        if not isinstance(items, list):
            return self._json({"error": {"message": "atoms 必须是数组"}}, 400)

        threshold = float((self.cfg.get("memory") or {}).get("dedupe_jaccard", 0.86))
        created = 0
        for item in items[:20000]:
            if isinstance(item, str):
                item = {"text": item}
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or item.get("content") or "").strip()
            if len(text) < 4:
                continue
            try:
                created_at = float(item.get("created_at") or 0) or None
            except (TypeError, ValueError):
                created_at = None
            atom = self.store.add_atom(
                str(item.get("person_id") or default_person),
                text,
                kind=str(item.get("kind") or "fact"),
                importance=float(item.get("importance") or 0.6),
                source=str(item.get("source") or "import"),
                scene=str(item.get("scene") or "import"),
                visibility=str(item.get("visibility") or "private"),
                dedupe_threshold=threshold,
                created_at=created_at,
            )
            if atom:
                created += 1
        self._json({"ok": True, "received": len(items), "created": created})

    def _export(self) -> None:
        atoms = self.store.list_atoms(person_id=self.query.get("person_id") or None, limit=10000)
        self._json({"count": len(atoms), "atoms": atoms})

    # ---------- 管理页 ----------

    def _admin(self) -> None:
        query = self.query.get("q") or ""
        person = (
            self.query.get("person")
            or (self.cfg.get("memory") or {}).get("default_person")
            or "owner"
        )
        if query:
            k = max(1, min(200, int(self.query.get("k", 100))))
            items = retrieval.select(self.store, person, query, k=k, recent_k=0)
        else:
            items = self.store.list_atoms(person_id=None, limit=200)
        stats = self.store.stats()

        rows = []
        for atom in items:
            day = time.strftime("%m-%d %H:%M", time.localtime(float(atom.get("created_at") or 0)))
            rows.append(
                "<tr>"
                f"<td class='dim'>{atom.get('id')}</td>"
                f"<td class='dim'>{day}</td>"
                f"<td><span class='tag'>{html.escape(str(atom.get('kind')))}</span></td>"
                f"<td>{html.escape(str(atom.get('text')))}</td>"
                f"<td class='dim'>{float(atom.get('importance') or 0):.2f}</td>"
                f"<td class='dim'>{html.escape(str(atom.get('source')))}</td>"
                "</tr>"
            )

        persons = (
            "、".join(f"{html.escape(p['person_id'])}（{p['count']}）" for p in stats.get("persons", []))
            or "暂无"
        )
        page = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>大肥鱼记忆 · {stats['atoms']} 条</title>
<style>
 body{{font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;margin:24px;color:#222;background:#fafafa}}
 h1{{font-size:18px;margin:0 0 4px}} .dim{{color:#888}}
 .tag{{background:#eef;border-radius:4px;padding:1px 6px;font-size:12px}}
 table{{border-collapse:collapse;width:100%;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.08);border-radius:8px;overflow:hidden}}
 th,td{{text-align:left;padding:8px 10px;border-bottom:1px solid #f0f0f0;vertical-align:top}}
 th{{background:#f6f7fb;font-weight:600;font-size:12px;color:#555}}
 form{{margin:16px 0}} input[type=text]{{padding:6px 10px;border:1px solid #ddd;border-radius:6px;width:320px}}
 button{{padding:6px 14px;border:0;border-radius:6px;background:#3b5bdb;color:#fff;cursor:pointer}}
 .meta{{color:#666;margin-bottom:12px}}
</style></head><body>
<h1>大肥鱼记忆中枢</h1>
<div class="meta">共 <b>{stats['atoms']}</b> 条记忆 · {stats['turns']} 轮对话 · 人：{persons} · 上游 {html.escape(self.upstream.base_url)}</div>
<form method="get" action="/admin">
  <input type="text" name="q" value="{html.escape(query)}" placeholder="搜记忆，比如「火锅」">
  <input type="hidden" name="person" value="{html.escape(person)}">
  <button type="submit">检索</button>
</form>
<table><thead><tr><th>#</th><th>时间</th><th>类型</th><th>内容</th><th>重要度</th><th>来源</th></tr></thead>
<tbody>{''.join(rows) or "<tr><td colspan='6' class='dim'>还没有记忆，先和桌宠聊几句</td></tr>"}</tbody></table>
</body></html>"""
        self._send_bytes(page.encode("utf-8"), 200, "text/html; charset=utf-8")
