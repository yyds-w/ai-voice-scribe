# -*- coding: utf-8 -*-
"""WASAPI 回环采集（回调式；保真：沿用设备原生采样率与声道）。

与面试软件同款的关键处理（其工程上已验证）：
  - 打开流前校验 isLoopbackDevice 与可用声道数，给出可读的报错；
  - 声道数逐档降级（原生 -> 2 -> 1），规避多声道声卡 [Errno -9998]；
  - 回调异常经 on_error 上报（只报一次，避免每 20ms 刷一条）。

差异（面向"录音保真"而非"喂识别引擎"）：
  - 不重采样、不混单声道；多声道（>2）时取前两个声道（前左/前右）；
  - 回调直接把 float32 数据 copy 一份交给上层——PortAudio 的输入缓冲在
    回调返回后会被复用，不 copy 会读到被覆盖的数据（正确性关键点）。
"""
from __future__ import annotations

import pyaudiowpatch as pyaudio
import numpy as np


def make_capture(device_index: int, on_audio, on_error=None, process_pid: int = 0,
                 name: str = ""):
    """按模式造采集器（2026-10-24 新增进程级捕获）。

    process_pid > 0 → WASAPI process loopback（**无视系统静音/音量**，只采该进程）；
    否则 → 设备回环（采端点混音，系统静音时无信号）。
    两者接口一致（start/stop/on_audio/sample_rate/channels/name），上层无感。
    """
    if process_pid:
        from .process_loopback import ProcessLoopbackCapture
        return ProcessLoopbackCapture(int(process_pid), on_audio,
                                      on_error=on_error, name=name)
    return LoopbackCapture(device_index, on_audio, on_error=on_error)


class LoopbackCapture:
    def __init__(self, device_index: int, on_audio, on_error=None,
                 frames_per_buffer: int = 4096):
        self.device_index = device_index
        self.on_audio = on_audio          # on_audio(samples: np.ndarray[(N, ch)])
        self.on_error = on_error          # on_error(reason: str)
        self.frames_per_buffer = frames_per_buffer
        self.sample_rate = 48000
        self.channels = 2
        self.name = ""
        self._p = None
        self._stream = None
        self._running = False
        self._err_reported = False

    def _report(self, why: str) -> None:
        if self._err_reported:
            return
        self._err_reported = True
        if self.on_error:
            try:
                self.on_error(why)
            except Exception:  # noqa: BLE001
                pass

    def _callback(self, in_data, frame_count, time_info, status):
        if not self._running or in_data is None:
            return (None, pyaudio.paContinue)
        try:
            arr = np.frombuffer(in_data, dtype=np.float32)
            if arr.size:
                arr = arr.reshape(-1, self.channels)
                if self.channels > 2:
                    arr = arr[:, :2].copy()
                else:
                    arr = arr.copy()      # 见模块 docstring：必须 copy
                self.on_audio(arr)
        except Exception as e:            # noqa: BLE001
            self._report("采集回调异常：%s: %s" % (type(e).__name__, e))
        return (None, pyaudio.paContinue)

    def start(self) -> None:
        self._p = pyaudio.PyAudio()
        try:
            info = self._p.get_device_info_by_index(self.device_index)
        except Exception:
            self._p.terminate()
            self._p = None
            raise RuntimeError(
                "设备 [%d] 已不存在（刚被拔掉或 Windows 重新编号了）。" % self.device_index)

        self.name = info.get("name") or ""
        self.sample_rate = int(info["defaultSampleRate"])
        native = max(1, int(info["maxInputChannels"]))

        if not info.get("isLoopbackDevice"):
            self._p.terminate()
            self._p = None
            raise RuntimeError(
                "设备 [%d] %s 不是回环录音设备（多半是 Windows 重排了音频设备，刷新后重选）。"
                % (self.device_index, self.name))
        if native < 1:
            self._p.terminate()
            self._p = None
            raise RuntimeError(
                "设备 [%d] %s 报告可用输入声道为 0，打不开。" % (self.device_index, self.name))

        cands = []
        for ch in (native, 2, 1):
            if ch >= 1 and ch not in cands:
                cands.append(ch)
        last = None
        for ch in cands:
            try:
                self._stream = self._p.open(
                    format=pyaudio.paFloat32,
                    channels=ch,
                    rate=self.sample_rate,
                    input=True,
                    input_device_index=self.device_index,
                    frames_per_buffer=self.frames_per_buffer,
                    stream_callback=self._callback,
                )
                self.channels = ch
                last = None
                break
            except Exception as e:        # noqa: BLE001
                last = e
                self._stream = None
        if last is not None or self._stream is None:
            self._p.terminate()
            self._p = None
            raise RuntimeError(
                "设备 [%d] %s 打不开（已试过 %s 声道，%d Hz）。原始错误：%s\n"
                "常见原因：设备被别的程序独占，或它其实不是扬声器回环。"
                % (self.device_index, self.name, cands, self.sample_rate, last))
        self._running = True
        self._stream.start_stream()

    def stop(self) -> None:
        self._running = False
        try:
            if self._stream is not None:
                self._stream.stop_stream()
                self._stream.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._p is not None:
                self._p.terminate()
        except Exception:  # noqa: BLE001
            pass
        self._stream = None
        self._p = None
