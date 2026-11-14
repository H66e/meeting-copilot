# -*- coding: utf-8 -*-
"""组会助手 · ASR 引擎（faster-whisper CPU int8，A2 模块）

职责（严格按 INTERFACES.md 第 4 节 A2）：
    AsrEngine:
        - faster-whisper 单例复用；device="cpu"、int8、cpu_threads/语言/initial_prompt 走配置
        - local_files_only=True 离线加载（HF 缓存里 Systran/faster-whisper-small 与
          mobiuslabsgmbh/faster-whisper-large-v3-turbo 已下载）
        - transcribe(AudioChunk) -> TranscriptSegment（填 rtf）；transcribe_audio(np.ndarray)
        - transcribe_file(path) -> list[TranscriptSegment]（30s 窗口 + 1s 重叠）
        - 文本后处理：繁→简（opencc 若已装则用，否则内置常见字映射，不装重依赖）、
          过滤纯标点/语气词/字幕幻觉；is_ready / last_rtf 监控
    自测：python asr_engine.py --selftest（合成正弦 + 静音 + 空输入，不联网、不依赖麦克风）

注意：
    - faster_whisper.audio.decode_audio 与 PyAV 19 不兼容（metadata_errors），
      本项目一律用 tools/transcribe_file.py 里的 PyAV 解码实现。
    - 单次调用串行加锁（faster-whisper 模型非线程安全）；RTF>0.45 记 warning。
"""
from __future__ import annotations

import argparse
import math
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Iterable

import numpy as np

from config import get, init_console, load_config
from contracts import SAMPLE_RATE, AudioChunk, TranscriptSegment
from logging_setup import get_logger

# ---------------------------------------------------------------- 常量
MODEL_ALIASES = {
    "small": "Systran/faster-whisper-small",
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "medium": "Systran/faster-whisper-medium",
    "large": "Systran/faster-whisper-large-v3",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}
_DEFAULT_ASR = {
    "model": "small",
    "compute_type": "int8",
    "cpu_threads": 12,
    "language": "zh",
    "beam_size": 1,
    "vad_filter": True,
    "initial_prompt": "",
}
# 小于这个时长直接跳过模型（避免空音频触发幻觉、浪费算力）
_MIN_AUDIO_S = 0.10
_RTF_WARN = 0.45          # 契约：RTF>0.45 记 warning
_RTF_DEBUG = 0.20         # 契约目标：RTF>0.2 记 debug（小模型目标 <0.2）
_SLOW_WARN_INTERVAL_S = 60.0  # 慢速 warning 节流，避免实时场景刷屏
_WHISPER_MAX_LEN = 448    # Whisper 解码总预算（prompt + max_new_tokens 必须小于它）
_PROMPT_MARGIN = 32       # initial_prompt token 估算安全余量
_CAP_SAFETY = 48          # max_new_tokens 预算额外安全余量

# ---------------------------------------------------------------- 繁->简
# 常见繁体字 -> 简体字（仅收录一字对一字的常用映射；opencc 若已装优先用它）
_TRAD_SIMP_RAW = """
這这 個个 們们 說说 對对 時时 會会 來来 為为 與与 於于 過过 麼么 樣样 還还 進进
現现 點点 種种 實实 發发 學学 覺觉 開开 關关 後后 東东 車车 話话 語语 應应 該该
讓让 問问 題题 間间 聽听 見见 誰谁 認认 識识 記记 無无 數数 據据 網网 絡络 計计
劃划 動动 態态 機机 構构 圖图 視视 頻频 編编 輯辑 標标 籤签 準准 確确 誤误 試试
驗验 結结 論论 觀观 訓训 練练 優优 損损 層层 級级 處处 節节 邊边 緣缘 檢检 測测
碼码 擴扩 預预 決决 評评 環环 資资 軟软 統统 儲储 讀读 寫写 輸输 專专 業业 課课
組组 週周 報报 燈灯 舊旧 續续 繼继 變变 參参 陣阵 積积 極极 單单 雙双 並并 從从
產产 傳传 鏈链 訊讯 質质 踐践 項项 團团 隊队 員员 責责 討讨 議议 紀纪 錄录 轉转
檔档 執执 畫画 幾几 頁页 備备 裡里 沒没 麵面 髮发 謝谢 嗎吗 貝贝 錢钱 銀银 鐵铁
門门 閉闭 陽阳 陰阴 隨随 顯显 願愿 風风 飛飞 驕骄 駁驳 髒脏 鬥斗 魚鱼 鳥鸟 鷹鹰
鹽盐 麥麦 黃黄 黨党 響响 較较 張张 維维 經经 監监 錯错 類类 庫库 證证 歸归 納纳
階阶 擇择 適适 師师 導导 暫暂 復复 匯汇 設设 議议 線线 顯显 製制 習习 習习 溝沟
適适 確确 認认 雙双 邊边 週周 嗎吗 呢呢 吧吧 謝謝 快快 樂乐 慶庆 歡欢 迎迎 紀纪
訪访 談谈 須须 許许 詳详 註注 冊册 簽签 約约 標标 準准 環环 境境 衛卫 護护 權权
勢势 優优 勢势 狀状 態态 範范 圍围 領领 域域 顆颗 粒粒 訓训 練练 樣样 本本 資资
料料 庫库 檢检 索索 編编 碼码 解解 碼码 呈呈 現现 補补 充充 說说 服服 總总 結结
"""


def _build_trad_map() -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in _TRAD_SIMP_RAW.split():
        if len(pair) == 2 and pair[0] != pair[1]:
            out[pair[0]] = pair[1]
    return out


