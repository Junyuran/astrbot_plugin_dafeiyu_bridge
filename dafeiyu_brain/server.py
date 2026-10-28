"""启动入口：python -m dafeiyu_brain.server --config config.json"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from logging.handlers import RotatingFileHandler

from . import config
from .httpapi import BrainHTTPServer, Handler
from .llm import Upstream
from .store import Store

log = logging.getLogger("brain")


def setup_logging(cfg: dict) -> None:
    server = cfg.get("server") or {}
    path = server.get("log_file") or ""
    level = logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    if path:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        file_handler = RotatingFileHandler(
            path, maxBytes=int(server.get("log_max_bytes", 5_000_000)), backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)


def banner(cfg: dict, store: Store, upstream: Upstream, host: str, port: int) -> None:
    stats = store.stats()
    print("")
    print("  大肥鱼记忆中枢  Dafeiyu Brain")
    print("  " + "-" * 52)
    print(f"  监听        http://{host}:{port}")
    print(f"  记忆面板    http://{host}:{port}/admin")
    print(f"  上游模型    {upstream.base_url}  ({upstream.model})")
    print(f"  记忆库      {store.path}  （{stats['atoms']} 条 / {stats['turns']} 轮）")
    key = (cfg.get("upstream") or {}).get("api_key") or ""
    print(f"  API Key     {'已配置' if key else '！！未配置，聊天会 401'}")
    token = (cfg.get("server") or {}).get("token") or ""
    print(f"  访问令牌    {'已开启' if token else '未设置（仅监听本机时可以不管）'}")
    print("  " + "-" * 52)
    print("  桌宠设置里把 base_url 改成上面的地址即可；Ctrl+C 退出。")
    print("")


def check(cfg: dict) -> int:
    """只做连通性检查：健康检查 + 一次极短的模型调用。"""
    store = Store(cfg["memory"]["db_path"])
    upstream = Upstream(cfg)
    stats = store.stats()
    print(f"记忆库 {store.path}: {stats['atoms']} 条记忆 / {stats['turns']} 轮对话")
    print(f"上游   {upstream.url}  model={upstream.model}")
    if not upstream.api_key:
        print("！ upstream.api_key 为空，请在 config.json 或 secrets.json 里填")
        return 2
    started = time.time()
    try:
        text = upstream.chat(
            [{"role": "user", "content": "只回复两个字：在的"}],
            max_tokens=16,
            temperature=0.0,
            timeout=30,
        )
    except Exception as exc:
        print(f"！ 调用失败：{exc}")
        return 1
    print(f"上游正常（{time.time() - started:.1f}s）：{text.strip()[:60]}")
    return 0


def _utf8_console() -> None:
    """Windows 控制台默认 GBK，中文日志会炸；这里强制 UTF-8。

    pythonw.exe（无窗口启动）下 sys.stdout/stderr 是 None，直接 print 会崩，
    所以先兜底到 devnull。
    """
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    parser = argparse.ArgumentParser(description="大肥鱼记忆中枢 / OpenAI 兼容网关")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--check", action="store_true", help="只检查上游连通性后退出")
    args = parser.parse_args(argv)

    cfg = config.load(args.config)
    setup_logging(cfg)

    if args.check:
        return check(cfg)

    server_cfg = cfg.get("server") or {}
    host = args.host or server_cfg.get("host") or "127.0.0.1"
    port = int(args.port or server_cfg.get("port") or 8787)

    store = Store(cfg["memory"]["db_path"])
    upstream = Upstream(cfg)

    try:
        httpd = BrainHTTPServer((host, port), Handler, cfg=cfg, store=store, upstream=upstream)
    except OSError as exc:
        log.error("端口 %s:%s 起不来：%s（是不是已经开了一个？）", host, port, exc)
        return 1

    banner(cfg, store, upstream, host, port)

    def _stop(*_args) -> None:
        log.info("收到退出信号，正在关闭…")
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _stop)
    except (ValueError, OSError):
        pass

    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        httpd.server_close()
        log.info("已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
