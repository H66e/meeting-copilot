# -*- coding: utf-8 -*-
"""Meeting Copilot · 置顶浮窗 UI（tkinter）

契约（INTERFACES.md C1）：
    class OverlayUI:
        def __init__(self, cfg: dict, bus: EventBus, log=None): ...
        def register_actions(self, actions: dict[str, Callable]) -> None: ...
        def start(self) -> None: ...
        def run(self) -> None: ...
        def stop(self) -> None: ...

约定（与 main.py 对齐）：
- 无边框、置顶、半透明，停靠主屏右上角（DPI 感知后按物理像素计算），可拖 header、右下角可拉伸；
- 三标签页：实时字幕 / 即时建议（流式卡片）/ 纪要待办（小结 + □ 待办 + 导出）；
- 顶部状态条：● 红/绿、计时、RTF、设备简写、动作按钮、思考中指示、音量电平条；
- 命令总线：UI 独立消费 ``bus.commands``；``hide`` 本地处理，``quit`` 调 actions 后自毁，
  其它命令在 Tk 线程直接调 ``actions[cmd]()``（**不回写 bus.commands，避免自激循环**）；
- UI 线程不做网络/模型调用，只轮询 EventBus（80ms）。

自测：``python ui_overlay.py --selftest``（假数据渲染三类卡片，10 秒后自动关闭并输出报告）
"""
from __future__ import annotations

import argparse
import ctypes
import os
import queue
import re
import sys
import threading
import time
import traceback
import types
import unicodedata
from pathlib import Path
from typing import Any, Callable, Optional

if __name__ == "__main__" or True:  # noqa: SIM108 - 允许以脚本方式直接运行
    _ROOT = Path(__file__).resolve().parent
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

from config import get
from contracts import (KIND_ANSWER, KIND_HINT, KIND_MINUTES, KIND_SCRIPT, KIND_SLIDE,
                       KIND_SUMMARY, KIND_TERM, LANE_DEEP, LANE_FAST, BrainEvent,
                       EventBus, Status, TranscriptSegment, fmt_ts, now_str)
from logging_setup import get_logger

# ---------------------------------------------------------------- 配色
C_BG = "#16181d"          # 窗口背景
C_PANEL = "#1e2128"       # 面板/卡片
C_PANEL_2 = "#23262e"     # 次级面板（输入框/深色卡片）
C_BTN = "#262a33"
C_BTN_HOVER = "#313743"
C_BORDER = "#2a2e37"
C_TEXT = "#e8eaed"
C_MUTED = "#9aa0a6"
C_DIM = "#6b7280"
C_ACCENT = "#5b9dff"      # 强调蓝
C_WARM = "#ffb454"        # 应答思路暖色
C_ERR = "#ff6b6b"
C_ERR_SOFT = "#3a2328"   # 危险按钮底色（退出）
C_OK = "#3ddc84"
C_TEAL = "#6fd6b8"
C_READ = "#7ee787"      # 照读稿（可直接照着念的稿子）
C_TS = "#6ea8fe"

HOTKEY_HINT = "F8 听/停 · F9 看屏幕 · F5 照读稿 · F10 纪要 · F7 隐藏 · F6 我被点名 · F4 穿透"
CARD_TAB_KINDS = (KIND_SUMMARY, KIND_MINUTES)
# 过程记录区每类最多保留几张（防止长会堆积几百张卡片来回翻）
KIND_KEEP = {KIND_HINT: 3, KIND_TERM: 2, KIND_SLIDE: 2}


# ================================================================ Win32 / 纯函数辅助
def enable_dpi_awareness() -> bool:
    """进程 DPI 感知（必须在创建 Tk 窗口之前调用）。失败只返回 False，不抛异常。"""
    if os.name != "nt":
        return False
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE
        return True
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        return True
    except Exception:
        return False


def iter_monitors(root: Optional[tk.Misc] = None) -> list[dict]:
    """枚举显示器（Win32 物理像素）；失败回退为单屏。返回 primary 在前。"""
    if os.name == "nt":
        try:
            from ctypes import wintypes

            class _RECT(ctypes.Structure):
                _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                            ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

            class _MONITORINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", _RECT),
                            ("rcWork", _RECT), ("dwFlags", wintypes.DWORD)]

            user32 = ctypes.windll.user32
            found: list[dict] = []

            def _cb(hmon, hdc, lprc, lparam):  # noqa: ANN001
                try:
                    mi = _MONITORINFO()
                    mi.cbSize = ctypes.sizeof(_MONITORINFO)
                    if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                        found.append({
                            "monitor": (int(mi.rcMonitor.left), int(mi.rcMonitor.top),
                                        int(mi.rcMonitor.right), int(mi.rcMonitor.bottom)),
                            "work": (int(mi.rcWork.left), int(mi.rcWork.top),
                                     int(mi.rcWork.right), int(mi.rcWork.bottom)),
                            "primary": bool(int(mi.dwFlags) & 1),
                        })
                except Exception:
                    pass
                return True

            proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HANDLE, wintypes.HDC,
                                      ctypes.POINTER(_RECT), wintypes.LPARAM)
            user32.EnumDisplayMonitors(0, 0, proc(_cb), 0)
            if found:
                found.sort(key=lambda m: (not m["primary"], m["monitor"][0]))
                return found
        except Exception:
            pass
    w = int(root.winfo_screenwidth()) if root is not None else 1920
    h = int(root.winfo_screenheight()) if root is not None else 1080
    return [{"monitor": (0, 0, w, h), "work": (0, 0, w, h), "primary": True}]


def pick_monitor(monitors: list[dict], which: str = "primary") -> Optional[dict]:
    """按 primary/secondary 选显示器；副屏不存在时回退主屏。"""
    if not monitors:
        return None
    primary = next((m for m in monitors if m.get("primary")), monitors[0])
    if str(which).lower() == "secondary":
        secondary = next((m for m in monitors if not m.get("primary")), None)
        return secondary or primary
    return primary


def anchor_top_right(work: tuple[int, int, int, int], size: tuple[int, int],
                     margin: int = 16) -> tuple[int, int]:
    """工作区右上角坐标（处理多屏负坐标/越界）。work=(left,top,right,bottom)。"""
    left, top, right, bottom = (int(v) for v in work)
    w, h = int(size[0]), int(size[1])
    x = right - w - int(margin)
    y = top + int(margin)
    x = max(left, min(x, max(left, right - w)))
    y = max(top, min(y, max(top, bottom - h)))
    return int(x), int(y)


def display_width(text: str) -> int:
    """字符串显示宽度（CJK 全角算 2 列），用于对齐即可截断。"""
    total = 0
    for ch in str(text):
        total += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return total


def truncate_text(text: object, max_cols: int, ellipsis: str = "…") -> str:
    """按显示宽度截断，超长加省略号。"""
    s = str(text or "")
    if max_cols <= 0:
        return ""
    if display_width(s) <= max_cols:
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


def short_device(name: object, limit: int = 18) -> str:
    """设备名简写：``扬声器 (Razer XX)`` -> ``扬声器·Razer XX`` 并截断。"""
    s = re.sub(r"\s+", " ", str(name or "").strip())
    if not s:
        return "默认设备"
    m = re.match(r"^(扬声器|音箱|耳机|Speakers?)\s*[（(]\s*(.+?)\s*[)）]\s*$", s, re.IGNORECASE)
    if m:
        s = f"{m.group(1)}·{m.group(2)}"
    return truncate_text(s, limit)


def format_transcript_line(t_start: float, text: str) -> str:
    """字幕行格式：``[12:34] 文本``。"""
    return f"[{fmt_ts(t_start)}] {str(text or '').strip()}"


def title_for(kind: str, lane: str = "") -> tuple[str, str]:
    """卡片标题与强调色。"""
    kind = str(kind or "")
    if kind == KIND_ANSWER:
        return "应答思路", C_WARM
    if kind == KIND_SCRIPT:
        return "照读稿", C_READ
    if kind == KIND_SLIDE:
        return "屏幕解读", C_ACCENT
    if kind == KIND_HINT:
        return "要点", C_ACCENT
    if kind == KIND_TERM:
        return "术语解释", C_TEAL
    if kind == KIND_SUMMARY:
        return "阶段小结", C_TEAL
    if kind == KIND_MINUTES:
        return "最终纪要", C_TEAL
    return "建议", C_ACCENT


def card_style(kind: str, lane: str = "") -> dict:
    """不同卡片类型的完整视觉样式（纯函数，便于单测）。"""
    title, color = title_for(kind, lane)
    kind = str(kind or "")
    if kind == KIND_ANSWER:
        return {"title": title, "color": color, "bg": "#262a33", "border": "#4a3a20",
                "title_size": 12, "body_size": 11, "accent": 4}
    if kind == KIND_SCRIPT:
        return {"title": title, "color": color, "bg": "#1b2420", "border": "#2f5d43",
                "title_size": 12, "body_size": 12, "accent": 4}
    if kind == KIND_SLIDE:
        return {"title": title, "color": color, "bg": C_PANEL, "border": C_BORDER,
                "title_size": 11, "body_size": 10, "accent": 3}
    if kind == KIND_HINT:
        return {"title": title, "color": color, "bg": C_PANEL, "border": C_BORDER,
                "title_size": 10, "body_size": 10, "accent": 3}
    if kind in CARD_TAB_KINDS:
        return {"title": title, "color": color, "bg": C_PANEL, "border": C_BORDER,
                "title_size": 11, "body_size": 10, "accent": 3}
    return {"title": title, "color": color, "bg": C_PANEL, "border": C_BORDER,
            "title_size": 10, "body_size": 10, "accent": 3}


def lane_badge(lane: str) -> str:
    if lane == LANE_DEEP:
        return "深度"
    if lane == LANE_FAST:
        return "快速"
    return ""


def status_text(listening: bool, asr_ready: bool, has_status: bool = True) -> str:
    if not has_status:
        return "待机"
    if listening:
        return "聆听中" if asr_ready else "模型加载中"
    return "已暂停"


def rtf_text(rtf: float) -> str:
    try:
        v = float(rtf or 0.0)
    except (TypeError, ValueError):
        v = 0.0
    return f"RTF {v:.2f}" if v > 0 else "RTF -"


def delay_text(delay: float, rtf: float = 0.0) -> str:
    """状态条优先显示"说完→出字"的真实延迟，比 RTF 直观。"""
    try:
        d = float(delay or 0.0)
    except (TypeError, ValueError):
        d = 0.0
    if d > 0:
        return f"延迟 {d:.1f}s"
    return rtf_text(rtf)


# ---- 待办提取（纪要/阶段小结文本 -> 待办列表）
_CHECK_PREFIXES = ("□", "☐", "☑", "■", "- [ ]", "[ ]", "- [x]", "[x]")
_SECTION_MARKERS = ("待办", "todo", "to-do", "行动项", "下一步", "后续推进", "任务清单")
_BULLET_RE = re.compile(r"^(?:[-*•·]|\d+\s*[.、)）])\s*(.+)$")


def extract_todos(text: str) -> list[str]:
    """从阶段小结/纪要文本里提取待办清单（``□``/``- [ ]``/「待办：」小节）。"""
    todos: list[str] = []
    in_section = False
    for raw in str(text or "").replace("\r", "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if any(marker in low for marker in _SECTION_MARKERS):
            in_section = True
            rest = re.split(r"[：:]", line, maxsplit=1)
            if len(rest) == 2 and rest[1].strip():
                cand = rest[1].strip(" 　")
                if cand and cand not in todos:
                    todos.append(cand)
            continue

        candidate: Optional[str] = None
        for prefix in _CHECK_PREFIXES:
            if line.startswith(prefix):
                candidate = line[len(prefix):].strip(" ：:、.·\t")
                break
        if candidate is None and in_section:
            m = _BULLET_RE.match(line)
            if m:
                candidate = m.group(1).strip()
            elif not line.startswith(("#", "**", "|")) and display_width(line) <= 90 \
                    and not line.endswith(("。", "！", "？", "；")):
                candidate = line
        if candidate:
            candidate = re.sub(r"\s+", " ", candidate).strip(" 　")
            if candidate and candidate not in todos:
                todos.append(candidate)
    return todos


# ================================================================ 卡片状态机（纯逻辑，可单测）
class Card:
    """一张建议卡片的累积状态。"""

    __slots__ = ("request_id", "kind", "lane", "title", "text", "done", "error",
                 "created_at", "stamp", "n_events")

    def __init__(self, request_id: str, kind: str = "", lane: str = "",
                 created_at: Optional[float] = None):
        self.request_id = str(request_id)
        self.kind = str(kind or "")
        self.lane = str(lane or "")
        self.title = ""
        self.text = ""
        self.done = False
        self.error: Optional[str] = None
        self.created_at = float(created_at if created_at is not None else time.time())
        self.stamp = time.strftime("%H:%M", time.localtime(self.created_at))
        self.n_events = 0

    @property
    def display_title(self) -> str:
        if self.title:
            return self.title
        return title_for(self.kind, self.lane)[0]

    @property
    def is_minutes_kind(self) -> bool:
        return self.kind in CARD_TAB_KINDS


class CardStore:
    """同一 request_id 的流式 delta 累积到同一张卡片；done 收尾；限量淘汰。"""

    def __init__(self, max_cards: int = 60):
        self.max_cards = max(1, int(max_cards))
        self._cards: dict[str, Card] = {}
        self._order: list[str] = []

    def put(self, ev: BrainEvent) -> tuple[Card, bool]:
        rid = str(getattr(ev, "request_id", "") or "")
        if not rid:
            rid = f"anon-{len(self._order)}-{int(time.time() * 1000) % 100000}"
        card = self._cards.get(rid)
        is_new = card is None
        if is_new:
            card = Card(rid, getattr(ev, "kind", "") or "", getattr(ev, "lane", "") or "")
            self._cards[rid] = card
            self._order.append(rid)
        assert card is not None
        card.n_events += 1
        if getattr(ev, "kind", ""):
            card.kind = ev.kind
        if getattr(ev, "lane", ""):
            card.lane = ev.lane
        title = str(getattr(ev, "title", "") or "").strip()
        if title:
            card.title = title
        delta = str(getattr(ev, "delta", "") or "")
        if delta:
            card.text += delta
        error = getattr(ev, "error", None)
        if error:
            card.error = str(error)
            card.done = True
        full = str(getattr(ev, "full_text", "") or "")
        if full and len(full) >= len(card.text):
            card.text = full
        if getattr(ev, "done", False):
            card.done = True
        if is_new:
            self._evict()
        return card, is_new

    def _evict(self) -> None:
        while len(self._order) > self.max_cards:
            old = self._order.pop(0)
            self._cards.pop(old, None)

    def get(self, request_id: str) -> Optional[Card]:
        return self._cards.get(str(request_id))

    def cards(self) -> list[Card]:
        return [self._cards[rid] for rid in self._order if rid in self._cards]

    def clear(self) -> None:
        self._cards.clear()
        self._order.clear()

    def __len__(self) -> int:
        return len(self._order)

    def __contains__(self, request_id: object) -> bool:
        return str(request_id) in self._cards


# ================================================================ Win32：共享检测 / 前台窗口
def _window_process_name(hwnd: int) -> str:
    if os.name != "nt" or not hwnd:
        return ""
    try:
        from ctypes import wintypes
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        if not pid.value:
            return ""
        handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return os.path.basename(buf.value).lower()
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        pass
    return ""


def detect_share_window(exclude_hwnd: int = 0) -> Optional[dict]:
    """best-effort 检测腾讯会议共享屏幕。命中返回 ``{"title","reason"}``，否则 None。

    启发式（失败不影响主功能）：
    1) 窗口标题/进程名属于腾讯会议（wemeet），且标题含「共享/屏幕共享/投屏」；
    2) 腾讯会议窗口在任一显示器上近似全屏（视为全屏演示/共享中）。
    """
    if os.name != "nt":
        return None
    try:
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        monitors = iter_monitors()
        areas = []
        for m in monitors:
            l, t, r, b = m["monitor"]
            if r > l and b > t:
                areas.append((r - l) * (b - t))
        max_area = max(areas) if areas else 0
        hits: list[dict] = []

        class _RECT(ctypes.Structure):
            _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                        ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

        def _cb(hwnd, lparam):  # noqa: ANN001
            try:
                if not hwnd or int(hwnd) == int(exclude_hwnd):
                    return True
                if not user32.IsWindowVisible(hwnd):
                    return True
                n = user32.GetWindowTextLengthW(hwnd)
                if n <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                title = (buf.value or "").strip()
                if not title:
                    return True
                cls_buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, cls_buf, 256)
                cls = cls_buf.value or ""
                proc = _window_process_name(int(hwnd))
                # 只认腾讯会议**进程**：绝不能按标题含"腾讯会议"判断，
                # 否则任何标题里带"腾讯会议"的窗口（比如聊天/IDE/本项目会话标题）
                # 都会被误判成会议共享，导致浮窗被错误避让（变淡+移到副屏）。
                proc_l = (proc or "").lower()
                cls_l = (cls or "").lower()
                is_meet = ("wemeet" in proc_l) or ("wemeet" in cls_l)
                if not is_meet:
                    return True
                if "共享" in title or "投屏" in title or "正在共享" in title:
                    hits.append({"title": title, "reason": f"标题含共享（proc={proc or '?'}）"})
                    return True
                # 注意：**不做"会议窗口全屏"判定**——腾讯会议全屏开会 ≠ 正在共享，
                # 这时自动避让只会让用户莫名看不到浮窗。宁可漏判，用户按 F7 手动隐藏即可。
            except Exception:
                pass
            return True

        proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(proc_type(_cb), 0)
        _ = max_area
        return hits[0] if hits else None
    except Exception:
        return None


