# -*- coding: utf-8 -*-
"""Meeting Copilot · 可自由缩放的双栏桌面界面。

保留 OverlayUI(cfg, bus, log) 及 register_actions/start/run/stop 契约。
使用 Windows 原生标题栏、边框、最大化与 Snap；内容区使用可拖动 PanedWindow。
"""
from __future__ import annotations

import json
import os
import queue
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

from config import get
from contracts import (KIND_ANSWER, KIND_FREE_QA, KIND_HINT, KIND_MINUTES, KIND_SCRIPT,
                       KIND_SLIDE, KIND_SUMMARY, KIND_TERM, BrainEvent, EventBus, Status,
                       TranscriptSegment, fmt_ts)
from logging_setup import get_logger
from scenarios import normalize_scenario, scenario_label
from ui_overlay import (delay_text, enable_dpi_awareness, iter_monitors,
                        list_output_devices_safe, pick_monitor, short_device, status_text)

# Apple 式浅色工作台：浅灰背景、白色内容卡片、系统蓝主操作。
C_BG = "#F5F5F7"
C_PANEL = "#FFFFFF"
C_CARD = "#FFFFFF"
C_ANSW_BG = C_PANEL
C_READ_BG = C_PANEL
C_ASK_BG = "#F7F7F9"
C_BORDER = "#E2E2E7"
C_TEXT = "#1D1D1F"
C_MUTED = "#6E6E73"
C_DIM = "#8E8E93"
C_ACCENT = "#007AFF"
C_ACCENT_HOVER = "#3395FF"
C_ACCENT_SOFT = "#E8F1FF"
C_ACCENT_SOFT_HOVER = "#D6E7FF"
C_WARM = "#B25000"
C_WARM_SOFT = "#FFF1DE"
C_WARM_HOVER = "#F5DFC0"
C_READ = "#248A3D"
C_OK = "#34C759"
C_ERR = "#D70015"
C_ERR_SOFT = "#FFE9E7"
C_TS = "#8E8E93"
C_BTN = "#E9E9EE"
C_BTN_HOVER = "#DFDFE5"
C_SELECT = "#B9D9FF"
C_SHADOW = "#ECECF1"


def _rounded_points(width: float, height: float, radius: float) -> list[float]:
    """返回 Canvas 平滑多边形顶点；无需第三方圆角控件依赖。"""
    w, h = max(2.0, width - 1), max(2.0, height - 1)
    r = max(1.0, min(radius, w / 2, h / 2))
    return [r, 0, w-r, 0, w, 0, w, r, w, h-r, w, h,
            w-r, h, r, h, 0, h, 0, h-r, 0, r, 0, 0]


class RoundedButton(tk.Canvas):
    """保留 Button 常用接口的轻量圆角按钮。"""

    def __init__(self, parent, *, text, command, font, bg, fg, hover,
                 padx: int, pady: int, radius: int):
        self._text = str(text)
        self._command = command
        self._font = font
        self._mc_bg = bg
        self._mc_fg = fg
        self._mc_hover = hover
        self._mc_active_fg = fg
        self._mc_border = bg
        self._focus_color = C_ACCENT
        self._disabled_fg = C_DIM
        self._padx = int(padx)
        self._pady = int(pady)
        self._radius = int(radius)
        self._width_chars = 0
        self._state = "normal"
        self._hovered = False
        super().__init__(parent, bg=parent.cget("bg"), bd=0, highlightthickness=0,
                         takefocus=0, cursor="hand2")
        self._shape = self.create_polygon([0, 0], smooth=True, splinesteps=24)
        self._caption = self.create_text(0, 0, text=self._text, font=self._font)
        self.bind("<ButtonRelease-1>", self._activate)
        self.bind("<Return>", self._activate)
        self.bind("<space>", self._activate)
        self.bind("<FocusIn>", lambda _e: self._draw(self.winfo_width(), self.winfo_height()))
        self.bind("<FocusOut>", lambda _e: self._draw(self.winfo_width(), self.winfo_height()))
        self.bind("<Configure>", lambda e: self._draw(e.width, e.height), add="+")
        self._redraw()

    def _measure(self) -> tuple[int, int]:
        font = tkfont.Font(master=self, font=self._font)
        text_width = font.measure(self._text)
        if self._width_chars:
            text_width = max(text_width, font.measure("0" * self._width_chars))
        return (text_width + self._padx * 2 + 4,
                font.metrics("linespace") + self._pady * 2 + 4)

    def _draw(self, width: int, height: int) -> None:
        if width <= 2 or height <= 2:
            return
        hovered = self._hovered and self._state == "normal"
        fill = self._mc_hover if hovered else self._mc_bg
        fg = self._disabled_fg if self._state == "disabled" else (
            self._mc_active_fg if hovered else self._mc_fg)
        outline = self._focus_color if self.focus_get() is self else self._mc_border
        self.coords(self._shape, _rounded_points(width, height, self._radius))
        self.itemconfigure(self._shape, fill=fill, outline=outline, width=1)
        self.coords(self._caption, width / 2, height / 2)
        self.itemconfigure(self._caption, text=self._text, font=self._font, fill=fg)

    def _redraw(self) -> None:
        width, height = self._measure()
        super().configure(width=width, height=height,
                          cursor="hand2" if self._state == "normal" else "arrow")
        self._draw(width, height)

    def set_hover(self, active: bool) -> None:
        self._hovered = bool(active)
        self._draw(self.winfo_width(), self.winfo_height())

    def _activate(self, event=None):
        if self._state != "normal":
            return "break"
        if event is not None and hasattr(event, "x"):
            if not (0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height()):
                return "break"
        if callable(self._command):
            self._command()
        return "break"

    def invoke(self):
        """兼容 tk.Button.invoke，便于测试与外部控制。"""
        if self._state == "normal" and callable(self._command):
            return self._command()
        return None

    def configure(self, cnf=None, **kwargs):
        if isinstance(cnf, str) and not kwargs:
            return self.cget(cnf)
        options = dict(cnf or {})
        options.update(kwargs)
        mapping = {
            "text": "_text", "command": "_command", "font": "_font",
            "bg": "_mc_bg", "fg": "_mc_fg", "activebackground": "_mc_hover",
            "activeforeground": "_mc_active_fg", "highlightbackground": "_mc_border",
            "highlightcolor": "_focus_color", "disabledforeground": "_disabled_fg",
        }
        for key, value in list(options.items()):
            if key in mapping:
                setattr(self, mapping[key], value)
                options.pop(key)
        if "state" in options:
            self._state = str(options.pop("state"))
        if "width" in options:
            self._width_chars = int(options.pop("width"))
        if options:
            super().configure(**options)
        self._redraw()
        return None

    config = configure

    def cget(self, key):
        values = {
            "text": self._text, "font": self._font, "bg": self._mc_bg,
            "fg": self._mc_fg, "activebackground": self._mc_hover,
            "activeforeground": self._mc_active_fg, "highlightbackground": self._mc_border,
            "disabledforeground": self._disabled_fg, "state": self._state,
            "width": self._width_chars,
        }
        return values[key] if key in values else super().cget(key)


class RoundedPanel(tk.Frame):
    """带圆角背景的 Frame；子控件仍使用原生 pack / grid。"""

    def __init__(self, parent, *, bg=C_PANEL, radius=12, border=C_BORDER,
                 highlightbackground=None, highlightthickness=1, shadow=False, **_kwargs):
        self._panel_bg = bg
        self._radius = radius
        self._border = highlightbackground or border
        self._border_width = int(highlightthickness)
        super().__init__(parent, bg=parent.cget("bg"), bd=0, highlightthickness=0)
        self._surface = tk.Canvas(self, bg=parent.cget("bg"), bd=0, highlightthickness=0)
        self._shadow = (self._surface.create_polygon([0, 0], smooth=True, splinesteps=32)
                        if shadow else None)
        self._shape = self._surface.create_polygon([0, 0], smooth=True, splinesteps=32)
        self._surface.place(x=0, y=0, relwidth=1, relheight=1)
        # 背景最先创建，后续子控件自然位于其上层。
        self._surface.bind("<Configure>", lambda e: self._draw_panel(e.width, e.height))

    def _draw_panel(self, width: int, height: int) -> None:
        if width <= 2 or height <= 2:
            return
        if self._shadow is not None:
            points = _rounded_points(width, max(2, height - 2), self._radius)
            points[1::2] = [y + 2 for y in points[1::2]]
            self._surface.coords(self._shadow, points)
            self._surface.itemconfigure(self._shadow, fill=C_SHADOW, outline=C_SHADOW)
        self._surface.coords(self._shape, _rounded_points(width, height, self._radius))
        self._surface.itemconfigure(
            self._shape, fill=self._panel_bg,
            outline=self._border if self._border_width else self._panel_bg,
            width=max(1, self._border_width))

    def configure(self, cnf=None, **kwargs):
        if isinstance(cnf, str) and not kwargs:
            return self.cget(cnf)
        options = dict(cnf or {})
        options.update(kwargs)
        if "bg" in options:
            self._panel_bg = options.pop("bg")
        if "highlightbackground" in options:
            self._border = options.pop("highlightbackground")
        if "highlightthickness" in options:
            self._border_width = int(options.pop("highlightthickness"))
        if options:
            super().configure(**options)
        self._draw_panel(self._surface.winfo_width(), self._surface.winfo_height())
        return None

    config = configure

    def cget(self, key):
        if key == "bg":
            return self._panel_bg
        if key == "highlightbackground":
            return self._border
        if key == "highlightthickness":
            return self._border_width
        return super().cget(key)


