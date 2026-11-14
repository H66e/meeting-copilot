# -*- coding: utf-8 -*-
"""Meeting Copilot · 环境体检 CLI（tools/selfcheck.py）

用法：
    python tools/selfcheck.py                # 完整体检（含网络 ping、whisper 加载、热键注册）
    python tools/selfcheck.py --no-network   # 跳过 LLM 网络 ping
    python tools/selfcheck.py --strict       # ⚠️ 警告也算失败（默认只有 ❌ 才非 0）
    python tools/selfcheck.py --quiet        # 只打印汇总

输出：中文 ✅/❌/⚠️ 表格，并写入 ``logs/selfcheck_YYYYmmdd_HHMMSS.txt``。
退出码：0 仅当没有 ❌（--strict 时要求全 ✅）。
main.py 的 ``--check`` 会调用 ``run_all(cfg) -> bool``。
"""
from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import os
import platform
import shutil
import sys
import time
import traceback
import unicodedata
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from config import PROJECT_DIR
_ROOT = PROJECT_DIR

ICON = {"ok": "✅", "warn": "⚠️", "fail": "❌", "skip": "⏭️"}


# ---------------------------------------------------------------- 文本宽度工具（表格对齐）
def display_width(text: object) -> int:
    """显示宽度：CJK 全角字符算 2 列。"""
    total = 0
    for ch in str(text):
        total += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return total


def pad_display(text: object, width: int) -> str:
    s = str(text)
    return s + " " * max(0, int(width) - display_width(s))


def truncate_display(text: object, max_cols: int, ellipsis: str = "…") -> str:
    s = str(text)
    if max_cols <= 0 or display_width(s) <= max_cols:
        return s
    limit = max_cols - display_width(ellipsis)
    out, width = [], 0
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if width + cw > limit:
            break
        out.append(ch)
        width += cw
    return "".join(out) + ellipsis


# ---------------------------------------------------------------- 结果收集
class Result:
    """一条体检结果。"""

    __slots__ = ("status", "name", "detail", "seconds")

    def __init__(self, status: str, name: str, detail: str = "", seconds: float = 0.0):
        self.status = status
        self.name = name
        self.detail = str(detail or "")
        self.seconds = float(seconds or 0.0)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def line(self, detail_width: int = 110) -> str:
        icon = ICON.get(self.status, "?")
        name = pad_display(self.name, 22)
        detail = truncate_display(self.detail, detail_width)
        timing = f" [{self.seconds:.1f}s]" if self.seconds >= 0.5 else ""
        return f"{icon}  {name}{detail}{timing}"


class _Checker:
    def __init__(self, cfg: dict, network: bool = True, verbose: bool = True):
        self.cfg = cfg or {}
        self.network = network
        self.verbose = verbose
        self.results: list[Result] = []

    def add(self, status: str, name: str, detail: str = "", seconds: float = 0.0) -> Result:
        result = Result(status, name, detail, seconds)
        self.results.append(result)
        if self.verbose and status != "ok":
            print("  " + result.line(), flush=True)
        return result

    def run(self, status_ok: str, name: str, fn) -> Optional[Any]:
        """执行单项检查：fn() 返回 (status, detail) 或 detail；异常 -> ❌。"""
        t0 = time.time()
        try:
            out = fn()
            if isinstance(out, tuple) and len(out) >= 2:
                status, detail = out[0], out[1]
            else:
                status, detail = status_ok, str(out or "")
            self.add(status, name, detail, time.time() - t0)
            return out
        except Exception as e:  # noqa: BLE001
            self.add("fail", name, f"{type(e).__name__}: {e}", time.time() - t0)
            return None


# ---------------------------------------------------------------- 单项检查
_CORE_DEPS = [
    ("numpy", "numpy"), ("PyYAML", "yaml"), ("faster-whisper", "faster_whisper"),
    ("ctranslate2", "ctranslate2"), ("av", "av"), ("Pillow", "PIL"),
    ("httpx", "httpx"), ("requests", "requests"), ("keyboard", "keyboard"),
    ("soundcard", "soundcard"), ("jieba", "jieba"),
]
_OPTIONAL_DEPS = [
    ("opencv-python", "cv2"), ("rapidocr", "rapidocr"),
    ("pypdf", "pypdf"), ("python-docx", "docx"), ("python-pptx", "pptx"),
]


