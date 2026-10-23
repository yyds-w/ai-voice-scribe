# -*- coding: utf-8 -*-
"""编码器：MP3（lameenc 流式）/ WAV（标准库 wave）。

统一约定：
  - 输入 float32、范围 [-1, 1]、形状 (N,) 或 (N, channels)，交错内存布局由
    numpy 保证（astype(int16).tobytes() 即标准 interleaved PCM）；
  - write() 可增量调用任意多次；close() 落尾并关文件；
  - 统一输出 16bit PCM（MP3 与 WAV 相同的位深，避免中途变格式）。
"""
from __future__ import annotations

import wave

import lameenc
import numpy as np


def _to_int16(samples: np.ndarray) -> bytes:
    x = np.clip(samples, -1.0, 1.0)
    return (x * 32767.0).astype(np.int16).tobytes()


class WavEncoder:
    ext = "wav"

    def __init__(self, path: str, sample_rate: int, channels: int):
        self._w = wave.open(path, "wb")
        self._w.setnchannels(channels)
        self._w.setsampwidth(2)          # 16bit
        self._w.setframerate(sample_rate)

    def write(self, samples: np.ndarray) -> None:
        if samples.size:
            self._w.writeframes(_to_int16(samples))

    def close(self) -> None:
        # wave 模块在 close 时回填正确的帧数头，正常关闭即可得到合法文件
        self._w.close()


class Mp3Encoder:
    ext = "mp3"

    def __init__(self, path: str, sample_rate: int, channels: int, bitrate: int = 192):
        self._f = open(path, "wb")
        self._e = lameenc.Encoder()
        self._e.set_bit_rate(int(bitrate))
        self._e.set_in_sample_rate(int(sample_rate))
        self._e.set_channels(int(channels))
        self._e.set_quality(2)           # 0 = 最慢最好, 9 = 最快；2 为高质量档

    def write(self, samples: np.ndarray) -> None:
        if samples.size:
            data = self._e.encode(_to_int16(samples))
            if data:
                self._f.write(data)

    def close(self) -> None:
        try:
            tail = self._e.flush()
            if tail:
                self._f.write(tail)
        except RuntimeError:
            # 整段一个样本都没写过（秒录秒停 / 设备未就绪）时 lameenc 的 flush
            # 会抛 "Not currently encoding"——空段也必须能正常收尾，不能炸保存流程
            pass
        finally:
            self._f.close()


def normalize_fmt(fmt: str) -> str:
    """把格式名规范化到受支持的集合（未知一律回退 mp3）。"""
    f = (fmt or "mp3").lower().strip()
    return "wav" if f == "wav" else "mp3"


def make_encoder(fmt: str, path: str, sample_rate: int, channels: int, bitrate: int = 192):
    """按格式名创建编码器；未知格式回退为 MP3。"""
    if normalize_fmt(fmt) == "wav":
        return WavEncoder(path, sample_rate, channels)
    return Mp3Encoder(path, sample_rate, channels, bitrate)
