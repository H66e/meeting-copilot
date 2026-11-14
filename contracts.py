# -*- coding: utf-8 -*-
"""组会助手 - 模块间契约（唯一事实来源 / single source of truth）

所有模块只通过本文件定义的 dataclass 与 EventBus 交互，避免并行开发时接口漂移。
禁止在此文件之外自行发明跨模块数据结构。
"""
from __future__ import annotations

import queue
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Optional

import numpy as np

# ---------------------------------------------------------------- 常量
SAMPLE_RATE = 16000
FRAME_MS = 100
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 1600

LANE_FAST = "fast"    # DeepSeek：低延迟短提示
LANE_DEEP = "deep"    # Claude Opus 5.5：深思考

KIND_HINT = "hint"              # 实时要点
KIND_ANSWER = "answer"          # 被点名/提问时给回答思路
KIND_SCRIPT = "script"          # 照读稿：可以直接照着念的逐字稿
KIND_FREE_QA = "free_qa"        # 用户自由提问（左下角输入框）的回答
KIND_SLIDE = "slide"            # 共享屏幕页面解读
KIND_TERM = "term"              # 英文术语解释
KIND_SCRIPT = "script"          # 照读稿：可以直接照着念的逐字稿
KIND_SUMMARY = "summary"        # 阶段小结
KIND_MINUTES = "minutes"        # 最终纪要


def now_str() -> str:
    return time.strftime("%H:%M:%S")


def fmt_ts(sec: float) -> str:
    """秒 -> mm:ss"""
    sec = max(0, int(sec))
    return f"{sec // 60:02d}:{sec % 60:02d}"


# ---------------------------------------------------------------- 数据
@dataclass
class AudioChunk:
    """一段已切分好的语音（一句话级别，0.3~12 秒）"""
    data: np.ndarray            # float32 mono 16k
    t_start: float              # 会话内起始秒
    t_end: float
    peak_rms: float = 0.0
    source: str = "loopback"

    @property
    def duration(self) -> float:
        return self.t_end - self.t_start


@dataclass
class TranscriptSegment:
    seg_id: int
    t_start: float
    t_end: float
    text: str
    is_final: bool = True
    rtf: float = 0.0
    source: str = "loopback"
    text_fixed: str = ""        # 后处理（繁->简/纠错）后的文本，空则用 text
    confidence: float = 0.0     # ASR 置信度；0 表示未知，低置信字幕由 UI 弱化标记

    @property
    def display_text(self) -> str:
        return self.text_fixed or self.text


@dataclass
class BrainRequest:
    kind: str                       # KIND_*
    lane: str = "auto"              # fast | deep | auto
    payload: str = ""               # 触发内容（问题、页面文字、术语）
    context: str = ""               # 附加上下文
    image_b64: str = ""             # 截图（KIND_SLIDE 用）
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    created_at: float = field(default_factory=time.time)
    priority: int = 5               # 数字越小越优先


@dataclass
class BrainEvent:
    """大脑 -> UI 的流式事件"""
    request_id: str
    kind: str
    lane: str
    delta: str = ""                 # 增量文本
    done: bool = False
    error: Optional[str] = None
    title: str = ""                 # 面板标题，如「要点」「应答思路」
    usage: dict = field(default_factory=dict)
    full_text: str = ""             # 累积文本（done 时填）


@dataclass
class Status:
    listening: bool = False
    asr_ready: bool = False
    device: str = ""
    audio_level: float = 0.0
    pending_asr: int = 0
    last_rtf: float = 0.0
    last_delay: float = 0.0     # 最近一句"说完 -> 字幕出来"的实际延迟（秒）
    llm_ok: bool = True
    kb_ok: bool = False
    session_sec: float = 0.0
    message: str = ""
    vision_busy: bool = False


# ---------------------------------------------------------------- 事件总线
class EventBus:
    """跨线程队列集合。UI 侧用 poll 消费，worker 侧用 put 生产。"""

    def __init__(self) -> None:
        self.transcripts: "queue.Queue[TranscriptSegment]" = queue.Queue()
        self.brain_events: "queue.Queue[BrainEvent]" = queue.Queue()
        self.status: "queue.Queue[Status]" = queue.Queue()
        self.commands: "queue.Queue[dict]" = queue.Queue()      # UI/热键 -> 主循环
        self.logs: "queue.Queue[str]" = queue.Queue()
        self._stop = False

    # ---- 生产侧便捷方法
    def emit_transcript(self, seg: TranscriptSegment) -> None:
        self.transcripts.put(seg)

    def emit_brain(self, ev: BrainEvent) -> None:
        self.brain_events.put(ev)

    def emit_status(self, st: Status) -> None:
        self.status.put(st)

    def command(self, cmd: str, **kw: Any) -> None:
        self.commands.put({"cmd": cmd, **kw})

    def log(self, msg: str) -> None:
        self.logs.put(f"[{now_str()}] {msg}")

    # ---- 停止
    def request_stop(self) -> None:
        self._stop = True

    @property
    def stopping(self) -> bool:
        return self._stop


# ---------------------------------------------------------------- 回调类型
OnUtterance = Callable[[AudioChunk], None]
OnDelta = Callable[[str], None]
OnEvent = Callable[[BrainEvent], None]
