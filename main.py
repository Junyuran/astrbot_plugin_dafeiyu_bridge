"""大肥鱼记忆桥：给 AstrBot 一块独立于平台的长期记忆。

两种跑法：
- 自带中枢（默认）：插件加载时在后台线程里起一个本地记忆服务，桌宠/微信连同一个地址就能共用记忆
- 外接中枢：如果 brain_url 指向的端口已经有服务在跑，就直接用那个

回复前注入记忆、回复后异步落库；每次钩子触发/跳过都会写一条自检记录到 /diag。
任何一步失败都静默跳过，绝不影响正常回复。
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig

PLUGIN_NAME = "astrbot_plugin_dafeiyu_bridge"


def _ensure_brain_importable() -> bool:
    """记忆中枢的代码要么在项目根目录（开发时），要么打进插件目录（发布时）。"""
    try:
        import dafeiyu_brain  # noqa: F401

        return True
    except Exception:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import dafeiyu_brain  # noqa: F401

        return True
    except Exception:
        return False


def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


class DafeiyuBridge(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config

        self.base = str(self._get("brain_url", "http://127.0.0.1:8787") or "").rstrip("/")
        self.token = str(self._get("token", "") or "")
        self.person = str(self._get("person_id", "owner") or "owner")
        self.owner_ids = self._parse_ids(self._get("owner_qq_ids", ""))
        self.only_private = bool(self._get("only_private", True))
        self.top_k = int(self._get("top_k", 5) or 5)
        self.inject_enabled = bool(self._get("inject_enabled", True))
        self.ingest_enabled = bool(self._get("ingest_enabled", True))
        self.timeout = float(self._get("timeout", 20) or 20)
        self.debug = bool(self._get("debug", False))
        self.diag_enabled = bool(self._get("diag_enabled", True))

        self.embedded = bool(self._get("embedded", True))
        self.embedded_db = str(self._get("embedded_db_path", "") or "")
        self.upstream_base = str(self._get("upstream_base_url", "https://api.deepseek.com") or "")
        self.upstream_model = str(self._get("upstream_model", "deepseek-chat") or "")
        self.upstream_key = str(self._get("upstream_api_key", "") or "")

        self._hub = None
        self._hub_mode = "off"
        self._recent: dict[str, float] = {}

        self._start_hub()

        logger.info(
            "[大肥鱼桥] 中枢模式=%s 地址=%s 主人=%s",
            self._hub_mode,
            self.base or "(未配置)",
            self.person,
        )
        if self._hub_mode == "embedded" and not self.upstream_key:
            logger.warning(
                "[大肥鱼桥] 自带中枢已启动，但没填 upstream_api_key，记忆抽取会失败（聊天注入仍可用）"
            )

    # ---------- 配置 & 小工具 ----------

    def _get(self, key: str, default: Any) -> Any:
        try:
            return self.conf.get(key, default)
        except Exception:
            return default

    @staticmethod
    def _parse_ids(value: Any) -> set[str]:
        raw = str(value or "").replace("，", ",").replace("；", "\n").replace(";", "\n")
        parts: list[str] = []
        for chunk in raw.replace(",", "\n").splitlines():
            parts.extend(chunk.split())
        return {p.strip() for p in parts if p.strip()}

    def _data_dir(self) -> str:
        try:
            from astrbot.core.star.star_tools import StarTools

            path = str(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        os.makedirs(path, exist_ok=True)
        return path

    # ---------- 自带中枢 ----------

    def _start_hub(self) -> None:
        """端口空闲就在后台线程里起中枢；已经有服务在跑就直接用。"""
        if not self.embedded or not self.base:
            self._hub_mode = "external"
            return

        parts = urlsplit(self.base)
        host = parts.hostname or "127.0.0.1"
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if parts.scheme == "https" or host not in ("127.0.0.1", "localhost", "::1"):
            # 远端地址不归我们管
            self._hub_mode = "external"
            return

        if _port_open(host, port):
            self._hub_mode = "external"
            logger.info("[大肥鱼桥] %s:%s 上已有中枢，直接使用", host, port)
            return

        if not _ensure_brain_importable():
            self._hub_mode = "missing"
            logger.warning(
                "[大肥鱼桥] 找不到内置记忆中枢（dafeiyu_brain），请自行启动一个，或重新安装完整包"
            )
            return

        try:
            from dafeiyu_brain.httpapi import BrainHTTPServer, Handler
            from dafeiyu_brain.llm import Upstream
            from dafeiyu_brain.store import Store
        except Exception as exc:
            self._hub_mode = "missing"
            logger.warning("[大肥鱼桥] 内置中枢导入失败：%s", exc)
            return

        data_dir = self._data_dir()
        # 指定了就共用那个库，避免「外部中枢」和「自带中枢」各写一份、记忆分裂
        db_path = self.embedded_db or os.path.join(data_dir, "brain.db")
        cfg = {
            "server": {"host": host, "port": port, "token": self.token, "host_public": False},
            "upstream": {
                "base_url": self.upstream_base,
                "chat_path": "/v1/chat/completions",
                "model": self.upstream_model,
                "api_key": self.upstream_key,
                "timeout": 90.0,
                "extra_body": {},
            },
            "memory": {
                "enabled": True,
                "default_person": self.person,
                "default_scene": "qq",
                "top_k": self.top_k,
                "recent_k": 2,
                "inject_max_chars": 900,
                "inject_header": "【你记得关于这个人的事】",
                "dedupe_jaccard": 0.86,
                "db_path": db_path,
            },
            "extraction": {
                "enabled": True,
                "model": "",
                "max_items": 3,
                "timeout": 45.0,
                "max_concurrency": 2,
                "min_user_chars": 3,
                "temperature": 0.1,
                "max_tokens": 400,
            },
            "persons": {},
        }

        try:
            store = Store(cfg["memory"]["db_path"])
            upstream = Upstream(cfg)
            httpd = BrainHTTPServer((host, port), Handler, cfg=cfg, store=store, upstream=upstream)
        except OSError as exc:
            self._hub_mode = "external"
            logger.warning("[大肥鱼桥] 端口 %s 起不来（%s），当作已有中枢", port, exc)
            return
        except Exception as exc:
            self._hub_mode = "missing"
            logger.warning("[大肥鱼桥] 内置中枢启动失败：%s", exc)
            return

        thread = threading.Thread(
            target=httpd.serve_forever,
            kwargs={"poll_interval": 0.5},
            name="dafeiyu-hub",
            daemon=True,
        )
        thread.start()
        self._hub = httpd
        self._hub_mode = "embedded"
        logger.info(
            "[大肥鱼桥] 已启动内置记忆中枢 http://%s:%s（数据：%s）", host, port, cfg["memory"]["db_path"]
        )

    # ---------- HTTP ----------

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _post(self, path: str, payload: dict, timeout: float | None = None) -> dict:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base}{path}", data=data, headers=self._headers(), method="POST"
        )
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
        try:
            return json.loads(body or "{}")
        except json.JSONDecodeError:
            return {}

    def _get_json(self, path: str, timeout: float | None = None) -> dict:
        req = urllib.request.Request(f"{self.base}{path}", headers=self._headers(), method="GET")
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
        try:
            return json.loads(body or "{}")
        except json.JSONDecodeError:
            return {}

    # ---------- 自检上报（不阻塞、不抛异常） ----------

    def _diag_sync(self, name: str, detail: str = "") -> None:
        if not self.diag_enabled or not self.base:
            return
        try:
            self._post("/diag", {"name": name, "detail": detail, "source": "qq"}, timeout=4)
        except Exception:
            pass

    def _diag(self, name: str, detail: str = "") -> None:
        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, self._diag_sync, name, detail)
        except Exception:
            pass

    # ---------- 身份 & 范围 ----------

    def _is_private(self, event: Any) -> bool:
        try:
            return bool(event.is_private_chat())
        except Exception:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            return ("FriendMessage" in umo) or ("private" in umo.lower())

    def _should_handle(self, event: Any) -> bool:
        if not self.base:
            return False
        if self.only_private:
            return self._is_private(event)
        return True

    def _person_for(self, event: Any) -> tuple[str, str]:
        """返回 (person_id, scene)。群友各自成一个人，避免串到主人身上。"""
        sender = ""
        try:
            sender = str(event.get_sender_id() or "")
        except Exception:
            pass
        is_private = self._is_private(event)

        if not self.owner_ids or (sender and sender in self.owner_ids):
            person = self.person
        else:
            person = f"qq:{sender or 'unknown'}"

        if is_private:
            scene = f"qq-private:{sender or 'unknown'}"
        else:
            try:
                gid = str(event.get_group_id() or "")
            except Exception:
                gid = ""
            scene = f"qq-group:{gid or 'unknown'}"
        return person, scene

    # ---------- 钩子 ----------

    @filter.on_llm_request()
    async def inject_memory(self, event: Any, req: Any) -> None:
        person, scene = self._person_for(event)
        if not self.inject_enabled:
            self._diag("req_skip", "inject_enabled=false")
            return
        if not self._should_handle(event):
            self._diag("req_skip", f"not_private scene={scene}")
            return
        try:
            query = (getattr(req, "prompt", None) or getattr(event, "message_str", "") or "")[:400]
            if not query.strip():
                self._diag("req_skip", "empty_query")
                return
            data = await asyncio.to_thread(
                self._post, "/memory/recall", {"person_id": person, "query": query, "k": self.top_k}
            )
            block = (data or {}).get("block") or ""
            if not block:
                self._diag("req_ok", f"hits=0 person={person}")
                return
            req.system_prompt = ((req.system_prompt or "").rstrip() + "\n\n" + block).strip()
            self._diag("req_ok", f"hits={data.get('count')} person={person}")
            if self.debug:
                logger.info("[大肥鱼桥] 注入 %s 条记忆（%s）", data.get("count"), person)
        except urllib.error.URLError as exc:
            self._diag("req_error", f"unreachable: {exc}")
            if self.debug:
                logger.info("[大肥鱼桥] 中枢不可达，跳过注入：%s", exc)
        except Exception as exc:
            self._diag("req_error", f"{type(exc).__name__}: {exc}")
            logger.debug("[大肥鱼桥] 注入失败", exc_info=True)

    @filter.on_llm_response()
    async def remember(self, event: Any, resp: Any) -> None:
        person, scene = self._person_for(event)
        if not self.ingest_enabled:
            return
        if not self._should_handle(event):
            self._diag("resp_skip", f"not_private scene={scene}")
            return
        try:
            user_text = str(getattr(event, "message_str", "") or "").strip()
            reply = str(getattr(resp, "completion_text", "") or "").strip()
            if not user_text or not reply:
                self._diag("resp_skip", f"empty user={len(user_text)} reply={len(reply)}")
                return
            key = f"{user_text[:60]}|{reply[:60]}"
            now = time.time()
            if now - self._recent.get(key, 0.0) < 120:
                return
            self._recent[key] = now
            if len(self._recent) > 200:
                self._recent = {k: v for k, v in self._recent.items() if now - v < 300}

            result = await asyncio.to_thread(
                self._post,
                "/memory/ingest",
                {
                    "person_id": person,
                    "scene": scene,
                    "source": "qq",
                    "user_text": user_text,
                    "assistant_text": reply,
                    "wait": "0",
                },
            )
            self._diag("resp_ok", f"queued={bool(result)} person={person} scene={scene}")
            if self.debug:
                logger.info("[大肥鱼桥] 已提交记忆抽取（%s / %s）", person, scene)
        except urllib.error.URLError as exc:
            self._diag("resp_error", f"unreachable: {exc}")
            if self.debug:
                logger.info("[大肥鱼桥] 中枢不可达，跳过入库：%s", exc)
        except Exception as exc:
            self._diag("resp_error", f"{type(exc).__name__}: {exc}")
            logger.debug("[大肥鱼桥] 入库失败", exc_info=True)

    # ---------- 命令 ----------

    @filter.command("桥状态")
    async def bridge_status(self, event: Any):
        """看看记忆中枢通不通、库里有几条。"""
        if not self.base:
            yield event.plain_result("记忆桥没配置 brain_url，先到插件设置里填一下。")
            return
        try:
            stats = await asyncio.to_thread(self._get_json, "/memory/stats", 8)
        except Exception as exc:
            yield event.plain_result(f"连不上记忆中枢 {self.base}：{exc}")
            return
        mode = {"embedded": "自带中枢", "external": "外接中枢", "missing": "缺中枢"}.get(
            self._hub_mode, self._hub_mode
        )
        persons = "、".join(f"{p['person_id']}({p['count']})" for p in stats.get("persons", [])) or "暂无"
        yield event.plain_result(
            f"记忆中枢在线（{mode}）\n地址：{self.base}\n"
            f"记忆：{stats.get('atoms', 0)} 条 / {stats.get('turns', 0)} 轮\n人：{persons}"
        )

    async def terminate(self) -> None:
        if self._hub is not None:
            try:
                self._hub.shutdown()
                self._hub.server_close()
            except Exception:
                pass
            self._hub = None
        logger.info("[大肥鱼桥] 已卸载")
