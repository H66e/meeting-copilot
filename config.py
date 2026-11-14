# -*- coding: utf-8 -*-
"""加载 config.yaml，并从 api_key_env 声明的环境变量解析 API key。

也兼容本机 ~/.dsh/.credentials.yaml 中的 KEY: value 形式；凭据不会写入仓库。
推荐使用环境变量，不要把真实密钥直接写进配置文件。

用法：
    from config import load_config
    cfg = load_config()
    cfg["audio"]["sample_rate"]
    cfg["_providers"]["openai"]["api_key"]   # 已解析好
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

# 源码运行时指向项目目录；打包后指向 exe 所在目录，保证配置与输出可写。
# PyInstaller 的只读资源仍在 sys._MEIPASS，不把这些资源误当用户数据目录。
PROJECT_DIR = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
               else Path(__file__).resolve().parent)
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", PROJECT_DIR))
CONFIG_PATH = PROJECT_DIR / "config.yaml"
CRED_PATH = Path.home() / ".dsh" / ".credentials.yaml"

_DIRS = {
    "logs": PROJECT_DIR / "logs",
    "exports": PROJECT_DIR / "exports",
    "data": PROJECT_DIR / "data",
    "records": PROJECT_DIR / "records",
}


def init_console() -> None:
    """Windows 控制台强制 UTF-8，避免中文乱码。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass


def load_credentials() -> dict[str, str]:
    """从 DSH 凭据文件读取 API key（只在内存中使用，不落盘不外传）。"""
    keys: dict[str, str] = {}
    try:
        text = CRED_PATH.read_text(encoding="utf-8")
    except Exception:
        return keys
    # 形如 `DEEPSEEK_API_KEY: sk-xxx` 或 `- KEY: value`
    for m in re.finditer(r"([A-Z0-9_]*(?:API_KEY|TOKEN|SECRET)[A-Z0-9_]*)\s*:\s*['\"]?([^\s'\"#]+)", text):
        keys[m.group(1)] = m.group(2)
    # 环境变量由各 provider 的 api_key_env 显式声明，在 load_config() 中解析。
    return keys


def get(cfg: dict, path: str, default: Any = None) -> Any:
    """cfg 里按 a.b.c 取值"""
    cur: Any = cfg
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _ensure_dirs() -> None:
    for d in _DIRS.values():
        d.mkdir(parents=True, exist_ok=True)


def load_config(path: str | os.PathLike | None = None) -> dict:
    import yaml

    p = Path(path) if path else CONFIG_PATH
    if not p.exists() and path is None:
        example = PROJECT_DIR / "config.example.yaml"
        if example.exists():
            p = example
    with open(p, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    _ensure_dirs()
    cfg["_project_dir"] = str(PROJECT_DIR)
    cfg["_dirs"] = {k: str(v) for k, v in _DIRS.items()}
    cfg["_path"] = str(p)

    creds = load_credentials()
    cfg["_credentials"] = creds
    providers = {}
    for name, prov in (cfg.get("llm", {}).get("providers") or {}).items():
        prov = dict(prov or {})
        key_env = str(prov.get("api_key_env", "") or "")
        if key_env:
            prov["api_key"] = creds.get(key_env) or os.environ.get(key_env, "")
        else:
            prov["api_key"] = prov.get("api_key", "")
        providers[name] = prov
    cfg["_providers"] = providers
    cfg["_missing_keys"] = [n for n, p in providers.items() if not p.get("api_key")]
    return cfg


def provider(cfg: dict, name: str) -> dict:
    """取 provider 配置（含已解析 api_key）。"""
    p = cfg.get("_providers", {}).get(name)
    if not p:
        raise KeyError(f"未配置 LLM provider: {name}")
    return p


if __name__ == "__main__":
    init_console()
    c = load_config()
    print("config:", c["_path"])
    print("providers:", {k: {"base_url": v.get("base_url"), "key": bool(v.get("api_key")),
                            "type": v.get("type")} for k, v in c["_providers"].items()})
    print("missing keys:", c["_missing_keys"])
    print("dirs:", c["_dirs"])