def _foreground_hwnd() -> int:
    try:
        return int(ctypes.windll.user32.GetForegroundWindow())
    except Exception:
        return 0


# ================================================================ 滚动区域辅助
def make_scroll_area(parent: tk.Misc, bg: str = C_BG) -> tuple[tk.Frame, tk.Canvas, tk.Frame]:
    """返回 (outer, canvas, inner)：canvas 内嵌一个宽度自适应的 inner Frame。"""
    outer = tk.Frame(parent, bg=bg)
    canvas = tk.Canvas(outer, bg=bg, highlightthickness=0, bd=0, takefocus=0)
    vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview,
                        style="Dark.Vertical.TScrollbar")
    canvas.configure(yscrollcommand=vsb.set)
    canvas.pack(side="left", fill="both", expand=True)
    vsb.pack(side="right", fill="y")
    inner = tk.Frame(canvas, bg=bg)
    window = canvas.create_window((0, 0), window=inner, anchor="nw")

    def _on_inner(_event=None) -> None:
        try:
            canvas.configure(scrollregion=canvas.bbox("all"))
        except Exception:
            pass

    def _on_canvas(event) -> None:  # noqa: ANN001
        try:
            canvas.itemconfigure(window, width=max(1, int(event.width)))
        except Exception:
            pass

    inner.bind("<Configure>", _on_inner)
    canvas.bind("<Configure>", _on_canvas)
    return outer, canvas, inner


# ================================================================ 卡片控件
class _CardWidget:
    """流式增量卡片控件（正文用只读 Text，wrap=char 才能让中文自然换行）。"""

    def __init__(self, parent: tk.Misc, card: Card, ui: "OverlayUI"):
        self.ui = ui
        self.card = card
        style = card_style(card.kind, card.lane)
        self.color = style["color"]
        self.deep = card.kind == KIND_ANSWER
        self._size_job = None
        self.frame = tk.Frame(parent, bg=style["bg"], highlightbackground=style["border"],
                              highlightthickness=max(1, ui._px(1)))
        accent = tk.Frame(self.frame, bg=style["color"], width=ui._px(style["accent"]))
        accent.pack(side="left", fill="y")
        body = tk.Frame(self.frame, bg=style["bg"])
        body.pack(side="left", fill="both", expand=True,
                  padx=ui._px(10), pady=ui._px(7))
        head = tk.Frame(body, bg=style["bg"])
        head.pack(fill="x")
        self.title_lbl = tk.Label(head, text=style["title"], fg=style["color"],
                                  bg=style["bg"], font=ui._font(style["title_size"], "bold"),
                                  anchor="w")
        self.title_lbl.pack(side="left")
        badge_text = lane_badge(card.lane)
        self.badge_lbl = tk.Label(head, text=badge_text, fg=C_MUTED, bg=style["bg"],
                                  font=ui._font(8), anchor="w")
        self.badge_lbl.pack(side="left", padx=(ui._px(6), 0))
        self.time_lbl = tk.Label(head, text=card.stamp, fg=C_DIM, bg=style["bg"],
                                 font=ui._font(8), anchor="e")
        self.time_lbl.pack(side="right")
        self.body_lbl = tk.Text(body, height=1, width=1, wrap="char", bg=style["bg"],
                                fg=C_TEXT, bd=0, highlightthickness=0, padx=0, pady=0,
                                font=ui._font(style["body_size"]), state="disabled",
                                cursor="arrow", takefocus=0, spacing1=0,
                                spacing3=ui._px(1), insertwidth=0)
        self.body_lbl.pack(fill="x", pady=(ui._px(3), 0))
        self.frame.pack(fill="x", padx=ui._px(8), pady=(0, ui._px(6)))
        self._style = style
        self._apply_text("正在生成…", C_DIM)

    # -- 正文更新（只读 Text，自动高度）
    def _apply_text(self, text: str, color: Optional[str] = None) -> None:
        widget = self.body_lbl
        try:
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.insert("1.0", text or "")
            widget.configure(state="disabled")
            if color:
                widget.configure(fg=color)
        except Exception:
            pass
        self._schedule_autosize()

    def _schedule_autosize(self, delay: int = 40) -> None:
        if self._size_job is not None:
            try:
                self.ui.root.after_cancel(self._size_job)
            except Exception:
                pass
        try:
            self._size_job = self.ui.root.after(delay, self._autosize)
        except Exception:
            self._size_job = None

    def _autosize(self) -> None:
        self._size_job = None
        try:
            widget = self.body_lbl
            if not widget.winfo_ismapped():
                return
            widget.update_idletasks()
            # 用文本布局总像素高 / 字体行高 计算行数（Tk 8.6 的 count displaylines 不可靠）
            linespace = int(widget.tk.call("font", "metrics", widget.cget("font"),
                                           "-linespace") or 16)
            # Tk 的 count -ypixels 返回首末字符的纵向距离，行数 = 距离/行高 + 1
            ypixels = int(widget.count("1.0", "end-1c", "ypixels", "update") or 0)
            lines = max(1, min(60, 1 + int(round(ypixels / max(1, linespace)))))
            if int(widget.cget("height")) != lines:
                widget.configure(height=lines)
        except Exception:
            pass

    def set_wrap(self, wrap_px: int) -> None:  # 兼容旧接口：宽度不可控，改为重算行高
        self._schedule_autosize(20)

    def relayout(self) -> None:
        self._schedule_autosize(20)

    def refresh(self, card: Card) -> None:
        self.card = card
        done_mark = " · 完成" if card.done and not card.error else ""
        self.time_lbl.configure(text=f"{card.stamp}{done_mark}")
        if card.error:
            self.title_lbl.configure(text="生成失败", fg=C_ERR)
            self.badge_lbl.configure(text="错误", fg=C_ERR)
            self._apply_text(card.error, C_ERR)
            return
        self.title_lbl.configure(text=card.display_title, fg=self.color)
        badge = lane_badge(card.lane)
        self.badge_lbl.configure(text=badge, fg=C_MUTED)
        if card.text:
            text = card.text if card.done else card.text + " ▌"
            self._apply_text(text, C_TEXT)
        elif card.done:
            self._apply_text("（无内容）", C_DIM)
        else:
            self._apply_text("正在生成…", C_DIM)

    def bind_wheel(self, handler: Callable[..., Any]) -> None:
        def _bind(widget: tk.Misc) -> None:
            try:
                widget.bind("<MouseWheel>", handler, add="+")
            except Exception:
                pass
            for child in widget.winfo_children():
                _bind(child)

        _bind(self.frame)

    def destroy(self) -> None:
        if self._size_job is not None:
            try:
                self.ui.root.after_cancel(self._size_job)
            except Exception:
                pass
            self._size_job = None
        try:
            self.frame.destroy()
        except Exception:
            pass


# ================================================================ 自绘滑条（深色主题）
class _CanvasSlider(tk.Canvas):
    """深色主题自绘滑条：tk.Scale 在深色背景下滑块对比度太差，这里自绘。"""

    def __init__(self, parent: tk.Misc, from_: float, to: float, value: float,
                 command: Optional[Callable[[float], Any]] = None, *,
                 ui: "OverlayUI", **kw: Any):
        super().__init__(parent, bg=C_BG, highlightthickness=0, bd=0,
                         height=ui._px(24), takefocus=0, **kw)
        self._ui = ui
        self._from = float(from_)
        self._to = float(to)
        self._value = max(self._from, min(self._to, float(value)))
        self._command = command
        self.bind("<Configure>", lambda _e: self._redraw())
        self.bind("<ButtonPress-1>", self._on_event)
        self.bind("<B1-Motion>", self._on_event)

    def get(self) -> float:
        return float(self._value)

    def set(self, value: float) -> None:
        try:
            self._value = max(self._from, min(self._to, float(value)))
        except (TypeError, ValueError):
            return
        self._redraw()

    def _frac(self) -> float:
        span = self._to - self._from
        if span <= 0:
            return 0.0
        return max(0.0, min(1.0, (self._value - self._from) / span))

    def _redraw(self) -> None:
        try:
            self.delete("all")
            width = max(2, int(self.winfo_width()))
            height = max(2, int(self.winfo_height()))
            pad = self._ui._px(9)
            y = height // 2
            x0, x1 = pad, max(pad + 2, width - pad)
            track = self._ui._px(5)
            self.create_line(x0, y, x1, y, fill="#2f3542", width=track, capstyle="round")
            knob_x = x0 + (x1 - x0) * self._frac()
            if knob_x > x0:
                self.create_line(x0, y, knob_x, y, fill=C_ACCENT, width=track,
                                 capstyle="round")
            radius = self._ui._px(7)
            self.create_oval(knob_x - radius, y - radius, knob_x + radius, y + radius,
                             fill="#e8eaf0", outline=C_ACCENT,
                             width=max(1, self._ui._px(1)))
        except Exception:
            pass

    def _on_event(self, event) -> None:  # noqa: ANN001
        try:
            width = max(2, int(self.winfo_width()))
            pad = self._ui._px(9)
            frac = (int(event.x) - pad) / max(1, width - 2 * pad)
            frac = max(0.0, min(1.0, frac))
            self._value = self._from + frac * (self._to - self._from)
            self._redraw()
            if callable(self._command):
                self._command(self._value)
        except Exception:
            pass


