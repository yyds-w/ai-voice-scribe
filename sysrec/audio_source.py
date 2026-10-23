# -*- coding: utf-8 -*-
"""录音来源识别：枚举音频会话 → 轮询电平 → 判定"哪个应用在出声"。

## 能力边界（2026-10-23 需求确认，写在最前面）
  · 只能知道"**哪个进程**有音频会话、当前电平多少"——录到的仍是系统混音，
    无法只录某一个应用；浏览器多标签会合并成一个会话（进程名 chrome.exe）。
  · 会话枚举用 pycaw（AudioUtilities.GetAllSessions = 默认渲染设备的会话），
    电平用会话对象上的 IAudioMeterInformation.GetPeakValue()（0..1 峰值）。
    （实测：QueryInterface 在会话对象上可用；本机 Realtek 默认输出验证通过。）
  · "点录音的瞬间可能没有声音"（先点录音、再打开播放）→ 名字在**停止时**才定
    （Q1=C：录制全程用临时名）；检测不到应用就回落"系统声音"（Q4=A）。

## 判定规则（用户 2026-10-23 确认）
  · 第一个电平超过阈值的应用即"来源应用"，之后**不换**（Q2=A）；
  · 同一时刻多个应用超阈值 → 取**电平最高**的（Q3=A）；
  · pid=0 的"系统声音"会话（MessageBeep / 系统提示音走这里，实测）无进程、
    不可归因，直接忽略——这正是 Q4 里"只有系统提示音"时回落系统声音名的方式。

## 线程与降级
  · 轮询跑在 daemon 线程里（默认 5 次/秒，单次开销 = 枚举会话 + 各取一次
    GetPeakValue，微秒级）；**任何异常都不许影响录音**——枚举失败当"本轮无声"。
  · pycaw/comtypes 不在时（非 Windows / 依赖缺失）：tracker 直接不可用，
    调用方看到 app_name=None → 走"系统声音"回落名，录音照常。
"""
from __future__ import annotations

import ctypes
import os
import struct
import threading
import time

DEFAULT_THRESHOLD = 0.01     # 峰值阈值（≈ -40 dBFS）：低于此视为"没出声"
POLL_INTERVAL = 0.2          # 轮询间隔（秒）
FALLBACK_NAME = "系统声音"    # 检测不到来源时的文件名（Q4=A）

_ILLEGAL_CHARS = '\\/:*?"<>|'   # Windows 文件名非法字符（Q9=A）


# ---------------- 名字清洗（Q9=A） ----------------

def sanitize_app_name(name: str, limit: int = 30) -> str:
    """把应用名清洗成合法的 Windows 文件名片段。

    规则（用户确认）：非法字符替换成下划线；超长截断到 limit 个字；
    另外去掉首尾空格与点（Windows 文件名不能以点/空格结尾，属于同类卫生问题）。
    清洗后为空串时返回 ""（调用方自行回落）。
    """
    s = (name or "").strip()
    if not s:
        return ""
    s = "".join("_" if ch in _ILLEGAL_CHARS else ch for ch in s)
    s = s.strip(" .")                      # 首尾点/空格在 Windows 上不合法
    if len(s) > limit:
        s = s[:limit].rstrip(" .")
    return s


# ---------------- 友好名解析（Q5=A） ----------------

_DESC_CACHE: dict = {}          # exe 路径(小写) → FileDescription（进程存活期内有效）
_DESC_LOCK = threading.Lock()


def file_description(exe_path: str) -> str:
    """读 PE 的 FileDescription（国产软件的中文产品名大多在这里）；读不到返回 ""。

    用 version.dll 的 GetFileVersionInfoW + VerQueryValueW（不依赖第三方包）。
    结果按 exe 路径缓存——轮询每秒可能查几次，版本信息不会变。
    """
    if not exe_path:
        return ""
    key = exe_path.lower()
    with _DESC_LOCK:
        if key in _DESC_CACHE:
            return _DESC_CACHE[key]
    desc = _read_file_description(exe_path)
    with _DESC_LOCK:
        _DESC_CACHE[key] = desc
    return desc


def _read_file_description(exe_path: str) -> str:
    if os.name != "nt" or not os.path.exists(exe_path):
        return ""
    try:
        ver = ctypes.windll.version
        size = ver.GetFileVersionInfoSizeW(exe_path, None)
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not ver.GetFileVersionInfoW(exe_path, 0, size, buf):
            return ""
        plang = ctypes.c_void_p()
        nlang = ctypes.c_uint()
        if not ver.VerQueryValueW(buf, r"\VarFileInfo\Translation",
                                  ctypes.byref(plang), ctypes.byref(nlang)):
            return ""
        raw = ctypes.string_at(plang.value, nlang.value)
        # Translation 是 (lang, codepage) DWORD 对数组；逐个语言试，取第一个非空
        for i in range(0, max(0, len(raw) - 3), 4):
            lang_id = struct.unpack_from("<H", raw, i)[0]
            cp_id = struct.unpack_from("<H", raw, i + 2)[0]
            query = r"\StringFileInfo\%04X%04X\FileDescription" % (lang_id, cp_id)
            pval = ctypes.c_void_p()
            nval = ctypes.c_uint()
            if not ver.VerQueryValueW(buf, query, ctypes.byref(pval), ctypes.byref(nval)):
                continue
            value = ctypes.wstring_at(pval.value).strip()
            if value:
                return value
    except Exception:  # noqa: BLE001  读不到就当没有，绝不抛给录音线程
        return ""
    return ""


