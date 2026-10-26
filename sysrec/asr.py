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

# ---- Paraformer-Large（2026-10-26 接入）----
# 实测（同素材、非重叠 30s 窗）：2 倍速素材准确率 75.2%（SenseVoice-small 仅 21.2%），
# 正常语速 51.8% vs 48.6%；体积 232MB vs 228MB；CPU 6.6s/6 分钟（比 SenseVoice 还快）。
_PF_DIRNAME = "paraformer-large"
_PF_MODEL = "model.int8.onnx"
_PF_TOKENS = "tokens.txt"

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


#: 合法叠词白名单里出现的字（这些字连续两次**不**合并）。
#  其余字连续重复一律合并——Paraformer int8 有复读副作用（"这这包包""要要算未未"），
#  实测 6 分钟素材 120 处，且 3 次以上的极少（1 处）。
_DUP_KEEP = set("看谢试想说等通慢好常天年人刚问听走坐聊谈查找用做玩碰拍打跳跑歇猜数量尝闻瞧望盯笑哭痛痒暖冷热静轻深紧悄默明偏恰仅统各种件条层步点多早晚高大小")


def _dedup_stutter(text: str) -> str:
    """合并 int8 量化导致的连续重复字（2026-10-26）。

    `这这包包一些` → `这包一些`；`要要算未未七十年` → `要算未七十年`。
    只做同一字符连续出现：2 次且该字不在白名单 → 合并；≥3 次 → 一律合并。
    实测收益：CER 24.8% → 24.2%（+0.6pt），成本几乎为零，故保留。
    """
    if not text:
        return text
    out = []
    i, n = 0, len(text)
    while i < n:
        j = i
        while j + 1 < n and text[j + 1] == text[i]:
            j += 1
        run = j - i + 1
        if run >= 3 or (run == 2 and text[i] not in _DUP_KEEP):
            out.append(text[i])
        else:
            out.append(text[i:j + 1])
        i = j + 1
    return "".join(out)


def _normalize_peak(audio, floor=SO_GAIN_FLOOR, target=SO_GAIN_TARGET):
    """峰值过低时线性放大到 target；正常音量原样返回（不改"有没有语音"）。"""
    try:
        peak = float(np.max(np.abs(audio))) if len(audio) else 0.0
    except Exception:                                      # noqa: BLE001
        return audio
    if not (peak > 1e-6) or peak >= floor:
        return audio
    return (audio * (target / peak)).astype(np.float32)


def _merge_overlap(prev: str, nxt: str) -> str:
    """拼接两个分段文本，去掉重叠区重复（2026-10-24 长音频分段解码）。

    窗间留了 overlap_sec 秒重叠，同一句话会被相邻两窗各识别一次。
    在 prev 尾部找 nxt 开头的最长重复（≥6 字）并去掉；找不到就原样拼接
    （接缝处最多多一两个短语，转写稿可接受）。
    """
    # 2026-10-26：窗口放大到 200 字 + 允许"近似相同"。
    # 原因：15s 小窗（快语速素材）接缝重复可达 60~90 字，且同一段音频在相邻两窗
    # 识别出的文字**不完全一致**（差 1~2 字）→ 老的"尾部 80 字精确匹配"匹配不上，
    # 转写稿就会出现整句重复（用户实测看到"买房子、养孩子…"重复两遍）。
    tail = prev[-200:]
    best = 0
    maxk = min(len(tail), len(nxt))
    for k in range(maxk, 5, -1):                        # ① 精确匹配
        if tail.endswith(nxt[:k]):
            best = k
            break
    if not best:                                        # ② 近似匹配（≥80% 相同）
        for k in range(maxk, 12, -1):
            a, b = tail[-k:], nxt[:k]
            same = sum(1 for x, y in zip(a, b) if x == y)
            if same >= k * 0.8:
                best = k
                break
    return nxt[best:] if best else nxt


