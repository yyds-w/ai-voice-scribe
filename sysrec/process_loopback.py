# -*- coding: utf-8 -*-
"""WASAPI process loopback（Win10 2004+）：按进程捕获音频，**无视系统静音/音量**。

为什么需要（2026-10-24 用户实测）：设备回环（WASAPI loopback）采的是端点
混音输出（post-mix），系统总静音 → 信号被乘 0 → 采不到；应用自己的音量
是 pre-mix，改变不了结果。NVIDIA HDMI（G52）端点尤其明显（静音即停采）。

process loopback 是 Win10 2004 引入的 ApplicationLoopback API：
    用 ActivateAudioInterfaceAsync 激活一个"虚拟捕获端点"，只包含/排除指定
    进程树的音频流，且**在端点音量/静音之前**取信号——所以系统静音也能录。
        params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
        params.ProcessLoopbackParams.TargetProcessId = <目标 PID>
        params.ProcessLoopbackParams.ProcessLoopbackMode = INCLUDE_TARGET_PROCESS_TREE
实测（2026-10-24，系统 mute 状态）：INCLUDE 播放进程 → 峰值 0.985（满幅）。
注意 EXCLUDE 模式（排除自己）在本机实测采到全 0，故只用 INCLUDE。

对外接口对齐 capture.LoopbackCapture：start() / stop() / on_audio(Nx2 float32)
/ sample_rate / channels / name，便于 Recorder / SubtitleEngine 无感切换。
"""
from __future__ import annotations

import ctypes
import threading
import time
from ctypes import (POINTER, Structure, byref, c_int, c_int64, c_ubyte, c_uint32,
                    c_ulong, c_ushort, c_void_p, wintypes)

import numpy as np

try:                                  # comtypes 是既有依赖（pycaw 也用它）
    import comtypes  # noqa: F401
    from comtypes import (COMMETHOD, COMObject, GUID, HRESULT, IUnknown,
                          CoInitialize, CoUninitialize)
    _COM_OK = True
except Exception:                     # noqa: BLE001
    _COM_OK = False

# ---------------- 常量 ----------------
VT_BLOB = 65
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
ACTIVATION_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE = 0          # 只采目标进程树（本机唯一可用模式）
VAD_PROCESS_LOOPBACK = "VAD\\Process_Loopback"

SAMPLE_RATE = 48000
CHANNELS = 2


# ---------------- 结构 ----------------
class _PL_PARAMS(Structure):
    _fields_ = [("TargetProcessId", wintypes.DWORD),
                ("ProcessLoopbackMode", c_int)]


class _ACTIVATION_PARAMS(Structure):
    _fields_ = [("ActivationType", c_int),
                ("ProcessLoopbackParams", _PL_PARAMS)]


class _BLOB(Structure):
    _fields_ = [("cbSize", c_ulong), ("pBlobData", c_void_p)]


class _PROPVARIANT(Structure):
    _fields_ = [("vt", c_ushort), ("r1", c_ushort), ("r2", c_ushort), ("r3", c_ushort),
                ("blob", _BLOB)]


class _WAVEFORMATEX(Structure):
    _fields_ = [("wFormatTag", c_ushort), ("nChannels", c_ushort),
                ("nSamplesPerSec", c_ulong), ("nAvgBytesPerSec", c_ulong),
                ("nBlockAlign", c_ushort), ("wBitsPerSample", c_ushort),
                ("cbSize", c_ushort)]