def _dep_versions(deps: list[tuple[str, str]]) -> tuple[list[str], list[str]]:
    found, missing = [], []
    for dist_name, module_name in deps:
        try:
            importlib.import_module(module_name)
        except Exception:  # noqa: BLE001
            missing.append(dist_name)
            continue
        try:
            version = importlib.metadata.version(dist_name)
        except importlib.metadata.PackageNotFoundError:
            version = "?"
        found.append(f"{dist_name} {version}")
    return found, missing


def _check_python() -> tuple[str, str]:
    version = sys.version.split()[0]
    bits = platform.architecture()[0]
    if sys.version_info < (3, 10):
        return "fail", f"Python {version} 过低（需 ≥3.10）"
    return "ok", f"Python {version} · {bits} · {platform.system()} {platform.release()}"


def _check_deps() -> tuple[str, str]:
    found, missing = _dep_versions(_CORE_DEPS)
    opt_found, opt_missing = _dep_versions(_OPTIONAL_DEPS)
    if missing:
        return "fail", f"缺少核心依赖: {', '.join(missing)}（已装 {len(found)} 个）"
    detail = f"核心 {len(found)}/{len(_CORE_DEPS)} 个已装"
    if opt_missing:
        detail += f"；可选缺失: {', '.join(opt_missing)}"
    return "ok", detail


def _check_tk() -> tuple[str, str]:
    import tkinter as tk
    t0 = time.time()
    root = tk.Tk()
    try:
        root.withdraw()
        version = str(root.tk.call("info", "patchlevel"))
        width, height = root.winfo_screenwidth(), root.winfo_screenheight()
    finally:
        try:
            root.destroy()
        except Exception:
            pass
    used = time.time() - t0
    if width <= 0 or height <= 0:
        return "fail", "没有可用显示（Tk 屏幕尺寸为 0）"
    return "ok", f"tkinter {version} · 屏幕 {width}×{height} · 建窗 {used:.2f}s"


def _check_config(cfg: dict) -> tuple[str, str]:
    if not cfg:
        return "fail", "config.yaml 未加载"
    missing_sections = [s for s in ("audio", "asr", "llm", "ui", "hotkeys")
                        if not isinstance(cfg.get(s), dict)]
    if missing_sections:
        return "fail", f"配置缺少段: {', '.join(missing_sections)}"
    screen = (cfg.get("ui") or {}).get("screen", "?")
    size = (cfg.get("ui") or {}).get("size", "?")
    return "ok", f"{cfg.get('_path', 'config.yaml')} · ui.screen={screen} · size={size}"


def _check_credentials(cfg: dict) -> tuple[str, str]:
    providers = cfg.get("_providers") or {}
    if not providers:
        return "fail", "未解析到任何 LLM provider"
    missing = [name for name, prov in providers.items() if not prov.get("api_key")]
    has = [name for name, prov in providers.items() if prov.get("api_key")]
    if missing:
        return "fail", f"缺少 API key: {', '.join(missing)}（已有: {', '.join(has) or '无'}）"
    return "ok", f"已解析: {', '.join(f'{name}(有key)' for name in has)}"


def _list_output_devices() -> tuple[list[str], str]:
    """枚举输出设备：优先 audio_capture.list_output_devices()，失败回退 soundcard。"""
    names: list[str] = []
    source = ""
    try:
        module = importlib.import_module("audio_capture")
        fn = getattr(module, "list_output_devices", None)
        if callable(fn):
            names = [str(x) for x in (fn() or [])]
            source = "audio_capture"
        if not names:
            cls = getattr(module, "LoopbackCapture", None)
            method = getattr(cls, "list_output_devices", None) if cls else None
            if callable(method):
                dummy = type("_Dummy", (), {})()
                names = [str(x) for x in (method(dummy) or [])]
                source = "audio_capture.LoopbackCapture"
    except Exception:  # noqa: BLE001
        names = []
    names = list(dict.fromkeys(n for n in names if n))
    if not names:
        try:
            import soundcard as sc
            names = [sp.name for sp in sc.all_speakers()]
            source = "soundcard 直连"
        except Exception:  # noqa: BLE001
            names = []
    return names, source


