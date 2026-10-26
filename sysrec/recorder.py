# -*- coding: utf-8 -*-
"""录音核心：回环采集 → 缺口补零 → 编码落盘。

## 缺口补零（本模块的关键逻辑，原理）
WASAPI 回环在系统没有任何播放流时不产出数据（PortAudio issue #935 等证实）。
本机实测两类设备：
    Realtek 扬声器 —— 无播放时持续供给全零帧（时间轴天然连续）；
    NVIDIA HDMI   —— 无播放时完全断流（回调停止）。
断流型设备上不补零，会让录音时长比真实经过时间短、静音段被"抽走"。

统一策略（以墙钟为准，兼容两类设备），每个数据包到达时：
    应写总帧数 E = (now - 锚点) * 采样率
    缺口 = E - 已写帧数 - 本包帧数
    缺口 > 阈值（0.5s 对应帧数）→ 先写入等量零帧（静音），再写本包。
持续供帧型：缺口恒 ≈ 0（只受缓冲抖动影响）；
断流型：断流期间由 writer 空闲循环持续补零（见 _fill_idle_gap），数据恢复
时只剩余一个小于阈值的小缺口，由收包路径补齐。
精度上限 = 阈值（0.5s），对静音段完全够用（判定逻辑见 compute_gap_frames，带单测）。

## 暂停/继续
暂停期间丢弃数据；恢复时把时间锚点重设为"当前时刻 - 已写时长"，
让 E 与已写帧数重新对齐——时间轴不跳、文件不加密。

## 文件命名（2026-10-23「录音来源识别 + 自动命名」）
录音中一律用临时名 ``临时-时间戳``（Q1=C：录制全程看不到最终名字）；
stop() 关闭编码器后，按 AudioSourceTracker 锁定的"第一个出声的应用"改名：
    ``应用名-时间戳.mp3``（Q8=A：半角连字符，无下划线）
检测不到应用（静音开始 / 只有系统提示音）→ ``系统声音-时间戳.mp3``（Q4=A）。
分卷的 ``_pNN`` 后缀同步迁移；目标名冲突时自动追加 ``_1``。

## 线程模型
    PortAudio 回调线程：仅做数据 copy + 入队（不做重活）；
    writer 线程：出队 → 补零 → 编码 → 落盘 → 电平统计 → 分卷判断；
    monitor 线程：每秒盯"默认输出是否变化"（插拔耳机场景），必要时热切换；
    source 线程（audio_source.AudioSourceTracker）：轮询音频会话电平，锁定来源应用；
    控制线程（调用 start/stop 的线程，GUI=主线程）：防休眠状态按它维护。
"""
from __future__ import annotations

import ctypes
import os
import queue
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from . import config as config_mod
from .audio_source import FALLBACK_NAME, AudioSourceTracker, sanitize_app_name
from .capture import make_capture
from .devices import default_loopback_device
from .encoder import make_encoder, normalize_fmt


def compute_gap_frames(expected_total: int, written: int, packet_frames: int,
                       threshold_frames: int) -> int:
    """缺口补零的纯函数（原理见模块 docstring）。

    gap <= 阈值（含负数）视为正常抖动/正常连续，不补；否则返回需补的零帧数。
    """
    gap = int(expected_total) - int(written) - int(packet_frames)
    return gap if gap > int(threshold_frames) else 0


def _prevent_sleep(on: bool) -> None:
    """录音期间阻止系统自动睡眠（屏幕仍可关闭）。

    注意：SetThreadExecutionState 的状态按"调用线程"维护——本工具
    start()/stop() 都由同一控制线程调用（GUI 主线程 / CLI 主线程），
    状态设置与清除成对出现。
    """
    if os.name != "nt":
        return
    try:
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:  # noqa: BLE001
        pass


