# -*- coding: utf-8 -*-
"""实时转文字引擎：抓系统播放音 → 切段 → SenseVoice → 字幕行。

和「录音」完全解耦：**不建文件、不计时、不落盘**，只出字。
两条轨（与面试软件同一套实测口径）：
  · 活跃行：说不停时每 flush_sec 刷一次预览（灰字，会变）
  · 定版行：静音 silence_sec 或累计 max_seg_sec 时整段定版（永不回改）

为什么不用"2.5s 硬切"：实测 40s 连贯回答会被切成 11 段碎片，读不了；
拆成两轨后同样的音频是 4 条完整句子 + 15 次活跃行刷新。
"""
from __future__ import annotations

import threading
import time
from collections import deque

import numpy as np
from PyQt6.QtCore import QObject, pyqtSignal

from . import devices
from .asr import Asr
from .capture import make_capture

SR = 16000
RMS_GATE = 0.008        # 绝对下限：全静音时别把门槛压到 0
# 【自适应噪声门】固定门槛 0.008 在真实录音里必然失效：底噪常年 0.05~0.15，
# 于是"静音"永远不成立 → 只能靠 8s 上限每 8 秒硬切一刀
# （2026-10-18 实测 11.6 分钟真人录音：86/86 段全是 8.02s，静音收尾 0 次触发）。
NOISE_WIN = 60          # 60 块 × 85ms ≈ 5 秒的 RMS 历史
NOISE_PCT = 20          # 取 20% 分位 ≈ 底噪水平
NOISE_RATIO = 1.8       # 高于底噪 1.8 倍才算"有人在说话"


def _resample(mono, src_sr, dst_sr):
    if src_sr == dst_sr or len(mono) == 0:
        return mono.astype(np.float32)
    n = int(round(len(mono) * dst_sr / src_sr))
    x_old = np.linspace(0, len(mono) - 1, len(mono))
    x_new = np.linspace(0, len(mono) - 1, n)
    return np.interp(x_new, x_old, mono).astype(np.float32)