if _COM_OK:
    class _IActivateOp(IUnknown):
        _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetActivateResult",
                      (["out"], POINTER(HRESULT), "hr"),
                      (["out"], POINTER(POINTER(IUnknown)), "unk")),
        ]

    class _ICompletionHandler(IUnknown):
        _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
        _methods_ = [
            COMMETHOD([], HRESULT, "ActivateCompleted",
                      (["in"], POINTER(_IActivateOp), "op")),
        ]

    class _IAgileObject(IUnknown):
        """标记接口；不实现它 ActivateAudioInterfaceAsync 会返回 E_ILLEGAL_METHOD_CALL。"""
        _iid_ = GUID("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
        _methods_ = []

    class _Handler(COMObject):
        _com_interfaces_ = [_ICompletionHandler, _IAgileObject]

        def __init__(self, evt):
            self.evt = evt
            super().__init__()

        def ActivateCompleted(self, op):
            self.evt.set()
            return 0

    class _IAudioClient(IUnknown):
        _iid_ = GUID("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
        _methods_ = [
            COMMETHOD([], HRESULT, "Initialize",
                      (["in"], c_int, "ShareMode"),
                      (["in"], c_uint32, "StreamFlags"),
                      (["in"], c_int64, "hnsBufferDuration"),
                      (["in"], c_int64, "hnsPeriodicity"),
                      (["in"], POINTER(_WAVEFORMATEX), "pFormat"),
                      (["in"], c_void_p, "AudioSessionGuid")),
            COMMETHOD([], HRESULT, "GetBufferSize", (["out"], POINTER(c_uint32), "p")),
            COMMETHOD([], HRESULT, "GetStreamLatency", (["out"], POINTER(c_int64), "p")),
            COMMETHOD([], HRESULT, "GetCurrentPadding", (["out"], POINTER(c_uint32), "p")),
            COMMETHOD([], HRESULT, "IsFormatSupported",
                      (["in"], c_int, "ShareMode"),
                      (["in"], POINTER(_WAVEFORMATEX), "pFormat"),
                      (["out"], POINTER(POINTER(_WAVEFORMATEX)), "ppClosest")),
            COMMETHOD([], HRESULT, "GetMixFormat",
                      (["out"], POINTER(POINTER(_WAVEFORMATEX)), "ppFormat")),
            COMMETHOD([], HRESULT, "GetDevicePeriod",
                      (["out"], POINTER(c_int64), "pDefault"),
                      (["out"], POINTER(c_int64), "pMin")),
            COMMETHOD([], HRESULT, "Start"),
            COMMETHOD([], HRESULT, "Stop"),
            COMMETHOD([], HRESULT, "Reset"),
            COMMETHOD([], HRESULT, "SetEventHandle", (["in"], wintypes.HANDLE, "h")),
            COMMETHOD([], HRESULT, "GetService",
                      (["in"], POINTER(GUID), "riid"),
                      (["out"], POINTER(POINTER(IUnknown)), "ppv")),
        ]

    class _IAudioCaptureClient(IUnknown):
        _iid_ = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetBuffer",
                      (["out"], POINTER(POINTER(c_ubyte)), "ppData"),
                      (["out"], POINTER(c_uint32), "pNumFrames"),
                      (["out"], POINTER(c_uint32), "pdwFlags"),
                      (["out"], POINTER(ctypes.c_uint64), "pDevPos"),
                      (["out"], POINTER(ctypes.c_uint64), "pQpcPos")),
            COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], c_uint32, "n")),
            COMMETHOD([], HRESULT, "GetNextPacketSize", (["out"], POINTER(c_uint32), "p")),
        ]


def available() -> bool:
    """process loopback 是否可用（Win10 2004+ 且 comtypes 在）。"""
    if not _COM_OK:
        return False
    try:
        v = ctypes.windll.kernel32.VerifyVersionInfoW  # 仅探存在性，不依赖具体版本判断
        del v
        return True
    except Exception:  # noqa: BLE001
        return False


def system_muted():
    """默认输出端点是否静音 → (muted, volume)。读不到返回 (False, 1.0)。"""
    try:
        from pycaw.pycaw import AudioUtilities
        dev = AudioUtilities.GetSpeakers()
        ev = getattr(dev, "EndpointVolume", None)
        if ev is None:
            return False, 1.0
        return bool(ev.GetMute()), float(ev.GetMasterVolumeLevelScalar())
    except Exception:  # noqa: BLE001
        return False, 1.0


def active_audio_procs(threshold: float = 0.01, samples: int = 3, gap: float = 0.03):
    """当前有声音的会话 → [(pid, 应用名, peak)]，按 peak 降序。失败返回 []。

    GetPeakValue 是**瞬时值**，单次采样可能撞上声音间隙读到 0（2026-10-24 实测：
    同一会话前一次读到 1.0、紧接着读到 0）→ 每个会话采样 samples 次取最大值。
    """
    try:
        from pycaw.api.endpointvolume import IAudioMeterInformation
        from pycaw.pycaw import AudioUtilities
        from .audio_source import session_app_name
    except Exception:  # noqa: BLE001
        return []
    out = []
    try:
        sessions = AudioUtilities.GetAllSessions()
    except Exception:  # noqa: BLE001
        return []
    for s in sessions:
        try:
            proc = getattr(s, "Process", None)
            if proc is None:
                continue
            if s.SimpleAudioVolume.GetMute():
                continue                       # 应用自身静音 → 忽略
            meter = s._ctl.QueryInterface(IAudioMeterInformation)
            peak = 0.0
            for _ in range(max(1, int(samples))):
                peak = max(peak, float(meter.GetPeakValue()))
                if peak >= threshold:
                    break
                time.sleep(max(0.0, gap))
            if peak < threshold:
                continue
            pid = int(getattr(proc, "pid", 0) or 0)
            if pid <= 0:
                continue
            out.append((pid, session_app_name(s), peak))
        except Exception:  # noqa: BLE001
            continue
    out.sort(key=lambda x: x[2], reverse=True)
    return out


