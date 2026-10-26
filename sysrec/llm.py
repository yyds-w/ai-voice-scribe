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
#: 摘要提示词里这两个占位符会被 LlmClient 渲染成具体的读者/用途
#  （2026-10-26 优化：读者与用途是摘要质量的最大杠杆——同一段录音，
#   "给研发组长 30 秒判断跟进什么"和"给管理层决定是否投入"该写完全不同）
DEFAULT_READER = "录音主人本人（参会者/内容创作者），想快速回顾"
DEFAULT_PURPOSE = "30 秒内抓住要点，找出待办、责任人与争议"

DEFAULT_PROMPT = (
    "你是严谨的会议纪要助手。下面是一场会议/课程/视频的**实时语音转写文本**——"
    "可能有个别识别错别字或重复词，不要因此改变原意，更不要据此编造。\n"
    "【读者】{读者}\n"
    "【用途】{用途}\n"
    "请输出结构化中文摘要：\n"
    "1. 主题（一句话）\n"
    "2. 核心要点（3~7 条，每条一行；优先决策、结论、责任人、期限、关键数字）\n"
    "3. 待办事项（谁 + 做什么 + 时间；原文没提就写「待确认」，不要猜）\n"
    "4. 争议或待确认问题（多方说法不一致时并列保留，不要合并成含糊结论）\n"
    "\n"
    "【必须保留】决策与结论、责任人、期限、关键数字与指标、领域关键实体名\n"
    "【可以省略】寒暄、重复背景、与决策无关的细节、营销措辞与铺垫\n"
    "\n"
    "【忠实度规则】\n"
    "1. 只用原文已有内容，不得补充原文没有的日期、人名、数字\n"
    "2. 数字/百分比/指标/专有名词照抄原文，不改写、不四舍五入\n"
    "3. 原文未提及的信息写「待确认」，不得猜测\n"
    "4. 原文明确表达了因果才可用「导致/因此」；只是先后叙述时不得加因果词\n"
    "5. 原文不足以支撑某条目时就不写该条，不要凑内容；整段太短直接说明\n"
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
    "1. 这段讨论了什么（3~6 条，每条一行；优先决策、结论、责任人、期限、关键数字）\n"
    "2. 得出的结论 / 决定（没有就写“无”）\n"
    "3. 提到的待办或承诺（谁、做什么；没有就写“无”）\n"
    "忠实度：数字与专有名词照抄原文；原文没说的不补；只是先后叙述不得加“因此/导致”。\n"
    "不要写开场白，不要复述原文。"
)

