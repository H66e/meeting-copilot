# -*- coding: utf-8 -*-
"""视觉读屏：PIL 截图（主屏 / 腾讯会议窗口）+ 缩放 + Claude 读图（流式）+ rapidocr 本地兜底。

对外契约（见 INTERFACES.md 第 4 节 B2）::

    vr = VisionReader(cfg, bus, brain.client)
    img, b64 = vr.grab("primary")            # 主屏；"wemeet_window" 截腾讯会议窗口
    ev = vr.read_screen(question="", context="")   # 返回 done 的 BrainEvent（已流式 emit）
    text = vr.ocr(img)                       # rapidocr 本地兜底

实现要点：
- 截图前设置进程 DPI 感知，按物理像素截整块虚拟桌面后按需裁剪，多屏/负坐标安全。
- 长边缩到 cfg.vision.max_width（默认 1280），PNG base64，OpenAI 兼容 image_url 格式。
- 视觉模型：cfg.vision.model 主模型，失败按 cfg.vision.fallback_models 依次重试；
  全失败且 cfg.vision.ocr_fallback=true 时用 rapidocr 本地 OCR，绝不让调用方转圈。
- 腾讯会议窗口：Win32 EnumWindows 找标题含「腾讯会议/WeMeet」或进程 WeMeet.exe 的窗口，
  取面积最大的可见窗口；找不到回退主屏（不报错）。
- 注意：屏幕截图是"所见即所得"，如果腾讯会议窗口被别的窗口挡住，截到的是挡住它的内容；
  UI 侧尽量在共享时保持会议窗口前台。
"""
from __future__ import annotations

import base64
import ctypes
import io
import os
import time
import uuid
from typing import Any

from PIL import Image, ImageGrab

from config import get
from contracts import (KIND_SLIDE, LANE_DEEP, BrainEvent, EventBus)
from logging_setup import get_logger
from scenarios import is_interview


def _ensure_dpi_aware() -> None:
    """Windows 高 DPI：让截图坐标与 Win32 窗口 rect 都是物理像素。"""
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _virtual_screen_rect() -> tuple[int, int, int, int]:
    """(x, y, w, h) 虚拟桌面（多屏拼接）物理像素。"""
    if os.name != "nt":
        return (0, 0, 0, 0)
    try:
        u = ctypes.windll.user32
        return (int(u.GetSystemMetrics(76)), int(u.GetSystemMetrics(77)),
                int(u.GetSystemMetrics(78)), int(u.GetSystemMetrics(79)))
    except Exception:
        return (0, 0, 0, 0)


def _primary_screen_rect() -> tuple[int, int]:
    if os.name != "nt":
        return (0, 0)
    try:
        u = ctypes.windll.user32
        return (int(u.GetSystemMetrics(0)), int(u.GetSystemMetrics(1)))
    except Exception:
        return (0, 0)


def _process_image_name(hwnd: int) -> str:
    """取窗口所属进程的完整 exe 路径；失败返回 ""。"""
    if os.name != "nt":
        return ""
    try:
        import ctypes.wintypes  # noqa: PLC0415

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        pid = ctypes.wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value or ""
            return ""
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return ""


def enum_windows() -> list[dict]:
    """枚举可见顶层窗口：[{"hwnd","title","rect","area","process"}]。失败返回 []。"""
    if os.name != "nt":
        return []
    out: list[dict] = []
    try:
        import ctypes.wintypes  # noqa: PLC0415

        user32 = ctypes.windll.user32
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def _cb(hwnd, _lparam):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                length = int(user32.GetWindowTextLengthW(hwnd))
                if length <= 0:
                    return True
                tbuf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, tbuf, length + 1)
                rect = ctypes.wintypes.RECT()
                if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    return True
                w = int(rect.right - rect.left)
                h = int(rect.bottom - rect.top)
                if w < 200 or h < 120:
                    return True
                out.append({
                    "hwnd": int(hwnd or 0),
                    "title": tbuf.value or "",
                    "rect": (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)),
                    "area": w * h,
                })
            except Exception:
                pass
            return True

        user32.EnumWindows(WNDENUMPROC(_cb), 0)
    except Exception:
        return out
    return out


