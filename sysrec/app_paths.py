# -*- coding: utf-8 -*-
"""路径解析：把「只读资源」和「可写数据」分开。

为什么需要：
  · 源码模式（pythonw run.py）下两者的根都是项目目录，行为与改造前**完全一致**；
  · PyInstaller onedir 打包后，``sys._MEIPASS`` 指向 ``_internal``（模型/图标随包进这里），
    而 ``config.json``、``录音``、``转写``、``摘要`` 必须落在 **exe 旁边**
    —— 用户看得见、能改、不涉及 Program Files 的写权限。

规律（同 AI面试软件 app_paths 的做法）：
  · 只读资源（models/、assets/）→ resource_path()
  · 可写数据（config.json、输出目录、startup.log）→ data_path()
"""
from __future__ import annotations

import os
import sys


def _source_root() -> str:
    """源码模式的根：<项目>/sysrec/app_paths.py 往上两级。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_path(*parts: str) -> str:
    """只读资源根：冻结后为 sys._MEIPASS（onedir 的 _internal），源码模式为项目根。"""
    if _frozen():
        base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(sys.executable))
    else:
        base = _source_root()
    return os.path.join(base, *parts) if parts else base


def data_path(*parts: str) -> str:
    """可写数据根：冻结后为 exe 所在目录，源码模式为项目根。"""
    if _frozen():
        base = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base = _source_root()
    return os.path.join(base, *parts) if parts else base