class SubtitleEngine(QObject):
    line = pyqtSignal(str)         # 定版一行（上屏正文，永不回改）
    partial = pyqtSignal(str)      # 活跃行（正在说，会变）
    status = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, device_index=None, cfg=None, parent=None, process_pid: int = 0):
        super().__init__(parent)
        c = dict(cfg or {})
        self.device_index = device_index
        self.process_pid = int(process_pid or 0)   # >0 = 按进程捕获（无视系统静音）
        # 2026-10-25：字幕定版用的 SenseVoice 跟流式模型同口径（默认 CPU）。
        # 定版是一句一句的短音频（2~8s），实测短句 GPU 反而慢 2.6 倍
        # （109ms vs 42ms，每次 decode 的固定开销 ~67ms），内存还多 300MB
        # （cuda 620MB vs cpu 317MB）。要用 GPU 把 subtitle_provider 改 "cuda"。
        self._asr = Asr(
            model_dir=(c.get("asr_model_dir") or None),
            vad_threshold=float(c.get("vad_threshold", 0.50)),
            vad_min_speech=float(c.get("vad_min_speech", 0.25)),
            vad_min_silence=float(c.get("vad_min_silence", 0.10)),
            gate_sec=float(c.get("vad_gate_sec", 0.25)),
            auto_gain=bool(c.get("auto_gain", True)),
            provider=c.get("subtitle_provider", "cpu"),     # 2026-10-25 GPU 加速
        )
        # 字幕草稿轨（流式）默认 CPU：2026-10-24 实测流式上 GPU 慢 3 倍
        self._stream_provider = c.get("subtitle_provider", "cpu")
        self._cap = None
        self._running = False
        self._lock = threading.Lock()
        self._buf = []
        self._n = 0
        self._speech = 0
        self._silence = 0
        self._last_preview = 0
        self._seq = 0
        self._busy = False
        self._src_sr = 48000
        self._err_reported = False
        self._cost = 0.0                # 上次识别耗时（自适应降频用）
        self._rms_hist = deque(maxlen=NOISE_WIN)   # 最近 5 秒的 RMS，估底噪用
        # 流式识别（边说边出字）：草稿的主来源；不可用时自动回退"整段重识别"老路径
        self._stream_asr = None
        self._stream_tried = False
        self._stream_sess = None
        self._stream_text = ""
        # 识别模式：
        #   dual   = 流式出草稿 + 定版用 SenseVoice 整段重转（更准，但定版文字会和草稿不同 → 视觉跳字）
        #   stream = 只用流式结果，定版只补标点（文字与草稿完全一致，不跳；术语精度略低）
        self.mode = str(c.get("asr_mode") or "stream").strip().lower()
        if self.mode not in ("dual", "stream"):
            self.mode = "stream"
        self._punct = None
        self._punct_tried = False
        # 【2026-10-18 调整】flush_sec 1.2→0.8s、降频系数 3.0→1.6：
        #   用户反馈"每 2.5 秒才吐一段"。实测单次预览耗时里 VAD 全段扫描占大头，
        #   预览现在跳过 VAD（见 _work），单次降到 ~0.2s，节奏可以放快一倍。
        self.flush_sec = float(c.get("subtitle_flush_sec", 0.8))
        self.first_sec = float(c.get("subtitle_first_sec", 0.6))
        self.silence_sec = float(c.get("subtitle_silence_sec", 0.7))
        self.min_speech_sec = float(c.get("seg_min_speech", 0.30))
        self.max_seg_sec = float(c.get("subtitle_max_seg_sec", 0.0))
        # 2026-10-23 断句三层（用户 Q6=B/Q10 + Q4/Q5=500 字兜底）：
        #   1) 模型级端点：Paraformer is_endpoint（rule1 句尾静音 endpoint_sec）
        #      ——替代原来的 15s 硬切；
        #   2) 能量门兜底：静音 silence_sec（模型端点漏判时接着兜）；
        #   3) 字数兜底：定版文本达 max_chars 强切——放音乐/噪声时能量门永远
        #      不触发"静音"，没有它段落会无限变长（字幕一行几千字卡死窗口）。
        # max_seg_sec > 0 时仍保留按时间强切（默认已取消：0）。
        # ep_min_chars：端点触发时的最小字数门槛——2026-10-23 实测 Paraformer
        # rule1=0.4s 把句中停顿当句尾（每 ~1.8s 一刀、每行 2~4 字），除了把
        # rule1 放宽到 1.0s，再加这道门槛挡掉识别初期的不稳定残句。
        self.endpoint_sec = float(c.get("subtitle_endpoint_sec", 1.0))
        self.ep_min_chars = int(c.get("subtitle_ep_min_chars", 6) or 0)
        # 换行最小字数（2026-10-24 用户要求：一行至少攒够 N 字才换行）
        self.min_line_chars = int(c.get("subtitle_min_line_chars", 100) or 0)
        self.max_chars = int(c.get("subtitle_max_chars", 500) or 0)
        self._ep_hit = False              # 流式块里查到 endpoint → 锁内立即定版

    @property
    def running(self):
        return self._running

    # ---------- 开关 ----------
    def start(self):
        if self._running:
            return True
        idx = self.device_index
        if idx is None:
            idx, _name = devices.default_loopback_device()
        if idx is None:
            self.failed.emit("找不到可用的回环录音设备（不是扬声器回环）。")
            return False
        # 2026-10-25：stream 模式不需要 SenseVoice（定版不重转），别再因此拒绝启动
        if self.mode != "stream" and not self._asr.ready():
            self.failed.emit(
                "找不到 SenseVoice 模型：\n%s\n\n"
                "模型在「AI面试软件\\interview_assistant\\models」下；"
                "如果那边挪过位置，把 config.json 的 asr_model_dir 改成新路径即可。"
                % self._asr.model_hint())
            return False
        try:
            self._cap = make_capture(int(idx), self._on_audio,
                                     on_error=self._on_cap_error,
                                     process_pid=self.process_pid)
            self._cap.start()
        except Exception as e:                      # noqa: BLE001
            self._cap = None
            self.failed.emit("打开系统声音失败：%s" % e)
            return False
        self._src_sr = int(self._cap.sample_rate)
        self._running = True
        self.status.emit("实时转文字已开启（只听系统声音）")
        threading.Thread(target=self._warm, daemon=True).start()
        return True

    def stop(self):
        self._running = False
        if self._cap is not None:
            try:
                self._cap.stop()
            except Exception:                       # noqa: BLE001
                pass
            self._cap = None
        with self._lock:
            self._buf = []
            self._n = 0
            self._speech = 0
            self._silence = 0
            self._last_preview = 0
            self._seq += 1
            self._busy = False
        # 2026-10-24：成行有最小字数门槛后，停止时必须把不足门槛的尾段也上屏，
        # 否则最后一句（往往是最重要的一句）会被清掉。
        tail = (self._stream_text or "").strip()
        self._stream_sess = None
        self._stream_text = ""
        if tail:
            try:
                self._finalize_stream(tail, force=True)
            except Exception:                       # noqa: BLE001
                pass
        self.status.emit("实时转文字已关闭")

    # ---------- 内部 ----------
    def _warm(self):
        try:
            # 2026-10-25：stream 模式不加载 SenseVoice——定版只用流式文本 + 标点模型
            # + 术语纠错（_finalize_stream），SenseVoice 只服务 dual 模式的"定版重转"。
            # 原来无差别 warmup：stream 模式下白吃 ~640MB（cuda 版还要建 CUDA 上下文），
            # 这是"字幕一开就占九百多兆"的主因（Paraformer 只要 227MB）。
            if self.mode != "stream":
                self._asr.warmup()
            self._ensure_stream()          # 流式模型（字幕主力）
            self._ensure_punct()           # 单模型模式：标点模型（语义断句）
            if self._running:
                self.status.emit("识别模型已就绪，说话就出字")
        except Exception as e:                      # noqa: BLE001
            if self._running:
                self.failed.emit("识别模型加载失败：%s" % e)

    def _ensure_stream(self):
        """懒加载流式识别器；模型缺失/加载失败 → 返回 None（回退整段重识别）。"""
        if self._stream_tried:
            return self._stream_asr
        self._stream_tried = True
        try:
            from .stream_asr import StreamingASR
            m = StreamingASR(threads=2,
                             endpoint_rule1_silence=self.endpoint_sec,
                             endpoint_max_utterance=60.0,
                             provider=self._stream_provider)   # 2026-10-24 GPU 加速（默认 CPU）
            if m.ready():
                m.ensure()
                self._stream_asr = m
                self.status.emit("流式识别就绪（边说边出字）")
        except Exception as e:                        # noqa: BLE001
            self._stream_asr = None
            try:
                self.status.emit("流式模型不可用（回退整段识别）：%s" % str(e)[:100])
            except Exception:                         # noqa: BLE001
                pass
        return self._stream_asr

    def _ensure_punct(self):
        """标点模型（只在单模型模式用）。不可用就返回 None —— 字幕照出，只是没标点。"""
        if self.mode != "stream":
            return None
        if self._punct_tried:
            return self._punct
        self._punct_tried = True
        try:
            from .punctuate import Punctuator
            p = Punctuator()
            if not p.ready():
                self._punct = None
                self.status.emit("标点模型缺失（字幕仍会出，只是没有标点）：%s" % p.model_path)
                return None
            p.ensure()
            self._punct = p
            try:
                self.status.emit("标点模型已就绪")
            except Exception:                         # noqa: BLE001
                pass
        except Exception as e:                        # noqa: BLE001
            self._punct = None
            try:
                self.status.emit("标点模型加载失败（字幕仍会出）：%s" % str(e)[:100])
            except Exception:                         # noqa: BLE001
                pass
        return self._punct

    def _punct_confirms(self, text: str):
        """标点模型语义句界确认（2026-10-23）。

        返回：加标点后的文本（末字符是句末标点 → 这句语义上结束了，可定版）；
        返回 None（末字符是句中标点/无标点 → 句中停顿，继续攒）。
        标点模型不可用 → 返回 None 由调用方降级（老逻辑放过）。
        """
        t = (text or "").strip()
        if not t:
            return None
        p = self._ensure_punct()
        if p is None:
            return None
        try:
            marked = (p.add(t) or "").strip()
        except Exception:  # noqa: BLE001  标点模型出错 → 不确认（降级老逻辑）
            return None
        if not marked:
            return None
        return marked if marked[-1] in "。！？；!?;" else None

    def _finalize_stream(self, text, already_punct=False, force=False):
        """单模型模式的「定版」：**不重转**，只给同一段流式文本补标点后上屏。

        这是"不跳字"的关键：定版内容与草稿同源，用户只会看到多了几个标点，
        不会出现"草稿写 A、定版变成 B"的跳变。
        already_punct=True：文本已由 _punct_confirms 加过标点，跳过重复推理。
        force=True：忽略 running 检查（停止字幕时把尾段补上屏，2026-10-24）。
        """
        t = (text or "").strip()
        if not t:
            return
        try:
            t = self._asr.fix(t)              # 术语纠错（Redis / String 之类）
        except Exception:                     # noqa: BLE001
            pass
        if not already_punct:
            p = self._ensure_punct()
            if p is not None:
                t = p.add(t)
        if t and (self._running or force):
            self.line.emit(t)

    def _on_cap_error(self, why):
        if self._running and not self._err_reported:
            self._err_reported = True
            self.failed.emit(why)

    def _on_audio(self, arr):
        """采集回调线程：48k 多声道 → 16k 单声道 → 独立缓冲 → 双轨切段。"""
        if not self._running:
            return
        try:
            mono = arr.mean(axis=1) if arr.ndim > 1 else arr
            mono = _resample(mono, self._src_sr, SR)
        except Exception:                           # noqa: BLE001
            return
        if not len(mono):
            return
        rms = float(np.sqrt(np.mean(mono ** 2)))
        # —— 流式草稿：边说边出字（每块增量解码，前缀稳定；这是"首次就准"的关键）——
        st = self._ensure_stream()
        if st is not None:
            try:
                if self._stream_sess is None:
                    self._stream_sess = st.new_session()
                    self._stream_text = ""
                st.feed(self._stream_sess, mono)
                r = st.result(self._stream_sess)
                if r and r != self._stream_text:
                    self._stream_text = r
                    self.partial.emit(r)
                # 模型级端点（2026-10-23）：这句说完了（rule1 句尾静音）→
                # 标记，下面进锁后立即定版。比能量门准：判定依据是识别器
                # 内部帧级声学活动，短促键盘声不会误触。
                if st.is_endpoint(self._stream_sess):
                    self._ep_hit = True
            except Exception as e:                    # noqa: BLE001
                self._stream_asr = None               # 出问题 → 本次起回退老路径
                self._stream_sess = None
                self._stream_text = ""
                try:
                    self.status.emit("流式识别异常，已回退整段识别：%s" % str(e)[:100])
                except Exception:                     # noqa: BLE001
                    pass
        with self._lock:
            self._buf.append(mono)
            self._n += len(mono)
            self._rms_hist.append(rms)
            # 【自适应噪声门】原来固定 0.008：真实录音底噪 0.05~0.15，
            # 于是"静音"永远不成立 → 只能靠 8s 上限每 8 秒硬切一刀。
            # 现在用最近 5 秒 RMS 的 20% 分位估底噪，比底噪高 1.8 倍才算有人在说。
            if len(self._rms_hist) >= 12:
                floor = float(np.percentile(self._rms_hist, NOISE_PCT))
            else:
                floor = 0.0
            gate = max(RMS_GATE, floor * NOISE_RATIO)
            if rms > gate:
                self._speech += len(mono)
                self._silence = 0
            else:
                self._silence += len(mono)
            if self._busy:
                return                              # 上一次识别还没回来：只攒不切
            ep = self._ep_hit                       # 模型端点：一句说完
            self._ep_hit = False
            # 端点 + 最小字数门槛：残句（"嗯""这个"）不够 ep_min_chars 字就不定版，
            # 攒进下一句（is_endpoint 会持续为真直到新语音进来，攒够自然定版）
            if ep and self.ep_min_chars > 0:
                if len(self._stream_text or "") < self.ep_min_chars:
                    ep = False
            said_done = (self._speech >= int(self.min_speech_sec * SR)
                         and self._silence >= int(self.silence_sec * SR))
            # 时间上限：max_seg_sec > 0 才按时间强切（默认 0 = 取消，2026-10-23）
            hard = (self.max_seg_sec > 0
                    and self._n >= int(self.max_seg_sec * SR))
            # 字数兜底：定版文本达 max_chars 强切（放音乐时能量门永不触发静音，
            # 没有它段落会无限变长）。只对 stream 模式有意义（dual 按 60s 音频兜底）。
            over_chars = (self.max_chars > 0 and self.mode == "stream"
                          and len(self._stream_text or "") >= self.max_chars)
            # 2026-10-24 用户要求：一行至少攒够 min_line_chars 字才换行。
            # 未达标时把端点/能量门/字数兜底全部压下（单段上限也不能低于成行门槛，
            # 否则会出现"50 字就切"与"至少 100 字"互相打架）；只对 stream 模式生效——
            # dual 模式没有流式文本，靠音频时长兜底。停止时会强制把尾段上屏。
            if (self.mode == "stream" and self.min_line_chars > 0
                    and len((self._stream_text or "").strip()) < self.min_line_chars):
                ep = False
                said_done = False
                over_chars = False
            if said_done or hard or ep or over_chars:
                if self._speech < int(self.min_speech_sec * SR) and not (ep or over_chars):
                    # 纯静音 / 键盘 / 提示音：丢掉，不调模型
                    # （ep/over_chars 时一定有话刚说完，speech 可能恰在阈值边缘，不丢）
                    self._reset_seg_state()
                    return
                if self.mode == "stream":
                    # 单模型：定版**不重转**，直接拿流式草稿补标点（文字与草稿同源 → 不跳）
                    txt = self._stream_text
                    pre = None
                    if ep:
                        # 端点 + 标点语义确认（2026-10-23 用户实测反馈：
                        # "都在不该断点的地方断了"）。is_endpoint 只表示"这里静音
                        # 够了"，句中换气/犹豫也触发；标点模型才懂"这句话在语义上
                        # 结束了吗"——末字符是句末标点才放行，否则继续攒。
                        # 标点模型缺失时降级：按老逻辑（字数门槛已过滤）放过。
                        pre = self._punct_confirms(txt)
                        if pre is not None:
                            txt = pre              # 确认 → 用带标点的版本定版
                        elif self._ensure_punct() is not None:
                            ep = False             # 语义未完 → 继续攒
                            said_done = False      # 同时抑制能量门（否则 0.7s 兜底会抢断）
                    if ep or said_done or over_chars or hard:
                        self._reset_seg_state()
                        self._finalize_stream(txt, already_punct=bool(pre))
                    return
                audio = np.concatenate(self._buf) if self._buf else np.zeros(0, np.float32)
                self._reset_seg_state()
                self._kick(audio, final=True)
                return
            first = (self._last_preview == 0)
            base = self.first_sec if first else self.flush_sec
            need = int(max(base, self._cost * 1.6) * SR)   # 机器忙时自动降频（预览已跳过 VAD，系数从 3.0 放宽到 1.6）
            if (self._speech >= int(self.min_speech_sec * SR)
                    and self._n - self._last_preview >= need):
                self._last_preview = self._n
                if self._stream_asr is None:          # 流式接管草稿时不再跑老预览（省 CPU）
                    self._kick(np.concatenate(self._buf), final=False)

    def _reset_seg_state(self):
        """定版后重置一段的状态（缓冲/计时/流式句）。

        流式 session 置 None：下一块音频进来时 new_session()——相当于
        reset 效果（endpoint 命中后必须开新流，否则下一句会粘进同一段）。
        """
        self._buf = []
        self._n = 0
        self._speech = 0
        self._silence = 0
        self._last_preview = 0
        self._stream_sess = None
        self._stream_text = ""

    def _kick(self, audio, final):
        self._seq += 1
        seq = self._seq
        self._busy = True
        try:
            threading.Thread(target=self._work, args=(audio, seq, final),
                             daemon=True).start()
        except Exception:                           # noqa: BLE001
            self._busy = False

    def _work(self, audio, seq, final):
        txt = ""
        t0 = time.time()
        try:
            # 预览跳过 VAD 闸门（定版才做静音/反幻觉过滤）——这是"出字慢"的主因
            txt = self._asr.transcribe(audio, skip_vad=not final)
        except Exception as e:                      # noqa: BLE001
            self.status.emit("识别异常：%s" % str(e)[:120])
        finally:
            self._busy = False
            self._cost = time.time() - t0
        t = (txt or "").strip()
        if not t or not self._running or seq != self._seq:
            return
        if final:
            self.line.emit(t)
        else:
            self.partial.emit(t)