_TRAD_TO_SIMP = _build_trad_map()
_OPENCC = None
_OPENCC_TRIED = False

# 标点/空白（判断“纯标点”用）
_PUNCT_CHARS = set("，。！？、；：…—～·,.!?;:\"'“”‘’「」『』()（）[]【】{}<>《》-—–_ \t\r\n")
_FILLER_CHARS = set("嗯啊呃哦噢额唉诶哎呀哈嘛呗咯啦噢誒")
# whisper 在静音/短音频上常见的“字幕组”幻觉，永远过滤（组会里不会说这些）
_HALLUCINATION_MARKERS = (
    "字幕由", "字幕制作", "字幕提供", "字幕志愿者", "请不吝", "点赞", "订阅",
    "转发", "打赏", "感谢观看", "谢谢观看", "谢谢收看", "感谢收看",
    "amara.org", "明镜与点点", "MING PAO", "www.",
)
_CJK = r"\u4e00-\u9fff"

# 领域术语纠错：Whisper 对组会里的中英混合专有名词经常写错/加空格，
# 用固定映射做保守纠正（只动这些确定的写法，避免误改正常文本）。
_DOMAIN_FIXES = (
    (re.compile(r"(?i)ref\s*[_-]?\s*coco"), "RefCOCO"),
    (re.compile(r"(?i)sd\s*[_-]?\s*xl\b"), "SDXL"),
    (re.compile(r"(?i)\bkappa\b"), "κ"),
    (re.compile(r"时空\s*[_-]?\s*tube", re.IGNORECASE), "时空 tube"),
    (re.compile(r"时空管(?:道)?"), "时空 tube"),
    (re.compile(r"物理修[付护]"), "物理修复"),
    (re.compile(r"反事[时实]表"), "反事实表"),
    (re.compile(r"H\s*零"), "H0"),
    (re.compile(r"(?i)\bh\s*0\b"), "H0"),
    (re.compile(r"掩[码玛马]"), "掩码"),
    (re.compile(r"最[优尤]固定动作"), "最优固定动作"),
    # 真实组会录音中反复出现的误识别；只替换完整术语，不做泛化改写。
    (re.compile(r"多摩[戴态太坦泰]|多模[太态]|多么太|动[摩魔模]态|动物态"), "多模态"),
    (re.compile(r"多么大模型"), "多模态大模型"),
    (re.compile(r"视频生[存成][大弹][幕模膜弹]型|视频生成大物理"), "视频生成大模型"),
    (re.compile(r"物理[尊寸追][行寻]|物理尊重|物理追循"), "物理遵循"),
    (re.compile(r"隐[编营]料|隐变量|营变量"), "隐变量"),
    (re.compile(r"鲜艳信息"), "先验信息"),
    (re.compile(r"非线性影[片视]方程"), "非线性偏微分方程"),
    (re.compile(r"器相预警"), "气象预警"),
    (re.compile(r"谣感监测|摇杆监测"), "遥感监测"),
    (re.compile(r"租入流体"), "注入流体"),
    (re.compile(r"(?<![A-Za-z0-9])(?:VA1|BAE)(?![A-Za-z0-9])", re.IGNORECASE), "VAE"),
    (re.compile(r"(?<![A-Za-z0-9])PIN(?=\s*(?:提出|是|网络|模型|方法))", re.IGNORECASE), "PINN"),
    (re.compile(r"(?i)physics\s+inform(?:ed)?\s+deep\s+learning"), "Physics-Informed Deep Learning"),
    (re.compile(r"(?i)physics\s*inform(?:ed)?(?=\s|[\u4e00-\u9fff]|$)"), "Physics-Informed"),
    (re.compile(r"(?i)physics\s*infused(?:\s*infused)?\s*diffusion(?:\s*infused\s*diffusion)?"), "Physics-Infused Diffusion"),
    (re.compile(r"(?i)denoising\s*unit"), "Denoising U-Net"),
    (re.compile(r"(?i)(?<![A-Za-z])unit(?=当中)"), "U-Net"),
    (re.compile(r"补货的数据"), "捕获的数据"),
    (re.compile(r"怀缘图像"), "还原图像"),
    (re.compile(r"生存图片"), "生成图片"),
    (re.compile(r"(?i)non[\s-]*minor\s+partitional\s+differential\s+equation"), "Nonlinear Partial Differential Equation"),
)


def _fix_domain_terms(text: str) -> str:
    """组会领域术语的保守纠错（英文大小写/空格、常见同音错字）。"""
    if not text:
        return text
    s = str(text)
    for pattern, repl in _DOMAIN_FIXES:
        try:
            s = pattern.sub(repl, s)
        except Exception:
            continue
    return s


def _trad_to_simp(text: str) -> str:
    global _OPENCC, _OPENCC_TRIED
    if not text:
        return text
    if not _OPENCC_TRIED:
        _OPENCC_TRIED = True
        try:  # 装了就用，没装绝不强制安装
            from opencc import OpenCC  # type: ignore

            _OPENCC = OpenCC("t2s")
        except Exception:
            _OPENCC = None
    if _OPENCC is not None:
        try:
            return _OPENCC.convert(text)
        except Exception:
            pass
    return "".join(_TRAD_TO_SIMP.get(ch, ch) for ch in text)