# ================================================================ 主 UI
class OverlayUI:
    """置顶无边框浮窗。start() 建窗（主线程），run() 进 mainloop，stop() 清理。"""

    POLL_MS = 80
    ANIMATE_MS = 260
    MAX_CARDS = 40
    ECHO_WINDOW_S = 0.25     # 防自激循环：同命令 0.25s 内的回声直接丢弃
    BURST_LIMIT = 6          # 同命令 2s 内超过该次数视为回路，熔断忽略

    def __init__(self, cfg: dict, bus: EventBus, log=None, *, exclude_from_capture: bool = True):
        self.cfg = cfg if isinstance(cfg, dict) else {}
        self.bus = bus
        self._log = log
        # 测试/调试钩子：设 MC_DISABLE_CAPTURE_EXCLUDE=1 可关闭 WDA 捕获排除（便于截图验收）
        if os.environ.get("MC_DISABLE_CAPTURE_EXCLUDE", "").strip() in ("1", "true", "yes"):
            exclude_from_capture = False
        self._exclude_from_capture = bool(exclude_from_capture)
        try:
            self._size = (int(get(self.cfg, "ui.size", [452, 780])[0]),
                          int(get(self.cfg, "ui.size", [452, 780])[1]))
        except Exception:
            self._size = (452, 780)
        self._alpha = self._safe_alpha(get(self.cfg, "ui.alpha", 0.95))
        self.root: Optional[tk.Tk] = None
        self._started = False
        self._mainloop_running = False
        self._closed = False
        self._actions: dict[str, Callable[..., Any]] = {}
        self._actions_fired: list[str] = []

        # 显示
        self._dpi = 96.0
        self._dpi_scale = 1.0
        self._monitors: list[dict] = []
        self._hwnd = 0
        self._capture_excluded = False
        self._font_family = "Microsoft YaHei UI"
        self._mono_family = "Consolas"
        self._logs_dir = self._resolve_logs_dir()

        # 状态
        self._status = Status()
        self._got_status = False
        self._status_ts = 0.0
        self._t0 = time.monotonic()
        self._listening = False
        self._visible = False
        self._frame = 0
        self._local_msg = ""
        self._local_msg_until = 0.0
        self._devices_q: "queue.Queue[tuple[list[str], str]]" = queue.Queue()

        # 内容
        self._card_store = CardStore()
        self._card_widgets: dict[str, _CardWidget] = {}
        self._card_order: list[str] = []
        self._transcript_count = 0
        self._transcript_placeholder = True
        self._thinking_deep: set[str] = set()
        self._thinking_fast: set[str] = set()
        self._todos: list[str] = []
        self._todo_labels: list[tk.Label] = []

        # 窗口行为
        self._drag_dx = 0
        self._drag_dy = 0
        self._resize_origin: Optional[tuple[int, int, int, int, int, int]] = None
        self._normal_geometry = ""
        self._evaded = False
        self._pre_evade_geometry = ""
        self._share_info: Optional[dict] = None
        self._share_misses = 0
        self._share_lock = threading.Lock()
        self._share_stop = threading.Event()
        self._share_thread: Optional[threading.Thread] = None
        self._settings_win: Optional[tk.Toplevel] = None

        # 调度
        self._poll_job = None
        self._animate_job = None
        self._configure_job = None
        self._dispatch_ts: dict[str, float] = {}
        self._dispatch_burst: dict[str, list[float]] = {}
        self._active_tab = 0
        self._tab_entries: list[dict] = []
        self._tab_unread = [0, 0, 0]
        self._buttons: dict[str, tk.Button] = {}
        self._action_available: dict[str, bool] = {}

    # ------------------------------------------------------------ 基础
    @property
    def log(self):
        if self._log is None:
            self._log = get_logger("ui")
        return self._log

    @staticmethod
    def _safe_alpha(value: Any, default: float = 0.95) -> float:
        try:
            a = float(value)
        except (TypeError, ValueError):
            return default
        return min(1.0, max(0.2, a))

    def _resolve_logs_dir(self) -> Path:
        try:
            dirs = get(self.cfg, "_dirs", {})
            p = Path(dirs.get("logs")) if isinstance(dirs, dict) and dirs.get("logs") else _ROOT / "logs"
        except Exception:
            p = _ROOT / "logs"
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return p

    def _px(self, value: float) -> int:
        return max(0, int(round(float(value) * self._dpi_scale)))

    def _font(self, size: int = 10, weight: str = "normal", mono: bool = False):
        family = self._mono_family if mono else self._font_family
        return (family, int(size), weight)

    def _card_wrap(self) -> int:
        try:
            width = int(self.root.winfo_width()) if self.root is not None else self._px(self._size[0])
        except Exception:
            width = self._px(self._size[0])
        return max(self._px(180), width - self._px(78))

    # ------------------------------------------------------------ 生命周期
    def register_actions(self, actions: dict[str, Callable[..., Any]]) -> None:
        """注册动作回调；未提供的按钮置灰。可用 key 见 INTERFACES.md C1。"""
        self._actions = dict(actions or {})
        self._refresh_action_states()
        self.log.info("UI 已注册动作: %s", ", ".join(sorted(self._actions)) or "（无）")

    def start(self) -> None:
        if self._started and self.root is not None:
            return
        fg_before = _foreground_hwnd()   # 必须在创建 Tk 之前记下原前台窗口
        enable_dpi_awareness()
        try:
            self.root = tk.Tk()
        except Exception as e:  # noqa: BLE001
            self.log.error("创建 Tk 窗口失败: %s", e)
            raise
        root = self.root
        try:
            root.withdraw()
            try:
                root.overrideredirect(True)
            except tk.TclError:
                pass
            root.configure(bg=C_BG)
            try:
                root.title("Meeting Copilot")
            except tk.TclError:
                pass
            if bool(get(self.cfg, "ui.always_on_top", True)):
                try:
                    root.attributes("-topmost", True)
                except tk.TclError:
                    pass
            try:
                root.attributes("-alpha", self._alpha)
            except tk.TclError:
                pass
            try:
                self._dpi = float(root.winfo_fpixels("1i") or 96.0)
            except tk.TclError:
                self._dpi = 96.0
            self._dpi_scale = max(1.0, self._dpi / 96.0)
            self._setup_fonts()
            self._setup_ttk_style()
            self._monitors = iter_monitors(root)
            self._build_ui()
            self._apply_geometry()
            root.update_idletasks()
            self._hwnd = self._get_hwnd()
            self._set_no_activate_style()
            self._show_no_activate(fg_before)
            root.update()
            self._hwnd = self._get_hwnd() or self._hwnd
            self._set_no_activate_style()
            self._topmost_noactivate()
            self._visible = True
            self._started = True
            self._t0 = time.monotonic()
            self._poll_job = root.after(self.POLL_MS, self._poll)
            self._animate_job = root.after(self.ANIMATE_MS, self._animate)
            root.bind("<Configure>", self._on_root_configure)
            root.after(120, self._relayout)
            if self._exclude_from_capture:
                root.after(400, self._apply_display_affinity)
            self._maybe_start_share_watcher()
            try:
                geo = root.geometry()
            except tk.TclError:
                geo = "?"
            self.log.info("浮窗已启动 geometry=%s dpi=%.0f scale=%.2f 屏=%s 监视器=%d",
                          geo, self._dpi, self._dpi_scale,
                          get(self.cfg, "ui.screen", "primary"), len(self._monitors))
        except Exception:
            self.log.error("UI 启动失败\n%s", traceback.format_exc())
            try:
                root.destroy()
            except Exception:
                pass
            self.root = None
            raise

    def run(self) -> None:
        if self.root is None:
            raise RuntimeError("OverlayUI.start() 未成功调用")
        self._mainloop_running = True
        try:
            self.root.mainloop()
        finally:
            self._mainloop_running = False
            self._closed = True
            self._stop_share_watcher()

    def stop(self) -> None:
        """关闭浮窗（任意线程可调用；重复调用安全）。"""
        self._closed = True
        self._stop_share_watcher()
        root = self.root
        if root is None:
            return
        try:
            if not root.winfo_exists():
                return
        except Exception:
            return
        if self._mainloop_running:
            try:
                root.after(0, self._destroy)
            except Exception:
                pass
        else:
            self._destroy()

    def _destroy(self) -> None:
        try:
            if self.root is not None and self.root.winfo_exists():
                self.root.destroy()
        except Exception:
            pass
        finally:
            self._visible = False

    # ------------------------------------------------------------ 公共便捷方法（供 main 集成）
    @property
    def is_visible(self) -> bool:
        return bool(self._visible)

    def hide(self) -> None:
        if self.root is None:
            return
        try:
            self.root.withdraw()
            self._visible = False
            self.log.info("浮窗已隐藏（F7 或热键可恢复）")
        except Exception:
            pass

    def show(self) -> None:
        if self.root is None:
            return
        try:
            self._hwnd = self._get_hwnd() or self._hwnd
            self._set_no_activate_style()
            self._show_no_activate()
            try:
                self.root.attributes("-topmost", True)
            except tk.TclError:
                pass
            self._topmost_noactivate()
            self._visible = True
            self.log.info("浮窗已显示")
        except Exception:
            pass

    def toggle_visible(self) -> None:
        if self._visible:
            self.hide()
        else:
            self.show()

    def toggle_click_through(self) -> bool:
        """鼠标穿透开关：浮窗不挡点击（再按一次恢复；热键 F4）。"""
        if self.root is None:
            return False
        try:
            hwnd = self._get_hwnd()
        except Exception:
            hwnd = 0
        if not hwnd:
            return False
        want = not bool(getattr(self, "_click_through", False))
        try:
            import ctypes
            GWL_EXSTYLE = -20
            WS_EX_LAYERED = 0x00080000
            WS_EX_TRANSPARENT = 0x00000020
            user32 = ctypes.windll.user32
            try:
                user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
                style = int(user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE))
            except Exception:
                style = int(user32.GetWindowLongW(hwnd, GWL_EXSTYLE))
            if want:
                style |= WS_EX_LAYERED | WS_EX_TRANSPARENT
            else:
                style &= ~WS_EX_TRANSPARENT
            try:
                user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ctypes.c_ssize_t(style))
            except Exception:
                user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
            self._click_through = want
        except Exception as e:  # noqa: BLE001
            self.log.warning("切换鼠标穿透失败: %s", e)
            self._click_through = False
        self._set_local_msg("鼠标穿透已开启（F4 关闭；此时按钮点不到，用热键）" if self._click_through
                            else "鼠标穿透已关闭", 4)
        return bool(self._click_through)

    def apply_settings(self, **kw: Any) -> bool:
        """应用设置（本地立即生效；同时由 settings 面板转发给 main 的 apply_settings action）。"""
        applied = False
        cfg = self.cfg
        alpha = kw.get("alpha")
        if alpha is not None:
            self._alpha = self._safe_alpha(alpha)
            cfg.setdefault("ui", {})["alpha"] = self._alpha
            if not self._evaded and self.root is not None:
                try:
                    self.root.attributes("-alpha", self._alpha)
                except Exception:
                    pass
            applied = True
        screen = kw.get("screen")
        if screen in ("primary", "secondary"):
            cfg.setdefault("ui", {})["screen"] = screen
            if not self._evaded:
                self._apply_geometry()
            self._relayout()
            applied = True
        if "evade_on_share" in kw:
            enabled = bool(kw.get("evade_on_share"))
            cfg.setdefault("ui", {})["evade_on_share"] = enabled
            if enabled:
                self._maybe_start_share_watcher()
            else:
                self._stop_share_watcher()
                if self._evaded:
                    self._exit_evade("共享避让已关闭")
            applied = True
        if kw.get("device"):
            cfg.setdefault("audio", {})["device"] = str(kw["device"])
            applied = True
        if kw.get("model"):
            cfg.setdefault("asr", {})["model"] = str(kw["model"])
            applied = True
        if kw.get("primary_model"):
            cfg.setdefault("llm", {}).setdefault("primary", {})["model"] = str(kw["primary_model"])
            applied = True
        if applied:
            self._set_local_msg("设置已保存", 4.0)
            self.log.info("设置已应用: %s", {k: v for k, v in kw.items() if v is not None})
        return applied

    # ------------------------------------------------------------ 字体 / ttk / 几何
    def _setup_fonts(self) -> None:
        preferred = str(get(self.cfg, "ui.font_family", "Microsoft YaHei UI") or "Microsoft YaHei UI")
        try:
            families = set(tkfont.families(self.root))
        except Exception:
            families = set()
        for name in (preferred, "Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑",
                     "PingFang SC", "SimHei", "Segoe UI", "Arial"):
            if not families or name in families:
                self._font_family = name
                break
        for name in ("Cascadia Mono", "Consolas", "JetBrains Mono", "Courier New"):
            if not families or name in families:
                self._mono_family = name
                break

    def _setup_ttk_style(self) -> None:
        try:
            style = ttk.Style(self.root)
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass
            style.configure("Dark.Vertical.TScrollbar", troughcolor=C_PANEL,
                            background=C_BTN, bordercolor=C_PANEL, arrowcolor=C_MUTED,
                            darkcolor=C_BTN, lightcolor=C_BTN, relief="flat", arrowsize=self._px(10))
            style.map("Dark.Vertical.TScrollbar",
                      background=[("active", C_BTN_HOVER), ("pressed", C_BTN_HOVER)])
            style.configure("Dark.TCombobox", fieldbackground=C_PANEL_2, background=C_PANEL_2,
                            foreground=C_TEXT, arrowcolor=C_MUTED, bordercolor=C_BORDER,
                            lightcolor=C_PANEL_2, darkcolor=C_PANEL_2, padding=self._px(3))
            style.map("Dark.TCombobox",
                      fieldbackground=[("readonly", C_PANEL_2)],
                      foreground=[("readonly", C_TEXT)],
                      selectbackground=[("readonly", C_PANEL_2)],
                      selectforeground=[("readonly", C_TEXT)])
        except Exception:
            pass

    def _get_hwnd(self) -> int:
        if self.root is None or os.name != "nt":
            return 0
        try:
            from ctypes import wintypes
            child = int(self.root.winfo_id())
            root_hwnd = ctypes.windll.user32.GetAncestor(wintypes.HWND(child), 2)  # GA_ROOT
            return int(root_hwnd or child)
        except Exception:
            return 0

    def _topmost_noactivate(self) -> None:
        """置顶但不抢焦点（SWP_NOACTIVATE）。"""
        if self.root is None or os.name != "nt" or not self._hwnd:
            return
        try:
            HWND_TOPMOST = ctypes.c_void_p(-1)
            flags = 0x0001 | 0x0002 | 0x0010  # NOSIZE | NOMOVE | NOACTIVATE
            ok = ctypes.windll.user32.SetWindowPos(
                ctypes.c_void_p(self._hwnd), HWND_TOPMOST, 0, 0, 0, 0, flags)
            if not ok:
                self.log.debug("SetWindowPos(TOPMOST) 失败（忽略）")
        except Exception:
            pass

    def _set_no_activate_style(self) -> None:
        """WS_EX_NOACTIVATE + WS_EX_TOOLWINDOW：置顶浮窗永不抢焦点、不进任务栏。"""
        if os.name != "nt":
            return
        hwnd = self._get_hwnd() or self._hwnd
        if not hwnd:
            return
        try:
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            GWL_EXSTYLE = -20
            WS_EX_NOACTIVATE = 0x08000000
            WS_EX_TOOLWINDOW = 0x00000080
            style = int(user32.GetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE))
            user32.SetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE,
                                  style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        except Exception:
            pass

    def _restore_foreground(self, prev_hwnd: int) -> None:
        """若浮窗抢了焦点，把前台窗口还给原窗口（仅当当前前台就是浮窗时）。"""
        if os.name != "nt" or not prev_hwnd:
            return
        hwnd = self._get_hwnd() or self._hwnd
        if not hwnd:
            return
        now = _foreground_hwnd()
        if now != hwnd:
            return
        try:
            from ctypes import wintypes
            ctypes.windll.user32.SetForegroundWindow(wintypes.HWND(int(prev_hwnd)))
        except Exception:
            pass

    def _show_no_activate(self, before: Optional[int] = None) -> None:
        """显示窗口但不抢焦点：WS_EX_NOACTIVATE + 抢到焦点就还给原窗口（持续 2 秒重试）。

        ``before`` 应传“创建 Tk 之前的原前台窗口”，否则 Tk 建窗瞬间就会激活自身，
        再取 GetForegroundWindow 已经是我们自己了。
        """
        if self.root is None:
            return
        if before is None:
            before = _foreground_hwnd()
        try:
            self.root.deiconify()
            self.root.update_idletasks()
        except Exception:
            return
        if os.name != "nt" or not before:
            return
        self._hwnd = self._get_hwnd() or self._hwnd
        if before == self._hwnd:
            return
        self._restore_foreground(before)
        self._schedule_focus_restore(before)

    def _schedule_focus_restore(self, before: int, attempts: int = 18,
                                interval_ms: int = 110) -> None:
        """窗口显示后 2 秒内反复检查，一旦发现浮窗抢了焦点就还给原窗口。"""
        def _step(remaining: int) -> None:
            self._restore_foreground(before)
            if remaining > 1 and not self._closed and self.root is not None:
                try:
                    self.root.after(interval_ms, lambda: _step(remaining - 1))
                except Exception:
                    pass

        try:
            self.root.after(0, lambda: _step(max(1, attempts)))
        except Exception:
            pass

    def _apply_display_affinity(self) -> None:
        """WDA_EXCLUDEFROMCAPTURE：让屏幕捕获类软件尽量拍不到浮窗（best-effort）。"""
        if os.name != "nt":
            return
        hwnd = self._get_hwnd() or self._hwnd
        if not hwnd:
            return
        try:
            from ctypes import wintypes
            WDA_EXCLUDEFROMCAPTURE = 0x00000011
            ok = ctypes.windll.user32.SetWindowDisplayAffinity(
                wintypes.HWND(hwnd), ctypes.c_uint(WDA_EXCLUDEFROMCAPTURE))
            if ok:
                self._capture_excluded = True
                self.log.info("已启用捕获排除（WDA_EXCLUDEFROMCAPTURE）")
            else:
                self.log.info("SetWindowDisplayAffinity 返回失败（系统不支持，已忽略）err=%s",
                              ctypes.get_last_error())
        except Exception as e:  # noqa: BLE001
            self.log.info("SetWindowDisplayAffinity 不可用（已忽略）: %s", e)

    def _target_geometry(self) -> tuple[int, int, int, int, Optional[dict]]:
        monitor = pick_monitor(self._monitors, str(get(self.cfg, "ui.screen", "primary")))
        if monitor is None:
            monitor = {"monitor": (0, 0, 1920, 1080), "work": (0, 0, 1920, 1080), "primary": True}
        w, h = self._px(self._size[0]), self._px(self._size[1])
        margin = self._px(16)
        x, y = anchor_top_right(monitor["work"], (w, h), margin)
        return w, h, x, y, monitor

    def _apply_geometry(self) -> None:
        if self.root is None:
            return
        try:
            w, h, x, y, _mon = self._target_geometry()
            self.root.geometry(f"{w}x{h}+{x}+{y}")
            self._normal_geometry = f"{w}x{h}+{x}+{y}"
        except Exception:
            self.log.warning("设置窗口几何失败\n%s", traceback.format_exc())

    # ------------------------------------------------------------ UI 构建
    def _build_ui(self) -> None:
        root = self.root
        assert root is not None
        self.container = tk.Frame(root, bg=C_BG, highlightbackground=C_BORDER,
                                  highlightthickness=max(1, self._px(1)))
        self.container.pack(fill="both", expand=True)
        self._build_header()
        self._build_message_strip()
        self._build_footer()          # 底部先 pack(side=bottom)，保证始终贴底
        self._build_tab_bar()
        self.content = tk.Frame(self.container, bg=C_BG)
        self.content.pack(fill="both", expand=True, padx=self._px(8), pady=(0, self._px(2)))
        self.transcript_frame = self._build_transcript_tab()
        self.advice_frame = self._build_advice_tab()
        self.minutes_frame = self._build_minutes_tab()
        self._build_grip()
        self._switch_tab(0, force=True)
        self._refresh_action_states()

    def _build_header(self) -> None:
        self.header = tk.Frame(self.container, bg=C_BG)
        self.header.pack(side="top", fill="x")
        row1 = tk.Frame(self.header, bg=C_BG)
        row1.pack(fill="x", padx=self._px(10), pady=(self._px(7), self._px(2)))
        self._row1 = row1

        # 右端：显式「退出」+ 折叠 + 设置（不再只能靠记 Ctrl+F12）
        self._buttons["quit"] = self._make_button(
            row1, "✕ 退出", self._on_click_quit, side="right", danger=True, padx=7)
        self._buttons["hide"] = self._make_button(
            row1, "─ 隐藏", self.toggle_visible, side="right", padx=7)
        self._buttons["settings"] = self._make_button(
            row1, "⚙ 设置", self._open_settings, side="right", padx=7)

        # 左端：标题 + 状态
        self.title_lbl = tk.Label(row1, text=str(get(self.cfg, "app.name", "Meeting Copilot") or "Meeting Copilot"),
                                  fg=C_MUTED, bg=C_BG, font=self._font(9, "bold"))
        self.title_lbl.pack(side="left", padx=(0, self._px(8)))
        self.dot_lbl = tk.Label(row1, text="●", fg=C_DIM, bg=C_BG, font=self._font(11))
        self.dot_lbl.pack(side="left")
        self.state_lbl = tk.Label(row1, text="待机", fg=C_TEXT, bg=C_BG,
                                  font=self._font(10, "bold"))
        self.state_lbl.pack(side="left", padx=(self._px(5), 0))
        self.timer_lbl = tk.Label(row1, text="00:00", fg="#cfd3da", bg=C_BG,
                                  font=self._font(10, mono=True))
        self.timer_lbl.pack(side="left", padx=(self._px(8), 0))
        self.think_lbl = tk.Label(row1, text="", fg=C_WARM, bg=C_BG, font=self._font(9, "bold"))
        self.think_lbl.pack(side="left", padx=(self._px(6), 0))
        # 延迟/RTF 放标题行（比挤在按钮行更清楚）
        self.rtf_lbl = tk.Label(row1, text="延迟 -", fg=C_MUTED, bg=C_BG, font=self._font(8))
        self.rtf_lbl.pack(side="left", padx=(self._px(8), 0))

        # 音量电平条（3px）
        self.level_canvas = tk.Canvas(self.header, height=max(2, self._px(3)), bg="#20242c",
                                      highlightthickness=0, bd=0, takefocus=0)
        self.level_canvas.pack(fill="x", padx=self._px(10), pady=(0, self._px(4)))
        self._level_rect = self.level_canvas.create_rectangle(0, 0, 0, self._px(3),
                                                              fill=C_OK, width=0)

        # 第二行：主要动作按钮（全部靠左，右侧留给窗口控制）
        row2 = tk.Frame(self.header, bg=C_BG)
        row2.pack(fill="x", padx=self._px(8), pady=(0, self._px(5)))
        self._row2 = row2
        self._buttons["listen"] = self._make_button(row2, "▶ 开始", self._on_click_listen,
                                                    accent=True, side="left", padx=7)
        self._buttons["capture"] = self._make_button(row2, "看屏 F9",
                                                      lambda: self._click_action("screenshot"),
                                                      side="left", padx=6)
        self._buttons["script"] = self._make_button(row2, "照读稿 F5",
                                                     lambda: self._click_action("script"),
                                                     side="left", padx=6)
        self._buttons["im_asked"] = self._make_button(row2, "点名 F6",
                                                       lambda: self._click_action("im_asked"),
                                                       side="left", padx=6)
        self._buttons["summary"] = self._make_button(row2, "纪要 F10",
                                                      lambda: self._click_action("summary"),
                                                      side="left", padx=6)
        self.dev_lbl = tk.Label(row2, text="默认设备", fg=C_DIM, bg=C_BG, font=self._font(8))
        self.dev_lbl.pack(side="right", padx=(self._px(2), 0))
        # 标题栏拖动（按钮除外）
        for widget in (self.header, row1, row2, self.title_lbl, self.dot_lbl, self.state_lbl,
                       self.timer_lbl, self.think_lbl, self.dev_lbl, self.rtf_lbl,
                       self.level_canvas):
            self._bind_drag(widget)

    def _build_message_strip(self) -> None:
        self.msg_lbl = tk.Label(self.container, text="", fg=C_WARM, bg=C_BG,
                                font=self._font(9), anchor="w", padx=self._px(10))
        self.msg_lbl.pack(fill="x", pady=(0, self._px(2)))

    def _build_tab_bar(self) -> None:
        self.tab_bar = tk.Frame(self.container, bg=C_BG)
        self.tab_bar.pack(fill="x", padx=self._px(8))
        sep_top = tk.Frame(self.container, bg=C_BORDER, height=max(1, self._px(1)))
        sep_top.pack(fill="x", padx=self._px(8))
        self._tab_entries = []
        for index, label in enumerate(("实时字幕", "即时建议", "纪要待办")):
            holder = tk.Frame(self.tab_bar, bg=C_BG)
            holder.pack(side="left", padx=(0, self._px(2)))
            lbl = tk.Label(holder, text=label, fg=C_MUTED, bg=C_BG, cursor="hand2",
                           font=self._font(10), padx=self._px(10), pady=self._px(3))
            lbl.pack(fill="x")
            underline = tk.Frame(holder, bg=C_BG, height=max(1, self._px(2)))
            underline.pack(fill="x")
            for widget in (holder, lbl, underline):
                widget.bind("<Button-1>", lambda _e, i=index: self._switch_tab(i))
            self._tab_entries.append({"label": label, "holder": holder, "lbl": lbl,
                                      "underline": underline, "frame": None})

    def _build_transcript_tab(self) -> tk.Frame:
        frame = tk.Frame(self.content, bg=C_BG)
        wrap = tk.Frame(frame, bg=C_BG)
        wrap.pack(fill="both", expand=True)
        self.transcript = tk.Text(wrap, bg=C_PANEL, fg=C_TEXT, insertbackground=C_TEXT,
                                  relief="flat", bd=0, highlightthickness=0, wrap="char",
                                  padx=self._px(9), pady=self._px(8), cursor="arrow",
                                  font=self._font(10), state="disabled",
                                  spacing1=self._px(3), spacing3=self._px(3))
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.transcript.yview,
                            style="Dark.Vertical.TScrollbar")
        self.transcript.configure(yscrollcommand=vsb.set)
        self.transcript.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.transcript.tag_configure("ts", foreground=C_TS, font=self._font(9, mono=True))
        self.transcript.tag_configure("body", foreground=C_TEXT, font=self._font(10))
        self.transcript.tag_configure("muted", foreground=C_DIM, font=self._font(9))
        self.transcript.insert("end", "等待声音…\n播放腾讯会议/视频的声音后，这里会出现实时字幕。\n",
                               ("muted",))
        self.transcript.configure(state="disabled")
        self.transcript.bind("<MouseWheel>",
                             lambda _e: self._after(30, self._sync_transcript), add="+")
        return frame

    def _build_advice_tab(self) -> tk.Frame:
        frame = tk.Frame(self.content, bg=C_BG)

        # ---- 上方：固定"当前答复"区（最新应答思路 + 照读稿），不用翻就能看到
        tk.Label(frame, text="当前答复 · 最新", fg=C_MUTED, bg=C_BG,
                 font=self._font(8, "bold"), anchor="w").pack(
            fill="x", padx=self._px(10), pady=(self._px(4), self._px(2)))
        pin_h = int(get(self.cfg, "ui.pin_height", 330) or 330)
        self.advice_pin_outer, self.advice_pin_canvas, self.advice_pin_inner = \
            make_scroll_area(frame, C_BG)
        self.advice_pin_canvas.configure(height=self._px(pin_h))
        self.advice_pin_outer.pack(fill="x", padx=self._px(6))
        self.advice_pin_empty = tk.Label(
            self.advice_pin_inner,
            text="等老师点名/提问后，这里固定显示「应答思路 + 照读稿」，不用来回翻。",
            fg=C_DIM, bg=C_BG, font=self._font(9), justify="left", anchor="w",
            padx=self._px(8), pady=self._px(8))
        self.advice_pin_empty.pack(fill="x")
        self.advice_pin_cards = tk.Frame(self.advice_pin_inner, bg=C_BG)
        self.advice_pin_cards.pack(fill="x")
        pin_handler = self._make_wheel_handler(self.advice_pin_canvas, "_advice_pin_follow")
        self._bind_wheel(self.advice_pin_canvas, pin_handler)
        self._bind_wheel(self.advice_pin_inner, pin_handler)
        self._advice_pin_follow = True

        # ---- 分隔 + 下方：过程记录（要点/术语/读屏，限量紧凑，自动滚最新）
        sep = tk.Frame(frame, bg=C_BORDER, height=max(1, self._px(1)))
        sep.pack(fill="x", padx=self._px(8), pady=self._px(4))
        tk.Label(frame, text="过程记录 · 要点 / 术语 / 读屏（只留最近几条）", fg=C_MUTED,
                 bg=C_BG, font=self._font(8, "bold"), anchor="w").pack(
            fill="x", padx=self._px(10), pady=(0, self._px(2)))
        self.advice_outer, self.advice_canvas, self.advice_inner = make_scroll_area(frame, C_BG)
        self.advice_outer.pack(fill="both", expand=True)
        self._advice_follow = True
        self.advice_empty = tk.Label(
            self.advice_inner, text="等待 AI 建议…\n老师发言后自动生成要点 / 术语 / 屏幕解读",
            fg=C_DIM, bg=C_BG, font=self._font(9), justify="center", pady=self._px(20))
        self.advice_empty.pack(fill="x")
        self.advice_cards = tk.Frame(self.advice_inner, bg=C_BG)
        self.advice_cards.pack(fill="x")
        handler = self._make_wheel_handler(self.advice_canvas, "_advice_follow")
        self._bind_wheel(self.advice_canvas, handler)
        self._bind_wheel(self.advice_inner, handler)
        return frame

    def _build_minutes_tab(self) -> tk.Frame:
        frame = tk.Frame(self.content, bg=C_BG)
        bottom = tk.Frame(frame, bg=C_BG)
        bottom.pack(side="bottom", fill="x", pady=(self._px(4), 0))
        sep = tk.Frame(bottom, bg=C_BORDER, height=max(1, self._px(1)))
        sep.pack(fill="x", pady=(0, self._px(5)))
        bar = tk.Frame(bottom, bg=C_BG)
        bar.pack(fill="x")
        self.export_btn = tk.Button(bar, text="导出纪要", command=lambda: self._click_action("export"),
                                    bg=C_ACCENT, fg="#0f1115", activebackground="#7ab0ff",
                                    activeforeground="#0f1115", bd=0, relief="flat",
                                    font=self._font(10, "bold"), padx=self._px(16),
                                    pady=self._px(5), cursor="hand2", highlightthickness=0,
                                    takefocus=0)
        self.export_btn.pack(side="left")
        tk.Label(bar, text="F10 阶段小结 · Ctrl+F10 导出", fg=C_DIM, bg=C_BG,
                 font=self._font(8)).pack(side="right")
        self._bind_hover(self.export_btn, C_ACCENT, "#7ab0ff")

        self.minutes_outer, self.minutes_canvas, self.minutes_inner = make_scroll_area(frame, C_BG)
        self.minutes_outer.pack(fill="both", expand=True)
        self._minutes_follow = True
        self.minutes_empty = tk.Label(
            self.minutes_inner, text="本场还没有阶段小结……（按 F10 手动生成，或每 5 分钟自动生成）",
            fg=C_DIM, bg=C_BG, font=self._font(9), justify="left", anchor="w",
            wraplength=self._card_wrap(), padx=self._px(8))
        self.minutes_empty.pack(fill="x", pady=(self._px(10), self._px(4)))
        self.minutes_cards = tk.Frame(self.minutes_inner, bg=C_BG)
        self.minutes_cards.pack(fill="x")
        tk.Label(self.minutes_inner, text="待办事项（自动从纪要中提取）", fg=C_MUTED,
                 bg=C_BG, font=self._font(9, "bold"), anchor="w").pack(
            fill="x", padx=self._px(8), pady=(self._px(8), self._px(3)))
        self.todo_frame = tk.Frame(self.minutes_inner, bg=C_BG)
        self.todo_frame.pack(fill="x")
        handler = self._make_wheel_handler(self.minutes_canvas, "_minutes_follow")
        self._bind_wheel(self.minutes_canvas, handler)
        self._bind_wheel(self.minutes_inner, handler)
        self._rebuild_todos()
        return frame

    def _build_footer(self) -> None:
        footer = tk.Frame(self.container, bg=C_BG)
        footer.pack(side="bottom", fill="x", padx=self._px(10),
                    pady=(self._px(3), self._px(5)))
        sep = tk.Frame(self.container, bg=C_BORDER, height=max(1, self._px(1)))
        sep.pack(side="bottom", fill="x", padx=self._px(8))
        tk.Label(footer, text=HOTKEY_HINT, fg=C_DIM, bg=C_BG, font=self._font(8),
                 anchor="w").pack(side="left")
        tk.Label(footer, text="", fg=C_DIM, bg=C_BG, font=self._font(8)).pack(side="right")

    def _build_grip(self) -> None:
        self.grip = tk.Label(self.root, text="◢", fg="#4a4f5a", bg=C_BG,
                             font=self._font(10), cursor="size_nw_se", takefocus=0)
        try:
            self.grip.place(relx=1.0, rely=1.0, anchor="se", x=-self._px(1), y=-self._px(1))
        except Exception:
            pass
        self.grip.bind("<ButtonPress-1>", self._resize_start)
        self.grip.bind("<B1-Motion>", self._resize_move)
        self.grip.bind("<ButtonRelease-1>", self._resize_end)

    # ---- 控件辅助
    def _make_button(self, parent: tk.Misc, text: str, command: Callable[[], Any], *,
                     accent: bool = False, side: str = "left", danger: bool = False,
                     padx: Optional[int] = None) -> tk.Button:
        bg = C_ACCENT if accent else (C_ERR_SOFT if danger else C_BTN)
        fg = "#0f1115" if accent else (C_ERR if danger else C_TEXT)
        hover = "#7ab0ff" if accent else ("#4a2a2f" if danger else C_BTN_HOVER)
        btn = tk.Button(parent, text=text, command=command, bg=bg, fg=fg,
                        activebackground=hover, activeforeground=fg, bd=0, relief="flat",
                        font=self._font(9, "bold"), padx=self._px(9 if padx is None else padx),
                        pady=self._px(3), cursor="hand2", highlightthickness=0,
                        takefocus=0, disabledforeground=C_DIM)
        btn.pack(side=side, padx=self._px(2))
        self._bind_hover(btn, bg, hover)
        return btn

    def _refresh_tab_labels(self) -> None:
        """标签页未读角标：``即时建议·2``。"""
        for i, entry in enumerate(self._tab_entries):
            try:
                base = str(entry.get("label") or "")
                n = int(self._tab_unread[i]) if i < len(self._tab_unread) else 0
                active = i == self._active_tab
                entry["lbl"].configure(
                    text=f"{base}·{n}" if n > 0 else base,
                    fg=C_TEXT if active else (C_ACCENT if n > 0 else C_MUTED))
            except Exception:
                pass

    def _on_click_quit(self) -> None:
        """显式退出入口：先弹确认（会导出纪要），再走正常关闭流程。"""
        ok = True
        try:
            from tkinter import messagebox
            ok = bool(messagebox.askokcancel(
                "退出Meeting Copilot",
                "退出前会自动生成并导出本场纪要。\n\n确定现在退出吗？（快捷键 Ctrl+F12）",
                parent=self.root))
        except Exception:
            ok = True
        if not ok:
            self._set_local_msg("已取消退出", 2.5)
            return
        self._set_local_msg("正在退出并导出纪要…", 5)
        try:
            self._invoke("quit")
        except Exception:
            pass
        self._after(120, self.stop)

    def _bind_hover(self, btn: tk.Button, normal_bg: str, hover_bg: str) -> None:
        def _enter(_e):
            try:
                if str(btn.cget("state")) != "disabled":
                    btn.configure(bg=hover_bg)
            except Exception:
                pass

        def _leave(_e):
            try:
                if str(btn.cget("state")) != "disabled":
                    btn.configure(bg=normal_bg)
            except Exception:
                pass

        btn.bind("<Enter>", _enter, add="+")
        btn.bind("<Leave>", _leave, add="+")

    def _bind_drag(self, widget: tk.Misc) -> None:
        try:
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
            widget.bind("<ButtonRelease-1>", self._drag_end)
        except Exception:
            pass

    def _bind_wheel(self, widget: tk.Misc, handler: Callable[[Any], str]) -> None:
        try:
            widget.bind("<MouseWheel>", handler, add="+")
        except Exception:
            pass

    def _make_wheel_handler(self, canvas: tk.Canvas, follow_attr: str) -> Callable[[Any], str]:
        def handler(event) -> str:  # noqa: ANN001
            try:
                step = -2 if int(getattr(event, "delta", 0) or 0) > 0 else 2
                canvas.yview_scroll(step, "units")
                setattr(self, follow_attr, canvas.yview()[1] >= 0.995)
            except Exception:
                pass
            return "break"

        return handler

    def _after(self, ms: int, fn: Callable[[], Any]) -> None:
        try:
            if self.root is not None:
                self.root.after(ms, fn)
        except Exception:
            pass

    # ------------------------------------------------------------ 标签页/布局
    def _switch_tab(self, index: int, *, force: bool = False) -> None:
        if index == self._active_tab and not force:
            return
        self._active_tab = index
        frames = [self.transcript_frame, self.advice_frame, self.minutes_frame]
        for i, entry in enumerate(self._tab_entries):
            active = i == index
            entry["lbl"].configure(fg=C_TEXT if active else C_MUTED,
                                   font=self._font(10, "bold" if active else "normal"))
            entry["underline"].configure(bg=C_ACCENT if active else C_BG)
        for i, frame in enumerate(frames):
            try:
                if i == index:
                    frame.pack(fill="both", expand=True)
                else:
                    frame.pack_forget()
            except Exception:
                pass
        self._tab_unread[index] = 0
        self._refresh_tab_labels()
        self._after(30, self._relayout)

    def _relayout(self) -> None:
        if self.root is None or self._closed:
            return
        wrap = self._card_wrap()
        for card_widget in self._card_widgets.values():
            card_widget.relayout()
        for lbl in self._todo_labels:
            try:
                lbl.configure(wraplength=wrap)
            except Exception:
                pass
        try:
            self.minutes_empty.configure(wraplength=wrap)
        except Exception:
            pass
        # 切换回已渲染过的面板时，若之前停在底部则补一次滚底
        try:
            if self._active_tab == 1 and self._advice_follow:
                self._auto_scroll(self.advice_canvas)
            elif self._active_tab == 2 and self._minutes_follow:
                self._auto_scroll(self.minutes_canvas)
        except Exception:
            pass

    def _on_root_configure(self, event) -> None:  # noqa: ANN001
        if self.root is None or event.widget is not self.root:
            return
        if self._configure_job is not None:
            try:
                self.root.after_cancel(self._configure_job)
            except Exception:
                pass
        self._configure_job = self.root.after(140, self._relayout)

    def _drag_start(self, event) -> None:  # noqa: ANN001
        if self.root is None:
            return
        try:
            self._drag_dx = int(event.x_root) - int(self.root.winfo_x())
            self._drag_dy = int(event.y_root) - int(self.root.winfo_y())
        except Exception:
            self._drag_dx = self._drag_dy = 0

    def _drag_move(self, event) -> None:  # noqa: ANN001
        if self.root is None:
            return
        try:
            x = int(event.x_root) - self._drag_dx
            y = int(event.y_root) - self._drag_dy
            self.root.geometry(f"+{x}+{y}")
        except Exception:
            pass

    def _drag_end(self, _event=None) -> None:  # noqa: ANN001
        if self.root is not None and not self._evaded:
            try:
                self._normal_geometry = self.root.geometry()
            except Exception:
                pass

    def _resize_start(self, event) -> None:  # noqa: ANN001
        if self.root is None:
            return
        try:
            self._resize_origin = (int(event.x_root), int(event.y_root),
                                   int(self.root.winfo_x()), int(self.root.winfo_y()),
                                   int(self.root.winfo_width()), int(self.root.winfo_height()))
        except Exception:
            self._resize_origin = None

    def _resize_move(self, event) -> None:  # noqa: ANN001
        if self.root is None or self._resize_origin is None:
            return
        x0, y0, wx, wy, w0, h0 = self._resize_origin
        try:
            w = max(self._px(360), w0 + int(event.x_root) - x0)
            h = max(self._px(420), h0 + int(event.y_root) - y0)
            self.root.geometry(f"{w}x{h}+{wx}+{wy}")
        except Exception:
            pass

    def _resize_end(self, _event=None) -> None:  # noqa: ANN001
        self._resize_origin = None
        self._drag_end()

    # ------------------------------------------------------------ 数据轮询
    def _poll(self) -> None:
        self._poll_job = None
        if self._closed or self.root is None:
            return
        try:
            self._drain_commands()
            if self._closed:
                return
            self._drain_transcripts()
            self._drain_brain()
            self._drain_status()
            self._drain_logs()
            self._drain_devices()
            self._refresh_timer()
            self._refresh_message()
            self._apply_share_state()
        except Exception:
            self.log.warning("UI 轮询异常\n%s", traceback.format_exc())
        finally:
            if not self._closed:
                try:
                    self._poll_job = self.root.after(self.POLL_MS, self._poll)
                except Exception:
                    pass

    def _animate(self) -> None:
        self._animate_job = None
        if self._closed or self.root is None:
            return
        self._frame = (self._frame + 1) % 1000
        try:
            if self._thinking_deep:
                dots = "·" * (1 + self._frame % 4)
                self.think_lbl.configure(text=f"思考中{dots}", fg=C_WARM)
            elif self._thinking_fast:
                dots = "·" * (1 + self._frame % 3)
                self.think_lbl.configure(text=f"生成中{dots}", fg=C_ACCENT)
            else:
                self.think_lbl.configure(text="")
            # 聆听状态圆点轻闪
            if self._listening:
                self.dot_lbl.configure(fg=C_OK if self._frame % 2 == 0 else "#2f8f63")
            else:
                self.dot_lbl.configure(fg=C_ERR)
        except Exception:
            pass
        try:
            self._animate_job = self.root.after(self.ANIMATE_MS, self._animate)
        except Exception:
            pass

    # ---- 命令总线（UI 独立消费；绝不回写 bus.commands）
    def _drain_commands(self) -> None:
        if self.bus is None:
            return
        for _ in range(24):
            try:
                cmd = self.bus.commands.get_nowait()
            except queue.Empty:
                return
            except Exception:
                return
            try:
                self._handle_ui_command(cmd)
            except Exception:
                self.log.warning("UI 命令处理失败 %r\n%s", cmd, traceback.format_exc())

    def _handle_ui_command(self, cmd: Any) -> None:
        if not isinstance(cmd, dict):
            return
        name = str(cmd.get("cmd") or "").strip()
        if not name:
            return
        now = time.monotonic()
        last = self._dispatch_ts.get(name, 0.0)
        if self.ECHO_WINDOW_S > 0 and (now - last) < self.ECHO_WINDOW_S:
            return  # 回声抑制（防止 actions 内部误回写造成自激循环）
        burst = self._dispatch_burst.setdefault(name, [])
        burst[:] = [t for t in burst if now - t < 2.0]
        if len(burst) >= self.BURST_LIMIT:
            self.log.warning("命令 %s 2 秒内触发 %d 次，疑似回路，已忽略", name, len(burst))
            return
        burst.append(now)
        self._dispatch_ts[name] = now

        if name == "hide":
            self.toggle_visible()
            return
        if name == "clickthrough":
            self.toggle_click_through()
            return
        if name == "quit":
            self.log.info("收到 quit 命令，准备关闭浮窗")
            self._invoke("quit")
            self.stop()
            return
        # pause/resume/grab 兼容别名
        alias = {"pause": "stop_listen", "resume": "start_listen", "grab": "screenshot"}
        action_name = alias.get(name, name)
        kwargs = {k: v for k, v in cmd.items() if k != "cmd"}
        if action_name in self._actions:
            self._call_action(action_name, **kwargs)
        else:
            self.log.info("收到未注册的 UI 命令，已忽略: %s", name)

    def _call_action(self, name: str, **kwargs: Any) -> bool:
        action = self._actions.get(name)
        if action is None:
            return False
        try:
            if kwargs:
                try:
                    action(**kwargs)
                except TypeError:
                    action()
            else:
                action()
            self._actions_fired.append(name)
            return True
        except Exception:
            self.log.warning("动作 %s 执行失败\n%s", name, traceback.format_exc())
            return False

    def _invoke(self, name: str, **kwargs: Any) -> bool:
        return self._call_action(name, **kwargs)

    def _click_action(self, name: str) -> None:
        if name not in self._actions:
            self.log.info("动作未注册，忽略点击: %s", name)
            return
        if name == "screenshot":
            self._set_local_msg("正在看屏幕…", 5.0)
        elif name == "summary":
            self._set_local_msg("正在生成阶段小结…", 5.0)
        elif name == "export":
            self._set_local_msg("正在生成最终纪要并导出…", 8.0)
        self._call_action(name)

    def _on_click_listen(self) -> None:
        target = "toggle_listen" if "toggle_listen" in self._actions else (
            "stop_listen" if self._listening else "start_listen")
        if target not in self._actions:
            self._set_local_msg("聆听动作未就绪（模块加载中）", 3.0)
            return
        self._listening = not self._listening  # 乐观更新，下一帧 Status 覆盖
        self._refresh_listen_button()
        self._call_action(target)

    # ---- 字幕
    def _drain_transcripts(self) -> None:
        if self.bus is None:
            return
        for _ in range(40):
            try:
                seg = self.bus.transcripts.get_nowait()
            except queue.Empty:
                return
            except Exception:
                return
            try:
                self._append_transcript(seg)
            except Exception:
                self.log.warning("字幕渲染失败\n%s", traceback.format_exc())

    def _transcript_at_bottom(self) -> bool:
        try:
            first, last = self.transcript.yview()
            return last >= 0.999 or (first <= 0.0001 and last >= 0.999)
        except Exception:
            return True

    def _sync_transcript(self) -> None:
        pass  # 视觉同步用：下一次追加时按当前位置判断是否跟随

    def _append_transcript(self, seg: TranscriptSegment) -> None:
        text = str(getattr(seg, "display_text", "") or "").strip()
        if not text:
            return
        is_final = bool(getattr(seg, "is_final", True))
        at_bottom = self._transcript_at_bottom()
        widget = self.transcript
        widget.configure(state="normal")
        if self._transcript_placeholder:
            widget.delete("1.0", "end")
            self._transcript_placeholder = False
        # 「边说边出字」：语音进行中的草稿行原地替换；正式句到达后删草稿再写正式行
        try:
            prev = widget.tag_ranges("partial")
            if prev:
                widget.delete(prev[0], prev[1])
        except Exception:
            pass
        ts = fmt_ts(getattr(seg, "t_start", 0.0))
        if not is_final:
            try:
                widget.tag_configure("partial", foreground=C_DIM)
            except Exception:
                pass
            widget.insert("end", f"[{ts}] {text}▍\n", ("partial",))
            widget.configure(state="disabled")
            if at_bottom:
                widget.see("end")
            return
        widget.insert("end", f"[{ts}] ", ("ts",))
        widget.insert("end", text + "\n", ("body",))
        self._transcript_count += 1
        try:
            lines = int(widget.index("end-1c").split(".")[0])
            if lines > 1400:
                widget.delete("1.0", "400.0")
        except Exception:
            pass
        widget.configure(state="disabled")
        if at_bottom:
            widget.see("end")

    # ---- 建议卡片
    def _drain_brain(self) -> None:
        if self.bus is None:
            return
        for _ in range(160):
            try:
                ev = self.bus.brain_events.get_nowait()
            except queue.Empty:
                return
            except Exception:
                return
            try:
                self._on_brain_event(ev)
            except Exception:
                self.log.warning("建议事件渲染失败\n%s", traceback.format_exc())

    def _on_brain_event(self, ev: BrainEvent) -> None:
        card, is_new = self._card_store.put(ev)
        widget = self._card_widgets.get(card.request_id)
        if widget is None or is_new:
            self._create_card_widget(card)
            widget = self._card_widgets.get(card.request_id)
        if widget is not None:
            widget.refresh(card)
        # 被点名/追问的第一条建议到达时，如果用户还在看字幕页，自动跳到「即时建议」
        if is_new and card.kind in (KIND_ANSWER, KIND_SCRIPT) and self._active_tab == 0:
            self._switch_tab(1)
        # 未读角标：新卡片落在非当前页时累加
        if is_new:
            tab = 2 if card.is_minutes_kind else 1
            if 0 <= tab < len(self._tab_unread) and tab != self._active_tab:
                self._tab_unread[tab] += 1
                self._refresh_tab_labels()
        rid = card.request_id
        if ev.done or ev.error:
            self._thinking_deep.discard(rid)
            self._thinking_fast.discard(rid)
        elif card.lane == LANE_DEEP or card.kind in (KIND_ANSWER, KIND_SLIDE,
                                                     KIND_SUMMARY, KIND_MINUTES):
            self._thinking_deep.add(rid)
            self._thinking_fast.discard(rid)
        else:
            self._thinking_fast.add(rid)
            self._thinking_deep.discard(rid)
        if ev.done and card.kind in CARD_TAB_KINDS and card.text:
            self._add_todos(extract_todos(card.text))
        if card.is_minutes_kind:
            if self._minutes_follow:
                self._after(20, lambda: self._auto_scroll(self.minutes_canvas))
        elif card.kind in (KIND_ANSWER, KIND_SCRIPT):
            # 固定答复区：新答复出现时回到开头，保证第一眼看到答案
            if is_new:
                self._after(20, lambda: self._auto_scroll_top(self.advice_pin_canvas))
        else:
            if self._advice_follow:
                self._after(20, lambda: self._auto_scroll(self.advice_canvas))

    def _auto_scroll(self, canvas: tk.Canvas) -> None:
        try:
            canvas.update_idletasks()
            canvas.yview_moveto(1.0)
        except Exception:
            pass

    def _auto_scroll_top(self, canvas: tk.Canvas) -> None:
        try:
            canvas.update_idletasks()
            canvas.yview_moveto(0.0)
        except Exception:
            pass

    def _create_card_widget(self, card: Card) -> None:
        if card.request_id in self._card_widgets:
            return
        is_pin = card.kind in (KIND_ANSWER, KIND_SCRIPT)
        if card.is_minutes_kind:
            parent = self.minutes_cards
            try:
                self.minutes_empty.pack_forget()
            except Exception:
                pass
        elif is_pin:
            # 固定区只留最新的一张：新建前先把旧的同类清掉（上限 1）
            self._evict_kind(card.kind, keep=0)
            parent = self.advice_pin_cards
            try:
                self.advice_pin_empty.pack_forget()
            except Exception:
                pass
        else:
            # 过程记录：每类只留最近几条，防止 3 小时会议堆几百张卡片
            keep = KIND_KEEP.get(card.kind, 3)
            self._evict_kind(card.kind, keep=max(0, keep - 1))
            parent = self.advice_cards
            try:
                self.advice_empty.pack_forget()
            except Exception:
                pass
        widget = _CardWidget(parent, card, self)
        widget.bind_wheel(self._make_wheel_handler(
            self.minutes_canvas if card.is_minutes_kind else (
                self.advice_pin_canvas if is_pin else self.advice_canvas),
            "_minutes_follow" if card.is_minutes_kind else (
                "_advice_pin_follow" if is_pin else "_advice_follow")))
        self._card_widgets[card.request_id] = widget
        self._card_order.append(card.request_id)
        while len(self._card_order) > self.MAX_CARDS:
            old = self._card_order.pop(0)
            old_widget = self._card_widgets.pop(old, None)
            if old_widget is not None:
                old_widget.destroy()

    def _evict_kind(self, kind: str, keep: int = 1) -> None:
        """同一 kind 只保留最近 keep 张（keep=0 表示全清）；其余连同控件销毁。"""
        keep = max(0, int(keep))
        same = [rid for rid in self._card_order
                if rid in self._card_widgets
                and getattr(self._card_widgets[rid].card, "kind", "") == kind]
        targets = same if keep == 0 else (same[:len(same) - keep] if len(same) > keep else [])
        for rid in targets:
            widget = self._card_widgets.pop(rid, None)
            if widget is not None:
                try:
                    widget.destroy()
                except Exception:
                    pass
            try:
                self._card_order.remove(rid)
            except ValueError:
                pass

    def _add_todos(self, todos: list[str]) -> None:
        if not todos:
            return
        changed = False
        for todo in todos:
            if todo and todo not in self._todos:
                self._todos.append(todo)
                changed = True
        if changed:
            self._rebuild_todos()
            if self._minutes_follow:
                self._after(20, lambda: self._auto_scroll(self.minutes_canvas))

    def _rebuild_todos(self) -> None:
        try:
            for child in self.todo_frame.winfo_children():
                child.destroy()
        except Exception:
            pass
        self._todo_labels = []
        wrap = self._card_wrap()
        if not self._todos:
            lbl = tk.Label(self.todo_frame, text="□ 暂无待办（阶段小结 / 纪要生成后自动提取）",
                           fg=C_DIM, bg=C_BG, font=self._font(9), anchor="w",
                           justify="left", wraplength=wrap)
            lbl.pack(fill="x", padx=self._px(8), pady=self._px(3))
            self._todo_labels.append(lbl)
            return
        for todo in self._todos:
            row = tk.Frame(self.todo_frame, bg=C_BG)
            row.pack(fill="x", padx=self._px(8), pady=self._px(2))
            tk.Label(row, text="□", fg=C_ACCENT, bg=C_BG, font=self._font(10, "bold"),
                     anchor="n").pack(side="left", padx=(0, self._px(5)))
            lbl = tk.Label(row, text=todo, fg=C_TEXT, bg=C_BG, font=self._font(9),
                           anchor="w", justify="left", wraplength=wrap)
            lbl.pack(side="left", fill="x", expand=True)
            self._todo_labels.append(lbl)

    # ---- 状态栏
    def _drain_status(self) -> None:
        if self.bus is None:
            return
        latest: Optional[Status] = None
        for _ in range(60):
            try:
                latest = self.bus.status.get_nowait()
            except queue.Empty:
                break
            except Exception:
                break
        if latest is not None:
            self._status = latest
            self._got_status = True
            self._status_ts = time.monotonic()
            self._apply_status(latest)

    def _apply_status(self, st: Status) -> None:
        self._listening = bool(st.listening)
        label = status_text(self._listening, bool(st.asr_ready), True)
        self.state_lbl.configure(text=label)
        self.dot_lbl.configure(fg=C_OK if self._listening else C_ERR)
        self.rtf_lbl.configure(text=delay_text(getattr(st, "last_delay", 0.0), st.last_rtf))
        self.dev_lbl.configure(text=short_device(st.device, 8))
        self._refresh_level(st.audio_level)
        self._refresh_listen_button()
        self._refresh_vision_button()

    def _refresh_level(self, level: float) -> None:
        try:
            width = max(1, int(self.level_canvas.winfo_width()))
            height = max(2, self._px(3))
            try:
                lv = max(0.0, min(1.0, float(level or 0.0)))
            except (TypeError, ValueError):
                lv = 0.0
            fill = int(width * lv)
            color = "#3fbf7f" if lv < 0.85 else C_WARM
            self.level_canvas.coords(self._level_rect, 0, 0, fill, height)
            self.level_canvas.itemconfigure(self._level_rect, fill=color)
        except Exception:
            pass

    def _refresh_listen_button(self) -> None:
        btn = self._buttons.get("listen")
        if btn is None:
            return
        available = any(k in self._actions for k in
                        ("toggle_listen", "start_listen", "stop_listen"))
        if not available:
            btn.configure(state="disabled", text="开始", bg=C_BTN, fg=C_TEXT)
            return
        try:
            btn.configure(state="normal")
            if self._listening:
                btn.configure(text="‖ 暂停", bg=C_BTN, fg=C_TEXT)
            else:
                btn.configure(text="▶ 开始", bg=C_ACCENT, fg="#0f1115")
        except Exception:
            pass

    def _drain_logs(self) -> None:
        if self.bus is None:
            return
        for _ in range(30):
            try:
                msg = self.bus.logs.get_nowait()
            except queue.Empty:
                return
            except Exception:
                return
            try:
                self.log.debug("bus: %s", msg)
            except Exception:
                pass

    def _drain_devices(self) -> None:
        try:
            names, source = self._devices_q.get_nowait()
        except queue.Empty:
            return
        except Exception:
            return
        win = self._settings_win
        if win is None:
            return
        try:
            if not win.winfo_exists():
                return
            combo = getattr(self, "_device_combo", None)
            if combo is not None:
                combo.configure(values=names)
                if source:
                    self.log.info("输出设备列表来源: %s（%d 个）", source, len(names))
        except Exception:
            pass

    def _refresh_timer(self) -> None:
        if self.root is None:
            return
        now = time.monotonic()
        sec = self._status.session_sec if self._got_status else 0.0
        if not self._got_status or (now - self._status_ts) > 5.0:
            sec = max(float(sec or 0.0), now - self._t0)
        try:
            self.timer_lbl.configure(text=fmt_ts(sec))
        except Exception:
            pass

    def _set_local_msg(self, text: str, seconds: float = 4.0) -> None:
        self._local_msg = str(text or "")
        self._local_msg_until = time.monotonic() + max(0.5, float(seconds))

    def _refresh_message(self) -> None:
        if self.root is None:
            return
        status_msg = str(getattr(self._status, "message", "") or "").strip()
        if status_msg:
            self.msg_lbl.configure(text="· " + truncate_text(status_msg, 52), fg=C_WARM)
        elif time.monotonic() < self._local_msg_until and self._local_msg:
            self.msg_lbl.configure(text="· " + truncate_text(self._local_msg, 52), fg=C_ACCENT)
        else:
            self.msg_lbl.configure(text="")

    def _refresh_action_states(self) -> None:
        self._action_available = {
            "start_listen": "start_listen" in self._actions,
            "stop_listen": "stop_listen" in self._actions,
            "toggle_listen": "toggle_listen" in self._actions,
            "screenshot": "screenshot" in self._actions,
            "summary": "summary" in self._actions,
            "export": "export" in self._actions,
            "im_asked": "im_asked" in self._actions,
            "script": "script" in self._actions,
            "apply_settings": "apply_settings" in self._actions,
        }

        def _state(btn: Optional[tk.Button], available: bool) -> None:
            if btn is None:
                return
            try:
                btn.configure(state="normal" if available else "disabled")
            except Exception:
                pass

        _state(self._buttons.get("capture"), self._action_available["screenshot"])
        _state(self._buttons.get("summary"), self._action_available["summary"])
        _state(self._buttons.get("script"), self._action_available["script"])
        _state(self._buttons.get("im_asked"), self._action_available["im_asked"])
        for _key in ("quit", "hide", "settings"):
            _state(self._buttons.get(_key), True)
        try:
            self.export_btn.configure(
                state="normal" if self._action_available["export"] else "disabled")
        except Exception:
            pass
        self._refresh_listen_button()
        capture_btn = self._buttons.get("capture")
        if capture_btn is not None and not self._action_available["screenshot"]:
            try:
                capture_btn.configure(text="看屏 F9", fg=C_TEXT)
            except Exception:
                pass
        self._refresh_vision_button()

    def _refresh_vision_button(self) -> None:
        btn = self._buttons.get("capture")
        if btn is None:
            return
        busy = bool(getattr(self._status, "vision_busy", False))
        available = self._action_available.get("screenshot", False)
        try:
            if busy:
                btn.configure(text="看屏中…", state="disabled")
            elif available:
                btn.configure(text="看屏 F9", state="normal")
        except Exception:
            pass

    # ------------------------------------------------------------ 共享避让
    def _maybe_start_share_watcher(self) -> None:
        if not bool(get(self.cfg, "ui.evade_on_share", False)):
            return
        if os.name != "nt":
            return
        if self._share_thread is not None and self._share_thread.is_alive():
            return
        self._share_stop.clear()
        self._share_thread = threading.Thread(target=self._share_watch_loop,
                                              name="ui-share-watch", daemon=True)
        self._share_thread.start()
        self.log.info("共享避让检测已启动（每 2 秒）")

    def _stop_share_watcher(self) -> None:
        self._share_stop.set()
        thread = self._share_thread
        self._share_thread = None
        with self._share_lock:
            self._share_info = None

    def _share_watch_loop(self) -> None:
        while not self._share_stop.wait(2.0):
            info = None
            try:
                info = detect_share_window(self._hwnd)
            except Exception:
                info = None
            with self._share_lock:
                self._share_info = info
            self._share_stop.wait(0.01)

    def _apply_share_state(self) -> None:
        if not bool(get(self.cfg, "ui.evade_on_share", False)):
            return
        if not self._started:
            return
        with self._share_lock:
            info = self._share_info
        detected = info is not None
        if detected:
            self._share_misses = 0
            if not self._evaded:
                self._enter_evade(info)
        elif self._evaded:
            self._share_misses += 1
            if self._share_misses >= 3:
                self._exit_evade("共享结束，浮窗已恢复")
        else:
            self._share_misses = 0

    def _enter_evade(self, info: Optional[dict]) -> None:
        if self.root is None or self._evaded:
            return
        self._evaded = True
        try:
            self._pre_evade_geometry = self.root.geometry()
        except Exception:
            self._pre_evade_geometry = ""
        try:
            self.root.attributes("-alpha", 0.30)
        except Exception:
            pass
        normal_screen = str(get(self.cfg, "ui.screen", "primary"))
        if normal_screen != "secondary":
            secondary = pick_monitor(self._monitors, "secondary")
            primary = pick_monitor(self._monitors, "primary")
            if secondary is not None and secondary is not primary:
                try:
                    w, h = self._px(self._size[0]), self._px(self._size[1])
                    x, y = anchor_top_right(secondary["work"], (w, h), self._px(16))
                    self.root.geometry(f"{w}x{h}+{x}+{y}")
                except Exception:
                    pass
        title = (info or {}).get("title", "?")
        reason = (info or {}).get("reason", "")
        self._set_local_msg("检测到共享，浮窗已避让（F7 可隐藏）", 6.0)
        self.log.info("共享避让开启: reason=%s title=%s", reason, title)

    def _exit_evade(self, message: str = "") -> None:
        if not self._evaded:
            return
        self._evaded = False
        self._share_misses = 0
        try:
            self.root.attributes("-alpha", self._alpha)
        except Exception:
            pass
        if self._pre_evade_geometry:
            try:
                self.root.geometry(self._pre_evade_geometry)
            except Exception:
                pass
        self._pre_evade_geometry = ""
        if message:
            self._set_local_msg(message, 5.0)
        self.log.info("共享避让结束，恢复原位置与透明度")

    # ------------------------------------------------------------ 设置面板
    def _open_settings(self) -> None:
        if self.root is None:
            return
        win = self._settings_win
        if win is not None:
            try:
                if win.winfo_exists():
                    win.lift()
                    win.focus_set()
                    return
            except Exception:
                pass
            self._settings_win = None
        try:
            win = tk.Toplevel(self.root)
        except Exception:
            self.log.warning("无法创建设置面板\n%s", traceback.format_exc())
            return
        self._settings_win = win
        win.title("设置 · Meeting Copilot")
        win.configure(bg=C_BG)
        try:
            win.attributes("-topmost", True)
        except tk.TclError:
            pass
        try:
            win.resizable(False, False)
        except tk.TclError:
            pass
        try:
            win.option_add("*TCombobox*Listbox.background", C_PANEL_2)
            win.option_add("*TCombobox*Listbox.foreground", C_TEXT)
            win.option_add("*TCombobox*Listbox.selectBackground", C_ACCENT)
            win.option_add("*TCombobox*Listbox.selectForeground", "#0f1115")
        except Exception:
            pass
        pad = self._px(14)
        box = tk.Frame(win, bg=C_BG, padx=pad, pady=self._px(11))
        box.pack(fill="both", expand=True)
        tk.Label(box, text="浮窗设置", fg=C_TEXT, bg=C_BG,
                 font=self._font(12, "bold"), anchor="w").pack(fill="x", pady=(0, self._px(8)))

        # 输出设备
        self._settings_row_label(box, "输出设备（回环）")
        self._device_combo = ttk.Combobox(box, style="Dark.TCombobox", state="readonly",
                                          height=8, font=self._font(9))
        self._device_combo.pack(fill="x", pady=(0, self._px(7)))
        current_device = str(get(self.cfg, "audio.device", "auto") or "auto")
        device_value = "auto（默认设备）" if current_device in ("", "auto") else current_device
        self._device_combo.configure(values=[device_value])
        self._device_combo.set(device_value)

        # ASR 模型
        self._settings_row_label(box, "ASR 模型（改后需重启生效）")
        asr_combo = ttk.Combobox(box, style="Dark.TCombobox", state="readonly",
                                 values=["small", "large-v3-turbo"], font=self._font(9))
        asr_combo.set(str(get(self.cfg, "asr.model", "small") or "small"))
        asr_combo.pack(fill="x", pady=(0, self._px(7)))

        # 主脑模型
        self._settings_row_label(box, "主脑模型（应答思路 / 纪要）")
        brain_combo = ttk.Combobox(box, style="Dark.TCombobox", state="readonly",
                                   values=["claude-opus-5-5", "claude-sonnet-5"],
                                   font=self._font(9))
        brain_combo.set(str(get(self.cfg, "llm.primary.model", "claude-opus-5-5")
                            or "claude-opus-5-5"))
        brain_combo.pack(fill="x", pady=(0, self._px(7)))

        # 透明度
        alpha_row = tk.Frame(box, bg=C_BG)
        alpha_row.pack(fill="x", pady=(0, self._px(2)))
        tk.Label(alpha_row, text="透明度", fg=C_MUTED, bg=C_BG,
                 font=self._font(9)).pack(side="left")
        alpha_lbl = tk.Label(alpha_row, text=f"{int(self._alpha * 100)}%", fg=C_MUTED,
                             bg=C_BG, font=self._font(9, mono=True))
        alpha_lbl.pack(side="right")
        alpha_slider = _CanvasSlider(
            box, 0.50, 1.0, self._alpha,
            command=lambda v: alpha_lbl.configure(text=f"{int(float(v) * 100)}%"),
            ui=self)
        alpha_slider.pack(fill="x", pady=(0, self._px(9)))

        # 屏幕
        self._settings_row_label(box, "显示器")
        screen_var = tk.StringVar(value=str(get(self.cfg, "ui.screen", "primary")))
        screen_row = tk.Frame(box, bg=C_BG)
        screen_row.pack(fill="x", pady=(0, self._px(7)))
        monitors = self._monitors or [{"primary": True}]
        has_secondary = any(not m.get("primary") for m in monitors)
        for value, text, enabled in (("primary", "主屏", True),
                                     ("secondary", "副屏" + ("" if has_secondary else "（未检测到）"),
                                      has_secondary)):
            rb = tk.Radiobutton(screen_row, text=text, value=value, variable=screen_var,
                                bg=C_BG, fg=C_TEXT if enabled else C_DIM, selectcolor=C_PANEL_2,
                                activebackground=C_BG, activeforeground=C_TEXT,
                                highlightthickness=0, bd=0, font=self._font(9),
                                state="normal" if enabled else "disabled")
            rb.pack(side="left", padx=(0, self._px(12)))

        # 共享避让
        evade_var = tk.BooleanVar(value=bool(get(self.cfg, "ui.evade_on_share", True)))
        cb = tk.Checkbutton(box, text="共享屏幕时自动避让（变淡 + 移到副屏）",
                            variable=evade_var, bg=C_BG, fg=C_TEXT, selectcolor=C_PANEL_2,
                            activebackground=C_BG, activeforeground=C_TEXT,
                            highlightthickness=0, bd=0, font=self._font(9), anchor="w")
        cb.pack(fill="x", pady=(0, self._px(10)))

        tk.Frame(box, bg=C_BORDER, height=max(1, self._px(1))).pack(fill="x",
                                                                    pady=(0, self._px(8)))
        btn_row = tk.Frame(box, bg=C_BG)
        btn_row.pack(fill="x")
        save_btn = tk.Button(btn_row, text="保存", command=lambda: self._save_settings(
            device=self._device_combo.get(), asr_model=asr_combo.get(),
            primary_model=brain_combo.get(), alpha=alpha_slider.get(), screen=screen_var.get(),
            evade=bool(evade_var.get()), win=win),
            bg=C_ACCENT, fg="#0f1115", activebackground="#7ab0ff", activeforeground="#0f1115",
            bd=0, relief="flat", font=self._font(10, "bold"), padx=self._px(22),
            pady=self._px(5), cursor="hand2", highlightthickness=0, takefocus=0)
        save_btn.pack(side="right")
        cancel_btn = tk.Button(btn_row, text="取消", command=lambda: self._close_settings(win),
                               bg=C_BTN, fg=C_TEXT, activebackground=C_BTN_HOVER,
                               activeforeground=C_TEXT, bd=0, relief="flat",
                               font=self._font(10), padx=self._px(14), pady=self._px(5),
                               cursor="hand2", highlightthickness=0, takefocus=0)
        cancel_btn.pack(side="right", padx=(0, self._px(8)))
        self._bind_hover(save_btn, C_ACCENT, "#7ab0ff")
        self._bind_hover(cancel_btn, C_BTN, C_BTN_HOVER)

        # 位置：当前显示器右上角、浮窗下方
        try:
            mon = pick_monitor(self._monitors, str(get(self.cfg, "ui.screen", "primary"))) or {}
            work = mon.get("work", (0, 0, 1920, 1080))
            win.update_idletasks()
            ww = win.winfo_width() if win.winfo_width() > 1 else self._px(400)
            wh = win.winfo_height() if win.winfo_height() > 1 else self._px(430)
            x = max(work[0], work[2] - ww - self._px(24))
            y = work[1] + self._px(96)
            win.geometry(f"+{x}+{y}")
        except Exception:
            pass
        # 异步枚举输出设备（可能 import audio_capture / soundcard，不能在 UI 线程做）
        self._load_devices_async()

    def _settings_row_label(self, parent: tk.Misc, text: str) -> None:
        tk.Label(parent, text=text, fg=C_MUTED, bg=C_BG, font=self._font(9),
                 anchor="w").pack(fill="x", pady=(0, self._px(2)))

    def _load_devices_async(self) -> None:
        def worker() -> None:
            try:
                names, source = list_output_devices_safe(self.log)
            except Exception as e:  # noqa: BLE001
                names, source = ["auto（默认设备）"], f"fallback({e})"
            combined = ["auto（默认设备）"] + [n for n in names if n and n != "默认设备"]
            try:
                self._devices_q.put((combined, source))
            except Exception:
                pass

        threading.Thread(target=worker, name="ui-device-scan", daemon=True).start()

    def _save_settings(self, *, device: str, asr_model: str, primary_model: str,
                       alpha: float, screen: str, evade: bool,
                       win: Optional[tk.Toplevel] = None) -> None:
        device_value = "auto" if str(device).startswith("auto") else str(device)
        kw = {
            "device": device_value,
            "model": str(asr_model or "small"),
            "primary_model": str(primary_model or ""),
            "alpha": float(alpha),
            "screen": str(screen or "primary"),
            "evade_on_share": bool(evade),
        }
        self.apply_settings(**kw)
        if "apply_settings" in self._actions:
            self._call_action("apply_settings", **kw)
        else:
            self.log.info("apply_settings 动作未注册，设置仅本地生效")
        self._close_settings(win)

    def _close_settings(self, win: Optional[tk.Toplevel] = None) -> None:
        target = win or self._settings_win
        self._settings_win = None
        self._device_combo = None  # type: ignore[assignment]
        try:
            if target is not None and target.winfo_exists():
                target.destroy()
        except Exception:
            pass

    # ------------------------------------------------------------ 无障碍调试/截图
    def debug_info(self) -> dict:
        """返回窗口几何/DPI/屏幕信息（自测与故障排查用）。"""
        info: dict[str, Any] = {
            "dpi": self._dpi,
            "dpi_scale": self._dpi_scale,
            "monitors": self._monitors,
            "capture_excluded": self._capture_excluded,
            "evaded": self._evaded,
            "visible": self._visible,
        }
        try:
            w, h, x, y, mon = self._target_geometry()
            info.update({
                "expected": {"x": x, "y": y, "w": w, "h": h},
                "actual": {"x": self.root.winfo_x(), "y": self.root.winfo_y(),
                           "w": self.root.winfo_width(), "h": self.root.winfo_height()},
                "topmost": int(self.root.attributes("-topmost")),
                "alpha": float(self.root.attributes("-alpha")),
                "state": str(self.root.state()),
                "ismapped": bool(self.root.winfo_ismapped()),
                "screen": {"w": self.root.winfo_screenwidth(),
                           "h": self.root.winfo_screenheight()},
                "hwnd": self._hwnd,
                "screen_setting": get(self.cfg, "ui.screen", "primary"),
                "work": mon.get("work") if isinstance(mon, dict) else None,
            })
        except Exception as e:  # noqa: BLE001
            info["error"] = f"{type(e).__name__}: {e}"
        return info

    def take_screenshot(self, path: Optional[str] = None, full: bool = False) -> Optional[str]:
        """截取浮窗区域（selftest 用，需窗口未被 WDA 排除捕获）。"""
        if os.name != "nt" or self.root is None:
            return None
        try:
            x, y = int(self.root.winfo_x()), int(self.root.winfo_y())
            w, h = int(self.root.winfo_width()), int(self.root.winfo_height())
        except Exception:
            return None
        return self._grab_region(x, y, w, h, path=path, full=full)

    def take_screenshot_widget(self, widget: tk.Misc,
                               path: Optional[str] = None) -> Optional[str]:
        """截取任意 Toplevel/Frame 的区域（设置面板截图用）。"""
        if os.name != "nt" or widget is None:
            return None
        try:
            x = int(widget.winfo_rootx())
            y = int(widget.winfo_rooty())
            w = int(widget.winfo_width())
            h = int(widget.winfo_height())
        except Exception:
            return None
        return self._grab_region(x, y, w, h, path=path, full=False)

    def _grab_region(self, x: int, y: int, w: int, h: int,
                     path: Optional[str] = None, full: bool = False) -> Optional[str]:
        try:
            from PIL import ImageGrab
        except Exception as e:  # noqa: BLE001
            self.log.warning("PIL 不可用，无法截图: %s", e)
            return None
        try:
            image = ImageGrab.grab(all_screens=True)
        except Exception as e:  # noqa: BLE001
            self.log.warning("ImageGrab 截图失败: %s", e)
            return None
        try:
            if not full and w > 0 and h > 0:
                vx, vy = 0, 0
                try:
                    user32 = ctypes.windll.user32
                    vx = int(user32.GetSystemMetrics(76))
                    vy = int(user32.GetSystemMetrics(77))
                except Exception:
                    pass
                box = (max(0, x - vx), max(0, y - vy),
                       min(image.width, max(1, x - vx + w)),
                       min(image.height, max(1, y - vy + h)))
                if box[2] > box[0] and box[3] > box[1]:
                    image = image.crop(box)
            if path is None:
                stamp = time.strftime("%Y%m%d_%H%M%S")
                path = str(self._logs_dir / f"ui_{stamp}.png")
            image.save(path)
            return str(path)
        except Exception as e:  # noqa: BLE001
            self.log.warning("保存截图失败: %s", e)
            return None


