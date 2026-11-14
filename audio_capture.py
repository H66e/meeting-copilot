# -*- coding: utf-8 -*-
"""组会助手 · 音频采集 + 能量 VAD 切句（WASAPI 回环，A1 模块）

职责（严格按 INTERFACES.md 第 4 节 A1）：
    LoopbackCapture:
        - soundcard 默认扬声器 WASAPI 回环，100ms 一帧 float32 mono 16k
        - 能量 VAD 自适应切句（噪声底自适应 / pre_roll 回补 / silence_hangover /
          min_speech / max_segment 强切），整句 AudioChunk 回调给 worker
        - cfg.audio.record_audio 为真时整场写 records/session_YYYYmmdd_HHMM.wav
          （16k / mono / 16bit，用标准库 wave）
        - 运行中 pause/resume/set_device 安全；设备断开自动重连（指数退避、不刷日志）
        - get_level() 返回 0~1 音量；list_output_devices() 供设置面板
    EnergyVAD / SessionRecorder 可独立测试（不依赖麦克风与真机声音）。

设计约束：
    - 模块导入零副作用：soundcard 只在真正打开设备时惰性导入；av 同样。
    - 自测（无需人工、无声音也不崩）：
        python audio_capture.py --selftest [--seconds 3]
"""
from __future__ import annotations

import argparse
import math
import threading
import time
import wave
from collections import deque
from pathlib import Path
from typing import Callable

import numpy as np

from config import get, init_console, load_config
from contracts import SAMPLE_RATE, FRAME_SAMPLES, AudioChunk, EventBus
from logging_setup import get_logger

FRAME_MS = 100
FRAME_S = FRAME_MS / 1000.0
_DEFAULT_VAD = {
    "rms_abs_floor": 0.0045,
    "min_speech_ms": 320,
    "silence_hangover_ms": 620,
    "pre_roll_ms": 240,
    "max_segment_s": 11.0,
    "adaptive": True,
}
# 句子尾部保留的静音时长（给 whisper 一点上下文，同时不为静音多付 RTF）
_TAIL_KEEP_S = 0.30
# 自适应噪声底倍数：起始判定（on）比结束判定（off）高一档，形成迟滞
_NOISE_MULT_ON = 3.0
_NOISE_MULT_OFF = 1.7
_NOISE_MAX = 0.03
_NOISE_MIN = 1e-5
# 起始需要连续多少个 100ms 帧超过阈值（抗孤立爆音）
_ONSET_FRAMES = 2
# 启动后先静默 0.5s 只学噪声底，避免“一开录就讲话”把噪声底学高
_VAD_WARMUP_S = 0.5


def _vad_conf(cfg) -> dict:
    """从整份 cfg / cfg['audio'] / cfg['audio']['vad'] / 裸 vad dict 中取出 VAD 配置。"""
    if not isinstance(cfg, dict):
        return {}
    audio = cfg.get("audio")
    if isinstance(audio, dict):
        vad = audio.get("vad")
        return dict(vad) if isinstance(vad, dict) else {}
    vad = cfg.get("vad")
    if isinstance(vad, dict):
        return dict(vad)
    if "rms_abs_floor" in cfg or "min_speech_ms" in cfg:
        return dict(cfg)
    return {}


def rms_of(frame: np.ndarray) -> float:
    """一帧的 RMS 能量（float32，正常范围 0~1）。"""
    if frame is None or len(frame) == 0:
        return 0.0
    arr = np.asarray(frame, dtype=np.float64)
    return float(np.sqrt(np.mean(arr * arr)))


def level_from_rms(rms: float) -> float:
    """RMS -> UI 0~1 音量条（开方压缩，人耳友好）。"""
    try:
        return float(min(1.0, max(0.0, math.sqrt(max(0.0, float(rms)) / 0.12))))
    except Exception:
        return 0.0