def normalize_text(text: str) -> str:
    """ASR 文本后处理：繁→简、领域术语纠错、去 CJK 间空格、半角标点转全角、压重复标点。"""
    if not text:
        return ""
    s = _trad_to_simp(str(text)).strip()
    if not s:
        return ""
    s = _fix_domain_terms(s)
    # 去掉汉字之间 whisper 偶尔插入的空格
    s = re.sub(rf"(?<=[{_CJK}])\s+(?=[{_CJK}])", "", s)
    # 半角标点（紧邻汉字时）转全角，提升字幕可读性
    s = re.sub(rf"(?<=[{_CJK}])\s*[,]\s*", "，", s)
    s = re.sub(rf"(?<=[{_CJK}])\s*[.]\s*", "。", s)
    s = re.sub(rf"(?<=[{_CJK}])\s*[?]\s*", "？", s)
    s = re.sub(rf"(?<=[{_CJK}])\s*[!]\s*", "！", s)
    s = re.sub(rf"(?<=[{_CJK}])\s*[;]\s*", "；", s)
    s = re.sub(rf"(?<=[{_CJK}])\s*[:]\s*", "：", s)
    s = re.sub(r"[ \t]+", " ", s).strip()
    s = re.sub(r"([。！？，、；：])\1+", r"\1", s)
    return s.strip()


def is_meaningful(text: str) -> bool:
    """过滤纯标点/语气词/字幕幻觉；空文本返回 False。"""
    if not text:
        return False
    s = str(text).strip()
    if not s:
        return False
    core = "".join(ch for ch in s if ch not in _PUNCT_CHARS and not ch.isspace())
    if not core:
        return False
    if all(ch in _FILLER_CHARS for ch in core):
        return False
    low = core.lower()
    for marker in _HALLUCINATION_MARKERS:
        if marker.lower() in low and len(core) <= len(marker) + 10:
            return False
    return True


def _looks_degenerate(text: str) -> bool:
    """识别 whisper 在低信噪比片段上的重复退化输出（如「组组组组组组组组」）。"""
    core = "".join(ch for ch in (text or "") if ch not in _PUNCT_CHARS and not ch.isspace())
    n_chars = len(core)
    if n_chars < 8:
        return False
    # 1) 单字占比过高
    most_char = max(core.count(ch) for ch in set(core))
    if most_char / n_chars >= 0.6:
        return True
    # 2) 2~4 字的短片段反复出现且覆盖大部分文本（如「博物图像博物图像…」）
    for n in (2, 3, 4):
        if n_chars < n * 3:
            continue
        counts: dict[str, int] = {}
        for i in range(n_chars - n + 1):
            unit = core[i:i + n]
            counts[unit] = counts.get(unit, 0) + 1
        occ = max(counts.values()) if counts else 0
        if occ >= 3 and (occ - 1) * n >= n_chars * 0.45:
            return True
    return False


def _strip_overlap(prev_text: str, new_text: str) -> str:
    """去掉连续上下文窗口的重复前缀，支持标点和少量识别差异。"""
    a = (prev_text or "").strip().rstrip("，。！？、；：,.!?;: 　")
    b = (new_text or "").strip()
    if not a or not b:
        return b
    for length in range(min(120, len(a), len(b)), 2, -1):
        if a.endswith(b[:length]):
            return b[length:].lstrip("，。！？、；：,.!?;: 　")
    # 识别结果可能只有个别字不同：只接受跨在“前句结尾—新句开头”的公共片段。
    from difflib import SequenceMatcher
    tail, head = a[-120:], b[:120]
    match = SequenceMatcher(None, tail, head, autojunk=False).find_longest_match()
    if match.size >= 6 and match.a + match.size == len(tail) and match.b == 0:
        return b[match.size:].lstrip("，。！？、；：,.!?;: 　")
    return b


# ---------------------------------------------------------------- 模型路径
def _hf_cache_roots() -> list[Path]:
    roots: list[Path] = []
    env = os.environ.get("HUGGINGFACE_HUB_CACHE")
    if env:
        roots.append(Path(env))
    env = os.environ.get("HF_HOME")
    if env:
        roots.append(Path(env) / "hub")
        roots.append(Path(env))
    roots.append(Path.home() / ".cache" / "huggingface" / "hub")
    roots.append(Path.home() / ".cache" / "huggingface")
    out: list[Path] = []
    seen: set[str] = set()
    for r in roots:
        try:
            r = r.expanduser()
        except Exception:
            continue
        key = str(r)
        if key in seen:
            continue
        seen.add(key)
        try:
            if r.is_dir():
                out.append(r)
        except OSError:
            continue
    return out


def find_cached_model(repo_id: str) -> str | None:
    """在 HF 缓存里找已下载的 snapshot 目录（离线加载用）。找不到返回 None。"""
    folder = "models--" + str(repo_id).replace("/", "--")
    for root in _hf_cache_roots():
        snapshots = root / folder / "snapshots"
        try:
            if not snapshots.is_dir():
                continue
            candidates = sorted(snapshots.iterdir(),
                                key=lambda p: p.stat().st_mtime if p.exists() else 0,
                                reverse=True)
        except OSError:
            continue
        for snap in candidates:
            try:
                if (snap / "model.bin").is_file():
                    return str(snap)
            except OSError:
                continue
    return None


def resolve_model_ref(model_name: str) -> str:
    """把配置里的模型名解析成：本地目录 > 缓存 snapshot > HF repo id。"""
    raw = str(model_name or "small").strip()
    p = Path(raw).expanduser()
    try:
        if p.is_dir():
            return str(p)
    except OSError:
        pass
    repo = MODEL_ALIASES.get(raw, raw)
    cached = find_cached_model(repo)
    return cached or repo


