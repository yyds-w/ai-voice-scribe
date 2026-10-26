# -*- coding: utf-8 -*-
"""文件命名（2026-10-24 三功能解耦：摘要缓存 / 摘要文件统一命名）。

用户 Q1 选定 B 格式：**时间在前**，目录排序即时间序（`YYYYmmdd-HHMMSS`）：
    摘要缓存：20261024-114833_ETF跟踪误差讨论.md
    摘要文件：20261024-114833_ETF跟踪误差讨论_摘要.md
标题：LLM 生成（≤20 字），这里负责剔除文件名非法字符、兜底"未命名"。

另含缓存清理（用户 Q4：保留 7 天，过期删除）。
"""
import os
import re
import time

# Windows 文件名非法字符（半角）+ 常见全角敏感字符 + 控制字符
_BAD = re.compile(r'[\\/:*?"<>|：／？＊\r\n\t\x00-\x1f]')
_TITLE_MAX = 20


def safe_title(s: str, max_len: int = _TITLE_MAX) -> str:
    """标题 → 可安全用于文件名的形式（去非法字符/首尾标点，截断 max_len）。"""
    t = _BAD.sub("", (s or "").strip())
    t = t.strip(' .。，、；：!！?？"\'“”‘’《》【】()（）')
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > max_len:
        t = t[:max_len].rstrip()
    return t


def stamp(ts: float = None) -> str:
    """时间戳 → 20261024-114833（秒级，保证同分钟内多次也不撞名）。"""
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(ts) if ts else time.localtime())


def cache_filename(title: str, ts: float = None, ext: str = ".md") -> str:
    """摘要缓存文件名：<时间>_<标题>.md"""
    return "%s_%s%s" % (stamp(ts), safe_title(title) or "未命名", ext)


def summary_filename(title: str, ts: float = None, ext: str = ".md") -> str:
    """摘要文件名：<时间>_<标题>_摘要.md"""
    return "%s_%s_摘要%s" % (stamp(ts), safe_title(title) or "未命名", ext)


def cleanup_cache(dir_path: str, keep_days: int, now: float = None) -> int:
    """删除 dir_path 下修改时间早于 keep_days 天的 .md；返回删除个数。"""
    if not dir_path or keep_days <= 0:
        return 0
    cutoff = (now if now is not None else time.time()) - keep_days * 86400
    n = 0
    try:
        names = os.listdir(dir_path)
    except OSError:
        return 0
    for name in names:
        if not name.lower().endswith(".md"):
            continue
        p = os.path.join(dir_path, name)
        try:
            if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                os.remove(p)
                n += 1
        except OSError:
            continue
    return n
