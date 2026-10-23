# -*- coding: utf-8 -*-
"""LLM 摘要客户端（OpenAI 兼容 `/chat/completions`）。

为什么用 OpenAI 兼容格式：DeepSeek / 通义 / 智谱 / Kimi / Ollama / vLLM **全部兼容**
这一套接口，一份代码接所有模型。零第三方依赖（只用 urllib）——录音机 venv 不用装
openai SDK。

安全说明：API Key 存 config.json（本地工具可接受），也可用环境变量
`SYSREC_LLM_KEY` 覆盖（优先级低于 config，便于临时切换）。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_PROMPT = (
    "你是会议纪要助手。下面是一场会议/课程/视频的实时转写文本，"
    "请输出结构化中文摘要，包含：\n"
    "1. 主题（一句话）\n"
    "2. 核心要点（3~7 条）\n"
    "3. 待办事项（如有，尽量标注负责人）\n"
    "4. 争议或待确认问题（如有）\n"
    "只依据文本内容，不要编造；文本太短或不足以摘要时直接说明。"
)

# ---- Prompt 三档预设（2026-10-23 用户 Q9：简洁/标准/详细）----
# 设置里"保存模型"旁的预设下拉选中即填入 Prompt 框（可再手改）。
# 注意：详细度主要由 max_tokens + map-reduce 分段架构决定，Prompt 定风格。
PROMPT_PRESETS = {
    "简洁": (
        "你是会议纪要助手。下面是一场会议/课程/视频的实时转写文本。"
        "请输出极简中文摘要：\n"
        "1. 主题（一句话）\n"
        "2. 核心要点（不超过 3 条，每条一行）\n"
        "只保留最关键的信息，不要展开细节，不要编造。"
    ),
    "标准": DEFAULT_PROMPT,
    "详细": (
        "你是会议纪要助手。下面是一场会议/课程/视频的实时转写文本，"
        "请输出详尽的结构化中文纪要，包含：\n"
        "1. 主题与背景（一两句话）\n"
        "2. 核心要点（尽量穷尽，按主题分组，每组 2~5 条，每条说清「谁/做了什么/结论」）\n"
        "3. 关键数据、数字、时间、人名（原文出现过的，原样保留）\n"
        "4. 决策与结论（逐条列出）\n"
        "5. 待办事项（谁、做什么、什么时间；没有就写“无”）\n"
        "6. 争议或待确认问题（原文中的分歧与开放问题）\n"
        "只依据文本内容，不要编造；文本太短或不足以摘要时直接说明。"
    ),
}

# ---- 长会议 map-reduce：先分段提炼，再合并成一份纪要 ----
CHUNK_PROMPT = (
    "你是会议纪要助手。下面是一场长会议**其中一段**的实时语音转写。"
    "转写里可能有个别错别字（语音识别误差）——不要因此改变原意，更不要据此编造。\n"
    "请只提炼这一段的内容：\n"
    "1. 这段讨论了什么（3~6 条，每条一行）\n"
    "2. 得出的结论 / 决定（没有就写“无”）\n"
    "3. 提到的待办或承诺（谁、做什么；没有就写“无”）\n"
    "不要写开场白，不要复述原文。"
)

MERGE_PROMPT = (
    "以下是同一场会议按时间顺序分段生成的摘要。请合并成一份完整的中文会议纪要：\n"
    "1. 主题（一句话）\n"
    "2. 核心要点（按主题归并、去重，5~12 条）\n"
    "3. 决策与结论\n"
    "4. 待办事项（如有，尽量标注负责人与时间）\n"
    "5. 遗留问题 / 待确认\n"
    "只依据摘要内容组织，不要编造，也不要逐段罗列。"
)


class LlmError(Exception):
    """带可读原因的 LLM 调用失败（未配置/网络/超时/鉴权/限流/服务端/格式）。"""


# ---------- 多 LLM profile（2026-10-23 用户需求：保存多个 LLM 随心切换） ----------

PROFILE_FIELDS = ("name", "base_url", "api_key", "model",
                  "temperature", "max_tokens", "timeout_sec", "prompt_template")


def normalize_profile(p: dict) -> dict:
    """把一个 profile dict 补齐成规范形态（缺字段给默认值，用于新建/迁移）。"""
    p = dict(p or {})
    return {
        "name": str(p.get("name") or "").strip(),
        "base_url": str(p.get("base_url") or DEFAULT_BASE_URL).rstrip("/"),
        "api_key": str(p.get("api_key") or ""),
        "model": str(p.get("model") or DEFAULT_MODEL),
        "temperature": float(p.get("temperature", 0.3) or 0.3),
        "max_tokens": int(p.get("max_tokens", 2000) or 2000),
        "timeout_sec": float(p.get("timeout_sec", 60) or 60),
        "prompt_template": str(p.get("prompt_template") or ""),
    }


def active_profile(cfg: dict):
    """按 cfg['llm_active'] 从 cfg['llm_profiles'] 里找当前 profile。

    返回规范化后的 profile dict；没有 profile / 找不到激活名（被删/改名）
    → 降级返回第一个；一个都没有 → None。
    """
    profs = [normalize_profile(p) for p in (cfg.get("llm_profiles") or [])
             if isinstance(p, dict)]
    profs = [p for p in profs if p["name"]]
    if not profs:
        return None
    name = str(cfg.get("llm_active") or "")
    for p in profs:
        if p["name"] == name:
            return p
    return profs[0]


def profile_client_cfg(profile: dict) -> dict:
    """profile → LlmClient(cfg) 吃的扁平 dict。"""
    p = normalize_profile(profile)
    return {k: p[k] for k in ("base_url", "api_key", "model",
                              "temperature", "max_tokens", "timeout_sec",
                              "prompt_template")}


def _classify_http_error(code: int) -> str:
    if code in (401, 403):
        return "API Key 无效或无权限（HTTP %d）" % code
    if code == 429:
        return "触发限流（HTTP 429）——稍后重试或降低频率"
    if 500 <= code < 600:
        return "服务端错误（HTTP %d）——对方服务异常，稍后重试" % code
    return "HTTP 错误 %d" % code


class LlmClient:
    """OpenAI 兼容 chat/completions 客户端（线程安全：无共享可变状态）。"""

    def __init__(self, cfg=None):
        c = dict(cfg or {})
        self.base_url = (c.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = c.get("api_key") or os.environ.get("SYSREC_LLM_KEY", "")
        self.model = c.get("model") or DEFAULT_MODEL
        self.temperature = float(c.get("temperature", 0.3) or 0.3)
        self.max_tokens = int(c.get("max_tokens", 2000) or 2000)
        self.timeout = float(c.get("timeout_sec", 60) or 60)
        self.prompt_template = c.get("prompt_template") or DEFAULT_PROMPT

    # ---------- 基础调用 ----------

    def chat(self, messages, timeout=None, max_tokens=None) -> str:
        """发一次 chat/completions；失败抛 LlmError（消息面向用户、可读）。"""
        if not self.api_key:
            raise LlmError("未配置 API Key（设置 → LLM 摘要 → API Key）")
        url = self.base_url + "/chat/completions"
        body = json.dumps({
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": int(max_tokens or self.max_tokens),
            "stream": False,
        }).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + self.api_key,
        })
        to = timeout or self.timeout
        try:
            with urllib.request.urlopen(req, timeout=to) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "ignore")[:200]
            except Exception:  # noqa: BLE001
                pass
            raise LlmError("%s %s" % (_classify_http_error(e.code), detail))
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
                raise LlmError("请求超时（%ds）——网络慢或服务无响应" % to)
            raise LlmError("网络失败：%s——检查 base_url 与网络连接" % reason)
        except TimeoutError:
            raise LlmError("请求超时（%ds）——网络慢或服务无响应" % to)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LlmError("响应格式异常（缺少 choices[0].message.content）")
        return (content or "").strip()

    # ---------- 摘要 ----------

    def summarize(self, text: str) -> str:
        """字幕全文 → 结构化摘要。"""
        text = (text or "").strip()
        if not text:
            raise LlmError("字幕文本为空，无可摘要内容")
        return self.chat([
            {"role": "system", "content": self.prompt_template},
            {"role": "user", "content": text},
        ])

    def test_connection(self) -> str:
        """最小请求验证配置；成功返回模型回复。"""
        return self.chat([{"role": "user", "content": "请只回复两个字：正常"}],
                         timeout=20)


# ---------- 长文本：map-reduce ----------

def _split_chunks(text: str, limit: int):
    """按行切块（约 limit 字一块）；尽量不把 `## [mm:ss]` 锚点和它后面的正文切开。"""
    lines = (text or "").splitlines()
    chunks, cur, n = [], [], 0
    for ln in lines:
        add = len(ln) + 1
        if cur and n + add > limit and not ln.startswith("## "):
            chunks.append("\n".join(cur))
            cur, n = [], 0
        cur.append(ln)
        n += add
    if cur:
        chunks.append("\n".join(cur))
    return chunks or [text or ""]


_ANCHOR_RE = re.compile(r"^##\s+(\d{1,2}):(\d{2}):(\d{2})\s*$")


def _anchor_seconds(line: str):
    """解析转写稿的时间锚点行 `## HH:MM:SS`（transcript._hms 的输出格式）；不是则 None。"""
    m = _ANCHOR_RE.match((line or "").strip())
    if not m:
        return None
    h, mnt, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
    return h * 3600 + mnt * 60 + s


def _mmss(sec: int) -> str:
    sec = max(0, int(sec))
    return "%02d:%02d" % (sec // 60, sec % 60)


def _split_by_minutes(text: str, minutes: int):
    """按"每 N 分钟一段"切（依赖转写稿的 `## HH:MM:SS` 锚点）。

    返回段列表（每段带 `### 时段 mm:ss–mm:ss` 头）；无锚点 / minutes<=0 返回 None，
    由调用方回退按字符切分。2026-10-21 用户需求：长音频每 20 分钟自动分段。
    """
    lines = (text or "").splitlines()
    secs = int(minutes or 0) * 60
    if secs <= 0:
        return None
    marks = []                                  # (行号, 秒)
    for i, ln in enumerate(lines):
        s = _anchor_seconds(ln)
        if s is not None:
            marks.append((i, s))
    if not marks:
        return None
    segs, start, base = [], 0, 0
    for i, s in marks:
        if s - base >= secs:
            segs.append((start, i, base))
            start, base = i, s
    segs.append((start, len(lines), base))
    out = []
    for k, (a, b, t0) in enumerate(segs):
        if b - a < 2:                           # 空段 / 只有一行 → 跳过
            continue
        if k + 1 < len(segs):
            t1 = segs[k + 1][2]                 # 下一段起点 = 本段覆盖终点
        else:
            inner = [s for i, s in marks if a <= i < b]
            t1 = inner[-1] if inner else t0     # 末段：到最后锚点
        out.append("### 时段 %s–%s\n%s" % (_mmss(t0), _mmss(t1),
                                          "\n".join(lines[a:b])))
    return out or None


def summarize_long(client, text: str, chunk_chars: int = 6000, progress=None,
                   segment_minutes: int = 0) -> str:
    """转写全文 → 纪要。短稿一次过；长稿先分段提炼，再合并成一份。

    为什么不让模型一次性读完全文：2 万字中文（≈1.5 万 token）塞一次，
    中段内容最容易被忽略（"lost in the middle"），待办常常整段漏掉。
    分段 + 合并虽然多几次请求，但每一段的要点都被"看见"过一次。

    segment_minutes > 0 时按"每 N 分钟一段"切（用转写稿时间锚点，段带时段头）；
    无锚点自动回退按字符（chunk_chars）。2026-10-21 用户需求：长音频每 20 分钟分段。
    """
    text = (text or "").strip()
    if not text:
        raise LlmError("转写文本为空，无可摘要内容")
    chunks = None
    if int(segment_minutes or 0) > 0:
        chunks = _split_by_minutes(text, segment_minutes)
        if not chunks and progress:
            progress("（未找到时间锚点，已回退按字数分段）")
    if not chunks:
        chunks = _split_chunks(text, int(chunk_chars or 0)) if chunk_chars else [text]
    if len(chunks) <= 1:
        if progress:
            progress("正在生成摘要…")
        return client.summarize(text)

    parts = []
    for i, ch in enumerate(chunks):
        if progress:
            progress("正在分段提炼 %d/%d…" % (i + 1, len(chunks)))
        got = client.chat([{"role": "system", "content": CHUNK_PROMPT},
                           {"role": "user", "content": ch}],
                          max_tokens=min(900, int(client.max_tokens or 900)))
        parts.append("### 第 %d/%d 段\n%s" % (i + 1, len(chunks), (got or "").strip()))

    if progress:
        progress("正在汇总 %d 段摘要…" % len(chunks))
    return client.chat([{"role": "system", "content": MERGE_PROMPT},
                        {"role": "user", "content": "\n\n".join(parts)}])
