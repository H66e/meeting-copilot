# -*- coding: utf-8 -*-
"""Meeting Copilot · 2 分钟彩排（明早开会前跑一遍，确认"真的能听清会"）

做三件事：
1. 起一个真实模式的助手（headless，不弹窗），确认模型/知识库/热键都就绪；
2. 把上次组会录音的一段（默认 60 秒）播放到默认扬声器，模拟腾讯会议声音；
3. 统计实时字幕条数 / 延迟，并实际生成一次「要点/应答思路」，确认双脑链路通。

用法：
    python tools/rehearse.py            # 彩排（会外放 60 秒录音）
    python tools/rehearse.py --seconds 30
    python tools/rehearse.py --audio "D:\\某段会议录音.m4a"
"""
from __future__ import annotations

import argparse
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 录音只能由用户显式提供，不随项目附带，也不默认查找私人目录。
import os
DEFAULT_AUDIO = os.environ.get("MC_TEST_AUDIO_FILE", "")


def _decode(path: str, sr: int = 16000):
    import numpy as np
    from tools.transcribe_file import decode_audio  # 自带 PyAV 解码
    return decode_audio(path, sr=sr) if "sr" in decode_audio.__code__.co_varnames else decode_audio(path)


def main() -> int:
    ap = argparse.ArgumentParser(description="Meeting Copilot彩排：真实回环 + 听写 + 双脑一次")
    ap.add_argument("--seconds", type=float, default=60.0, help="播放/彩排时长（默认 60 秒）")
    ap.add_argument("--audio", default=str(DEFAULT_AUDIO), help="用来模拟会议声音的音频文件")
    ap.add_argument("--keep-app", action="store_true", help="彩排后不关闭助手")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    audio_path = Path(args.audio)
    if not audio_path.exists():
        print(f"❌ 找不到彩排音频：{audio_path}")
        print("   可指定自己的会议录音：python tools/rehearse.py --audio \"D:\\xxx.m4a\"")
        return 2

    print("=" * 70)
    print("Meeting Copilot · 彩排开始（外放一段会议录音，模拟腾讯会议声音）")
    print("=" * 70)

    # 1) 启动助手（headless 真实模式）
    print("[1/3] 启动助手（真实回环模式）…")
    t0 = time.time()
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "main.py"), "--no-ui"],
        cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    lines: list[str] = []
    ready = threading.Event()
    got_brain = threading.Event()

    def reader() -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.rstrip("\n")
            lines.append(line)
            if "已开始聆听" in line:
                ready.set()
            if line.startswith("[") and ("answer/" in line or "hint/" in line):
                got_brain.set()

    threading.Thread(target=reader, daemon=True).start()
    if not ready.wait(90):
        print("❌ 90 秒内没等到「已开始聆听」，最近日志：")
        for l in lines[-15:]:
            print("   " + l[:180])
        proc.kill()
        return 1
    print(f"    ✅ 就绪（{time.time()-t0:.1f}s），模型加载完成")

    # 2) 外放录音
    print(f"[2/3] 外放 {args.seconds:.0f} 秒会议录音到默认扬声器…")
    try:
        import numpy as np
        import soundcard as sc
        from tools.transcribe_file import decode_audio
        audio = decode_audio(str(audio_path))[: int(16000 * args.seconds)]
        sc.default_speaker().play(np.clip(audio * 2.0, -1, 1).astype("float32"), samplerate=16000)
    except Exception as e:
        print(f"   ⚠️ 外放失败：{type(e).__name__}: {e}")
        print("      可以自己放一段会议视频/音频代替，然后观察浮窗是否出字幕。")

    # 3) 等字幕 + 大脑
    print("[3/3] 等待听写与 AI 建议…")
    deadline = time.time() + args.seconds + 35
    while time.time() < deadline:
        n = sum(1 for l in lines if l.startswith("[字幕"))
        if n >= 3 and got_brain.is_set():
            break
        time.sleep(1.0)

    segs = [l for l in lines if l.startswith("[字幕")]
    brains = [l for l in lines if l.startswith("[") and ("answer/" in l or "hint/" in l)]
    print("\n" + "=" * 70)
    print(f"字幕 {len(segs)} 条 | AI 建议 {len(brains)} 条")
    for s in segs[:6]:
        print("   " + s[:150])
    for b in brains[:2]:
        print("   " + b[:400])
    errs = [l for l in lines if "Traceback" in l or "识别异常" in l or "网络错误" in l]
    if errs:
        print(f"\n⚠️ 有 {len(errs)} 条错误日志（不影响彩排结论的话可忽略）：")
        for e in errs[:3]:
            print("   " + e[:180])

    if not args.keep_app:
        try:
            proc.stdin.write("quit\n"); proc.stdin.flush()  # type: ignore[union-attr]
        except Exception:
            pass
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()

    ok = len(segs) >= 2
    print("\n" + ("✅ 彩排通过：回环录音 → 实时字幕 链路正常，可以开会。" if ok
                  else "❌ 彩排未通过：字幕少于 2 条，请把上面的日志发我。"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
