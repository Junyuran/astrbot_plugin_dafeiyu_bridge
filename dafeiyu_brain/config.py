"""配置加载：config.json -> secrets.json -> 环境变量，逐层覆盖。"""

from __future__ import annotations

import copy
import json
import os
from typing import Any

DEFAULTS: dict[str, Any] = {
    "server": {
        "host": "127.0.0.1",
        "port": 8787,
        "token": "",
        "log_file": "logs/brain.log",
        "log_max_bytes": 5_000_000,
    },
    "upstream": {
        "base_url": "https://api.deepseek.com",
        "chat_path": "/v1/chat/completions",
        "model": "deepseek-v4-flash",
        "api_key": "",
        "timeout": 90.0,
        "extra_body": {},
        "pass_headers": ["accept"],
    },
    "memory": {
        "enabled": True,
        "default_person": "owner",
        "default_scene": "desktop",
        "top_k": 5,
        "recent_k": 2,
        "inject_max_chars": 900,
        "inject_header": "【你记得关于这个人的事】",
        "dedupe_jaccard": 0.86,
        "db_path": "data/brain.db",
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
    "persons": {"owner": "主人"},
}


def _deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (extra or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:  # 配置写坏了不要直接崩，打印出来继续用默认值
        print(f"[config] 读取 {path} 失败：{exc}")
        return {}


def _apply_env(cfg: dict) -> dict:
    env_map = {
        "DAFEIYU_PORT": ("server", "port", int),
        "DAFEIYU_HOST": ("server", "host", str),
        "DAFEIYU_TOKEN": ("server", "token", str),
        "DAFEIYU_UPSTREAM": ("upstream", "base_url", str),
        "DAFEIYU_MODEL": ("upstream", "model", str),
        "DAFEIYU_API_KEY": ("upstream", "api_key", str),
        "DAFEIYU_DB": ("memory", "db_path", str),
    }
    for env, (section, key, cast) in env_map.items():
        raw = os.environ.get(env)
        if raw is None or raw == "":
            continue
        try:
            cfg[section][key] = cast(raw)
        except Exception:
            pass
    return cfg


def load(path: str = "config.json") -> dict[str, Any]:
    base = os.path.dirname(os.path.abspath(path))
    cfg = _deep_merge(DEFAULTS, _read_json(path))

    secrets_path = os.path.join(base, "secrets.json")
    cfg = _deep_merge(cfg, _read_json(secrets_path))
    cfg = _apply_env(cfg)

    # 相对路径一律按 config.json 所在目录解析，避免从别处启动时找不到库
    for section, key in (("memory", "db_path"), ("server", "log_file")):
        value = cfg[section].get(key) or ""
        if value and not os.path.isabs(value):
            cfg[section][key] = os.path.normpath(os.path.join(base, value))
    return cfg
