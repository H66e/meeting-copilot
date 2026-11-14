# -*- coding: utf-8 -*-
"""离线测试：路由 / 去重 / SSE 解析 / BM25 / 空库 / 视觉降级 / 空内容兜底。

不依赖网络（httpx 用 MockTransport、LLM 用假客户端），不依赖麦克风/桌面内容。
运行（项目根）：

    python tests/test_brain_vision.py
"""
from __future__ import annotations

import base64
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from PIL import Image  # noqa: E402

from brain import (DEEP_KINDS, FAST_MIN_MAX_TOKENS, Brain, LLMClient, LLMError,  # noqa: E402
                   extract_text, iter_sse)
from config import init_console, load_config  # noqa: E402
from contracts import (KIND_ANSWER, KIND_FREE_QA, KIND_HINT, KIND_MINUTES,  # noqa: E402
                       KIND_SCRIPT, KIND_SLIDE, KIND_SUMMARY, KIND_TERM, LANE_DEEP,
                       LANE_FAST, BrainEvent, BrainRequest, EventBus)
from knowledge_base import (CHUNK_OVERLAP, KnowledgeBase, build_index,  # noqa: E402
                            chunk_text, tokenize)
from vision_reader import VisionReader  # noqa: E402

_RESULTS: list[tuple[str, bool, str]] = []
_TESTS: list[tuple[str, object]] = []


def case(name: str):
    """装饰器：登记测试用例（不立即执行），由 main() 统一运行。"""
    def _wrap(fn):
        _TESTS.append((name, fn))
        return fn
    return _wrap


def _run_one(name: str, fn) -> None:
    try:
        fn()
        _RESULTS.append((name, True, ""))
        print(f"  ✅ {name}")
    except AssertionError as e:
        _RESULTS.append((name, False, str(e)))
        print(f"  ❌ {name}: {e}")
    except Exception as e:  # noqa: BLE001
        _RESULTS.append((name, False, f"{type(e).__name__}: {e}"))
        print(f"  ❌ {name}: {type(e).__name__}: {e}")


def _drain(bus: EventBus) -> list[BrainEvent]:
    import queue as _q
    out = []
    while True:
        try:
            out.append(bus.brain_events.get_nowait())
        except _q.Empty:
            return out


def _fake_provider_cfg() -> dict:
    return {
        "_providers": {
            "fake": {"type": "openai", "base_url": "https://fake.local/v1",
                     "api_key": "sk-test-SECRET-123456", "api_key_env": "FAKE_API_KEY"},
        },
        "llm": {"request_timeout_s": 20},
        "vision": {"capture": "primary", "max_width": 1280,
                   "model": "claude-opus-5-5",
                   "fallback_models": ["claude-sonnet-5", "claude-opus-4-8"]},
        "kb": {"top_k": 2},
        "brain": {"max_context_chars": 3500},
    }


def _mk_mock_client(handler) -> LLMClient:
    c = LLMClient(_fake_provider_cfg())
    c._http = httpx.Client(transport=httpx.MockTransport(handler))
    return c


# ================================================================== SSE 解析
@case("1. SSE 解析：正文/思维链分离、[DONE] 截断、坏行跳过")
def t_sse():
    lines = [
        'data: {"choices":[{"delta":{"content":"你好"}}]}',
        ": 这是心跳注释",
        "event: message",
        'data: {"choices":[{"delta":{"content":null,"reasoning_content":"我在思考"}}]}',
        'data: {"choices":[{"delta":{"content":"世界"}}],"usage":{"total_tokens":9}}',
        "data: 这不是JSON",
        "data: [DONE]",
        'data: {"choices":[{"delta":{"content":"DONE之后不应出现"}}]}',
    ]
    evs = list(iter_sse(lines))
    content = "".join(e.get("content", "") for e in evs)
    reasoning = "".join(e.get("reasoning", "") for e in evs)
    assert content == "你好世界", f"正文拼接错误：{content!r}"
    assert reasoning == "我在思考", f"思维链未被单独解析：{reasoning!r}"
    assert any(e.get("done") for e in evs), "未识别 [DONE]"
    assert any(e.get("usage", {}).get("total_tokens") == 9 for e in evs), "usage 未解析"
    assert "DONE之后" not in content, "[DONE] 后不应继续解析"


