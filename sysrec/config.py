# -*- coding: utf-8 -*-
"""运行配置：读写工具目录下的 config.json。"""
from __future__ import annotations

import json
import os

from . import app_paths

# 可写数据根：源码模式=项目目录；打包后=exe 所在目录（不写 _internal）
APP_DIR = app_paths.data_path()
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

DEFAULTS = {
    "output_dir": "",            # 空字符串 = 默认 <工具目录>\录音
    "format": "mp3",             # mp3 | wav
    "mp3_bitrate": 192,          # kbps
    "segment_minutes": 120,      # 自动分卷时长（分钟）；0 = 不分卷
    "device_index": None,        # None = 自动跟随系统默认输出
    "follow_default": True,      # 录音中默认输出切换时自动跟随续录
    # 2026-10-25（用户拍板）：有应用在放声就按应用录（进程回环，绕过系统音量）。
    # 设备回环采的是"音量之后"的混音，系统音量低会录出 -40dBFS 的小声文件；
    # 进程回环取音量之前的信号，静音也能录。关掉则回到"录整个系统混音"。
    "record_by_app": True,
    # 录音端自动增益：音量很小（如系统音量 5%）时最多放大 32 倍再存盘
    # （2026-10-26 定档：峰值 0.71 / 上限 32× / 软限幅，实测 RMS -40.7 → -14.0 dBFS）
    "record_auto_gain": True,
    # 人声增强（线性相位 FIR，1kHz 归 0dB 只改音色）：off / mild / strong。
    #   mild   = 2.8kHz +2.1dB、200Hz -2.9dB（自然）
    #   strong = 2.8kHz +3.4dB、200Hz -3.9dB（更清晰，2026-10-26 用户 A/B 后定档）
    # 三档在设置里可选；1kHz 归 0dB 保证不改变整体响度（响度归「录音自动增益」管）。
    "record_voice_boost": "strong",
    "tray": True,                # 关闭窗口时最小化到系统托盘
    # ---- 面板外观 ----
    "panel_opacity": 0.80,       # 整个面板的不透明度（1.0=完全实心）。
                                 # 目标是"能透出桌面/窗口"：0.75~0.85 之间最像毛玻璃。
    # ---- 识别模式（决定"定版"是怎么来的）----
    "asr_mode": "stream",        # dual   = 流式草稿 + SenseVoice 定版重转（更准，但定版会换词 → 看着跳）
                                 # stream = 只用流式结果 + 标点模型（文字与草稿同源，不跳；术语略差）
    # ---- 实时转文字：识别敏感度（VAD）----
    # 这组值是拿真人面试录音实测出来的默认组合，改之前先看设置面板里的说明。
    "vad_threshold": 0.50,       # 0.10~0.90：越低越容易把小声当人声，也越容易收进噪声
    "vad_min_speech": 0.25,      # 0.10~1.00：VAD 内部判定"这一段是语音"的最短时长
    "vad_min_silence": 0.10,     # 0.05~0.50：VAD 内部判定"这一段结束"的静音时长
    "vad_gate_sec": 0.25,        # 0.10~1.00：整段语音少于这个秒数 → 直接当噪声丢掉
    "seg_min_speech": 0.30,      # 0.20~1.00：切段时，一段里至少有这么多秒人声才送识别
    "auto_gain": True,           # 真·小声：峰值低于 0.2 时自动放大到 0.7 再识别
    # ---- 字幕节奏 ----
    "subtitle_flush_sec": 1.2,   # 0.6~3.0：活跃行刷新间隔（越小越跟手、越费 CPU）
    "subtitle_first_sec": 0.8,   # 0.4~2.0：一段刚开口后的首刷间隔
    "subtitle_silence_sec": 0.7, # 0.3~2.0：能量门判定"这句说完了"的静音时长（兜底断句）
    "subtitle_max_seg_sec": 0.0, # 2026-10-23：默认取消时间上限（原 15s）——断句交给
                                 # 句尾静音（endpoint）+ 单段字数兜底；>0 时仍按时间强切。
                                 # 8s 太短：真人连续说话几乎不换气，实测 11.6 分钟录音
                                 # 36% 的段被硬切在句子中间；流式草稿负责实时出字，
                                 # 定版等多久都不影响观感。
    "subtitle_endpoint_sec": 1.0,  # 0.2~1.0：模型级端点检测的句尾静音（Paraformer
                                   # is_endpoint rule1）。2026-10-23 实测修正：0.4s 太
                                   # 敏感——Paraformer 把句中停顿也当句尾，真人语音每
                                   # ~1.8s 切一刀、每行只剩 2~4 字（真值比对膨胀率 7.25）。
                                   # 1.0s 过滤句中停顿、保留自然句间停顿。
    "subtitle_ep_min_chars": 6,    # 1~20：端点触发时的最小字数门槛——不够这么多字就不
                                   # 定版，残句（"嗯""这个"）攒进下一句。
    "subtitle_min_line_chars": 100,  # 0~300：**换行的最小字数**（2026-10-24 用户要求）。
                                   # 一行攒够这么多字才允许换行——端点/能量门/字数兜底
                                   # 在未达门槛时都不放行（只对 stream 模式；dual 模式
                                   # 无流式文本，按音频时长兜底）。0 = 不限（老行为）。
                                   # 停止字幕时会强制把不足门槛的尾段也上屏，不丢字。
    "subtitle_max_chars": 500,   # 200~2000：定版文本达到该字数强制收尾；0 = 不限。
                                 # 放音乐/噪声时能量门永远不触发"静音"，没有这个兜底
                                 # 段落会无限变长（字幕一行几千字卡死窗口）。
    "typewriter_ms": 30,         # 10~80：打字机每拍间隔（毫秒），越小吐字越快
    # ---- 字幕外观 ----
    "subtitle_lines": 3,         # 常驻行数
    "subtitle_lines_expanded": 8,  # 点一下展开到几行
    "subtitle_width": 640,       # 逻辑像素（比主面板 560 更长）
    "subtitle_font_size": 15,
    "subtitle_opacity": 0.80,    # 与主面板一致
    "asr_model_dir": "",         # SenseVoice 模型目录；空 = 复用 AI 面试软件那份
    # ---- GPU 加速（2026-10-24 用户要求③：CUDA 版 sherpa-onnx 上 RTX 4060）----
    # 决策（2026-10-24 用户拍板 + 全量实测背书，详见 AI语音摘要_任务交接.md §9.6）：
    #   摘要走 GPU、字幕走 CPU；以后 assistant 做本地转写也按此口径自行决策。
    # auto  = 探测到 CUDA 运行时（nvidia-*-cu12 pip 包）就用 cuda，否则 cpu；
    # cuda  = 强制 GPU（运行时缺失会静默回退 CPU，见 startup.log 的 provider 行）；
    # cpu   = 强制 CPU。管摘要的 SenseVoice 离线识别（用户摘要对象 ≥30 分钟长语音，
    #        GPU 实测快 5~8 倍且能耗省 3~8 倍；但短句逐条调用 GPU 反而慢 2.6x，
    #        所以这项只服务"整段一次解码"的摘要场景）。
    "asr_provider": "auto",
    # 摘要转写模型（2026-10-26）：paraformer（Paraformer-Large，默认）/ sensevoice（旧模型）
    # 实测 2 倍速素材：paraformer 准确率 76.5% vs sensevoice 21.2%
    "asr_model": "paraformer",
    # 摘要提示词的两个变量（2026-10-26 优化）：留空 = 用 llm.py 里的默认值
    #  （"录音主人本人 / 30 秒抓住要点、找待办与争议"）
    "summary_reader": "",
    "summary_purpose": "",
    # 字幕轨 provider（流式 Paraformer + dual 模式定版用的 SenseVoice 共用）：
    # 2026-10-25 实测——流式上 GPU 慢 2.8x（0.1s 小块 kernel 启动开销）；定版是
    # 2~8s 短音频，GPU 也慢 2.6x（109ms vs 42ms）且多占 300MB（620 vs 317）。
    # 所以两条字幕轨都默认 cpu；摘要的整段解码才用 asr_provider（默认 auto→cuda）。
    "subtitle_provider": "cpu",
    "subtitle_capture_exclude": True,  # 不在录屏/屏幕共享里出现
    "subtitle_x": -1,            # 上次位置（-1 = 贴屏幕底部居中）
    "subtitle_y": -1,
    # ---- 摘要（2026-10-24 用户要求三功能彻底解耦：录音只录音、字幕只字幕、摘要只摘要）----
    # 两个旧开关已删：auto_transcribe / auto_summary。
    # 摘要需要文字时自己做：读本次录音文件 → 本地 SenseVoice 转写 → 落摘要缓存 → 交 LLM。
    "summary_dir": "",            # 空 = 默认 <工具目录>\摘要
    "summary_cache_dir": "",      # 空 = 默认 <工具目录>\摘要\摘要缓存（转写缓存，供复用/翻查）
    "summary_cache_keep_days": 7, # 3~90：摘要缓存保留天数，过期自动清理（用户 Q4=7 天）
    "summary_chunk_chars": 6000,  # 超长转写稿分段摘要的阈值（字）；0 = 不分段
    "summary_segment_minutes": 20,  # 摘要按时段分段（分钟/段）；0 = 按字数分（上一项）
    # ---- LLM 摘要：多 profile（2026-10-23 用户需求：保存多个 LLM 随心切换）----
    # 每个 profile 全套参数独立（地址/Key/模型/温度/长度/超时/Prompt，用户 Q1=全套独立）；
    # llm_active = 当前使用的 profile 名；llm_confirm_shown = {profile名: bool}，
    # 每个 profile 首次摘要时各自确认一次"文本发往哪个 URL"（用户 Q3）。
    # 兼容：load() 会把旧的单组 llm_* 键自动迁移成第一个 profile（见 _migrate_llm）。
    "llm_profiles": [],            # [{name, base_url, api_key, model,
                                   #   temperature, max_tokens, timeout_sec, prompt_template}]
    "llm_active": "",              # 当前使用的 profile 名；空 = profiles[0]
    "llm_confirm_shown": {},       # profile 名 → 是否已弹过发送确认
}


