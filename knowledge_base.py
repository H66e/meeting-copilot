# -*- coding: utf-8 -*-
"""知识库：jieba 分词 + 自制 BM25 检索 + 滑窗切块 + index.json 落盘。

对外契约（见 INTERFACES.md 第 4 节 B3）::

    kb = KnowledgeBase(cfg)          # 构造即尝试 load()，空库不抛
    ok = kb.load()                   # 读 <data>/kb/index.json，不存在返回 False
    hits = kb.retrieve("掩码收缩扩张收益", top_k=4)
    # -> [{"title": str, "text": str, "score": float}, ...]
    kb.stats()                       # {"chunks": n, "docs": n, "built_at": str}

    stats = build_index(materials, out_dir)
    # materials: [{"title": str, "text": str, "source": str, "weight": float(可选)}]

实现说明：
- 不引入 rank_bm25，BM25（k1=1.5, b=0.75）自己实现；索引里存倒排表 postings，
  加载后即可检索，不需要重新分词。
- 切块 500 字、重叠 80 字，尽量在段落/句号处断开，避免把一句话切两半。
- 材料可带 weight（默认 1.0）：data/materials/ 下 Lead 现场生成的速览/老师建议
  用 1.5 左右权重，检索排序时优先。
- 空库、索引损坏、分词为空等所有异常路径都只记日志，不向上抛。
"""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from config import get
from logging_setup import get_logger

# ------------------------------------------------------------------ 常量
CHUNK_SIZE = 500          # 每块目标字符数
CHUNK_OVERLAP = 80        # 相邻块重叠字符数
BM25_K1 = 1.5
BM25_B = 0.75
INDEX_FILENAME = "index.json"
INDEX_VERSION = 1

# 中文/英文常见停用词（只影响排序，不影响能否召回）
_STOPWORDS = set("""
的 了 和 与 及 或 是 在 有 我 你 他 她 它 们 这 那 个 些 就 都 也 还 又 而 但 被 把 让 给 对 从 到 为 以 于 之 其 中 上 下 里 外 前 后 时 会 能 要 不 没 很 更 最 再 才 已 将 可 该 等 各 每 些 么 呢 吗 吧 啊 呀 哦 嗯 哈 得 地 着 过 来 去 说 做 看 想 知道 就是 所以 因为 如果 但是 然后 现在 一个 一下 一些 这种 那种 我们 你们 他们 可以 需要 应该 已经 还是 或者 以及 关于 通过 进行 对于 由于 其中 方面 问题 情况 时候 目前 主要 比较 非常 特别 更加 这样 那样 什么 怎么 为什么 这个 那个 这些 那些
the a an of to in on for with and or is are was were be been being this that these those it its as at by from we you they i he she not no yes can will would should could may might do does did done have has had there their our your his her them us me my
""".split())

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_ALNUM_RE = re.compile(r"[a-z0-9]")

# 通用技术术语：改善中英文混合材料的 BM25 分词，不包含具体项目或个人信息。
_DOMAIN_TERMS = (
    "对照实验", "消融实验", "计算预算", "双盲评估", "人工评估", "物理一致性",
    "视频生成", "扩散模型", "机器学习", "深度学习", "强化学习", "多模态",
    "baseline", "ablation", "iou", "clip", "transformer", "diffusion",
    "instruction tuning", "verifier", "router", "reward hacking",
)

_NUM_CN = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_NUM_MEASURE_RE = re.compile(r"^([一二两三四五六七八九十])(个|项|条|张|次|件|类|页|种|轮|名|位)")

_jieba_mod = None


def _jieba():
    """惰性加载 jieba（import 本模块时不加载词典，避免任何导入期开销）。"""
    global _jieba_mod
    if _jieba_mod is None:
        import jieba  # noqa: PLC0415  (惰性导入是有意的)

        try:
            jieba.setLogLevel(60)  # 关掉 "Building prefix dict" 等 INFO 日志
        except Exception:
            pass
        for _w in _DOMAIN_TERMS:
            try:
                jieba.add_word(_w, freq=2000)
            except Exception:
                pass
        _jieba_mod = jieba
    return _jieba_mod


