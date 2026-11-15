# -*- coding: utf-8 -*-
"""A 模块（音频采集 / ASR）自测 —— 不需要麦克风、不需要联网即可全绿。

覆盖：
    1) RMS/音量电平工具函数
    2) EnergyVAD：合成音频（正弦+静音交替）切句数量/起止/时长合理
    3) EnergyVAD：纯静音不出句；噪声底自适应；max_segment 强切
    4) SessionRecorder：整场 wav 可被标准库 wave 读回（16k/mono/16bit）
    5) AsrEngine：空输入 / 超短输入 / 未加载模型调用 均不崩
    6) 文本后处理：繁→简、半角标点、纯标点/语气词/字幕幻觉过滤、窗口重叠去重
    7) PyAV 解码：真实 m4a 能解码成 16k mono float32（文件存在时）
    8) LoopbackCapture 公开 API：不打开设备也不崩

运行：
    python tests/test_audio_asr.py
    # 可选：额外用真实模型跑 1 秒正弦冒烟（约 3~5s）
    $env:MC_TEST_ASR_MODEL=1; python tests/test_audio_asr.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import traceback
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np

from config import load_config
from contracts import SAMPLE_RATE, FRAME_SAMPLES, AudioChunk, EventBus
from audio_capture import (
    EnergyVAD,
    LoopbackCapture,
    SessionRecorder,
    level_from_rms,
    rms_of,
)
from asr_engine import (
    AsrEngine,
    find_cached_model,
    is_meaningful,
    normalize_text,
    resolve_model_ref,
    _looks_degenerate,
    _strip_overlap,
)
from tools.transcribe_file import OVERLAP_S, STRIDE_S, WINDOW_S, decode_audio

SR = SAMPLE_RATE
FRAME_S = FRAME_SAMPLES / SR
# 真实音频属于用户隐私，只能通过显式环境变量参与可选测试。
REAL_AUDIO = Path(os.environ.get("MC_TEST_AUDIO_FILE", "")).expanduser() if os.environ.get("MC_TEST_AUDIO_FILE") else None


# ---------------------------------------------------------------- 工具
def _make_frames(pattern: list[tuple[float, bool]], seed: int = 11):
    """pattern: [(时长s, 是否正弦语音)] -> 连续 100ms 帧列表（含时间戳）。"""
    rng = np.random.default_rng(seed)
    sig_parts = []
    for dur, voiced in pattern:
        n = int(round(dur * SR))
        if voiced:
            tt = np.arange(n, dtype=np.float64) / SR
            sig = (0.30 * np.sin(2 * np.pi * 440.0 * tt)).astype(np.float32)
            sig = sig + rng.normal(0, 2e-4, n).astype(np.float32)
        else:
            sig = rng.normal(0, 2e-4, n).astype(np.float32)
        sig_parts.append(sig)
    signal = np.concatenate(sig_parts)
    n_frames = len(signal) // FRAME_SAMPLES
    frames = []
    for i in range(n_frames):
        frames.append((signal[i * FRAME_SAMPLES:(i + 1) * FRAME_SAMPLES], i * FRAME_S))
    return frames


def _vad_run(frames, cfg=None) -> list[AudioChunk]:
    vad = EnergyVAD(cfg if cfg is not None else load_config())
    chunks: list[AudioChunk] = []
    for frame, t0 in frames:
        chunks.extend(vad.feed(frame, t0))
    chunks.extend(vad.flush())
    return chunks


# ---------------------------------------------------------------- 用例
def test_level_helpers():
    assert rms_of(np.zeros(FRAME_SAMPLES, dtype=np.float32)) == 0.0
    assert level_from_rms(0.0) == 0.0
    assert abs(level_from_rms(0.12) - 1.0) < 1e-9
    for rms in (0.0001, 0.0045, 0.01, 0.05, 0.5):
        lv = level_from_rms(rms)
        assert 0.0 <= lv <= 1.0, f"level_from_rms({rms})={lv} 越界"
    assert level_from_rms(0.05) > level_from_rms(0.005), "音量条应随 RMS 单调"
    print(f"   电平: rms(0.0045)->{level_from_rms(0.0045):.3f}, "
          f"rms(0.05)->{level_from_rms(0.05):.3f}")


def test_vad_two_sentences():
    # 1.0s 静音 + 1.2s 正弦 + 1.0s 静音 + 1.5s 正弦 + 1.2s 静音
    frames = _make_frames([(1.0, False), (1.2, True), (1.0, False),
                           (1.5, True), (1.2, False)])
    chunks = _vad_run(frames)
    assert len(chunks) == 2, f"应切出 2 句，实际 {len(chunks)}: {[c.duration for c in chunks]}"
    c0, c1 = chunks
    assert 0.8 <= c0.t_start <= 1.3, f"第1句起点应≈1.0s，实际 {c0.t_start:.2f}"
    assert 0.9 <= c0.duration <= 2.6, f"第1句时长异常 {c0.duration:.2f}s"
    assert 3.0 <= c1.t_start <= 3.6, f"第2句起点应≈3.2s，实际 {c1.t_start:.2f}"
    assert 0.9 <= c1.duration <= 2.9, f"第2句时长异常 {c1.duration:.2f}s"
    for c in chunks:
        assert c.data.dtype == np.float32 and c.data.size == int(round(c.duration * SR))
        assert c.peak_rms > 0.1, f"peak_rms 太小: {c.peak_rms}"
    print(f"   切句: {[(round(c.t_start, 2), round(c.t_end, 2), round(c.duration, 2)) for c in chunks]}")


def test_vad_cold_start_preserves_first_sentence():
    """开始采集即讲话时，0.5s 热身不能把句首当噪声吃掉。"""
    frames = _make_frames([(0.1, False), (3.8, True), (1.0, False)])
    chunks = _vad_run(frames)
    assert len(chunks) == 1, f"开录即讲话应保留完整一句，实际 {len(chunks)} 句"
    assert chunks[0].t_start <= 0.1, f"句首被热身阶段截掉: {chunks[0].t_start:.2f}s"
    assert chunks[0].t_end >= 3.8, f"句尾不完整: {chunks[0].t_end:.2f}s"
    print(f"   冷启动: 首句 {chunks[0].t_start:.2f}–{chunks[0].t_end:.2f}s ✅")


def test_vad_silence_only():
    frames = _make_frames([(5.0, False)])
    chunks = _vad_run(frames)
    assert len(chunks) == 0, f"纯静音不应切出句子，实际 {len(chunks)} 句"
    print("   纯静音: 0 句 ✅")


def test_vad_noise_adapt():
    # 3s 环境噪声(rms≈0.012) + 1.5s 正弦 + 1.0s 噪声：噪声底应自适应抬高，只切出语音那句
    frames = _make_frames([(3.0, False), (1.5, True), (1.0, False)])
    # 把静音段替换成更大的类噪声（0.012 幅度），模拟真实环境噪声
    vad = EnergyVAD(load_config())
    chunks: list[AudioChunk] = []
    rng = np.random.default_rng(23)
    idx = 0
    for frame, t0 in frames:
        if 3.0 <= t0 < 4.5:
            n = len(frame)
            tt = np.arange(n, dtype=np.float64) / SR
            frame = (0.30 * np.sin(2 * np.pi * 440.0 * tt)).astype(np.float32)
        elif idx < 30 or idx >= 45:
            frame = rng.normal(0, 0.012, len(frame)).astype(np.float32)
        idx += 1
        chunks.extend(vad.feed(frame, t0))
    noise_floor = vad.noise_floor
    threshold = vad.last_threshold
    chunks.extend(vad.flush())
    assert len(chunks) == 1, f"应只切出 1 句，实际 {len(chunks)} 句"
    assert 0.004 <= noise_floor <= 0.03, f"自适应噪声底异常: {noise_floor:.4f}"
    assert chunks[0].peak_rms > 0.15
    print(f"   噪声自适应: 切出 1 句，噪声底≈{noise_floor:.4f}，"
          f"阈值≈{threshold:.4f}")


def test_vad_max_segment():
    frames = _make_frames([(0.6, False), (25.0, True), (0.5, False)])
    chunks = _vad_run(frames)
    assert len(chunks) >= 2, f"25s 连续讲话应按 max_segment=11s 强切，实际 {len(chunks)} 句"
    longest = max(c.duration for c in chunks)
    assert longest <= 12.0, f"强切后单句仍过长: {longest:.2f}s"
    print(f"   强切: {len(chunks)} 句, 最长 {longest:.2f}s ✅")


def test_vad_config():
    cfg = load_config()
    vad = EnergyVAD(cfg)
    assert abs(vad.abs_floor - 0.0045) < 1e-9, vad.abs_floor
    assert abs(vad.min_speech_s - 0.28) < 1e-9, vad.min_speech_s
    # 准确率优先档位：停顿 450ms 算一句结束、单句上限 12s（改动请同步这里）
    assert abs(vad.hangover_s - 0.45) < 1e-9, vad.hangover_s
    assert abs(vad.max_segment_s - 12.0) < 1e-9, vad.max_segment_s
    vad2 = EnergyVAD(cfg["audio"])  # 也允许直接传 audio 子树
    assert abs(vad2.abs_floor - vad.abs_floor) < 1e-12
    print(f"   配置读取: abs_floor={vad.abs_floor} hangover={vad.hangover_s}s "
          f"max_segment={vad.max_segment_s}s ✅")


def test_session_recorder_wav():
    tmp = tempfile.mkdtemp(prefix="mc_wav_test_")
    try:
        rec = SessionRecorder(tmp, log=None)
        n_frames = 40  # 4.0 秒
        t = np.arange(FRAME_SAMPLES, dtype=np.float64) / SR
        for i in range(n_frames):
            amp = 0.2 if (i // 10) % 2 == 0 else 0.05
            frame = (amp * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
            assert rec.write(frame) is True
        path = rec.close()
        assert Path(path).exists(), "wav 文件未生成"
        assert abs(rec.seconds - n_frames * FRAME_S) < 0.05, rec.seconds
        with wave.open(path, "rb") as wf:
            assert wf.getnchannels() == 1, wf.getnchannels()
            assert wf.getsampwidth() == 2, wf.getsampwidth()
            assert wf.getframerate() == SR, wf.getframerate()
            raw = wf.readframes(wf.getnframes())
            nread = wf.getnframes()
        arr = np.frombuffer(raw, dtype="<i2")
        assert nread == n_frames * FRAME_SAMPLES, (nread, n_frames * FRAME_SAMPLES)
        assert arr.size == nread
        assert int(np.max(np.abs(arr.astype(np.int32)))) > 1000, "读回的 wav 数据全零"
        # 同一分钟内再建一个，文件名不能覆盖
        rec2 = SessionRecorder(tmp, log=None)
        path2 = rec2.close()
        assert path2 != path, "同一分钟两次录音文件名冲突"
        print(f"   整场 wav 读回: {Path(path).name} {nread} 帧 "
              f"{nread / SR:.2f}s 非零峰值 {int(np.max(np.abs(arr.astype(np.int32))))}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_asr_empty_input_no_crash():
    cfg = load_config()
    eng = AsrEngine(cfg)
    assert eng.is_ready is False
    assert eng.last_rtf == 0.0
    seg = eng.transcribe_audio(np.zeros(0, dtype=np.float32), t0=0.0)
    assert seg.display_text == "" and seg.rtf == 0.0
    seg2 = eng.transcribe_audio(np.zeros(int(0.05 * SR), dtype=np.float32), t0=1.0)
    assert seg2.display_text == ""
    seg3 = eng.transcribe(AudioChunk(data=np.zeros(0, dtype=np.float32),
                                     t_start=2.0, t_end=2.0))
    assert seg3.display_text == ""
    seg4 = eng.transcribe(AudioChunk(data=np.zeros(int(0.5 * SR), dtype=np.float32),
                                     t_start=3.0, t_end=3.5))
    assert seg4.display_text == ""
    assert eng.is_ready is False
    print("   空输入/超短输入/未加载模型: 均返回空文本且不崩 ✅")


def test_text_postprocess():
    s = normalize_text("這是 一個 測試，我們來說明這個問題。")
    assert s == "这是一个测试，我们来说明这个问题。", s
    s2 = normalize_text("能力,而不是让它的轨迹")
    assert s2 == "能力，而不是让它的轨迹", s2
    s3 = normalize_text("   ")
    assert s3 == "", repr(s3)
    assert is_meaningful("这个实验的ablation结果怎么样？") is True
    assert is_meaningful("。") is False
    assert is_meaningful("。。。！？") is False
    assert is_meaningful("嗯，啊，呃") is False
    assert is_meaningful("字幕由志愿者提供") is False
    assert is_meaningful("") is False
    dedup = _strip_overlap("…我们来看这个实验。", "这个实验的结果很好。")
    assert dedup == "的结果很好。", dedup
    assert WINDOW_S == 30.0 and OVERLAP_S == 1.0 and STRIDE_S == 29.0
    # 退化重复输出识别
    assert _looks_degenerate("组组组组组组组组组组") is True
    assert _looks_degenerate("组织组织组织组织组织组织") is True
    assert _looks_degenerate("") is False
    assert _looks_degenerate("这个实验的结果很好") is False
    print(f"   文本后处理: 繁简/标点/过滤/去重/退化识别 OK；窗参数 {WINDOW_S}s+{OVERLAP_S}s 重叠")


def test_prompt_budget_math():
    """prompt + max_new_tokens 必须给 Whisper 448 token 上限留足余量，超长 prompt 不发 cap。"""
    cfg = load_config()
    eng = AsrEngine(cfg)
    cap = eng._max_new_tokens(np.zeros(11 * SR, dtype=np.float32))
    assert cap is not None, "正常 prompt 应能给出 cap"
    assert eng._prompt_token_len() + cap + 12 <= 448, \
        f"预算超限: prompt={eng._prompt_token_len()} cap={cap}"
    # 超长 prompt：预算不足 -> 返回 None（不下发 cap，从而不会触发 ValueError）
    eng2 = AsrEngine(cfg)
    eng2.initial_prompt = "议题：" + "、".join(f"术语{i}" for i in range(120))
    assert eng2._max_new_tokens(np.zeros(11 * SR, dtype=np.float32)) is None, \
        "超长 prompt 应自动放弃 max_new_tokens"
    print(f"   prompt 预算: 正常 cap={cap}（prompt={eng._prompt_token_len()}+cap+12<=448）；"
          f"超长 prompt -> cap=None ✅")


def test_decode_real_audio():
    if REAL_AUDIO is None:
        return "SKIP 设置 MC_TEST_AUDIO_FILE 可测试自己的音频文件"
    if not REAL_AUDIO.is_file():
        raise AssertionError(f"MC_TEST_AUDIO_FILE 指向的文件不存在：{REAL_AUDIO}")
    import time
    t0 = time.perf_counter()
    audio = decode_audio(str(REAL_AUDIO), SR)
    el = time.perf_counter() - t0
    assert audio.dtype == np.float32, audio.dtype
    assert audio.size > SR, f"解码过短: {audio.size / SR:.1f}s"
    assert np.all(np.isfinite(audio)), "解码出现 NaN/Inf"
    assert float(np.max(np.abs(audio))) <= 1.0 + 1e-6, "解码未 clip 到 [-1,1]"
    print(f"   PyAV 解码: {audio.size / SR:.2f}s 音频用时 {el:.3f}s "
          f"峰值 {float(np.max(np.abs(audio))):.3f} ✅")
    return None


def test_capture_api_without_device():
    cfg = load_config()
    cap = LoopbackCapture(cfg, EventBus(), on_utterance=lambda c: None)
    assert isinstance(cap.list_output_devices(), list)
    assert cap.get_level() == 0.0
    assert cap.is_running is False
    assert isinstance(cap.device_name, str)
    cap.pause()
    cap.resume()
    cap.set_device("auto")
    cap.set_device(cap.device_name or "auto")
    cap.stop()  # 未启动时 stop 不能崩
    st = cap.stats()
    assert st["running"] is False and st["chunks"] == 0
    print(f"   LoopbackCapture API: 设备数={len(cap.list_output_devices())}，"
          f"未启动状态 stop/pause/set_device 均正常 ✅")


def test_pause_resume_clock_monotonic():
    """暂停期间不产生时间戳，恢复后会话时钟不回退（字幕时间单调）。"""
    import time
    cfg = load_config()
    cap = LoopbackCapture(cfg, EventBus(), on_utterance=lambda c: None)
    cap._session_t0 = time.monotonic() - 2.0
    t_before = cap._session_now()
    cap.pause()
    time.sleep(0.15)
    cap.resume()
    t_after = cap._session_now()
    assert abs(t_after - t_before) < 0.12, f"恢复后时钟回退/跳变: {t_before:.2f} -> {t_after:.2f}"
    time.sleep(0.12)
    t_next = cap._session_now()
    assert t_next > t_after + 0.08, "恢复后时钟未继续前进"
    print(f"   暂停/恢复时钟: {t_before:.2f}s -> {t_after:.2f}s -> {t_next:.2f}s 单调 ✅")


def test_real_model_smoke():
    if os.environ.get("MC_TEST_ASR_MODEL", "") not in ("1", "true", "TRUE", "yes"):
        return "SKIP 设置 MC_TEST_ASR_MODEL=1 可跑真实模型冒烟"
    cfg = load_config()
    eng = AsrEngine(cfg)
    ref = resolve_model_ref(eng.model_name)
    eng.load()
    assert eng.is_ready
    seg = eng.transcribe_audio(
        np.zeros(int(1.0 * SR), dtype=np.float32), t0=0.0)
    assert seg.display_text == ""
    assert eng.last_rtf >= 0.0
    # 超长 prompt + hotwords 场景：不得抛异常（明早链路回归重点）
    cfg2 = load_config()
    cfg2.setdefault("asr", {})["initial_prompt"] = (
        "议题：" + "、".join(f"物理修复路由器{i}" for i in range(30)))
    cfg2["asr"]["use_hotwords"] = True
    eng2 = AsrEngine(cfg2)
    eng2.load()
    pt = eng2._prompt_exact_tokens()
    cap = eng2._max_new_tokens(np.zeros(int(11 * SR), dtype=np.float32))
    assert cap is None or pt + 32 + cap + 12 <= 448
    s2 = eng2.transcribe_audio(np.zeros(int(2 * SR), dtype=np.float32))
    assert s2 is not None
    # 故意制造 ValueError，验证三级兜底（不会让整场 0 字幕）
    old_cap = eng2._max_new_tokens
    eng2._max_new_tokens = lambda _a: 999
    try:
        s3 = eng2.transcribe_audio(np.zeros(int(2 * SR), dtype=np.float32))
        assert s3 is not None
    finally:
        eng2._max_new_tokens = old_cap
    print(f"   真实模型 {eng.model_name} 就绪（{ref}），静音 1s 返回空文本，"
          f"last_rtf={eng.last_rtf:.2f}；长 prompt({pt} tokens)/hotwords/超预算兜底 均不崩 ✅")


# ---------------------------------------------------------------- 运行
def main() -> int:
    print("=" * 70)
    print("组会助手 · A 模块自测（音频采集 / 能量 VAD / ASR 空输入）")
    print("=" * 70)
    cases = [
        ("电平/音量工具函数", test_level_helpers),
        ("VAD 合成音频切句（2 句）", test_vad_two_sentences),
        ("VAD 冷启动保留首句", test_vad_cold_start_preserves_first_sentence),
        ("VAD 纯静音不出句", test_vad_silence_only),
        ("VAD 噪声底自适应", test_vad_noise_adapt),
        ("VAD max_segment 强切", test_vad_max_segment),
        ("VAD 读取 config.yaml", test_vad_config),
        ("整场 wav 可读回", test_session_recorder_wav),
        ("ASR 空输入不崩", test_asr_empty_input_no_crash),
        ("文本后处理（繁简/过滤/去重）", test_text_postprocess),
        ("prompt/448 token 预算", test_prompt_budget_math),
        ("PyAV 解码用户指定的真实音频（可选）", test_decode_real_audio),
        ("LoopbackCapture 公开 API", test_capture_api_without_device),
        ("暂停/恢复时钟单调", test_pause_resume_clock_monotonic),
        ("真实 whisper 模型冒烟（可选）", test_real_model_smoke),
    ]
    failed = 0
    skipped = 0
    for name, fn in cases:
        try:
            ret = fn()
            if isinstance(ret, str) and ret.startswith("SKIP"):
                skipped += 1
                print(f"  ⚠ {name}: {ret}")
            else:
                print(f"  ✅ {name}")
        except AssertionError as e:
            failed += 1
            print(f"  ❌ {name}: 断言失败: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ❌ {name}: {type(e).__name__}: {e}")
            traceback.print_exc()
    total = len(cases) - skipped
    print("-" * 70)
    print(f"结果: {total - failed}/{total} 通过, {failed} 失败, {skipped} 跳过")
    print("✅ 全部通过" if failed == 0 else "❌ 存在失败项")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