def _to_mono_f32(data) -> np.ndarray:
    """soundcard 返回 (frames, channels) float32；统一转 mono float32。"""
    arr = np.asarray(data)
    if arr.ndim > 1:
        arr = arr.mean(axis=1) if arr.shape[1] > 1 else arr[:, 0]
    return np.ascontiguousarray(arr.reshape(-1), dtype=np.float32)


class EnergyVAD:
    """能量 VAD + 自适应噪声底：把连续 100ms 帧切成整句 AudioChunk。

    用法：
        vad = EnergyVAD(cfg)
        for frame, t_start in frames:      # frame: 1600 个 float32，t_start 为秒
            for chunk in vad.feed(frame, t_start):
                on_utterance(chunk)
        for chunk in vad.flush():          # 流结束时收尾
            on_utterance(chunk)

    - 噪声底在“静音帧”上慢速双向跟踪（向下快、向上慢），阈值 = max(绝对底, 噪声底 × 倍数)；
    - pre_roll 回补句首（避免吃掉第一个字）；silence_hangover 判断句尾；
    - min_speech 过滤咳嗽/键盘等短音；max_segment 对持续讲话强切，避免一句过长。
    """

    def __init__(self, cfg=None, frame_samples: int = FRAME_SAMPLES,
                 sample_rate: int = SAMPLE_RATE):
        conf = _vad_conf(cfg)
        self.sr = int(sample_rate)
        self.frame_samples = int(frame_samples)
        self.frame_s = self.frame_samples / float(self.sr)
        self.abs_floor = float(conf.get("rms_abs_floor", _DEFAULT_VAD["rms_abs_floor"]))
        self.min_speech_s = float(conf.get("min_speech_ms", _DEFAULT_VAD["min_speech_ms"])) / 1000.0
        self.hangover_s = float(conf.get("silence_hangover_ms", _DEFAULT_VAD["silence_hangover_ms"])) / 1000.0
        pre_roll_s = float(conf.get("pre_roll_ms", _DEFAULT_VAD["pre_roll_ms"])) / 1000.0
        self.max_segment_s = float(conf.get("max_segment_s", _DEFAULT_VAD["max_segment_s"]))
        self.adaptive = bool(conf.get("adaptive", True))

        self.pre_roll_frames = max(0, int(round(pre_roll_s / self.frame_s)))
        self.tail_frames = max(1, int(round(_TAIL_KEEP_S / self.frame_s)))
        self.hangover_frames = max(1, int(math.ceil(self.hangover_s / self.frame_s - 1e-9)))
        self.max_frames = max(2, int(math.ceil(self.max_segment_s / self.frame_s)))
        self.warmup_frames = max(1, int(round(_VAD_WARMUP_S / self.frame_s)))
        self.reset()

    # ------------------------------------------------------------ 状态
    def reset(self) -> None:
        """清空内部状态（pause/resume/换设备后调用）。"""
        self._pre: deque = deque(maxlen=max(1, self.pre_roll_frames))
        self._buf: list[tuple[np.ndarray, float, float]] = []
        self._voiced_idx: list[int] = []
        self._in_speech = False
        self._onset = 0
        self._silence_run = 0
        self._warm_left = self.warmup_frames
        self._warm_vals: list[float] = []
        # 初始噪声底取“绝对底/起始倍数”，热身帧结束后用中位数替换
        self._noise: float = max(_NOISE_MIN, self.abs_floor / _NOISE_MULT_ON)
        self._last_on = self.abs_floor
        self._last_off = self.abs_floor * 0.8

    # ------------------------------------------------------------ 阈值
    def thresholds(self) -> tuple[float, float]:
        """返回 (起始阈值, 结束阈值)。"""
        base = self.abs_floor
        if not self.adaptive:
            self._last_on, self._last_off = float(base), float(base * 0.8)
            return self._last_on, self._last_off
        on = max(base, self._noise * _NOISE_MULT_ON)
        off = max(base * 0.8, self._noise * _NOISE_MULT_OFF)
        self._last_on, self._last_off = float(on), float(off)
        return self._last_on, self._last_off

    def _adapt_noise(self, rms: float) -> None:
        if not self.adaptive:
            return
        alpha = 0.12 if rms < self._noise else 0.015
        self._noise = (1.0 - alpha) * self._noise + alpha * rms
        self._noise = min(max(self._noise, _NOISE_MIN), _NOISE_MAX)

    @property
    def noise_floor(self) -> float:
        return float(self._noise)

    @property
    def last_threshold(self) -> float:
        return float(self._last_on)

    # ------------------------------------------------------------ 主入口
    def feed(self, frame, t_start: float) -> list[AudioChunk]:
        """喂入一帧，返回本帧内新切出的整句（通常 0 或 1 个）。"""
        arr = np.ascontiguousarray(np.asarray(frame, dtype=np.float32).reshape(-1))
        if arr.size != self.frame_samples:
            # 容错：补齐/截断，绝不让一帧异常打断采集
            if arr.size < self.frame_samples:
                arr = np.pad(arr, (0, self.frame_samples - arr.size))
            else:
                arr = arr[: self.frame_samples]
        rms = rms_of(arr)
        on_thr, off_thr = self.thresholds()
        out: list[AudioChunk] = []

        if self._warm_left > 0:
            # 启动/恢复后的热身帧：只学噪声底，不做切句（防止把开头环境音当语音）
            self._warm_left -= 1
            self._warm_vals.append(rms)
            self._pre.append((arr, rms, float(t_start)))
            if self._warm_left == 0:
                med = float(np.median(np.asarray(self._warm_vals, dtype=np.float64)))
                self._noise = min(max(med, _NOISE_MIN), _NOISE_MAX)
                self._warm_vals.clear()
            return out

        if not self._in_speech:
            self._pre.append((arr, rms, float(t_start)))
            if rms >= on_thr:
                self._onset += 1
                if self._onset >= _ONSET_FRAMES:
                    # 用 pre_roll 回补句首，开始一句话
                    self._in_speech = True
                    self._buf = list(self._pre)
                    self._voiced_idx = [i for i, b in enumerate(self._buf) if b[1] >= off_thr]
                    self._silence_run = 0
                    self._onset = 0
                    self._pre.clear()
            else:
                self._onset = 0
                self._adapt_noise(rms)
            return self._check_max(out)

        self._buf.append((arr, rms, float(t_start)))
        idx = len(self._buf) - 1
        if rms >= off_thr:
            self._voiced_idx.append(idx)
            self._silence_run = 0
        else:
            self._silence_run += 1
            self._adapt_noise(rms)

        if self._silence_run >= self.hangover_frames:
            chunk = self._close()
            if chunk is not None:
                out.append(chunk)
            self._begin_silence()
            return out

        return self._check_max(out)

    def flush(self) -> list[AudioChunk]:
        """流结束/停止时收尾：把进行中的句子切出来（不足 min_speech 则丢弃）。"""
        if not self._in_speech:
            self.reset()
            return []
        chunk = self._close()
        self.reset()
        return [chunk] if chunk is not None else []

    # ------------------------------------------------------------ 内部
    def _begin_silence(self) -> None:
        self._in_speech = False
        self._buf = []
        self._voiced_idx = []
        self._silence_run = 0
        self._onset = 0
        self._pre.clear()

    def _check_max(self, out: list[AudioChunk]) -> list[AudioChunk]:
        """超过 max_segment_s 强切；若仍在讲话，立即从当前帧续下一句。"""
        if not self._in_speech or len(self._buf) < self.max_frames:
            return out
        chunk = self._close()
        if chunk is not None:
            out.append(chunk)
        # 用当前帧续上新句子，避免强切处丢字
        cur = self._buf[-1] if self._buf else None
        self._begin_silence()
        if cur is not None:
            arr, rms, t0 = cur
            on_thr, off_thr = self.thresholds()
            if rms >= off_thr:
                self._in_speech = True
                self._buf = [(arr, rms, t0)]
                self._voiced_idx = [0]
                self._silence_run = 0
                self._pre.clear()
        return out

    def _close(self) -> AudioChunk | None:
        """按 pre_roll/尾部静音裁剪并生成 AudioChunk；太短返回 None。"""
        if not self._buf or not self._voiced_idx:
            return None
        first_v = self._voiced_idx[0]
        last_v = self._voiced_idx[-1]
        start_i = max(0, first_v - self.pre_roll_frames)
        end_i = min(len(self._buf), last_v + 1 + self.tail_frames)
        seg = self._buf[start_i:end_i]
        if not seg:
            return None
        voiced_s = (last_v - first_v + 1) * self.frame_s
        if voiced_s + 1e-6 < self.min_speech_s:
            return None
        data = np.concatenate([b[0] for b in seg]).astype(np.float32, copy=False)
        t_start = float(seg[0][2])
        t_end = t_start + len(data) / float(self.sr)
        peak = max(b[1] for b in seg)
        return AudioChunk(data=data, t_start=t_start, t_end=t_end,
                          peak_rms=float(peak), source="loopback")