# ------------------------------------------------------------------ 分词 / 切块
def tokenize(text: str) -> list[str]:
    """中文用 jieba 精确模式，英文/数字整词小写；过滤停用词与单字噪声。

    额外归一化（检索命中很关键）：
    - 希腊字母 κ -> kappa（"人评 κ 多少" 能命中材料里的 κ）；
    - 中文数量词 -> 阿拉伯数字（"六个动作" 也能命中材料里的 "6 个修复动作"）。
    """
    if not text:
        return []
    text = _clean(text)
    if not text:
        return []
    text = text.replace("κ", " kappa ").replace("Κ", " kappa ")
    raw = list(_jieba().cut_for_search(text.lower()))
    toks: list[str] = []
    for t in raw:
        t = t.strip().lower()
        if not t or t in _STOPWORDS:
            continue
        has_cjk = bool(_CJK_RE.search(t))
        has_alnum = bool(_ALNUM_RE.search(t))
        if not (has_cjk or has_alnum):
            continue
        if t.isdigit():          # 纯数字（年份等）保留，但权重意义有限
            toks.append(t)
        elif len(t) == 1 and has_cjk:   # 单个汉字噪声太大，丢弃
            continue
        else:
            toks.append(t)
        # 中文数量词归一：六个 -> 6，五件 -> 5，十项 -> 10
        m = _NUM_MEASURE_RE.match(t)
        if m:
            toks.append(str(_NUM_CN[m.group(1)]))
    if not toks:  # 全是单字（如 "掩 码"）时退一步保留单字，别返回空
        toks = [t for t in (x.strip().lower() for x in raw)
                if t and t not in _STOPWORDS and (_CJK_RE.search(t) or _ALNUM_RE.search(t))]
    return toks