# ---------------------------------------------------------------- 设备枚举（best-effort）
def list_output_devices_safe(log=None) -> tuple[list[str], str]:
    """尽力枚举输出设备：优先 audio_capture.list_output_devices()，失败回退 soundcard。"""
    names: list[str] = []
    source = ""
    try:
        import audio_capture as ac  # noqa: PLC0415 - 仅在设置面板按需导入
        fn = getattr(ac, "list_output_devices", None)
        if callable(fn):
            names = [str(x) for x in (fn() or [])]
            source = "audio_capture"
        if not names:
            cls = getattr(ac, "LoopbackCapture", None)
            method = getattr(cls, "list_output_devices", None) if cls is not None else None
            if callable(method):
                dummy = type("_Dummy", (), {})()
                names = [str(x) for x in (method(dummy) or [])]
                source = "audio_capture.LoopbackCapture"
    except Exception as e:  # noqa: BLE001
        if log is not None:
            log.info("audio_capture.list_output_devices 不可用（已降级）: %s", e)
    names = list(dict.fromkeys(n for n in names if n))
    if not names:
        try:
            import soundcard as sc  # noqa: PLC0415
            names = [sp.name for sp in sc.all_speakers()]
            source = "soundcard"
        except Exception as e:  # noqa: BLE001
            if log is not None:
                log.info("soundcard 枚举输出设备失败: %s", e)
    names = list(dict.fromkeys(n for n in names if n))
    if not names:
        return ["默认设备"], "fallback"
    return names, source or "unknown"