def _check_audio_devices() -> tuple[str, str]:
    names, source = _list_output_devices()
    if not names:
        return "fail", "未枚举到任何输出设备（回环采集可能不可用）"
    try:
        import soundcard as sc
        default_name = sc.default_speaker().name
    except Exception:  # noqa: BLE001
        default_name = "?"
    src = f"（来源: {source}）" if source else ""
    return "ok", f"{len(names)} 个输出设备 · 默认: {default_name}{src}"


def _check_asr_module() -> tuple[str, str]:
    try:
        module = importlib.import_module("asr_engine")
    except Exception as e:  # noqa: BLE001
        return "warn", f"asr_engine.py 未就绪（{type(e).__name__}），跳过模块级检查"
    if not hasattr(module, "AsrEngine"):
        return "warn", "asr_engine.py 已存在但缺少 AsrEngine 类"
    return "ok", "asr_engine.AsrEngine 可导入"


def _check_whisper_and_asr(cfg: dict) -> tuple[Result, Result]:
    """加载 whisper 模型并做合成音频冒烟；返回两条结果。"""
    import numpy as np
    from faster_whisper import WhisperModel

    model_name = str((cfg.get("asr") or {}).get("model", "small"))
    compute_type = str((cfg.get("asr") or {}).get("compute_type", "int8"))
    threads = int((cfg.get("asr") or {}).get("cpu_threads", 8) or 8)
    t0 = time.time()
    model = WhisperModel(model_name, device="cpu", compute_type=compute_type,
                         cpu_threads=min(8, max(1, threads)), local_files_only=True)
    load_s = time.time() - t0
    load_result = Result("ok", "Whisper 离线加载",
                         f"{model_name} · compute_type={compute_type} · 耗时 {load_s:.1f}s",
                         load_s)
    # 合成 1 秒 440Hz 正弦，冒烟测试（不要求识别出文字；跑两次取第二次，避开首帧预热）
    sr = 16000
    audio = (0.1 * np.sin(2 * np.pi * 440 * np.arange(sr) / sr)).astype(np.float32)

    def _run_once() -> tuple[float, str]:
        t = time.time()
        segments, _info = model.transcribe(audio, language="zh", beam_size=1, vad_filter=False)
        text = "".join(getattr(seg, "text", "") for seg in segments)
        return time.time() - t, text

    _run_once()                      # 预热（不计入结果）
    dur, text = _run_once()
    rtf = dur / (len(audio) / sr)
    smoke_result = Result("ok", "ASR 合成音频冒烟",
                          f"耗时 {dur:.2f}s · 输出 {len(text)} 字"
                          f"（1 秒片段含固定编码开销，不能代表整场 RTF；合成正弦识别不出字属正常）",
                          dur)
    return load_result, smoke_result


def _llm_ping(cfg: dict, provider_name: str, model: str,
              timeout: float = 30.0) -> tuple[float, str]:
    """直连 OpenAI 兼容口 ping 一次，返回 (秒, 回复片段)。不打印 key。"""
    from config import provider
    import httpx

    prov = provider(cfg, provider_name)
    api_key = str(prov.get("api_key") or "")
    base_url = str(prov.get("base_url") or "").rstrip("/")
    if not api_key:
        raise RuntimeError("缺少 API key")
    if not base_url:
        raise RuntimeError("缺少 base_url")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "只回复两个字：正常"}],
        "max_tokens": 8,
        "temperature": 0.0,
        "stream": False,
    }
    t0 = time.time()
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(f"{base_url}/chat/completions",
                           headers={"Authorization": f"Bearer {api_key}"},
                           json=payload)
    resp.raise_for_status()
    data = resp.json()
    try:
        text = str(data["choices"][0]["message"]["content"] or "").strip()
    except Exception:  # noqa: BLE001
        text = ""
    return time.time() - t0, text[:20]