def _is_wemeet_window(win: dict) -> bool:
    """只认腾讯会议**进程**（wemeetapp.exe / WeMeet.exe）。

    绝不能按标题判断——否则标题里恰好带「腾讯会议」的窗口（浏览器、聊天、
    本项目自己的会话窗口）会被误当成会议窗口截进来。真实会议窗口标题通常是
    「XXX的个人会议室」，并不含"腾讯会议"字样。
    """
    proc = (win.get("process") or "").lower()
    return "wemeet" in proc


def find_wemeet_window() -> dict | None:
    """找腾讯会议窗口（优先面积最大的那个，即会议主窗口）。找不到返回 None。"""
    wins = enum_windows()
    if not wins:
        return None
    for w in wins:
        w["process"] = _process_image_name(w["hwnd"])
    picked = [w for w in wins if _is_wemeet_window(w)]
    if not picked:
        return None
    # 过滤掉完全在屏幕外的窗口
    vx, vy, vw, vh = _virtual_screen_rect()
    if vw and vh:
        def _on_screen(w):
            l, t, r, b = w["rect"]
            return r > vx and b > vy and l < vx + vw and t < vy + vh

        on = [w for w in picked if _on_screen(w)]
        if on:
            picked = on
    # 过滤掉工具条/提示框这类小窗（会议主窗口至少 20 万像素）
    big = [w for w in picked if w.get("area", 0) >= 200000]
    return max(big or picked, key=lambda w: w["area"])


_SYSTEM_VISION = """你是 Meeting Copilot，帮助用户读懂在线组会或面试中共享的屏幕内容（PPT/文档/代码/实验结果页）。

请用中文输出，严格按四段，每段以段名开头：
【页面主题】一句话说清这页在讲什么；
【关键点】2~3 条，每条 ≤40 字，抓最重要的信息；
【公式/数字】只解读页面里真实出现的公式或数字，没有就写「本页无公式数字」；
【可能被问】3 个提问者可能就这页追问的问题（只写问题，不写答案）。

硬性要求：
1. 只解读页面上实际出现的内容，严禁编造页面没有的数字、指标、baseline、结论；
2. 画面模糊、被遮挡、文字太小或看不清，就直接写「这页看不清（原因）」，不要猜；
3. 不要推测页面没展示的内容，不要写"作者可能想说明…"这类发挥；
4. 页面上的数字与公式照原样转述，不要自行换算或改动。"""

_QUESTION_DEFAULT = "这是用户现在看到的屏幕，请按四段格式解读这一页，帮助用户准备回应。"


def _vision_system_prompt(cfg: dict) -> str:
    if is_interview(cfg):
        return _SYSTEM_VISION + "\n当前是面试场景，潜在追问应偏向项目经历、技术原理、方案权衡与岗位相关能力。"
    return _SYSTEM_VISION + "\n当前是研究组会场景，潜在追问应偏向实验依据、方法对比、结论边界与下一步。"


def _looks_blank(img) -> bool:
    """灰度极差很小 => 白屏/黑屏（PrintWindow 抓 GPU 窗口时可能返回空白）。"""
    try:
        lo, hi = img.convert("L").getextrema()
        return (hi - lo) < 8
    except Exception:
        return False


def _printwindow_grab(hwnd: int, w: int, h: int):
    """用 PrintWindow(PW_RENDERFULLCONTENT) 抓窗口自身画面。

    关键：直接渲染窗口内容，**即使窗口被别的窗口遮挡也能拿到会议画面**；
    之前的区域截图只能拍到最上层窗口（实测被编辑器/聊天窗口盖住时会读错内容）。
    失败/得到空白返回 None。
    """
    if os.name != "nt" or hwnd <= 0 or w <= 0 or h <= 0:
        return None
    import ctypes
    from ctypes import wintypes

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    hwnd_dc = mem_dc = bmp = 0
    try:
        hwnd_dc = user32.GetWindowDC(hwnd)
        if not hwnd_dc:
            return None
        mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
        bmp = gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
        if not mem_dc or not bmp:
            return None
        gdi32.SelectObject(mem_dc, bmp)
        ok = user32.PrintWindow(hwnd, mem_dc, 2)   # PW_RENDERFULLCONTENT
        if not ok:
            ok = user32.PrintWindow(hwnd, mem_dc, 0)
        if not ok:
            return None
        bi = BITMAPINFO()
        bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.bmiHeader.biWidth = w
        bi.bmiHeader.biHeight = -h
        bi.bmiHeader.biPlanes = 1
        bi.bmiHeader.biBitCount = 32
        bi.bmiHeader.biCompression = 0
        buf = ctypes.create_string_buffer(w * h * 4)
        if not gdi32.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(bi), 0):
            return None
        return Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
    except Exception:
        return None
    finally:
        try:
            if bmp:
                gdi32.DeleteObject(bmp)
            if mem_dc:
                gdi32.DeleteDC(mem_dc)
            if hwnd_dc:
                user32.ReleaseDC(hwnd, hwnd_dc)
        except Exception:
            pass


