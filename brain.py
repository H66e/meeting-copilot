# -*- coding: utf-8 -*-
"""双脑 LLM 大脑：LLMClient（OpenAI 兼容流式/非流式 + 重试 + ping）+ Brain（五模式提示词/路由/去重）。

对外契约（见 INTERFACES.md 第 4 节 B1）::

    client = LLMClient(cfg)
    text = client.chat("openai", "gpt-4o-mini", messages,
                       stream=True, on_delta=fn, max_tokens=900, temperature=0.4)
    ok, latency, msg = client.ping("deepseek", "deepseek-chat")

    brain = Brain(cfg, bus, kb)          # kb 可以是 KnowledgeBase / None / 甚至 bool
    brain.handle(req)                    # 同步执行，通过 bus.emit_brain 流式推送
    brain.kickoff(req)                   # 丢内部队列，由 worker 线程消费
    brain.stop()

路由：
- 深思考 kind（ANSWER / SLIDE / SUMMARY / MINUTES）→ llm.primary
- 低延迟 kind（HINT / TERM）→ llm.fast
- 自由提问可用 llm.free_qa_provider/model 独立指定模型
- 主脑失败 → cfg.llm.fallback（默认降级到 fast）；fast 失败 → 最后兜底主脑

兼容性处理：
- 部分 reasoning 模型的 SSE 正文在 delta.content，思维链在 delta.reasoning_content。
  思维链会消耗 max_tokens；正文为空而 reasoning 非空时，自动改用非流式并重试一次。
- 无论成功失败，Brain.handle 必定 emit 一个 done=True 的 BrainEvent，且 full_text
  非空——绝不让 UI 一直转圈。

日志/异常里绝不打印 api_key。
"""
from __future__ import annotations

import json
import queue
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

import httpx

from config import get
from contracts import (KIND_ANSWER, KIND_FREE_QA, KIND_HINT, KIND_MINUTES, KIND_SCRIPT,
                       KIND_SLIDE, KIND_SUMMARY, KIND_TERM, LANE_DEEP, LANE_FAST,
                       BrainEvent, BrainRequest, EventBus, fmt_ts)
from logging_setup import get_logger
from scenarios import is_interview

# ------------------------------------------------------------------ 常量
KIND_TITLES = {
    KIND_HINT: "实时要点",
    KIND_ANSWER: "应答思路",
    KIND_SCRIPT: "照读稿",
    KIND_FREE_QA: "自由提问",
    KIND_SLIDE: "屏幕解读",
    KIND_TERM: "术语解释",
    KIND_SUMMARY: "阶段小结",
    KIND_MINUTES: "最终纪要",
}
# 走主脑（deep）的 kind：需要深思考
DEEP_KINDS = {KIND_ANSWER, KIND_SCRIPT, KIND_FREE_QA, KIND_SLIDE, KIND_SUMMARY, KIND_MINUTES}
# 走辅助（fast）的 kind：低延迟
FAST_KINDS = {KIND_HINT, KIND_TERM}
# reasoning 模型的正文预算下限（deepseek-flash 思维链会吃 token，实测 <=300 时正文为空）
FAST_MIN_MAX_TOKENS = 700
DEDUPE_WINDOW_S = 30.0

_SYSTEM_PROMPT = """你是「Meeting Copilot」，帮助用户理解和参与研究组会。

判断依据的优先级：当前问题与现场转写 > 你的通用专业知识 > 可选的历史记忆。
历史记忆只是过往记录，可能过时或与当前话题无关；只在确实相关时参考，绝不把它当作回答范围、固定口径或必须提及的背景。记忆没有覆盖时，应正常运用自己的知识分析、解释和设计实验，不要回避问题。

禁止编造用户做过的工作、实验、指标或数字。通用技术知识可以正常回答；涉及用户个人工作且没有依据时，明确区分已知、推测和待验证。现场转写可能有错字，结合语义理解，不要逐字照抄明显错误的识别结果。

默认使用自然、清楚的中文，保留必要的英文术语。不要展示思维链，不要说「我理解了」「准备就绪」等元回复。"""

_INTERVIEW_SYSTEM_PROMPT = _SYSTEM_PROMPT.replace(
    "研究组会", "面试").replace(
    "默认使用自然、清楚的中文", "面试应答应直接、诚实、有重点，先给结论，再补真实依据；不知道的经历或数字不编造。默认使用自然、清楚的中文")

