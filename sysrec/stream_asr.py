# -*- coding: utf-8 -*-
"""流式 ASR（sherpa-onnx OnlineRecognizer + 流式 Paraformer，中英双语）。

与 asr.py（SenseVoice 整段识别）互补：
  · 本模块负责"边说边出"的草稿——增量解码，**前缀稳定**（不是整段重猜）；
  · asr.py 负责句子定版（整句高精度重识别）。

模型目录：本工具 ``models/streaming-paraformer/``
（encoder.int8.onnx / decoder.int8.onnx / tokens.txt，来自 hf-mirror 官方仓库）。
"""
from __future__ import annotations

import os
import threading

import numpy as np

from . import app_paths

SR = 16000


def _app_dir() -> str:
    """只读资源根：源码模式=项目目录；打包后=_MEIPASS（_internal）。"""
    return app_paths.resource_path()


MODEL_DIR = os.path.join(_app_dir(), "models", "streaming-paraformer")
ENCODER = os.path.join(MODEL_DIR, "encoder.int8.onnx")
DECODER = os.path.join(MODEL_DIR, "decoder.int8.onnx")
TOKENS = os.path.join(MODEL_DIR, "tokens.txt")


class StreamingASR:
    """流式识别器（懒加载；session = 一条独立的识别流）。"""

    def __init__(self, threads: int = 2):
        self._threads = max(1, int(threads))
        self._rec = None
        self._lock = threading.Lock()

    def ready(self) -> bool:
        return all(os.path.exists(p) for p in (ENCODER, DECODER, TOKENS))

    def ensure(self):
        if self._rec is not None:
            return self._rec
        with self._lock:
            if self._rec is not None:
                return self._rec
            if not self.ready():
                raise RuntimeError("流式模型不完整：%s" % MODEL_DIR)
            import sherpa_onnx
            self._rec = sherpa_onnx.OnlineRecognizer.from_paraformer(
                encoder=ENCODER,
                decoder=DECODER,
                tokens=TOKENS,
                num_threads=self._threads,
                sample_rate=SR,
                feature_dim=80,
                decoding_method="greedy_search",
            )
            return self._rec

    def new_session(self):
        return self.ensure().create_stream()

    def feed(self, session, audio16k: np.ndarray) -> None:
        """喂一块 16k 音频并做增量解码（每块一次；CPU 只处理新帧）。"""
        rec = self.ensure()
        a = np.ascontiguousarray(audio16k, dtype=np.float32)
        if a.size == 0:
            return
        session.accept_waveform(SR, a)
        while rec.is_ready(session):
            rec.decode_stream(session)

    def result(self, session) -> str:
        try:
            return (self.ensure().get_result(session) or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    def reset(self, session) -> None:
        try:
            self.ensure().reset(session)
        except Exception:  # noqa: BLE001
            pass