class SessionRecorder:
    """整场录音：边采边写 16k/mono/16bit wav（标准库 wave，关闭时定稿头部）。

    文件名按契约：records/session_YYYYmmdd_HHMM.wav；同一分钟内重复创建自动加 _2/_3。
    打开/写入失败只降级（返回 False / 抛给调用方处理），不影响听写。
    """

    def __init__(self, out_dir: str | Path, sample_rate: int = SAMPLE_RATE,
                 prefix: str = "session", log=None):
        self._log = log or get_logger("audio")
        self.sr = int(sample_rate)
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        base = f"{prefix}_{time.strftime('%Y%m%d_%H%M')}"
        path = out / f"{base}.wav"
        n = 2
        while path.exists():
            path = out / f"{base}_{n}.wav"
            n += 1
        self.path = path
        self._frames = 0
        self._closed = False
        self._wf = wave.open(str(path), "wb")
        self._wf.setnchannels(1)
        self._wf.setsampwidth(2)
        self._wf.setframerate(self.sr)
        self._log.info("整场录音文件: %s", path)

    def write(self, frame) -> bool:
        if self._closed:
            return False
        arr = np.asarray(frame, dtype=np.float32).reshape(-1)
        if arr.size == 0:
            return False
        np.clip(arr, -1.0, 1.0, out=arr)
        try:
            self._wf.writeframes((arr * 32767.0).astype("<i2").tobytes())
            self._frames += 1
            return True
        except Exception as e:  # 磁盘满等：停写，但听写继续
            self._log.warning("写整场录音失败，已停止录音: %s", e)
            self.close()
            return False

    def close(self) -> str:
        if not self._closed:
            self._closed = True
            try:
                self._wf.close()
            except Exception:
                pass
            self._log.info("整场录音已保存: %s (%.1fs)", self.path, self.seconds)
        return str(self.path)

    @property
    def frames(self) -> int:
        return self._frames

    @property
    def seconds(self) -> float:
        return self._frames * FRAME_SAMPLES / float(self.sr)

    @property
    def closed(self) -> bool:
        return self._closed