# ================================================================ 自测
def _selftest(argv: Optional[list[str]] = None) -> int:
    """假数据渲染三类卡片 + 状态条，验证窗口位置/渲染/隐藏恢复，10 秒后自动关闭。"""
    from config import init_console, load_config

    ap = argparse.ArgumentParser(description="Meeting Copilot · 浮窗自测")
    ap.add_argument("--seconds", type=float, default=10.0, help="自动关闭前运行秒数（默认 10）")
    ap.add_argument("--shot-dir", default="", help="截图输出目录（默认 logs/）")
    args = ap.parse_args(argv)
    init_console()
    try:
        cfg = load_config()
    except Exception as e:  # noqa: BLE001
        print(f"[警告] 读取 config.yaml 失败，用内置默认配置: {e}")
        cfg = {"ui": {"size": [452, 780], "alpha": 0.95, "font_family": "Microsoft YaHei UI",
                      "screen": "primary", "evade_on_share": True},
               "hotkeys": {}, "_dirs": {"logs": str(_ROOT / "logs")}}
    # 自测期间关闭共享避让，保证窗口位置/截图稳定（避免用户正好开着腾讯会议时干扰）
    cfg.setdefault("ui", {})["evade_on_share"] = False
    bus = EventBus()
    log = get_logger("ui-selftest")
    checks: list[dict] = []
    shots: list[str] = []

    def check(ok: bool, name: str, detail: str = "") -> None:
        checks.append({"ok": bool(ok), "name": name, "detail": str(detail)})
        print(f"  {'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""), flush=True)

    before_fg = _foreground_hwnd()
    ui = OverlayUI(cfg, bus, log, exclude_from_capture=False)
    dispatched_kw: list[tuple[str, dict]] = []

    def _fake_action(name: str):
        def _cb(**kw: Any) -> None:
            dispatched_kw.append((name, dict(kw)))
        return _cb

    actions = {k: _fake_action(k)
               for k in ("start_listen", "stop_listen", "toggle_listen", "screenshot",
                         "summary", "export", "im_asked", "quit", "apply_settings")}
    ui.register_actions(actions)
    try:
        ui.start()
    except Exception as e:  # noqa: BLE001
        print(f"❌ 浮窗启动失败: {type(e).__name__}: {e}")
        return 1
    print("== 浮窗自测开始（假数据） ==", flush=True)

    state: dict[str, Any] = {"timers": [], "ok_steps": 0}

    def _shot(tag: str) -> None:
        path = ui.take_screenshot(Path(ui._logs_dir) / f"ui_selftest_{tag}.png")
        if path:
            shots.append(path)
            print(f"  📷 截图: {path}", flush=True)
        else:
            print("  ⚠️ 截图失败（不影响自测）", flush=True)

    # --- t=300ms：窗口几何 / DPI / 焦点
    def st_geometry() -> None:
        info = ui.debug_info()
        print(f"  窗口位置: {info.get('actual')} 期望: {info.get('expected')}", flush=True)
        print(f"  DPI={info.get('dpi'):.0f} 缩放={info.get('dpi_scale'):.2f} "
              f"监视器={len(info.get('monitors') or [])} 工作区={info.get('work')}", flush=True)
        exp, act = info.get("expected") or {}, info.get("actual") or {}
        tol = max(4, ui._px(4))
        pos_ok = all(abs(int(act.get(k, 0)) - int(exp.get(k, 0))) <= tol
                     for k in ("x", "y"))
        check(pos_ok, "窗口停靠主屏右上角", f"actual=({act.get('x')},{act.get('y')}) "
                                        f"expected=({exp.get('x')},{exp.get('y')})")
        tol_size = max(2, ui._px(2))
        size_ok = abs(int(act.get("w", 0)) - int(exp.get("w", 0))) <= tol_size and \
                  abs(int(act.get("h", 0)) - int(exp.get("h", 0))) <= tol_size
        check(size_ok, "窗口尺寸正确", f"{act.get('w')}×{act.get('h')} 期望 "
                                    f"{exp.get('w')}×{exp.get('h')}")
        check(bool(info.get("ismapped")), "窗口已真实显示（mapped）")
        check(int(info.get("topmost") or 0) == 1, "置顶属性 (-topmost)")
        check(abs(float(info.get("alpha") or 0) - 0.95) < 0.02, "半透明属性 (-alpha≈0.95)",
              f"{info.get('alpha')}")
        after_fg = _foreground_hwnd()
        print(f"  前台窗口: before={before_fg} after_300ms={after_fg} "
              f"浮窗hwnd={info.get('hwnd')}（焦点回收会在 2 秒内持续重试）", flush=True)
        try:
            share_hit = detect_share_window(info.get("hwnd") or 0)
            check(True, "共享避让检测可用（EnumWindows/Win32）",
                  f"当前检测结果: {share_hit if share_hit else '未检测到共享'}")
        except Exception as e:  # noqa: BLE001
            check(False, "共享避让检测可用（EnumWindows/Win32）", f"{type(e).__name__}: {e}")
        _shot("01_launch")

    # --- t=500ms：注入假数据（字幕 + 流式卡片 + 状态）
    def st_inject() -> None:
        now = time.time()
        segs = [
            TranscriptSegment(seg_id=1, t_start=12.0, t_end=18.0,
                              text="老师：那我们先从图像编辑这块开始，同学你先讲一下这周的进展。"),
            TranscriptSegment(seg_id=2, t_start=18.4, t_end=27.6,
                              text="同学：我这周主要在 RefCOCO 上把掩码动作的实验框架搭起来了，"
                                   "收缩和扩张两个动作现在都能跑通。"),
            TranscriptSegment(seg_id=3, t_start=27.9, t_end=36.0,
                              text="老师：你这个掩码收缩扩张的收益到底有多大？有没有做过对照实验？"),
        ]
        for seg in segs:
            bus.emit_transcript(seg)
        bus.emit_brain(BrainEvent(request_id="hint001", kind=KIND_HINT, lane=LANE_FAST,
                                  delta="老师关注掩码收缩/扩张的定量收益，", done=False))
        bus.emit_brain(BrainEvent(request_id="ans001", kind=KIND_ANSWER, lane=LANE_DEEP,
                                  delta="应答思路：\n1）先直接回答：RefCOCO 验证集上收缩动作 mIoU 约 +2.3，"
                                        "扩张动作约 +1.1；\n", done=False))
        bus.emit_brain(BrainEvent(request_id="slide001", kind=KIND_SLIDE, lane=LANE_DEEP,
                                  delta="屏幕解读：当前页是实验设置与消融结果（Table 2）。", done=False))
        bus.emit_status(Status(listening=True, asr_ready=True,
                               device="扬声器 (Razer BlackShark V2 HS 2.4)",
                               audio_level=0.62, pending_asr=1, last_rtf=0.13,
                               llm_ok=True, kb_ok=True, session_sec=754.0, message=""))
        bus.command("screenshot")  # 模拟 F9 热键：UI 应消费命令并派发给 actions
        _ = now

    # --- t=850ms：追加流式增量（验证同一卡片追加）
    def st_stream_more() -> None:
        bus.emit_brain(BrainEvent(request_id="hint001", kind=KIND_HINT, lane=LANE_FAST,
                                  delta="建议先给两个动作的指标对比（mIoU/CLIP），再说明与 baseline 的差异。",
                                  done=False))
        bus.emit_brain(BrainEvent(request_id="ans001", kind=KIND_ANSWER, lane=LANE_DEEP,
                                  delta="2）再做对照：已经和未加动作的 baseline 做了同数据划分的对齐实验；\n"
                                        "3）最后补一句：完整消融下周三前给出。", done=False))
        bus.emit_brain(BrainEvent(request_id="slide001", kind=KIND_SLIDE, lane=LANE_DEEP,
                                  delta="关键点：掩码比例 0.3~0.7 区间收益最稳定。老师可能问为什么 0.2 "
                                        "以下掉点，答：小掩码信息量不足，模型倾向复制原图。",
                                  done=True,
                                  full_text="屏幕解读：当前页是实验设置与消融结果（Table 2）。"
                                            "关键点：掩码比例 0.3~0.7 区间收益最稳定。老师可能问为什么 0.2 "
                                            "以下掉点，答：小掩码信息量不足，模型倾向复制原图。"))

    # --- t=1400ms：pending 状态检查
    def st_check_pending() -> None:
        t_text = ""
        try:
            t_text = ui.transcript.get("1.0", "end")
        except Exception:
            pass
        check("掩码动作" in t_text and "有没有做过对照实验" in t_text,
              "实时字幕逐条渲染", f"{ui._transcript_count} 条")
        advice_cards = [w for w in ui._card_widgets.values() if not w.card.is_minutes_kind]
        check(len(advice_cards) >= 3, "三类建议卡片已渲染", f"advice={len(advice_cards)}")
        ans_widget = ui._card_widgets.get("ans001")
        pending_text = ""
        try:
            pending_text = ans_widget.body_lbl.get("1.0", "end-1c") if ans_widget else ""
        except Exception:
            pass
        check(bool(ans_widget) and "▌" in pending_text, "流式增量：未完成卡片带光标",
              "同一 request_id 追加到同一卡片")
        think_text = str(ui.think_lbl.cget("text") or "")
        check("思考" in think_text, "状态条「思考中」指示", repr(think_text))
        check("screenshot" in ui._actions_fired, "热键命令经 UI 消费并派发 actions",
              f"已触发: {ui._actions_fired}")
        check(ui._status.listening and ui.state_lbl.cget("text") == "聆听中",
              "状态条聆听状态", str(ui.state_lbl.cget("text")))
        check(ui.timer_lbl.cget("text") == "12:34", "计时显示", str(ui.timer_lbl.cget("text")))
        _shot("02_streaming")

    # --- t=1700ms：完成流式 + 纪要 + 待办
    def st_finish_stream() -> None:
        bus.emit_brain(BrainEvent(request_id="hint001", kind=KIND_HINT, lane=LANE_FAST, done=True,
                                  full_text="老师关注掩码收缩/扩张的定量收益，建议先给两个动作的指标对比"
                                            "（mIoU/CLIP），再说明与 baseline 的差异。"))
        bus.emit_brain(BrainEvent(request_id="ans001", kind=KIND_ANSWER, lane=LANE_DEEP, done=True,
                                  full_text="应答思路：\n1）先直接回答：RefCOCO 验证集上收缩动作 mIoU 约 "
                                            "+2.3，扩张动作约 +1.1；\n2）再做对照：已经和未加动作的 "
                                            "baseline 做了同数据划分的对齐实验；\n3）最后补一句：完整消融"
                                            "下周三前给出。"))
        bus.emit_brain(BrainEvent(
            request_id="sum001", kind=KIND_SUMMARY, lane=LANE_DEEP, done=True,
            full_text="阶段小结：本阶段确认了实验框架与两动作收益；老师要求补充对照实验与人工评估协议。\n"
                      "待办：\n□ 补齐掩码比例 0.2/0.8 的消融实验\n"
                      "□ 和 baseline 做同数据划分的对照\n□ 下周三前定人工评估 protocol"))
        bus.emit_status(Status(listening=True, asr_ready=True,
                               device="扬声器 (Razer BlackShark V2 HS 2.4)",
                               audio_level=0.28, pending_asr=0, last_rtf=0.13,
                               llm_ok=True, kb_ok=True, session_sec=772.0,
                               message="已开始聆听（WASAPI 回环）"))

    # --- t=2600ms：完成态检查
    def st_check_done() -> None:
        ans_widget = ui._card_widgets.get("ans001")
        ans_text = ""
        try:
            ans_text = ans_widget.body_lbl.get("1.0", "end-1c") if ans_widget else ""
        except Exception:
            pass
        check(bool(ans_widget) and "▌" not in ans_text and "baseline" in ans_text,
              "流式完成：光标收起、全文保留")
        titles = []
        for widget in ui._card_widgets.values():
            try:
                titles.append(str(widget.title_lbl.cget("text")))
            except Exception:
                pass
        check("应答思路" in titles and "要点" in titles and "屏幕解读" in titles,
              "三种卡片标题正确", " / ".join(titles))
        check("阶段小结" in titles, "阶段小结卡片渲染", " / ".join(titles))
        check(len(ui._todos) >= 3, "待办清单提取（□ 前缀）", f"{len(ui._todos)} 条: {ui._todos}")
        think_text = str(ui.think_lbl.cget("text") or "")
        check(think_text == "", "无未完成请求时思考指示消失", repr(think_text))
        msg_text = str(ui.msg_lbl.cget("text") or "")
        check("已开始聆听" in msg_text, "状态消息条显示", repr(msg_text))
        fg_now = _foreground_hwnd()
        check(not (fg_now and fg_now == ui._hwnd), "启动未抢占焦点（2.6s 后前台不是浮窗）",
              f"fg_now={fg_now} 浮窗hwnd={ui._hwnd}（WS_EX_NOACTIVATE + 焦点回收）")
        _shot("03_transcript")
        # 三个标签页逐个切换截图（验证渲染效果）
        ui._switch_tab(1)
        state["timers"].append(ui.root.after(220, _st_advice_shot))

    def _st_advice_shot() -> None:
        _shot("04_advice")
        ui._switch_tab(2)
        state["timers"].append(ui.root.after(220, _st_minutes_shot))

    def _st_minutes_shot() -> None:
        _shot("05_minutes")
        ui._switch_tab(0)
        state["timers"].append(ui.root.after(200, _st_hide_test))

    def _st_hide_test() -> None:
        bus.command("hide")  # 走热键命令通路：UI 应自己消费并 withdraw
        state["timers"].append(ui.root.after(350, st_check_hidden))

    def st_check_hidden() -> None:
        try:
            hidden = ui.root.state()
        except Exception:
            hidden = "?"
        check(hidden == "withdrawn" and not ui.is_visible, "F7 隐藏（bus.command 通路）",
              str(hidden))
        bus.command("hide")
        state["timers"].append(ui.root.after(350, _shown))

    def _shown() -> None:
        try:
            shown = ui.root.state()
        except Exception:
            shown = "?"
        check(shown == "normal" and ui.is_visible, "F7 恢复（bus.command 通路）", str(shown))
        _shot("06_restored")

    # --- 拖动 / 右下角拉伸 / 重新停靠
    def st_interaction() -> None:
        try:
            x0, y0 = ui.root.winfo_x(), ui.root.winfo_y()
            ui._drag_start(types.SimpleNamespace(x_root=x0 + 40, y_root=y0 + 10))
            ui._drag_move(types.SimpleNamespace(x_root=x0 - 120, y_root=y0 + 100))
            ui._drag_end()
            ui.root.update()
            moved = (ui.root.winfo_x(), ui.root.winfo_y())
            expect_moved = (x0 - 160, y0 + 90)
            check(abs(moved[0] - expect_moved[0]) <= 2 and abs(moved[1] - expect_moved[1]) <= 2,
                  "拖动 header 可移动浮窗（无边框窗口）", f"{moved} 期望 {expect_moved}")
            wx, wy = ui.root.winfo_x(), ui.root.winfo_y()
            w0, h0 = ui.root.winfo_width(), ui.root.winfo_height()
            ui._resize_start(types.SimpleNamespace(x_root=wx + w0, y_root=wy + h0))
            ui._resize_move(types.SimpleNamespace(x_root=wx + w0 + 100,
                                                  y_root=wy + h0 + 60))
            ui._resize_end()
            ui.root.update()
            new_size = (ui.root.winfo_width(), ui.root.winfo_height())
            check(abs(new_size[0] - (w0 + 100)) <= 4 and abs(new_size[1] - (h0 + 60)) <= 4,
                  "右下角可拉伸调整尺寸", f"{new_size} 期望 {(w0 + 100, h0 + 60)}")
            # 通过 apply_settings(screen) 重新停靠回主屏右上角（恢复默认尺寸）
            ui.apply_settings(screen=str(get(ui.cfg, "ui.screen", "primary")))
            ui.root.update()
            exp = ui._target_geometry()
            check(abs(ui.root.winfo_x() - exp[2]) <= 2 and abs(ui.root.winfo_y() - exp[3]) <= 2,
                  "重新停靠主屏右上角", f"({ui.root.winfo_x()},{ui.root.winfo_y()})")
        except Exception as e:  # noqa: BLE001
            check(False, "拖动/拉伸交互", f"{type(e).__name__}: {e}")

    # --- 设置面板 / 透明度
    def st_settings() -> None:
        try:
            ui._open_settings()
        except Exception as e:  # noqa: BLE001
            check(False, "设置面板创建", f"{type(e).__name__}: {e}")
            return
        state["timers"].append(ui.root.after(900, _check_win))

    def _check_win() -> None:
        win = ui._settings_win
        exists = False
        try:
            exists = win is not None and bool(win.winfo_exists())
        except Exception:
            exists = False
        check(exists, "设置面板（Toplevel 置顶）可打开")
        if exists:
            combo = getattr(ui, "_device_combo", None)
            values = list(combo.cget("values")) if combo is not None else []
            check(len(values) >= 1, "输出设备下拉（audio_capture/soundcard 降级枚举）",
                  f"{len(values)} 项: {values[:4]}")
            shot = ui.take_screenshot_widget(
                win, Path(ui._logs_dir) / "ui_selftest_07_settings.png")
            if shot:
                shots.append(shot)
                print(f"  📷 截图: {shot}", flush=True)
            old_alpha = ui._alpha
            ui.apply_settings(alpha=0.90)
            alpha_ok = abs(float(ui.root.attributes("-alpha")) - 0.90) < 0.02
            ui.apply_settings(alpha=old_alpha)
            check(alpha_ok, "设置保存：透明度本地生效")
            ui._call_action("apply_settings", device="auto", alpha=old_alpha)
            check(any(name == "apply_settings" for name, _kw in dispatched_kw),
                  "apply_settings 转发 actions（带参数）",
                  repr(dispatched_kw[-1][1] if dispatched_kw else {}))
            ui._close_settings(win)
        check("screenshot" in ui._actions_fired, "热键/总线命令派发到 actions",
              f"已触发: {ui._actions_fired}")

    def finish() -> None:
        print("\n== 自测报告 ==", flush=True)
        failed = [c for c in checks if not c["ok"]]
        for c in checks:
            print(f"  {'✅' if c['ok'] else '❌'} {c['name']}"
                  + (f" — {c['detail']}" if c["detail"] else ""), flush=True)
        print(f"\n汇总: {len(checks) - len(failed)}/{len(checks)} 通过", flush=True)
        print(f"截图({len(shots)}): {shots}", flush=True)
        print("窗口将在 1 秒内自动关闭…", flush=True)
        ui.stop()

    # 时间线
    state["timers"].append(ui.root.after(300, st_geometry))
    state["timers"].append(ui.root.after(500, st_inject))
    state["timers"].append(ui.root.after(850, st_stream_more))
    state["timers"].append(ui.root.after(1400, st_check_pending))
    state["timers"].append(ui.root.after(1700, st_finish_stream))
    state["timers"].append(ui.root.after(2600, st_check_done))
    state["timers"].append(ui.root.after(4300, st_interaction))
    state["timers"].append(ui.root.after(5200, st_settings))
    state["timers"].append(ui.root.after(int(max(3.0, args.seconds) * 1000), finish))

    ui.run()
    print("浮窗已关闭。", flush=True)
    failed = [c for c in checks if not c["ok"]]
    if failed:
        print(f"❌ 自测失败: {', '.join(c['name'] for c in failed)}", flush=True)
        return 1
    print("✅ 浮窗自测全部通过。", flush=True)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Meeting Copilot · 置顶浮窗")
    ap.add_argument("--selftest", action="store_true", help="假数据渲染自测（10 秒后自动关闭）")
    args, rest = ap.parse_known_args(argv)
    if args.selftest:
        return _selftest(rest)
    print("用法: python ui_overlay.py --selftest [--seconds 10]")
    print("模块用法: from ui_overlay import OverlayUI")
    return 0


if __name__ == "__main__":
    sys.exit(main())
