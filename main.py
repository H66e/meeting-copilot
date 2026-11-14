# -*- coding: utf-8 -*-
"""Meeting Copilot · 主程序

用法：
    python main.py              # 正常启动浮窗
    python main.py --fake       # 无音频假数据演示（自检 UI 管线）
    python main.py --no-ui      # 无界面控制台模式（调试听写/大脑链路）
    python main.py --check      # 只做环境自检
"""
from __future__ import annotations

import argparse
import queue
import sys
import threading
import time
import traceback
from typing import Optional

import numpy as np

from config import get, init_console, load_config
from contracts import (KIND_ANSWER, KIND_FREE_QA, KIND_HINT, KIND_MINUTES, KIND_SCRIPT,
                       KIND_SLIDE, KIND_SUMMARY, LANE_DEEP, LANE_FAST, SAMPLE_RATE,
                       BrainEvent, BrainRequest, EventBus, Status, TranscriptSegment, fmt_ts)
from logging_setup import get_logger
from session_store import SessionStore
from scenarios import normalize_scenario, scenario_label

# ---- 可选模块：任何一个缺失都允许降级运行 ----
MODULES: dict[str, object] = {}
for name, modname, attr in [
    ("audio", "audio_capture", "LoopbackCapture"),
    ("asr", "asr_engine", "AsrEngine"),
    ("brain", "brain", "Brain"),
    ("vision", "vision_reader", "VisionReader"),
    ("kb", "knowledge_base", "KnowledgeBase"),
    ("hotkeys", "hotkeys", "HotkeyManager"),
]:
    try:
        m = __import__(modname, fromlist=[attr])
        MODULES[name] = getattr(m, attr)
    except Exception as e:  # noqa: BLE001
        MODULES[name] = None
        _import_err = f"{modname}: {type(e).__name__}: {e}"


class TeeBus:
    """包一层 EventBus：把已完成的 deep 结果沉淀进会话记录，再转发给 UI。"""

    def __init__(self, bus: EventBus, session: SessionStore, on_done=None):
        self.bus = bus
        self.session = session
        self.on_done = on_done

    def emit_brain(self, ev: BrainEvent) -> None:
        try:
            self.session.add_brain_event(ev)
            if ev.done and self.on_done:
                self.on_done(ev)
        except Exception:
            pass
        self.bus.emit_brain(ev)

    def __getattr__(self, item):
        return getattr(self.bus, item)