def _check_llm(cfg: dict, kind: str, network: bool) -> tuple[str, str]:
    if not network:
        return "skip", "跳过（--no-network）"
    if kind == "fast":
        primary = cfg.get("llm") or {}
        cfg_item = primary.get("fast") or {}
        provider_name = str(cfg_item.get("provider", "deepseek"))
        model = str(cfg_item.get("model", "deepseek-flash"))
    else:
        cfg_item = (cfg.get("llm") or {}).get("primary") or {}
        provider_name = str(cfg_item.get("provider", "openai"))
        model = str(cfg_item.get("model", "gpt-4o-mini"))
    # DeepSeek 官方口偶发 SSL EOF（实测约 1/6），重试 3 次再判定；它是可降级项，
    # 真失败也不能判"必须修复"，因为主脑会自动兜底、听写不受影响。
    attempts = 3 if kind == "fast" else 2
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            seconds, text = _llm_ping(cfg, provider_name, model)
            extra = "" if i == 0 else f"（第 {i + 1} 次重试成功）"
            return "ok", f"{model} @ {provider_name} · {seconds:.1f}s · 回复={text or '空'}{extra}"
        except Exception as e:  # noqa: BLE001
            last_err = e
            if i + 1 < attempts:
                time.sleep(1.5)
    if kind == "fast":
        return "warn", (f"{model} @ {provider_name} 连接不稳定（{type(last_err).__name__}，"
                        f"{attempts} 次重试均失败）；助手会自动改用主脑兜底，不影响听写")
    return "fail", f"{model} @ {provider_name} {type(last_err).__name__}: {last_err}"


def _check_kb(cfg: dict) -> tuple[str, str]:
    try:
        module = importlib.import_module("knowledge_base")
        kb_class = getattr(module, "KnowledgeBase")
        kb = kb_class(cfg)
        kb.load()
        stats = kb.stats() if hasattr(kb, "stats") else {}
        chunks = int((stats or {}).get("chunks") or 0)
        if chunks > 0:
            return "ok", f"chunks={chunks} docs={stats.get('docs', '?')} built_at={stats.get('built_at', '?')}"
        return "warn", "知识库可加载但索引为空（运行 python tools/build_kb.py 构建）"
    except Exception as e:  # noqa: BLE001
        index_path = _ROOT / "data" / "kb" / "index.json"
        if index_path.exists():
            try:
                data = json.loads(index_path.read_text(encoding="utf-8"))
                chunks = len(data.get("chunks") or [])
                return "ok", f"knowledge_base.py 未就绪，但 index.json 存在（chunks={chunks}）"
            except Exception:  # noqa: BLE001
                pass
        return "warn", f"knowledge_base.py 未就绪/索引未构建（{type(e).__name__}）"


def _check_hotkeys(cfg: dict) -> tuple[str, str]:
    try:
        from hotkeys import normalize_hotkey, parse_bindings
    except Exception as e:  # noqa: BLE001
        return "warn", f"hotkeys.py 未就绪（{type(e).__name__}）"
    combos = parse_bindings(cfg)
    if not combos:
        return "fail", "config.yaml hotkeys 段为空或全部无效"
    try:
        import keyboard
    except Exception as e:  # noqa: BLE001
        return "fail", f"keyboard 库不可用: {type(e).__name__}: {e}"
    ok, failed = 0, []
    handles: list[Any] = []
    for config_key, combo in combos.items():
        try:
            handle = keyboard.add_hotkey(combo, lambda: None, suppress=False)
            handles.append(handle)
            ok += 1
        except Exception as e:  # noqa: BLE001
            failed.append(f"{combo}({type(e).__name__})")
    for handle in handles:
        try:
            keyboard.remove_hotkey(handle)
        except Exception:  # noqa: BLE001
            pass
    detail = f"{ok}/{len(combos)} 可注册: " + ", ".join(combos.values())
    if failed:
        return "warn", f"{detail}；失败: {', '.join(failed)}"
    return "ok", detail


def _check_dirs() -> tuple[str, str]:
    targets = ["logs", "exports", "records", "data"]
    writable, failed = [], []
    for name in targets:
        path = _ROOT / name
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".selfcheck_write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            writable.append(name)
        except Exception as e:  # noqa: BLE001
            failed.append(f"{name}({type(e).__name__})")
    if failed:
        return "fail", f"目录不可写: {', '.join(failed)}"
    return "ok", f"{'/'.join(writable)} 均可写"


def _check_disk() -> tuple[str, str]:
    usage = shutil.disk_usage(str(_ROOT))
    free_gb = usage.free / (1024 ** 3)
    total_gb = usage.total / (1024 ** 3)
    detail = f"剩余 {free_gb:.1f} GB / 共 {total_gb:.1f} GB"
    if free_gb < 0.5:
        return "fail", detail
    if free_gb < 2.0:
        return "warn", detail
    return "ok", detail


