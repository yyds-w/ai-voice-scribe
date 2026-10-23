# -*- coding: utf-8 -*-
"""标点恢复（CT-Transformer 中英双语 int8 ONNX）。

为什么需要它：流式 Paraformer 只吐字、不吐标点（实测 60s 输出 0 个标点），
所有字连成一堵墙。这个模型专门补「，。？！」。

性能（本机实测）：加载 0.24s，单次 3~7ms（180 字以内），纯 CPU。
模型：models/punct/model.int8.onnx（75MB，Apache 2.0，阿里达摩院 CT-Transformer）
"""
import os
import threading

from . import app_paths


def _app_dir() -> str:
    """只读资源根：源码模式=项目目录；打包后=_MEIPASS（_internal）。"""
    return app_paths.resource_path()


MODEL_DIR = app_paths.resource_path("models")
MODEL_PATH = os.path.join(MODEL_DIR, "punct", "model.int8.onnx")


class Punctuator:
    """懒加载；任何异常都原样返回文本，绝不吞字。"""

    def __init__(self, model_path=None, threads=2):
        self.model_path = os.path.expandvars(model_path or MODEL_PATH)
        self._threads = max(1, int(threads))
        self._p = None
        self._lock = threading.Lock()

    def ready(self) -> bool:
        return os.path.exists(self.model_path)

    def ensure(self):
        if self._p is not None:
            return self._p
        with self._lock:
            if self._p is not None:
                return self._p
            if not self.ready():
                raise RuntimeError("找不到标点模型：%s" % self.model_path)
            import sherpa_onnx
            cfg = sherpa_onnx.OfflinePunctuationConfig(
                model=sherpa_onnx.OfflinePunctuationModelConfig(
                    ct_transformer=self.model_path,
                    num_threads=self._threads,
                    provider="cpu",
                )
            )
            self._p = sherpa_onnx.OfflinePunctuation(cfg)
            return self._p

    def add(self, text: str) -> str:
        t = (text or "").strip()
        if not t:
            return ""
        try:
            out = self.ensure().add_punctuation(t)
            return (out or t).strip()
        except Exception:                       # noqa: BLE001
            return t