def design_voice_kernel(sr: int, presence_db: float = 2.5, mud_db: float = 1.5,
                        taps: int = 257):
    """人声增强 FIR（2026-10-26）：高通去轰鸣 + 去浑浊 + 2.8k 存在感 + 空气感。

    用逆 FFT 法按频响曲线设计**线性相位** FIR（不改相位 → 不会有"闷/糊"），
    再把 1kHz 处的实际响应归一到 0dB —— 只改音色，不改整体响度（增益交给 AGC）。
    实测响应（48k、257 抽头）：60Hz -4.8 ｜ 200Hz -2.9 ｜ 1k 0.0 ｜ 2.8k +2.1。
    """
    n = int(taps)
    freqs = np.fft.rfftfreq(8192, 1.0 / float(sr))

    def curve(f):
        db = np.where(f < 60, -12.0, 0.0)
        db = np.where((f >= 60) & (f < 120), -12.0 * (120 - f) / 60.0, db)
        db = db - mud_db * np.exp(-0.5 * ((np.log2(np.maximum(f, 1.0) / 200.0)) / 0.55) ** 2)
        db = db + presence_db * np.exp(-0.5 * ((np.log2(np.maximum(f, 1.0) / 2800.0)) / 0.75) ** 2)
        db = db + 1.5 * np.clip((f - 7000) / 4000.0, 0, 1)
        return db

    mag = 10 ** (curve(freqs) / 20.0)
    spec = np.zeros(8192, dtype=complex)
    spec[:len(mag)] = mag
    spec[len(mag):] = mag[-2:0:-1].conj()
    k = np.fft.ifft(spec).real
    k = np.roll(k, n // 2)[:n] * np.hanning(n)
    H = np.abs(np.fft.rfft(k, 8192))
    f = np.fft.rfftfreq(8192, 1.0 / float(sr))
    g1k = float(np.interp(1000.0, f, H))
    return (k / max(g1k, 1e-6)).astype(np.float32)


@dataclass
class RecorderConfig:
    device_index: int | None = None      # None = 自动跟随系统默认输出
    formats: list = field(default_factory=lambda: ["mp3"])   # ["mp3"] / ["wav"] / ["mp3","wav"]
    mp3_bitrate: int = 192
    out_dir: str = ""                    # 空 = <工具目录>\录音
    temp_prefix: str = "临时"            # 录音中的临时名前缀（停止时按来源改名）
    segment_minutes: int = 120           # 0 = 不分卷
    follow_default: bool = True          # 录音中默认输出切换时自动跟随续录
    name_by_source: bool = True          # 停止时按"音频来源应用"命名（2026-10-23）
    no_source_name: str = FALLBACK_NAME  # 检测不到来源时的名字（Q4=A：系统声音）
    process_pid: int = 0                 # >0 = 按进程捕获（无视系统静音，2026-10-24）
    auto_gain: bool = True               # 录音端自动增益（2026-10-25，很小声时放大再存盘）
    voice_boost: str = "mild"            # 人声增强：off / mild / strong（2026-10-26）


class Recorder:
    GAP_THRESHOLD_S = 0.5

    def __init__(self, cfg: RecorderConfig, on_event=None):
        self.cfg = cfg
        self.on_event = on_event                 # on_event(kind: str, payload)
        self._state = "idle"                     # idle | recording | paused
        self._agc_gain = 1.0                     # 当前录音增益（自动增益用）
        self._agc_packets = 0
        # 人声增强档位：只认 mild / strong（"off"、空值、非法值一律等于不做 EQ）
        _vb = str(cfg.voice_boost or "off").strip().lower()
        self._eq_mode = _vb if (cfg.auto_gain and _vb in ("mild", "strong")) else ""
        self._eq_kernel = None
        self._eq_tail = []
        self._q = queue.Queue(maxsize=512)
        self._capture = None
        self._encoders = []
        self._files = []
        self._sr = 0
        self._ch = 0
        self._out_dir = ""
        self._stamp = ""
        self._seg_index = 1
        self._seg_frames = 0
        self._frames_written = 0
        self._gap_frames = 0
        self._dropped_packets = 0
        self._io_error_reported = False
        self._capture_dead = False
        self._last_device_name = ""
        self._cur_device_index = None
        self._level_rms = 0.0
        self._level_peak = 0.0
        self._t_anchor = 0.0
        self._pause_started = 0.0
        self._gap_threshold = 0
        self._paused = False
        self._stopping = False
        self._writer = None
        self._monitor = None
        self._tracker = None                 # AudioSourceTracker（来源识别）
        self._file_suffixes = []             # 与 _files 对齐：分段后缀（"" / "_pNN"）
        self._fmt_list = []

    # ---------------- 对外 API ----------------

    def start(self) -> None:
        if self._state != "idle":
            raise RuntimeError("录音已在进行中。")
        idx = self.cfg.device_index
        if idx is None:
            idx, name = default_loopback_device()
            if idx is None:
                raise RuntimeError("找不到可用的回环录音设备（系统里没有可录的输出端点）。")

        self._capture = make_capture(idx, self._on_audio, on_error=self._on_capture_error,
                                     process_pid=self.cfg.process_pid)
        try:
            self._capture.start()
        except Exception:
            self._capture = None
            raise
        self._sr = self._capture.sample_rate
        self._ch = self._capture.channels
        self._cur_device_index = idx
        self._last_device_name = self._capture.name

        self._fmt_list = [normalize_fmt(f) for f in (self.cfg.formats or ["mp3"])]
        self._out_dir = self.cfg.out_dir or config_mod.default_output_dir()
        os.makedirs(self._out_dir, exist_ok=True)
        # Q8=A：最终名格式「应用名-年月日时分秒」（无下划线）；录音中用临时名
        self._stamp = time.strftime("%Y%m%d%H%M%S")
        self._files = []
        self._file_suffixes = []
        self._seg_index = 1
        self._seg_frames = (int(self.cfg.segment_minutes) * 60 * self._sr
                            if int(self.cfg.segment_minutes or 0) > 0 else 0)
        self._frames_written = 0
        self._gap_frames = 0
        self._dropped_packets = 0
        self._io_error_reported = False
        self._capture_dead = False
        self._level_rms = self._level_peak = 0.0
        self._gap_threshold = int(self.GAP_THRESHOLD_S * self._sr)
        self._t_anchor = time.perf_counter()
        self._paused = False
        self._stopping = False
        self._open_encoders()

        self._writer = threading.Thread(target=self._writer_loop, name="sysrec-writer", daemon=True)
        self._writer.start()
        self._monitor = threading.Thread(target=self._monitor_loop, name="sysrec-monitor", daemon=True)
        self._monitor.start()
        self._start_source_tracker()

        self._state = "recording"
        _prevent_sleep(True)
        self._emit("started", {"device": self._capture.name, "sample_rate": self._sr,
                               "channels": self._ch, "files": list(self._files)})

    def stop(self):
        """停止并返回结果 dict（files / seconds / gap_seconds ...）。"""
        if self._state == "idle":
            return None
        cap, self._capture = self._capture, None
        if cap is not None:
            try:
                cap.stop()
            except Exception:  # noqa: BLE001
                pass
        self._stopping = True
        t_stop = time.perf_counter()
        try:
            # "收尾哨兵"：排在所有在途数据之后，由 writer 做最后一次时间轴结算
            # （把 written 补齐到停止时刻），然后退出。用哨兵而不是直接锁，
            # 保证所有写操作都发生在 writer 单线程里、不竞争。
            self._q.put(("__final__", t_stop), timeout=5)
        except Exception:  # noqa: BLE001
            pass
        if self._writer is not None:
            self._writer.join(timeout=15)
            self._writer = None
        if self._monitor is not None:
            self._monitor.join(timeout=3)
            self._monitor = None
        for enc in self._encoders:
            try:
                enc.close()
            except Exception:  # noqa: BLE001
                pass
        self._encoders = []
        # ---- 按音频来源改名（Q1=C：编码器全部关闭后改，此刻没有线程持有文件句柄）----
        tracker, self._tracker = self._tracker, None
        app = None
        if tracker is not None:
            try:
                tracker.stop()
                app = tracker.app_name
            except Exception:  # noqa: BLE001  tracker 异常不能拖垮停止流程
                app = None
        if self.cfg.name_by_source:
            try:
                self._rename_by_source(app)
            except Exception as e:  # noqa: BLE001  改名失败保留临时名，绝不抛
                self._emit("error", "按来源改名异常（保留临时名）：%s" % e)
        _prevent_sleep(False)
        self._state = "idle"
        result = {
            "files": list(self._files),
            "seconds": (self._frames_written / self._sr) if self._sr else 0.0,
            "gap_seconds": (self._gap_frames / self._sr) if self._sr else 0.0,
            "dropped_packets": self._dropped_packets,
            "sample_rate": self._sr,
            "channels": self._ch,
        }
        self._emit("stopped", result)
        return result

    def pause(self) -> None:
        if self._state == "recording":
            self._paused = True
            self._pause_started = time.perf_counter()
            self._state = "paused"
            self._emit("paused", {})

    def resume(self) -> None:
        if self._state == "paused":
            self._t_anchor += time.perf_counter() - self._pause_started
            self._paused = False
            self._state = "recording"
            self._emit("resumed", {})

    def snapshot(self) -> dict:
        """给 UI 轮询的状态快照（线程安全：均为简单读）。"""
        sr = self._sr or 1
        tracker = self._tracker
        return {
            "state": self._state,
            "seconds": self._frames_written / sr if self._sr else 0.0,
            "files": list(self._files),
            "level_rms": self._level_rms,
            "level_peak": self._level_peak,
            "gap_seconds": self._gap_frames / sr if self._sr else 0.0,
            "device_name": self._last_device_name,
            "sample_rate": self._sr,
            "channels": self._ch,
            "dropped_packets": self._dropped_packets,
            # 来源识别：录音中实时可见（名字要停止时才落盘，见 _rename_by_source）
            "source_app": (tracker.app_name if tracker is not None else None),
            "source_locked": bool(tracker is not None and tracker.app_name),
        }

    # ---------------- 来源识别 / 按来源改名 ----------------

    def _start_source_tracker(self) -> None:
        """起音频来源识别线程；依赖缺失 / COM 失败时降级（不影响录音）。"""
        self._tracker = None
        if not self.cfg.name_by_source:
            return
        try:
            tr = AudioSourceTracker(on_event=self._on_source_event)
            if tr.start():
                self._tracker = tr
            else:
                self._emit("source_unavailable", tr.error or "来源识别不可用")
        except Exception as e:  # noqa: BLE001
            self._tracker = None
            self._emit("source_unavailable", "%s: %s" % (type(e).__name__, e))

    def _on_source_event(self, kind, payload) -> None:
        # 跑在 tracker 线程：只转发事件，不做重活
        self._emit("source_" + kind, payload)

    def _rename_by_source(self, app: str | None) -> None:
        """停止时按来源改名（Q1=C / Q8=A / Q4=A）。

        app = tracker 锁定的"第一个出声的应用"；None（静音开始 / 只有系统提示音 /
        tracker 不可用）→ 回落 ``系统声音-时间戳``。分卷 ``_pNN`` 后缀同步迁移。
        单文件失败只影响该文件（保留临时名 + error 事件），不抛异常。
        """
        if not self._files:
            return
        name = sanitize_app_name(app) if app else ""
        base = "%s-%s" % (name or (self.cfg.no_source_name or FALLBACK_NAME),
                          self._stamp)
        out, ok = [], True
        for i, path in enumerate(self._files):
            seg = self._file_suffixes[i] if i < len(self._file_suffixes) else ""
            ext = os.path.splitext(path)[1]
            target = self._avoid_collision(os.path.join(self._out_dir, base + seg + ext))
            if os.path.abspath(target) != os.path.abspath(path):
                try:
                    os.replace(path, target)
                except OSError as e:
                    ok = False
                    out.append(path)
                    self._emit("error", "按音频来源改名失败（保留临时名）：%s" % e)
                    continue
            out.append(target)
        self._files = out
        if ok:
            self._emit("renamed", {"base": base, "app": name or None,
                                   "files": list(self._files)})

    @staticmethod
    def _avoid_collision(path: str) -> str:
        """目标名已存在（同一秒内两次录音等）→ 追加 _1 / _2 …；找不到就原样返回。"""
        if not os.path.exists(path):
            return path
        stem, ext = os.path.splitext(path)
        for i in range(1, 100):
            cand = "%s_%d%s" % (stem, i, ext)
            if not os.path.exists(cand):
                return cand
        return path

    # ---------------- 内部实现 ----------------

    def _emit(self, kind: str, payload) -> None:
        if self.on_event:
            try:
                self.on_event(kind, payload)
            except Exception:  # noqa: BLE001
                pass

    def _open_encoders(self) -> None:
        """按「临时名-时间戳」开编码器（停止时才按来源改成最终名，见 _rename_by_source）。"""
        self._encoders = []
        self._files = []
        self._file_suffixes = []
        base = "%s-%s" % (self.cfg.temp_prefix, self._stamp)
        seg = "" if self._seg_index == 1 else "_p%02d" % self._seg_index
        for fmt in self._fmt_list:
            path = os.path.join(self._out_dir, "%s%s.%s" % (base, seg, fmt))
            enc = make_encoder(fmt, path, self._sr, self._ch, self.cfg.mp3_bitrate)
            self._encoders.append(enc)
            self._files.append(path)
            self._file_suffixes.append(seg)

    # --- 采集回调线程（仅 copy + 入队）---

    def _on_audio(self, samples: np.ndarray) -> None:
        try:
            self._q.put_nowait((time.perf_counter(), samples))
        except queue.Full:
            self._dropped_packets += 1

    def _on_capture_error(self, reason: str) -> None:
        # ⚠ 本方法运行在 PortAudio 回调线程里：绝不能在这里调用
        #   capture.stop()/重开流（回调线程内停流会死锁）。只上报 + 标记，
        #   由 monitor 线程接手处理。
        self._capture_dead = True
        self._emit("capture_error", reason)

    # --- writer 线程 ---

    def _writer_loop(self) -> None:
        while True:
            try:
                item = self._q.get(timeout=0.25)
            except queue.Empty:
                if self._stopping:
                    break
                # 【为什么空闲时也要补零】断流型设备（如 NVIDIA HDMI，实测）
                # 在"无任何播放"期间一个数据包都不来；若无数据时什么都不做，
                # "全程静音的录音"会得到 0 秒文件、时间轴缺失。这里周期性把
                # 时间轴推进到当前时刻（补零），数据恢复后自动无缝接上。
                try:
                    self._fill_idle_gap()
                except Exception as e:  # noqa: BLE001
                    if not self._io_error_reported:
                        self._io_error_reported = True
                        self._emit("error", "写盘异常：%s: %s" % (type(e).__name__, e))
                continue
            if item is None:
                break
            if isinstance(item, tuple) and item and item[0] == "__final__":
                self._final_fill(float(item[1]))
                break
            t, samples = item
            try:
                self._write_packet(t, samples)
            except Exception as e:  # noqa: BLE001
                if not self._io_error_reported:
                    self._io_error_reported = True
                    self._emit("error", "写盘异常：%s: %s" % (type(e).__name__, e))

    def _fill_idle_gap(self) -> None:
        """空闲时推进时间轴：把"该写但没收到数据"的部分补成静音。

        阈值与收包路径一致（0.5s）：小于阈值视为正常缓冲抖动，不补。
        补零期间电平归零（当前确实没有声音在播放）。
        """
        if self._paused or self._state == "idle" or not self._sr:
            return
        expected = int((time.perf_counter() - self._t_anchor) * self._sr)
        gap = compute_gap_frames(expected, self._frames_written, 0, self._gap_threshold)
        if gap:
            zeros = np.zeros((gap, self._ch), dtype=np.float32)
            for enc in self._encoders:
                enc.write(zeros)
            self._frames_written += gap
            self._gap_frames += gap
            self._level_rms = self._level_peak = 0.0

    # ---- 录音端自动增益（2026-10-25/26）----
    # 参数定档（用户 A/B 试听后选 N）：峰值目标 0.71（-3dBFS）、上限 32×、
    # 超过 0.70 的峰值做软限幅到 0.95（实测只动 0.65% 的样本，听不出失真）。
    # 只放大不压小；快降慢升（0.35/0.05）避免抽气。
    AGC_TARGET = 0.71
    AGC_MAX_GAIN = 32.0
    LIMIT_THR = 0.70
    LIMIT_CEIL = 0.95

    def _ensure_eq(self) -> None:
        """按当前采样率设计人声增强 FIR（1kHz 归 0dB，只改音色不改响度）。"""
        if self._eq_kernel is not None or not self._eq_mode:
            return
        sr = int(self._sr or 48000)
        presence, mud = (2.5, 1.5) if self._eq_mode == "mild" else (4.0, 3.0)
        self._eq_kernel = design_voice_kernel(sr, presence, mud)
        self._eq_tail = [None] * max(1, int(self._ch or 2))

    def _apply_eq(self, samples: np.ndarray) -> np.ndarray:
        """线性相位 FIR 人声增强（重叠相加，流式无接缝）。"""
        k = self._eq_kernel
        if k is None:
            return samples
        n = len(samples)
        chs = []
        for c in range(samples.shape[1]):
            full = np.convolve(samples[:, c], k, mode="full")
            seg = full[:n].copy()
            tail = self._eq_tail[c]
            if tail is not None:
                m = min(len(tail), n)
                seg[:m] += tail[:m]
            self._eq_tail[c] = full[n:]
            chs.append(seg)
        return np.stack(chs, axis=1).astype(np.float32)

    def _soft_limit(self, y: np.ndarray) -> np.ndarray:
        """只压超过 LIMIT_THR 的峰值（阈值以下完全透明）。"""
        a = np.abs(y)
        over = a > self.LIMIT_THR
        if not np.any(over):
            return y
        span = self.LIMIT_CEIL - self.LIMIT_THR
        a[over] = self.LIMIT_THR + span * np.tanh((a[over] - self.LIMIT_THR) / span)
        return (np.sign(y) * a).astype(np.float32)

    def _apply_agc(self, samples: np.ndarray) -> np.ndarray:
        """RECORDER 增益链：人声增强（可选）→ 峰值自适应增益 → 软限幅。

        2026-10-26 用户 A/B 后定档：峰值 0.71 / 上限 32× / 软限幅 0.70→0.95。
        实测（6 分钟 -40dBFS 录音）：RMS -40.7 → -14.0 dBFS（接近视频响度），
        软限幅只触及 0.65% 样本，无硬削波。
        """
        if self._eq_mode:
            self._ensure_eq()
            samples = self._apply_eq(samples)
        try:
            peak = float(np.max(np.abs(samples))) if len(samples) else 0.0
        except Exception:                                   # noqa: BLE001
            return samples
        if peak > 1e-5:
            want = min(self.AGC_MAX_GAIN, max(1.0, self.AGC_TARGET / peak))
            if self._agc_packets < 8:                       # 开录头几包直接定档
                self._agc_gain = want
            else:
                alpha = 0.35 if want < self._agc_gain else 0.05   # 快降 / 慢升
                self._agc_gain += alpha * (want - self._agc_gain)
        self._agc_packets += 1
        if abs(self._agc_gain - 1.0) < 0.02:
            return self._soft_limit(samples)
        return self._soft_limit(samples * float(self._agc_gain))

    def _write_packet(self, t: float, samples: np.ndarray) -> None:
        if self._paused:
            self._level_rms = self._level_peak = 0.0
            return
        n = int(samples.shape[0])
        if n <= 0:
            return
        # 0) 自动增益（2026-10-25）：设备回环录的是"音量之后"的信号，
        #    系统音量低就会录出很小声的文件（实测一段 -14LUFS 素材录成 -40dBFS）。
        #    float32 只是被数字缩小，乘回来几乎无损；放大后再编码 mp3，
        #    编码器还会把更多比特分给这段信号。
        if self.cfg.auto_gain:
            samples = self._apply_agc(samples)
        # 1) 缺口补零
        expected = int((t - self._t_anchor) * self._sr)
        gap = compute_gap_frames(expected, self._frames_written, n, self._gap_threshold)
        if gap:
            zeros = np.zeros((gap, self._ch), dtype=np.float32)
            for enc in self._encoders:
                enc.write(zeros)
            self._frames_written += gap
            self._gap_frames += gap
        # 2) 写实际数据
        for enc in self._encoders:
            enc.write(samples)
        self._frames_written += n
        # 3) 电平统计（供 UI 电平表）
        self._level_peak = float(np.max(np.abs(samples)))
        self._level_rms = float(np.sqrt(np.mean(np.square(samples))))
        # 4) 分卷判断
        while self._seg_frames and self._frames_written >= self._seg_frames * self._seg_index:
            self._rollover()

    def _final_fill(self, t_stop: float) -> None:
        """停止时收尾：把时间轴补齐到停止时刻。

        与 _fill_idle_gap 的区别：此刻已确定不会再有新数据，因此**不再受
        0.5s 阈值限制**，缺多少补多少（否则每次停止都会系统性少 0~0.5s）。
        暂停状态下停止时，结算到暂停开始时刻（暂停期间的时间不计入文件）。
        """
        if not self._sr:
            return
        t_end = self._pause_started if self._paused else t_stop
        expected = int((t_end - self._t_anchor) * self._sr)
        gap = expected - int(self._frames_written)
        if gap > 0:
            zeros = np.zeros((gap, self._ch), dtype=np.float32)
            for enc in self._encoders:
                enc.write(zeros)
            self._frames_written += gap
            self._gap_frames += gap

    def _rollover(self) -> None:
        for enc in self._encoders:
            try:
                enc.close()
            except Exception:  # noqa: BLE001
                pass
        self._seg_index += 1
        self._open_encoders()
        self._emit("segment", {"index": self._seg_index})

    # --- monitor 线程（默认输出变化 / 采集流失效的兜底）---

    def _monitor_loop(self) -> None:
        while not self._stopping:
            # 分片睡眠，stop 时能快速退出
            for _ in range(10):
                if self._stopping:
                    return
                time.sleep(0.1)
            if self._state == "idle":
                continue
            if self._capture_dead:
                self._capture_dead = False
                self._recover_capture()
                continue
            idx, name = default_loopback_device()
            if idx is None or idx == self._cur_device_index:
                continue
            self._emit("device_changed", {"index": idx, "name": name})
            if self.cfg.follow_default:
                self._switch_device(idx)

    def _recover_capture(self) -> None:
        """采集流报错后的恢复：重开当前设备，失败则换默认回环。"""
        if self._state == "idle":
            return
        # 进程级捕获：目标应用可能已退出 → 换成当前最响的应用；都没有则回退设备级
        if self.cfg.process_pid:
            try:
                from .process_loopback import active_audio_procs
                procs = active_audio_procs()
            except Exception:                    # noqa: BLE001
                procs = []
            self.cfg.process_pid = procs[0][0] if procs else 0
            if not self.cfg.process_pid:
                self._emit("error", "目标应用已退出，且当前没有应用在放声 → 已回退设备级采集"
                                   "（系统静音时仍然录不到，请调高系统音量或先播放声音）。")
        idx = self._cur_device_index
        for target in (idx, None):
            if target is None:
                target, _n = default_loopback_device()
                target = target if target != idx else None
            if target is None:
                continue
            try:
                new = make_capture(target, self._on_audio,
                                   on_error=self._on_capture_error,
                                   process_pid=self.cfg.process_pid)
                new.start()
            except Exception as e:  # noqa: BLE001
                self._emit("error", "重开采集流失败（设备 %s）：%s" % (target, e))
                continue
            if new.sample_rate != self._sr or new.channels != self._ch:
                new.stop()
                self._emit("error", "重开的采集流格式不一致，已放弃自动恢复。")
                continue
            self._capture = new
            self._cur_device_index = target
            self._last_device_name = new.name
            self._emit("capture_recovered", {"index": target, "name": new.name})
            return
        self._emit("error", "采集流已中断，且自动恢复失败。请停止后重新开始。")

    def _switch_device(self, idx: int) -> None:
        if self.cfg.process_pid:
            return                           # 进程级捕获不跟随默认输出设备（与端点无关）
        old = self._capture
        old_idx = self._cur_device_index
        if old is not None:
            try:
                old.stop()
            except Exception:  # noqa: BLE001
                pass
        new = make_capture(idx, self._on_audio, on_error=self._on_capture_error)
        try:
            new.start()
        except Exception as e:  # noqa: BLE001
            self._emit("error", "切换默认输出失败：%s" % e)
            self._rollback_capture(old_idx)
            return
        if new.sample_rate != self._sr or new.channels != self._ch:
            new.stop()
            self._emit("error", "默认输出已切换，但新设备格式与当前录音不一致"
                               "（%d Hz/%dch vs %d Hz/%dch），无法无缝续录。"
                               % (new.sample_rate, new.channels, self._sr, self._ch))
            self._rollback_capture(old_idx)
            return
        self._capture = new
        self._cur_device_index = idx
        self._last_device_name = new.name
        # 切换造成的空档（几十毫秒级）由"缺口补零"自动补齐
        self._emit("device_switched", {"index": idx, "name": new.name})

    def _rollback_capture(self, idx) -> None:
        if idx is None:
            return
        try:
            back = make_capture(idx, self._on_audio, on_error=self._on_capture_error,
                                process_pid=self.cfg.process_pid)
            back.start()
            self._capture = back
            self._cur_device_index = idx
            self._last_device_name = back.name
        except Exception:  # noqa: BLE001
            self._capture = None
