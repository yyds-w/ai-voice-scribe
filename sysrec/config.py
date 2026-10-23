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
    "subtitle_silence_sec": 0.7, # 0.3~2.0：静音多久算这句说完（定版）
    "subtitle_max_seg_sec": 15.0,  # 3~20：一直说不停时的强制定版上限；**0 = 取消上限**
                                   # （只按停顿断句，内部仍有 60s 安全兜底）。
                                   # 8s 太短：真人连续说话（自我介绍那种）几乎不换气，
                                   # 实测 11.6 分钟录音有 36% 的段是被 8s 硬切在句子中间的；
                                   # 流式草稿已经负责实时出字，定版等久一点不影响观感。
    "typewriter_ms": 30,         # 10~80：打字机每拍间隔（毫秒），越小吐字越快
    # ---- 字幕外观 ----
    "subtitle_lines": 3,         # 常驻行数
    "subtitle_lines_expanded": 8,  # 点一下展开到几行
    "subtitle_width": 640,       # 逻辑像素（比主面板 560 更长）
    "subtitle_font_size": 15,
    "subtitle_opacity": 0.80,    # 与主面板一致
    "asr_model_dir": "",         # SenseVoice 模型目录；空 = 复用 AI 面试软件那份
    "subtitle_capture_exclude": True,  # 不在录屏/屏幕共享里出现
    "subtitle_x": -1,            # 上次位置（-1 = 贴屏幕底部居中）
    "subtitle_y": -1,
    # ---- 自动转写 / 自动摘要（2026-10-20 方案 B）----
    "auto_transcribe": True,      # 录音时后台自动转文字（不弹字幕窗），文稿落盘到 transcript_dir
    "auto_summary": True,         # 停止录音后自动把转写稿交给 LLM 生成摘要
    "transcript_dir": "",         # 空 = 默认 <工具目录>\转写
    "summary_dir": "",            # 空 = 默认 <工具目录>\摘要
    "summary_chunk_chars": 6000,  # 超长转写稿分段摘要的阈值（字）；0 = 不分段
    "summary_segment_minutes": 20,  # 摘要按时段分段（分钟/段）；0 = 按字数分（上一项）
    # ---- LLM 摘要（OpenAI 兼容：DeepSeek / 通义 / 智谱 / Kimi / Ollama 通用）----
    "llm_base_url": "https://api.deepseek.com/v1",
    "llm_api_key": "",            # 存本机 config.json（明文）；也可用环境变量 SYSREC_LLM_KEY 覆盖
    "llm_model": "deepseek-chat",
    "llm_temperature": 0.3,       # 低 = 稳定，适合摘要
    "llm_max_tokens": 2000,       # 摘要长度上限
    "llm_timeout_sec": 60,
    "llm_prompt_template": "",    # 空 = 用 llm.py 内置的默认会议纪要模板
    "llm_confirm_shown": False,   # 隐私确认（文本发往哪个 URL）只问一次
}


def default_output_dir() -> str:
    return os.path.join(APP_DIR, "录音")


def default_transcript_dir() -> str:
    return os.path.join(APP_DIR, "转写")


def default_summary_dir() -> str:
    return os.path.join(APP_DIR, "摘要")


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in DEFAULTS})
    except Exception:  # noqa: BLE001  首次运行没有文件 / 文件损坏 → 用默认值
        pass
    return cfg


def save(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({k: cfg.get(k, v) for k, v in DEFAULTS.items()},
                      f, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001  配置保存失败不致命
        pass