def _resample_linear(mono, src_sr, dst_sr):
    """线性插值重采样（与 subtitle._resample 同算法，供子进程复用）。"""
    if src_sr == dst_sr or len(mono) == 0:
        return np.asarray(mono, dtype=np.float32)
    n = int(round(len(mono) * dst_sr / src_sr))
    x_old = np.linspace(0, len(mono) - 1, len(mono))
    x_new = np.linspace(0, len(mono) - 1, n)
    return np.interp(x_new, x_old, mono).astype(np.float32)


def read_audio_files(files):
    """读音频文件（wav/mp3/flac…，soundfile 支持即可）→ (16k mono float32, 秒)。

    2026-10-25 从 gui.py 挪到这里：摘要转写已改到子进程（asr_worker），
    子进程不能 import gui（会拖进 Qt），所以音频读取要放在与 GUI 无关的层。
    分卷按顺序拼接；单个文件读不了就跳过；全读不了返回 (None, 0)。
    """
    import soundfile as sf
    chunks, total = [], 0.0
    for p in files or []:
        try:
            data, sr = sf.read(p, dtype="float32", always_2d=False)
        except Exception:                        # noqa: BLE001  读不了就跳过
            continue
        if data.ndim > 1:
            data = data.mean(axis=1)
        if int(sr) != SR:
            data = _resample_linear(np.asarray(data, dtype=np.float32), int(sr), SR)
        chunks.append(np.ascontiguousarray(data, dtype=np.float32))
        total += len(data) / float(SR)
    if not chunks:
        return None, 0.0
    return np.concatenate(chunks), total


def probe_audio_seconds(files):
    """只读元信息估总时长（不整段解码，用于进度文案）。"""
    import soundfile as sf
    total = 0.0
    for p in files or []:
        try:
            total += float(sf.info(p).duration)
        except Exception:                        # noqa: BLE001
            continue
    return total


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


