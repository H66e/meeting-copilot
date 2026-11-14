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
from tkinter import ttk

from config import get
from contracts import (KIND_ANSWER, KIND_FREE_QA, KIND_HINT, KIND_MINUTES, KIND_SCRIPT,
                       KIND_SLIDE, KIND_SUMMARY, KIND_TERM, BrainEvent, EventBus, Status,
                       TranscriptSegment, fmt_ts)
from logging_setup import get_logger
from scenarios import normalize_scenario, scenario_label
from ui_overlay import (delay_text, enable_dpi_awareness, iter_monitors,
                        list_output_devices_safe, pick_monitor, short_device, status_text)

C_BG = "#0d1117"
C_PANEL = "#141a23"
C_CARD = "#19212d"
C_ANSW_BG = "#182231"
C_READ_BG = "#13251f"
C_ASK_BG = "#111923"
C_BORDER = "#293342"
C_TEXT = "#edf2f8"
C_MUTED = "#a9b4c3"
C_DIM = "#748195"
C_ACCENT = "#68a7ff"
C_WARM = "#efbd72"
C_READ = "#78d7ab"
C_OK = "#55d68b"
C_ERR = "#f08383"
C_ERR_SOFT = "#392229"
C_TS = "#718096"
C_BTN = "#202a38"
C_BTN_HOVER = "#2c394b"


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
        self._btn: dict[str, tk.Button] = {}
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
        head = tk.Frame(self.root, bg=C_BG)
        head.pack(fill="x", padx=self._px(16), pady=(self._px(12), 0))
        title = tk.Frame(head, bg=C_BG)
        title.pack(side="left")
        app_name = str(get(self.cfg, "app.name", "Meeting Copilot") or "Meeting Copilot")
        self.app_title_lbl = tk.Label(title, text=f"{app_name} · {scenario_label(self.cfg)}",
                                      bg=C_BG, fg=C_TEXT, font=self._font(15, "bold"))
        self.app_title_lbl.pack(side="left")
        self.dot = tk.Label(title, text="●", bg=C_BG, fg=C_DIM, font=self._font(10))
        self.dot.pack(side="left", padx=(self._px(16), self._px(4)))
        self.state_lbl = tk.Label(title, text="待机", bg=C_BG, fg=C_MUTED, font=self._font(10))
        self.state_lbl.pack(side="left")
        self.timer_lbl = tk.Label(title, text="00:00", bg=C_BG, fg=C_MUTED,
                                  font=self._font(10, mono=True))
        self.timer_lbl.pack(side="left", padx=(self._px(10), 0))
        self._btn["settings"] = self._button(head, "设置", self._open_settings, side="right")
        self._btn["hide"] = self._button(head, "隐藏  F7", self.toggle_visible, side="right")
        self._btn["quit"] = self._button(head, "退出", self._on_click_quit, side="right", danger=True)

        toolbar = tk.Frame(self.root, bg=C_BG)
        toolbar.pack(fill="x", padx=self._px(14), pady=(self._px(10), self._px(8)))
        self._btn["listen"] = self._button(toolbar, "▶ 开始聆听  F8", self._on_click_listen, accent=True)
        for key, label in (("capture", "看屏  F9"), ("script", "照读稿  F5"),
                           ("im_asked", "被点名  F6"), ("summary", "小结  F10"),
                           ("export", "导出纪要")):
            action = {"capture": "screenshot"}.get(key, key)
            self._btn[key] = self._button(toolbar, label, lambda a=action: self._click_action(a))
        self._btn["ask"] = self._button(toolbar, "自由提问  F2", self.toggle_ask_panel, accent=True)
        self.delay_lbl = tk.Label(toolbar, text="延迟 -", bg=C_BG, fg=C_DIM, font=self._font(9))
        self.delay_lbl.pack(side="right", padx=self._px(4))

        bottom = tk.Frame(self.root, bg=C_BG)
        bottom.pack(side="bottom", fill="x", padx=self._px(16), pady=(self._px(4), self._px(9)))
        self.msg_lbl = tk.Label(bottom, text="", bg=C_BG, fg=C_WARM, font=self._font(9), anchor="w")
        self.msg_lbl.pack(side="left", fill="x", expand=True)
        self.dev_lbl = tk.Label(bottom, text="默认设备", bg=C_BG, fg=C_DIM, font=self._font(9))
        self.dev_lbl.pack(side="right")

        self.workspace = tk.PanedWindow(self.root, orient="vertical", bg=C_BG, bd=0,
                                        sashwidth=7, sashrelief="flat", relief="flat",
                                        opaqueresize=True, showhandle=False)
        self.workspace.pack(fill="both", expand=True, padx=self._px(14), pady=(0, self._px(4)))
        self.main_pane = tk.PanedWindow(self.workspace, orient="horizontal", bg=C_BORDER, bd=0,
                                        sashwidth=7, sashrelief="flat", relief="flat",
                                        opaqueresize=True, showhandle=False)
        self.workspace.add(self.main_pane, stretch="always")

        left = self._panel(self.main_pane)
        self._section_head(left, "实时字幕", "听清讨论，也允许保留不确定文本", C_ACCENT)
        sub = tk.Frame(left, bg=C_PANEL)
        sub.pack(fill="x", padx=self._px(14), pady=(0, self._px(4)))
        self._follow_var = tk.BooleanVar(value=True)
        self._check(sub, "跟随最新字幕", self._follow_var).pack(side="right")
        transcript_wrap = tk.Frame(left, bg=C_PANEL)
        transcript_wrap.pack(fill="both", expand=True, padx=self._px(6), pady=(0, self._px(8)))
        self.transcript = self._text(transcript_wrap, int(get(self.cfg, "ui.font_transcript", 16)))
        self.transcript.pack(side="left", fill="both", expand=True, padx=(self._px(8), 0))
        self._scrollbar(transcript_wrap, self.transcript).pack(side="right", fill="y")
        self.transcript.tag_configure("ts", foreground=C_TS, font=self._font(10, mono=True))
        self.transcript.tag_configure("body", foreground=C_TEXT)
        self.transcript.tag_configure("uncertain", foreground=C_MUTED)
        self.transcript.tag_configure("partial", foreground=C_DIM)
        self._set_text(self.transcript, "等待声音…\n播放会议声音后，实时字幕会显示在这里。\n")
        self.main_pane.add(left, minsize=280, stretch="always", width=650)

        right = tk.Frame(self.main_pane, bg=C_BG)
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(0, weight=5)
        right.grid_rowconfigure(1, weight=6)
        answer_wrap = self._card(right, "应答思路", "先说结论，再补依据", C_WARM, C_ANSW_BG,
                                 lambda: self._copy_text(self._displayed_text("answer")))
        answer_wrap.grid(row=0, column=0, sticky="nsew", pady=(0, self._px(5)))
        answer_nav = tk.Frame(answer_wrap, bg=C_ANSW_BG)
        answer_nav.pack(fill="x", padx=self._px(10), pady=(self._px(4), 0))
        self._btn["history_prev"] = self._button(
            answer_nav, "‹ 上一组", lambda: self._navigate_history(-1), compact=True)
        self._btn["history_next"] = self._button(
            answer_nav, "下一组 ›", lambda: self._navigate_history(1), compact=True)
        self._btn["freeze"] = self._button(
            answer_nav, "定格  F3", self.toggle_freeze, side="right", compact=True)
        self._history_status_lbl = tk.Label(
            answer_nav, text="最新", bg=C_ANSW_BG, fg=C_DIM, font=self._font(8))
        self._history_status_lbl.pack(side="right", padx=self._px(6))
        self.answer_box = self._text(answer_wrap, int(get(self.cfg, "ui.font_answer", 14)))
        self.answer_box.pack(fill="both", expand=True, padx=self._px(8), pady=(0, self._px(8)))
        self._set_text(self.answer_box, "被提问或点击“被点名”后，这里显示可直接使用的回答要点。")
        script_wrap = self._card(right, "照读稿", "自然口语 · 可直接照着念", C_READ, C_READ_BG,
                                 lambda: self._copy_text(self._displayed_text("script")))
        script_wrap.grid(row=1, column=0, sticky="nsew", pady=(self._px(5), 0))
        self.script_box = self._text(script_wrap, int(get(self.cfg, "ui.font_script", 17)))
        self.script_box.pack(fill="both", expand=True, padx=self._px(8), pady=(0, self._px(8)))
        self._set_text(self.script_box, "需要发言时按 F5，或与应答思路一起自动生成。")
        self.main_pane.add(right, minsize=340, stretch="always", width=560)

        hints = self._panel(self.root)
        hints.place_forget()  # 信息放入左栏底部，不占独立标签页
        self.hints_wrap = tk.Frame(left, bg=C_PANEL)
        self.hints_wrap.pack(fill="x", padx=self._px(14), pady=(0, self._px(10)))
        self.hint_labels: list[tk.Label] = []
        for _ in range(2):
            lbl = tk.Label(self.hints_wrap, text="", bg=C_PANEL, fg=C_MUTED,
                           font=self._font(9), anchor="w", justify="left", wraplength=500)
            lbl.pack(fill="x", pady=self._px(1))
            self.hint_labels.append(lbl)
        left.bind("<Configure>", lambda _e: self._relayout_wrap(), add="+")
        self._build_ask_panel()

    def _panel(self, parent) -> tk.Frame:
        return tk.Frame(parent, bg=C_PANEL, highlightbackground=C_BORDER,
                        highlightthickness=1, bd=0)

    def _section_head(self, parent, title: str, subtitle: str, color: str) -> None:
        row = tk.Frame(parent, bg=C_PANEL)
        row.pack(fill="x", padx=self._px(14), pady=(self._px(12), self._px(7)))
        tk.Frame(row, bg=color, width=3).pack(side="left", fill="y")
        tk.Label(row, text=title, bg=C_PANEL, fg=C_TEXT,
                 font=self._font(12, "bold")).pack(side="left", padx=(self._px(8), 0))
        tk.Label(row, text=subtitle, bg=C_PANEL, fg=C_DIM,
                 font=self._font(9)).pack(side="right")

    def _card(self, parent, title: str, subtitle: str, color: str, bg: str, copy_cmd) -> tk.Frame:
        wrap = tk.Frame(parent, bg=bg, highlightbackground=C_BORDER, highlightthickness=1)
        row = tk.Frame(wrap, bg=bg)
        row.pack(fill="x", padx=self._px(12), pady=(self._px(9), 0))
        tk.Label(row, text=title, bg=bg, fg=color, font=self._font(12, "bold")).pack(side="left")
        tk.Label(row, text=subtitle, bg=bg, fg=C_DIM, font=self._font(9)).pack(side="left", padx=8)
        self._button(row, "复制", copy_cmd, side="right", compact=True)
        return wrap

    def _text(self, parent, size: int) -> tk.Text:
        txt = tk.Text(parent, bg=parent.cget("bg"), fg=C_TEXT, relief="flat", bd=0,
                      highlightthickness=0, wrap="word", undo=False, cursor="arrow",
                      font=self._font(size), width=1, height=4, padx=self._px(8),
                      pady=self._px(7), spacing1=self._px(3), spacing3=self._px(5),
                      selectbackground="#315784", inactiveselectbackground="#315784",
                      state="disabled")
        txt.bind("<MouseWheel>", lambda e: (txt.yview_scroll(int(-e.delta / 120), "units"), "break")[1])
        self._tk_texts.append(txt)
        return txt

    @staticmethod
    def _scrollbar(parent, widget) -> ttk.Scrollbar:
        bar = ttk.Scrollbar(parent, orient="vertical", command=widget.yview)
        widget.configure(yscrollcommand=bar.set)
        return bar

    def _button(self, parent, text, command, *, accent=False, danger=False, side="left", compact=False):
        bg = C_ACCENT if accent else (C_ERR_SOFT if danger else C_BTN)
        fg = "#0c1420" if accent else (C_ERR if danger else C_TEXT)
        hover = "#88baff" if accent else C_BTN_HOVER
        btn = tk.Button(parent, text=text, command=command, bg=bg, fg=fg,
                        activebackground=hover, activeforeground=fg, bd=0, relief="flat",
                        font=self._font(9 if compact else 10, "bold"),
                        padx=self._px(7 if compact else 11), pady=self._px(3 if compact else 6),
                        cursor="hand2", highlightthickness=0, takefocus=0,
                        disabledforeground=C_DIM)
        btn.pack(side=side, padx=self._px(2))
        btn.bind("<Enter>", lambda _e: btn.configure(bg=hover), add="+")
        btn.bind("<Leave>", lambda _e: btn.configure(bg=bg), add="+")
        return btn

    def _check(self, parent, text, variable, command=None) -> tk.Checkbutton:
        return tk.Checkbutton(parent, text=text, variable=variable, command=command,
                              bg=parent.cget("bg"), fg=C_MUTED, selectcolor=C_CARD,
                              activebackground=parent.cget("bg"), activeforeground=C_TEXT,
                              highlightthickness=0, bd=0, font=self._font(9), cursor="hand2")

    # ------------------------------------------------------------ 自由提问会话
    def _build_ask_panel(self) -> None:
        self.qa_frame = tk.Frame(self.workspace, bg=C_ASK_BG,
                                 highlightbackground=C_BORDER, highlightthickness=1)
        head = tk.Frame(self.qa_frame, bg=C_ASK_BG)
        head.pack(fill="x", padx=self._px(12), pady=(self._px(8), self._px(4)))
        tk.Label(head, text="自由提问", bg=C_ASK_BG, fg=C_ACCENT,
                 font=self._font(12, "bold")).pack(side="left")
        tk.Label(head, text="通用知识为主 · 历史仅在相关时参考", bg=C_ASK_BG, fg=C_DIM,
                 font=self._font(9)).pack(side="left", padx=self._px(12))
        self._button(head, "收起  Esc", self.toggle_ask_panel, side="right", compact=True)
        self._button(head, "清空会话", self._clear_qa, side="right", compact=True)
        self._button(head, "复制最新回答", self._copy_latest_qa, side="right", compact=True)
        raw_memory = get(self.cfg, "brain.free_qa_memory", "smart")
        self.qa_memory_var = tk.BooleanVar(value=raw_memory not in (False, "off", "false", "0"))
        self._check(head, "相关记忆", self.qa_memory_var, self._toggle_qa_memory).pack(side="right", padx=8)
        body = tk.Frame(self.qa_frame, bg=C_ASK_BG)
        body.pack(fill="both", expand=True, padx=self._px(10))
        self.qa_box = self._text(body, int(get(self.cfg, "ui.font_qa", 13)))
        self.qa_box.configure(bg=C_ASK_BG)
        self.qa_box.pack(side="left", fill="both", expand=True)
        self._scrollbar(body, self.qa_box).pack(side="right", fill="y")
        self.qa_box.tag_configure("question", foreground=C_ACCENT, spacing1=8, spacing3=4)
        self.qa_box.tag_configure("answer", foreground=C_TEXT, spacing3=10)
        self.qa_box.tag_configure("pending", foreground=C_DIM)
        composer = tk.Frame(self.qa_frame, bg=C_ASK_BG)
        composer.pack(fill="x", padx=self._px(10), pady=(self._px(6), self._px(10)))
        entry_wrap = tk.Frame(composer, bg=C_CARD, highlightbackground=C_BORDER, highlightthickness=1)
        entry_wrap.pack(side="left", fill="x", expand=True)
        self.question_entry = tk.Text(entry_wrap, height=3, bg=C_CARD, fg=C_TEXT,
                                      insertbackground=C_ACCENT, relief="flat", bd=0,
                                      highlightthickness=0, wrap="word", undo=True,
                                      font=self._font(int(get(self.cfg, "ui.font_qa", 13))),
                                      padx=self._px(10), pady=self._px(7))
        self.question_entry.pack(fill="both", expand=True)
        self.question_entry.bind("<Return>", self._question_return)
        self.question_entry.bind("<Shift-Return>", self._question_newline)
        self.question_entry.bind("<Escape>", lambda _e: (self.toggle_ask_panel(), "break")[1])
        self._tk_texts.append(self.question_entry)
        side = tk.Frame(composer, bg=C_ASK_BG)
        side.pack(side="right", fill="y", padx=(self._px(8), 0))
        self._button(side, "发送", self._send_question, accent=True).pack(fill="x")
        tk.Label(side, text="Enter 发送\nShift+Enter 换行", bg=C_ASK_BG, fg=C_DIM,
                 font=self._font(8), justify="center").pack(pady=self._px(5))
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
            return
        try:
            self.workspace.add(self.qa_frame, minsize=230, stretch="never", height=330)
        except Exception:
            pass
        self._qa_visible = True
        self.root.after(40, lambda: self.question_entry and self.question_entry.focus_set())

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
        if not self._qa_order:
            w.insert("end", "问任何技术问题，不必局限在知识库。历史问答会保留在这里。\n", ("pending",))
        for rid in self._qa_order:
            turn = self._qa_turns[rid]
            w.insert("end", "你  " + turn["question"].strip() + "\n", ("question",))
            answer = turn["answer"].strip() or ("正在思考…" if not turn["done"] else "未返回内容")
            if turn.get("error"):
                answer += "\n" + turn["error"]
            w.insert("end", answer + "\n", ("answer" if turn["answer"] else "pending",))
        w.configure(state="disabled")
        w.see("end")

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
            text = f"最新 · 已存 {count} 组" if count else "最新"
        else:
            text = f"历史 {self._history_index + 1}/{count}"
        self._history_status_lbl.configure(text=text, fg=C_WARM if self._frozen else C_DIM)
        prev_ok = bool(count) and not self._frozen and self._history_index != 0
        next_ok = bool(count) and not self._frozen and self._history_index is not None
        self._btn["history_prev"].configure(state="normal" if prev_ok else "disabled")
        self._btn["history_next"].configure(state="normal" if next_ok else "disabled")
        self._btn["freeze"].configure(text="解除定格  F3" if self._frozen else "定格  F3")

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
        self.state_lbl.configure(text=status_text(self._listening, bool(latest.asr_ready), True))
        self.dot.configure(fg=C_OK if self._listening else C_DIM)
        self.delay_lbl.configure(text=f"终句 {self._delay:.1f}s" if self._delay else "延迟 -")
        self.dev_lbl.configure(text=short_device(self._device, 30))
        self._refresh_listen_btn()

    def _refresh_listen_btn(self) -> None:
        if "listen" in self._btn:
            self._btn["listen"].configure(text="Ⅱ 暂停聆听  F8" if self._listening else "▶ 开始聆听  F8")

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
                self.app_title_lbl.configure(text=f"{app_name} · {scenario_label(self.cfg)}")
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
        win.title("设置 · Meeting Copilot")
        win.configure(bg=C_BG)
        win.resizable(True, True)
        win.minsize(440, 600)
        frame = tk.Frame(win, bg=C_BG)
        frame.pack(fill="both", expand=True, padx=20, pady=16)
        tk.Label(frame, text="设置", bg=C_BG, fg=C_TEXT, font=self._font(15, "bold")).pack(anchor="w", pady=(0, 12))
        scenario_var = self._combo(frame, "使用场景（立即生效）", scenario_label(self.cfg), ["组会", "面试"])
        devices, _ = list_output_devices_safe(self.log)
        dev_var = self._combo(frame, "会议输出设备", str(get(self.cfg, "audio.device", "auto")), ["auto"] + devices)
        asr_var = self._combo(frame, "识别模型（重启后生效）", str(get(self.cfg, "asr.model", "small")),
                              ["small", "large-v3-turbo"])
        scr_var = self._combo(frame, "初始屏幕", str(get(self.cfg, "ui.screen", "secondary")),
                              ["primary", "secondary"])
        size_var = self._combo(frame, "窗口预设（也可拖边框 / 系统最大化）", "普通窗口",
                               ["普通窗口", "系统最大化", "2/3 屏幕", "1/2 屏幕"])
        font_var = self._combo(frame, "界面缩放", f"{int(round(self._font_scale*100))}%",
                               ["80%", "90%", "100%", "110%", "125%", "140%", "150%"])
        top_var = tk.BooleanVar(value=bool(get(self.cfg, "ui.split_topmost", False)))
        self._check(frame, "窗口始终置顶", top_var).pack(anchor="w", pady=8)
        tk.Label(frame, text="small 为实时档；turbo 更慢，不保证所有片段更准。",
                 bg=C_BG, fg=C_DIM, font=self._font(9)).pack(anchor="w", pady=4)
        bar = tk.Frame(frame, bg=C_BG)
        bar.pack(fill="x", pady=(14, 0))
        def save():
            scenario = "interview" if scenario_var.get() == "面试" else "meeting"
            try:
                self.bus.command("apply_settings", device=dev_var.get() or "auto",
                                 model=asr_var.get(), screen=scr_var.get(), scenario=scenario)
            except Exception:
                pass
            self.apply_settings(screen=scr_var.get(), scenario=scenario)
            self._apply_size_mode({"系统最大化": "full", "2/3 屏幕": "2/3", "1/2 屏幕": "1/2"}.get(size_var.get(), "normal"))
            self.root.attributes("-topmost", bool(top_var.get()))
            self.cfg.setdefault("ui", {})["split_topmost"] = bool(top_var.get())
            self.apply_font_scale(int(font_var.get().replace("%", "")) / 100)
            self._close_settings()
        self._button(bar, "保存", save, accent=True, side="right")
        self._button(bar, "取消", self._close_settings, side="right")
        win.protocol("WM_DELETE_WINDOW", self._close_settings)

    def _combo(self, parent, label: str, value: str, values: list[str]) -> tk.StringVar:
        tk.Label(parent, text=label, bg=C_BG, fg=C_MUTED, font=self._font(10)).pack(anchor="w", pady=(8, 3))
        var = tk.StringVar(value=value)
        ttk.Combobox(parent, textvariable=var, values=values, state="readonly",
                     font=self._font(10)).pack(fill="x", ipady=4)
        return var

    def _close_settings(self) -> None:
        if self._settings_win is not None:
            try:
                self._settings_win.destroy()
            except Exception:
                pass
        self._settings_win = None

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
        checks.extend([
            ("原生窗口可自由拉伸", ui.root.resizable() == (True, True) and not ui.root.overrideredirect()),
            ("看屏等动作按钮可用", all(ui._btn[k].cget("state") == "normal" for k in ("listen", "capture", "script"))),
            ("左右分栏可拖动", isinstance(ui.main_pane, tk.PanedWindow)),
            ("F2 展开多行提问区", ui._qa_visible and isinstance(ui.question_entry, tk.Text)),
            ("两轮问题均已发送", len(sent) == 2 and "第二轮" in sent[1]),
            ("问答历史完整保留", all(x in ui.qa_box.get("1.0", "end") for x in ("GAN", "第二轮问题", "失败案例"))),
            ("实时字幕已渲染", "多模态视频生成" in ui.transcript.get("1.0", "end")),
            ("应答与照读稿已渲染", "第三轮" in ui.answer_box.get("1.0", "end") and "第三组照读稿" in ui.script_box.get("1.0", "end")),
        ])
        ui.apply_settings(scenario="interview")
        ui.root.update_idletasks()
        checks.append(("场景可即时切换为面试", scenario_label(ui.cfg) == "面试" and
                       ui.root.title().endswith("面试")))
        ui.apply_settings(scenario="meeting")
        ui.root.update_idletasks()
        checks.append(("场景可切回组会", scenario_label(ui.cfg) == "组会" and
                       ui.root.title().endswith("组会")))
        ui.root.state("normal")
        ui.root.geometry("1120x760+40+40")
        ui.root.update_idletasks()
        checks.append(("普通窗口尺寸可调整", ui.root.winfo_width() == 1120 and ui.root.winfo_height() == 760))
        ui.root.state("zoomed")
        ui.root.update()
        checks.append(("支持系统最大化", ui.root.state() == "zoomed"))
        ui.root.state("normal")
        ui.root.geometry("1380x900+20+20")
        ui.root.attributes("-topmost", True)
        ui.root.lift()
        ui.root.focus_force()
        ui.root.update()
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