@case("2. 非流式响应文本提取：str / content parts / 空正文 / reasoning")
def t_extract():
    a = {"choices": [{"message": {"content": "普通文本"}}], "usage": {"total_tokens": 3}}
    t, r, u = extract_text(a)
    assert (t, r, u.get("total_tokens")) == ("普通文本", "", 3)
    b = {"choices": [{"message": {"content": [{"type": "text", "text": "分段"},
                                              {"type": "text", "text": "拼接"}]}}]}
    t, _, _ = extract_text(b)
    assert t == "分段拼接", f"content parts 提取错误：{t!r}"
    c = {"choices": [{"message": {"content": None, "reasoning_content": "只有思维链"}}]}
    t, r, _ = extract_text(c)
    assert t == "" and r == "只有思维链"
    t, r, u = extract_text({})
    assert (t, r, u) == ("", "", {})


# ================================================================== LLMClient
@case("3. LLMClient 非流式：正常返回 + usage 记录")
def t_client_once():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["stream"] is False and body["model"] == "m1"
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "收到"}}],
            "usage": {"total_tokens": 5}})

    c = _mk_mock_client(handler)
    text = c.chat("fake", "m1", [{"role": "user", "content": "hi"}], stream=False)
    assert text == "收到", f"返回值错误：{text!r}"
    assert c.last_usage.get("total_tokens") == 5, "usage 未记录"


@case("4. LLMClient 流式：SSE 解析 + on_delta 逐段回调")
def t_client_stream():
    sse = (
        'data: {"choices":[{"delta":{"reasoning_content":"思考"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"答"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"案"}}],"usage":{"total_tokens":7}}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, content=sse.encode("utf-8"),
                              headers={"content-type": "text/event-stream"})

    c = _mk_mock_client(handler)
    deltas: list[str] = []
    text = c.chat("fake", "m1", [{"role": "user", "content": "hi"}],
                  stream=True, on_delta=deltas.append)
    assert text == "答案", f"流式拼接错误：{text!r}"
    assert deltas == ["答", "案"], f"on_delta 回调错误：{deltas}"
    assert c.last_reasoning_chars == 2, "思维链字符数未记录"


@case("5. LLMClient 只有思维链 -> 自动非流式加大预算重试")
def t_reasoning_retry():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        body = json.loads(request.content)
        if body.get("stream"):
            sse = ('data: {"choices":[{"delta":{"reasoning_content":"很长的思维链"}}]}\n\n'
                   "data: [DONE]\n\n")
            return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})
        # 非流式重试应给足预算
        assert int(body["max_tokens"]) >= FAST_MIN_MAX_TOKENS, "重试预算不足"
        return httpx.Response(200, json={"choices": [{"message": {"content": "兜底答案"}}]})

    c = _mk_mock_client(handler)
    deltas: list[str] = []
    text = c.chat("fake", "m1", [{"role": "user", "content": "hi"}],
                  stream=True, on_delta=deltas.append, max_tokens=100)
    assert text == "兜底答案", f"思维链兜底失败：{text!r}"
    assert calls["n"] == 2, f"应恰好重试一次，实际请求 {calls['n']} 次"
    assert "兜底答案" in "".join(deltas), "兜底文本未回调给 UI"


@case("6. LLMClient 401 不重试、错误信息不含 api_key")
def t_http_error():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, content=b'{"error":"invalid key sk-test-SECRET-123456"}')

    c = _mk_mock_client(handler)
    try:
        c.chat("fake", "m1", [{"role": "user", "content": "hi"}], stream=False)
        raise AssertionError("401 应当抛 LLMError")
    except LLMError as e:
        msg = str(e)
        assert "401" in msg, f"错误信息丢失状态码：{msg}"
        assert "SECRET-123456" not in msg, f"错误信息泄露了 api_key：{msg}"
        assert "***" in msg, f"api_key 应被替换为 ***：{msg}"
    assert calls["n"] == 1, f"401 不应重试，实际 {calls['n']} 次"


@case("7. LLMClient 5xx 自动重试一次后成功")
def t_retry_5xx():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, content=b'{"error":"upstream busy"}')
        return httpx.Response(200, json={"choices": [{"message": {"content": "重试成功"}}]})

    c = _mk_mock_client(handler)
    t0 = time.time()
    text = c.chat("fake", "m1", [{"role": "user", "content": "hi"}], stream=False)
    assert text == "重试成功" and calls["n"] == 2
    assert time.time() - t0 < 5, "重试退避过长"


