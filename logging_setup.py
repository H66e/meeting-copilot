# -*- coding: utf-8 -*-
"""日志：文件（UTF-8，按天滚动）+ 控制台。"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from config import _DIRS, init_console

_ready = False
_FMT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"


def setup(level: int = logging.INFO) -> None:
    global _ready
    if _ready:
        return
    init_console()
    log_dir: Path = _DIRS["logs"]
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("mc")
    root.setLevel(level)
    root.handlers.clear()

    fh = logging.handlers.TimedRotatingFileHandler(
        log_dir / "assistent.log", when="midnight", backupCount=14, encoding="utf-8")
    fh.setFormatter(logging.Formatter(_FMT))
    root.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    ch.setLevel(level)
    root.addHandler(ch)
    _ready = True


def get_logger(name: str = "app") -> logging.Logger:
    setup()
    return logging.getLogger(f"mc.{name}")