class VisionReader:
    """截屏 + 视觉模型解读（主 claude-opus-5-5，降级 sonnet-5 / opus-4-8，本地 OCR 兜底）。"""

    def __init__(self, cfg: dict, bus: EventBus, client, log=None):
        self.cfg = cfg or {}
        self.bus = bus
        self.client = client
        self.log = log or get_logger("vision")
        self.last_stats: dict = {}
        self.last_mode: str = ""
        self.last_image_size: tuple[int, int] = (0, 0)
        self._ocr_engine = None

    # ---------------------------------------------------------- 截图
    def grab(self, mode: str | None = None) -> tuple[Image.Image, str]:
        """截图 -> (缩放到长边 max_width 的 PIL 图, base64 PNG)。

        mode: primary（主屏）| wemeet_window（腾讯会议窗口，失败回退主屏）| region（同主屏）。
        """
        _ensure_dpi_aware()
        mode = mode or str(get(self.cfg, "vision.capture", "primary") or "primary")
        img = None
        used_mode = mode
        if mode == "wemeet_window":
            img = self._grab_wemeet()
            if img is None:
                used_mode = "primary(fallback)"
        if img is None:
            img = self._grab_primary()
            used_mode = used_mode if mode == "primary" else used_mode
        if img is None:  # 极端情况下连主屏都截不到
            img = Image.new("RGB", (1280, 720), (24, 24, 24))
            used_mode = "blank"
        img = img.convert("RGB")
        img = self._fit(img)
        b64 = self._png_b64(img)
        self.last_mode = used_mode
        self.last_image_size = img.size
        self.log.info("截图完成 mode=%s size=%dx%d b64=%.0fKB",
                      used_mode, img.size[0], img.size[1], len(b64) / 1024)
        return img, b64

    def _grab_primary(self) -> Image.Image | None:
        w, h = _primary_screen_rect()
        try:
            with ImageGrab.grab(all_screens=True) as full:
                vx, vy, vw, vh = _virtual_screen_rect()
                if w and h:
                    return full.crop((0 - vx, 0 - vy, w - vx, h - vy))
                return full.copy()
        except Exception as e:
            self.log.warning("整屏截图失败（尝试单屏）：%s", e)
        try:
            return ImageGrab.grab()
        except Exception as e:
            self.log.warning("主屏截图失败：%s", e)
            return None

    def _grab_wemeet(self) -> Image.Image | None:
        win = find_wemeet_window()
        if not win:
            self.log.info("未找到腾讯会议窗口，回退主屏截图")
            return None
        l, t, r, b = win["rect"]
        self.log.info("腾讯会议窗口：title=%r rect=%s area=%d", win.get("title"), win["rect"], win["area"])
        # 优先 PrintWindow：直接取窗口自身画面，**即使被别的窗口遮挡**也是会议内容
        # （区域截图只能拍到最上层窗口，实测被 DSH/编辑器盖住时会读错内容）
        method = str(get(self.cfg, "vision.capture_method", "auto") or "auto").strip().lower()
        if method in ("auto", "printwindow"):
            try:
                hwnd = int(win.get("hwnd") or 0)
                img = _printwindow_grab(hwnd, r - l, b - t) if hwnd else None
            except Exception as e:
                self.log.warning("PrintWindow 截图异常：%s", e)
                img = None
            if img is not None and not _looks_blank(img):
                return img
            if method == "printwindow":
                self.log.warning("PrintWindow 截图空白，回退区域截图")
        try:
            vx, vy, vw, vh = _virtual_screen_rect()
            with ImageGrab.grab(all_screens=True) as full:
                x0, y0 = l - vx, t - vy
                x1, y1 = r - vx, b - vy
                x0, y0 = max(0, x0), max(0, y0)
                x1, y1 = min(full.width, x1), min(full.height, y1)
                if x1 - x0 < 80 or y1 - y0 < 60:
                    return None
                return full.crop((x0, y0, x1, y1))
        except Exception as e:
            self.log.warning("腾讯会议窗口截图失败：%s", e)
            return None

    def _fit(self, img: Image.Image) -> Image.Image:
        try:
            max_side = int(get(self.cfg, "vision.max_width", 1280) or 1280)
        except Exception:
            max_side = 1280
        max_side = max(320, min(max_side, 2048))
        w, h = img.size
        if max(w, h) <= max_side:
            return img
        scale = max_side / float(max(w, h))
        return img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)

    @staticmethod
    def _png_b64(img: Image.Image) -> str:
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=False)
        return base64.b64encode(buf.getvalue()).decode("ascii")

    # ---------------------------------------------------------- 本地 OCR 兜底
    def ocr(self, image: Image.Image) -> str:
        """rapidocr 本地 OCR。失败返回空串（不抛）。"""
        try:
            import numpy as np
            from rapidocr_onnxruntime import RapidOCR

            if self._ocr_engine is None:
                self._ocr_engine = RapidOCR()
            arr = np.array(image.convert("RGB"))[:, :, ::-1]  # RGB -> BGR
            result, _elapse = self._ocr_engine(arr)
            if not result:
                return ""
            lines = []
            for item in result:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    txt = str(item[1] or "").strip()
                    if txt:
                        lines.append(txt)
            text = "\n".join(lines)
            return text[:4000]
        except Exception as e:
            self.log.warning("本地 OCR 失败：%s", e)
            return ""

    # ---------------------------------------------------------- emit
    def _emit(self, ev: BrainEvent) -> None:
        try:
            self.bus.emit_brain(ev)
        except Exception as e:
            self.log.warning("emit_brain 异常（忽略）：%s", e)

    # ---------------------------------------------------------- 读屏
    def read_screen(self, question: str = "", context: str = "") -> BrainEvent:
        """截屏 + 视觉模型解读；流式 emit（先占位，后增量），返回 done 的 BrainEvent。"""
        t0 = time.time()
        req_id = uuid.uuid4().hex[:8]
        title = "屏幕解读"
        lane = LANE_DEEP
        errors: list[str] = []

        # 首包占位：让 UI 立刻显示「正在看屏幕…」，视觉模型 9s 也不至于像卡死
        self._emit(BrainEvent(request_id=req_id, kind=KIND_SLIDE, lane=lane,
                              title=title, delta="正在看屏幕…", done=False))

        img = None
        try:
            img, b64 = self.grab()
        except Exception as e:
            b64 = ""
            errors.append(f"截图失败: {type(e).__name__}: {e}")
            self.log.warning("截图异常：%s", e)

        text = ""
        model_used = ""
        model_ok = False
        ttft = 0.0
        if img is not None and b64 and self.client is not None:
            primary = get(self.cfg, "llm.primary", {}) or {}
            vision_provider = str(get(self.cfg, "vision.provider", primary.get("provider", "openai"))
                                  or primary.get("provider", "openai"))
            default_model = str(primary.get("model", "gpt-4o-mini") or "gpt-4o-mini")
            primary_model = str(get(self.cfg, "vision.model", default_model) or default_model)
            fallbacks = list(get(self.cfg, "vision.fallback_models", []) or [])
            models: list[str] = []
            for m in [primary_model] + fallbacks:
                if m and m not in models:
                    models.append(str(m))
            prompt = (question or "").strip() or _QUESTION_DEFAULT
            user_text = prompt
            ctx = (context or "").strip()
            if ctx:
                user_text = "【现场上下文】\n" + ctx[-1200:] + "\n\n" + user_text
            messages = [
                {"role": "system", "content": _vision_system_prompt(self.cfg)},
                {"role": "user", "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
                ]},
            ]
            timeout = float(get(self.cfg, "vision.timeout_s", 100) or 100)
            for i, model in enumerate(models):
                state = {"first": None, "chars": 0}

                def on_delta(piece: str, _m: str = model) -> None:
                    if not piece:
                        return
                    if state["first"] is None:
                        state["first"] = round(time.time() - t0, 2)
                    state["chars"] += len(piece)
                    self._emit(BrainEvent(request_id=req_id, kind=KIND_SLIDE, lane=lane,
                                          title=title, delta=piece, done=False))

                try:
                    piece_text = self.client.chat(
                        vision_provider, model, messages,
                        stream=True, on_delta=on_delta, max_tokens=1000,
                        temperature=0.2, timeout=timeout,
                    )
                    if piece_text and piece_text.strip():
                        text = piece_text.strip()
                        model_used = f"{vision_provider}/{model}"
                        model_ok = True
                        ttft = state["first"] or 0.0
                        break
                    errors.append(f"{model} 返回空文本")
                except Exception as e:
                    err = f"{model}: {type(e).__name__}: {e}"
                    errors.append(err)
                    self.log.warning("视觉模型失败（第 %d 个）：%s", i + 1, err)
                    if i + 1 < len(models):
                        self._emit(BrainEvent(request_id=req_id, kind=KIND_SLIDE, lane=lane,
                                              title=title, delta="\n（换用备用视觉模型重试…）\n",
                                              done=False))
        elif self.client is None:
            errors.append("未提供 LLMClient")

        # 视觉模型全挂 -> 本地 OCR 兜底
        ocr_used = False
        if not text:
            ocr_enabled = bool(get(self.cfg, "vision.ocr_fallback", True))
            if ocr_enabled and img is not None:
                ocr_text = self.ocr(img)
                ocr_used = True
                if ocr_text.strip():
                    text = ("（视觉模型不可用，已用本地 OCR 识别页面文字）\n"
                            + ocr_text.strip())
                    model_used = "rapidocr/local"
                else:
                    text = "（视觉模型不可用，本地 OCR 也没识别到文字；可以手动补充这一页的内容后再提问。）"
                    model_used = "rapidocr/local(empty)"
            else:
                text = ("（截图/视觉模型暂时不可用："
                        + ("；".join(errors[-2:]) if errors else "未知原因")
                        + "）")
            self.log.warning("视觉解读失败，已走本地兜底：%s", errors[-2:])

        total = round(time.time() - t0, 2)
        done_ev = BrainEvent(request_id=req_id, kind=KIND_SLIDE, lane=lane,
                             title=title, delta="", done=True,
                             error=None if model_ok else ("；".join(errors[-2:]) or None),
                             usage=dict(getattr(self.client, "last_usage", {}) or {})
                             if self.client is not None else {},
                             full_text=text)
        self._emit(done_ev)
        self.last_stats = {
            "mode": self.last_mode,
            "image_size": self.last_image_size,
            "provider_model": model_used,
            "vision_ok": model_ok,
            "ocr_used": ocr_used,
            "ttft": ttft,
            "total": total,
            "chars": len(text),
        }
        self.log.info("读屏完成：ok=%s model=%s ttft=%ss total=%ss chars=%d",
                      model_ok, model_used, ttft, total, len(text))
        return done_ev

    def close(self) -> None:
        self._ocr_engine = None