# ================================================================== 路由
@case("8. 路由：各 kind 的 lane 与可配置模型链")
def t_routing():
    cfg = load_config()
    bus = EventBus()
    brain = Brain(cfg, bus, None)
    primary = cfg["llm"]["primary"]
    fast = cfg["llm"]["fast"]

    assert primary["provider"] and primary["model"], "主脑必须可配置"
    assert fast["provider"] and fast["model"], "辅助模型必须可配置"

    # lane 解析
    assert brain._lane(KIND_ANSWER, "auto") == LANE_DEEP
    assert brain._lane(KIND_SCRIPT, "auto") == LANE_DEEP
    assert brain._lane(KIND_FREE_QA, "auto") == LANE_DEEP
    assert brain._lane(KIND_SLIDE, "auto") == LANE_DEEP
    assert brain._lane(KIND_SUMMARY, "auto") == LANE_DEEP
    assert brain._lane(KIND_MINUTES, "auto") == LANE_DEEP
    assert brain._lane(KIND_HINT, "auto") == LANE_FAST
    assert brain._lane(KIND_TERM, "auto") == LANE_FAST
    assert brain._lane(KIND_ANSWER, "fast") == LANE_FAST, "显式 lane 应覆盖 auto 路由"
    assert brain._lane(KIND_HINT, "deep") == LANE_DEEP
    assert DEEP_KINDS == {KIND_ANSWER, KIND_SCRIPT, KIND_FREE_QA, KIND_SLIDE,
                          KIND_SUMMARY, KIND_MINUTES}

    # 自由提问使用显式 provider/model，未配置时跟随 primary。
    qa_chain = brain._chains(KIND_FREE_QA, LANE_DEEP)
    expected_provider = cfg["llm"].get("free_qa_provider") or primary["provider"]
    expected_model = cfg["llm"].get("free_qa_model") or primary["model"]
    assert (qa_chain[0]["provider"], qa_chain[0]["model"]) == \
        (expected_provider, expected_model), f"自由提问模型链错误：{qa_chain[0]}"

    # 模型链：deep = 主脑 -> 辅助；fast = 辅助 -> 主脑，且 reasoning 预算 >= 700
    deep_chain = brain._chains(KIND_ANSWER, LANE_DEEP)
    assert (deep_chain[0]["provider"], deep_chain[0]["model"]) == \
        (primary["provider"], primary["model"]), f"deep 首选错误：{deep_chain[0]}"
    assert (deep_chain[1]["provider"], deep_chain[1]["model"]) == \
        (fast["provider"], fast["model"]), f"deep 降级链错误：{deep_chain}"

    fast_chain = brain._chains(KIND_HINT, LANE_FAST)
    assert (fast_chain[0]["provider"], fast_chain[0]["model"]) == \
        (fast["provider"], fast["model"]), f"fast 首选错误：{fast_chain[0]}"
    assert fast_chain[0]["max_tokens"] >= FAST_MIN_MAX_TOKENS, "reasoning 模型思维链预算不足"
    assert (fast_chain[1]["provider"], fast_chain[1]["model"]) == \
        (primary["provider"], primary["model"]), "fast 最后兜底应是主脑"

    # 超时：deep 更宽松，fast 更短
    assert brain._timeout_for(LANE_FAST) <= 30 < brain._timeout_for(LANE_DEEP) <= 90

    # 上下文拼装：知识库命中 + 最近转写都会进 messages
    class FakeKB:
        def retrieve(self, q, top_k=None):
            return [{"title": "掩码实验", "text": "收缩扩张在 RefCOCO 上做等预算对比。", "score": 2.0}]

    brain2 = Brain(cfg, EventBus(), FakeKB())
    msgs = brain2._build_messages(KIND_ANSWER, BrainRequest(
        kind=KIND_ANSWER, payload="老师问收益", context="刚才老师追问掩码细节"), "")
    user_text = msgs[-1]["content"]
    assert isinstance(user_text, str) and "最近转写" in user_text and "老师追问掩码细节" in user_text
    kb_text = brain2._kb_context("老师问收益")
    assert "掩码实验" in kb_text, f"知识库检索上下文为空：{kb_text!r}"
    msgs2 = brain2._build_messages(KIND_ANSWER,
                                   BrainRequest(kind=KIND_ANSWER, payload="x", context="最近讨论"), kb_text)
    assert "过往记忆" in msgs2[-1]["content"], "知识库上下文未作为「过往记忆」拼进 messages"
    assert "最近转写" in msgs2[-1]["content"]
    assert "禁止编造" in msgs2[0]["content"], "system prompt 缺少防编造约束"
    assert "知识库" not in msgs2[0]["content"], "system prompt 不应再把知识库写成硬背景"
    # 自由提问默认让通用知识主导；相关记忆只是可选补充，可显式关闭。
    msgs_qa = brain2._build_messages(KIND_FREE_QA,
                                     BrainRequest(kind=KIND_FREE_QA, payload="sim2real gap 是什么"),
                                     "不相关的记忆片段")
    assert "项目速览节选" not in msgs_qa[-1]["content"]
    assert "不相关的记忆片段" in msgs_qa[-1]["content"]
    brain2.cfg.setdefault("brain", {})["free_qa_memory"] = False
    msgs_qa_off = brain2._build_messages(KIND_FREE_QA,
                                         BrainRequest(kind=KIND_FREE_QA, payload="sim2real gap 是什么"),
                                         "不该出现的记忆片段")
    assert "不该出现的记忆片段" not in msgs_qa_off[-1]["content"]