class OverlayUI:
    """原生可缩放窗口；start() 建窗，run() 进入 Tk 主循环。"""

    POLL_MS = 70
    MAX_TRANSCRIPT_LINES = 800
    MAX_ANSWER_HISTORY = 20
    ECHO_WINDOW_S = 0.25
    BURST_LIMIT = 6

    def __init__(self, cfg: dict, bus: EventBus, log=None, *, exclude_from_capture: bool = True):
        self.cfg = cfg if isinstance(cfg, dict) else {}
        self.bus = bus
        self._log = log or get_logger("ui-split")
        if os.environ.get("MC_DISABLE_CAPTURE_EXCLUDE", "").lower() in ("1", "true", "yes"):
            exclude_from_capture = False
        self._exclude_from_capture = bool(exclude_from_capture)
        enable_dpi_awareness()
        self.root: Optional[tk.Tk] = None
        self._started = self._closed = self._visible = False
        self._scale = float(get(self.cfg, "ui.split_scale", 1.0) or 1.0)
        self._font_scale = float(get(self.cfg, "ui.font_scale", 100) or 100) / 100.0
        self._actions: dict[str, Callable[..., Any]] = {}
        self._btn: dict[str, RoundedButton] = {}
        self._compact_commands = False
        self._dispatch_ts: dict[str, float] = {}
        self._dispatch_burst: dict[str, list[float]] = {}
        self._click_through = False
        self._hwnd = 0
        self._status = Status()
        self._listening = False
        self._session_sec = 0.0
        self._delay = 0.0
        self._device = ""
        self._local_msg = ""
        self._local_msg_until = 0.0
        self._answer = ""
        self._answer_done = False
        self._script = ""
        self._script_done = False
        self._answer_request_id = ""
        self._answer_history: list[dict] = []
        self._history_index: Optional[int] = None
        self._frozen = False
        self._frozen_view: Optional[dict] = None
        self._history_status_lbl: Optional[tk.Label] = None
        self._hints: list[list] = []
        self._settings_win: Optional[tk.Toplevel] = None
        self._settings_save: Optional[Callable[[], None]] = None
        self._poll_job = None
        self._save_job = None
        self._state_path = ROOT / "data" / "ui_state.json"
        self._follow_var: Optional[tk.BooleanVar] = None
        self._qa_visible = False
        self._qa_turns: dict[str, dict] = {}
        self._qa_order: list[str] = []
        self._qa_waiting: list[str] = []
        self._qa_alias: dict[str, str] = {}
        self._qa_counter = 0
        self.question_entry: Optional[tk.Text] = None
        self._tk_texts: list[tk.Text] = []
        self._transcript_started = False

    @property
    def log(self):
        return self._log

    def _px(self, value: float) -> int:
        return max(1, int(round(float(value) * self._scale)))

    def _font(self, size: int, weight: str = "normal", mono: bool = False) -> tuple:
        family = "Consolas" if mono else str(get(self.cfg, "ui.font_family", "Microsoft YaHei UI"))
        return (family, max(8, int(round(size * self._font_scale))), weight)

    # ------------------------------------------------------------ 原生窗口与状态
    def _target_geometry(self) -> tuple[int, int, int, int]:
        mon = pick_monitor(iter_monitors(self.root), str(get(self.cfg, "ui.screen", "secondary")))
        if mon is None:
            return 60, 60, 1200, 800
        left, top, right, bottom = [int(v) for v in mon["work"]]
        try:
            w, h = [int(v) for v in (get(self.cfg, "ui.split_size", [1280, 860]) or [1280, 860])]
        except Exception:
            w, h = 1280, 860
        w = max(760, min(w, right - left - 24))
        h = max(520, min(h, bottom - top - 24))
        return left + (right - left - w) // 2, top + (bottom - top - h) // 2, w, h

    def _load_ui_state(self) -> dict:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _apply_geometry(self, prefer_saved: bool = True) -> None:
        if self.root is None:
            return
        saved = self._load_ui_state().get("geometry", "") if prefer_saved else ""
        if isinstance(saved, str) and "x" in saved:
            try:
                self.root.state("normal")
                self.root.geometry(saved)
                return
            except Exception:
                pass
        x, y, w, h = self._target_geometry()
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _on_configure(self, event) -> None:
        if self.root is None or event.widget is not self.root or self._closed:
            return
        if self._save_job is not None:
            try:
                self.root.after_cancel(self._save_job)
            except Exception:
                pass
        self._save_job = self.root.after(700, self._save_ui_state)
        if "ask" in self._btn:
            compact = self.root.winfo_width() < 940
            if compact != self._compact_commands:
                self._compact_commands = compact
                self._refresh_command_labels()
        self.root.after_idle(self._follow_after_resize)

    def _follow_after_resize(self) -> None:
        """最大化或拖动窗口后，跟随模式仍停在最新内容。"""
        if self.root is None or self._closed:
            return
        try:
            if hasattr(self, "transcript") and self._follow_var and self._follow_var.get():
                self.transcript.see("end")
            if self._qa_visible and hasattr(self, "qa_box"):
                self._scroll_qa_to_latest()
        except tk.TclError:
            pass

    def _save_ui_state(self) -> None:
        self._save_job = None
        if self.root is None or self._closed:
            return
        try:
            maximized = self.root.state() == "zoomed"
            data = self._load_ui_state()
            data["maximized"] = maximized
            if not maximized and self.root.winfo_width() > 100:
                data["geometry"] = self.root.geometry()
                data["main_sash"] = list(self.main_pane.sash_coord(0))
            data["font_scale"] = int(round(self._font_scale * 100))
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._state_path)
        except Exception:
            pass

    def _get_hwnd(self) -> int:
        if self._hwnd or self.root is None:
            return self._hwnd
        try:
            import ctypes
            hwnd = int(self.root.winfo_id())
            self._hwnd = int(ctypes.windll.user32.GetParent(hwnd)) or hwnd
        except Exception:
            self._hwnd = 0
        return self._hwnd

    def _apply_display_affinity(self) -> None:
        if not self._exclude_from_capture:
            return
        try:
            import ctypes
            hwnd = self._get_hwnd()
            if hwnd:
                ctypes.windll.user32.SetWindowDisplayAffinity(hwnd, 0x11)
        except Exception:
            pass

    def _apply_size_mode(self, mode: str) -> None:
        if self.root is None:
            return
        try:
            if mode == "full":
                self.root.state("zoomed")
                return
            self.root.state("normal")
            mon = pick_monitor(iter_monitors(self.root), str(get(self.cfg, "ui.screen", "secondary")))
            if mon is None:
                return
            left, top, right, bottom = [int(v) for v in mon["work"]]
            ratio = 2 / 3 if mode == "2/3" else 0.5
            w = max(760, int((right - left) * ratio))
            h = max(520, int((bottom - top) * ratio))
            self.root.geometry(f"{w}x{h}+{left + (right-left-w)//2}+{top + (bottom-top-h)//2}")
        except Exception:
            pass

    # ------------------------------------------------------------ 构建
    def start(self) -> None:
        if self._started:
            return
        self.root = tk.Tk()
        self.root.withdraw()
        app_name = str(get(self.cfg, "app.name", "Meeting Copilot") or "Meeting Copilot")
        self.root.title(f"{app_name} · {scenario_label(self.cfg)}")
        self.root.configure(bg=C_BG)
        self.root.minsize(760, 500)
        self.root.resizable(True, True)
        self.root.protocol("WM_DELETE_WINDOW", self._on_click_quit)
        self.root.bind("<Configure>", self._on_configure, add="+")
        self.root.bind("<F2>", lambda _e: self.toggle_ask_panel())
        self.root.bind("<F3>", lambda _e: self.toggle_freeze())
        self.root.bind("<Control-Left>", lambda _e: (self._navigate_history(-1), "break")[1])
        self.root.bind("<Control-Right>", lambda _e: (self._navigate_history(1), "break")[1])
        self.root.bind("<Control-c>", self._copy_focused, add="+")
        try:
            self.root.attributes("-topmost", bool(get(self.cfg, "ui.split_topmost", False)))
            self.root.attributes("-alpha", float(get(self.cfg, "ui.alpha", 1.0) or 1.0))
        except tk.TclError:
            pass
        self._build_ui()
        self._apply_geometry()
        self.root.deiconify()
        self.root.update_idletasks()
        self._restore_sash()
        self._apply_display_affinity()
        self._refresh_action_states()
        self._visible = self._started = True
        saved = self._load_ui_state()
        if bool(get(self.cfg, "ui.split_fullscreen", False)) or saved.get("maximized"):
            self.root.after(80, lambda: self.root and self.root.state("zoomed"))
        self.log.info("原生双栏界面已启动 geometry=%s", self.root.geometry())
        self._poll()

    def _restore_sash(self) -> None:
        try:
            pos = self._load_ui_state().get("main_sash")
            if pos:
                self.main_pane.sash_place(0, int(pos[0]), int(pos[1]))
        except Exception:
            pass

    def _build_ui(self) -> None:
        assert self.root is not None
        self._configure_ttk()
        head = tk.Frame(self.root, bg=C_BG)
        head.pack(fill="x", padx=self._px(24), pady=(self._px(20), 0))

        brand = tk.Frame(head, bg=C_BG)
        brand.pack(side="left")
        size = self._px(38)
        mark = tk.Canvas(brand, width=size, height=size, bg=C_BG,
                         bd=0, highlightthickness=0)
        mark.pack(side="left", padx=(0, self._px(12)))
        mark.create_oval(1, 1, size-1, size-1, fill=C_ACCENT_SOFT, outline="")
        for x, top, bottom in ((0.29, 0.38, 0.62), (0.41, 0.28, 0.72),
                               (0.53, 0.20, 0.80), (0.65, 0.29, 0.71), (0.77, 0.39, 0.61)):
            mark.create_line(size*x, size*top, size*x, size*bottom,
                             fill=C_ACCENT, width=max(2, self._px(2)), capstyle="round")
        title = tk.Frame(brand, bg=C_BG)
        title.pack(side="left")
        app_name = str(get(self.cfg, "app.name", "Meeting Copilot") or "Meeting Copilot")
        self.app_title_lbl = tk.Label(title, text=app_name,
                                      bg=C_BG, fg=C_TEXT, font=self._font(16, "bold"))
        self.app_title_lbl.pack(anchor="w")

        utilities = tk.Frame(head, bg=C_BG)
        utilities.pack(side="right", pady=self._px(2))
        self._btn["settings"] = self._button(
            utilities, "设置", self._open_settings, compact=True)
        self._btn["hide"] = self._button(
            utilities, "隐藏", self.toggle_visible, compact=True)
        self._btn["quit"] = self._button(
            utilities, "退出", self._on_click_quit, danger=True, compact=True)

        status = RoundedPanel(head, bg=C_PANEL, radius=10)
        status.pack(side="right", padx=(0, self._px(10)), pady=self._px(2))
        self.dot = tk.Label(status, text="●", bg=C_PANEL, fg=C_DIM, font=self._font(7))
        self.dot.pack(side="left", padx=(self._px(11), self._px(5)), pady=self._px(7))
        self.state_lbl = tk.Label(status, text="待机", bg=C_PANEL, fg=C_MUTED, font=self._font(9))
        self.state_lbl.pack(side="left", pady=self._px(7))
        self.timer_lbl = tk.Label(status, text="00:00", bg=C_PANEL, fg=C_TEXT,
                                  font=self._font(10, mono=True))
        self.timer_lbl.pack(side="left", padx=(self._px(12), self._px(11)), pady=self._px(7))

        toolbar = RoundedPanel(self.root, bg=C_PANEL, radius=14, border=C_BORDER)
        toolbar.pack(fill="x", padx=self._px(20), pady=(self._px(18), self._px(14)))
        tools = tk.Frame(toolbar, bg=C_PANEL)
        tools.pack(fill="x", padx=self._px(6), pady=self._px(5))
        commands = (
            ("listen", "▶ 开始聆听  F8", self._on_click_listen),
            ("capture", "看屏", lambda: self._click_action("screenshot")),
            ("script", "照读稿", lambda: self._click_action("script")),
            ("im_asked", "被点名", lambda: self._click_action("im_asked")),
            ("summary", "小结", lambda: self._click_action("summary")),
            ("export", "导出纪要", lambda: self._click_action("export")),
            ("ask", "自由提问  F2", self.toggle_ask_panel),
        )
        for key, label, command in commands:
            self._btn[key] = self._button(
                tools, label, command, accent=key == "listen",
                side="right" if key == "ask" else "left")
            self._btn[key].pack_configure(padx=(0, self._px(3)))
        tk.Frame(tools, bg=C_BORDER, width=1).pack(
            side="right", fill="y", padx=self._px(7), pady=self._px(8))
        self._set_button_palette(self._btn["ask"], C_PANEL, C_ACCENT, C_ACCENT_SOFT)

        bottom = tk.Frame(self.root, bg=C_BG)
        bottom.pack(side="bottom", fill="x", padx=self._px(22), pady=(self._px(3), self._px(10)))
        self.msg_lbl = tk.Label(bottom, text="", bg=C_BG, fg=C_WARM, font=self._font(9), anchor="w")
        self.msg_lbl.pack(side="left", fill="x", expand=True)
        self.delay_lbl = tk.Label(bottom, text="", bg=C_BG, fg=C_DIM, font=self._font(9))
        self.delay_lbl.pack(side="right", padx=(self._px(12), 0))
        self.dev_lbl = tk.Label(bottom, text="默认设备", bg=C_BG, fg=C_DIM, font=self._font(9))
        self.dev_lbl.pack(side="right")

        self.workspace = tk.PanedWindow(self.root, orient="vertical", bg=C_BG, bd=0,
                                        sashwidth=8, sashrelief="flat", relief="flat",
                                        opaqueresize=True, showhandle=False)
        self.workspace.pack(fill="both", expand=True, padx=self._px(20), pady=(0, self._px(5)))
        self.main_pane = tk.PanedWindow(self.workspace, orient="horizontal", bg=C_BG, bd=0,
                                        sashwidth=10, sashrelief="flat", relief="flat",
                                        opaqueresize=True, showhandle=False)
        self.workspace.add(self.main_pane, stretch="always")

        left = self._panel(self.main_pane)
        transcript_head = self._section_head(left, "实时字幕", "讨论记录 · 自动转写", C_ACCENT)
        self._follow_var = tk.BooleanVar(value=True)
        self._check(transcript_head, "自动跟随", self._follow_var).pack(side="right")
        transcript_wrap = tk.Frame(left, bg=C_PANEL)
        transcript_wrap.pack(fill="both", expand=True, padx=self._px(8), pady=(0, self._px(8)))
        self.transcript = self._text(transcript_wrap, int(get(self.cfg, "ui.font_transcript", 16)))
        self.transcript.pack(side="left", fill="both", expand=True)
        self._scrollbar(transcript_wrap, self.transcript).pack(side="right", fill="y", pady=self._px(8))
        self.transcript.tag_configure("ts", foreground=C_TS, font=self._font(10, mono=True))
        self.transcript.tag_configure("body", foreground=C_TEXT)
        self.transcript.tag_configure("uncertain", foreground=C_MUTED)
        self.transcript.tag_configure("partial", foreground=C_DIM)
        self._set_text(self.transcript, "等待声音…\n\n播放会议声音后，实时字幕会显示在这里。\n")
        self.main_pane.add(left, minsize=280, stretch="always", width=650)

        right = tk.Frame(self.main_pane, bg=C_BG)
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(0, weight=5)
        right.grid_rowconfigure(1, weight=6)
        answer_wrap = self._card(right, "应答思路", "先结论，再依据", C_WARM, C_ANSW_BG,
                                 lambda: self._copy_text(self._displayed_text("answer")))
        answer_wrap.grid(row=0, column=0, sticky="nsew", pady=(0, self._px(5)))
        answer_nav = tk.Frame(answer_wrap, bg=C_ANSW_BG)
        answer_nav.pack(fill="x", padx=self._px(14), pady=(self._px(2), 0))
        self._btn["history_prev"] = self._button(
            answer_nav, "‹", lambda: self._navigate_history(-1), compact=True)
        self._btn["history_next"] = self._button(
            answer_nav, "›", lambda: self._navigate_history(1), compact=True)
        for key in ("history_prev", "history_next"):
            self._btn[key].configure(font=self._font(13), width=2)
        self._btn["freeze"] = self._button(
            answer_nav, "定格  F3", self.toggle_freeze, side="right", compact=True)
        self._history_status_lbl = tk.Label(
            answer_nav, text="最新", bg=C_ANSW_BG, fg=C_DIM, font=self._font(9))
        self._history_status_lbl.pack(side="left", padx=self._px(5))
        self.answer_box = self._text(answer_wrap, int(get(self.cfg, "ui.font_answer", 14)))
        self.answer_box.pack(fill="both", expand=True, padx=self._px(7), pady=(0, self._px(7)))
        self._set_text(self.answer_box, "被提问或点击“被点名”后，这里显示可直接使用的回答要点。")
        script_wrap = self._card(right, "照读稿", "自然口语 · 可直接照着念", C_READ, C_READ_BG,
                                 lambda: self._copy_text(self._displayed_text("script")))
        script_wrap.grid(row=1, column=0, sticky="nsew", pady=(self._px(5), 0))
        self.script_box = self._text(script_wrap, int(get(self.cfg, "ui.font_script", 17)))
        self.script_box.pack(fill="both", expand=True, padx=self._px(7), pady=(0, self._px(7)))
        self._set_text(self.script_box, "需要发言时按 F5，或与应答思路一起自动生成。")
        self.main_pane.add(right, minsize=340, stretch="always", width=560)

        tk.Frame(left, bg=C_BORDER, height=1).pack(fill="x", padx=self._px(16))
        self.hints_wrap = tk.Frame(left, bg=C_PANEL)
        self.hints_wrap.pack(fill="x", padx=self._px(17), pady=(self._px(6), self._px(10)))
        self.hint_labels: list[tk.Label] = []
        for _ in range(2):
            lbl = tk.Label(self.hints_wrap, text="", bg=C_PANEL, fg=C_MUTED,
                           font=self._font(9), anchor="w", justify="left", wraplength=500)
            lbl.pack(fill="x", pady=self._px(2))
            self.hint_labels.append(lbl)
        left.bind("<Configure>", lambda _e: self._relayout_wrap(), add="+")
        self._build_ask_panel()

    def _configure_ttk(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("MeetingCopilot.Vertical.TScrollbar", width=10,
                        background=C_BTN, troughcolor=C_PANEL, bordercolor=C_PANEL,
                        darkcolor=C_PANEL, lightcolor=C_PANEL, arrowcolor=C_MUTED)
        style.map("MeetingCopilot.Vertical.TScrollbar",
                  background=[("active", C_BTN_HOVER), ("pressed", C_BTN_HOVER)])
        style.configure("MeetingCopilot.TCombobox", fieldbackground=C_ASK_BG,
                        background=C_BTN, foreground=C_TEXT, arrowcolor=C_MUTED,
                        bordercolor=C_BORDER, darkcolor=C_BORDER, lightcolor=C_BORDER,
                        padding=5)
        style.map("MeetingCopilot.TCombobox",
                  fieldbackground=[("readonly", C_ASK_BG)],
                  foreground=[("readonly", C_TEXT)],
                  bordercolor=[("focus", C_ACCENT)])
        self.root.option_add("*TCombobox*Listbox.background", C_CARD)
        self.root.option_add("*TCombobox*Listbox.foreground", C_TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", C_SELECT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", C_TEXT)

    def _panel(self, parent) -> tk.Frame:
        return RoundedPanel(parent, bg=C_PANEL, radius=16, shadow=True)

    def _section_head(self, parent, title: str, subtitle: str, color: str) -> tk.Frame:
        row = tk.Frame(parent, bg=C_PANEL)
        row.pack(fill="x", padx=self._px(18), pady=(self._px(16), self._px(6)))
        tk.Label(row, text=title, bg=C_PANEL, fg=C_TEXT,
                 font=self._font(12, "bold")).pack(side="left")
        return row

    def _card(self, parent, title: str, subtitle: str, color: str, bg: str, copy_cmd) -> tk.Frame:
        wrap = RoundedPanel(parent, bg=bg, radius=16, shadow=True)
        row = tk.Frame(wrap, bg=bg)
        row.pack(fill="x", padx=self._px(16), pady=(self._px(13), self._px(3)))
        tk.Label(row, text=title, bg=bg, fg=C_TEXT,
                 font=self._font(12, "bold")).pack(side="left")
        self._button(row, "复制", copy_cmd, side="right", compact=True)
        return wrap

    def _text(self, parent, size: int) -> tk.Text:
        txt = tk.Text(parent, bg=parent.cget("bg"), fg=C_TEXT, relief="flat", bd=0,
                      highlightthickness=0, wrap="char", undo=False, cursor="arrow",
                      font=self._font(size), width=1, height=4, padx=self._px(10),
                      pady=self._px(9), spacing1=self._px(4), spacing3=self._px(7),
                      selectbackground=C_SELECT, inactiveselectbackground=C_SELECT,
                      state="disabled")
        txt.bind("<MouseWheel>", lambda e: (txt.yview_scroll(int(-e.delta / 120), "units"), "break")[1])
        self._tk_texts.append(txt)
        return txt

    @staticmethod
    def _scrollbar(parent, widget) -> ttk.Scrollbar:
        bar = ttk.Scrollbar(parent, orient="vertical", command=widget.yview,
                            style="MeetingCopilot.Vertical.TScrollbar")
        widget.configure(yscrollcommand=bar.set)
        return bar

    @staticmethod
    def _set_button_palette(btn: RoundedButton, bg: str, fg: str, hover: str) -> None:
        btn._mc_bg = bg
        btn._mc_fg = fg
        btn._mc_hover = hover
        btn.configure(bg=bg, fg=fg, activebackground=hover, activeforeground=fg,
                      highlightbackground=bg)

    def _button(self, parent, text, command, *, accent=False, danger=False, side="left", compact=False):
        bg = C_ACCENT if accent else str(parent.cget("bg"))
        fg = "#FFFFFF" if accent else (C_ERR if danger else (C_MUTED if compact else C_TEXT))
        hover = C_ACCENT_HOVER if accent else (C_ERR_SOFT if danger else C_BTN_HOVER)
        btn = RoundedButton(
            parent, text=text, command=command, bg=bg, fg=fg, hover=hover,
            font=self._font(9 if compact else 10, "bold" if accent else "normal"),
            padx=self._px(9 if compact else 14), pady=self._px(5 if compact else 8),
            radius=9 if compact else 11)
        self._set_button_palette(btn, bg, fg, hover)
        if side is not None:
            btn.pack(side=side, padx=self._px(2))
        btn.bind("<Enter>", lambda _e, b=btn: b.set_hover(str(b.cget("state")) == "normal"), add="+")
        btn.bind("<Leave>", lambda _e, b=btn: b.set_hover(False), add="+")
        return btn

    def _check(self, parent, text, variable, command=None) -> tk.Checkbutton:
        return tk.Checkbutton(parent, text=text, variable=variable, command=command,
                              bg=parent.cget("bg"), fg=C_MUTED, selectcolor=C_BG,
                              activebackground=parent.cget("bg"), activeforeground=C_TEXT,
                              highlightthickness=0, bd=0, font=self._font(9), cursor="hand2")

    # ------------------------------------------------------------ 自由提问会话
    def _build_ask_panel(self) -> None:
        self.qa_frame = RoundedPanel(self.workspace, bg=C_PANEL, radius=16, shadow=True)
        head = tk.Frame(self.qa_frame, bg=C_PANEL)
        head.pack(fill="x", padx=self._px(16), pady=(self._px(13), self._px(9)))
        tk.Label(head, text="自由提问", bg=C_PANEL, fg=C_TEXT,
                 font=self._font(12, "bold")).pack(side="left")
        self._button(head, "收起", self.toggle_ask_panel, side="right", compact=True)
        self._button(head, "清空", self._clear_qa, side="right", compact=True)
        self._button(head, "复制", self._copy_latest_qa, side="right", compact=True)
        raw_memory = get(self.cfg, "brain.free_qa_memory", "smart")
        self.qa_memory_var = tk.BooleanVar(value=raw_memory not in (False, "off", "false", "0"))
        self._check(head, "相关记忆", self.qa_memory_var,
                    self._toggle_qa_memory).pack(side="right", padx=self._px(8))

        body = RoundedPanel(self.qa_frame, bg=C_ASK_BG, radius=10)
        body.pack(fill="both", expand=True, padx=self._px(12))
        self.qa_box = self._text(body, int(get(self.cfg, "ui.font_qa", 13)))
        self.qa_box.configure(height=2)  # 空间不足时优先保留完整的三行输入框
        self.qa_box.pack(side="left", fill="both", expand=True, padx=2, pady=2)
        self._scrollbar(body, self.qa_box).pack(side="right", fill="y", pady=self._px(8))
        self.qa_box.tag_configure("question", foreground=C_ACCENT, spacing1=9, spacing3=4)
        self.qa_box.tag_configure("answer", foreground=C_TEXT, spacing3=11)
        self.qa_box.tag_configure("pending", foreground=C_DIM)

        composer = tk.Frame(self.qa_frame, bg=C_PANEL)
        composer.pack(fill="x", padx=self._px(14), pady=(self._px(10), self._px(13)))
        editor = tk.Frame(composer, bg=C_PANEL)
        editor.pack(fill="x")
        entry_wrap = RoundedPanel(editor, bg=C_ASK_BG, radius=12)
        entry_wrap.pack(side="left", fill="both", expand=True)
        entry_wrap.grid_columnconfigure(0, weight=1)
        entry_wrap.grid_rowconfigure(0, weight=1)
        self.question_entry = tk.Text(entry_wrap, height=3, bg=C_ASK_BG, fg=C_TEXT,
                                      insertbackground=C_ACCENT, relief="flat", bd=0,
                                      highlightthickness=0, wrap="char", undo=True,
                                      font=self._font(int(get(self.cfg, "ui.font_qa", 13))),
                                      padx=self._px(12), pady=self._px(9),
                                      selectbackground=C_SELECT, selectforeground=C_TEXT)
        self.question_entry.grid(row=0, column=0, sticky="nsew", padx=3, pady=3)
        self.question_entry.bind("<Return>", self._question_return)
        self.question_entry.bind("<Shift-Return>", self._question_newline)
        self.question_entry.bind("<Escape>", lambda _e: (self.toggle_ask_panel(), "break")[1])
        self._tk_texts.append(self.question_entry)

        placeholder = tk.Label(self.question_entry, text="输入任何问题…",
                               bg=C_ASK_BG, fg=C_DIM, font=self._font(10), cursor="xterm")
        placeholder.place(x=self._px(13), y=self._px(10))

        def sync_placeholder():
            empty = not self.question_entry.get("1.0", "end-1c").strip()
            focused = self.root.focus_get() is self.question_entry
            if empty and not focused:
                placeholder.place(x=self._px(13), y=self._px(10))
            else:
                placeholder.place_forget()

        def focus_in(_event=None):
            entry_wrap.configure(highlightbackground=C_ACCENT)
            placeholder.place_forget()

        def focus_out(_event=None):
            entry_wrap.configure(highlightbackground=C_BORDER)
            self.root.after_idle(sync_placeholder)

        self.question_entry.bind("<FocusIn>", focus_in, add="+")
        self.question_entry.bind("<FocusOut>", focus_out, add="+")
        self.question_entry.bind("<KeyPress>", lambda _e: placeholder.place_forget(), add="+")
        placeholder.bind("<Button-1>", lambda _e: self.question_entry.focus_set())
        send = self._button(entry_wrap, "发送", self._send_question,
                            accent=True, side=None, compact=True)
        send.configure(font=self._font(10, "bold"), width=4)
        send.grid(row=0, column=1, sticky="se",
                  padx=(0, self._px(9)), pady=(0, self._px(8)))
        self._render_qa()

    def toggle_ask_panel(self) -> None:
        if self.root is None:
            return
        if self._qa_visible:
            try:
                self.workspace.forget(self.qa_frame)
            except Exception:
                pass
            self._qa_visible = False
        else:
            try:
                self.workspace.add(self.qa_frame, minsize=230, stretch="never", height=330)
            except Exception:
                pass
            self._qa_visible = True
        self._refresh_command_labels()
        self._set_button_palette(
            self._btn["ask"], C_ACCENT_SOFT if self._qa_visible else C_PANEL,
            C_ACCENT, C_ACCENT_SOFT_HOVER if self._qa_visible else C_ACCENT_SOFT)
        if self._qa_visible:
            self.root.after(40, lambda: self.question_entry and self.question_entry.focus_set())

    def _refresh_command_labels(self) -> None:
        if "ask" not in self._btn:
            return
        compact = self._compact_commands
        labels = {
            "capture": "看屏",
            "script": "照读稿",
            "im_asked": "被点名",
            "summary": "小结",
            "export": "导出" if compact else "导出纪要",
        }
        for key, text in labels.items():
            self._btn[key].configure(text=text)
        ask_text = "收起提问" if self._qa_visible else "自由提问"
        if compact:
            ask_text = "收起" if self._qa_visible else "提问"
        self._btn["ask"].configure(text=f"{ask_text}  F2")
        self._refresh_listen_btn()

    def _open_ask_bar(self) -> None:
        if not self._qa_visible:
            self.toggle_ask_panel()
        elif self.question_entry is not None:
            self.question_entry.focus_set()

    def _question_return(self, event):
        if int(getattr(event, "state", 0)) & 0x1:
            return None
        self._send_question()
        return "break"

    @staticmethod
    def _question_newline(_event):
        return None

    def _send_question(self) -> None:
        if self.question_entry is None:
            return
        text = self.question_entry.get("1.0", "end-1c").strip()
        if not text:
            return
        self.question_entry.delete("1.0", "end")
        self._submit_question(text)

    def _submit_question(self, text: str) -> None:
        question = str(text or "").strip()
        if not question:
            return
        self._qa_counter += 1
        rid = f"local-{self._qa_counter}"
        self._qa_turns[rid] = {"question": question, "answer": "", "done": False, "error": ""}
        self._qa_order.append(rid)
        self._qa_waiting.append(rid)
        self._render_qa()
        try:
            action = self._actions.get("ask")
            if action:
                action(question=question)
            else:
                self.bus.command("ask", question=question)
        except Exception:
            self.log.warning("自由提问发送失败\n%s", traceback.format_exc())

    def _clear_qa(self) -> None:
        self._qa_turns.clear()
        self._qa_order.clear()
        self._qa_waiting.clear()
        self._qa_alias.clear()
        self._render_qa()

    def _render_qa(self) -> None:
        if not hasattr(self, "qa_box"):
            return
        w = self.qa_box
        w.configure(state="normal")
        w.delete("1.0", "end")
        self._qa_last_question_index = None
        for rid in self._qa_order:
            turn = self._qa_turns[rid]
            self._qa_last_question_index = w.index("end-1c")
            w.insert("end", "你  " + turn["question"].strip() + "\n", ("question",))
            answer = turn["answer"].strip() or ("正在思考…" if not turn["done"] else "未返回内容")
            if turn.get("error"):
                answer += "\n" + turn["error"]
            w.insert("end", answer + "\n", ("answer" if turn["answer"] else "pending",))
        w.configure(state="disabled")
        self._scroll_qa_to_latest()
        self.root.after_idle(self._scroll_qa_to_latest)

    def _scroll_qa_to_latest(self) -> None:
        """长回答跟随末尾；短问答避免最新问题只露出半行。"""
        if not hasattr(self, "qa_box"):
            return
        w = self.qa_box
        try:
            w.yview_moveto(1.0)
            marker = getattr(self, "_qa_last_question_index", None)
            info = w.dlineinfo(marker) if marker else None
            if info is not None and info[1] < 0:
                w.yview_scroll(-1, "units")
        except tk.TclError:
            pass

    def _copy_latest_qa(self) -> None:
        for rid in reversed(self._qa_order):
            answer = self._qa_turns[rid]["answer"].strip()
            if answer:
                self._copy_text(answer)
                return
        self._set_msg("暂无可复制的回答")

    def _toggle_qa_memory(self) -> None:
        mode = "smart" if self.qa_memory_var.get() else False
        self.cfg.setdefault("brain", {})["free_qa_memory"] = mode
        try:
            self.bus.command("apply_settings", free_qa_memory=mode)
        except Exception:
            pass
        self._set_msg("相关记忆：按需检索" if mode else "相关记忆已关闭")

    # ------------------------------------------------------------ 事件与渲染
    @staticmethod
    def _set_text(widget: tk.Text, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", str(text or ""))
        widget.configure(state="disabled")

    def _copy_text(self, text: str) -> None:
        if self.root is None or not str(text or "").strip():
            self._set_msg("暂无可复制内容")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(str(text))
        self._set_msg("已复制到剪贴板")

    def _copy_focused(self, _event=None):
        try:
            focused = self.root.focus_get()
            if isinstance(focused, tk.Text) and focused.tag_ranges("sel"):
                return None
        except Exception:
            pass
        return None

    def _live_answer_view(self) -> dict:
        return {"answer": self._answer, "script": self._script,
                "answer_done": self._answer_done, "script_done": self._script_done}

    def _history_view(self) -> Optional[dict]:
        if self._history_index is not None and 0 <= self._history_index < len(self._answer_history):
            return self._answer_history[self._history_index]
        return None

    def _displayed_text(self, key: str) -> str:
        if self._frozen and self._frozen_view is not None:
            return str(self._frozen_view.get(key, "") or "")
        view = self._history_view() or self._live_answer_view()
        return str(view.get(key, "") or "")

    def _archive_current_answer(self) -> None:
        """新一组应答到来前保存快照；后台生成与当前阅读互不干扰。"""
        if not (self._answer or self._script or self._answer_done or self._script_done):
            return
        item = self._live_answer_view()
        item["request_id"] = self._answer_request_id
        self._answer_history.append(item)
        overflow = len(self._answer_history) - self.MAX_ANSWER_HISTORY
        if overflow > 0:
            del self._answer_history[:overflow]
            if self._history_index is not None:
                self._history_index = max(0, self._history_index - overflow)

    def _render_answer(self) -> None:
        if self._frozen:
            return
        view = self._history_view() or self._live_answer_view()
        if view.get("answer") or view.get("answer_done") or self._history_view() is not None:
            self._set_text(self.answer_box, view.get("answer") or "正在组织应答思路…")

    def _render_script(self) -> None:
        if self._frozen:
            return
        view = self._history_view() or self._live_answer_view()
        if view.get("script") or view.get("script_done") or self._history_view() is not None:
            self._set_text(self.script_box, view.get("script") or "正在准备照读稿…")

    def toggle_freeze(self) -> None:
        """F3：定格只影响展示，后台事件仍持续更新；解除后跳到最新。"""
        if not self._frozen:
            self._frozen_view = {"answer": self._displayed_text("answer"),
                                 "script": self._displayed_text("script"),
                                 "answer_done": True, "script_done": True}
            self._frozen = True
            self._history_index = None
            self._set_msg("答复已定格；后台继续生成，再按 F3 回到最新")
        else:
            self._frozen = False
            self._frozen_view = None
            self._history_index = None
            self._set_msg("已恢复到最新答复")
        self._render_answer()
        self._render_script()
        self._refresh_history_controls()

    def _navigate_history(self, step: int) -> None:
        if self._frozen:
            self._set_msg("请先按 F3 解除定格，再回看历史")
            return
        if not self._answer_history:
            self._set_msg("暂无历史答复")
            return
        if step < 0:
            self._history_index = (len(self._answer_history) - 1 if self._history_index is None
                                   else max(0, self._history_index - 1))
        else:
            if self._history_index is None:
                return
            if self._history_index >= len(self._answer_history) - 1:
                self._history_index = None   # 最新一组（可能仍在流式生成）
            else:
                self._history_index += 1
        self._render_answer()
        self._render_script()
        self._refresh_history_controls()

    def _refresh_history_controls(self) -> None:
        if self._history_status_lbl is None:
            return
        count = len(self._answer_history)
        if self._frozen:
            text = "定格中"
        elif self._history_index is None:
            text = "最新"
        else:
            text = f"历史 {self._history_index + 1}/{count}"
        self._history_status_lbl.configure(text=text, fg=C_WARM if self._frozen else C_DIM)
        prev_ok = bool(count) and not self._frozen and self._history_index != 0
        next_ok = bool(count) and not self._frozen and self._history_index is not None
        self._btn["history_prev"].configure(state="normal" if prev_ok else "disabled")
        self._btn["history_next"].configure(state="normal" if next_ok else "disabled")
        self._btn["freeze"].configure(text="解除定格  F3" if self._frozen else "定格  F3")
        self._set_button_palette(
            self._btn["freeze"], C_WARM_SOFT if self._frozen else C_PANEL,
            C_WARM if self._frozen else C_MUTED,
            C_WARM_HOVER if self._frozen else C_BTN_HOVER)

    def _render_hints(self) -> None:
        for lbl, item in zip(self.hint_labels, self._hints[-2:]):
            body = " ".join(str(item[2]).split())
            lbl.configure(text=f"{item[1]} · {body[:180]}")
        self._relayout_wrap()

    def _relayout_wrap(self) -> None:
        try:
            width = max(220, self.transcript.winfo_width() - 30)
            for lbl in self.hint_labels:
                lbl.configure(wraplength=width)
        except Exception:
            pass

    def _poll(self) -> None:
        self._poll_job = None
        if self._closed or self.root is None:
            return
        try:
            self._drain_commands()
            self._drain_transcripts()
            self._drain_brain()
            self._drain_status()
            self._refresh_clock()
            self._refresh_msg()
        except Exception:
            self.log.warning("UI 轮询异常\n%s", traceback.format_exc())
        finally:
            if not self._closed and self.root is not None:
                self._poll_job = self.root.after(self.POLL_MS, self._poll)

    def _drain_commands(self) -> None:
        for _ in range(20):
            try:
                self._handle_command(self.bus.commands.get_nowait())
            except queue.Empty:
                return

    def _handle_command(self, cmd: Any) -> None:
        if not isinstance(cmd, dict) or not cmd.get("cmd"):
            return
        name = str(cmd["cmd"])
        now = time.monotonic()
        if now - self._dispatch_ts.get(name, 0) < self.ECHO_WINDOW_S:
            return
        burst = self._dispatch_burst.setdefault(name, [])
        burst[:] = [t for t in burst if now - t < 2]
        if len(burst) >= self.BURST_LIMIT:
            return
        burst.append(now)
        self._dispatch_ts[name] = now
        if name == "hide":
            self.toggle_visible()
        elif name == "quit":
            self._invoke("quit")
            self.stop()
        elif name == "clickthrough":
            self.toggle_click_through()
        elif name == "ask_focus":
            self._open_ask_bar()
        elif name == "freeze":
            self.toggle_freeze()
        else:
            action = {"pause": "stop_listen", "resume": "start_listen", "grab": "screenshot"}.get(name, name)
            kwargs = {k: v for k, v in cmd.items() if k != "cmd"}
            if action in self._actions:
                try:
                    self._actions[action](**kwargs) if kwargs else self._actions[action]()
                except Exception:
                    self.log.warning("动作执行失败 %s\n%s", action, traceback.format_exc())

    def _invoke(self, name: str) -> bool:
        try:
            action = self._actions.get(name)
            if action:
                action()
                return True
        except Exception:
            pass
        return False

    def _click_action(self, name: str) -> None:
        if name not in self._actions:
            self._set_msg("动作尚未就绪")
            return
        try:
            self._actions[name]()
        except Exception:
            self.log.warning("动作执行失败 %s\n%s", name, traceback.format_exc())

    def _on_click_listen(self) -> None:
        if "toggle_listen" in self._actions:
            self._click_action("toggle_listen")
        else:
            self._click_action("stop_listen" if self._listening else "start_listen")

    def _on_click_quit(self) -> None:
        try:
            from tkinter import messagebox
            if not messagebox.askokcancel("退出Meeting Copilot", "退出前会自动导出本场纪要，确定退出吗？", parent=self.root):
                return
        except Exception:
            pass
        self._invoke("quit")
        if self.root is not None:
            self.root.after(100, self.stop)
        else:
            self.stop()

    def _drain_transcripts(self) -> None:
        for _ in range(40):
            try:
                self._append_transcript(self.bus.transcripts.get_nowait())
            except queue.Empty:
                return
            except Exception:
                self.log.warning("字幕渲染失败\n%s", traceback.format_exc())
                return

    def _at_bottom(self) -> bool:
        try:
            return self.transcript.yview()[1] >= 0.99
        except Exception:
            return True

    def _append_transcript(self, seg: TranscriptSegment) -> None:
        text = str(getattr(seg, "display_text", "") or "").strip()
        if not text:
            return
        final = bool(getattr(seg, "is_final", True))
        follow = bool(self._follow_var and self._follow_var.get())
        at_bottom = self._at_bottom()
        w = self.transcript
        w.configure(state="normal")
        if not self._transcript_started:
            w.delete("1.0", "end")
            self._transcript_started = True
        partial = w.tag_ranges("partial")
        if partial:
            w.delete(partial[0], partial[1])
        ts = fmt_ts(getattr(seg, "t_start", 0.0))
        if not final:
            w.insert("end", f"[{ts}] {text}▍\n", ("partial",))
        else:
            confidence = float(getattr(seg, "confidence", 0.0) or 0.0)
            tag = "uncertain" if 0 < confidence < 0.45 else "body"
            w.insert("end", f"[{ts}] ", ("ts",))
            w.insert("end", text + "\n", (tag,))
        try:
            if int(w.index("end-1c").split(".")[0]) > self.MAX_TRANSCRIPT_LINES:
                w.delete("1.0", "200.0")
        except Exception:
            pass
        w.configure(state="disabled")
        if follow and (at_bottom or not final):
            w.see("end")

    def _drain_brain(self) -> None:
        changed = False
        for _ in range(80):
            try:
                ev = self.bus.brain_events.get_nowait()
            except queue.Empty:
                break
            try:
                changed |= self._apply_brain_event(ev)
            except Exception:
                self.log.warning("建议渲染失败\n%s", traceback.format_exc())
        if changed:
            self._render_answer()
            self._render_script()
            self._render_hints()
            self._render_qa()
            self._refresh_history_controls()

    def _apply_brain_event(self, ev: BrainEvent) -> bool:
        kind = str(getattr(ev, "kind", "") or "")
        delta = str(getattr(ev, "delta", "") or "")
        full = str(getattr(ev, "full_text", "") or "")
        done = bool(getattr(ev, "done", False))
        if kind == KIND_ANSWER:
            rid = str(getattr(ev, "request_id", "") or "")
            if rid != self._answer_request_id:
                self._archive_current_answer()
                self._answer_request_id = rid
                self._answer = ""
                self._script = ""
                self._answer_done = self._script_done = False
            self._answer = full or self._answer + delta
            self._answer_done = done
            return True
        if kind == KIND_SCRIPT:
            self._script = full or self._script + delta
            self._script_done = done
            return True
        if kind == KIND_FREE_QA:
            rid = str(getattr(ev, "request_id", "") or "")
            local = self._qa_alias.get(rid)
            if local is None and self._qa_waiting:
                local = self._qa_waiting.pop(0)
                self._qa_alias[rid] = local
            if local is None:
                return False
            turn = self._qa_turns[local]
            turn["answer"] = full or turn["answer"] + delta
            turn["done"] = done
            turn["error"] = str(getattr(ev, "error", "") or "")
            return True
        if kind in (KIND_HINT, KIND_SLIDE, KIND_TERM, KIND_SUMMARY, KIND_MINUTES):
            titles = {KIND_HINT: "要点", KIND_SLIDE: "读屏", KIND_TERM: "术语",
                      KIND_SUMMARY: "小结", KIND_MINUTES: "纪要"}
            body = full or delta
            if not body:
                return False
            rid = str(getattr(ev, "request_id", "") or "")
            for item in self._hints:
                if rid and item[0] == rid:
                    item[1], item[2], item[3] = titles.get(kind, "提示"), body, time.time()
                    break
            else:
                self._hints.append([rid, titles.get(kind, "提示"), body, time.time()])
            self._hints = self._hints[-8:]
            return True
        return False

    def _drain_status(self) -> None:
        latest = None
        for _ in range(50):
            try:
                latest = self.bus.status.get_nowait()
            except queue.Empty:
                break
        if latest is None:
            return
        self._status = latest
        self._listening = bool(latest.listening)
        self._session_sec = float(latest.session_sec or 0)
        self._delay = float(getattr(latest, "last_delay", 0) or 0)
        self._device = str(latest.device or "")
        self.state_lbl.configure(text=status_text(self._listening, bool(latest.asr_ready), True),
                                 fg=C_TEXT if self._listening else C_MUTED)
        self.dot.configure(fg=C_OK if self._listening else C_DIM)
        self.timer_lbl.configure(fg=C_ACCENT if self._listening else C_TEXT)
        self.delay_lbl.configure(text=f"{self._delay:.1f} s" if self._delay else "")
        self.dev_lbl.configure(text=short_device(self._device, 30))
        self._refresh_listen_btn()

    def _refresh_listen_btn(self) -> None:
        if "listen" not in self._btn:
            return
        if self._compact_commands:
            text = "Ⅱ 暂停  F8" if self._listening else "▶ 开始  F8"
        else:
            text = "Ⅱ 暂停聆听  F8" if self._listening else "▶ 开始聆听  F8"
        self._btn["listen"].configure(text=text)

    def _refresh_clock(self) -> None:
        seconds = int(self._session_sec)
        self.timer_lbl.configure(text=f"{seconds // 60:02d}:{seconds % 60:02d}")

    def _refresh_msg(self) -> None:
        self.msg_lbl.configure(text=self._local_msg if time.time() < self._local_msg_until else "")

    def _set_msg(self, text: str, seconds: float = 4.0) -> None:
        self._local_msg = str(text or "")
        self._local_msg_until = time.time() + seconds

    # ------------------------------------------------------------ 显示与设置
    def is_visible(self) -> bool:
        return self._visible

    def hide(self) -> None:
        if self.root is not None:
            self.root.withdraw()
            self._visible = False

    def show(self) -> None:
        if self.root is not None:
            self.root.deiconify()
            self._visible = True
            self._apply_display_affinity()

    def toggle_visible(self) -> None:
        self.hide() if self._visible else self.show()

    def toggle_click_through(self) -> bool:
        if self.root is None:
            return False
        try:
            import ctypes
            hwnd = self._get_hwnd()
            style = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
            want = not self._click_through
            style = style | 0x00080000 | 0x00000020 if want else style & ~0x00000020
            ctypes.windll.user32.SetWindowLongW(hwnd, -20, style)
            self._click_through = want
            self._set_msg("鼠标穿透已开启（F4 关闭）" if want else "鼠标穿透已关闭")
            return want
        except Exception:
            self._set_msg("鼠标穿透切换失败")
            return False

    def apply_font_scale(self, scale: float) -> None:
        new = max(0.8, min(1.5, float(scale)))
        ratio = new / max(self._font_scale, 0.01)
        if abs(ratio - 1) < 0.001:
            return
        def walk(widget):
            for child in widget.winfo_children():
                try:
                    font = child.cget("font")
                    if isinstance(font, tuple) and len(font) >= 2:
                        child.configure(font=(font[0], max(8, int(round(int(font[1]) * ratio))),
                                            *(font[2:] if len(font) > 2 else ())))
                except Exception:
                    pass
                walk(child)
        if self.root is not None:
            walk(self.root)
        self._font_scale = new
        self.cfg.setdefault("ui", {})["font_scale"] = int(round(new * 100))

    def apply_settings(self, **kw: Any) -> bool:
        changed = False
        if kw.get("scenario") is not None:
            scenario = normalize_scenario(kw["scenario"])
            self.cfg.setdefault("app", {})["scenario"] = scenario
            app_name = str(get(self.cfg, "app.name", "Meeting Copilot") or "Meeting Copilot")
            if self.root is not None:
                self.root.title(f"{app_name} · {scenario_label(self.cfg)}")
            if hasattr(self, "app_title_lbl"):
                self.app_title_lbl.configure(text=app_name)
            changed = True
        if kw.get("screen") in ("primary", "secondary"):
            self.cfg.setdefault("ui", {})["screen"] = kw["screen"]
            self._apply_geometry(prefer_saved=False)
            changed = True
        return changed

    def _open_settings(self) -> None:
        if self.root is None:
            return
        if self._settings_win is not None:
            try:
                self._settings_win.lift()
                return
            except Exception:
                pass
        win = tk.Toplevel(self.root)
        self._settings_win = win
        win.withdraw()
        win.title("设置 · Meeting Copilot")
        win.configure(bg=C_BG)
        win.resizable(True, True)
        win.transient(self.root)
        frame = tk.Frame(win, bg=C_BG)
        frame.pack(fill="both", expand=True, padx=self._px(22), pady=self._px(20))
        header = tk.Frame(frame, bg=C_BG)
        header.pack(fill="x", pady=(0, self._px(14)))
        tk.Label(header, text="设置", bg=C_BG, fg=C_TEXT,
                 font=self._font(18, "bold")).pack(side="left")

        footer = tk.Frame(frame, bg=C_BG)
        footer.pack(side="bottom", fill="x", pady=(self._px(14), 0))
        body = tk.Frame(frame, bg=C_BG)
        body.pack(fill="both", expand=True)
        canvas = tk.Canvas(body, bg=C_BG, bd=0, highlightthickness=0)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar = self._scrollbar(body, canvas)
        content = tk.Frame(canvas, bg=C_BG)
        pane = canvas.create_window((0, 0), window=content, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(pane, width=e.width))

        def sync_scrollbar(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
            needed = content.winfo_reqheight() > canvas.winfo_height() + 2
            if needed and not scrollbar.winfo_ismapped():
                scrollbar.pack(side="right", fill="y", padx=(self._px(8), 0))
            elif not needed and scrollbar.winfo_ismapped():
                scrollbar.pack_forget()

        content.bind("<Configure>", sync_scrollbar)
        canvas.bind("<Configure>", sync_scrollbar, add="+")
        win.bind("<MouseWheel>", lambda e: (
            canvas.yview_scroll(int(-e.delta / 120), "units"), "break")[1])

        meeting = self._settings_group(content, "场景与识别")
        scenario_var = self._combo(meeting, "使用场景", scenario_label(self.cfg), ["组会", "面试"])
        self._settings_rule(meeting)
        devices, _ = list_output_devices_safe(self.log)
        dev_var = self._combo(meeting, "输出设备", str(get(self.cfg, "audio.device", "auto")),
                              ["auto"] + devices)
        self._settings_rule(meeting)
        asr_var = self._combo(meeting, "识别模型", str(get(self.cfg, "asr.model", "small")),
                              ["small", "large-v3-turbo"])
        tk.Label(meeting, text="模型重启后生效；turbo 更慢，不保证所有片段更准。",
                 bg=C_PANEL, fg=C_DIM, font=self._font(9), anchor="w",
                 justify="left").pack(fill="x", padx=self._px(16), pady=(0, self._px(13)))

        window_group = self._settings_group(content, "窗口")
        screen = str(get(self.cfg, "ui.screen", "secondary"))
        scr_var = self._combo(window_group, "初始屏幕",
                              "主屏幕" if screen == "primary" else "副屏幕", ["主屏幕", "副屏幕"])
        self._settings_rule(window_group)
        size_var = self._combo(window_group, "窗口预设", "普通窗口",
                               ["普通窗口", "系统最大化", "2/3 屏幕", "1/2 屏幕"])
        self._settings_rule(window_group)
        font_var = self._combo(window_group, "界面缩放", f"{int(round(self._font_scale*100))}%",
                               ["80%", "90%", "100%", "110%", "125%", "140%", "150%"])
        self._settings_rule(window_group)
        top_var = tk.BooleanVar(value=bool(get(self.cfg, "ui.split_topmost", False)))
        top_check = self._check(window_group, "窗口始终置顶", top_var)
        top_check.configure(font=self._font(10))
        top_check.pack(anchor="w", padx=self._px(16), pady=self._px(10))

        def save():
            scenario = "interview" if scenario_var.get() == "面试" else "meeting"
            screen = "primary" if scr_var.get() == "主屏幕" else "secondary"
            try:
                self.bus.command("apply_settings", device=dev_var.get() or "auto",
                                 model=asr_var.get(), screen=screen, scenario=scenario)
            except Exception:
                pass
            self.apply_settings(screen=screen, scenario=scenario)
            self._apply_size_mode({"系统最大化": "full", "2/3 屏幕": "2/3", "1/2 屏幕": "1/2"}.get(size_var.get(), "normal"))
            self.root.attributes("-topmost", bool(top_var.get()))
            self.cfg.setdefault("ui", {})["split_topmost"] = bool(top_var.get())
            self.apply_font_scale(int(font_var.get().replace("%", "")) / 100)
            self._close_settings()

        self._settings_save = save
        self._button(footer, "保存", save, accent=True, side="right")
        self._button(footer, "取消", self._close_settings, side="right")
        win.protocol("WM_DELETE_WINDOW", self._close_settings)
        win.bind("<Escape>", lambda _e: self._close_settings())
        win.update_idletasks()
        width = min(self._px(620), win.winfo_screenwidth() - 60)
        chrome = (header.winfo_reqheight() + footer.winfo_reqheight() + self._px(74))
        height = min(content.winfo_reqheight() + chrome, win.winfo_screenheight() - 60)
        height = max(self._px(540), height)
        win.minsize(min(self._px(500), width), min(self._px(540), height))
        x = max(20, min(self.root.winfo_rootx() + self.root.winfo_width() - width - 30,
                        win.winfo_screenwidth() - width - 20))
        y = max(20, min(self.root.winfo_rooty() + 30, win.winfo_screenheight() - height - 20))
        win.geometry(f"{width}x{height}+{x}+{y}")
        win.deiconify()
        win.lift()
        win.focus_set()

    def _settings_group(self, parent, title: str) -> tk.Frame:
        tk.Label(parent, text=title, bg=C_BG, fg=C_MUTED,
                 font=self._font(10, "bold")).pack(
                     anchor="w", padx=self._px(4), pady=(self._px(12), self._px(6)))
        group = RoundedPanel(parent, bg=C_PANEL, radius=14)
        group.pack(fill="x", pady=(0, self._px(8)))
        return group

    def _settings_rule(self, parent) -> None:
        tk.Frame(parent, bg=C_BORDER, height=1).pack(
            fill="x", padx=(self._px(16), 0))

    def _combo(self, parent, label: str, value: str, values: list[str]) -> tk.StringVar:
        row = tk.Frame(parent, bg=C_PANEL)
        row.pack(fill="x", padx=self._px(16), pady=self._px(8))
        tk.Label(row, text=label, bg=C_PANEL, fg=C_TEXT,
                 font=self._font(10)).pack(side="left", padx=(0, self._px(14)))
        var = tk.StringVar(value=value)
        ttk.Combobox(row, textvariable=var, values=values, state="readonly",
                     style="MeetingCopilot.TCombobox", width=24,
                     font=self._font(10)).pack(side="right")
        return var

    def _close_settings(self) -> None:
        if self._settings_win is not None:
            try:
                self._settings_win.destroy()
            except Exception:
                pass
        self._settings_win = None
        self._settings_save = None

    # ------------------------------------------------------------ 对外
    def register_actions(self, actions: dict[str, Callable[..., Any]]) -> None:
        self._actions = dict(actions or {})
        self._refresh_action_states()

    def _refresh_action_states(self) -> None:
        for key, btn in self._btn.items():
            if key in ("quit", "hide", "settings", "ask", "freeze"):
                btn.configure(state="normal")
            elif key not in ("history_prev", "history_next"):
                action = {"capture": "screenshot", "listen": "toggle_listen"}.get(key, key)
                available = action in self._actions or (key == "listen" and "start_listen" in self._actions)
                btn.configure(state="normal" if available else "disabled")
        self._refresh_history_controls()

    def run(self) -> None:
        if self.root is not None:
            self.root.mainloop()

    def stop(self) -> None:
        if self._closed:
            return
        self._save_ui_state()
        self._closed = True
        for job in (self._poll_job, self._save_job):
            if job is not None and self.root is not None:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
        self._close_settings()
        if self.root is not None:
            try:
                self.root.destroy()
            except Exception:
                pass
        self.root = None
        self._started = False

    def debug_info(self) -> dict:
        return {"layout": "native-split", "geometry": self.root.geometry() if self.root else "",
                "answer_len": len(self._answer), "script_len": len(self._script),
                "answer_history": len(self._answer_history), "frozen": self._frozen,
                "history_index": self._history_index,
                "qa_turns": len(self._qa_order), "qa_open": self._qa_visible,
                "resizable": self.root.resizable() if self.root else (False, False)}


# ================================================================== 真机渲染自测
def _selftest() -> int:
    import argparse
    from PIL import ImageGrab
    ap = argparse.ArgumentParser(description="原生双栏界面自测")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--seconds", type=float, default=3.0)
    ap.parse_args()
    os.environ.setdefault("MC_DISABLE_CAPTURE_EXCLUDE", "1")
    from config import load_config
    cfg = load_config()
    cfg.setdefault("ui", {}).update(screen="primary", alpha=1.0, split_fullscreen=False,
                                     split_size=[1380, 900], font_scale=100)
    bus = EventBus()
    ui = OverlayUI(cfg, bus, get_logger("ui-split-selftest"))
    ui._state_path = ROOT / "_dev_test" / "ui_state_selftest.json"
    ui.start()
    checks: list[tuple[str, bool]] = []
    sent: list[str] = []

    def setup():
        ui.register_actions({name: (lambda **kw: None) for name in
                             ("toggle_listen", "screenshot", "script", "summary", "export", "im_asked")})
        ui.register_actions({**ui._actions, "ask": lambda **kw: sent.append(str(kw.get("question") or "")),
                             "quit": lambda: None})
        for i in range(5):
            bus.emit_transcript(TranscriptSegment(seg_id=i, t_start=i * 8, t_end=i * 8 + 5,
                text=f"第 {i+1} 句：我们讨论多模态视频生成与物理约束，下一步要设计可验证的对照实验。"))
        bus.emit_brain(BrainEvent(request_id="a", kind=KIND_ANSWER, lane="deep", done=True,
            full_text="1. 先明确评价目标：区分视觉质量、时序一致性与物理合理性。\n2. 使用同一输入和采样预算，避免把算力差异误认为方法收益。\n3. 对关键失败案例做人工复核，并保留可复现记录。"))
        bus.emit_brain(BrainEvent(request_id="s", kind=KIND_SCRIPT, lane="deep", done=True,
            full_text="我这边的理解是，下一步不只是比较生成效果，还要把物理合理性拆成可以检验的指标。我们可以先固定输入和采样预算，再针对典型失败案例做对照，这样结论会更可靠。"))
        bus.emit_brain(BrainEvent(request_id="h", kind=KIND_HINT, lane="fast", done=True,
                                  full_text="可补充说明评价指标与实验预算。"))

    def ask():
        ui._open_ask_bar()
        ui.question_entry.insert("1.0", "GAN 和 diffusion 在视频生成中怎么比较？")
        ui._send_question()
        bus.emit_brain(BrainEvent(request_id="q1", kind=KIND_FREE_QA, lane="deep", done=True,
            full_text="可以从训练稳定性、采样成本、时序建模和评价协议四个方面比较，不应只看单帧指标。"))
        ui.question_entry.insert("1.0", "第二轮问题：实验应该怎么设计？")
        ui._send_question()
        bus.emit_brain(BrainEvent(request_id="q2", kind=KIND_FREE_QA, lane="deep", done=True,
            full_text="固定数据与计算预算，分别评价画质、时序一致性和物理误差，并报告失败案例。"))

    def history_test():
        bus.emit_brain(BrainEvent(request_id="a2", kind=KIND_ANSWER, lane="deep", done=True,
                                  full_text="第二轮应答：固定实验预算，再比较物理误差。"))
        bus.emit_brain(BrainEvent(request_id="s2", kind=KIND_SCRIPT, lane="deep", done=True,
                                  full_text="第二组照读稿：我们先固定预算，再比较误差。"))
        ui._drain_brain()
        checks.append(("新应答自动归档上一组", len(ui._answer_history) == 1 and
                       "评价目标" in ui._answer_history[0]["answer"]))
        ui.toggle_freeze()
        before = ui.answer_box.get("1.0", "end")
        bus.emit_brain(BrainEvent(request_id="a3", kind=KIND_ANSWER, lane="deep", done=True,
                                  full_text="第三轮应答：补充失败案例与可复现记录。"))
        bus.emit_brain(BrainEvent(request_id="s3", kind=KIND_SCRIPT, lane="deep", done=True,
                                  full_text="第三组照读稿：最后补充失败案例和复现方式。"))
        ui._drain_brain()
        checks.append(("定格时画面不变且后台更新", ui._frozen and
                       ui.answer_box.get("1.0", "end") == before and "第三轮" in ui._answer))
        ui.toggle_freeze()
        checks.append(("解除定格恢复最新", not ui._frozen and
                       "第三轮" in ui.answer_box.get("1.0", "end") and
                       "第三组照读稿" in ui.script_box.get("1.0", "end")))
        ui._navigate_history(-1)
        previous_ok = "第二轮" in ui.answer_box.get("1.0", "end")
        ui._navigate_history(-1)
        first_ok = "评价目标" in ui.answer_box.get("1.0", "end")
        ui._navigate_history(1)
        next_ok = "第二轮" in ui.answer_box.get("1.0", "end")
        ui._navigate_history(1)
        latest_ok = ui._history_index is None and "第三轮" in ui.answer_box.get("1.0", "end")
        checks.append(("历史可前后回看并返回最新", previous_ok and first_ok and next_ok and latest_ok))

    def shot():
        ui._btn["ask"].invoke()
        ui.root.update()
        click_closed = not ui._qa_visible
        ui._btn["ask"].invoke()
        ui.root.update()
        checks.append(("圆角按钮支持真实点击", click_closed and ui._qa_visible))
        checks.extend([
            ("原生窗口可自由拉伸", ui.root.resizable() == (True, True) and not ui.root.overrideredirect()),
            ("看屏等动作按钮可用", all(ui._btn[k].cget("state") == "normal" for k in ("listen", "capture", "script"))),
            ("左右分栏可拖动", isinstance(ui.main_pane, tk.PanedWindow)),
            ("F2 展开三行提问区", ui._qa_visible and isinstance(ui.question_entry, tk.Text)
             and ui.question_entry.winfo_height() >= 60),
            ("两轮问题均已发送", len(sent) == 2 and "第二轮" in sent[1]),
            ("问答历史完整保留", all(x in ui.qa_box.get("1.0", "end") for x in ("GAN", "第二轮问题", "失败案例"))),
            ("实时字幕已渲染", "多模态视频生成" in ui.transcript.get("1.0", "end")),
            ("应答与照读稿已渲染", "第三轮" in ui.answer_box.get("1.0", "end") and "第三组照读稿" in ui.script_box.get("1.0", "end")),
        ])
        ui._open_settings()
        ui.root.update()
        settings_win = ui._settings_win
        save_settings = ui._settings_save
        settings_ok = (settings_win is not None and settings_win.cget("bg") == C_BG and
                       settings_win.resizable() == (True, True) and callable(save_settings))
        checks.append(("设置窗口使用统一浅色主题", settings_ok))
        time.sleep(0.3)
        ui.root.update()
        try:
            img = ImageGrab.grab()
            x, y = settings_win.winfo_rootx(), settings_win.winfo_rooty()
            out = ROOT / "docs" / "images" / "meeting-copilot-settings.png"
            out.parent.mkdir(parents=True, exist_ok=True)
            img.crop((x, y, x + settings_win.winfo_width(), y + settings_win.winfo_height())).save(out)
            checks.append(("设置窗口截图成功", out.exists()))
        except Exception as e:
            checks.append((f"设置截图失败：{e}", False))

        def packed_canvas(widget):
            for child in widget.winfo_children():
                if (isinstance(child, tk.Canvas) and not isinstance(child, RoundedButton)
                        and child.winfo_manager() == "pack"):
                    return child
                found = packed_canvas(child)
                if found is not None:
                    return found
            return None

        settings_win.geometry("560x540+60+60")
        ui.root.update()
        canvas = packed_canvas(settings_win)
        if canvas is not None:
            canvas.yview_moveto(1.0)
            ui.root.update()
        scroll_ok = canvas is not None and canvas.yview()[0] > 0
        if not scroll_ok:
            print("[CHECK] 设置滚动", settings_win.geometry(),
                  None if canvas is None else (canvas.yview(), canvas.cget("scrollregion"),
                                                canvas.bbox("all"), canvas.winfo_height()))
        checks.append(("设置窗口缩小后可滚动", scroll_ok))
        if callable(save_settings):
            save_settings()
        checks.append(("设置可保存并关闭", ui._settings_win is None and ui._settings_save is None))
        base_size = ui._btn["listen"].cget("font")[1]
        ui.apply_font_scale(1.1)
        scaled_size = ui._btn["listen"].cget("font")[1]
        ui.apply_font_scale(1.0)
        ui.root.update()
        checks.append(("圆角控件支持界面缩放", scaled_size > base_size and
                       ui._btn["listen"].cget("font")[1] == base_size))
        ui.apply_settings(scenario="interview")
        ui.root.update_idletasks()
        checks.append(("场景可即时切换为面试", scenario_label(ui.cfg) == "面试" and
                       ui.root.title().endswith("面试") and
                       ui.app_title_lbl.cget("text") == "Meeting Copilot"))
        ui.apply_settings(scenario="meeting")
        ui.root.update_idletasks()
        checks.append(("场景可切回组会", scenario_label(ui.cfg) == "组会" and
                       ui.root.title().endswith("组会") and
                       ui.app_title_lbl.cget("text") == "Meeting Copilot"))
        ui.root.state("normal")
        ui.root.geometry("1120x760+40+40")
        ui.root.update()
        checks.append(("普通窗口尺寸可调整", ui.root.winfo_width() == 1120 and ui.root.winfo_height() == 760))
        command_keys = ("listen", "capture", "script", "im_asked", "summary", "export", "ask")
        checks.append(("窄窗口顶部操作完整可见", all(
            ui._btn[k].winfo_x() >= 0 and
            ui._btn[k].winfo_x() + ui._btn[k].winfo_width() <= ui._btn[k].master.winfo_width()
            for k in command_keys)))
        ui.root.geometry("800x650+40+40")
        ui.root.update()
        checks.append(("800 像素宽度使用紧凑命令", ui._compact_commands and all(
            ui._btn[k].winfo_x() >= 0 and
            ui._btn[k].winfo_x() + ui._btn[k].winfo_width() <= ui._btn[k].master.winfo_width()
            for k in command_keys)))
        ui.root.state("zoomed")
        ui.root.update()
        checks.append(("支持系统最大化", ui.root.state() == "zoomed"))
        ui.root.state("normal")
        ui.root.geometry("1380x900+20+20")
        ui.root.attributes("-topmost", True)
        ui.root.lift()
        ui.root.focus_force()
        ui._btn["listen"].focus_set()  # 收起输入法候选框，不遮挡演示截图
        ui.root.update()
        ui.root.focus_set()
        ui._follow_after_resize()
        ui.root.update()
        checks.append(("缩放后字幕跟随最新", ui.transcript.yview()[1] >= 0.99))
        question_info = ui.qa_box.dlineinfo(ui._qa_last_question_index)
        answer_info = ui.qa_box.dlineinfo("end-2c")
        checks.append(("最新问题与回答完整可见",
                       question_info is not None and question_info[1] >= 0 and
                       answer_info is not None and answer_info[1] >= 0 and
                       answer_info[1] + answer_info[3] <= ui.qa_box.winfo_height()))
        time.sleep(0.5)  # 等 Windows DWM 完成合成，避免截到遮挡窗口残影
        ui.root.update()
        try:
            img = ImageGrab.grab()
            x, y = ui.root.winfo_rootx(), ui.root.winfo_rooty()
            out = ROOT / "docs" / "images" / "meeting-copilot.png"
            out.parent.mkdir(parents=True, exist_ok=True)
            img.crop((x, y, x + ui.root.winfo_width(), y + ui.root.winfo_height())).save(out)
            checks.append(("真机截图成功", out.exists()))
        except Exception as e:
            checks.append((f"截图失败：{e}", False))
        ui.root.after(150, ui.stop)

    ui.root.after(350, setup)
    ui.root.after(850, ask)
    ui.root.after(1250, history_test)
    ui.root.after(1900, shot)
    ui.run()
    try:
        ui._state_path.unlink(missing_ok=True)
        ui._state_path.with_suffix(".tmp").unlink(missing_ok=True)
    except Exception:
        pass
    for name, ok in checks:
        print(("✅ " if ok else "❌ ") + name)
    ok = bool(checks) and all(v for _, v in checks)
    print("✅ 原生双栏界面自测通过" if ok else "❌ 自测失败")
    return 0 if ok else 1


def main(argv=None) -> int:
    if "--selftest" in (argv if argv is not None else sys.argv):
        try:
            return _selftest()
        except KeyboardInterrupt:
            return 130
    print("用法: python ui_split.py --selftest")
    return 0


if __name__ == "__main__":
    sys.exit(main())