def default_output_dir() -> str:
    return os.path.join(APP_DIR, "录音")


def default_summary_dir() -> str:
    return os.path.join(APP_DIR, "摘要")


def default_summary_cache_dir() -> str:
    return os.path.join(APP_DIR, "摘要", "摘要缓存")


def _migrate_llm(data: dict, cfg: dict) -> None:
    """旧单组 llm_* 配置 → llm_profiles[0]（2026-10-23 多 LLM 管理）。

    老用户升级后零丢失：原来填的 base_url / api_key / model / 温度…整体
    搬进第一个 profile（名字取模型名），llm_active 指向它，隐私确认记录
    也按 profile 名平移。没有旧配置（全新安装）时什么都不做。
    """
    if not isinstance(data, dict):
        return
    if data.get("llm_profiles") or cfg.get("llm_profiles"):
        return                                  # 已是新结构 / 已迁移过
    if not data.get("llm_base_url"):
        return                                  # 全新安装：交给用户自己在设置里配
    model = str(data.get("llm_model") or "").strip()
    prof = {
        "name": model or "默认模型",
        "base_url": str(data.get("llm_base_url") or "").rstrip("/"),
        "api_key": str(data.get("llm_api_key") or ""),
        "model": model,
        "temperature": float(data.get("llm_temperature", 0.3) or 0.3),
        "max_tokens": int(data.get("llm_max_tokens", 2000) or 2000),
        "timeout_sec": float(data.get("llm_timeout_sec", 60) or 60),
        "prompt_template": str(data.get("llm_prompt_template") or ""),
    }
    cfg["llm_profiles"] = [prof]
    cfg["llm_active"] = prof["name"]
    if data.get("llm_confirm_shown"):
        cfg["llm_confirm_shown"] = {prof["name"]: True}


def load() -> dict:
    cfg = dict(DEFAULTS)
    data = {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
    except Exception:  # noqa: BLE001  首次运行没有文件 / 文件损坏 → 用默认值
        pass
    _migrate_llm(data, cfg)
    return cfg


def save(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({k: cfg.get(k, v) for k, v in DEFAULTS.items()},
                      f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001  配置保存失败不致命
        pass