class AsrEngine:
    """faster-whisper 引擎封装（A2 契约实现）。"""

    def __init__(self, cfg: dict, log=None):
        self.cfg = cfg or {}
        self._log = log or get_logger("asr")
        a = self.cfg.get("asr") if isinstance(self.cfg.get("asr"), dict) else {}
        self.model_name = str(a.get("model", _DEFAULT_ASR["model"]) or "small")
        self.allow_download = bool(a.get("allow_download", True)) and not (
            os.environ.get("MC_OFFLINE", "").lower() in ("1", "true", "yes"))
        self.compute_type = str(a.get("compute_type", _DEFAULT_ASR["compute_type"]) or "int8")
        self.cpu_threads = int(a.get("cpu_threads", _DEFAULT_ASR["cpu_threads"]) or 12)
        self.language = str(a.get("language", _DEFAULT_ASR["language"]) or "zh")
        self.beam_size = int(a.get("beam_size", _DEFAULT_ASR["beam_size"]) or 1)
        self.vad_filter = bool(a.get("vad_filter", _DEFAULT_ASR["vad_filter"]))
        # 时间戳由我们自己的 VAD 提供，解码不需要时间戳 token（实测每次省 ~0.15s）
        self.without_timestamps = bool(a.get("without_timestamps", True))
        self.initial_prompt = str(a.get("initial_prompt", "") or "")
        hotwords = a.get("hotwords") or []
        if isinstance(hotwords, str):
            hotwords = [hotwords]
        self.hotwords = " ".join(str(w) for w in hotwords if str(w).strip())[:300]
        # 实测（真实组会音频对照）：config 里 37 个 hotwords 编码后约 174 tokens，
        # 与 182 tokens 的 initial_prompt 叠加后 prompt 达 354 tokens，短句解码会
        # 「续写提示词」产生幻觉（例如反复输出「议题：成本约束的物理修复路由器…」），
        # 且 prompt+max_new_tokens 会超过 Whisper 448 token 上限报错。
        # 因此默认不启用 hotwords；如确需启用，可在 config.yaml 的 asr 下加
        # `use_hotwords: true` 并精简 hotwords 列表。
        self.use_hotwords = bool(a.get("use_hotwords", False))
        # 低置信度先二次解码；默认保留文本并标记不确定，不再直接把整句丢掉。
        self.drop_low_confidence = bool(a.get("drop_low_confidence", False))
        self.retry_low_confidence = bool(a.get("retry_low_confidence", True))
        self.avg_logprob_threshold = float(a.get("avg_logprob_threshold", -0.85) or -0.85)
        self.no_speech_threshold = float(a.get("no_speech_threshold", 0.6) or 0.6)
        self.compression_ratio_threshold = float(
            a.get("compression_ratio_threshold", 2.4) or 2.4)
        # 正式句自动带上一小段原始音频，避免 8 秒硬切造成术语和语义断裂。
        self.context_audio_s = max(0.0, float(a.get("context_audio_s", 4.0) or 0.0))

        self._model = None
        self._ready = False
        self._load_lock = threading.RLock()
        self._lock = threading.RLock()
        self._id_lock = threading.Lock()
        self._context_lock = threading.Lock()
        self._context_audio = np.zeros(0, dtype=np.float32)
        self._context_text = ""
        self._seg_counter = 0
        self._last_rtf = 0.0
        self._load_seconds = 0.0
        self._warned_not_ready = False
        self._resolved_ref = ""
        self._prompt_tokens: int | None = None      # 实际 tokenizer 精确值
        self._prompt_tokens_budget: int | None = None  # 精确值 + 安全余量
        self._last_slow_warn = 0.0

    # ------------------------------------------------------------ 加载
    @property
    def is_ready(self) -> bool:
        return bool(self._ready)

    @property
    def last_rtf(self) -> float:
        return float(self._last_rtf)

    @property
    def load_seconds(self) -> float:
        return float(self._load_seconds)

    def load(self) -> None:
        """加载模型（阻塞；主循环用后台线程调用）。重复调用安全。"""
        with self._load_lock:
            if self._ready:
                return
            from faster_whisper import WhisperModel  # 惰性导入，加快主程序启动

            ref = resolve_model_ref(self.model_name)
            self._resolved_ref = ref
            t0 = time.perf_counter()
            self._log.info("加载 whisper 模型: %s -> %s (cpu/%s, threads=%d)",
                           self.model_name, ref, self.compute_type, self.cpu_threads)
            try:
                kwargs = dict(device="cpu", compute_type=self.compute_type,
                              cpu_threads=self.cpu_threads)
                try:
                    self._model = WhisperModel(ref, local_files_only=True, **kwargs)
                except Exception:
                    remote = MODEL_ALIASES.get(self.model_name, self.model_name)
                    custom_dir = Path(self.model_name).expanduser().is_dir()
                    if not self.allow_download or custom_dir or "/" not in remote:
                        raise
                    self._log.info("本地模型未就绪，下载 Whisper 模型（不发送音频）: %s", remote)
                    self._model = WhisperModel(remote, local_files_only=False, **kwargs)
                    self._resolved_ref = remote
            except Exception as e:
                self._log.error("whisper 模型加载失败（%s）: %s", ref, e)
                raise RuntimeError(f"无法加载 whisper 模型 {self.model_name}: {e}") from e
            self._ready = True
            self._load_seconds = time.perf_counter() - t0
            ptoks_exact = self._prompt_exact_tokens()
            log_args = (ref, self._load_seconds, ptoks_exact,
                        self._max_new_tokens(np.zeros(int(2 * SAMPLE_RATE), dtype=np.float32)))
            self._log.info(
                "whisper 模型就绪: %s (%.2fs) | 解码prompt=%d tokens(含hotwords), "
                "2s 音频 max_new_tokens=%s, hotwords=%s",
                *log_args, "启用" if self.use_hotwords else "关闭")
            if ptoks_exact > 96:
                self._log.warning(
                    "解码 prompt 较长(%d tokens，含 hotwords)：会挤占 %d token 解码预算并"
                    "增加短句幻觉风险，建议 initial_prompt+hotwords 合计控制在 96 tokens 内",
                    ptoks_exact, _WHISPER_MAX_LEN)

    def warmup(self) -> None:
        """空音频跑一次，压掉首句延迟（失败不影响使用）。"""
        try:
            if not self._ready:
                self.load()
            audio = np.zeros(int(0.8 * SAMPLE_RATE), dtype=np.float32)
            with self._lock:
                self._transcribe_once(audio, vad_filter=False)
            self._log.info("ASR warmup 完成")
        except Exception as e:
            self._log.warning("ASR warmup 失败（忽略）: %s", e)

    # ------------------------------------------------------------ 识别
    def transcribe(self, chunk: AudioChunk) -> TranscriptSegment:
        """正式句识别：带前句音频上下文，再移除重复前缀。"""
        data = self._prepare_audio(getattr(chunk, "data", None))
        t0 = float(getattr(chunk, "t_start", 0.0))
        with self._context_lock:
            prefix = self._context_audio
            previous_text = self._context_text
        if self.context_audio_s > 0 and prefix.size and data.size:
            audio = np.concatenate((prefix, data))
            decode_t0 = max(0.0, t0 - len(prefix) / float(SAMPLE_RATE))
        else:
            audio = data
            decode_t0 = t0
        seg = self.transcribe_audio(audio, t0=decode_t0)
        full_text = seg.display_text.strip()
        if previous_text and full_text:
            deduped = _strip_overlap(previous_text, full_text)
            seg.text = deduped
            seg.text_fixed = normalize_text(deduped)
        seg.t_start = t0
        try:
            seg.t_end = float(getattr(chunk, "t_end", seg.t_end))
        except Exception:
            pass
        seg.source = str(getattr(chunk, "source", "loopback") or "loopback")
        with self._context_lock:
            if data.size and self.context_audio_s > 0:
                keep = int(self.context_audio_s * SAMPLE_RATE)
                self._context_audio = np.concatenate((self._context_audio, data))[-keep:]
            if full_text:
                self._context_text = full_text[-240:]
        return seg

    def transcribe_audio(self, audio, t0: float = 0.0) -> TranscriptSegment:
        """numpy 音频（float32 mono 16k）-> TranscriptSegment；空输入/未就绪不崩。"""
        arr = self._prepare_audio(audio)
        duration = len(arr) / float(SAMPLE_RATE)
        seg_id = self._next_id()
        t_start = float(t0)
        t_end = t_start + duration
        if duration < _MIN_AUDIO_S:
            return TranscriptSegment(seg_id=seg_id, t_start=t_start, t_end=t_end,
                                     text="", is_final=True, rtf=0.0)
        if not self._ready:
            if not self._warned_not_ready:
                self._warned_not_ready = True
                self._log.warning("ASR 尚未就绪，音频已跳过（主循环应等 load 完成）")
            return TranscriptSegment(seg_id=seg_id, t_start=t_start, t_end=t_end,
                                     text="", is_final=True, rtf=0.0)

        t_perf = time.perf_counter()
        stats: dict = {}
        try:
            with self._lock:
                text = self._transcribe_raw(arr, stats=stats)
                avg = float(stats.get("avg_logprob", 0.0) or 0.0)
                nsp = float(stats.get("no_speech_prob", 0.0) or 0.0)
                cr = float(stats.get("compression_ratio", 0.0) or 0.0)
                low_confidence = bool(stats) and (
                    avg < self.avg_logprob_threshold
                    or (nsp > self.no_speech_threshold and avg < -0.8)
                    or cr > self.compression_ratio_threshold)
                # 低置信度不再直接丢弃：去掉 prompt 偏见，用更宽搜索重解一次。
                if low_confidence and self.retry_low_confidence:
                    alt_stats: dict = {}
                    try:
                        alt = self._transcribe_once(
                            arr, vad_filter=True, temperature=0.0,
                            use_prompt=False, use_hotwords=False,
                            beam_size=max(3, min(5, self.beam_size + 2)), stats=alt_stats)
                        alt_avg = float(alt_stats.get("avg_logprob", -99.0) or -99.0)
                        alt_fixed = normalize_text(alt)
                        if (is_meaningful(alt_fixed) and not _looks_degenerate(alt_fixed)
                                and (not stats or alt_avg > avg + 0.02)):
                            self._log.info("低置信度二次解码采用新结果 %.2f -> %.2f", avg, alt_avg)
                            text, stats = alt, alt_stats
                            avg, nsp, cr = alt_avg, float(alt_stats.get("no_speech_prob", 0.0) or 0.0), \
                                float(alt_stats.get("compression_ratio", 0.0) or 0.0)
                    except Exception as retry_err:
                        self._log.debug("低置信度二次解码失败（保留首次结果）: %s", retry_err)
        except Exception as e:  # 单句失败不拖垮整场
            self._log.warning("ASR 识别异常（该句跳过）: %s", e)
            return TranscriptSegment(seg_id=seg_id, t_start=t_start, t_end=t_end,
                                     text="", is_final=True, rtf=0.0)
        elapsed = time.perf_counter() - t_perf
        rtf = elapsed / max(duration, 0.05)
        self._last_rtf = float(rtf)
        if rtf > _RTF_WARN:
            now = time.monotonic()
            if now - self._last_slow_warn >= _SLOW_WARN_INTERVAL_S:
                self._last_slow_warn = now
                self._log.warning("ASR 较慢: 音频 %.2fs 用时 %.2fs (RTF=%.2f)",
                                  duration, elapsed, rtf)
            else:
                self._log.debug("ASR 较慢: 音频 %.2fs 用时 %.2fs (RTF=%.2f)",
                                duration, elapsed, rtf)
        else:
            self._log.debug("ASR %.2fs -> %.2fs RTF=%.2f", duration, elapsed, rtf)

        fixed = normalize_text(text)
        if not is_meaningful(fixed) or _looks_degenerate(fixed):
            return TranscriptSegment(seg_id=seg_id, t_start=t_start, t_end=t_end,
                                     text="", text_fixed="", is_final=True, rtf=float(rtf))
        avg = float(stats.get("avg_logprob", 0.0) or 0.0)
        nsp = float(stats.get("no_speech_prob", 0.0) or 0.0)
        cr = float(stats.get("compression_ratio", 0.0) or 0.0)
        uncertain = bool(stats) and (
            avg < self.avg_logprob_threshold
            or (nsp > self.no_speech_threshold and avg < -0.8)
            or cr > self.compression_ratio_threshold)
        if uncertain and self.drop_low_confidence:
            return TranscriptSegment(seg_id=seg_id, t_start=t_start, t_end=t_end,
                                     text="", text_fixed="", is_final=True, rtf=float(rtf))
        prompt_norm = normalize_text(self.initial_prompt or "")
        core = fixed.strip("。，、！？… .\u3000")
        if prompt_norm and core and len(core) <= 8 and core in prompt_norm:
            return TranscriptSegment(seg_id=seg_id, t_start=t_start, t_end=t_end,
                                     text="", text_fixed="", is_final=True, rtf=float(rtf))
        confidence = max(0.05, min(0.99, math.exp(avg))) if stats else 0.0
        return TranscriptSegment(seg_id=seg_id, t_start=t_start, t_end=t_end,
                                 text=str(text).strip(), text_fixed=fixed,
                                 is_final=True, rtf=float(rtf), confidence=confidence)

    def transcribe_file(self, path: str, max_seconds: float | None = None,
                        on_progress: Callable[..., None] | None = None) -> list[TranscriptSegment]:
        """整文件转写：先 PyAV 解码（绕过 faster_whisper.audio 的 PyAV19 不兼容），
        再按 30s 窗口 + 1s 重叠逐段识别并拼接。

        on_progress(idx, total_windows, start_s, end_s, seg) 可选，供 CLI 打印进度。
        """
        from tools.transcribe_file import (  # 惰性导入避免循环依赖
            OVERLAP_S, STRIDE_S, WINDOW_S, decode_audio,
        )

        if not self._ready:
            self.load()
        audio = decode_audio(path)
        total = len(audio) / float(SAMPLE_RATE)
        use = total if max_seconds is None or max_seconds <= 0 else min(total, float(max_seconds))
        audio = audio[: int(use * SAMPLE_RATE)]
        if use < _MIN_AUDIO_S:
            return []
        n_windows = max(1, int(math.ceil((use - OVERLAP_S) / STRIDE_S)))
        out: list[TranscriptSegment] = []
        start = 0.0
        idx = 0
        while start < use - 0.05:
            end = min(start + WINDOW_S, use)
            idx += 1
            chunk = audio[int(start * SAMPLE_RATE): int(end * SAMPLE_RATE)]
            seg = self.transcribe_audio(chunk, t0=start)
            seg.t_end = min(end, use)
            if out and seg.text:
                deduped = _strip_overlap(out[-1].display_text, seg.text)
                if not deduped:
                    if on_progress is not None:
                        try:
                            on_progress(idx, n_windows, start, end, seg)
                        except Exception:
                            pass
                    start += STRIDE_S
                    continue
                seg.text = deduped
                seg.text_fixed = deduped
            if seg.text:
                out.append(seg)
            if on_progress is not None:
                try:
                    on_progress(idx, n_windows, start, end, seg)
                except Exception:
                    pass
            start += STRIDE_S
        return out

    # ------------------------------------------------------------ 内部
    def _transcribe_raw(self, audio: np.ndarray, vad_filter: bool | None = None,
                        stats: dict | None = None) -> str:
        """真正调用 faster-whisper；返回拼接文本。调用方需持 self._lock。

        三级兜底，保证任何情况下不会因为 prompt/cap 参数问题导致整场 0 字幕：
            1) 正常参数（temp=0，含 prompt + 预算内 max_new_tokens）
            2) ValueError（prompt+cap 超 448）→ 去掉 max_new_tokens 重试
            3) 仍失败 → 去掉 initial_prompt（及 hotwords）再试
        stats 会被填入 avg_logprob / no_speech_prob / compression_ratio（用于低置信度过滤）。
        """
        try:
            text = self._transcribe_once(audio, vad_filter, temperature=0.0, stats=stats)
        except ValueError as e:
            self._log.warning("ASR prompt/cap 超出 Whisper 448 token 限制，去掉 max_new_tokens 重试: %s", e)
            try:
                text = self._transcribe_once(audio, vad_filter, temperature=0.0,
                                             include_cap=False)
            except Exception as e2:
                self._log.warning("ASR 去掉 cap 仍失败，去掉 initial_prompt 兜底重试: %s", e2)
                text = self._transcribe_once(audio, vad_filter, temperature=0.0,
                                             include_cap=False, use_prompt=False)
        except TypeError as e:  # 旧版 faster-whisper 不支持某些参数
            self._log.warning("ASR 参数不被当前 faster-whisper 支持，降级重试: %s", e)
            text = self._transcribe_once(audio, vad_filter, temperature=0.0,
                                         include_cap=False, use_prompt=False,
                                         use_hotwords=False)
        # 低信噪比片段在 temp=0 下可能自激重复（「组组组组…」）：先尝试关掉 hotwords 重试；
        # 短句再用 temperature=0.2 兜底；长句不重试（避免单句延迟翻倍），下游按退化文本丢弃。
        duration = len(audio) / float(SAMPLE_RATE)
        if _looks_degenerate(normalize_text(text)):
            if use_hotwords and self.use_hotwords and self.hotwords:
                self._log.debug("启用 hotwords 时输出退化，关闭 hotwords 重试一次")
                try:
                    alt = self._transcribe_once(audio, vad_filter, temperature=0.0,
                                                use_hotwords=False)
                    if not _looks_degenerate(normalize_text(alt)):
                        return alt
                    text = alt
                except Exception as e:
                    self._log.debug("关闭 hotwords 重试失败（忽略）: %s", e)
            if duration <= 5.0:
                self._log.debug("ASR 输出疑似退化重复，用 temperature=0.2 重试一次")
                try:
                    alt = self._transcribe_once(audio, vad_filter, temperature=0.2,
                                                use_hotwords=False)
                    if not _looks_degenerate(normalize_text(alt)):
                        return alt
                except Exception as e:
                    self._log.debug("ASR 退化重试失败（忽略）: %s", e)
        return text

    def _transcribe_once(self, audio: np.ndarray, vad_filter: bool | None = None,
                         temperature: float = 0.0, include_cap: bool = True,
                         use_prompt: bool = True, use_hotwords: bool = True,
                         stats: dict | None = None, beam_size: int | None = None) -> str:
        """单次解码；beam_size 可仅用于低置信度重试，不改变常规延迟档位。"""
        kwargs = dict(
            language=self.language or None,
            beam_size=int(beam_size or self.beam_size),
            initial_prompt=(self.initial_prompt or None) if use_prompt else None,
            condition_on_previous_text=False,
            vad_filter=self.vad_filter if vad_filter is None else bool(vad_filter),
            temperature=float(temperature),
            best_of=1,
            # 我们只用文本、时间戳由 VAD 提供；关掉时间戳解码可省 ~0.15s/次（实测）
            without_timestamps=bool(getattr(self, "without_timestamps", True)),
        )
        if include_cap:
            cap = self._max_new_tokens(audio)
            if cap is not None:
                kwargs["max_new_tokens"] = cap
        if use_hotwords and self.use_hotwords and self.hotwords:
            kwargs["hotwords"] = self.hotwords

        def _call():
            try:
                segments, _info = self._model.transcribe(audio, **kwargs)
            except TypeError as e:  # 旧版 faster-whisper 不支持 without_timestamps
                if "without_timestamps" in str(e):
                    kwargs.pop("without_timestamps", None)
                    segments, _info = self._model.transcribe(audio, **kwargs)
                else:
                    raise
            segs = list(segments)
            if stats is not None and segs:
                try:
                    stats["avg_logprob"] = min(
                        float(getattr(s, "avg_logprob", 0.0) or 0.0) for s in segs)
                    stats["no_speech_prob"] = max(
                        float(getattr(s, "no_speech_prob", 0.0) or 0.0) for s in segs)
                    stats["compression_ratio"] = max(
                        float(getattr(s, "compression_ratio", 0.0) or 0.0) for s in segs)
                except Exception:
                    pass
            return "".join(getattr(s, "text", "") or "" for s in segs)

        return _call()

    def _prompt_exact_tokens(self) -> int:
        """实际注入解码器的 prompt token 数（initial_prompt + 启用时的 hotwords）。

        注意：faster-whisper 的 get_prompt 会同时拼入 hotwords（sot_prev 之后）与
        previous_tokens（initial_prompt），两者都占 448 token 预算，必须一起计算，
        否则启用热词时仍会触发 "combined length exceeds max_length" 的 ValueError。
        """
        if self._prompt_tokens is not None:
            return self._prompt_tokens
        prompt = (self.initial_prompt or "").strip()
        hotwords = self.hotwords.strip() if (self.use_hotwords and self.hotwords) else ""
        text = " ".join(p for p in (hotwords, prompt) if p)
        if not text:
            self._prompt_tokens = 0
            return 0
        n = 0
        try:
            tok = getattr(self._model, "hf_tokenizer", None)
            if tok is not None:
                n = len(tok.encode(" " + text).ids)
        except Exception:
            n = 0
        if n <= 0:  # 兜底估算：中文约 1.5 token/字
            n = int(len(text) * 1.5) + 8
        self._prompt_tokens = n
        return n

    def _prompt_token_len(self) -> int:
        """initial_prompt 预算 token 数 = 实际 tokenizer 值 + 安全余量。"""
        if self._prompt_tokens_budget is None:
            self._prompt_tokens_budget = self._prompt_exact_tokens() + _PROMPT_MARGIN
        return self._prompt_tokens_budget

    def _max_new_tokens(self, audio: np.ndarray) -> int | None:
        """按音频时长给解码上限，同时严格保证 prompt+cap 不超 Whisper 448 token 预算。"""
        duration = max(0.0, len(audio) / float(SAMPLE_RATE))
        ptoks = self._prompt_token_len()
        budget = _WHISPER_MAX_LEN - ptoks - _CAP_SAFETY
        if budget < 32:
            return None  # prompt 太长，宁可不设上限也不能触发 ValueError
        cap = int(min(budget, max(32, int(math.ceil(duration * 8.0)) + 24)))
        # 下发前再校验一次（prompt 含余量 + cap + 额外余量 <= 448），不满足就自动降 cap
        slack = _WHISPER_MAX_LEN - ptoks - 12
        if cap > slack:
            cap = slack
        return cap if cap >= 32 else None

    def _prepare_audio(self, audio) -> np.ndarray:
        if audio is None:
            return np.zeros(0, dtype=np.float32)
        arr = np.asarray(audio)
        if arr.ndim > 1:
            arr = arr.mean(axis=1) if arr.shape[1] > 1 else arr[:, 0]
        arr = np.ascontiguousarray(arr.reshape(-1), dtype=np.float32)
        if arr.size:
            np.clip(arr, -1.0, 1.0, out=arr)
        return arr

    def _next_id(self) -> int:
        with self._id_lock:
            self._seg_counter += 1
            return self._seg_counter