def _log_provider(provider: str, pref: str) -> None:
    """把 provider 决策写进 startup.log（best-effort，失败静默）。

    为什么需要：sherpa-onnx 在 CUDA 不可用时会**静默回退 CPU**（不抛异常），
    没有这条日志就无法分辨"真用了 GPU"和"回退了 CPU"。
    """
    try:
        import datetime
        log = os.path.join(app_paths.data_path(), "startup.log")
        with open(log, "a", encoding="utf-8") as f:
            f.write("[%s] SenseVoice provider=%s（配置 asr_provider=%s）\n"
                    % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       provider, pref))
    except Exception:                                  # noqa: BLE001
        pass


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
                 gate_sec=0.25, auto_gain=True, provider="auto",
                 model_type="paraformer"):
        self.model_dir = os.path.expandvars(model_dir or DEFAULT_MODEL_DIR)
        # 模型类型：paraformer（默认，2026-10-26 换成 Large）/ sensevoice（旧路径，可回退）
        self.model_type = ("sensevoice" if str(model_type or "").strip().lower()
                           == "sensevoice" else "paraformer")
        self.language = language or "zh"
        self._threads = int(threads or 0)
        self.vad_threshold = max(0.05, min(0.95, float(vad_threshold)))
        self.vad_min_speech = max(0.05, min(2.0, float(vad_min_speech)))
        self.vad_min_silence = max(0.05, min(1.0, float(vad_min_silence)))
        self.gate_sec = max(0.05, min(2.0, float(gate_sec)))
        self.auto_gain = bool(auto_gain)
        self.provider_pref = (provider or "auto").strip().lower()
        self.provider = "cpu"            # _ensure() 时定稿；True=cuda 才算用上 GPU
        self._rec = None
        self._vad_cfg = None
        self._vad_tried = False
        self._fix = load_fix_terms(terms_dir)
        self._lock = threading.Lock()

    # ---------- 就绪检查 ----------
    def model_hint(self):
        # Paraformer 目录不存在时回落到 SenseVoice（保证换了配置也不会启动失败）
        if self.model_type == "paraformer":
            d = os.path.join(self.model_dir, _PF_DIRNAME)
            if os.path.exists(os.path.join(d, _PF_MODEL)):
                return d
        return os.path.join(self.model_dir, _SO_DIRNAME)

    def ready(self):
        d = self.model_hint()
        name = _PF_MODEL if self.model_type == "paraformer" and os.path.basename(d) == _PF_DIRNAME else _SO_MODEL
        tok = _PF_TOKENS if name == _PF_MODEL else _SO_TOKENS
        return (os.path.exists(os.path.join(d, name))
                and os.path.exists(os.path.join(d, tok)))

    # ---------- 懒加载 ----------
    def _ensure(self):
        if self._rec is not None:
            return self._rec
        from . import gpu
        gpu.ensure_cuda_dlls()               # CUDA provider DLL 的前置依赖（幂等）
        self.provider = gpu.resolve_provider(self.provider_pref)
        if self.model_type == "paraformer" and self.provider == "cuda":
            # 2026-10-26 实测：Paraformer 在 CUDA 上**慢 33 倍**
            # （同一 6 分钟音频：CPU 4.8s / CUDA 157.9s）。ONNX CUDA provider
            # 对它的部分算子不支持，逐算子回退 CPU 的代价极高。
            # 所以 Paraformer 固定走 CPU（CPU 已经 74× 实时，够快）。
            self.provider = "cpu"
            try:
                import sys as _sys
                print("[asr] Paraformer 固定 CPU（CUDA 下慢 33 倍，实测）", file=_sys.stderr)
            except Exception:                  # noqa: BLE001
                pass
        _log_provider(self.provider, self.provider_pref)
        import sherpa_onnx
        d = self.model_hint()
        if not self.ready():
            raise RuntimeError("找不到 ASR 模型：%s" % d)
        threads = self._threads or min(8, max(1, (os.cpu_count() or 4) - 1))
        if self.model_type == "paraformer" and os.path.basename(d) == _PF_DIRNAME:
            # Paraformer-Large：非流式，**必须分窗**（整段喂进去会内容错乱，见 9.32）
            self._rec = sherpa_onnx.OfflineRecognizer.from_paraformer(
                paraformer=os.path.join(d, _PF_MODEL),
                tokens=os.path.join(d, _PF_TOKENS),
                num_threads=threads,
                sample_rate=SR,
                feature_dim=80,
                decoding_method="greedy_search",
                provider=self.provider,
                debug=False,
            )
        else:
            self.model_type = "sensevoice"
            self._rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=os.path.join(d, _SO_MODEL),
                tokens=os.path.join(d, _SO_TOKENS),
                num_threads=threads,
                use_itn=True,
                language=self.language,
                provider=self.provider,
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

    def transcribe(self, audio16k, skip_vad=False, max_window_sec=30,
                   overlap_sec=5):
        """返回文字；没人说话 / 纯噪声 / 空结果 → ""。

        skip_vad=True：跳过 VAD 闸门（**预览专用**）。
        【为什么预览必须跳过】预览每秒都要跑一次，而 VAD 闸门要把整段音频
        逐 512 样本过一遍 silero（实测占单次识别耗时的大头）→ 白等。
        预览是"边说边刷"的草稿，噪声由外部能量门（RMS_GATE）先挡过一道，
        不需要再逐段做静音判定；**定版**（final）才跑 VAD 做反幻觉过滤。

        max_window_sec / overlap_sec（2026-10-25 定为 30s；2026-10-26 重叠 3s → **5s**）：
        SenseVoice 是**固定窗**模型，超过它熟悉的长度会**安静地丢内容**
        （不报错、不出乱码，只是字变少）。同一段 5.95 分钟音频实测：
          300s 窗 → 8 字（+1244MB 显存）｜60s → 251 字｜**30s → 1218 字**（+0MB）
        重叠（2026-10-26 实测）：3s → 1218 字｜**5s → 1327 字**（+9%，接缝丢字更少）
          8s → 1326 字（不再增加，白花时间）→ 定 5s。
        注意：窗再小（25s/20s）字数会暴涨到 2153/3058，但那是**重复没去干净**
          （8.5 字/秒远超正常语速），不是更准——别被字数骗了。
        基准语料 623s：300s 窗 2410 字 ｜ 60s 2737 ｜ **30s 2832**（小窗反而更全）
        速度：30s 窗 8.2s vs 300s 窗 9.4s（小窗走"快档"，不吃亏）
        所以默认 30s 窗：质量最好、显存几乎为零、速度不降。
        另：300s 窗还会因注意力中间缓冲过大把 8GB 显卡撑爆（见 9.6.1）。
        """
        if audio16k is None or len(audio16k) == 0:
            return ""
        # 2026-10-26：Paraformer **不要重叠**。它整段吞吐完整（2 倍速素材 3888 字 vs
        # 参考 3931），重叠 5s 只会让接缝内容重复、去重去不干净：
        #   非重叠 → CER 24.2%（准确率 75.8%）｜重叠 5s + 现有去重 → CER 41.5%（58.5%）
        # SenseVoice 才需要重叠（它会吞字，靠重叠补内容）。
        if self.model_type == "paraformer":
            overlap_sec = 0
        total_sec = len(audio16k) / SR
        if total_sec <= max_window_sec:
            text = self._transcribe_one(audio16k, skip_vad=skip_vad)
        else:
            text = self._decode_windows(audio16k, max_window_sec, overlap_sec, skip_vad)
        # ---- 2026-10-26：按"字率"自适应窗长（重要）----
        # 背景：窗长应按**原始语音时长**定，而不是音频时长。2 倍速素材 30s 音频
        #   里塞了 60s 语音 → 超出模型能力 → 疯狂吞字。同一段 6 分钟 2 倍速素材
        #   （云端参考 3931 字）实测 CER：
        #     30s 窗 1273 字 → CER 72.3%（准确率 27.7%）
        #     20s 窗 2878 字 → CER 49.7%（50.3%）
        #   **15s 窗 3985 字 → CER 39.2%（60.8%）** ← 最优
        #     12s 窗 4691 字 → CER 45.0%（又开始重复/错乱）
        # 但**正常语速**下三者基本持平（45.7%~46.7%，30s 略好 1 个点）。
        # 所以用字率判断：正常语速 ≈ 4~5 字/秒；快语速素材在 30s 窗下会明显低于
        # 4.0（因为丢字了）→ 触发 15s 窗重解，取更长的结果。
        # 误触发的代价只是多解码一次（结果不会更差，因为取更长的）。
        if total_sec >= 60 and self.model_type != "paraformer":
            # 仅 SenseVoice 需要这条兜底（快语速会吞字）；Paraformer 吞吐完整，
            # 小窗重解只会引入重复（实测 15s 窗字数虚高、CER 反而变差）。
            rate = len(text) / total_sec
            if rate < 4.0:
                alt = self._decode_windows(audio16k, 15, 3, skip_vad)
                if len(alt) > len(text):
                    return alt
        return text

    def _decode_windows(self, audio16k, win_s, overlap_s, skip_vad):
        """按固定窗长切分解码并合并（重叠区去重）。"""
        win = int(win_s * SR)
        step = max(1, int((win_s - overlap_s) * SR))
        parts = []
        for start in range(0, len(audio16k), step):
            seg = np.ascontiguousarray(audio16k[start:start + win])
            if len(seg) < int(self.gate_sec * SR):
                break                                   # 尾巴不足闸门长度，弃
            txt = self._transcribe_one(seg, skip_vad=skip_vad)
            if not txt:
                continue                               # 该窗纯噪声/幻觉 → 跳过
            if parts:
                txt = _merge_overlap(parts[-1], txt)
            parts.append(txt)
        return "".join(parts).strip()

    def _transcribe_one(self, audio16k, skip_vad=False):
        """单段（≤ max_window_sec）解码：VAD 闸门 / 峰值归一化 / 反幻觉 / 术语纠错。"""
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
        if self.model_type == "paraformer":
            txt = _dedup_stutter(txt)      # int8 复读合并（见 _dedup_stutter）
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