@case("9. 去重：同 kind 同内容 30s 内跳过，过期/新内容放行")
def t_dedupe():
    cfg = load_config()
    brain = Brain(cfg, EventBus(), None)
    req = BrainRequest(kind=KIND_HINT, lane="fast", payload="")
    assert brain._should_skip(req)[0] is False, "第一次不应跳过"
    brain._remember(req, "上一条要点")
    skip, prev = brain._should_skip(req)
    assert skip is True and prev == "上一条要点", "第二次同请求未去重"
    assert brain._should_skip(req, now=time.time() + 31)[0] is False, "31 秒后应放行"

    req_other = BrainRequest(kind=KIND_HINT, lane="fast", payload="完全不同的问题")
    assert brain._should_skip(req_other)[0] is False, "内容不同的请求不应被去重"

    req_answer = BrainRequest(kind=KIND_ANSWER, lane="deep", payload="老师问我收益")
    brain._remember(req_answer, "答案文本")
    assert brain._should_skip(req_answer)[0] is True
    req_answer2 = BrainRequest(kind=KIND_ANSWER, lane="deep", payload="老师问我收益")
    assert brain._should_skip(req_answer2)[0] is True, "同 kind 同内容应去重"
    req_answer3 = BrainRequest(kind=KIND_ANSWER, lane="deep", payload="老师换了个问题")
    assert brain._should_skip(req_answer3)[0] is False, "新问题不应被旧缓存挡住"


# ================================================================== Brain.handle 兜底
@case("10. Brain.handle 成功路径：首包 title、流式 delta、done 完整文本")
def t_handle_success():
    cfg = load_config()

    class FakeStreamClient:
        last_usage = {"total_tokens": 12}

        def __init__(self):
            self.calls = []

        def chat(self, provider, model, messages, *, stream=True, on_delta=None,
                 max_tokens=None, temperature=None, timeout=None):
            self.calls.append((provider, model, stream, max_tokens))
            for p in ["结论：收益主要来自掩码精修。", "但要做等预算对比。"]:
                on_delta(p)
            return "结论：收益主要来自掩码精修。但要做等预算对比。"

    bus = EventBus()
    brain = Brain(cfg, bus, None)
    fake = FakeStreamClient()
    brain._client = fake
    req = BrainRequest(kind=KIND_HINT, lane="fast", payload="老师问掩码收益", context="现场讨论")
    brain.handle(req)
    evs = _drain(bus)
    assert evs, "没有 emit 任何事件"
    assert evs[0].title == "实时要点", f"首包 title 错误：{evs[0].title}"
    assert evs[0].done is False, "首包不应是 done"
    assert any(not e.done and e.delta for e in evs), "没有流式 delta 事件"
    done = [e for e in evs if e.done]
    assert len(done) == 1, f"done 事件数量错误：{len(done)}"
    assert done[0].full_text == "结论：收益主要来自掩码精修。但要做等预算对比。"
    assert done[0].request_id == req.request_id
    assert done[0].lane == LANE_FAST
    assert fake.calls and fake.calls[0][0] == cfg["llm"]["fast"]["provider"], "fast lane 选错模型"
    assert brain.last_stats.get("ok") is True