# ================================================================== 自测
def _selftest() -> int:
    from config import init_console, load_config
    from contracts import EventBus
    from brain import LLMClient

    init_console()
    print("=" * 72)
    print("vision_reader.py --selftest （截当前屏 + 真实视觉模型解读）")
    print("=" * 72)
    cfg = load_config()
    vision_provider = str(get(cfg, "vision.provider", "openai") or "openai")
    if vision_provider in (cfg.get("_missing_keys") or []):
        print(f"SKIP：provider {vision_provider} 缺少 API key，未发起视觉请求")
        return 0

    bus = EventBus()
    client = LLMClient(cfg)
    vr = VisionReader(cfg, bus, client)

    print("\n[1] 截图（当前桌面）：")
    img, b64 = vr.grab()
    print(f"    mode={vr.last_mode} size={img.size} base64={len(b64)/1024:.1f}KB")
    assert img.size[0] > 0 and b64, "截图失败"

    print("\n[2] 视觉模型读屏（真实调用，约 5~15s）：")
    t0 = time.time()
    ev = vr.read_screen(question="请解读当前屏幕内容。如果屏幕上没有学术内容，就如实说这是什么界面。")
    dt = time.time() - t0
    stream_events = 0
    while True:
        try:
            e = bus.brain_events.get_nowait()
        except __import__("queue").Empty:
            break
        stream_events += 1
    print(f"    总耗时 {dt:.2f}s | 流式事件数 {stream_events} | done={ev.done} | "
          f"error={ev.error}")
    print(f"    stats={vr.last_stats}")
    print("    ---- 解读 ----")
    for line in (ev.full_text or "").splitlines():
        print("    " + line)
    assert ev.done and ev.full_text.strip(), "未返回非空解读（UI 会一直转圈）"
    client.close()
    print("\nSELFTEST OK ✅")
    return 0


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("用法：python vision_reader.py --selftest")