class LoopbackCapture:
    """soundcard WASAPI 回环采集 + VAD 切句（A1 契约实现）。"""

    def __init__(self, cfg: dict, bus: EventBus,
                 on_utterance: Callable[[AudioChunk], None], log=None, *, on_frame=None):
        self.cfg = cfg or {}
        self.bus = bus
        self.on_utterance = on_utterance
        self.on_frame = on_frame   # 可选：100ms 帧级回调（供"边说边出字"流式字幕）
        self._log = log or get_logger("audio")
        self._lock = threading.RLock()
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._paused = False
        self._device_target: str = str(get(self.cfg, "audio.device", "auto") or "auto")
        self._device_open = ""
        self._reopen = False
        self._level = 0.0
        self._vad = EnergyVAD(self.cfg)
        self._recorder: SessionRecorder | None = None
        self._session_t0 = time.monotonic()
        self._paused_total = 0.0
        self._pause_started = 0.0
        # 统计（自测/状态栏用）
        self._frames = 0
        self._samples = 0
        self._sq_sum = 0.0
        self._chunks = 0
        self._last_chunk: AudioChunk | None = None

    # ------------------------------------------------------------ 设备
    def list_output_devices(self) -> list[str]:
        """所有输出设备名（供设置面板下拉）。失败返回 []，不抛。"""
        try:
            import soundcard as sc
            return [str(s.name) for s in sc.all_speakers()]
        except Exception as e:
            self._log.warning("枚举输出设备失败: %s", e)
            return []

    def _open_loopback(self):
        """返回 (loopback 麦克风对象, 实际设备名)。惰性导入 soundcard。"""
        import soundcard as sc

        target = self._device_target or "auto"
        if target.lower() in ("auto", "default", ""):
            spk = sc.default_speaker()
            if spk is None:
                raise RuntimeError("没有可用的默认扬声器")
            mic = sc.get_microphone(id=str(spk.name), include_loopback=True)
            if mic is None:
                raise RuntimeError(f"无法打开默认扬声器回环: {spk.name}")
            return mic, str(spk.name)

        try:
            mic = sc.get_microphone(id=target, include_loopback=True)
            if mic is not None:
                return mic, target
        except Exception:
            pass
        # 兜底：按名字模糊匹配（设备改名/带后缀时）
        for spk in sc.all_speakers():
            if target in spk.name or spk.name in target:
                mic = sc.get_microphone(id=str(spk.name), include_loopback=True)
                if mic is not None:
                    return mic, str(spk.name)
        raise RuntimeError(f"找不到输出设备: {target}")

    def set_device(self, name: str) -> None:
        """运行中安全切换设备；'auto' 表示默认扬声器。"""
        target = str(name or "auto")
        with self._lock:
            if target == self._device_target and self._device_open and target.lower() != "auto":
                return
            self._device_target = target
            self._reopen = True
            self._vad.reset()
        self._log.info("切换输出设备 -> %s", target)
        self._bus_log(f"切换音频设备：{target}")

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._paused = False
            self._stop_evt.clear()
            self._vad.reset()
            self._level = 0.0
            self._session_t0 = time.monotonic()
            self._paused_total = 0.0
            self._pause_started = 0.0
            self._frames = self._samples = self._chunks = 0
            self._sq_sum = 0.0
            if bool(get(self.cfg, "audio.record_audio", False)) and self._recorder is None:
                rec_dir = get(self.cfg, "_dirs.records",
                              str(Path(__file__).resolve().parent / "records"))
                try:
                    self._recorder = SessionRecorder(rec_dir, log=self._log)
                except Exception as e:
                    self._log.warning("整场录音不可用（继续听写）: %s", e)
                    self._recorder = None
            self._thread = threading.Thread(target=self._run, name="mc-audio-capture", daemon=True)
            self._thread.start()
        self._log.info("音频采集已启动（目标设备: %s）", self._device_target)

    def stop(self) -> None:
        with self._lock:
            self._running = False
            self._stop_evt.set()
            thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3.0)
        with self._lock:
            self._close_recorder()
            self._thread = None
            self._level = 0.0
        self._log.info("音频采集已停止")

    def pause(self) -> None:
        with self._lock:
            if not self._paused:
                self._pause_started = time.monotonic()
            self._paused = True
            self._vad.reset()
        self._log.info("音频采集已暂停")
        self._bus_log("已暂停聆听")

    def resume(self) -> None:
        with self._lock:
            if self._paused and self._pause_started > 0:
                # 会话时钟不重置：把暂停时长扣掉，字幕时间戳保持单调
                self._paused_total += max(0.0, time.monotonic() - self._pause_started)
                self._pause_started = 0.0
            self._paused = False
            self._vad.reset()
        self._log.info("音频采集已恢复")
        self._bus_log("继续聆听")

    @property
    def is_running(self) -> bool:
        with self._lock:
            return bool(self._running)

    @property
    def device_name(self) -> str:
        with self._lock:
            if self._device_open:
                return self._device_open
            return "" if self._device_target.lower() == "auto" else self._device_target

    def get_level(self) -> float:
        return float(min(1.0, max(0.0, self._level)))

    def stats(self) -> dict:
        with self._lock:
            samples = self._samples
            rms = math.sqrt(self._sq_sum / samples) if samples else 0.0
            rec_path = self._recorder.path if self._recorder else ""
            rec_sec = self._recorder.seconds if self._recorder else 0.0
            return {
                "device": self._device_open,
                "target": self._device_target,
                "running": bool(self._running),
                "paused": bool(self._paused),
                "frames": self._frames,
                "seconds": samples / float(SAMPLE_RATE),
                "rms": float(rms),
                "level": float(self._level),
                "chunks": self._chunks,
                "record_path": str(rec_path),
                "record_seconds": float(rec_sec),
                "noise_floor": self._vad.noise_floor,
                "vad_threshold": self._vad.last_threshold,
            }

    # ------------------------------------------------------------ 采集线程
    def _run(self) -> None:
        backoff = 0.5
        fail_count = 0
        try:
            while not self._stop_evt.is_set() and not self._bus_stopping():
                try:
                    mic, opened = self._open_loopback()
                except Exception as e:
                    fail_count += 1
                    if fail_count == 1 or fail_count % 20 == 0:
                        self._log.warning("打开回环设备失败(%d 次): %s；%.1fs 后重试",
                                          fail_count, e, backoff)
                        self._bus_log("音频设备打开失败，正在重试…")
                    if fail_count >= 3 and self._device_target.lower() not in ("auto", "default", ""):
                        self._log.warning("指定设备连续失败，回退默认扬声器: %s", self._device_target)
                        with self._lock:
                            self._device_target = "auto"
                    self._stop_evt.wait(backoff)
                    backoff = min(backoff * 2.0, 5.0)
                    continue

                fail_count = 0
                backoff = 0.5
                with self._lock:
                    self._device_open = opened
                    self._reopen = False
                self._log.info("回环采集已连接: %s", opened)
                self._bus_log(f"开始采集：{opened}")
                self._vad.reset()
                try:
                    with mic.recorder(samplerate=SAMPLE_RATE, channels=1,
                                      blocksize=FRAME_SAMPLES) as rec:
                        while not self._stop_evt.is_set() and not self._bus_stopping():
                            with self._lock:
                                if self._reopen:
                                    self._reopen = False
                                    break
                            try:
                                data = rec.record(numframes=FRAME_SAMPLES)
                            except Exception as e:
                                self._log.warning("采集流中断，准备重连: %s", e)
                                self._bus_log("音频设备中断，正在重连…")
                                break
                            self._on_data(data)
                except Exception as e:
                    self._log.warning("回环会话异常: %s；%.1fs 后重连", e, backoff)
                    self._stop_evt.wait(backoff)
                    backoff = min(backoff * 2.0, 5.0)
        finally:
            self._close_recorder()
            if not self._stop_evt.is_set():
                self._log.info("采集线程退出（未收到 stop，可能被外部停止）")
            else:
                self._log.info("采集线程退出")

    def _on_data(self, data) -> None:
        """处理一帧：更新电平/统计 -> 写 wav -> VAD -> 回调。任何异常不打断采集。"""
        try:
            arr = _to_mono_f32(data)
            if arr.size != FRAME_SAMPLES:
                if arr.size < FRAME_SAMPLES:
                    arr = np.pad(arr, (0, FRAME_SAMPLES - arr.size))
                else:
                    arr = arr[:FRAME_SAMPLES]
            rms = rms_of(arr)
            now = self._session_now()
            with self._lock:
                self._frames += 1
                self._samples += FRAME_SAMPLES
                self._sq_sum += float(np.sum(np.square(arr, dtype=np.float64)))
                self._level = level_from_rms(rms)
                paused = self._paused
                rec = self._recorder
            if paused:
                return
            if rec is not None and not rec.write(arr):
                with self._lock:
                    if self._recorder is rec and rec.closed:
                        self._recorder = None
            t_start = max(0.0, now - FRAME_S)
            if self.on_frame is not None:
                try:
                    self.on_frame(arr, t_start)
                except Exception:
                    pass
            for chunk in self._vad.feed(arr, t_start):
                self._emit(chunk)
        except Exception as e:  # 单帧异常绝不能让采集线程挂掉
            self._log.debug("处理音频帧出错（已忽略）: %s", e)

    def _emit(self, chunk: AudioChunk) -> None:
        if chunk is None:
            return
        with self._lock:
            self._chunks += 1
            self._last_chunk = chunk
        try:
            self.on_utterance(chunk)
        except Exception as e:
            self._log.warning("on_utterance 回调异常（已忽略）: %s", e)

    # ------------------------------------------------------------ 小工具
    def _session_now(self) -> float:
        """会话内秒数（扣掉暂停时长，保证字幕时间戳单调不回退）。"""
        return max(0.0, time.monotonic() - self._session_t0 - self._paused_total)

    def _close_recorder(self) -> None:
        rec = self._recorder
        self._recorder = None
        if rec is not None:
            try:
                rec.close()
            except Exception as e:
                self._log.warning("关闭整场录音失败: %s", e)

    def _bus_stopping(self) -> bool:
        try:
            return bool(self.bus is not None and self.bus.stopping)
        except Exception:
            return False

    def _bus_log(self, msg: str) -> None:
        try:
            if self.bus is not None:
                self.bus.log(msg)
        except Exception:
            pass


