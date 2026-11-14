# -*- coding: utf-8 -*-
"""Meeting Copilot · 音频文件转写 CLI（A3 模块）

用法（项目根或任意目录均可）：
    python tools/transcribe_file.py <音频文件> [--model small|large-v3-turbo] [--out xx.txt]
                                    [--max-seconds N] [--language zh] [--threads 12]

要点：
    - faster_whisper.audio.decode_audio 与已装 PyAV 19 不兼容
      （TypeError: open() got an unexpected keyword argument 'metadata_errors'），
      所以这里自己用 av 解码：av.open + AudioResampler(format="flt", layout="mono", rate=sr)
      + frame.to_ndarray()。实测 4.9 分钟 m4a 解码 0.3s。
    - 长音频按 30s 窗口 + 1s 重叠逐段转录，打印进度；--max-seconds 只转前 N 秒。
    - 中文输出统一 UTF-8，不受 Windows 控制台 GBK 影响。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# 允许 `python tools/transcribe_file.py`（此时 sys.path[0] 是 tools/，需补项目根）
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import numpy as np

from config import get, init_console, load_config
from contracts import SAMPLE_RATE, fmt_ts

# 长音频切窗参数（契约要求：30s 窗口 + 1s 重叠）
WINDOW_S = 30.0
OVERLAP_S = 1.0
STRIDE_S = WINDOW_S - OVERLAP_S


def decode_audio(path: str, sr: int = SAMPLE_RATE) -> np.ndarray:
    """用 PyAV 解码任意音视频文件为 mono float32 @ sr（默认 16k）。失败抛 RuntimeError。

    注意：不要用 faster_whisper.audio.decode_audio（PyAV 19 不兼容）。
    """
    import av  # 惰性导入：只解码时付出导入成本

    p = Path(path)
    if not p.exists():
        raise RuntimeError(f"音频文件不存在: {path}")
    parts: list[np.ndarray] = []
    with av.open(str(p)) as container:
        streams = [s for s in container.streams if s.type == "audio"]
        if not streams:
            raise RuntimeError(f"文件里没有音频流: {path}")
        stream = streams[0]
        resampler = av.AudioResampler(format="flt", layout="mono", rate=int(sr))
        for frame in container.decode(stream):
            frame.pts = None  # 避免部分容器 pts 异常导致 resampler 报错
            for out in resampler.resample(frame):
                arr = out.to_ndarray()
                parts.append(arr.reshape(-1).astype(np.float32))
        # 冲掉 resampler 内部缓冲
        for out in resampler.resample(None):
            arr = out.to_ndarray()
            parts.append(arr.reshape(-1).astype(np.float32))
    if not parts:
        return np.zeros(0, dtype=np.float32)
    audio = np.concatenate(parts).astype(np.float32, copy=False)
    np.clip(audio, -1.0, 1.0, out=audio)
    return audio


def _cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk / max(1, len(text))


def _write_out(path: str, src: str, model: str, audio_sec: float, total_sec: float,
               asr_elapsed: float, avg_rtf: float, segs) -> None:
    out = Path(path)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    lines = [
        "# Meeting Copilot · 音频转写结果",
        f"# 源文件: {src}",
        f"# 模型: {model} | 转写区间: {audio_sec:.2f}s / 总时长 {total_sec:.2f}s",
        f"# 用时: {asr_elapsed:.2f}s | 平均 RTF: {avg_rtf:.3f} | 分段: {len(segs)}",
        f"# 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    for seg in segs:
        lines.append(f"[{fmt_ts(seg.t_start)} - {fmt_ts(seg.t_end)}] {seg.display_text}")
    full = "".join(seg.display_text for seg in segs)
    lines += ["", "===== 全文 =====", full, ""]
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main(argv=None) -> int:
    init_console()
    ap = argparse.ArgumentParser(
        description="Meeting Copilot · 音频文件转写（PyAV 解码 + faster-whisper，支持 m4a/mp3/wav/mp4）")
    ap.add_argument("audio", help="音频/视频文件路径")
    ap.add_argument("--model", default=None,
                    help="模型: small（默认，RTF≈0.13）| large-v3-turbo（更准）| 本地目录")
    ap.add_argument("--out", default=None, help="输出 txt 路径（UTF-8）；不填只打印")
    ap.add_argument("--max-seconds", type=float, default=None,
                    help="只转写前 N 秒（默认整段；长音频建议限量）")
    ap.add_argument("--language", default=None, help="语言（默认取 config.yaml asr.language=zh）")
    ap.add_argument("--threads", type=int, default=None, help="CPU 线程数（默认取配置）")
    args = ap.parse_args(argv)

    try:
        cfg = load_config()
    except Exception as e:
        print(f"❌ 配置加载失败: {e}")
        return 1
    if args.model:
        cfg.setdefault("asr", {})["model"] = args.model
    if args.language:
        cfg.setdefault("asr", {})["language"] = args.language
    if args.threads:
        cfg.setdefault("asr", {})["cpu_threads"] = args.threads
    model_name = str(get(cfg, "asr.model", "small"))

    # ---- 1) 解码
    t0 = time.perf_counter()
    try:
        audio = decode_audio(args.audio, SAMPLE_RATE)
    except Exception as e:
        print(f"❌ 解码失败: {e}")
        return 2
    decode_elapsed = time.perf_counter() - t0
    total_sec = len(audio) / float(SAMPLE_RATE)
    if total_sec < 0.1:
        print(f"❌ 音频太短或为空（{total_sec:.2f}s）: {args.audio}")
        return 2
    use_sec = total_sec if not args.max_seconds or args.max_seconds <= 0 \
        else min(total_sec, float(args.max_seconds))
    print(f"[解码] {Path(args.audio).name}: {total_sec:.2f}s -> 16k mono "
          f"({decode_elapsed:.3f}s, 峰值 {float(np.max(np.abs(audio))):.3f})")
    if use_sec < total_sec:
        print(f"[范围] 仅转写前 {use_sec:.1f}s / 总长 {total_sec:.1f}s")
    del audio

    # ---- 2) 加载模型
    from asr_engine import AsrEngine, resolve_model_ref

    print(f"[模型] {model_name} ({resolve_model_ref(model_name)})")
    eng = AsrEngine(cfg)
    t0 = time.perf_counter()
    try:
        eng.load()
    except Exception as e:
        print(f"❌ 模型加载失败: {e}")
        return 3
    print(f"[模型] 加载完成 {time.perf_counter() - t0:.2f}s | cpu_threads="
          f"{eng.cpu_threads} compute={eng.compute_type}")
    t0 = time.perf_counter()
    try:
        eng.warmup()
    except Exception as e:
        print(f"[warn] warmup 失败（忽略）: {e}")

    # ---- 3) 转写（30s 窗口 + 1s 重叠，进度回调）
    def on_progress(idx, n, start, end, seg):
        preview = (seg.display_text or "").strip()
        if len(preview) > 42:
            preview = preview[:42] + "…"
        print(f"[进度] {idx}/{n} {fmt_ts(start)}-{fmt_ts(end)} "
              f"rtf={seg.rtf:.2f} | {preview}")

    t_asr = time.perf_counter()
    try:
        segs = eng.transcribe_file(args.audio, max_seconds=use_sec, on_progress=on_progress)
    except Exception as e:
        print(f"❌ 转写失败: {e}")
        import traceback
        traceback.print_exc()
        return 4
    asr_elapsed = time.perf_counter() - t_asr
    avg_rtf = asr_elapsed / max(use_sec, 0.01)
    full_text = "".join(s.display_text for s in segs)
    print("-" * 66)
    print(f"[完成] 转写 {use_sec:.1f}s 音频 | {len(segs)} 段 | 文本 {len(full_text)} 字 | "
          f"用时 {asr_elapsed:.2f}s | 平均 RTF {avg_rtf:.3f} | 中文占比 {_cjk_ratio(full_text):.0%}")
    print(f"[样例] {full_text[:120]}")
    if not full_text:
        print("[warn] 没有识别出文本（检查音频是否有语音 / 模型是否匹配）")
    if args.out:
        _write_out(args.out, args.audio, model_name, use_sec, total_sec,
                   asr_elapsed, avg_rtf, segs)
        print(f"[输出] {Path(args.out).resolve()}")
    return 0 if segs else 5


if __name__ == "__main__":
    sys.exit(main())