@case("11. Brain.handle 全部失败：必定 done + 非空提示文本（UI 不转圈）")
def t_handle_all_fail():
    cfg = load_config()

    class FakeFailClient:
        last_usage = {}

        def chat(self, *a, **kw):
            raise LLMError("模拟网络错误", retriable=False)

    bus = EventBus()
    brain = Brain(cfg, bus, None)
    brain._client = FakeFailClient()
    req = BrainRequest(kind=KIND_ANSWER, lane="deep", payload="老师问我实验数据")
    brain.handle(req)   # 不应抛异常
    evs = _drain(bus)
    done = [e for e in evs if e.done]
    assert done, "全部失败时也必须 emit done 事件"
    assert done[-1].full_text.strip(), "done 事件 full_text 为空，UI 会一直转圈"
    assert done[-1].error, "done 事件应带 error 说明"
    assert brain.last_stats.get("ok") is False

    # 未预期异常（非 LLMError）也不能冒泡
    class FakeBoomClient:
        last_usage = {}

        def chat(self, *a, **kw):
            raise RuntimeError("boom")

    bus2 = EventBus()
    brain2 = Brain(cfg, bus2, None)
    brain2._client = FakeBoomClient()
    brain2.handle(BrainRequest(kind=KIND_HINT, lane="fast", payload="异常测试"))
    done2 = [e for e in _drain(bus2) if e.done]
    assert done2 and done2[-1].full_text.strip(), "未预期异常时没有兜底 done 事件"


@case("12. Brain.handle 去重路径：第二次同请求秒回上一条")
def t_handle_dedupe():
    cfg = load_config()

    class FakeClient:
        last_usage = {}

        def __init__(self):
            self.calls = 0

        def chat(self, provider, model, messages, *, stream=True, on_delta=None, **kw):
            self.calls += 1
            if on_delta:
                on_delta("首次答案")
            return "首次答案"

    bus = EventBus()
    brain = Brain(cfg, bus, None)
    fake = FakeClient()
    brain._client = fake
    req1 = BrainRequest(kind=KIND_TERM, lane="fast", payload="解释 ablation")
    brain.handle(req1)
    _drain(bus)
    req2 = BrainRequest(kind=KIND_TERM, lane="fast", payload="解释 ablation")
    t0 = time.time()
    brain.handle(req2)
    dt = time.time() - t0
    done = [e for e in _drain(bus) if e.done]
    assert fake.calls == 1, f"重复请求不应再调模型，实际 {fake.calls} 次"
    assert done and "首次答案" in done[-1].full_text, "去重后应复用上一条文本"
    assert dt < 1.0, f"去重路径太慢：{dt:.2f}s"


