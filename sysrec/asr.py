# -*- coding: utf-8 -*-
"""SenseVoice 转写（录音机版，精简）：一段 16k 单声道音频 → 文字。

设计取舍：
  · 识别 / VAD 闸门 / 峰值归一化的参数与「AI 面试软件」的 stt.py **保持一致**
    （那套参数是拿真人面试录音实测出来的，不在这里重新发明）；
  · 技术词纠错直接动态导入那边的 fix_terms（成功就用、失败就跳过）——
    那张表有几百条，两边各维护一份必然走样；
  · 不做 whisper / 云端 API 分支：录音机只需要 SenseVoice 这一条路。
"""
from __future__ import annotations

import os
import re
import sys
import threading

import numpy as np

from . import app_paths

SR = 16000
_SO_DIRNAME = "sense-voice-small"
_SO_MODEL = "model.int8.onnx"
_SO_TOKENS = "tokens.txt"
_VAD_DIRNAME = "vad"
_VAD_FILE = "silero_vad.onnx"
_VAD_WINDOW = 512
_VAD_THRESHOLD = 0.5
_VAD_MIN_SPEECH = 0.25
_VAD_MIN_SILENCE = 0.1

SO_GAIN_FLOOR = 0.2
SO_GAIN_TARGET = 0.7
MIN_SPEECH_SEC = 0.25          # VAD 判定的语音时长低于这个 → 当噪声丢掉

# 模型路径：优先本工具自带（AI语音摘要\models），没有才回退到面试软件那份。
# 【为什么】2026-10-18 起录音机自带一份模型（models/ 下已拷好），
# 不再依赖另一个项目的目录——那边挪位置/重装都不会影响字幕。
_APP_MODELS = app_paths.resource_path("models")
DEFAULT_MODEL_DIR = (_APP_MODELS if os.path.isdir(os.path.join(_APP_MODELS, _SO_DIRNAME))
                     else r"D:\Program Files\AI面试软件\interview_assistant\models")
DEFAULT_TERMS_DIR = r"D:\Program Files\AI面试软件\interview_assistant"

_PUNCT_RE = re.compile(r"[\s，。、！？,.!?：:；;~～\-—_…“”\"'（）()\[\]【】]+")
_BOILER_RE = re.compile(r"字幕|订阅|点赞|感谢观看|下期再见|仅供学习|版权归")


def _normalize_peak(audio, floor=SO_GAIN_FLOOR, target=SO_GAIN_TARGET):
    """峰值过低时线性放大到 target；正常音量原样返回（不改"有没有语音"）。"""
    try:
        peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    except Exception:                                      # noqa: BLE001
        return audio
    if not (peak > 1e-6) or peak >= floor:
        return audio
    return (audio * (target / peak)).astype(np.float32)


def _text_ok(text):
    """文本级反幻觉：太短 / 单字复读 / 字幕口水句 → 丢掉。"""
    core = _PUNCT_RE.sub("", text or "")
    if len(core) < 2:
        return False
    if len(core) <= 12 and re.search(r"(.)\1{2,}", core):
        return False
    return not _BOILER_RE.search(text or "")


def load_fix_terms(terms_dir=None):
    """动态拿面试软件的 fix_terms（技术词纠错）。拿不到就返回恒等函数。"""
    def _id(s):
        return s
    d = terms_dir or DEFAULT_TERMS_DIR
    try:
        if d and os.path.isdir(d) and d not in sys.path:
            sys.path.insert(0, d)
        from stt import fix_terms          # noqa: PLC0415  延迟导入，失败不影响字幕
        return fix_terms
    except Exception:                       # noqa: BLE001
        return _id