def _check_ui_module() -> tuple[str, str]:
    """按 config.yaml 的 ui.layout 检查实际会用到的界面模块。"""
    cfg = {}
    try:
        from config import get, load_config
        cfg = load_config()
        layout = str(get(cfg, "ui.layout", "split") or "split").strip().lower()
        mod_name = "ui_split" if layout in ("split", "split2col", "wide", "dual") else "ui_overlay"
    except Exception:
        layout, mod_name = "tabs", "ui_overlay"
    try:
        module = importlib.import_module(mod_name)
    except Exception as e:  # noqa: BLE001
        return "fail", f"{mod_name}.py 不可导入: {type(e).__name__}: {e}"
    if not hasattr(module, "OverlayUI"):
        return "fail", f"{mod_name}.py 缺少 OverlayUI 类"
    screen = "副屏"
    try:
        from config import get
        screen = "副屏" if str(get(cfg, "ui.screen", "secondary")) == "secondary" else "主屏"
    except Exception:
        pass
    return "ok", f"{mod_name}.OverlayUI 可导入（{layout} 布局 · {screen}）"


def _check_session_store() -> tuple[str, str]:
    try:
        module = importlib.import_module("session_store")
    except Exception as e:  # noqa: BLE001
        return "warn", f"session_store.py 未就绪（{type(e).__name__}）"
    if not hasattr(module, "SessionStore"):
        return "warn", "session_store.py 缺少 SessionStore 类"
    return "ok", "session_store.SessionStore 可导入"


def _check_brain_module() -> tuple[str, str]:
    try:
        module = importlib.import_module("brain")
    except Exception as e:  # noqa: BLE001
        return "warn", f"brain.py 未就绪（{type(e).__name__}）"
    if not hasattr(module, "Brain"):
        return "warn", "brain.py 缺少 Brain 类"
    return "ok", "brain.Brain 可导入"


# ---------------------------------------------------------------- 主流程
def run_all(cfg: Optional[dict] = None, *, network: bool = True, write_report: bool = True,
            strict: bool = False, verbose: bool = True) -> bool:
    """执行全部体检。返回 True=无 ❌（strict 时要求全 ✅）。main.py --check 使用。"""
    from config import init_console, load_config

    init_console()
    if cfg is None:
        try:
            cfg = load_config()
        except Exception as e:  # noqa: BLE001
            print(f"[警告] 读取 config.yaml 失败: {e}")
            cfg = {}

    t_start = time.time()
    print("=" * 68)
    print("  Meeting Copilot · 环境体检")
    print(f"  项目: {_ROOT}")
    print(f"  时间: {time.strftime('%Y-%m-%d %H:%M:%S')}  网络 ping: {'开启' if network else '关闭'}")
    print("=" * 68)

    checker = _Checker(cfg, network=network, verbose=verbose)
    checker.run("ok", "Python 解释器", _check_python)
    checker.run("ok", "核心依赖", _check_deps)
    checker.run("ok", "tkinter / 显示器", _check_tk)
    checker.run("ok", "配置文件", lambda: _check_config(cfg))
    checker.run("ok", "LLM 凭据", lambda: _check_credentials(cfg))
    checker.run("ok", "回环输出设备", _check_audio_devices)
    checker.run("ok", "ASR 模块", _check_asr_module)
    checker.run("ok", "大脑模块", _check_brain_module)
    checker.run("ok", "UI 模块", _check_ui_module)
    checker.run("ok", "会话存储", _check_session_store)
    checker.run("ok", "知识库", lambda: _check_kb(cfg))
    checker.run("ok", "热键注册", lambda: _check_hotkeys(cfg))
    checker.run("ok", "目录可写", _check_dirs)
    checker.run("ok", "磁盘空间", _check_disk)

    # whisper 加载 + ASR 冒烟（较重，合并执行以便复用模型）
    whisper_results: Optional[tuple[Result, Result]] = None

    def _whisper_both() -> tuple[str, str]:
        nonlocal whisper_results
        load_result, smoke_result = _check_whisper_and_asr(cfg)
        whisper_results = (load_result, smoke_result)
        return "ok", load_result.detail

    checker.run("ok", "Whisper 离线加载", _whisper_both)
    if whisper_results is not None:
        smoke = whisper_results[1]
        checker.add(smoke.status, smoke.name, smoke.detail, smoke.seconds)
    else:
        checker.add("skip", "ASR 合成音频冒烟", "模型未加载，跳过")

    if network:
        checker.run("ok", "LLM fast ping",
                    lambda: _check_llm(cfg, "fast", network))
        checker.run("ok", "LLM primary ping",
                    lambda: _check_llm(cfg, "primary", network))
    else:
        checker.add("skip", "LLM fast ping", "跳过（--no-network）")
        checker.add("skip", "LLM primary ping", "跳过（--no-network）")

    # ---- 汇总
    elapsed = time.time() - t_start
    results = checker.results
    failed = [r for r in results if r.status == "fail"]
    warned = [r for r in results if r.status == "warn"]
    skipped = [r for r in results if r.status == "skip"]
    ok_count = sum(1 for r in results if r.status == "ok")

    table = render_table(results)
    print()
    print(table)
    print()
    print(f"汇总: ✅ {ok_count} 项通过 · ⚠️ {len(warned)} 项警告 · "
          f"❌ {len(failed)} 项失败 · ⏭️ {len(skipped)} 项跳过 · 用时 {elapsed:.1f}s")
    if warned:
        print("警告项（不致命，可继续使用）:")
        for r in warned:
            print(f"  ⚠️ {r.name}: {truncate_display(r.detail, 90)}")
    if failed:
        print("失败项（必须修复）:")
        for r in failed:
            print(f"  ❌ {r.name}: {truncate_display(r.detail, 90)}")

    report_path = ""
    if write_report:
        try:
            report_path = str(_write_report(results, cfg, network, elapsed))
            print(f"\n报告已写入: {report_path}")
        except Exception as e:  # noqa: BLE001
            print(f"\n[警告] 报告写入失败: {type(e).__name__}: {e}")

    passed = not failed and (not strict or not warned)
    if passed:
        print(f"\n✅ 环境自检通过{'（含警告）' if warned else '，全部正常'}，"
              f"可以运行 python main.py")
    else:
        print("\n❌ 环境自检未通过，请先修复上面的失败项")
    return passed


