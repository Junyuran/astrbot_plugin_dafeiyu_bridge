"""日志出口。

独立部署时用标准 logging；内置到 AstrBot 里跑时，由插件调用 attach()
把 brain.* 的日志转发给 AstrBot 自己的 logger，避免绕过它的日志体系。
"""

from __future__ import annotations

import logging

ROOT_NAME = "brain"
_PREFIX = "[中枢] "

_handler: logging.Handler | None = None


def attach(target: logging.Logger, level: int = logging.INFO) -> None:
    """把 brain.* 的日志转发到宿主 logger（AstrBot 里用 astrbot.api.logger）。"""
    global _handler
    root = logging.getLogger(ROOT_NAME)

    if _handler is not None:
        root.removeHandler(_handler)

    class _Forward(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            try:
                message = record.getMessage()
                if record.exc_info:
                    message += "\n" + logging.Formatter().formatException(record.exc_info)
                target.log(record.levelno, _PREFIX + message)
            except Exception:
                pass

    _handler = _Forward()
    root.handlers = [_handler]
    root.propagate = False
    root.setLevel(level)


def detach() -> None:
    global _handler
    root = logging.getLogger(ROOT_NAME)
    if _handler is not None:
        root.removeHandler(_handler)
        _handler = None
    root.propagate = True