class Asr:
    """SenseVoice 单实例包装。transcribe 内部串行（同一 session 不并发解码）。

    VAD 四个参数由外部传入（设置面板可调）：
      vad_threshold   0.10~0.90  越低越容易把小声当人声，也越容易收进噪声
      vad_min_speech  0.10~1.00  VAD 内部判定"这一段是语音"的最短时长
      vad_min_silence 0.05~0.50  VAD 内部判定"这一段结束"的静音时长
      gate_sec        0.10~1.00  整段语音少于这个秒数 → 直接当噪声丢掉
    """

    def __init__(self, model_dir=None, threads=0, terms_dir=None, language="zh",
                 vad_threshold=0.5, vad_min_speech=0.25, vad_min_silence=0.1,
                 gate_sec=0.25, auto_gain=True):
        self.model_dir = os.path.expandvars(model_dir or DEFAULT_MODEL_DIR)
        self.language = language or "zh"
        self._threads = int(threads or 0)
        self.vad_threshold = max(0.05, min(0.95, float(vad_threshold)))
        self.vad_min_speech = max(0.05, min(2.0, float(vad_min_speech)))
        self.vad_min_silence = max(0.05, min(1.0, float(vad_min_silence)))
        self.gate_sec = max(0.05, min(2.0, float(gate_sec)))
        self.auto_gain = bool(auto_gain)
        self._rec = None
        self._vad_cfg = None
        self._vad_tried = False
        self._fix = load_fix_terms(terms_dir)
        self._lock = threading.Lock()

    # ---------- 就绪检查 ----------
    def model_hint(self):
        return os.path.join(self.model_dir, _SO_DIRNAME)

    def ready(self):
        d = self.model_hint()
        return (os.path.exists(os.path.join(d, _SO_MODEL))
                and os.path.exists(os.path.join(d, _SO_TOKENS)))

    # ---------- 懒加载 ----------
    def _ensure(self):
        if self._rec is not None:
            return self._rec
        import sherpa_onnx
        d = self.model_hint()
        if not self.ready():
            raise RuntimeError("找不到 SenseVoice 模型：%s" % d)
        self._rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=os.path.join(d, _SO_MODEL),
            tokens=os.path.join(d, _SO_TOKENS),
            num_threads=self._threads or min(8, max(1, (os.cpu_count() or 4) - 1)),
            use_itn=True,
            language=self.language,
        )
        return self._rec

    def _vad(self):
        if self._vad_tried:
            return self._vad_cfg
        self._vad_tried = True
        try:
            import sherpa_onnx
            p = os.path.join(self.model_dir, _VAD_DIRNAME, _VAD_FILE)
            if not os.path.exists(p):
                return None
            c = sherpa_onnx.VadModelConfig()
            c.silero_vad.model = p
            c.silero_vad.threshold = self.vad_threshold
            c.silero_vad.min_speech_duration = self.vad_min_speech
            c.silero_vad.min_silence_duration = self.vad_min_silence
            c.sample_rate = SR
            self._vad_cfg = c
        except Exception:                                  # noqa: BLE001
            self._vad_cfg = None
        return self._vad_cfg

    def warmup(self):
        with self._lock:
            self._ensure()
        self._vad()                    # 顺手把 VAD 也加载好

    # ---------- 识别 ----------
    def _vad_speech_sec(self, audio):
        """VAD 判这段里有几秒像人说话；不可用/异常 → None（= 不拦）。"""
        cfg = self._vad()
        if cfg is None:
            return None
        try:
            import sherpa_onnx
            a = np.ascontiguousarray(audio, dtype=np.float32)
            secs = max(60, int(len(a) / SR) + 10)
            vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=secs)
            for i in range(0, len(a), _VAD_WINDOW):
                vad.accept_waveform(a[i:i + _VAD_WINDOW])
            vad.flush()
            total = 0
            while not vad.empty():
                total += len(vad.front.samples)
                vad.pop()
            return total / float(SR)
        except Exception:                                  # noqa: BLE001
            return None

    def transcribe(self, audio16k, skip_vad=False):
        """返回文字；没人说话 / 纯噪声 / 空结果 → ""。

        skip_vad=True：跳过 VAD 闸门（**预览专用**）。
        【为什么预览必须跳过】预览每秒都要跑一次，而 VAD 闸门要把整段音频
        逐 512 样本过一遍 silero（实测占单次识别耗时的大头）→ 白等。
        预览是"边说边刷"的草稿，噪声由外部能量门（RMS_GATE）先挡过一道，
        不需要再逐段做静音判定；**定版**（final）才跑 VAD 做反幻觉过滤。
        """
        if audio16k is None or len(audio16k) == 0:
            return ""
        with self._lock:
            rec = self._ensure()
            a = np.ascontiguousarray(audio16k, dtype=np.float32)
            if not skip_vad:
                sec = self._vad_speech_sec(a)
                if sec is not None and sec < self.gate_sec:
                    return ""
            if self.auto_gain:
                a = _normalize_peak(a)
            stream = rec.create_stream()
            stream.accept_waveform(SR, a)
            rec.decode_stream(stream)
            txt = (stream.result.text or "").strip()
        if not _text_ok(txt):
            return ""
        try:
            return self._fix(txt)
        except Exception:                                  # noqa: BLE001
            return txt

    def fix(self, text):
        """只做术语纠错、不跑识别 —— 单模型模式给流式草稿用。"""
        try:
            return self._fix(text or "")
        except Exception:                                  # noqa: BLE001
            return text