def render_table(results: list[Result], detail_width: int = 104) -> str:
    """渲染中文 ✅/❌ 对齐表格。"""
    name_width = max([display_width("检查项")] + [display_width(r.name) for r in results] + [10])
    header = f"{pad_display('状态', 3)}  {pad_display('检查项', name_width)}  {'结果'}"
    lines = [header, "-" * min(140, display_width(header) + detail_width)]
    for r in results:
        icon = ICON.get(r.status, "?")
        detail = truncate_display(r.detail, detail_width)
        timing = f" [{r.seconds:.1f}s]" if r.seconds >= 0.5 else ""
        lines.append(f"{pad_display(icon, 3)}  {pad_display(r.name, name_width)}  {detail}{timing}")
    return "\n".join(lines)


def _write_report(results: list[Result], cfg: dict, network: bool, elapsed: float) -> Path:
    logs_dir = _ROOT / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / f"selfcheck_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    lines = [
        "Meeting Copilot · 环境体检报告",
        f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"项目: {_ROOT}",
        f"配置: {cfg.get('_path', '?')}",
        f"Python: {sys.version.split()[0]} ({platform.architecture()[0]}) · {platform.platform()}",
        f"网络 ping: {'开启' if network else '关闭'} · 用时 {elapsed:.1f}s",
        "",
        render_table(results, detail_width=200),
        "",
        f"汇总: ✅ {sum(1 for r in results if r.status == 'ok')} · "
        f"⚠️ {sum(1 for r in results if r.status == 'warn')} · "
        f"❌ {sum(1 for r in results if r.status == 'fail')} · "
        f"⏭️ {sum(1 for r in results if r.status == 'skip')}",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Meeting Copilot · 环境体检")
    ap.add_argument("--no-network", action="store_true", help="跳过两个 LLM 网络 ping")
    ap.add_argument("--strict", action="store_true", help="警告（⚠️）也算不通过")
    ap.add_argument("--quiet", action="store_true", help="只打印表格与汇总")
    ap.add_argument("--no-report", action="store_true", help="不写 logs/selfcheck_*.txt")
    args = ap.parse_args(argv)
    try:
        passed = run_all(network=not args.no_network, write_report=not args.no_report,
                         strict=args.strict, verbose=not args.quiet)
    except KeyboardInterrupt:
        print("\n已中断")
        return 130
    except Exception:  # noqa: BLE001
        print("自检异常退出:\n" + traceback.format_exc())
        return 1
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