def _process_name(proc) -> str:
    """psutil.Process 的 exe 名去掉 .exe（FileDescription 读不到时的回落）。"""
    try:
        base = os.path.basename(proc.exe() or "") or (proc.name() or "")
    except Exception:  # noqa: BLE001  进程已退出 / 拒绝访问
        try:
            base = proc.name() or ""
        except Exception:  # noqa: BLE001
            base = ""
    return os.path.splitext(base)[0] if base else ""


def session_app_name(session) -> str:
    """会话 → 友好应用名（未清洗）；无进程会话（系统声音）返回 ""。

    优先级（Q5=A）：exe 的 FileDescription（产品名）→ 进程名去掉 .exe。
    """
    proc = getattr(session, "Process", None)
    if proc is None:
        return ""                          # pid=0 系统声音会话：不可归因
    try:
        exe = proc.exe()
    except Exception:  # noqa: BLE001
        exe = ""
    desc = file_description(exe) if exe else ""
    if desc:
        return desc
    return _process_name(proc)


# ---------------- 会话枚举 + 电平 ----------------

def _default_session_provider():
    """返回 [(app_name, peak), ...]：默认渲染设备上所有**有进程**会话的当前电平。

    任何单点失败（进程退出、COM 抖动）都跳过该会话，不影响其它会话与录音。
    """
    from pycaw.api.endpointvolume import IAudioMeterInformation
    from pycaw.pycaw import AudioUtilities

    out = []
    for session in AudioUtilities.GetAllSessions():
        try:
            if session.Process is None:
                continue                    # 系统声音（pid=0）等无主会话
            peak = session._ctl.QueryInterface(
                IAudioMeterInformation).GetPeakValue()
        except Exception:  # noqa: BLE001
            continue
        out.append((session_app_name(session), float(peak)))
    return out


class AudioSourceTracker:
    """轮询音频会话电平，锁定"第一个出声的应用"（锁定后不再更换）。

    用法：
        t = AudioSourceTracker()
        t.start()                # 起 daemon 线程
        ...                      # 录音进行中
        t.stop()                 # 停止轮询（join，最多 ~1 秒）
        t.app_name               # None = 全程没检测到应用（调用方回落系统声音）
    """

    def __init__(self, threshold: float = DEFAULT_THRESHOLD,
                 interval: float = POLL_INTERVAL, on_event=None,
                 session_provider=None):
        self.threshold = float(threshold)
        self.interval = max(0.02, float(interval))
        self.on_event = on_event                 # on_event(kind, payload)
        self._provider = session_provider or _default_session_provider
        self._app = None                          # 锁定的应用名（未清洗）
        self._locked = False                      # 是否已锁定（锁定后不更换）
        self._lock_peak = 0.0
        self._locked_at = 0.0
        self._stop_evt = threading.Event()
        self._thread = None
        self._error = ""                          # 初始化失败原因（不可用时）

    # ---------- 对外 ----------

    def start(self) -> bool:
        """启动轮询线程。依赖不可用（无 pycaw 等）时返回 False 且不抛异常。"""
        if self._thread is not None:
            return True
        try:
            probe = self._provider()             # 先探一次：缺依赖在这里就现形
        except Exception as e:  # noqa: BLE001   pycaw/comtypes 缺失或 COM 初始化失败
            self._error = "%s: %s" % (type(e).__name__, e)
            return False
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._loop, name="sysrec-src",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 1.0) -> None:
        """停止轮询并 join（超时也不阻塞——线程是 daemon，且名字已锁定在内存里）。"""
        self._stop_evt.set()
        th, self._thread = self._thread, None
        if th is not None and th is not threading.current_thread():
            th.join(timeout=max(0.0, timeout))

    @property
    def app_name(self):
        """锁定的来源应用名；None = 没检测到（回落"系统声音"）。"""
        return self._app

    @property
    def error(self) -> str:
        return self._error

    def snapshot(self) -> dict:
        """给 UI / 测试看的状态（简单读，线程安全）。"""
        return {
            "running": self._thread is not None,
            "locked": self._app is not None,
            "app": self._app,
            "peak_at_lock": self._lock_peak,
            "locked_at": self._locked_at,
            "error": self._error,
        }

    # ---------- 内部 ----------

    def _emit(self, kind: str, payload) -> None:
        if self.on_event:
            try:
                self.on_event(kind, payload)
            except Exception:  # noqa: BLE001
                pass

    def _loop(self) -> None:
        # 【为什么无条件先轮询一次】start() 后立刻 stop()（秒录秒停 / 单测）时，
        # 线程可能还没被调度到——一次都不轮就退出会丢判定（明明有声音却回落
        # "系统声音"）。因此循环体至少完整执行一次；停止信号只在每轮末尾生效，
        # 保证 stop() 最多多等一个轮询周期（provider 卡住时由 join 超时兜底）。
        while True:
            try:
                rows = self._provider()
            except Exception:  # noqa: BLE001  本轮枚举失败 → 当无声，下轮再来
                rows = []
            if not self._locked:
                # Q3=A：同一轮里多个应用超阈值 → 取电平最高的
                hot = [(pk, nm) for nm, pk in rows if nm and pk >= self.threshold]
                if hot:
                    peak, name = max(hot)
                    self._app = name
                    self._lock_peak = float(peak)
                    self._locked_at = time.time()
                    self._emit("locked", {"app": name, "peak": float(peak)})
                    return                   # Q2=A：锁定后整个录音期不换名，退出轮询
            if self._stop_evt.is_set():
                return
            self._stop_evt.wait(self.interval)