_TASKS = {
    KIND_ANSWER: (
        "【任务】根据触发内容和最近讨论，给出用户现在可用的应答思路。"
        "输出 1~3 条编号短句，每条直接表达一个结论、依据或下一步；优先回答真正被问的内容，"
        "不要复述材料，不要强行关联旧项目。每条一般不超过 60 字；问题复杂时可适当增加一条。"
        "没有明确问题时，概述最近讨论中最值得补充的一点，不要编造提问者的具体问题。"
    ),
    KIND_SCRIPT: (
        "【任务】写一段用户可以直接照着念的中文口语稿。第一人称，第一句就进入实质内容，"
        "通常 80~180 字、2~4 句，长短由问题复杂度决定。只输出口语正文，不要编号或标题。"
        "内容应回应当前讨论；可以使用通用专业知识提出合理方案，但不要把建议说成已经完成的实验或经历。"
        "信息不足时采用诚实、审慎的表述，不要套用固定项目话术。"
    ),
    KIND_FREE_QA: (
        "【任务】这是用户主动提出的问题。直接回答问题本身，充分发挥你的通用知识和推理能力。"
        "可以解释机制、比较方案、给出实现步骤或实验设计；以清楚、有用为先，不设机械字数上限。"
        "简单问题用 1~2 段，复杂问题最多分 4 点。不要复述问题，不要展示思维链，"
        "不要因为没有历史材料而拒答，也不要主动把话题拉回旧项目。"
    ),
    KIND_HINT: (
        "【任务】从最近讨论中提炼 1~3 条真正有用的实时提示，可以是关键结论、潜在追问或可补充的观点。"
        "每条一行，简洁自然；不要只复述转写，也不要强行联系无关的历史项目。"
    ),
    KIND_SLIDE: (
        "【任务】解读当前共享页面，依次输出：【页面主题】、【关键点】、【公式/数字】、【可能被问】。"
        "只依据图片实际内容，看不清就明说；可以结合通用知识解释，但不得编造页面数字。"
    ),
    KIND_TERM: (
        "【任务】用两行解释触发内容中的术语：第一行是准确、通俗的定义；第二行结合当前讨论说明它的意义。"
        "当前讨论不清楚时，只给定义和一个典型用途，不要强行套用固定项目口径。"
    ),
    KIND_SUMMARY: (
        "【任务】总结目前讨论，分「已明确的结论」「仍待确认」「下一步」三组，每组 1~3 条。"
        "没有内容的组省略。严格区分现场事实、模型推断和尚未开展的方案。"
    ),
    KIND_MINUTES: (
        "【任务】整理会议纪要，分【核心结论】【待办事项】【遗留问题】。"
        "忠实依据转写，区分实测与方案；没有负责人或时间的信息不要补造。"
    ),
}


# ================================================================== 异常
class LLMError(Exception):
    """LLM 调用错误。retriable=True 表示值得对同一模型重试（超时/限流/5xx）。"""

    def __init__(self, message: str, *, status: int | None = None, retriable: bool = False):
        super().__init__(message)
        self.status = status
        self.retriable = retriable


# ================================================================== SSE 解析（纯函数，便于离线测试）
def iter_sse(lines: Iterable[str]) -> Iterator[dict]:
    """把 SSE 行流解析成事件 dict。

    yield 形如：
        {"content": "正文增量"}
        {"reasoning": "思维链增量"}
        {"usage": {...}}
        {"done": True}            # 收到 data: [DONE]
    非 data 行、注释、坏 JSON 一律跳过，不抛异常。
    """
    for raw in lines:
        if raw is None:
            continue
        if isinstance(raw, bytes):
            try:
                raw = raw.decode("utf-8", "replace")
            except Exception:
                continue
        line = raw.strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data:
            continue
        if data == "[DONE]":
            yield {"done": True}
            return
        try:
            obj = json.loads(data)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        usage = obj.get("usage")
        choices = obj.get("choices") or []
        delta = (choices[0].get("delta") or {}) if isinstance(choices, list) and choices else {}
        if not isinstance(delta, dict):
            delta = {}
        content = delta.get("content")
        if content is None:
            content = delta.get("text")          # 少数中转用 text 字段
        if isinstance(content, list):            # 极少数中转返回 content parts
            content = "".join(
                str(p.get("text") or "") for p in content if isinstance(p, dict)
            )
        reasoning = delta.get("reasoning_content")
        if reasoning is None:
            reasoning = delta.get("reasoning")
        ev: dict = {}
        if content:
            ev["content"] = str(content)
        if reasoning:
            ev["reasoning"] = str(reasoning)
        if usage:
            ev["usage"] = usage
        if ev:
            yield ev


def extract_text(payload: dict) -> tuple[str, str, dict]:
    """从非流式响应里取 (content, reasoning, usage)，兼容 OpenAI / anthropic 中转风格。"""
    if not isinstance(payload, dict):
        return "", "", {}
    choices = payload.get("choices") or []
    msg = (choices[0].get("message") or {}) if choices else {}
    if not isinstance(msg, dict):
        msg = {}
    content = msg.get("content")
    if content is None:
        content = msg.get("text") or ""
    if isinstance(content, list):
        content = "".join(
            str(p.get("text") or p.get("content") or "")
            for p in content if isinstance(p, dict)
        )
    reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
    if isinstance(reasoning, list):
        reasoning = "".join(str(x) for x in reasoning)
    usage = payload.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    return str(content or "").strip(), str(reasoning or ""), usage


def _mask_secret(text: str, *secrets: str) -> str:
    """把可能混进错误信息的 api_key 替换掉（日志安全）。"""
    out = str(text)
    for s in secrets:
        if s and len(s) >= 8:
            out = out.replace(s, "***")
    return out