class ProcessLoopbackCapture:
    """按进程捕获（INCLUDE 目标进程树）。接口对齐 capture.LoopbackCapture。"""

    def __init__(self, target_pid: int, on_audio, on_error=None, name: str = "",
                 idle_timeout: float = 3.0):
        self.target_pid = int(target_pid)
        self.on_audio = on_audio
        self.on_error = on_error
        self.name = name or ("进程 #%d" % self.target_pid)
        self.sample_rate = SAMPLE_RATE
        self.channels = CHANNELS
        self.idle_timeout = float(idle_timeout)
        self._thread = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._err = ""
        self._err_reported = False

    # ---- 内部 ----
    def _report(self, why: str) -> None:
        if self._err_reported:
            return
        self._err_reported = True
        if self.on_error:
            try:
                self.on_error(why)
            except Exception:  # noqa: BLE001
                pass

    def _open(self):
        evt = threading.Event()
        handler = _Handler(evt)
        params = _ACTIVATION_PARAMS()
        params.ActivationType = ACTIVATION_PROCESS_LOOPBACK
        params.ProcessLoopbackParams.TargetProcessId = self.target_pid
        params.ProcessLoopbackParams.ProcessLoopbackMode = PROCESS_LOOPBACK_MODE_INCLUDE
        pv = _PROPVARIANT()
        pv.vt = VT_BLOB
        pv.blob.cbSize = ctypes.sizeof(params)
        pv.blob.pBlobData = ctypes.cast(byref(params), c_void_p)

        op = ctypes.POINTER(_IActivateOp)()
        mmdevapi = ctypes.WinDLL("mmdevapi.dll")
        proto = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, wintypes.LPCWSTR, POINTER(GUID), POINTER(_PROPVARIANT),
            POINTER(_ICompletionHandler), POINTER(ctypes.POINTER(_IActivateOp)))
        fn = proto(("ActivateAudioInterfaceAsync", mmdevapi))
        hr = fn(VAD_PROCESS_LOOPBACK, byref(_IAudioClient._iid_), byref(pv),
                handler.QueryInterface(_ICompletionHandler), byref(op))
        if hr < 0:
            raise OSError("激活失败 hr=0x%08X（Windows 需 10 2004+）" % (hr & 0xFFFFFFFF))
        if not evt.wait(10):
            raise TimeoutError("激活超时")
        act_hr, unk = op.GetActivateResult()
        if act_hr < 0:
            raise OSError("激活结果失败 hr=0x%08X" % (act_hr & 0xFFFFFFFF))
        client = unk.QueryInterface(_IAudioClient)

        wfx = _WAVEFORMATEX()
        wfx.wFormatTag = 3                    # IEEE float32
        wfx.nChannels = self.channels
        wfx.nSamplesPerSec = self.sample_rate
        wfx.wBitsPerSample = 32
        wfx.nBlockAlign = self.channels * 32 // 8
        wfx.nAvgBytesPerSec = self.sample_rate * wfx.nBlockAlign
        wfx.cbSize = 0
        flags = (AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM
                 | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY)
        client.Initialize(AUDCLNT_SHAREMODE_SHARED, flags, 2_000_000, 0,
                          byref(wfx), None)
        cap = client.GetService(_IAudioCaptureClient._iid_).QueryInterface(
            _IAudioCaptureClient)
        client.Start()
        return client, cap

    def _run(self) -> None:
        CoInitialize()
        client = cap = None
        try:
            client, cap = self._open()
            self._ready.set()
            last_data = time.time()
            while not self._stop.is_set():
                got = 0
                n = cap.GetNextPacketSize()
                while n:
                    pdata, frames, _flags, _d, _q = cap.GetBuffer()
                    if frames:
                        buf = ctypes.string_at(pdata, frames * self.channels * 4)
                        arr = np.frombuffer(buf, dtype=np.float32).reshape(
                            -1, self.channels).copy()
                        if arr.size:
                            self.on_audio(arr)
                            got += frames
                    cap.ReleaseBuffer(frames)
                    n = cap.GetNextPacketSize()
                if got:
                    last_data = time.time()
                elif time.time() - last_data > self.idle_timeout:
                    self._report("目标应用已停止出声或已退出（超过 %.0f 秒无音频）"
                                 % self.idle_timeout)
                    return
                time.sleep(0.01)
        except Exception as e:  # noqa: BLE001
            self._err = str(e)
            self._ready.set()                 # 让 start() 不用白等
            self._report("进程捕获异常：%s: %s" % (type(e).__name__, e))
        finally:
            try:
                if client is not None:
                    client.Stop()
            except Exception:  # noqa: BLE001
                pass
            CoUninitialize()

    # ---- 对外 ----
    def start(self) -> None:
        if not _COM_OK:
            raise RuntimeError("缺少 comtypes，无法使用进程级捕获")
        self._thread = threading.Thread(target=self._run, daemon=True,
                                       name="proc-loopback")
        self._thread.start()
        if not self._ready.wait(15):
            raise RuntimeError("进程捕获启动超时（15 秒）")
        if self._err:
            raise RuntimeError(self._err)

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        self._thread = None
        if t is not None:
            t.join(timeout=3.0)
