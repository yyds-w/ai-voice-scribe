# -*- coding: utf-8 -*-
"""设备定位：WASAPI 回环设备枚举、默认输出回环、默认输出变化监控。

经验直接复用自 AI 面试软件的 audio_capture.py（已在用户机器验证）：
  - 用 isLoopbackDevice 标志筛选回环设备；
  - get_default_wasapi_loopback() 拿"当前默认输出"对应的回环（最准）；
  - 找不到时按名字回退（扬声器 / Speaker / Headphones / 耳机）；
  - 默认输出变化的监控是必须的：插拔耳机后 Windows 会切换默认输出，但
    旧的回环流不会报错，只是从此只输出静音（实测，表现为"程序还在跑却
    再也录不到东西"）。
"""
from __future__ import annotations

import pyaudiowpatch as pyaudio


def list_loopback_devices():
    """返回 [(index, name, channels, sample_rate), ...]，列出所有回环录音设备。"""
    p = pyaudio.PyAudio()
    out = []
    try:
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            if info.get("isLoopbackDevice"):
                out.append((
                    i,
                    info.get("name"),
                    int(info.get("maxInputChannels", 0)),
                    int(info.get("defaultSampleRate", 48000)),
                ))
    finally:
        p.terminate()
    return out


def default_loopback_device():
    """返回 (index, name)：当前默认输出对应的回环设备；找不到返回 (None, None)。"""
    p = pyaudio.PyAudio()
    try:
        try:
            info = p.get_default_wasapi_loopback()
            if info and "index" in info:
                return int(info["index"]), info.get("name")
        except Exception:  # noqa: BLE001  回退到名字扫描
            pass
        best = None
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            if not info.get("isLoopbackDevice"):
                continue
            name = info.get("name") or ""
            if best is None:
                best = (i, name)
            if any(k in name for k in ("扬声器", "Speaker", "Headphones", "耳机")):
                return i, name
        return best or (None, None)
    finally:
        p.terminate()


def loopback_for_output_name(friendly: str):
    """按输出设备基础名找对应回环（Windows 会在回环名后加 " [Loopback]"）。

    只比基础名——设备名可能被驱动改写或加后缀（如 "G52 ... Aud" 截断）。
    返回 (index, name)；找不到返回 (None, None)。
    """
    if not friendly:
        return None, None
    key = friendly.strip()
    for i, name, _ch, _sr in list_loopback_devices():
        base = (name or "").replace(" [Loopback]", "").strip()
        if base == key or base.startswith(key[:16]) or key.startswith(base[:16]):
            return i, name
    return None, None