# ================================================================== LLMClient
class LLMClient:
    """OpenAI 兼容 /chat/completions 客户端（支持 SSE 流式、超时、重试、ping）。"""

    def __init__(self, cfg: dict, log=None):
        self.cfg = cfg or {}
        self.log = log or get_logger("llm")
        self._providers: dict = self.cfg.get("_providers") or {}
        self._default_timeout = float(
            get(self.cfg, "llm.request_timeout_s", 120) or 120
        )
        self._http = httpx.Client(timeout=None, follow_redirects=True)
        self._lock = threading.Lock()
        self.last_usage: dict = {}
        self.last_latency: float = 0.0
        self.last_reasoning_chars: int = 0
        self._effort_logged: set[str] = set()

    # ---------------------------------------------------------- 内部
    def close(self) -> None:
        try:
            self._http.close()
        except Exception:
            pass

    def _conf(self, provider_name: str) -> dict:
        prov = (self._providers or {}).get(provider_name)
        if not prov:
            raise LLMError(f"未配置 LLM provider「{provider_name}」")
        if not prov.get("api_key"):
            env = prov.get("api_key_env") or ""
            raise LLMError(
                f"provider「{provider_name}」缺少 API key"
                + (f"（应来自环境变量/凭据 {env}）" if env else "")
                + "，请在 ~/.dsh/.credentials.yaml 配置"
            )
        base = prov.get("base_url") or ""
        if not base:
            raise LLMError(f"provider「{provider_name}」缺少 base_url")
        return prov

    def _url(self, provider_name: str) -> str:
        return self._conf(provider_name)["base_url"].rstrip("/") + "/chat/completions"

    def _headers(self, provider_name: str) -> dict:
        return {
            "Authorization": "Bearer " + self._conf(provider_name)["api_key"],
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

    # 仅在 provider 显式声明支持时发送 reasoning_effort，避免不兼容端点报错。
    _EFFORT_ALIASES = {"middle": "medium", "mid": "medium", "medium": "medium",
                       "low": "low", "minimal": "low", "high": "high", "max": "high"}

    def _effort_for(self, provider_name: str, model: str) -> str:
        prov = (self._providers or {}).get(provider_name) or {}
        if not bool(prov.get("supports_reasoning_effort", False)):
            return ""
        raw = str(get(self.cfg, "llm.thinking", "") or "").strip().lower()
        effort = self._EFFORT_ALIASES.get(raw, "")
        if effort and effort not in self._effort_logged:
            self._effort_logged.add(effort)
            self.log.info("%s/%s 思考强度：%s", provider_name, model, effort)
        return effort

    def _payload(self, model: str, messages: list[dict], *, stream: bool,
                 max_tokens: int | None, temperature: float | None,
                 effort: str = "") -> dict:
        body: dict[str, Any] = {"model": model, "messages": messages, "stream": bool(stream)}
        if max_tokens is not None:
            body["max_tokens"] = int(max_tokens)
        if temperature is not None:
            body["temperature"] = float(temperature)
        if effort:
            # 中转要求 adaptive thinking：用 reasoning_effort 控制思考强度（low/medium/high）
            body["reasoning_effort"] = str(effort)
        return body

    def _set_usage(self, usage: dict, reasoning_chars: int = 0) -> None:
        with self._lock:
            if usage:
                self.last_usage = dict(usage)
            self.last_reasoning_chars = int(reasoning_chars)

    def _http_error(self, provider: str, model: str, status: int, body: str) -> LLMError:
        prov = (self._providers or {}).get(provider) or {}
        safe_body = _mask_secret(body[:300], str(prov.get("api_key") or ""))
        retriable = status == 429 or status >= 500
        return LLMError(f"{provider}/{model} 返回 HTTP {status}: {safe_body}",
                        status=status, retriable=retriable)

    def _log_error(self, provider: str, model: str, err: Exception) -> str:
        prov = (self._providers or {}).get(provider) or {}
        msg = _mask_secret(f"{type(err).__name__}: {err}", str(prov.get("api_key") or ""))
        self.log.warning("LLM 调用失败 %s/%s: %s", provider, model, msg)
        return msg

    # ---------------------------------------------------------- 非流式
    def _chat_once(self, provider: str, model: str, payload: dict,
                   timeout: float) -> tuple[str, str]:
        req_timeout = httpx.Timeout(connect=10.0, read=timeout, write=30.0, pool=10.0)
        try:
            resp = self._http.post(self._url(provider), headers=self._headers(provider),
                                   json=payload, timeout=req_timeout)
        except httpx.TimeoutException:
            raise LLMError(f"{provider}/{model} 请求超时（{timeout:.0f}s）", retriable=True)
        except httpx.HTTPError as e:
            raise LLMError(f"{provider}/{model} 网络错误：{type(e).__name__}", retriable=True)
        if resp.status_code != 200:
            body = ""
            try:
                body = resp.text
            except Exception:
                pass
            raise self._http_error(provider, model, resp.status_code, body)
        try:
            data = resp.json()
        except Exception:
            raise LLMError(f"{provider}/{model} 返回非 JSON 响应", retriable=True)
        text, reasoning, usage = extract_text(data)
        self._set_usage(usage, len(reasoning))
        return text, reasoning

    # ---------------------------------------------------------- 流式
    def _chat_stream(self, provider: str, model: str, payload: dict, timeout: float,
                     on_delta: Callable[[str], None] | None) -> tuple[str, str]:
        parts: list[str] = []
        reasoning_parts: list[str] = []
        usage: dict = {}
        req_timeout = httpx.Timeout(connect=10.0, read=timeout, write=30.0, pool=10.0)
        try:
            with self._http.stream("POST", self._url(provider),
                                   headers=self._headers(provider),
                                   json=payload, timeout=req_timeout) as resp:
                if resp.status_code != 200:
                    body = ""
                    try:
                        body = resp.read().decode("utf-8", "replace")
                    except Exception:
                        pass
                    raise self._http_error(provider, model, resp.status_code, body)
                for ev in iter_sse(resp.iter_lines()):
                    if ev.get("done"):
                        break
                    if ev.get("usage"):
                        usage = ev["usage"]
                    if ev.get("reasoning"):
                        reasoning_parts.append(ev["reasoning"])
                    piece = ev.get("content")
                    if piece:
                        parts.append(piece)
                        if on_delta is not None:
                            try:
                                on_delta(piece)
                            except Exception:
                                # UI 回调异常不能拖垮请求
                                self.log.warning("on_delta 回调异常（已忽略）")
        except httpx.TimeoutException:
            raise LLMError(f"{provider}/{model} 流式请求超时（{timeout:.0f}s）", retriable=True)
        except httpx.HTTPError as e:
            raise LLMError(f"{provider}/{model} 网络错误：{type(e).__name__}", retriable=True)
        text = "".join(parts).strip()
        reasoning = "".join(reasoning_parts)
        self._set_usage(usage, len(reasoning))
        return text, reasoning

    # ---------------------------------------------------------- 对外
    def chat(self, provider_name: str, model: str, messages: list[dict], *,
             stream: bool = True, on_delta: Callable[[str], None] | None = None,
             max_tokens: int | None = None, temperature: float | None = None,
             timeout: float | None = None) -> str:
        """一次对话请求，返回完整正文文本；流式时逐段调用 on_delta。

        自动处理：超时/网络错误/429/5xx 重试一次；reasoning 模型正文为空时
        自动改用非流式 + 更大预算再试一次。
        """
        t0 = time.time()
        timeout = float(timeout if timeout is not None else self._default_timeout)
        effort = self._effort_for(provider_name, model)
        payload = self._payload(model, messages, stream=stream,
                                max_tokens=max_tokens, temperature=temperature,
                                effort=effort)
        attempts = 3 if effort else 2
        last_err: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                if stream:
                    text, reasoning = self._chat_stream(provider_name, model, payload,
                                                        timeout, on_delta)
                    if not text and reasoning:
                        # 思维链把 max_tokens 吃光了：换非流式、放开预算再试一次
                        self.log.warning("%s/%s 只返回了思维链(%d字)，改用非流式重试",
                                         provider_name, model, len(reasoning))
                        bigger = max(int(max_tokens or FAST_MIN_MAX_TOKENS), FAST_MIN_MAX_TOKENS)
                        payload2 = self._payload(model, messages, stream=False,
                                                 max_tokens=bigger,
                                                 temperature=temperature, effort=effort)
                        text, reasoning = self._chat_once(provider_name, model,
                                                          payload2, timeout)
                        if text and on_delta is not None:
                            try:
                                on_delta(text)
                            except Exception:
                                pass
                else:
                    text, _reasoning = self._chat_once(provider_name, model, payload, timeout)
                if not text:
                    raise LLMError(
                        f"{provider_name}/{model} 返回空文本"
                        + ("（只有思维链）" if self.last_reasoning_chars else ""),
                        retriable=False)
                self.last_latency = round(time.time() - t0, 2)
                return text
            except LLMError as e:
                last_err = e
                # 中转若拒绝 reasoning_effort（例如不支持该值）：去掉参数再试一次
                if e.status == 400 and effort and "reasoning" in str(e).lower():
                    self.log.warning("中转不接受 reasoning_effort=%s，去掉后重试: %s",
                                     effort, str(e)[:120])
                    effort = ""
                    payload = self._payload(model, messages, stream=stream,
                                            max_tokens=max_tokens,
                                            temperature=temperature, effort="")
                    continue
                if not e.retriable or attempt >= attempts:
                    break
                self._log_error(provider_name, model, e)
                time.sleep(0.7 * attempt)
            except Exception as e:  # 绝不把未知异常抛给 UI 线程
                last_err = LLMError(self._log_error(provider_name, model, e))
                break
        if last_err is None:
            last_err = LLMError(f"{provider_name}/{model} 调用失败")
        raise last_err

    def ping(self, provider_name: str, model: str) -> tuple[bool, float, str]:
        """连通性探针：返回 (ok, 延迟秒, 简短说明)。不打印/不返回密钥。

        注意 deepseek-flash 是 reasoning 模型，max_tokens 给小了会只回思维链、
        正文为空，所以这里直接给足 700 token 预算；若仍只有思维链，也判为"可达"。
        """
        t0 = time.time()
        try:
            text = self.chat(provider_name, model,
                             [{"role": "user", "content": "请只回复两个字：收到"}],
                             stream=False, max_tokens=FAST_MIN_MAX_TOKENS,
                             temperature=0.0,
                             timeout=min(45.0, self._default_timeout))
            dt = round(time.time() - t0, 2)
            return True, dt, f"OK：{text[:30]!r}"
        except Exception as e:
            dt = round(time.time() - t0, 2)
            prov = (self._providers or {}).get(provider_name) or {}
            msg = _mask_secret(f"{type(e).__name__}: {e}", str(prov.get("api_key") or ""))
            if "只有思维链" in msg:
                return True, dt, "OK（reasoning 模型，连通正常）"
            return False, dt, msg[:200]


# ================================================================== Brain
class Brain:
    """按 kind/lane 选模型、拼中文提示词、流式 emit BrainEvent。"""

    def __init__(self, cfg: dict, bus: EventBus, kb, log=None):
        self.cfg = cfg or {}
        self.bus = bus
        self.kb = kb
        self.log = log or get_logger("brain")
        self._client = LLMClient(self.cfg, self.log)
        self._default_timeout = float(get(self.cfg, "llm.request_timeout_s", 120) or 120)
        self._lock = threading.Lock()
        self._last: dict[tuple, dict] = {}        # 去重：key -> {"ts","text"}
        self._recent: deque[str] = deque(maxlen=120)   # 可选：main 若调用 feed_transcript 则用
        self._q: "queue.PriorityQueue" = queue.PriorityQueue()
        self._seq = 0
        self._worker: threading.Thread | None = None
        self._stop = False
        self.last_stats: dict = {}
        self._overview_cache: dict = {"mtime": 0.0, "text": ""}

    # ---------------------------------------------------------- 属性
    @property
    def client(self) -> LLMClient:
        return self._client

    # ---------------------------------------------------------- 可选：喂转写
    def feed_transcript(self, text: str, t_start: float | None = None) -> None:
        """main 可选调用：把字幕文本喂进来，作为 Brain 自带的最近上下文。

        （正式契约里上下文由 BrainRequest.context 传入；这里只是兜底/便利接口）"""
        text = (text or "").strip()
        if not text:
            return
        line = f"[{fmt_ts(t_start)}] {text}" if t_start is not None else text
        with self._lock:
            self._recent.append(line)

    def _recent_transcript(self, max_chars: int = 1600) -> str:
        with self._lock:
            text = "\n".join(self._recent)
        return text[-max_chars:]

    # ---------------------------------------------------------- 路由
    def _lane(self, kind: str, lane: str | None) -> str:
        if lane in (LANE_FAST, LANE_DEEP):
            return lane
        return LANE_DEEP if kind in DEEP_KINDS else LANE_FAST

    def _spec_from_conf(self, name: str) -> dict:
        """把 cfg.llm.primary / cfg.llm.fast 变成统一 spec；缺失时给默认值。"""
        conf = get(self.cfg, f"llm.{name}", {}) or {}
        default_provider = "openai" if name == "primary" else "deepseek"
        default_model = "gpt-4o-mini" if name == "primary" else "deepseek-chat"
        return {
            "name": name,
            "provider": conf.get("provider") or default_provider,
            "model": conf.get("model") or default_model,
            "max_tokens": int(conf.get("max_tokens") or (900 if name == "primary" else 700)),
            "temperature": float(conf.get("temperature", 0.4 if name == "primary" else 0.3)),
            "lane": conf.get("lane") or (LANE_DEEP if name == "primary" else LANE_FAST),
        }

    def _chains(self, kind: str, lane: str) -> list[dict]:
        """返回按优先级排列的模型尝试链（含主脑→辅助降级）。

        深思考 lane：主脑 primary → 辅助 fast 兜底；
        低延迟 lane：辅助 fast → 主脑 primary 兜底（fast 用 reasoning 模型时也必须留足预算）。
        自由提问可用 llm.free_qa_provider/model 指定模型，缺省跟随 primary。
        cfg.llm.fallback 里的别名只用于"补充"降级项，不改变主次顺序。
        """
        primary = self._spec_from_conf("primary")
        fast = self._spec_from_conf("fast")
        if kind == KIND_FREE_QA:
            free_model = str(get(self.cfg, "llm.free_qa_model", primary["model"])
                             or primary["model"])
            free_provider = str(get(self.cfg, "llm.free_qa_provider", primary["provider"])
                                or primary["provider"])
            free_qa = {"name": "free_qa", "provider": free_provider, "model": free_model,
                       "max_tokens": primary["max_tokens"], "temperature": primary["temperature"],
                       "lane": LANE_DEEP}
            chain = [free_qa, primary, fast]
        elif lane == LANE_FAST:
            # reasoning 模型必须留足思维链预算（实测 <=300 正文为空）
            fast = dict(fast, max_tokens=max(int(fast.get("max_tokens") or 0), FAST_MIN_MAX_TOKENS))
            chain = [fast, primary]
        else:
            chain = [primary, fast]
        aliases = {"primary": primary, "fast": fast}
        for fb in (get(self.cfg, "llm.fallback", []) or []):
            if isinstance(fb, str) and fb in aliases and aliases[fb] not in chain:
                chain.append(aliases[fb])
        return chain

    def _timeout_for(self, lane: str) -> float:
        if lane == LANE_FAST:
            return max(10.0, min(self._default_timeout, 30.0))
        return max(20.0, min(self._default_timeout, 90.0))

    # ---------------------------------------------------------- 去重
    @staticmethod
    def _dedupe_key(req: BrainRequest) -> tuple:
        """同 kind + 同触发内容才算重复；自动类请求 payload 为空，等价于同 kind 去重。"""
        payload = re.sub(r"\s+", "", (req.payload or "")).lower()[:160]
        return (req.kind, payload)

    def _should_skip(self, req: BrainRequest, now: float | None = None) -> tuple[bool, str]:
        """同 kind 30 秒内重复请求兜底去重；返回 (是否跳过, 上一条文本)。"""
        now = time.time() if now is None else now
        key = self._dedupe_key(req)
        with self._lock:
            last = self._last.get(key)
        if last and now - float(last.get("ts") or 0) < DEDUPE_WINDOW_S:
            return True, str(last.get("text") or "")
        return False, ""

    def _remember(self, req: BrainRequest, text: str) -> None:
        key = self._dedupe_key(req)
        with self._lock:
            self._last[key] = {"ts": time.time(), "text": text}
            # 顺手清理过期记录，避免长会无限增长
            cutoff = time.time() - DEDUPE_WINDOW_S * 4
            for k in [k for k, v in self._last.items() if float(v.get("ts") or 0) < cutoff]:
                self._last.pop(k, None)

    # ---------------------------------------------------------- 上下文
    def _memory_enabled(self, kind: str) -> bool:
        """记忆默认按需检索；off 完全关闭，on 也只注入检索到的片段。"""
        raw = get(self.cfg, "brain.free_qa_memory", "smart") if kind == KIND_FREE_QA \
            else get(self.cfg, "brain.memory_mode", "smart")
        if isinstance(raw, bool):
            return raw
        return str(raw or "smart").strip().lower() not in ("off", "false", "0", "none", "关闭")

    def _kb_context(self, query: str) -> str:
        """只返回达到相关度门槛的少量历史片段；没有命中就不带记忆。"""
        kb = self.kb
        if kb is None or not hasattr(kb, "retrieve"):
            return ""
        try:
            hits = kb.retrieve(query, top_k=int(get(self.cfg, "kb.top_k", 2) or 2))
        except Exception as e:
            self.log.warning("知识库检索异常（忽略）：%s", e)
            return ""
        min_score = float(get(self.cfg, "kb.min_score", 1.5) or 1.5)
        hits = [h for h in hits if float(h.get("score", 0.0) or 0.0) >= min_score]
        if not hits:
            return ""
        limit = int(get(self.cfg, "kb.max_chars", 1800) or 1800)
        parts: list[str] = []
        used = 0
        for h in hits:
            title = str(h.get("title") or "")
            text = str(h.get("text") or "")
            piece = f"《{title}》{text}"
            if used + len(piece) > limit and parts:
                break
            parts.append(piece[: max(0, limit - used)])
            used += len(piece)
        return "\n---\n".join(parts)[:limit]

    def _system_prompt(self) -> str:
        return _INTERVIEW_SYSTEM_PROMPT if is_interview(self.cfg) else _SYSTEM_PROMPT

    @staticmethod
    def _memory_text(kb_text: str) -> str:
        return str(kb_text or "").strip()

    def _overview_text(self) -> str:
        """兼容旧自测接口；项目速览不再恒定注入，只能经相关检索进入记忆。"""
        return ""

    def _build_messages(self, kind: str, req: BrainRequest, kb_text: str) -> list[dict]:
        max_ctx = int(get(self.cfg, "brain.max_context_chars", 4500) or 4500)
        memory = self._memory_text(kb_text) if self._memory_enabled(kind) else ""
        blocks: list[str] = []
        ctx = (req.context or "").strip() or self._recent_transcript()
        if ctx:
            blocks.append("【当前讨论 / 最近转写（可能有识别错字）】\n" + ctx[-max_ctx:])
        if req.payload:
            label = "我的问题" if kind == KIND_FREE_QA else "当前触发"
            blocks.append(f"【{label}】\n" + req.payload.strip()[:2000])
        if memory:
            blocks.append("【过往记忆（可选）：仅在确实相关时参考，不相关就忽略】\n" + memory)
        task = _TASKS.get(kind, _TASKS[KIND_HINT])
        user_text = "\n\n".join(blocks + [task]).strip()
        system_prompt = self._system_prompt()
        if req.image_b64 and kind == KIND_SLIDE:
            content: list[dict] = [{"type": "text", "text": user_text}]
            content.append({"type": "image_url", "image_url": {
                "url": "data:image/png;base64," + req.image_b64}})
            return [{"role": "system", "content": system_prompt},
                    {"role": "user", "content": content}]
        return [{"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text}]

    # ---------------------------------------------------------- emit
    def _emit(self, ev: BrainEvent) -> None:
        try:
            self.bus.emit_brain(ev)
        except Exception as e:
            self.log.warning("emit_brain 异常（忽略）：%s", e)

    def _emit_done(self, req: BrainRequest, kind: str, lane: str, title: str,
                   text: str, *, error: str | None = None, usage: dict | None = None) -> None:
        self._emit(BrainEvent(
            request_id=req.request_id, kind=kind, lane=lane, delta="", done=True,
            error=error, title=title, usage=dict(usage or {}), full_text=text or "",
        ))

    # ---------------------------------------------------------- 输出清洗
    _FRAME_HINTS = ("应答思路", "照读稿", "回答要点", "回答如下", "参考回答", "分析：",
                    "以下", "以下是")

    def _clean_output(self, kind: str, text: str) -> str:
        """去掉模型偶尔加上的「思考过程/旁白/开场白」，只留答案本体（done 时生效）。"""
        t = str(text or "").strip()
        if not t:
            return t
        import re as _re
        lines = [ln.rstrip() for ln in t.splitlines()]
        while lines:
            first = lines[0].strip()
            if not first:
                lines.pop(0)
                continue
            if (first.endswith(("：", ":")) and any(k in first for k in self._FRAME_HINTS)) \
                    or first in ("回答：", "答案：", "应答思路：", "照读稿："):
                lines.pop(0)
                continue
            break
        cleaned = "\n".join(ln for ln in lines if ln.strip()).strip()
        cleaned = _re.sub(r"^(好的|明白|收到)[，,。、]\s*", "", cleaned)
        cleaned = _re.sub(r"^我(先|首先)(说|讲)(一下)?(结论)?[，,：:]\s*", "", cleaned)
        cleaned = _re.sub(r"^老师(您)?(问|说)的(是|问题)[，,：:].{0,30}[。；;]\s*", "", cleaned)
        # 元回复清理（模型偶尔会说"我理解了/已记住/准备就绪"这类确认话）
        cleaned = _re.sub(r"^(我)?(已经)?(理解|明白|收到)(了)?[，,。、：:\s].*?[。；;\n]\s*", "", cleaned)
        cleaned = _re.sub(r"^(已)?记住.{0,40}?[。；;\n]\s*", "", cleaned)
        cleaned = _re.sub(r"^等待(触发|问题|内容|指令).{0,60}?[。；;\n]\s*", "", cleaned)
        cleaned = _re.sub(r"^(我会|我会直接).{0,40}?[。；;\n]\s*", "", cleaned)
        cleaned = _re.sub(r"^准备(好|就绪).{0,20}?[。；;\n]\s*", "", cleaned)
        return cleaned.strip() or t

    def _fallback_text(self, kb_text: str, err: str) -> str:
        """所有模型都失败时保证 UI 有内容、不转圈。"""
        head = "（AI 暂时不可用，先顶一句：这个问题我记下来了，会后对照实验数据确认后再回复。）"
        if kb_text:
            return head + "\n（过往记忆片段：" + kb_text[:240].replace("\n", " ") + "…）"
        return head

    # ---------------------------------------------------------- 核心
    def handle(self, req: BrainRequest) -> None:
        """同步执行一次请求：流式 emit BrainEvent；任何异常都转成 done 事件，绝不外抛。"""
        t0 = time.time()
        kind = req.kind or KIND_HINT
        lane = self._lane(kind, req.lane)
        title = KIND_TITLES.get(kind, "建议")
        req_id = req.request_id
        try:
            skip, prev_text = self._should_skip(req)
            if skip:
                self.log.info("去重：%s 30 秒内重复请求，跳过（request_id=%s）", kind, req_id)
                text = prev_text or "（30 秒内已给过同类建议，跳过重复请求）"
                self._emit(BrainEvent(request_id=req_id, kind=kind, lane=lane,
                                      title=title, delta="", done=False))
                self._emit_done(req, kind, lane, title, text)
                self.last_stats = {"kind": kind, "lane": lane, "skipped": True,
                                   "total": round(time.time() - t0, 2), "provider": "-",
                                   "model": "-", "ttft": 0.0, "chars": len(text), "ok": True}
                return

            # 首包：立刻给 UI 一个带标题的占位事件（避免"看起来没反应"）
            self._emit(BrainEvent(request_id=req_id, kind=kind, lane=lane,
                                  title=title, delta="", done=False))

            query = (req.payload or "").strip()
            recent = (req.context or "")[-900:] or self._recent_transcript(900)
            query = (query + "\n" + recent).strip()
            # 所有模式都先让通用模型回答；记忆仅在启用且检索足够相关时补充。
            kb_text = self._kb_context(query) if self._memory_enabled(kind) else ""
            messages = self._build_messages(kind, req, kb_text)
            chain = self._chains(kind, lane)
            timeout = self._timeout_for(lane)

            state = {"ttft": 0.0, "any": False, "chars": 0}

            def on_delta(piece: str) -> None:
                if not piece:
                    return
                if not state["any"]:
                    state["any"] = True
                    state["ttft"] = round(time.time() - t0, 2)
                state["chars"] += len(piece)
                self._emit(BrainEvent(request_id=req_id, kind=kind, lane=lane,
                                      title=title, delta=piece, done=False))

            last_err = ""
            used_spec: dict = {}
            for i, spec in enumerate(chain):
                used_spec = spec
                try:
                    text = self._client.chat(
                        spec["provider"], spec["model"], messages,
                        stream=True, on_delta=on_delta,
                        max_tokens=int(spec["max_tokens"]),
                        temperature=float(spec["temperature"]),
                        timeout=timeout,
                    )
                    if text and text.strip():
                        # 不再用写死的项目话术替换模型回答。
                        text = self._clean_output(kind, text)
                        usage = dict(getattr(self._client, "last_usage", {}) or {})
                        self._emit_done(req, kind, lane, title, text, usage=usage)
                        self._remember(req, text)
                        self.last_stats = {
                            "kind": kind, "lane": lane, "provider": spec["provider"],
                            "model": spec["model"], "ttft": state["ttft"],
                            "total": round(time.time() - t0, 2),
                            "chars": len(text), "ok": True, "skipped": False,
                            "fallback_used": i,
                        }
                        self.log.info("完成 %s/%s lane=%s ttft=%.2fs total=%.2fs chars=%d",
                                      spec["provider"], spec["model"], lane,
                                      state["ttft"], time.time() - t0, len(text))
                        return
                    last_err = f"{spec['provider']}/{spec['model']} 返回空文本"
                except Exception as e:
                    prov = ((self.cfg.get("_providers") or {}).get(spec["provider"]) or {})
                    last_err = _mask_secret(f"{type(e).__name__}: {e}",
                                            str(prov.get("api_key") or ""))[:240]
                    self.log.warning("模型尝试失败（第 %d 个）: %s", i + 1, last_err)
                    if i + 1 < len(chain):
                        # 已经流式输出的部分保留，提示正在换用备用模型
                        if state["any"]:
                            self._emit(BrainEvent(request_id=req_id, kind=kind, lane=lane,
                                                  title=title, delta="\n（换用备用模型重试…）\n",
                                                  done=False))

            # 整条链都失败：done 事件也必须带非空文本，UI 不能一直转圈
            fb = self._fallback_text(kb_text, last_err)
            self._emit_done(req, kind, lane, title, fb,
                            error=last_err or "全部模型调用失败")
            self.last_stats = {
                "kind": kind, "lane": lane, "ok": False, "skipped": False,
                "provider": used_spec.get("provider", "-"),
                "model": used_spec.get("model", "-"),
                "ttft": state["ttft"], "total": round(time.time() - t0, 2),
                "chars": len(fb), "error": last_err,
            }
        except Exception as e:  # 最后一道防线
            self.log.exception("Brain.handle 未预期异常")
            try:
                self._emit_done(req, kind, lane, title,
                                "（大脑内部异常，先按上面字幕继续听，稍后再试）",
                                error=_mask_secret(f"{type(e).__name__}: {e}")[:200])
            except Exception:
                pass

    # ---------------------------------------------------------- 队列 worker
    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop = False
            self._worker = threading.Thread(target=self._worker_loop,
                                            name="brain-worker", daemon=True)
            self._worker.start()

    def _worker_loop(self) -> None:
        while not self._stop:
            try:
                item = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            _prio, _seq, req = item
            if req is None:
                break
            try:
                self.handle(req)
            except Exception:
                self.log.exception("brain worker 消费异常（已忽略）")

    def kickoff(self, req: BrainRequest) -> None:
        """丢内部队列异步执行（main 也可以直接调 handle）。"""
        self._ensure_worker()
        with self._lock:
            self._seq += 1
            seq = self._seq
        self._q.put((int(req.priority), seq, req))

    def stop(self) -> None:
        self._stop = True
        self._q.put((0, 0, None))
        w = self._worker
        if w is not None and w.is_alive():
            w.join(timeout=2.0)
        self._worker = None

    def close(self) -> None:
        self.stop()
        try:
            self._client.close()
        except Exception:
            pass


# ================================================================== 自测
def _selftest() -> int:
    """可选的真实 API 冒烟；离线回归请运行 tests/，无需密钥。"""
    from config import init_console, load_config
    from contracts import EventBus
    from knowledge_base import KnowledgeBase

    init_console()
    print("=" * 72)
    print("brain.py --selftest（真实 API：当前 primary / fast 配置）")
    print("=" * 72)
    cfg = load_config()
    bus = EventBus()
    kb = KnowledgeBase(cfg)
    brain = Brain(cfg, bus, kb)
    specs = [brain._spec_from_conf("primary"), brain._spec_from_conf("fast")]
    missing = cfg.get("_missing_keys") or []
    if any(spec["provider"] in missing for spec in specs):
        print(f"SKIP：当前模型缺少 API key（missing={missing}），未发起网络请求")
        return 0

    for spec in specs:
        ok, latency, message = brain.client.ping(spec["provider"], spec["model"])
        print(f"  {'✅' if ok else '❌'} {spec['provider']}/{spec['model']}："
              f"{latency:.2f}s，{message}")
    print(f"知识库：loaded={kb.loaded} stats={kb.stats()}；记忆仅按需检索")

    def run_case(name: str, req: BrainRequest) -> None:
        while True:
            try:
                bus.brain_events.get_nowait()
            except queue.Empty:
                break
        print(f"\n[{name}] {req.payload}")
        brain.handle(req)
        events = []
        while True:
            try:
                events.append(bus.brain_events.get_nowait())
            except queue.Empty:
                break
        done = [event for event in events if event.done]
        assert done and done[-1].full_text.strip(), "未收到非空 done 事件"
        print(done[-1].full_text)

    run_case("组会应答", BrainRequest(
        kind=KIND_ANSWER, lane="auto",
        payload="老师问：这个对照实验还不能证明收益来自你的方法，下一步怎么补齐？",
        context="正在讨论实验对照、相同计算预算和失败案例。"))
    run_case("自由提问", BrainRequest(
        kind=KIND_FREE_QA, lane="auto",
        payload="请用通俗语言解释消融实验，并举一个不涉及个人经历的例子。"))
    print("\nSELFTEST OK ✅")
    brain.close()
    return 0


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print("用法：python brain.py --selftest")