# ================================================================== BM25 / 知识库
@case("13. BM25 建索引 + 检索召回 + 权重生效")
def t_bm25():
    docs = [
        {"title": "掩码实验", "text": "掩码收缩扩张精修在 RefCOCO 上做等预算对比，收益来自边界更准。",
         "source": "a.md"},
        {"title": "预测器方案", "text": "反事实动作表配合冻结编辑器，先预测再编辑，baseline 是随机动作。",
         "source": "b.md"},
        {"title": "物理一致性", "text": "视频生成的物理一致性用轨迹追踪与人工双盲评估。", "source": "c.md"},
    ]
    tmp = tempfile.mkdtemp(prefix="mckb_test_")
    try:
        stats = build_index(docs, tmp)
        assert stats["docs"] == 3 and stats["chunks"] >= 3
        idx_path = Path(stats["index_path"])
        assert idx_path.exists() and idx_path.stat().st_size > 500
        payload = json.loads(idx_path.read_text(encoding="utf-8"))
        for k in ("chunks", "docs", "postings", "idf", "built_at", "avgdl"):
            assert k in payload, f"index.json 缺少字段 {k}"

        cfg = dict(_fake_provider_cfg())
        cfg["kb"] = {"dir": tmp, "top_k": 3}
        kb = KnowledgeBase(cfg)
        assert kb.loaded is True and kb.stats()["docs"] == 3
        for q, expected in [
            ("掩码收缩扩张收益", "掩码实验"),
            ("反事实动作表", "预测器方案"),
            ("物理一致性轨迹", "物理一致性"),
        ]:
            hits = kb.retrieve(q, top_k=2)
            assert hits and hits[0]["title"] == expected, f"检索召回错误 {q!r}: {[h['title'] for h in hits]}"
            assert set(["title", "text", "score"]) <= set(hits[0]), "返回字段不符合契约"
            assert hits[0]["score"] > 0
        assert kb.retrieve("", top_k=2) == [], "空查询应返回 []"
        assert isinstance(kb.retrieve("完全不相干的另一个话题"), list)

        # 权重生效：同样内容，weight 大者排前
        tmp2 = tempfile.mkdtemp(prefix="mckb_w_")
        try:
            build_index([
                {"title": "低权重", "text": "掩码收缩扩张收益分析", "source": "x", "weight": 1.0},
                {"title": "高权重", "text": "掩码收缩扩张收益分析", "source": "y", "weight": 3.0},
            ], tmp2)
            cfg2 = dict(_fake_provider_cfg())
            cfg2["kb"] = {"dir": tmp2, "top_k": 2}
            kb2 = KnowledgeBase(cfg2)
            hits2 = kb2.retrieve("掩码收缩扩张收益", top_k=2)
            assert hits2[0]["title"] == "高权重", "材料权重未影响排序"
        finally:
            import shutil
            shutil.rmtree(tmp2, ignore_errors=True)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


@case("14. 空库/无索引：load False、retrieve 空、stats 零，且不抛")
def t_empty_kb():
    tmp = tempfile.mkdtemp(prefix="mckb_empty_")
    try:
        cfg = dict(_fake_provider_cfg())
        cfg["kb"] = {"dir": tmp, "top_k": 3}
        kb = KnowledgeBase(cfg)
        assert kb.loaded is False
        assert kb.retrieve("掩码收缩") == []
        st = kb.stats()
        assert st["chunks"] == 0 and st["docs"] == 0 and st["built_at"] == ""
        # 索引损坏也不抛
        (Path(tmp) / "index.json").write_text("{坏掉的json", encoding="utf-8")
        kb2 = KnowledgeBase(cfg)
        assert kb2.loaded is False and kb2.retrieve("掩码") == []
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


@case("15. 切块 500/80 + 分词（停用词、英文小写、领域术语）")
def t_chunk_tokenize():
    text = "".join(f"第{i}段：掩码收缩扩张精修与等预算对比 (shrink-expand) 很重要。" for i in range(80))
    pieces = chunk_text(text)
    assert len(pieces) >= 5, f"切块过少：{len(pieces)}"
    assert all(len(p) <= 520 for p in pieces), "切块超过 500+20"
    # 相邻块应有 80 字左右重叠
    overlap = pieces[0][-CHUNK_OVERLAP // 2:]
    assert overlap in pieces[1], "相邻块重叠不足"
    toks = tokenize("掩码收缩扩张的收益 (Shrink-Expand) 很重要，的 了 是")
    assert "掩码" in toks, f"jieba 未切出掩码：{toks}"
    assert "shrink-expand" in toks or "shrink" in toks, f"英文未小写/未保留：{toks}"
    assert "的" not in toks and "了" not in toks and "是" not in toks, "停用词未过滤"


# ================================================================== 视觉
@case("16. 视觉：缩放长边 1280 / PNG base64 / 图片消息格式")
def t_vision_prepare():
    cfg = load_config()
    vr = VisionReader(cfg, EventBus(), None)
    max_side = int(cfg["vision"]["max_width"])
    big = Image.new("RGB", (5120, 1600), (10, 20, 30))
    small = vr._fit(big)
    assert max(small.size) == max_side, f"长边未缩到 {max_side}: {small.size}"
    assert vr._fit(Image.new("RGB", (640, 360))).size == (640, 360), "小图不应放大"
    b64 = VisionReader._png_b64(small)
    raw = base64.b64decode(b64)
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", "base64 不是 PNG"
    assert len(b64) > 1000

    # grab 真实截屏（不联网）：构造 client=None 也能返回图
    img, b64b = vr.grab("primary")
    assert img.size[0] > 0 and b64b
    assert max(img.size) <= max_side, "grab 未缩放"
    assert base64.b64decode(b64b)[:8] == b"\x89PNG\r\n\x1a\n"

    # 图片消息格式（OpenAI 兼容 image_url）
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "解读"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64b}}]}]
    part = msgs[-1]["content"][-1]
    assert part["type"] == "image_url" and part["image_url"]["url"].startswith("data:image/png;base64,")


