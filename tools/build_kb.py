# -*- coding: utf-8 -*-
"""从用户自己的材料构建可选的本地 BM25 记忆索引。

默认只读取项目内 ``data/materials/``，支持 md / txt / docx / pptx / pdf。
也可在 config.yaml 的 ``kb.materials`` 中显式声明材料；相对路径以项目根为基准。
本工具不会修改配置，不会读取项目外的默认目录，也不会上传材料。

用法：
    python tools/build_kb.py --dry-run   # 只检查材料与文本抽取
    python tools/build_kb.py             # 写入 data/kb/index.json
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

SUPPORTED_SUFFIXES = {".md", ".markdown", ".txt", ".docx", ".pptx", ".pdf"}
DEMO_QUERIES = ["实验对照怎么设计", "项目难点和解决方案", "如何评价方法效果"]


def _clean_text(text: str) -> str:
    """去除内嵌 base64 图片和多余空白，保留原始段落结构。"""
    if not text:
        return ""
    text = str(text).replace("\ufeff", "").replace("\u200b", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"data:image/[a-zA-Z]+;base64,[A-Za-z0-9+/=\s]+", "[图片]", text)
    text = re.sub(r"[ \t\u3000]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract_text(path: Path, kind: str = "") -> str:
    """按扩展名抽取纯文本；不支持的格式返回空字符串。"""
    kind = (kind or path.suffix.lstrip(".")).lower()
    if kind in ("md", "markdown", "txt", "text"):
        return _clean_text(path.read_text(encoding="utf-8", errors="replace"))
    if kind == "docx":
        return _clean_text(_extract_docx(path))
    if kind == "pptx":
        return _clean_text(_extract_pptx(path))
    if kind == "pdf":
        return _clean_text(_extract_pdf(path))
    return ""


def _extract_docx(path: Path) -> str:
    try:
        import docx
        doc = docx.Document(str(path))
        parts = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                line = " | ".join(c.text.strip() for c in row.cells if c.text.strip())
                if line:
                    parts.append(line)
        return "\n".join(parts)
    except ImportError:
        print(f"  [跳过] {path.name}：DOCX 支持需要 python-docx")
    except Exception as exc:
        print(f"  [跳过] {path.name}：DOCX 抽取失败：{exc}")
    return ""


def _extract_pptx(path: Path) -> str:
    try:
        from pptx import Presentation
        prs = Presentation(str(path))
        parts: list[str] = []
        for number, slide in enumerate(prs.slides, 1):
            slide_parts: list[str] = []
            for shape in slide.shapes:
                if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip():
                    slide_parts.append(shape.text_frame.text.strip())
                if getattr(shape, "has_table", False):
                    for row in shape.table.rows:
                        line = " | ".join(c.text.strip() for c in row.cells if c.text.strip())
                        if line:
                            slide_parts.append(line)
            if slide_parts:
                parts.append(f"【第{number}页】\n" + "\n".join(slide_parts))
        return "\n\n".join(parts)
    except ImportError:
        print(f"  [跳过] {path.name}：PPTX 支持需要 python-pptx")
    except Exception as exc:
        print(f"  [跳过] {path.name}：PPTX 抽取失败：{exc}")
    return ""


def _extract_pdf(path: Path) -> str:
    try:
        import pypdf
        reader = pypdf.PdfReader(str(path))
        parts = []
        for number, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            if text.strip():
                parts.append(f"【第{number}页】\n{text.strip()}")
        return "\n\n".join(parts)
    except ImportError:
        print(f"  [跳过] {path.name}：PDF 支持需要 pypdf")
    except Exception as exc:
        print(f"  [跳过] {path.name}：PDF 抽取失败：{exc}")
    return ""


def resolve_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_DIR / path


def _configured_specs(cfg: dict | None) -> list[dict]:
    """读取 kb.materials；每项支持 title/path/weight，拒绝目录。"""
    specs: list[dict] = []
    for index, item in enumerate((cfg or {}).get("kb", {}).get("materials", []) or [], 1):
        if isinstance(item, str):
            item = {"path": item}
        if not isinstance(item, dict) or not item.get("path"):
            print(f"  [跳过] kb.materials 第 {index} 项缺少 path")
            continue
        path = resolve_path(str(item["path"]))
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            print(f"  [跳过] {path.name}：不支持的文件格式")
            continue
        specs.append({
            "title": str(item.get("title") or path.stem),
            "path": str(path),
            "weight": float(item.get("weight", 1.0) or 1.0),
            "origin": "config.yaml: kb.materials",
        })
    return specs


def collect_materials(cfg: dict | None = None) -> tuple[list[dict], list[dict]]:
    """返回 (成功抽取的材料, 缺失材料)，不写入索引。"""
    mat_dir = PROJECT_DIR / "data" / "materials"
    paths = sorted(p for p in mat_dir.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES) \
        if mat_dir.is_dir() else []
    specs = [{
        "title": path.stem, "path": str(path), "weight": 1.0,
        "origin": "data/materials",
    } for path in paths]
    specs.extend(_configured_specs(cfg))

    materials: list[dict] = []
    missing: list[dict] = []
    seen: set[str] = set()
    print("\n材料抽取：")
    for spec in specs:
        path = resolve_path(spec["path"])
        key = str(path.resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        if not path.is_file():
            missing.append(spec)
            print(f"  [缺失] {spec['title']}：{path}")
            continue
        try:
            text = extract_text(path)
        except Exception as exc:
            missing.append(spec)
            print(f"  [错误] {spec['title']}：{exc}")
            continue
        if not text:
            print(f"  [空] {spec['title']}：没有可索引文本")
            continue
        try:
            source = str(path.relative_to(PROJECT_DIR))
        except ValueError:
            source = str(path)
        materials.append({**spec, "source": source, "text": text})
        print(f"  [完成] {spec['title']}：{len(text)} 字，weight={spec['weight']}")
    return materials, missing


def main(argv: list[str] | None = None) -> int:
    from config import init_console, load_config

    argv = list(sys.argv[1:] if argv is None else argv)
    dry_run = "--dry-run" in argv
    init_console()
    cfg = load_config()
    started = time.time()
    print("=" * 72)
    print("Meeting Copilot · 可选历史记忆索引")
    print("=" * 72)
    print(f"材料目录：{PROJECT_DIR / 'data' / 'materials'}")
    print("原则：材料只在本机检索；命中时作为可选记忆，不限制模型回答范围。")

    materials, missing = collect_materials(cfg)
    if not materials:
        print("\n未找到可索引材料。把文件放入 data/materials/ 后再运行；不使用知识库也可正常提问。")
        return 0 if dry_run else 1
    print(f"\n抽取完成：{len(materials)} 篇 / {sum(len(m['text']) for m in materials)} 字 / "
          f"{time.time() - started:.1f}s")
    if dry_run:
        print("dry-run：未写入索引。")
        return 0

    from knowledge_base import CHUNK_OVERLAP, CHUNK_SIZE, KnowledgeBase, build_index

    out_dir = PROJECT_DIR / "data" / "kb"
    stats = build_index(materials, out_dir)
    print(f"\n索引：{stats['docs']} 篇 / {stats['chunks']} 块 -> {stats['index_path']}")
    index_path = Path(stats["index_path"])
    try:
        per_doc = json.loads(index_path.read_text(encoding="utf-8")).get("docs") or []
    except Exception as exc:
        per_doc = []
        print(f"[警告] 无法读回索引目录：{exc}")
    manifest = {
        "built_at": stats["built_at"], "chunk_size": CHUNK_SIZE,
        "chunk_overlap": CHUNK_OVERLAP, "stats": stats,
        "materials": per_doc,
        "missing": [{"title": m["title"], "path": m["path"]} for m in missing],
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    kb = KnowledgeBase(cfg)
    print("\n召回示例：")
    for query in DEMO_QUERIES:
        hits = kb.retrieve(query, top_k=2)
        print(f"  {query} -> {[hit['title'] for hit in hits] or '无命中（不影响自由提问）'}")
    print(f"\n完成，用时 {time.time() - started:.1f}s。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
