# -*- coding: utf-8 -*-
"""摘要转写子进程客户端（2026-10-25 用户方案 B 的父进程侧）。

主界面进程永不 import sherpa-onnx / onnxruntime：转写全在 `asr_worker` 子进程里，
子进程退出即把模型、CUDA 上下文、显存全部还给系统（实测主进程稳定 ~72MB）。

用法（都在后台线程里调，别在 GUI 线程阻塞）：
    cli = AsrClient(provider="auto")
    cli.warm()                      # 起子进程并预加载模型（≈2s）
    text, secs, audio_secs = cli.transcribe(["a.mp3"])   # 转写
    cli.stop()                      # 杀子进程（3 分钟闲置时由 GUI 定时器调）
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading


class AsrClientError(RuntimeError):
    """子进程不可用 / 任务失败。"""


_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


class AsrClient:
    def __init__(self, provider: str = "auto", model_dir: str = "",
                 model_type: str = "paraformer"):
        self.provider_pref = (provider or "auto").strip().lower()
        self.model_dir = model_dir or ""
        self.model_type = model_type or "paraformer"
        self.provider = "cpu"          # 子进程回报的实际 provider
        self._proc = None
        self._q = None
        self._reader = None
        self._lock = threading.Lock()

    # ---------- 进程管理 ----------
    @staticmethod
    def _cmdline() -> list:
        if getattr(sys, "frozen", False):          # 打包后：exe --asr-worker
            return [sys.executable, "--asr-worker"]
        return [sys.executable, "-m", "sysrec.asr_worker"]

    def _spawn(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env.setdefault("PYTHONUTF8", "1")
        self._proc = subprocess.Popen(
            self._cmdline(),
            cwd=base if not getattr(sys, "frozen", False) else None,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            env=env, creationflags=_CREATE_NO_WINDOW)
        self._q = queue.Queue()
        proc, q = self._proc, self._q

        def _reader():
            try:
                for line in proc.stdout:
                    line = (line or "").strip()
                    if line:
                        try:
                            q.put(json.loads(line))
                        except Exception:          # noqa: BLE001  非协议输出丢弃
                            pass
            except Exception:                      # noqa: BLE001
                pass
            q.put(None)                            # EOF 标记

        self._reader = threading.Thread(target=_reader, daemon=True,
                                        name="asr-client-reader")
        self._reader.start()

    def _send(self, obj: dict) -> None:
        if self._proc is None or self._proc.poll() is not None:
            raise AsrClientError("转写子进程未运行")
        try:
            self._proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except Exception as e:                     # noqa: BLE001
            raise AsrClientError("写入子进程失败：%s" % e)

    def _recv(self, timeout: float) -> dict:
        try:
            obj = self._q.get(timeout=timeout)
        except queue.Empty:
            raise AsrClientError("转写子进程超时（%.0fs）" % timeout)
        if obj is None:
            raise AsrClientError("转写子进程意外退出")
        if not obj.get("ok"):
            raise AsrClientError(obj.get("error") or "转写子进程报错")
        return obj

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ---------- 对外接口 ----------
    def warm(self, timeout: float = 120.0) -> str:
        """起子进程并预加载模型（阻塞，返回实际 provider）。"""
        with self._lock:
            self._spawn()
            self._send({"cmd": "warm", "provider": self.provider_pref,
                        "model_dir": self.model_dir,
                        "model_type": self.model_type})
            obj = self._recv(timeout)
            self.provider = obj.get("provider") or "cpu"
            return self.provider

    def transcribe(self, files, timeout: float = 3600.0):
        """转写一组音频文件 → (text, 解码秒数, 音频秒数)。"""
        with self._lock:
            self._spawn()
            self._send({"cmd": "job", "files": list(files),
                        "provider": self.provider_pref, "model_dir": self.model_dir,
                        "model_type": self.model_type})
            obj = self._recv(timeout)
            self.provider = obj.get("provider") or self.provider
            return (obj.get("text") or "", float(obj.get("secs") or 0.0),
                    float(obj.get("audio_secs") or 0.0))

    def stop(self) -> None:
        """结束子进程（幂等）。"""
        proc = self._proc
        self._proc = None
        self._q = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.stdin.write('{"cmd":"quit"}\n')
                proc.stdin.flush()
                proc.wait(timeout=2.0)
        except Exception:                          # noqa: BLE001
            pass
        try:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=2.0)
        except Exception:                          # noqa: BLE001
            pass
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:                          # noqa: BLE001
            pass