def _clean(text: str) -> str:
    if not text:
        return ""
    text = str(text).replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\u3000]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """按 ~size 字滑窗切块，重叠 overlap 字；优先在段落/句号处断开。"""
    text = _clean(text)
    if not text:
        return []
    if len(text) <= size:
        return [text]
    # 断点候选（按优先级）
    breaks = ("\n\n", "\n", "。", "；", "！", "？", "，", "；")
    chunks: list[str] = []
    start = 0
    n = len(text)
    guard = 0
    while start < n and guard < 100000:
        guard += 1
        end = min(start + size, n)
        if end < n:
            # 在 [start + 0.55*size, end] 范围内找最后一个自然断点
            lo = start + int(size * 0.55)
            best = -1
            for bp in breaks:
                idx = text.rfind(bp, lo, end)
                if idx > best:
                    best = idx + len(bp)
            if best > start + int(size * 0.3):
                end = best
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= n:
            break
        nxt = max(end - overlap, start + max(1, size // 4))
        start = nxt
    return chunks


# ------------------------------------------------------------------ 索引构建
def build_index(materials: Iterable[dict], out_dir: str | os.PathLike,
                *, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP,
                log=None) -> dict:
    """materials -> 切块 + BM25 索引，落盘 <out_dir>/index.json，返回统计 dict。"""
    log = log or get_logger("kb")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    docs: list[dict] = []
    chunks: list[dict] = []
    postings: dict[str, list[list[int]]] = defaultdict(list)
    df: Counter = Counter()
    total_chars = 0

    for m in materials:
        title = str(m.get("title") or "未命名").strip()
        text = _clean(str(m.get("text") or ""))
        source = str(m.get("source") or "")
        weight = float(m.get("weight", 1.0) or 1.0)
        if not text:
            log.warning("知识库：材料《%s》正文为空，跳过（source=%s）", title, source)
            continue
        doc_id = len(docs)
        pieces = chunk_text(text, chunk_size, overlap)
        docs.append({"doc_id": doc_id, "title": title, "source": source,
                     "weight": weight, "chars": len(text), "chunks": len(pieces)})
        total_chars += len(text)
        for piece in pieces:
            cid = len(chunks)
            toks = tokenize(piece)
            tf = Counter(toks)
            dl = len(toks)
            chunks.append({"id": cid, "doc_id": doc_id, "title": title,
                           "source": source, "weight": weight,
                           "text": piece, "dl": dl})
            for term, cnt in tf.items():
                postings[term].append([cid, cnt])
            for term in tf:
                df[term] += 1

    n_chunks = len(chunks)
    avgdl = (sum(c["dl"] for c in chunks) / n_chunks) if n_chunks else 0.0
    idf = {term: round(math.log(1.0 + (n_chunks - d + 0.5) / (d + 0.5)), 4)
           for term, d in df.items()}

    built_at = time.strftime("%Y-%m-%d %H:%M:%S")
    index = {
        "version": INDEX_VERSION,
        "built_at": built_at,
        "chunk_size": chunk_size,
        "overlap": overlap,
        "n_chunks": n_chunks,
        "n_docs": len(docs),
        "avgdl": round(avgdl, 3),
        "docs": docs,
        "chunks": chunks,
        "df": dict(df),
        "idf": idf,
        # 倒排：[chunk_id, tf] 列表；JSON 里存 list 即可
        "postings": {t: v for t, v in postings.items()},
    }

    path = out / INDEX_FILENAME
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)

    stats = {
        "docs": len(docs),
        "chunks": n_chunks,
        "chars": total_chars,
        "built_at": built_at,
        "index_path": str(path),
        "size_bytes": path.stat().st_size,
        "dir": str(out),
    }
    log.info("知识库索引已生成：%d 篇材料 / %d 块 / %d 字符 -> %s",
             stats["docs"], stats["chunks"], stats["chars"], path)
    return stats


# ------------------------------------------------------------------ 检索
class KnowledgeBase:
    """BM25 知识库。空库/损坏索引都安全降级为"无命中"。"""

    def __init__(self, cfg: dict, log=None):
        self.cfg = cfg or {}
        self.log = log or get_logger("kb")
        self.dir = self._resolve_dir(self.cfg)
        self._index: dict | None = None
        self._chunks: list[dict] = []
        self._idf: dict[str, float] = {}
        self._postings: dict[str, list[tuple[int, int]]] = {}
        self._avgdl = 0.0
        self._k1 = BM25_K1
        self._b = BM25_B
        self.loaded = False
        # 构造即尝试加载：main 里 `kb = KnowledgeBase(cfg)` 之后无需再关心 load
        try:
            self.load()
        except Exception:  # load 内部已兜底，这里再兜一层，导入/构造绝不抛
            self.log.warning("知识库初始化异常，按空库运行")

    # ---- 路径
    def _resolve_dir(self, cfg: dict) -> Path:
        rel = get(cfg, "kb.dir", "") or ""
        project = cfg.get("_project_dir") or ""
        if rel and Path(rel).is_absolute():
            return Path(rel)
        if rel and project:
            return Path(project) / rel
        data_dir = (cfg.get("_dirs") or {}).get("data") or ""
        if data_dir:
            return Path(data_dir) / "kb"
        return Path(__file__).resolve().parent / "data" / "kb"

    # ---- 加载
    def load(self) -> bool:
        path = self.dir / INDEX_FILENAME
        if not path.exists():
            self.loaded = False
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                idx = json.load(f)
            if not isinstance(idx, dict) or "chunks" not in idx:
                raise ValueError("index.json 结构不正确")
            chunks = idx.get("chunks") or []
            self._chunks = chunks
            self._idf = {str(k): float(v) for k, v in (idx.get("idf") or {}).items()}
            raw_post = idx.get("postings") or {}
            self._postings = {
                str(term): [(int(p[0]), int(p[1])) for p in plist]
                for term, plist in raw_post.items() if plist
            }
            self._avgdl = float(idx.get("avgdl") or 0.0)
            if self._avgdl <= 0 and chunks:
                self._avgdl = sum(float(c.get("dl") or 0) for c in chunks) / len(chunks)
            self._index = idx
            self.loaded = True
            return True
        except Exception as e:
            self.loaded = False
            self._index = None
            self._chunks = []
            self._postings = {}
            self.log.warning("知识库索引读取失败（按空库运行）：%s", e)
            return False

    def is_available(self) -> bool:
        return self.loaded and bool(self._chunks)

    # ---- 检索
    def retrieve(self, query: str, top_k: int | None = None) -> list[dict]:
        """返回 [{"title","text","score"}]（BM25 分数，降序）；空库/空查询返回 []。"""
        if not self._chunks:
            return []
        query = (query or "").strip()
        if not query:
            return []
        k = int(top_k if top_k is not None else get(self.cfg, "kb.top_k", 4) or 4)
        k = max(1, min(k, 20))
        if k <= 0:
            return []

        q_tokens = tokenize(query)
        qtf = Counter(q_tokens)
        scores: dict[int, float] = defaultdict(float)
        if qtf and self._avgdl > 0:
            for term, qn in qtf.items():
                idf = self._idf.get(term)
                plist = self._postings.get(term)
                if idf is None or not plist:
                    continue
                q_weight = 1.0 + 0.3 * (qn - 1)   # 词频加成，但避免长 query 压垮短句
                for cid, tf in plist:
                    if cid < 0 or cid >= len(self._chunks):
                        continue
                    dl = float(self._chunks[cid].get("dl") or 0)
                    denom = tf + self._k1 * (1.0 - self._b + self._b * dl / self._avgdl)
                    scores[cid] += idf * (tf * (self._k1 + 1.0) / denom) * q_weight

        # 兜底：BM25 一个都没命中（分词不匹配/极短查询）时做字面子串扫描
        if not scores:
            scores = self._substring_scores(query)
        if not scores:
            return []

        ranked = sorted(
            scores.items(),
            key=lambda kv: kv[1] * float(self._chunks[kv[0]].get("weight", 1.0) or 1.0),
            reverse=True,
        )
        hits: list[dict] = []
        for cid, raw in ranked[:k]:
            ch = self._chunks[cid]
            weighted = raw * float(ch.get("weight", 1.0) or 1.0)
            hits.append({
                "title": str(ch.get("title") or ""),
                "text": str(ch.get("text") or ""),
                "score": round(weighted, 4),
            })
        return hits

    def _substring_scores(self, query: str) -> dict[int, float]:
        """查询词在原文里字面出现的兜底打分。"""
        probe = _clean(query)
        pieces = [p for p in re.split(r"[\s,，。;；、/]+", probe) if len(p) >= 2][:6]
        scores: dict[int, float] = {}
        for cid, ch in enumerate(self._chunks):
            text = str(ch.get("text") or "")
            hit = 0.0
            for p in pieces:
                if p and p in text:
                    hit += 1.0
            if hit:
                scores[cid] = hit
        return scores

    # ---- 统计
    def stats(self) -> dict:
        if not self._chunks:
            return {"chunks": 0, "docs": 0, "built_at": "",
                    "dir": str(self.dir), "index_path": str(self.dir / INDEX_FILENAME)}
        idx = self._index or {}
        return {
            "chunks": len(self._chunks),
            "docs": int(idx.get("n_docs") or len({c.get("doc_id") for c in self._chunks})),
            "built_at": str(idx.get("built_at") or ""),
            "dir": str(self.dir),
            "index_path": str(self.dir / INDEX_FILENAME),
        }

    # 便于 prompt 拼装：把检索结果拼成一段紧凑中文文本
    def context_text(self, query: str, top_k: int | None = None,
                     max_chars: int | None = None) -> str:
        hits = self.retrieve(query, top_k=top_k)
        if not hits:
            return ""
        limit = int(max_chars if max_chars is not None else get(self.cfg, "kb.max_chars", 3200) or 3200)
        parts: list[str] = []
        used = 0
        for h in hits:
            piece = f"《{h['title']}》{h['text']}"
            if used + len(piece) > limit and parts:
                break
            parts.append(piece[: max(0, limit - used)])
            used += len(piece)
        return "\n---\n".join(parts)[:limit]


# ------------------------------------------------------------------ 自测
def _selftest() -> int:
    from config import init_console, load_config

    init_console()
    log = get_logger("kb")
    cfg = load_config()
    print("=" * 64)
    print("knowledge_base.py --selftest")
    print("=" * 64)

    fake = [
        {"title": "掩码收缩扩张收益实验", "source": "fake/掩码.md", "text": (
            "掩码收缩扩张精修的收益：对粗糙掩码先做收缩再扩张（shrink-expand），"
            "可以去掉边缘的背景污染，同时保留目标主体细节。在 RefCOCO 指代式图像编辑"
            "任务上，等预算设置下收缩扩张两阶段的平均 IoU 提升明显，并且对冻结编辑器"
            "（FLUX / SDXL inpainting）都稳定有效。代价是每次编辑多一次前向，"
            "需要做等预算对比来排除算力差异。人工双盲评估显示边缘贴合度更好。")},
        {"title": "Predict-before-Edit 核心思想", "source": "fake/pbe.md", "text": (
            "Predict-before-Edit 的核心是先把要改的区域和预期结果预测出来，再交给冻结的"
            "编辑器执行。用反事实动作表（counterfactual action table）构造候选动作，"
            "预测器评分选最优动作后再编辑，从而避免盲目试错。实验上需要和随机动作、"
            "启发式动作两个 baseline 对比，并做消融验证预测器的必要性。")},
        {"title": "物理一致性评估", "source": "fake/physics.md", "text": (
            "物理一致性评估关注物体轨迹是否平滑、碰撞与重力是否合理、长时间生成是否累积漂移。"
            "评估可以结合人工双盲与轨迹追踪指标，轨迹偏移和漂移是常见失败模式。")},
    ]

    tmp = tempfile.mkdtemp(prefix="mckb_")
    try:
        stats = build_index(fake, tmp)
        print(f"[1] 建索引：docs={stats['docs']} chunks={stats['chunks']} "
              f"chars={stats['chars']} size={stats['size_bytes']}B")
        assert stats["docs"] == 3, "docs 数量不对"
        assert stats["chunks"] >= 3, "chunks 数量不对"
        assert Path(stats["index_path"]).exists(), "index.json 未落盘"

        cfg2 = dict(cfg)
        cfg2["kb"] = dict(cfg.get("kb") or {})
        cfg2["kb"]["dir"] = tmp
        kb = KnowledgeBase(cfg2, log=log)
        print(f"[2] 加载索引：loaded={kb.loaded} stats={kb.stats()}")
        assert kb.loaded and kb.stats()["chunks"] == stats["chunks"]

        cases = [
            ("掩码收缩扩张收益", "掩码收缩扩张收益实验"),
            ("等预算对比怎么做", "掩码收缩扩张收益实验"),
            ("反事实动作表和冻结编辑器", "Predict-before-Edit 核心思想"),
            ("物理一致性轨迹漂移", "物理一致性评估"),
            ("RefCOCO 指代式图像编辑", "掩码收缩扩张收益实验"),
        ]
        ok_all = True
        for q, expect_title in cases:
            hits = kb.retrieve(q, top_k=3)
            titles = [h["title"] for h in hits]
            ok = bool(hits) and hits[0]["title"] == expect_title
            ok_all = ok_all and ok
            print(f"[3] query={q!r} -> top1={titles[0] if titles else None!r} "
                  f"score={hits[0]['score'] if hits else 0} {'OK' if ok else 'FAIL'}")
            assert ok, f"检索召回失败：{q}"
        assert ok_all

        # 空查询 / 未命中 / 空库
        assert kb.retrieve("", top_k=3) == [], "空查询应返回 []"
        assert isinstance(kb.retrieve("完全不存在的词语xyzzy"), list), "未命中不应抛异常"
        empty_dir = Path(tmp) / "empty"
        empty_dir.mkdir(exist_ok=True)
        cfg3 = dict(cfg)
        cfg3["kb"] = dict(cfg.get("kb") or {})
        cfg3["kb"]["dir"] = str(empty_dir)
        kb_empty = KnowledgeBase(cfg3, log=log)
        print(f"[4] 空库：loaded={kb_empty.loaded} retrieve={kb_empty.retrieve('掩码')} "
              f"stats={kb_empty.stats()}")
        assert kb_empty.retrieve("掩码") == []
        assert kb_empty.stats()["chunks"] == 0

        # 长文切块 + 重叠连续性
        long_text = "".join(f"第{i}段：掩码收缩扩张精修与等预算对比。" for i in range(120))
        pieces = chunk_text(long_text)
        print(f"[5] 长文切块：{len(long_text)} 字 -> {len(pieces)} 块，"
              f"首块 {len(pieces[0])} 字，末块 {len(pieces[-1])} 字")
        assert len(pieces) >= 5, "长文切块数量不对"
        assert all(len(p) <= CHUNK_SIZE + 20 for p in pieces), "切块超长"
        assert pieces[0][-CHUNK_OVERLAP // 2:] in pieces[1], "重叠不足"

        # 关键词覆盖检查（Lead 指定的真实术语）
        terms = ["掩码收缩", "扩张", "精修", "重定位", "等预算", "反事实动作表",
                 "RefCOCO", "冻结编辑器", "人工双盲评估", "物理一致性", "轨迹", "视频生成"]
        docs_text = " ".join(m["text"] for m in fake)
        toks = set(tokenize(docs_text))
        missing = [t for t in terms if t not in toks and t.lower() not in toks]
        print(f"[6] 术语分词覆盖：{len(terms) - len(missing)}/{len(terms)}，"
              f"缺={missing if missing else '无'}")

        print("\nSELFTEST OK ✅  （BM25 检索、切块、空库降级全部通过）")
        return 0
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("用法：python knowledge_base.py --selftest")