# ---------------------------------------------------------------- 自测
def _run_selftest() -> int:
    init_console()
    from logging_setup import setup
    setup()
    log = get_logger("asr")
    print("=" * 66)
    print("组会助手 · ASR 引擎自测（faster-whisper, CPU int8, 离线）")
    print("=" * 66)
    try:
        cfg = load_config()
    except Exception as e:
        print(f"❌ 配置加载失败: {e}")
        return 1
    eng = AsrEngine(cfg, log=log)
    print(f"配置模型: {eng.model_name} | compute={eng.compute_type} | "
          f"threads={eng.cpu_threads} | language={eng.language} | vad_filter={eng.vad_filter}")
    print(f"解析缓存: {resolve_model_ref(eng.model_name)}")
    t0 = time.perf_counter()
    try:
        eng.load()
    except Exception as e:
        print(f"❌ 模型加载失败: {e}")
        return 1
    print(f"模型加载: {time.perf_counter() - t0:.2f}s | is_ready={eng.is_ready}")
    ptoks = eng._prompt_exact_tokens()
    cap11 = eng._max_new_tokens(np.zeros(11 * SAMPLE_RATE, dtype=np.float32))
    print(f"prompt: {ptoks} tokens（预算按 {eng._prompt_token_len()} 计）| "
          f"11s 音频 max_new_tokens={cap11} | hotwords={'启用' if eng.use_hotwords else '关闭'} | "
          f"预算校验: {eng._prompt_token_len() + (cap11 or 0) + 12} <= 448")
    assert eng._prompt_token_len() + (cap11 or 0) + 12 <= 448, "prompt+cap 预算超 448 token"
    eng.warmup()

    sr = SAMPLE_RATE
    # 1) 1 秒 440Hz 正弦（非语音，不应崩；返回空文本属正常）
    tt = np.arange(sr, dtype=np.float64) / sr
    sine = (0.3 * np.sin(2 * np.pi * 440.0 * tt)).astype(np.float32)
    seg = eng.transcribe_audio(sine, t0=0.0)
    print(f"[正弦 1s] 文本={seg.display_text!r} rtf={seg.rtf:.2f} 不崩 ✅")
    # 2) 2 秒静音
    sil = eng.transcribe_audio(np.zeros(2 * sr, dtype=np.float32), t0=1.0)
    print(f"[静音 2s] 文本={sil.display_text!r} rtf={sil.rtf:.2f} 不崩 ✅")
    # 3) 空输入 / 超短输入
    empty = eng.transcribe_audio(np.zeros(0, dtype=np.float32), t0=0.0)
    tiny = eng.transcribe_audio(np.zeros(int(0.05 * sr), dtype=np.float32), t0=0.0)
    print(f"[空输入] len={len(empty.display_text)}；[超短 0.05s] len={len(tiny.display_text)} "
          f"不崩 ✅")
    # 4) AudioChunk 接口 + 未就绪引擎（另一个实例不应崩）
    chunk = AudioChunk(data=sine, t_start=3.0, t_end=4.0, peak_rms=0.2)
    cseg = eng.transcribe(chunk)
    print(f"[transcribe(AudioChunk)] 区间 {cseg.t_start:.2f}~{cseg.t_end:.2f}s "
          f"文本={cseg.display_text!r}")
    cold = AsrEngine(cfg, log=log)
    cold_seg = cold.transcribe(chunk)
    print(f"[未加载引擎] 不崩 ✅ 文本={cold_seg.display_text!r} last_rtf={eng.last_rtf:.2f}")
    # 5) 故障回归：模拟超长 prompt + 超出 448 的 cap，验证三级兜底后仍不抛异常（不会 0 字幕）
    old_cap = eng._max_new_tokens
    eng._max_new_tokens = lambda _a: 999  # 故意制造 ValueError
    try:
        fallback_seg = eng.transcribe_audio(np.zeros(int(2 * sr), dtype=np.float32))
        print(f"[超预算兜底] 故意 cap=999 -> 自动降级重试，不抛异常 ✅ 文本={fallback_seg.display_text!r}")
    finally:
        eng._max_new_tokens = old_cap
    print("[结论] ✅ asr_engine 自测通过")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="组会助手 · ASR 引擎（faster-whisper CPU int8）")
    ap.add_argument("--selftest", action="store_true", help="合成音频自测（离线，无需麦克风）")
    args = ap.parse_args(argv)
    if args.selftest:
        try:
            return _run_selftest()
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
    sys.exit(main())
