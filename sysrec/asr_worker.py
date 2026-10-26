# -*- coding: utf-8 -*-
"""摘要转写子进程（2026-10-25 用户方案 B）。

【为什么单独一个进程】onnxruntime 的 CUDA 上下文一旦创建就常驻：实测把
sherpa 的 recognizer 释放掉，进程 RSS 仍残留 160~320MB、显存也不归还，
只能靠进程退出才彻底归还。所以把"读音频 + 转写"整体搬到子进程：
转写完子进程退出，内存/显存全部还给系统，主界面进程始终 ~72MB。

【协议】stdin/stdout 各一行 JSON（UTF-8，行缓冲）：
  → {"cmd":"warm","provider":"auto","model_dir":""}
  ← {"ok":true,"cmd":"warm","provider":"cuda"}
  → {"cmd":"job","files":["a.mp3"],"provider":"auto","model_dir":""}
  ← {"ok":true,"text":"...","secs":12.3,"audio_secs":600.0,"provider":"cuda"}
  → {"cmd":"quit"}
任何异常都回 {"ok":false,"error":"..."}，不写别的到 stdout（避免污染协议）。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time


def _punct_long(pu, raw: str, size: int = 120) -> str:
    """长文本加标点：分块跑再拼接（2026-10-26）。

    CT-Transformer 单次输入建议 ≤200 字；整篇 1400 字一次喂进去注意力会退化
    （实测断句明显变差）。这里按 ~120 字切块，块边界尽量落在原逗号处；
    任一块失败就退回原文，绝不吞字。
    """
    if not raw:
        return ""
    if len(raw) <= size:
        return (pu.add(raw) or raw).strip()
    out = []
    start = 0
    while start < len(raw):
        end = min(len(raw), start + size)
        if end < len(raw):                       # 尽量在逗号处断开
            cut = raw.rfind("，", start, end)
            if cut > start + size // 2:
                end = cut + 1
        seg = raw[start:end]
        out.append((pu.add(seg) or seg).strip())
        start = end
    return "".join(out)


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    # Windows 下管道默认用系统代码页，中文会乱码——强制 UTF-8 + 行缓冲
    try:
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:                              # noqa: BLE001
        pass

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from sysrec.asr import Asr, probe_audio_seconds, read_audio_files

    asr = None
    _punct_holder = [None]          # 标点模型（懒加载，子进程内复用）

    def _asr(provider: str, model_dir: str, model_type: str = "") -> "Asr":
        nonlocal asr
        if asr is None:
            asr = Asr(model_dir=(model_dir or None),
                      provider=(provider or "auto"),
                      model_type=(model_type or "paraformer"))
        return asr

    for line in sys.stdin:
        line = (line or "").strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:                          # noqa: BLE001
            _emit({"ok": False, "error": "bad json"})
            continue
        cmd = req.get("cmd")
        try:
            if cmd == "warm":
                a = _asr(req.get("provider"), req.get("model_dir"))
                a.warmup()
                _emit({"ok": True, "cmd": "warm", "provider": a.provider})
            elif cmd == "job":
                files = list(req.get("files") or [])
                a = _asr(req.get("provider"), req.get("model_dir"))
                audio_secs = probe_audio_seconds(files)
                t0 = time.time()
                audio, secs = read_audio_files(files)
                if audio is None or not len(audio):
                    _emit({"ok": False, "error": "读不到音频内容"})
                    continue
                text = (a.transcribe(audio, skip_vad=True) or "").strip()
                # 2026-10-26：转写稿补标点（SenseVoice 只出少量逗号/问号、几乎没句号，
                # 长文连成一坨没法读）。标点模型 75MB/纯 CPU，放在子进程里跑，
                # 主界面进程保持轻量；失败就原样返回，绝不吞字。
                if text and req.get("punct", True):
                    try:
                        from sysrec.punctuate import Punctuator
                        pu = _punct_holder[0]
                        if pu is None:
                            pu = _punct_holder[0] = Punctuator()
                        if pu.ready():
                            # 关键：CT-Transformer 期望**无标点**输入。SenseVoice 会吐少量
                            # 逗号/问号，直接喂会出现「，，」「。，」这种叠标点（实测）。
                            # 先剥掉所有标点 → 再让模型统一补，输出才干净。
                            raw = re.sub(r"[，。！？、；：…,\.!?;:]", "", text)
                            marked = _punct_long(pu, raw)
                            if marked and len(marked) >= len(raw) * 0.6:
                                text = marked
                    except Exception:                      # noqa: BLE001
                        pass
                _emit({"ok": True, "text": text,
                       "secs": time.time() - t0,
                       "audio_secs": secs or audio_secs,
                       "provider": a.provider})
            elif cmd == "quit":
                break
            else:
                _emit({"ok": False, "error": "unknown cmd: %r" % (cmd,)})
        except Exception as e:                     # noqa: BLE001
            _emit({"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:300])})
    return 0


if __name__ == "__main__":
    sys.exit(main())