@case("17. 视觉降级链：主模型失败 -> fallback 模型成功 -> done 非空")
def t_vision_fallback():
    cfg = load_config()
    # 无论用户配置是否启用备用模型，都显式构造一个来验证降级链。
    cfg["vision"]["fallback_models"] = [str(cfg["vision"]["model"]) + "-fallback"]
    models_called: list[str] = []

    class FakeVisionClient:
        last_usage = {}

        def chat(self, provider, model, messages, *, stream=True, on_delta=None,
                 max_tokens=None, temperature=None, timeout=None):
            models_called.append(model)
            if model == cfg["vision"]["model"]:
                raise LLMError("视觉主模型 503 不可用", status=503)
            if on_delta:
                on_delta("【页面主题】测试页")
            return "【页面主题】测试页\n【关键点】第一点\n【公式/数字】本页无公式数字\n【可能被问】问题1"

    bus = EventBus()
    vr = VisionReader(cfg, bus, FakeVisionClient())
    # 替换 grab，避免依赖桌面内容（不联网）
    fake_img = Image.new("RGB", (1280, 720), (30, 40, 50))
    fake_b64 = VisionReader._png_b64(fake_img)
    vr.grab = lambda mode=None: (fake_img, fake_b64)  # type: ignore[method-assign]
    ev = vr.read_screen(question="解读这一页")
    assert ev.done is True and ev.kind == KIND_SLIDE
    assert ev.full_text.strip(), "降级后仍应返回非空解读"
    assert models_called[0] == cfg["vision"]["model"], "应先试视觉主模型"
    assert len(models_called) >= 2 and models_called[1] in cfg["vision"]["fallback_models"], \
        f"未按 fallback_models 降级：{models_called}"
    assert vr.last_stats.get("vision_ok") is True
    evs = _drain(bus)
    assert evs, "视觉解读应流式 emit"
    assert json.dumps(evs[0].delta, ensure_ascii=False).find("正在看屏幕") >= 0, "缺少「正在看屏幕」占位事件"
    assert [e for e in evs if e.done], "缺少 done 事件"


@case("18. 视觉全失败 -> 本地 OCR 兜底（不联网）")
def t_vision_ocr_fallback():
    cfg = load_config()

    class FakeFailVision:
        last_usage = {}

        def chat(self, *a, **kw):
            raise LLMError("视觉模型全挂", status=503)

    bus = EventBus()
    vr = VisionReader(cfg, bus, FakeFailVision())
    fake_img = Image.new("RGB", (1280, 720), (30, 40, 50))
    vr.grab = lambda mode=None: (fake_img, VisionReader._png_b64(fake_img))  # type: ignore[method-assign]
    vr.ocr = lambda image: "页面标题：实验进展\n指标：mIoU"  # type: ignore[method-assign]
    ev = vr.read_screen()
    assert ev.done and "OCR" in ev.full_text, f"未走本地 OCR 兜底：{ev.full_text!r}"
    assert "指标" in ev.full_text
    assert ev.error, "降级路径应带 error 说明"
    assert vr.last_stats.get("ocr_used") is True

    # OCR 也失败时仍必须给非空提示
    vr2 = VisionReader(cfg, bus, FakeFailVision())
    vr2.grab = lambda mode=None: (fake_img, VisionReader._png_b64(fake_img))  # type: ignore[method-assign]
    vr2.ocr = lambda image: ""  # type: ignore[method-assign]
    ev2 = vr2.read_screen()
    assert ev2.done and ev2.full_text.strip(), "OCR 也失败时必须返回非空提示文本"


@case("18b. OCR 输出兼容：RapidOCR 3.x 对象与旧版列表")
def t_ocr_result_formats():
    class ModernOutput:
        txts = ["页面标题", "", "关键指标"]

    assert VisionReader._ocr_text(ModernOutput()) == "页面标题\n关键指标"
    legacy = [[[0, 0, 1, 1], "第一行", 0.99], [[0, 2, 1, 3], "第二行", 0.98]]
    assert VisionReader._ocr_text(legacy) == "第一行\n第二行"
    assert VisionReader._ocr_text([]) == ""


