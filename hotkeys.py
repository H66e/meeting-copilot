# -*- coding: utf-8 -*-
"""组会助手 · 全局热键管理（keyboard 库）

契约（INTERFACES.md C2）：
    class HotkeyManager:
        def __init__(self, bindings: dict[str, Callable[[], None]], log=None): ...
        def start(self) -> bool: ...
        def stop(self) -> None: ...

设计要点：
- 单个热键注册失败（被占用 / 无桌面会话）只 warning，不影响其它键；
- 回调必须立即返回：main.py 里传进来的回调只做 ``bus.command(...)``（入队），绝不阻塞；
- 注册/注销线程安全；``stop()`` 可重复调用。

main.py 用法：
    from hotkeys import HotkeyManager
    binds = {cfg["hotkeys"]["toggle_listen"]: lambda: bus.command("toggle_listen"), ...}
    hk = HotkeyManager(binds, log); hk.start(); ...; hk.stop()

也可以直接用辅助函数：
    from hotkeys import build_bindings
    hk = HotkeyManager(build_bindings(cfg, bus.command), log)

自测：``python hotkeys.py --selftest``（注册 3 个键并提示，Ctrl+C 或 --seconds 后自动退出）
"""
from __future__ import annotations

import argparse
import re
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Callable, Optional

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# config.yaml hotkeys 的语义键 -> 主循环命令名
CONFIG_COMMAND_MAP: dict[str, str] = {
    "toggle_listen": "toggle_listen",   # F8 听/停
    "capture_slide": "screenshot",      # F9 看屏幕
    "summary": "summary",               # F10 阶段小结
    "toggle_visible": "hide",           # F7 显示/隐藏
    "im_asked": "im_asked",             # F6 我被点名
    "freeze": "freeze",                 # F3 定格/恢复当前答复
    "export_minutes": "export",         # Ctrl+F10 导出纪要
    "quit": "quit",                     # Ctrl+F12 退出
}

_MODIFIER_ALIASES = {
    "control": "ctrl", "ctl": "ctrl", "ctrl": "ctrl",
    "shift": "shift",
    "alt": "alt", "option": "alt",
    "win": "windows", "windows": "windows", "super": "windows",
    "meta": "windows", "cmd": "windows",
}
_KEY_ALIASES = {
    "escape": "esc", "return": "enter", "del": "delete", "ins": "insert",
    "pgup": "pageup", "pgdn": "pagedown", "spacebar": "space",
    "bksp": "backspace", "brk": "pause",
}
_MODIFIERS = {"ctrl", "shift", "alt", "windows"}
_MODIFIER_ORDER = {"ctrl": 0, "shift": 1, "alt": 2, "windows": 3}
_NAMED_KEYS = {
    "space", "tab", "enter", "esc", "backspace", "delete", "insert", "home", "end",
    "pageup", "pagedown", "up", "down", "left", "right", "printscreen",
    "scrolllock", "pause", "capslock", "numlock", "apps", "menu",
}
_KEY_RE = re.compile(r"^[a-z0-9]$|^f(?:[1-9]|1[0-9]|2[0-4])$")


# ---------------------------------------------------------------- 解析辅助（纯函数，便于单测）
def normalize_hotkey(spec: object) -> str:
    """把配置里的热键字符串规范化成 keyboard 库可用的形式。

    例：``"Ctrl + F10"`` -> ``"ctrl+f10"``；``"control+f9"`` -> ``"ctrl+f9"``。
    非法字符串抛 ``ValueError``（调用方应跳过该键，不影响其它键）。
    """
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError("热键必须是非空字符串")
    raw = re.sub(r"\s+", "", spec.strip().lower())
    if not raw:
        raise ValueError("热键必须是非空字符串")
    parts = raw.split("+")
    if any(not p for p in parts):
        raise ValueError(f"热键格式错误: {spec!r}")
    parts = [_MODIFIER_ALIASES.get(p, _KEY_ALIASES.get(p, p)) for p in parts]
    mods, key = parts[:-1], parts[-1]
    for m in mods:
        if m not in _MODIFIERS:
            raise ValueError(f"未知修饰键: {m!r}（仅支持 ctrl/shift/alt/win）")
    if len(set(mods)) != len(mods):
        raise ValueError(f"重复的修饰键: {spec!r}")
    if not _KEY_RE.match(key) and key not in _NAMED_KEYS:
        raise ValueError(f"不支持的主键: {key!r}")
    if not mods:
        return key
    mods = sorted(mods, key=lambda m: _MODIFIER_ORDER.get(m, 9))
    return "+".join(mods + [key])