class App:
    def __init__(self, cfg: dict, fake: bool = False, no_ui: bool = False):
        self.cfg = cfg
        self.fake = fake
        self.no_ui = no_ui
        self.log = get_logger("main")
        self.bus = EventBus()
        self.session = SessionStore(cfg, self.log)
        self.tee = TeeBus(self.bus, self.session, on_done=self._on_brain_done)

        self.kb = None
        self.brain = None
        self.vision = None
        self.asr = None
        self.capture = None
        self.hotkeys = None
        self.ui = None

        self._asr_q: "queue.Queue" = queue.Queue(maxsize=128)
        self._work_q: "queue.PriorityQueue" = queue.PriorityQueue()
        self._work_seq = 0
        self._free_qa_history: list[tuple[str, str]] = []
        self._free_qa_pending: list[str] = []
        self._script_q: "queue.Queue" = queue.Queue(maxsize=4)
        self._vision_q: "queue.Queue" = queue.Queue(maxsize=4)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._status = Status()
        self._t0 = time.time()
        self._last_hint = time.time()
        self._last_summary = time.time()
        self._last_answer = 0.0
        # 疑问句短暂停留合并：等待老师续说，避免一次停顿就生成一套新答案。
        self._question_lock = threading.RLock()
        self._pending_question = ""
        self._pending_question_deadline = 0.0
        self._last_vision = 0.0
        self._last_seg_ts = 0.0
        self._last_term = 0.0
        self._last_script = 0.0
        # 流式草稿字幕（边说边出字）：默认关，开启会让终句延迟多 ~0.5s
        self._partial_enabled = bool(get(cfg, "asr.streaming_partial", False))
        self._frame_buf: list = []
        self._frame_lock = threading.Lock()
        self._last_voice = 0.0
        self._last_partial = 0.0
        self._voice_start = 0.0
        self._last_partial_text = ""
        self._last_delay = 0.0
        self._capture_t0 = 0.0
        self._pending_export = False
        self._minutes_requested = False
        self._listening = False
        self._last_rtf = 0.0
        self._asr_ready = False
        self._llm_ok = True
        self._last_msg = ""
        self._msg_until = 0.0
        self._fake_i = 0

        self._init_modules()

    # ------------------------------------------------------------ 初始化
    def _init_modules(self) -> None:
        cfg, log = self.cfg, self.log
        missing = [k for k, v in MODULES.items() if v is None]
        if missing:
            log.warning("以下模块暂不可用（降级运行）: %s", missing)

        if MODULES["kb"]:
            try:
                self.kb = MODULES["kb"](cfg, log)
                ok = self.kb.load()
                log.info("知识库加载: %s %s", "OK" if ok else "空库(未构建)", self.kb.stats())
            except Exception:
                log.warning("知识库初始化失败\n%s", traceback.format_exc())
                self.kb = None

        if MODULES["brain"]:
            try:
                self.brain = MODULES["brain"](cfg, self.tee, self.kb, log)
            except Exception:
                log.warning("大脑初始化失败\n%s", traceback.format_exc())
                self.brain = None

        if MODULES["asr"]:
            try:
                self.asr = MODULES["asr"](cfg, log)
            except Exception:
                log.warning("ASR 初始化失败\n%s", traceback.format_exc())
                self.asr = None

        # 草稿字幕用独立 ASR 引擎：仅在启用"边说边出字"时才加载（省内存/启动时间）
        self.asr_partial = None
        if self._partial_enabled and MODULES["asr"] and self.asr is not None:
            try:
                pcfg = dict(cfg)
                p_asr = dict(cfg.get("asr") or {})
                p_asr["cpu_threads"] = int(get(cfg, "asr.partial_threads", 6) or 6)
                p_asr["model"] = str(get(cfg, "asr.partial_model", "base") or "base")
                p_asr["vad_filter"] = bool(get(cfg, "asr.partial_vad_filter", False))
                pcfg["asr"] = p_asr
                self.asr_partial = MODULES["asr"](pcfg, get_logger("asr.partial"))
            except Exception:
                log.warning("草稿 ASR 初始化失败\n%s", traceback.format_exc())
                self.asr_partial = None

        if MODULES["vision"] and self.brain is not None:
            try:
                self.vision = MODULES["vision"](cfg, self.tee, self.brain.client, log)
            except Exception:
                log.warning("视觉模块初始化失败\n%s", traceback.format_exc())
                self.vision = None

        if MODULES["audio"] and not self.fake:
            try:
                self.capture = MODULES["audio"](cfg, self.bus, self._on_utterance, log,
                                                on_frame=self._on_frame)
            except Exception:
                log.warning("录音模块初始化失败\n%s", traceback.format_exc())
                self.capture = None

        # ---- UI：按 ui.layout 选择（split=副屏左右分栏新界面，tabs=旧三标签界面）
        ui_actions = {
            "start_listen": lambda: self._handle_command({"cmd": "start_listen"}),
            "stop_listen": lambda: self._handle_command({"cmd": "stop_listen"}),
            "screenshot": lambda: self._handle_command({"cmd": "screenshot"}),
            "script": lambda: self._handle_command({"cmd": "script"}),
            "summary": lambda: self._handle_command({"cmd": "summary"}),
            "export": lambda: self._handle_command({"cmd": "export"}),
            "quit": lambda: self._handle_command({"cmd": "quit"}),
            "im_asked": lambda: self._handle_command({"cmd": "im_asked"}),
            "ask": lambda **kw: self._ask_free(str(kw.get("question") or "")),
            "toggle_listen": lambda: self._handle_command({"cmd": "toggle_listen"}),
            "apply_settings": lambda **kw: self._handle_command({"cmd": "apply_settings", **kw}),
        }
        if not self.no_ui:
            layout = str(get(cfg, "ui.layout", "split") or "split").strip().lower()
            ui_mod_name = "ui_split" if layout in ("split", "split2col", "wide", "dual") else "ui_overlay"
            ui_cls = None
            try:
                _m = __import__(ui_mod_name, fromlist=["OverlayUI"])
                ui_cls = getattr(_m, "OverlayUI")
            except Exception:
                log.warning("界面模块 %s 加载失败，回退旧界面\n%s", ui_mod_name, traceback.format_exc())
                try:
                    _m = __import__("ui_overlay", fromlist=["OverlayUI"])
                    ui_cls = getattr(_m, "OverlayUI")
                except Exception:
                    ui_cls = None
            if ui_cls is not None:
                try:
                    self.ui = ui_cls(cfg, self.bus, log)
                    self.ui.register_actions(ui_actions)
                    self.log.info("界面布局: %s", layout)
                except Exception:
                    log.warning("UI 初始化失败\n%s", traceback.format_exc())
                    self.ui = None
            else:
                log.warning("没有任何可用的界面模块，以 headless 模式运行")
                self.ui = None

        if MODULES["hotkeys"]:
            try:
                binds = {}
                for name, cmd in (("toggle_listen", "toggle_listen"),
                                  ("capture_slide", "screenshot"),
                                  ("summary", "summary"),
                                  ("toggle_visible", "hide"),
                                  ("im_asked", "im_asked"),
                                  ("freeze", "freeze"),
                                  ("free_ask", "ask_focus"),
                                  ("script", "script"),
                                  ("click_through", "clickthrough"),
                                  ("export_minutes", "export"),
                                  ("quit", "quit")):
                    key = get(cfg, f"hotkeys.{name}", "")
                    if key:
                        binds[str(key)] = (lambda c=cmd: (lambda: self.bus.command(c)))(cmd)
                self.hotkeys = MODULES["hotkeys"](binds, log)
            except Exception:
                log.warning("热键初始化失败\n%s", traceback.format_exc())
                self.hotkeys = None

    # ------------------------------------------------------------ 线程
    def _spawn(self, fn, name: str) -> None:
        t = threading.Thread(target=fn, name=name, daemon=True)
        t.start()
        self._threads.append(t)

    def _asr_load_worker(self) -> None:
        if not self.asr:
            return
        try:
            t0 = time.time()
            self.asr.load()
            self._asr_ready = True
            self.log.info("ASR 模型就绪，耗时 %.1fs", time.time() - t0)
            try:
                self.asr.warmup()
            except Exception:
                pass
            # 草稿引擎（独立实例，默认 base 小模型）一起加载，首句草稿才不用等模型
            if self._partial_enabled and self.asr_partial is not None:
                try:
                    tp = time.time()
                    self.asr_partial.load()
                    self.asr_partial.warmup()
                    self.log.info("草稿 ASR 就绪（%s），耗时 %.1fs",
                                  getattr(self.asr_partial, "model_name", "?"), time.time() - tp)
                except Exception:
                    self.log.warning("草稿 ASR 加载失败，回退用主引擎\n%s", traceback.format_exc())
                    self.asr_partial = None
            if self.capture is not None:
                self._capture_t0 = time.time()
                self.capture.start()
                self._listening = True
                self._notify("已开始聆听（WASAPI 回环）")
        except Exception:
            self.log.error("ASR 加载失败\n%s", traceback.format_exc())
            self._notify("ASR 加载失败：请查看日志")

    def _audio_start_worker(self) -> None:
        if self.capture is None:
            self._notify("没有可用的音频采集模块")
            return
        try:
            if not self._asr_ready and self.asr is not None:
                self._notify("ASR 模型仍在加载，稍等…")
                return
            self.capture.start()
            self._capture_t0 = time.time()
            self._listening = True
            self._notify("已开始聆听")
        except Exception as e:
            self.log.warning("启动采集失败: %s", e)
            self._notify(f"启动采集失败: {e}")

    def _on_utterance(self, chunk) -> None:
        try:
            self._asr_q.put_nowait(chunk)
        except queue.Full:
            self._notify("听写排队已满，丢弃一段（CPU 忙）")

    def _asr_worker(self) -> None:
        while not self._stop.is_set():
            try:
                chunk = self._asr_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if self.asr is None:
                continue
            try:
                t0 = time.time()
                seg = self.asr.transcribe(chunk)
                asr_secs = time.time() - t0
                self._last_rtf = getattr(self.asr, "last_rtf", 0.0) or 0.0
                if seg and seg.display_text.strip():
                    if self._capture_t0:
                        delay = time.time() - (self._capture_t0 + float(getattr(chunk, "t_end", 0.0)))
                        self._last_delay = max(0.0, delay)
                        self.log.info("终句延迟 %.2fs（音频 %.1fs，解码 %.2fs）",
                                      delay, float(getattr(chunk, "duration", 0.0)), asr_secs)
                    self.bus.emit_transcript(seg)
                    self.session.add_segment(seg)
                    self._after_transcript(seg)
                if asr_secs > 8:
                    self.log.warning("单句听写耗时 %.1fs（rtf=%.2f）", asr_secs, self._last_rtf)
            except Exception:
                self.log.warning("听写异常\n%s", traceback.format_exc())

    def _work_worker(self) -> None:
        """串行执行所有 LLM 请求（按优先级）；视觉读屏走独立线程，避免堵住应答思路。"""
        while not self._stop.is_set():
            try:
                _priority, _seq, req = self._work_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if isinstance(req, BrainRequest):
                if time.time() - req.created_at > 120:
                    self.log.info("请求过期丢弃: %s", req.kind)
                    continue
                try:
                    if self.brain is None:
                        continue
                    self.brain.handle(req)
                except Exception as e:  # noqa: BLE001
                    self.log.warning("大脑异常: %s\n%s", e, traceback.format_exc())
                    self.bus.emit_brain(BrainEvent(request_id=req.request_id, kind=req.kind,
                                                   lane=req.lane, delta="", done=True,
                                                   error=f"生成失败：{type(e).__name__}",
                                                   title=req.kind))

    def _script_worker(self) -> None:
        """照读稿独立通道：和应答思路并行生成，避免被点名时排队等太久。"""
        while not self._stop.is_set():
            try:
                req = self._script_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if time.time() - req.created_at > 120:
                continue
            try:
                if self.brain is not None:
                    self.brain.handle(req)
            except Exception as e:  # noqa: BLE001
                self.log.warning("照读稿异常: %s", e)
                self.bus.emit_brain(BrainEvent(request_id=req.request_id, kind=req.kind,
                                               lane=req.lane, delta="", done=True,
                                               error=f"生成失败：{type(e).__name__}",
                                               title="照读稿"))

    def _vision_worker(self) -> None:
        """独立视觉线程：F9 读屏 5~10s 不阻塞 deep 应答。"""
        while not self._stop.is_set():
            try:
                kw = self._vision_q.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if self.vision is not None:
                    self.vision.read_screen(question=kw.get("question", ""),
                                            context=kw.get("context", ""))
            except Exception:
                self.log.warning("读屏异常\n%s", traceback.format_exc())
                self._status.vision_busy = False
            finally:
                self._status.vision_busy = False

    def _scheduler_worker(self) -> None:
        """自动触发节奏 + 状态上报。"""
        last_status = 0.0
        while not self._stop.is_set():
            try:
                now = time.time()
                self._flush_pending_question(now)
                # 每 1 秒状态
                if now - last_status >= 1.0:
                    last_status = now
                    self._emit_status(now)
                if self._listening and not self.fake:
                    self._maybe_auto_requests(now)
                if self.fake:
                    self._fake_feed(now)
            except Exception:
                self.log.warning("调度异常\n%s", traceback.format_exc())
            time.sleep(0.25)

    def _maybe_auto_requests(self, now: float) -> None:
        cfg = self.cfg
        # 自动要点
        hint_every = get(cfg, "brain.hint_every_s", 22)
        if now - self._last_hint >= hint_every and self._has_new_text():
            self._last_hint = now
            self._enqueue(BrainRequest(kind=KIND_HINT, lane=LANE_FAST, priority=6,
                                       context=self.session.recent_text()))
        # 自动阶段小结
        summ_every = get(cfg, "brain.summary_every_s", 300)
        if now - self._last_summary >= summ_every:
            self._last_summary = now
            self._enqueue(BrainRequest(kind=KIND_SUMMARY, lane=LANE_DEEP, priority=7,
                                       context=self.session.recent_text(4000)))
        # 自动读屏
        vint = get(cfg, "vision.auto_interval_s", 0) or 0
        if vint > 0 and now - self._last_vision >= vint:
            self._last_vision = now
            self._request_vision(auto=True)

    def _fake_feed(self, now: float) -> None:
        """演示模式：每 6 秒造一句中文发言，跑通 UI+大脑链路。"""
        if not self._listening:
            self._listening = True
            self._asr_ready = True
            self._notify("演示模式：无需音频设备，正在喂入模拟发言", seconds=10)
        if now - getattr(self, "_fake_last", 0) < 6:
            return
        self._fake_last = now
        samples = [
            "老师：这次组会我们先让每个人讲一下这周进展，先从图像编辑那边开始。",
            "你：我这周主要在搭 RefCOCO 上的掩码动作实验框架，目前跑通了收缩和扩张两个动作。",
            "老师：你这个掩码收缩扩张的收益到底有多大？有没有做过对照实验？",
            "老师：另外我建议你们去看看最近那篇关于扩散修复 latent 混合的论文，结论挺关键的。",
            "同学：我们现在只有一个编辑器，是不是需要再加一个 FLUX 的对比？",
            "老师：下周三之前把人工评估的 protocol 定下来，数据划分也要重新过一遍。",
        ]
        text = samples[self._fake_i % len(samples)]
        self._fake_i += 1
        seg = TranscriptSegment(seg_id=self._fake_i, t_start=now - self._t0 - 4,
                                t_end=now - self._t0, text=text)
        self.bus.emit_transcript(seg)
        self.session.add_segment(seg)
        self._after_transcript(seg)

    # ------------------------------------------------------------ 转录后处理
    def _has_new_text(self) -> bool:
        """自上次自动要点之后是否有新发言（避免会议室安静时刷屏）。"""
        return self._last_seg_ts > self._last_hint

    def _after_transcript(self, seg: TranscriptSegment, now: Optional[float] = None) -> None:
        if self.brain is None:
            return
        now = time.time() if now is None else float(now)
        self._last_seg_ts = now
        self._last_partial_text = ""   # 正式句到了，清掉草稿一致性缓存
        text = str(seg.display_text or "").strip()
        if not text:
            return
        cfg = self.cfg
        # 1) 叫到名字 => 高优先级立即应答，不等待合并窗口。
        names = get(cfg, "app.user_names", []) or []
        if any(n and n in text for n in names):
            if now - self._last_answer > 15:
                self._cancel_pending_question()
                self._last_answer = now
                self._notify("听到你的名字，正在准备回答思路 + 照读稿…")
                self._enqueue_answer_pair(text, self.session.recent_text(),
                                          force_script=True, priority=1)
            return
        # 2) 疑问句先等待短暂续说；窗口内的新字幕无论是否再带问号都并入同一题。
        kws = get(cfg, "brain.question_keywords", []) or []
        with self._question_lock:
            has_pending = bool(self._pending_question)
        if (has_pending or any(k and k in text for k in kws)) and now - self._last_answer > 35:
            self._queue_question_answer(text, now)
            return
        # 3) 英文术语 => 快脑解释（限频 60s）
        if now - getattr(self, "_last_term", 0) > 60:
            import re
            terms = re.findall(r"[A-Za-z][A-Za-z\-]{3,}(?:\s+[A-Za-z][A-Za-z\-]{3,})?", text)
            if terms and any(len(t) > 8 for t in terms):
                self._last_term = now
                self._enqueue(BrainRequest(kind="term", lane=LANE_FAST, priority=8,
                                           payload="；".join(sorted(set(terms))[:3]),
                                           context=text))

    def _queue_question_answer(self, text: str, now: float) -> None:
        """本地 debounce，不调用 LLM 判断；每段续说都会重置截止时间。"""
        wait_s = max(0.0, float(get(self.cfg, "brain.question_merge_s", 2.0) or 0.0))
        if wait_s <= 0:
            self._cancel_pending_question()
            self._last_answer = now
            self._enqueue_answer_pair(text, self.session.recent_text(),
                                      force_script=False, priority=2)
            return
        with self._question_lock:
            self._pending_question = "\n".join(
                part for part in (self._pending_question, text) if part).strip()
            self._pending_question_deadline = now + wait_s

    def _flush_pending_question(self, now: Optional[float] = None, *, force: bool = False) -> bool:
        """到期后只入队一次；返回是否真正触发了回答。"""
        now = time.time() if now is None else float(now)
        with self._question_lock:
            if not self._pending_question:
                return False
            if not force and now < self._pending_question_deadline:
                return False
            question = self._pending_question
            self._pending_question = ""
            self._pending_question_deadline = 0.0
        self._last_answer = now
        self._enqueue_answer_pair(question, self.session.recent_text(),
                                  force_script=False, priority=2)
        return True

    def _cancel_pending_question(self) -> None:
        with self._question_lock:
            self._pending_question = ""
            self._pending_question_deadline = 0.0

    def _enqueue(self, req: BrainRequest) -> None:
        """按优先级入队；主动提问优先于自动小结，同优先级保持先来后到。"""
        if self.brain is None:
            return
        try:
            self._work_seq += 1
            self._work_q.put_nowait((int(req.priority), self._work_seq, req))
        except queue.Full:
            self.log.warning("大脑队列满，丢弃 %s", req.kind)

    # ------------------------------------------------------------ 照读稿
    def _enqueue_answer_pair(self, payload: str, context: str, *,
                             force_script: bool = False, priority: int = 1) -> None:
        """一次触发同时排两条：应答思路（要点）+ 照读稿（可直接照着念的逐字稿）。

        两条走不同队列/线程并行生成：被点名时不用等完一份再来另一份。
        """
        self._enqueue(BrainRequest(kind=KIND_ANSWER, lane=LANE_DEEP, priority=priority,
                                   payload=payload, context=context))
        now = time.time()
        if force_script or now - self._last_script > 90:
            self._last_script = now
            self._enqueue_script_request(BrainRequest(
                kind=KIND_SCRIPT, lane=LANE_DEEP, priority=priority,
                payload=payload, context=context))

    def _enqueue_script_request(self, req: BrainRequest) -> None:
        try:
            self._script_q.put_nowait(req)
        except queue.Full:
            self.log.info("照读稿队列满，丢弃")

    def _enqueue_script(self) -> None:
        """F5：随时要一段可以直接照着念的稿子。"""
        self._last_script = time.time()
        self._notify("正在准备可以直接照着念的稿子…")
        self._enqueue_script_request(BrainRequest(
            kind=KIND_SCRIPT, lane=LANE_DEEP, priority=1,
            payload="现在需要一段可以直接照着念的发言稿（我会直接开口读）。",
            context=self.session.recent_text()))

    # ------------------------------------------------------------ 自由提问
    def _ask_free(self, question: str) -> None:
        """主动提问优先处理，并带少量既往问答，支持“继续展开”等追问。"""
        q = str(question or "").strip()
        if not q:
            return
        self.log.info("自由提问：%s", q[:120])
        self._notify("正在回答你的提问…", 4)
        parts: list[str] = []
        if self._free_qa_history:
            previous = "\n\n".join(f"我此前问：{question0}\n此前回答：{answer0}"
                                   for question0, answer0 in self._free_qa_history[-3:])
            parts.append("【既往自由问答（仅供理解追问）】\n" + previous[-3600:])
        recent = self.session.recent_text(1400)
        if recent:
            parts.append("【会议最近转写】\n" + recent)
        self._free_qa_pending.append(q)
        self._enqueue(BrainRequest(kind=KIND_FREE_QA, lane=LANE_DEEP, priority=0,
                                   payload=q, context="\n\n".join(parts)))

    # ------------------------------------------------------------ 流式草稿字幕
    def _on_frame(self, frame, t_start: float) -> None:
        """采集线程回调：缓存最近音频帧，供"边说边出字"的草稿字幕使用。"""
        try:
            with self._frame_lock:
                self._frame_buf.append((float(t_start), frame))
                if len(self._frame_buf) > 170:      # 约 17 秒
                    del self._frame_buf[:70]
            if frame is not None and getattr(frame, "size", 0):
                rms = float(np.sqrt(np.mean(np.square(frame))))
                if rms > 0.006:
                    now = time.time()
                    if now - self._last_voice > 0.9:   # 新一句开始
                        self._voice_start = float(t_start)
                        self._last_partial_text = ""
                        # 立刻给 UI 一个"…"占位：听感上从"说完才出字"变成"张嘴就有反应"
                        if self._partial_enabled and self._listening:
                            try:
                                self.bus.emit_transcript(TranscriptSegment(
                                    seg_id=-1, t_start=self._voice_start,
                                    t_end=self._voice_start, text="…", is_final=False))
                            except Exception:
                                pass
                    self._last_voice = now
        except Exception:
            pass

    def _partial_worker(self) -> None:
        """边说边出字：语音进行中高频刷新草稿；正式句到达后 UI 自动替换。

        两个提速点：
        1) 用**独立 ASR 引擎**（self.asr_partial）解码草稿，正式句解码不排它的队；
        2) 草稿只解最近 6 秒，且用 without_timestamps，单次 ~0.9~1.1s。

        借鉴 ufal/whisper_streaming 的 LocalAgreement 思路（简化版）：只有和上一次
        草稿"前缀一致"的部分才展示，避免短窗解码幻觉在屏幕上乱跳。
        """
        interval = float(get(self.cfg, "asr.partial_interval_s", 0.35) or 0.35)
        while not self._stop.is_set():
            time.sleep(0.12)
            try:
                if not self._partial_enabled:
                    continue
                if not self._listening or not self._asr_ready:
                    continue
                eng = self.asr_partial if getattr(self.asr_partial, "is_ready", False) else self.asr
                if eng is None or not eng.is_ready:
                    continue
                now = time.time()
                if now - self._last_voice > 2.2:     # 已静音：等正式句，不浪费 CPU
                    continue
                if now - self._last_partial < interval:
                    continue
                if self._asr_q.qsize() > 0:          # 正式句在排队：优先保正式延迟
                    continue
                with self._frame_lock:
                    frames = list(self._frame_buf)
                if len(frames) < 7:                  # 不足 0.7 秒音频
                    continue
                last_t = frames[-1][0]
                window = float(get(self.cfg, "asr.partial_window_s", 5.0) or 5.0)
                sel = [f for (t, f) in frames if t >= last_t - window]
                if len(sel) < 7:
                    continue
                audio = np.concatenate(sel)
                if len(audio) < int(SAMPLE_RATE * 0.7):
                    continue
                self._last_partial = now
                seg = eng.transcribe_audio(audio, t0=max(0.0, self._voice_start))
                text = str(getattr(seg, "display_text", "") or "").strip()
                if not text:
                    continue
                prev = self._last_partial_text
                self._last_partial_text = text
                if prev:
                    n = 0
                    for a, b in zip(prev, text):
                        if a != b:
                            break
                        n += 1
                    agreed = text[:n].strip()
                else:
                    agreed = text
                if len(agreed) < 4:
                    continue
                shown = agreed if len(agreed) >= len(text) - 2 else agreed + "…"
                seg.text = shown
                seg.text_fixed = shown
                seg.is_final = False
                seg.t_end = last_t
                self.bus.emit_transcript(seg)
            except Exception as e:  # 草稿失败绝不影响正式听写链路
                self.log.debug("草稿字幕异常（忽略）: %s", e)

    def _request_vision(self, auto: bool = False) -> None:
        if self.vision is None:
            self._notify("视觉模块不可用（缺少模块或大脑）")
            return
        if self._status.vision_busy:
            self.log.info("读屏正在进行，跳过")
            return
        self._status.vision_busy = True
        self._notify("正在看屏幕…")
        kw = {"question": "", "context": self.session.recent_text(), "auto": auto}
        try:
            self._vision_q.put_nowait(kw)
        except queue.Full:
            self._status.vision_busy = False
            self._notify("读屏排队已满，稍后再试")

    # ------------------------------------------------------------ 大脑回调
    def _on_brain_done(self, ev: BrainEvent) -> None:
        if ev.error:
            self._llm_ok = False
            self._notify(f"模型出错：{ev.error[:80]}")
        else:
            self._llm_ok = True
        if ev.kind == KIND_FREE_QA and self._free_qa_pending:
            question = self._free_qa_pending.pop(0)
            answer = str(ev.full_text or "").strip()
            if answer and not ev.error:
                self._free_qa_history.append((question, answer))
                self._free_qa_history = self._free_qa_history[-6:]
        # 纪要素材由 TeeBus 统一沉淀，这里只处理导出联动
        if ev.kind == KIND_MINUTES and not ev.error:
            self.session.add_note("最终纪要", ev.full_text)
            if self._pending_export:
                self._pending_export = False
                self._export()

    # ------------------------------------------------------------ 命令处理
    def _handle_command(self, cmd: dict) -> None:
        name = cmd.get("cmd")
        self.log.info("命令: %s", name)
        if name in ("grab", "screenshot"):
            self._request_vision()
        elif name in ("start_listen",):
            self._spawn(self._audio_start_worker, "audio-start")
        elif name in ("stop_listen", "pause"):
            if self.capture is not None:
                self.capture.pause()
            self._listening = False
            self._notify("已暂停聆听")
        elif name in ("resume",):
            if self.capture is not None:
                self.capture.resume()
            self._listening = True
            self._notify("已继续聆听")
        elif name == "toggle_listen":
            cmd2 = "pause" if self._listening else "resume"
            self._handle_command({"cmd": cmd2})
        elif name == "summary":
            self._notify("正在生成阶段小结…")
            self._enqueue(BrainRequest(kind=KIND_SUMMARY, lane=LANE_DEEP, priority=3,
                                       context=self.session.recent_text(4000)))
        elif name == "im_asked":
            self._cancel_pending_question()
            self._notify("收到：正在准备回答思路 + 照读稿…")
            self._enqueue_answer_pair(
                "对方刚点我/问我。请直接针对最近这段讨论给出我的回答，"
                "不要确认收到、不要寒暄、不要说明你准备怎么做、不要要求我提供内容，只给答案。",
                self.session.recent_text(), force_script=True, priority=1)
        elif name in ("script", "read_aloud"):
            self._enqueue_script()
        elif name == "ask_focus":
            self.log.info("自由提问聚焦由 UI 处理（无界面模式下忽略）")
        elif name == "clickthrough":
            self.log.info("鼠标穿透命令由 UI 内部处理（无界面模式下忽略）")
        elif name == "export":
            self._pending_export = True
            self._notes_for_export()
        elif name == "apply_settings":
            self._apply_settings(cmd)
        elif name == "quit":
            self._stop.set()
        else:
            self.log.info("未知命令: %s", cmd)

    def _apply_settings(self, cmd: dict) -> None:
        try:
            if "device" in cmd and self.capture is not None:
                self.capture.set_device(cmd["device"])
                self._notify(f"音频设备切换到 {cmd['device']}")
            if "model" in cmd and self.asr is not None:
                self.cfg["asr"]["model"] = cmd["model"]
                self._notify("ASR 模型改动需重启生效")
            if "screen" in cmd:
                self.cfg["ui"]["screen"] = cmd["screen"]
            if "scenario" in cmd:
                scenario = normalize_scenario(cmd["scenario"])
                self.cfg.setdefault("app", {})["scenario"] = scenario
                # Brain 与 VisionReader 共享同一份 cfg；新提示词从下一次请求生效。
                self._notify(f"已切换为{scenario_label(self.cfg)}模式")
            if "free_qa_memory" in cmd and self.brain is not None:
                mode = cmd["free_qa_memory"]
                self.cfg.setdefault("brain", {})["free_qa_memory"] = mode
                self.brain.cfg.setdefault("brain", {})["free_qa_memory"] = mode
                self._notify("自由提问相关记忆：" + ("按需检索" if mode else "已关闭"))
        except Exception as e:
            self.log.warning("应用设置失败: %s", e)

    def _notes_for_export(self) -> None:
        """先生成最终纪要（deep），完成后自动导出；失败则直接导出原始纪要。"""
        if self.brain is None or self._minutes_requested:
            self._export()
            return
        self._minutes_requested = True
        full = self.session.full_text()
        self._notify("正在生成最终纪要…")
        self._enqueue(BrainRequest(kind=KIND_MINUTES, lane=LANE_DEEP, priority=2,
                                   context=full[:9000]))
        # 兜底：45 秒后不管有没有结果都导出
        def _later():
            time.sleep(45)
            if self._pending_export:
                self._pending_export = False
                self._export()
        threading.Thread(target=_later, name="export-fallback", daemon=True).start()

    def _export(self) -> None:
        try:
            path = self.session.export_markdown()
            self._notify(f"纪要已导出：{path}")
        except Exception:
            self.log.warning("导出失败\n%s", traceback.format_exc())
            self._notify("导出失败，请查看日志")

    # ------------------------------------------------------------ 状态
    def _notify(self, msg: str, seconds: float = 6.0) -> None:
        self._last_msg = msg
        self._msg_until = time.time() + seconds
        self.log.info(msg)

    def _emit_status(self, now: float) -> None:
        st = self._status
        st.listening = self._listening
        st.asr_ready = self._asr_ready
        st.session_sec = now - self._t0
        st.last_rtf = self._last_rtf
        st.last_delay = self._last_delay
        st.pending_asr = self._asr_q.qsize()
        st.llm_ok = self._llm_ok
        st.kb_ok = bool(self.kb and self.kb.stats().get("chunks"))
        if self.capture is not None:
            try:
                st.audio_level = float(self.capture.get_level())
                st.device = self.capture.device_name
            except Exception:
                pass
        elif self.fake:
            st.device = "演示模式"
            st.audio_level = 0.35
        st.message = self._last_msg if now < self._msg_until else ""
        self.bus.emit_status(Status(**st.__dict__))

    # ------------------------------------------------------------ 主循环
    def run(self) -> int:
        self.log.info("Meeting Copilot启动 | 配置=%s | fake=%s | no_ui=%s",
                      self.cfg.get("_path"), self.fake, self.no_ui)
        self.hotkeys and self.hotkeys.start()

        # 命令消费：有 UI 时由 UI 消费；否则 main 自己消费
        if self.no_ui or self.ui is None:
            self._spawn(self._command_worker, "cmd")
        else:
            try:
                self.ui.start()
            except Exception:
                self.log.warning("UI 启动失败，转 headless\n%s", traceback.format_exc())
                self.ui = None
                self._spawn(self._command_worker, "cmd")

        self._spawn(self._asr_load_worker, "asr-load")
        self._spawn(self._asr_worker, "asr")
        self._spawn(self._work_worker, "work")
        self._spawn(self._script_worker, "script")
        self._spawn(self._vision_worker, "vision")
        self._spawn(self._scheduler_worker, "sched")
        self._spawn(self._partial_worker, "partial")
        if self.no_ui or self.ui is None:
            self._spawn(self._console_worker, "console")

        try:
            if self.ui is not None:
                self.ui.run()          # Tk mainloop，阻塞到关闭
            else:
                self._headless_loop()
        except KeyboardInterrupt:
            self.log.info("收到 Ctrl+C")
        finally:
            self.shutdown()
        return 0

    def _command_worker(self) -> None:
        while not self._stop.is_set():
            try:
                cmd = self.bus.commands.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._handle_command(cmd)
            except Exception:
                self.log.warning("命令处理失败\n%s", traceback.format_exc())

    def _console_worker(self) -> None:
        """headless 模式下把字幕/建议/状态打到控制台，方便调试与自动化测试。"""
        pending: dict[str, list[str]] = {}
        while not self._stop.is_set():
            try:
                seg = self.bus.transcripts.get(timeout=0.1)
                tag = "字幕" if bool(getattr(seg, "is_final", True)) else "草稿"
                print(f"[{tag} {fmt_ts(seg.t_start)}] {seg.display_text}", flush=True)
            except queue.Empty:
                pass
            try:
                while True:
                    ev = self.bus.brain_events.get_nowait()
                    if ev.delta:
                        pending.setdefault(ev.request_id, []).append(ev.delta)
                    if ev.done:
                        body = "".join(pending.pop(ev.request_id, [])) or (ev.full_text or "")
                        tag = f"[{ev.kind}/{ev.lane}]"
                        if ev.error:
                            print(f"{tag} 错误: {ev.error}", flush=True)
                        else:
                            print(f"{tag} {body[:1200]}", flush=True)
            except queue.Empty:
                pass
            time.sleep(0.05)

    def _headless_loop(self) -> None:
        if self.no_ui:
            print("=== headless 模式：命令输入（listen/pause/screenshot/summary/export/im_asked/quit）===")
            self._handle_command({"cmd": "start_listen"})
            while not self._stop.is_set():
                try:
                    line = sys.stdin.readline()
                    if not line:
                        time.sleep(0.5)
                        continue
                    self._handle_command({"cmd": line.strip() or "noop"})
                except Exception:
                    break
        else:
            while not self._stop.is_set():
                time.sleep(0.3)

    def shutdown(self) -> None:
        self.log.info("正在退出…")
        self._stop.set()
        try:
            if self.hotkeys is not None:
                self.hotkeys.stop()
        except Exception:
            pass
        try:
            if self.capture is not None:
                self.capture.stop()
        except Exception:
            pass
        try:
            self._export()
        except Exception:
            pass
        try:
            if self.ui is not None:
                self.ui.stop()
        except Exception:
            pass
        for t in self._threads:
            t.join(timeout=1.5)
        self.log.info("已退出。统计: %s", self.session.stats())


def main() -> int:
    ap = argparse.ArgumentParser(description="Meeting Copilot · 组会/面试实时助手")
    ap.add_argument("--fake", action="store_true", help="假数据演示（无需音频设备）")
    ap.add_argument("--no-ui", action="store_true", help="无界面控制台模式")
    ap.add_argument("--check", action="store_true", help="仅环境自检")
    ap.add_argument("--scenario", choices=("meeting", "interview"), default=None,
                    help="本次运行覆盖配置中的使用场景")
    ap.add_argument("--config", default=None, help="配置文件路径")
    args = ap.parse_args()

    init_console()
    cfg = load_config(args.config)
    if args.scenario:
        cfg.setdefault("app", {})["scenario"] = args.scenario
    if args.check:
        try:
            from tools.selfcheck import run_all
            return 0 if run_all(cfg) else 1
        except Exception as e:  # noqa: BLE001
            print(f"自检模块不可用: {e}")
            return 1

    app = App(cfg, fake=args.fake, no_ui=args.no_ui)
    return app.run()


if __name__ == "__main__":
    sys.exit(main())