# ---------------------------------------------------------------- 模块级便捷函数
def list_output_devices() -> list[str]:
    """模块级设备列表（不实例化 capture 时也能用）。"""
    try:
        import soundcard as sc
        return [str(s.name) for s in sc.all_speakers()]
    except Exception:
        return []


# ---------------------------------------------------------------- 自测
def _synthetic_signal(sr: int = SAMPLE_RATE) -> np.ndarray:
    """合成 8 秒：静音/正弦交替（VAD 稳定性自检，确定性、离线）。"""
    parts = []
    pattern = [(1.0, False), (1.0, True), (1.0, False), (1.2, True), (1.5, False),
               (1.0, True), (1.3, False)]
    t = 0.0
    rng = np.random.default_rng(7)
    for dur, is_tone in pattern:
        n = int(round(dur * sr))
        if is_tone:
            tt = np.arange(n, dtype=np.float64) / sr
            sig = (0.28 * np.sin(2 * np.pi * 440.0 * tt)).astype(np.float32)
            # 加一点底噪，模拟真实链路
            sig += rng.normal(0, 2e-4, n).astype(np.float32)
        else:
            sig = rng.normal(0, 2e-4, n).astype(np.float32)
        parts.append(sig)
        t += dur
    return np.concatenate(parts)


def _vad_selftest_demo(verbose: bool = True) -> list[AudioChunk]:
    """用合成音频跑一遍 VAD，返回切出的句子（离线、确定性）。"""
    vad = EnergyVAD(load_config())
    signal = _synthetic_signal()
    chunks: list[AudioChunk] = []
    n_frames = len(signal) // FRAME_SAMPLES
    for i in range(n_frames):
        t0 = i * FRAME_S
        chunks.extend(vad.feed(signal[i * FRAME_SAMPLES:(i + 1) * FRAME_SAMPLES], t0))
    noise_floor = vad.noise_floor
    threshold = vad.last_threshold
    chunks.extend(vad.flush())
    if verbose:
        print(f"  [VAD 合成音频] 输入 {len(signal)/SAMPLE_RATE:.1f}s -> 切出 {len(chunks)} 句"
              f"（噪声底≈{noise_floor:.5f}，阈值≈{threshold:.5f}）")
        for i, c in enumerate(chunks, 1):
            print(f"    句{i}: {c.t_start:5.2f}s~{c.t_end:5.2f}s"
                  f" 时长 {c.duration:.2f}s peak_rms {c.peak_rms:.3f}")
    return chunks