@case("19. 知识库空时 Brain 不崩、无知识库上下文也能正常构造 prompt")
def t_brain_empty_kb():
    cfg = load_config()
    for kb in (None, False, object()):
        brain = Brain(cfg, EventBus(), kb)
        ctx = brain._kb_context("掩码收缩扩张收益")
        assert ctx == "", f"异常 kb 应安全返回空串，实际 {ctx!r}"
        msgs = brain._build_messages(KIND_HINT, BrainRequest(kind=KIND_HINT, payload="测试"), "")
        assert msgs and msgs[-1]["content"].strip(), "无知识库时 prompt 为空"

    # 不读取开发者的本地索引；真实检索由下一个临时目录用例覆盖。


@case("20. 独立临时材料：通用查询稳定召回，不读取私人索引")
def t_real_queries():
    import shutil
    tmp = tempfile.mkdtemp(prefix="mckb_public_test_")
    try:
        build_index([
            {"title": "实验设计记录", "source": "experiment.md",
             "text": "对照组与实验组应使用相同数据和计算预算，消融实验用于验证各模块贡献。"},
            {"title": "项目复盘", "source": "project.md",
             "text": "项目难点是数据噪声与系统延迟，解决方法是增加清洗规则和异步处理。"},
        ], tmp)
        cfg = dict(_fake_provider_cfg())
        cfg["kb"] = {"dir": tmp, "top_k": 3}
        kb = KnowledgeBase(cfg)
        assert kb.loaded
        for query, expected in [("消融实验怎么设计", "实验设计记录"),
                                ("项目难点和解决方法", "项目复盘")]:
            hits = kb.retrieve(query, top_k=3)
            assert hits and hits[0]["title"] == expected, \
                f"query {query!r} 召回错误：{[h['title'] for h in hits]}"
            print(f"     {query} -> top1=《{hits[0]['title']}》")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@case("21. 历史材料只在相关检索命中后作为可选记忆，项目速览不恒定注入")
def t_overview_inject():
    cfg = load_config()
    brain = Brain(cfg, EventBus(), None)
    assert brain._overview_text() == "", "项目速览不应恒定注入"
    msgs = brain._build_messages(KIND_HINT, BrainRequest(kind=KIND_HINT, payload="测试"), "")
    system, user = msgs[0]["content"], msgs[-1]["content"]
    assert "过往记忆" not in user and "项目速览节选" not in user
    assert "项目速览" not in system and "知识库" not in system
    assert "禁止编造" in system and "通用专业知识" in system
    memory = "《旧实验记录》\n曾比较过两个方案，但结论可能已过时。"
    answer_user = brain._build_messages(
        KIND_ANSWER, BrainRequest(kind=KIND_ANSWER, payload="后来为什么换方案"), memory)[-1]["content"]
    assert "过往记忆" in answer_user and "不相关就忽略" in answer_user
    qa_user = brain._build_messages(
        KIND_FREE_QA, BrainRequest(kind=KIND_FREE_QA, payload="sim2real gap 是什么"), "")[-1]["content"]
    assert "过往记忆" not in qa_user, "无相关记忆时自由提问应由模型通用知识回答"
    msgs_img = brain._build_messages(KIND_SLIDE, BrainRequest(
        kind=KIND_SLIDE, payload="", image_b64="AAAA"), memory)
    assert "过往记忆" in msgs_img[-1]["content"][0]["text"]


# ================================================================== runner
def main() -> int:
    init_console()
    print("=" * 72)
    print("tests/test_brain_vision.py —— 离线测试（无网络 / 无需麦克风）")
    print("=" * 72)
    t0 = time.time()
    for name, fn in _TESTS:
        _run_one(name, fn)
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    failed = len(_RESULTS) - passed
    print("-" * 72)
    print(f"结果：{passed}/{len(_RESULTS)} 通过，{failed} 失败，耗时 {time.time()-t0:.2f}s")
    if failed:
        print("失败用例：")
        for name, ok, msg in _RESULTS:
            if not ok:
                print(f"  - {name}: {msg}")
        return 1
    print("ALL TESTS PASSED ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