MERGE_PROMPT = (
    "以下是同一场会议按时间顺序分段生成的摘要。请合并成一份完整的中文会议纪要：\n"
    "1. 主题（一句话）\n"
    "2. 核心要点（按主题归并、去重，5~12 条）\n"
    "3. 决策与结论\n"
    "4. 待办事项（如有，尽量标注负责人与时间；原文没提写“待确认”）\n"
    "5. 遗留问题 / 待确认（不同段落说法不一致时并列保留，不要抹平成一句）\n"
    "只依据摘要内容组织，不要编造，也不要逐段罗列；数字沿用原文。"
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
        # 2026-10-26：提示词支持 {读者} / {用途} 占位符——这两个变量是摘要质量的
        # 最大杠杆（同一段内容给不同读者该写不同摘要）。配置可覆盖，留空用默认。
        # 推理模型开关（None=不传该参数；True/False=显式传给服务端）
        et = c.get("enable_thinking")
        self.enable_thinking = None if et is None else bool(et)
        self.reader = str(c.get("summary_reader") or DEFAULT_READER).strip()
        self.purpose = str(c.get("summary_purpose") or DEFAULT_PURPOSE).strip()
        self.prompt_template = c.get("prompt_template") or DEFAULT_PROMPT

    def render_prompt(self, template: str = "") -> str:
        """把提示词里的 {读者} / {用途} 渲染成实际值（其它花括号原样保留）。"""
        t = template or self.prompt_template or ""
        try:
            return t.replace("{读者}", self.reader).replace("{用途}", self.purpose)
        except Exception:                      # noqa: BLE001
            return t

    # ---------- 基础调用 ----------

    def chat(self, messages, timeout=None, max_tokens=None, _retry_think=False) -> str:
        """发一次 chat/completions；失败抛 LlmError（消息面向用户、可读）。

        2026-10-26：**推理模型**（deepseek-v4.1-flash 这类会返回
        `reasoning_content` 思维链）实测：6 分钟转写稿摘要时，
        max_tokens=2000 → 思维链 3154 字、正文 **0 字**（token 全被思考吃掉）；
        提到 8000 仍不够（思维链 11934 字）。而 `enable_thinking: false`
        → 1.9s、正文正常、思维链 0 字。
        所以这里做**自动降级**：正文为空且存在思维链 → 带 `enable_thinking=false`
        重试一次（只一次，避免死循环）。
        """
        if not self.api_key:
            raise LlmError("未配置 API Key（设置 → LLM 摘要 → API Key）")
        url = self.base_url + "/chat/completions"
        body_d = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": int(max_tokens or self.max_tokens),
            "stream": False,
        }
        et = getattr(self, "enable_thinking", None)
        if et is not None:                       # profile 显式设置：照办
            body_d["enable_thinking"] = bool(et)
        elif _retry_think:                       # 自动降级：关掉思考再试
            body_d["enable_thinking"] = False
        body = json.dumps(body_d).encode("utf-8")
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
            msg = data["choices"][0]["message"]
            content = msg.get("content") or ""
        except (KeyError, IndexError, TypeError):
            raise LlmError("响应格式异常（缺少 choices[0].message.content）")
        if content.strip():
            return content.strip()
        # 正文为空：若是"推理模型把预算耗在思考上"→ 自动降级重试一次
        reasoning = ""
        try:
            reasoning = str(msg.get("reasoning_content") or "")
        except Exception:                      # noqa: BLE001
            reasoning = ""
        if reasoning and not _retry_think and getattr(self, "enable_thinking", None) is None:
            return self.chat(messages, timeout=timeout, max_tokens=max_tokens,
                             _retry_think=True)
        if reasoning:
            raise LlmError(
                "模型把输出预算全用在思维链上（reasoning %d 字，正文为空）。"
                "请在 LLM 设置里把 max_tokens 调大，或给该模型关闭思考模式"
                "（enable_thinking=false）。" % len(reasoning))
        return ""

    # ---------- 摘要 ----------

    def summarize(self, text: str) -> str:
        """字幕全文 → 结构化摘要。"""
        text = (text or "").strip()
        if not text:
            raise LlmError("字幕文本为空，无可摘要内容")
        return self.chat([
            {"role": "system", "content": self.render_prompt()},
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


#: 标题首尾要去掉的标点（用常量避免引号在字面量里转义出错）
_TITLE_STRIP_CHARS = " 　《》\"'“”‘’。，、：:！!？?；;-—"


def name_title(client, text: str, retries: int = 1) -> str:
    """给转写文本起一个中文标题（≤20 字，用于摘要缓存/摘要文件名）。

    2026-10-24 用户需求：摘要缓存命名要规范，"可以调用大模型进行命名"。
    只喂前 1200 字（够判断主题，省 token）。

    2026-10-26 修「摘要文件名显示未命名」：
      · 首次请求常因 TLS 握手/DNS 冷启动慢而超时（热请求 0.5s，冷启可能 >30s）
        → timeout 30 → 45s，失败**重试 1 次**（间隔 0.8s）；
      · 失败原因原来只 print 到 stderr（pythonw 无 stderr，等于丢失）→ 改写 startup.log；
      · 仍失败返回 ""，调用方用启发式标题兜底（gui._fallback_title）。
    """
    head = (text or "").strip()[:1200]
    if not head:
        return ""
    last_err = ""
    for attempt in range(max(0, int(retries)) + 1):
        try:
            r = client.chat([
                {"role": "system", "content":
                 "给下面这段会议/课程/视频的转写文本起一个中文标题：6~20 个字，"
                 "概括主题，不要引号、不要句号、不要解释，只输出标题本身。"},
                {"role": "user", "content": head},
            ], timeout=45)
            raw = (r or "").strip()
            t = raw.splitlines()[0].strip() if raw else ""
            t = t.strip(_TITLE_STRIP_CHARS)
            if t:
                return t
            last_err = "模型返回空标题"
        except Exception as e:  # noqa: BLE001  命名失败不影响摘要
            last_err = "%s: %s" % (type(e).__name__, e)
            if attempt < retries:
                import time as _t
                _t.sleep(0.8)
    try:                                   # 失败原因写日志（pythonw 下 stderr 会丢）
        import os as _os
        from datetime import datetime
        log = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                            "startup.log")
        with open(log, "a", encoding="utf-8") as f:
            f.write(("[%s] name_title 失败（已用启发式标题兜底）：%s"
                     % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), last_err))
                    + chr(10))
    except Exception:                      # noqa: BLE001
        pass
    return ""


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