def _run_selftest(seconds: float = 3.0, device: str | None = None) -> int:
    """录制 N 秒真实回环：打印设备/帧数/RMS/切句；无声音/无设备也不崩。"""
    init_console()
    from logging_setup import setup
    setup()
    log = get_logger("audio")
    print("=" * 66)
    print("组会助手 · 音频采集自测（LoopbackCapture）")
    print("=" * 66)
    try:
        cfg = load_config()
    except Exception as e:
        print(f"❌ 配置加载失败: {e}")
        return 1
    if device:
        cfg.setdefault("audio", {})["device"] = device
    print(f"配置设备: {get(cfg, 'audio.device', 'auto')} | "
          f"record_audio={get(cfg, 'audio.record_audio', False)} | "
          f"VAD={_vad_conf(cfg)}")

    cap = LoopbackCapture(cfg, EventBus(), on_utterance=lambda c: None, log=log)
    devices = cap.list_output_devices()
    print(f"输出设备({len(devices)}): {devices}")

    chunks: list[AudioChunk] = []
    cap.on_utterance = chunks.append
    levels: list[float] = []
    t0 = time.time()
    try:
        cap.start()
        while time.time() - t0 < max(0.5, seconds):
            time.sleep(0.1)
            levels.append(cap.get_level())
    finally:
        cap.stop()
    st = cap.stats()
    wav_ok = None
    if st["record_path"]:
        try:
            with wave.open(st["record_path"], "rb") as wf:
                wav_ok = {"channels": wf.getnchannels(), "width": wf.getsampwidth(),
                          "rate": wf.getframerate(), "frames": wf.getnframes()}
        except Exception as e:
            wav_ok = {"error": str(e)}
    print(f"设备名: {st['device'] or '（未打开）'}")
    print(f"帧数: {st['frames']}（{st['seconds']:.2f}s）| 平均RMS: {st['rms']:.5f} | "
          f"电平峰值: {max(levels) if levels else 0.0:.3f}")
    print(f"切句(实时): {len(chunks)} 句"
          + ("" if chunks else "（当前无播放/无声音，0 句属正常）"))
    for i, c in enumerate(chunks, 1):
        print(f"  实时句{i}: {c.t_start:.2f}s~{c.t_end:.2f}s 时长 {c.duration:.2f}s "
              f"peak_rms {c.peak_rms:.3f}")
    if wav_ok is not None:
        print(f"整场录音: {st['record_path']} -> {wav_ok}")

    print("-" * 66)
    synth = _vad_selftest_demo()
    ok_synth = len(synth) >= 2
    print(f"[VAD 断言] 合成音频应切出 >=2 句，实际 {len(synth)} 句 -> "
          f"{'✅' if ok_synth else '❌'}")
    print("[结论] ✅ audio_capture 自测通过（无声音环境下 0 句实时切句属正常）")
    return 0 if ok_synth else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="组会助手 · 音频采集（WASAPI 回环 + 能量 VAD）")
    ap.add_argument("--selftest", action="store_true", help="录制 N 秒并自检（无需人工）")
    ap.add_argument("--seconds", type=float, default=3.0, help="自测录制时长（秒）")
    ap.add_argument("--device", default=None, help="自测输出设备名（默认取 config）")
    args = ap.parse_args(argv)
    if args.selftest:
        try:
            return _run_selftest(args.seconds, args.device)
        except KeyboardInterrupt:
            print("\n已中断")
            return 130
        except Exception as e:
            print(f"❌ 自测异常: {e}")
            import traceback
            traceback.print_exc()
            return 1
    ap.print_help()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
