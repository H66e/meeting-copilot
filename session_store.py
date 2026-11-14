# -*- coding: utf-8 -*-
"""会话存储与纪要导出。"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from config import get
from contracts import BrainEvent, TranscriptSegment, fmt_ts
from scenarios import export_prefix


class SessionStore:
    def __init__(self, cfg: dict, log=None):
        self.cfg = cfg
        self.log = log
        self._lock = threading.Lock()
        self.segments: list[TranscriptSegment] = []
        self.notes: list[dict] = []          # {"t": "12:34", "kind": "summary"|"answer"|"slide", "text": ...}
        self.todos: list[str] = []
        self.started_at = time.time()
        self.dir = Path(get(cfg, "_dirs.exports", "exports"))
        self.dir.mkdir(parents=True, exist_ok=True)
        self.title = time.strftime(f"{export_prefix(cfg)}_%Y%m%d_%H%M")

    # ---------------- 写入
    def add_segment(self, seg: TranscriptSegment) -> None:
        with self._lock:
            self.segments.append(seg)

    def add_note(self, kind: str, text: str, ts: float | None = None) -> None:
        text = (text or "").strip()
        if not text:
            return
        with self._lock:
            self.notes.append({"t": fmt_ts((ts if ts is not None else time.time()) - self.started_at),
                               "kind": kind, "text": text})

    def add_todos(self, items: list[str]) -> None:
        with self._lock:
            for it in items:
                it = (it or "").strip(" -·•\t")
                if it and it not in self.todos:
                    self.todos.append(it)

    def add_brain_event(self, ev: BrainEvent) -> None:
        """把 deep lane 的成品文本沉淀进纪要素材。"""
        if not ev.done or ev.error or not (ev.full_text or "").strip():
            return
        if ev.kind in ("summary", "answer", "slide", "minutes"):
            self.add_note(ev.kind, ev.full_text)

    # ---------------- 读取
    def recent_text(self, max_chars: int = 2500) -> str:
        with self._lock:
            parts = [s.display_text for s in self.segments[-80:]]
        text = "\n".join(parts)
        return text[-max_chars:]

    def full_text(self) -> str:
        with self._lock:
            return "\n".join(s.display_text for s in self.segments)

    def stats(self) -> dict:
        with self._lock:
            n = len(self.segments)
        return {"segments": n, "notes": len(self.notes), "todos": len(self.todos),
                "elapsed_s": int(time.time() - self.started_at)}

    # ---------------- 导出
    def export_markdown(self, path: str | Path | None = None, extra_header: str = "") -> str:
        with self._lock:
            segs = list(self.segments)
            notes = list(self.notes)
            todos = list(self.todos)
        elapsed = int(time.time() - self.started_at)
        lines = [f"# {self.title}", ""]
        lines.append(f"- 时长：{elapsed // 3600:02d}:{elapsed % 3600 // 60:02d}:{elapsed % 60:02d}")
        lines.append(f"- 语音句数：{len(segs)}")
        lines.append(f"- 导出时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
        if extra_header:
            lines += ["", extra_header]
        if notes:
            lines += ["", "## 要点与建议（AI 记录）", ""]
            for n in notes[-40:]:
                lines.append(f"- **[{n['t']}] {n['kind']}**：{n['text'][:800]}")
        if todos:
            lines += ["", "## 待办", ""]
            lines += [f"- [ ] {t}" for t in todos]
        lines += ["", "## 完整转写", ""]
        for s in segs:
            lines.append(f"**[{fmt_ts(s.t_start)}]** {s.display_text}")
        md = "\n".join(lines) + "\n"
        out = Path(path) if path else self.dir / f"{self.title}_纪要.md"
        out.write_text(md, encoding="utf-8")
        # 同时存一份结构化 json，便于事后二次处理
        try:
            (self.dir / f"{self.title}_转写.json").write_text(
                json.dumps([{"t": round(s.t_start, 2), "text": s.display_text} for s in segs],
                           ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass
        if self.log:
            self.log.info("纪要已导出: %s", out)
        return str(out)