def config_hotkeys(cfg: dict) -> dict[str, object]:
    """从 cfg（或裸 hotkeys dict）里取出热键配置 dict。"""
    if not isinstance(cfg, dict):
        return {}
    hk = cfg.get("hotkeys")
    if isinstance(hk, dict):
        return dict(hk)
    # 允许直接传 hotkeys 段
    return dict(cfg)


def parse_bindings(cfg: dict, logger=None) -> dict[str, str]:
    """config -> {配置键: 规范化热键}；非法/空条目跳过（只 warning）。"""
    out: dict[str, str] = {}
    for key, spec in config_hotkeys(cfg).items():
        if not spec:
            continue
        try:
            out[str(key)] = normalize_hotkey(spec)
        except ValueError as e:
            if logger is not None:
                logger.warning("热键配置无效，已跳过 %s=%r: %s", key, spec, e)
    return out


def command_for_config_key(key: str) -> str:
    """配置键 -> 主循环命令名。未知键原样返回。"""
    return CONFIG_COMMAND_MAP.get(str(key), str(key))


def build_bindings(cfg: dict, handler: Callable[[str], None], logger=None) -> dict[str, Callable[[], None]]:
    """config -> {热键串: 回调}；回调只调用 ``handler(命令名)``（应是非阻塞的入队操作）。

    同一热键被配置两次时后者覆盖前者并 warning。
    """
    binds: dict[str, Callable[[], None]] = {}
    for key, combo in parse_bindings(cfg, logger).items():
        action = command_for_config_key(key)
        if combo in binds and logger is not None:
            logger.warning("热键重复：%s 被 %s 覆盖", combo, key)
        binds[combo] = (lambda a=action: handler(a))
    return binds


# ---------------------------------------------------------------- 管理器
class HotkeyManager:
    """线程安全的全局热键管理器。"""

    def __init__(self, bindings: dict[str, Callable[[], None]], log=None):
        self.bindings: dict[str, Callable[[], None]] = dict(bindings or {})
        self._log = log
        self._handles: list[object] = []
        self._registered: dict[str, str] = {}   # 规范化热键 -> 原始配置串
        self._failed: dict[str, str] = {}       # 原始串 -> 失败原因
        self._lock = threading.RLock()
        self._started = False

    # -- 日志（延迟到第一次使用时才创建，避免 import 副作用）
    @property
    def log(self):
        if self._log is None:
            from logging_setup import get_logger
            self._log = get_logger("hotkeys")
        return self._log

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._started and bool(self._handles)

    @property
    def registered(self) -> dict[str, str]:
        with self._lock:
            return dict(self._registered)

    @property
    def failed(self) -> dict[str, str]:
        with self._lock:
            return dict(self._failed)

    def start(self) -> bool:
        """注册全部热键；至少成功一个返回 True，全部失败/未配置返回 False（不抛异常）。"""
        with self._lock:
            if self._started:
                return bool(self._registered)
            self._started = True
            if not self.bindings:
                self.log.warning("没有配置任何全局热键（跳过）")
                return False
            try:
                import keyboard  # 延迟导入：无桌面会话时 import 本身可能异常
            except Exception as e:  # noqa: BLE001
                self.log.error("keyboard 库不可用，热键全部失效: %s: %s", type(e).__name__, e)
                return False

            for original, callback in self.bindings.items():
                try:
                    combo = normalize_hotkey(original)
                except ValueError as e:
                    self._failed[str(original)] = f"格式无效: {e}"
                    self.log.warning("热键格式无效，已跳过 %r: %s", original, e)
                    continue
                if not callable(callback):
                    self._failed[str(original)] = "回调不是可调用对象"
                    self.log.warning("热键 %s 的回调无效，已跳过", combo)
                    continue

                def _safe(cb=callback, name=combo):
                    try:
                        cb()
                    except Exception:  # noqa: BLE001
                        self.log.warning("热键回调异常 %s\n%s", name, traceback.format_exc())

                try:
                    handle = keyboard.add_hotkey(combo, _safe, suppress=False)
                    self._handles.append(handle)
                    self._registered[combo] = str(original)
                    self.log.info("热键已注册: %s", combo)
                except Exception as e:  # noqa: BLE001
                    self._failed[str(original)] = f"{type(e).__name__}: {e}"
                    self.log.warning("热键注册失败（可能被其它程序占用）%s: %s", combo, e)
            if self._failed:
                self.log.warning("共 %d 个热键不可用: %s",
                                 len(self._failed), ", ".join(self._failed))
            ok = bool(self._registered)
            self.log.info("热键注册结果: %d/%d 可用", len(self._registered), len(self.bindings))
            return ok

    def stop(self) -> None:
        """注销所有热键（可重复调用，不抛异常）。"""
        with self._lock:
            handles = self._handles[:]
            self._handles.clear()
            self._registered.clear()
            self._started = False
        if not handles:
            return
        try:
            import keyboard
        except Exception:  # noqa: BLE001
            return
        for handle in handles:
            try:
                keyboard.remove_hotkey(handle)
            except Exception:  # noqa: BLE001
                pass
        try:
            self.log.info("热键已全部注销")
        except Exception:  # noqa: BLE001
            pass

    def summary(self) -> str:
        return f"可用 {len(self.registered)}/{len(self.bindings)}，失败 {len(self.failed)}"


