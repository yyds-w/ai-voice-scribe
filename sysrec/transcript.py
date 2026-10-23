# -*- coding: utf-8 -*-
"""一次会话的转写稿：边转边落盘，内存里保留全文（不封顶）。

为什么单独成模块：
  字幕窗里的 `deque(maxlen=200)` 只是"显示缓存"——满了就静默丢掉最早的行。
  摘要靠的是**全文**，不能跟显示缓存共用那个容器。这里两件事分开：
    · 显示：字幕窗自己管事（想留多少行随它）
    · 记录：本模块负责，逐行 append 到磁盘 + 内存全文，退出/断电都不丢

文件格式（既是给人看的，也是喂给 LLM 的）：
    # 转写稿 2026-10-20 12:34:56

    ## [00:00:00]
    大家好，我们开始今天的评审。
    ...

每 5 分钟插一个 `## [mm:ss]` 锚点：比每行都打时间戳省 8 倍 token，
又足够让人（和模型）定位"这句话是会议哪一段说的"。
"""
from __future__ import annotations

import datetime
import os
import threading
import time

ANCHOR_SEC = 300          # 锚点间隔（秒）


def _hms(sec: float) -> str:
    s = max(0, int(sec))
    return "%02d:%02d:%02d" % (s // 3600, (s % 3600) // 60, s % 60)


class Transcript:
    """一份转写稿。线程安全（字幕回调线程写、GUI 线程读）。"""

    def __init__(self, path: str = ""):
        self.path = path
        self._lines = []                  # [(t_sec, text)] —— 内存全文
        self._lock = threading.RLock()
        self._fh = None
        self._t0 = None                   # monotonic 起点
        self._next_anchor = 0.0
        self._chars = 0
        self._closed = False

    # ---------- 生命周期 ----------

    @staticmethod
    def new_path(dir_path: str) -> str:
        os.makedirs(dir_path, exist_ok=True)
        now = datetime.datetime.now()
        return os.path.join(dir_path, "转写_%s.md" % now.strftime("%Y%m%d_%H%M%S"))

    def open(self, path: str = "", title: str = "") -> str:
        """开始记录（建文件 + 写头）。返回文件路径。"""
        with self._lock:
            if path:
                self.path = path
            if not self.path:
                raise ValueError("Transcript 没有给出路径")
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            self._fh = open(self.path, "w", encoding="utf-8")
            now = datetime.datetime.now()
            head = "# 转写稿 %s" % now.strftime("%Y-%m-%d %H:%M:%S")
            if title:
                head += "\n\n%s" % title
            self._fh.write(head + "\n")
            self._fh.flush()
            self._t0 = time.monotonic()
            self._next_anchor = 0.0
            self._lines = []
            self._chars = 0
            self._closed = False
            return self.path

    def add(self, text: str, t_sec: float = None) -> None:
        """追加一行定版文字（外部线程直接调，内部加锁）。"""
        t = " ".join((text or "").split())
        if not t:
            return
        with self._lock:
            if self._closed:
                return
            if t_sec is None:
                t_sec = (time.monotonic() - self._t0) if self._t0 else 0.0
            t_sec = float(t_sec)
            anchor = ""
            if t_sec >= self._next_anchor:            # 到点打一个时间锚
                anchor = "\n## %s\n" % _hms(t_sec)
                self._next_anchor = (int(t_sec) // ANCHOR_SEC + 1) * ANCHOR_SEC
            self._lines.append((t_sec, t))
            self._chars += len(t)
            fh = self._fh
            if fh is not None:
                try:
                    fh.write((anchor or "") + t + "\n")
                    fh.flush()                        # 崩了也只丢最后一句
                except Exception:                     # noqa: BLE001  落盘失败不影响上屏
                    pass

    def close(self) -> dict:
        """收尾落盘，返回统计。可重复调用。"""
        with self._lock:
            self._closed = True
            fh, self._fh = self._fh, None
            if fh is not None:
                try:
                    fh.close()
                except Exception:                     # noqa: BLE001
                    pass
            return {"path": self.path, "lines": len(self._lines), "chars": self._chars}

    # ---------- 读取 ----------

    def __len__(self) -> int:
        return len(self._lines)

    @property
    def chars(self) -> int:
        return self._chars

    @property
    def running(self) -> bool:
        return self._fh is not None and not self._closed

    def text(self, with_anchors: bool = True) -> str:
        """全文。with_anchors=True 时每 5 分钟带一个 `## [mm:ss]` 锚点。"""
        with self._lock:
            lines = list(self._lines)
        if not lines:
            return ""
        out = []
        next_anchor = 0.0
        for t_sec, txt in lines:
            if with_anchors and t_sec >= next_anchor:
                out.append("## %s" % _hms(t_sec))
                next_anchor = (int(t_sec) // ANCHOR_SEC + 1) * ANCHOR_SEC
            out.append(txt)
        return "\n".join(out)

    def plain(self) -> str:
        with self._lock:
            return "\n".join(t for _s, t in self._lines)

    # ---------- 读回旧稿 ----------

    @classmethod
    def load(cls, path: str) -> "Transcript":
        """把一个已落盘的转写稿读回内存对象。

        只取正文：`# 转写稿 …` 与它下面的标题行、以及 `## [mm:ss]` 锚点都跳过。
        锚点之前的行一律当头部丢掉（否则文件头会混进摘要里）；
        如果一个锚点都没有（极短稿），退化成"只跳过 # 开头的行"。
        """
        obj = cls(path)
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            raw = f.read()
        lines = raw.splitlines()
        has_anchor = any(s.strip().startswith("## ") for s in lines)
        started = not has_anchor
        cur_t = 0.0                      # 当前时间锚点（秒），读回后 text() 能复现锚点
        n = 0
        for line in lines:
            s = line.strip()
            if s.startswith("## "):
                started = True
                hh, mm, ss = 0, 0, 0
                try:
                    parts = s[3:].strip(" []").split(":")
                    hh, mm, ss = (list(map(int, parts)) + [0, 0, 0])[:3]
                except Exception:        # noqa: BLE001  锚点格式坏了就当 0
                    pass
                cur_t = float(hh * 3600 + mm * 60 + ss)
                continue
            if not started or not s:
                continue
            if s.startswith("#"):          # 头部标题（# 转写稿 / 设备信息）
                continue
            obj._lines.append((cur_t, s))
            obj._chars += len(s)
            n += 1
        if n == 0:
            raise ValueError("转写稿是空的：%s" % path)
        return obj