# ---------------------------------------------------------------- 自测
def _selftest(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="组会助手 · 热键自测")
    ap.add_argument("--seconds", type=float, default=6.0, help="自动退出前等待秒数（默认 6；--interactive 时忽略）")
    ap.add_argument("--interactive", action="store_true", help="一直等待，按 Ctrl+C 退出")
    ap.add_argument("--list", action="store_true", help="只打印配置里的热键，不注册")
    args = ap.parse_args(argv)

    from config import init_console
    init_console()

    cfg: dict = {}
    try:
        from config import load_config
        cfg = load_config()
    except Exception as e:  # noqa: BLE001
        print(f"[警告] 读取 config.yaml 失败（用默认热键继续）: {e}")
    raw_hotkeys = config_hotkeys(cfg)
    if args.list:
        for k, spec in raw_hotkeys.items():
            try:
                print(f"  {k:16s} {spec!r:12s} -> {normalize_hotkey(spec)}")
            except ValueError as e:
                print(f"  {k:16s} {spec!r:12s} -> 无效: {e}")
        return 0

    print("== 热键字符串解析自测 ==")
    samples = ["Ctrl+F10", "f8", "control+f9", "Alt + Shift + F6", "ctrl+ctrl+f1", "hyper+f1", ""]
    parse_ok = True
    for s in samples:
        try:
            print(f"  {s!r:22s} -> {normalize_hotkey(s)}")
        except ValueError as e:
            print(f"  {s!r:22s} -> 预期内的无效: {e}")
    try:
        normalize_hotkey("ctrl+ctrl+f1")
    except ValueError:
        parse_ok = True
    else:
        parse_ok = False
    print("  解析器:", "✅ 正常" if parse_ok else "❌ 未按预期拒绝非法输入")

    # 取配置里的前 3 个热键（F8/F9/F10），配置缺失则用默认键
    picks: list[tuple[str, str]] = []
    for cfg_key in ("toggle_listen", "capture_slide", "summary"):
        spec = raw_hotkeys.get(cfg_key)
        if spec:
            try:
                picks.append((CONFIG_COMMAND_MAP.get(cfg_key, cfg_key), normalize_hotkey(spec)))
            except ValueError:
                pass
    while len(picks) < 3:
        fallback = [("toggle_listen", "f8"), ("screenshot", "f9"), ("summary", "f10")][len(picks)]
        picks.append(fallback)

    fired: list[str] = []

    def _mk(action: str):
        def _cb():
            fired.append(action)
            print(f"  [触发] {action}", flush=True)
        return _cb

    binds = {combo: _mk(action) for action, combo in picks}
    print(f"\n== 注册 {len(binds)} 个热键 ==")
    for combo, cb in binds.items():
        print(f"  {combo:12s} -> {getattr(cb, '__name__', 'callback')}")
    hk = HotkeyManager(binds)
    ok = hk.start()
    print(f"  注册结果: 可用={list(hk.registered)} 失败={hk.failed or '无'}")
    print("\n提示：可依次按 " + " / ".join(c.upper() for c in binds) +
          " 验证（每按一次会打印 [触发]）；" +
          ("按 Ctrl+C 退出。" if args.interactive else f"等待 {args.seconds:.0f} 秒后自动退出。"))

    deadline = None if args.interactive else time.time() + max(1.0, args.seconds)
    try:
        while True:
            if deadline is not None and time.time() >= deadline:
                break
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，退出。")
    finally:
        hk.stop()

    print(f"\n回调触发次数: {len(fired)}" + (f"（{', '.join(fired)}）" if fired else "（无人按键属正常）"))
    if not ok:
        print("❌ 热键注册失败：keyboard 不可用或 3 个键均被占用")
        return 1
    print("✅ 热键注册链路正常（按键是否真正生效需人工按一次确认）")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="组会助手 · 全局热键")
    ap.add_argument("--selftest", action="store_true", help="注册 3 个热键并提示（自测）")
    args, rest = ap.parse_known_args(argv)
    if args.selftest:
        return _selftest(rest)
    print("用法: python hotkeys.py --selftest [--seconds 6] [--interactive] [--list]")
    print("模块用法: from hotkeys import HotkeyManager, build_bindings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
