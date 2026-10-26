# -*- coding: utf-8 -*-
"""图形界面：主窗口 + 系统托盘。

线程模型：Recorder 的 writer / monitor / 采集回调线程都会触发 on_event，
此处统一转成 Qt 信号（emit 线程安全），槽在 GUI 主线程执行；
录音状态用 QTimer（100ms）轮询 snapshot 刷新——简单且不会漏帧。
"""
from __future__ import annotations

import inspect
import math
import os
import re
import time
import shutil
import threading


from PyQt6.QtCore import QDateTime, QPoint, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QCursor, QIcon, QLinearGradient, QPainter, QPen
from PyQt6.QtWidgets import (QApplication, QComboBox, QDialog, QFileDialog,
                             QHBoxLayout, QLabel, QMainWindow, QMenu,
                             QPushButton, QSystemTrayIcon,
                             QVBoxLayout, QWidget)

from . import app_paths
from . import config as config_mod
from . import devices
from . import msgbox
from .recorder import Recorder, RecorderConfig
from .panel import AiPanel
from .settings_dialog import SettingsDialog
from .subtitle import SubtitleEngine
from .subtitle_window import SubtitleWindow

FORMATS = [
    ("MP3 · 192 kbps", "mp3", 192),
    ("MP3 · 320 kbps", "mp3", 320),
    ("WAV · 无损", "wav", 0),
]

STYLE = """
QMainWindow { background: #FAF7F0; }
QLabel { color: #2E2B26; font-size: 13px; }
QLabel#time { font-size: 40px; font-weight: 400; color: #26221D; }
QLabel#status { color: #6E675C; font-size: 12px; }
QLabel#statusWarn { color: #9E3B2E; font-size: 12px; }
QComboBox { background: #FFFFFF; border: 1px solid #E0D8CA; border-radius: 6px;
            padding: 5px 8px; color: #2E2B26; font-size: 13px; }
QComboBox:disabled { color: #A8A094; background: #F3EFE6; }
QPushButton { background: #FFFFFF; border: 1px solid #E0D8CA; border-radius: 8px;
              padding: 6px 12px; color: #2E2B26; font-size: 13px; }
QPushButton:hover { background: #F7F3EA; }
QPushButton:disabled { color: #A8A094; background: #F3EFE6; }
QPushButton#main { background: #26221D; border: none; border-radius: 8px;
                   font-size: 15px; color: #FAF7F0; }
QPushButton#main:hover { background: #3A342C; }
QPushButton#main[recording="true"] { background: #9E3B2E; color: #FAF7F0; }
QPushButton#main[recording="true"]:hover { background: #B0432F; }
QPushButton#sub { background: #FFFFFF; border: 1px solid #E0D8CA; border-radius: 8px;
                  padding: 7px 12px; color: #2E2B26; font-size: 13px; }
QPushButton#sub:hover { background: #F7F3EA; }
QPushButton#sub:checked { background: #26221D; border: 1px solid #26221D; color: #FAF7F0; }
QPushButton#ghost { background: transparent; border: none; color: #6E675C;
                    padding: 4px 8px; font-size: 12px; }
QPushButton#ghost:hover { color: #2E2B26; background: #F0EBE0; border-radius: 6px; }
/* 下拉列表（QComboBox 的 popup 是独立顶层窗口，不继承上面的 combo 样式）。
   【为什么必须显式定义】深色系统主题下，未定义时 popup 的普通行会取到
   "白背景 + 系统浅色文字"的组合 → 整列看不见（2026-10-16 用户实测 + 复现）。 */
QComboBox QAbstractItemView {
    background-color: #FFFFFF;
    color: #2E2B26;
    border: 1px solid #E0D8CA;
    selection-background-color: #EFE7D8;
    selection-color: #26221D;
    outline: 0;
}
QComboBox QAbstractItemView::item {
    min-height: 24px;
    padding: 2px 6px;
}
QComboBox QAbstractItemView::item:hover {
    background-color: #F7F3EA;
}
"""




def _dbfs(x: float) -> float:
    return 20.0 * math.log10(x) if x > 1e-9 else -120.0


def _fmt_hms(seconds: float) -> str:
    s = int(seconds)
    return "%02d:%02d:%02d" % (s // 3600, (s % 3600) // 60, s % 60)


#: 句末标点切分（预编译：_paragraphs 每次摘要都会调用）
_SENT_SPLIT_RE = re.compile("(?<=[。！？；!?;])")
#: 标题首行 "# xxx"
_MD_TITLE_RE = re.compile(r"^#+\s*(.+)$")
#: 去掉尾部的"（时间）"
_TITLE_TS_RE = re.compile(r"[（(]\d{4}-\d{2}-\d{2}[^)）]*[)）]\s*$")


def _paragraphs(text: str, max_chars: int = 170) -> str:
    """转写稿按句末标点切句、再聚成段落（2026-10-26）。

    SenseVoice 吐出来的几乎只有逗号，一整篇连成一堵墙；标点模型补上句号后，
    这里按「句号/问号/叹号/分号」切句，攒够 ~170 字成一段，段落间空一行
    （170 是对齐云端 ASR 的分段粒度：云端同素材 25 段、平均 174 字/段）。
    纯字符串处理，不加载任何模型；异常或没有句末标点时原样返回。
    """
    try:
        t = (text or "").strip()
        if not t:
            return text
        sents = [s.strip() for s in _SENT_SPLIT_RE.split(t) if s.strip()]
        if len(sents) <= 1:
            return text                      # 没有句末标点 → 分段没意义
        paras, cur = [], ""
        for s in sents:
            cur += s
            if len(cur) >= max_chars:
                paras.append(cur)
                cur = ""
        if cur:
            paras.append(cur)
        return "\n\n".join(paras) if paras else text
    except Exception:                         # noqa: BLE001
        return text


def electron_safe_env() -> dict:
    """剔除会破坏 Electron 应用启动的环境变量（2026-10-26 实测根因）。

    `ELECTRON_RUN_AS_NODE=1` 会让 Electron **以纯 Node.js 模式运行**：不创建
    窗口、main 脚本跑完即退出，进程返回码 1 —— 外部表现就是"点了 Typora
    没反应 / 一闪就没"。若本进程是从带该变量的环境（如某些 IDE / 终端 /
    自动化沙箱）启动的，它再拉起 Typora 也会继承该变量而失败。
    所以启动外部程序时统一用这份净化过的环境。
    """
    bad = ("ELECTRON_RUN_AS_NODE", "NODE_OPTIONS", "NODE_EXTRA_CA_CERTS")
    try:
        return {k: v for k, v in os.environ.items() if k.upper() not in bad}
    except Exception:                       # noqa: BLE001
        return dict(os.environ)


def focus_window_by_title(hint: str, timeout: float = 8.0) -> bool:
    """把"标题含 hint"的可见窗口提到前台（2026-10-26）。

    为什么需要：面板常驻置顶（WindowStaysOnTopHint）+ Typora 冷启动 2~4 秒，
    只靠"临时取消置顶"仍会出现"程序起来了却被面板挡住"。这里在后台轮询最多
    timeout 秒，找到就 SW_RESTORE + SetForegroundWindow。返回是否成功（供自测）。
    """
    if not hint:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        found = []

        def _cb(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd):
                n = user32.GetWindowTextLengthW(hwnd)
                if n:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    user32.GetWindowTextW(hwnd, buf, n + 1)
                    if hint in buf.value:
                        found.append(hwnd)
            return True

        deadline = time.time() + float(timeout)
        while time.time() < deadline:
            del found[:]
            user32.EnumWindows(EnumProc(_cb), 0)
            if found:
                hwnd = found[0]
                user32.ShowWindow(hwnd, 9)            # SW_RESTORE
                user32.SetForegroundWindow(hwnd)
                return True
            time.sleep(0.4)
    except Exception:                                 # noqa: BLE001
        return False
    return False




def _fallback_title(text: str) -> str:
    """LLM 起名失败时的兜底标题（2026-10-26）。

    取正文第一句（截断到 20 字、去掉标点）——比"未命名"有意义得多，
    用户能从文件名看出这是哪段录音/哪期视频。
    """
    t = (text or "").strip()
    if not t:
        return ""
    first = t.split(chr(10))[0]
    first = re.split("[。！？!?]", first, maxsplit=1)[0]
    first = re.sub("[^0-9A-Za-z\u4e00-\u9fff]", "", first)
    return first[:20]


def _voice_boost_mode(v) -> str:
    """配置里的人声增强档位 → RecorderConfig.voice_boost。

    2026-10-26：从布尔改成三选一字符串（off/mild/strong）。
    旧配置是 True/False，这里兼容一次（True → strong，即当时定档的档位）。
    """
    if isinstance(v, bool):
        return "strong" if v else "off"
    s = str(v or "").strip().lower()
    return s if s in ("off", "mild", "strong") else "off"


def _read_audio_files(files):
    """（兼容保留）读音频 → (16k mono, 秒)。实现已挪到 `asr.read_audio_files`。

    2026-10-25：摘要转写改到子进程后，主进程只用 `probe_audio_seconds` 读时长；
    这个函数保留给旧调用方/自测脚本，不再有第二份实现。
    """
    from .asr import read_audio_files
    return read_audio_files(files)


def _send_to_recycle_bin(path: str) -> None:
    """把文件移入回收站（Windows：SHFileOperation + FOF_ALLOWUNDO）。

    【为什么走回收站】"不保存（丢弃）"是用户主动动作，但回收站可还原、防误点。
    调用失败时抛异常，由调用方提示（宁可不删）。
    """
    if os.name != "nt":
        os.remove(path)
        return
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_uint16),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    op = SHFILEOPSTRUCTW()
    op.wFunc = 3                           # FO_DELETE
    op.pFrom = path + "\0\0"               # 双 NUL 结尾
    op.fFlags = 0x0040 | 0x0010 | 0x0004   # FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT
    res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if res != 0:
        raise OSError("SHFileOperation 返回 %d" % res)


def _guard_qt_callback(fn):
    """Qt 回调保护装饰器。

    【为什么必须】PyQt6 对"Qt 回调（QTimer/信号槽）里未捕获的 Python 异常"
    的默认行为是**直接终止进程**（qFatal），且 traceback 不可见
    （2026-10-16 实测：回调里 1/0 → 进程 exit 127、零输出）。
    所有可能抛异常的回调入口都包上本装饰器：出错时记日志 + 状态栏提示，
    程序保持存活。

    【为什么按签名截断参数】PyQt 对**普通方法**做槽时会自动裁剪多余的信号参数
    （如 triggered(bool) 连到无参方法时不传 bool）；但被本装饰器包成 *args
    函数后 PyQt 无法内省，会把信号参数**全部**传入 → 原方法收到多余参数
    → TypeError（2026-10-16 实测：点托盘"退出"→
    `_quit() takes 1 positional argument but 2 were given`）。
    因此这里按原函数签名把参数裁剪到它真正能接受的数量。
    """
    try:
        _accept = max(0, len(inspect.signature(fn).parameters) - 1)   # 去掉 self
    except (TypeError, ValueError):
        _accept = None                                                 # 无法内省 → 原样转发

    def wrapper(self, *args, **kwargs):
        call_args = args if _accept is None else args[:_accept]
        try:
            return fn(self, *call_args, **kwargs)
        except Exception as e:  # noqa: BLE001
            try:
                if getattr(self, "_logger", None):
                    self._logger("回调 %s 异常：%s: %s" % (fn.__name__, type(e).__name__, e))
            except Exception:  # noqa: BLE001
                pass
            try:
                self._set_status("操作出错（详情见 startup.log）：%s" % e, warn=True)
            except Exception:  # noqa: BLE001
                pass

    wrapper.__name__ = getattr(fn, "__name__", "callback")
    return wrapper


class MainWindow(QMainWindow):
    sig_event = pyqtSignal(str, object)
    sig_summary_ok = pyqtSignal(str, str)      # LLM 摘要完成：(path, text)
    sig_summary_fail = pyqtSignal(str)         # LLM 摘要失败：(msg,)
    sig_summary_step = pyqtSignal(str)         # 摘要进度（转写/分段提炼 x/y…）
    sig_transcribe_ok = pyqtSignal(str, str, str)   # 现场转写完成：(text, title, src)
    sig_transcribe_fail = pyqtSignal(str)           # 现场转写失败：(msg,)
    sig_warm_done = pyqtSignal()                    # 启动预热完成 → 主线程挂释放定时器
    sig_opened_file = pyqtSignal(str)               # 文件已交由某程序启动 → 主线程做窗口调度

    def __init__(self, ipc_server=None, logger=None):
        super().__init__()
        self._ipc_server = ipc_server      # 单实例命名管道（由 run.py 传入）
        self._logger = logger              # 可选日志回调（run.py 的 _log）
        self.cfg = config_mod.load()
        self.rec = None
        self._recording = False
        self.tray_ok = QSystemTrayIcon.isSystemTrayAvailable()
        # 实时转文字（字幕窗只是"显示"；不落稿、不摘要——2026-10-24 三功能解耦）
        self._sub_on = False
        self._sub_auto = False             # True = 录音时自动开的（不弹字幕窗）
        self.sub_engine = None
        # 摘要（2026-10-24 三功能解耦：摘要自己搞定文字，不再依赖"转写稿"）
        self._last_files = []              # 最近一次停止录音保存的文件（仅本次会话；
                                           # 从未录过音的裸窗口也必须存在——曾因漏初始化
                                           # 导致"点摘要就 AttributeError"，见 2026-10-24 修复）
        self._sum_cache = None             # (files_tuple, text, title) 本次录音的转写缓存（内存）
        self._sum_asr = None               # 现场转写用的 Asr（仅降级路径用；闲置 3 分钟释放）
        self._asr_cli = None               # 摘要转写子进程客户端（2026-10-25 方案 B）
        self._sum_asr_timer = None         # 闲置释放定时器（主线程）
        self._trans_busy = False           # 现场转写进行中
        self._summary_busy = False
        self._summary_auto = False         # True = 自动跑的摘要（完成后不弹模态框）
        # 2026-10-26：摘要持久化——启动就扫描摘要目录里最新的 .md，
        # 这样重启后点「打开摘要」也能打开上一轮的结果（以前只存在内存里，
        # 重启即丢，才出现"还没有生成过摘要"的红字提示）。
        self._last_summary_path = self._scan_last_summary()
        self.sub_win = None

        # 2026-10-18：主界面换成「AI 摘记面板」——深色无边框置顶浮层
        self.setWindowTitle("AI 语音摘记")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # 主窗口本身透明（深色面板自带底色）；米白 STYLE 仍供隐藏区控件使用
        self.setStyleSheet("QMainWindow{background:transparent;border:none;}")

        self.sig_event.connect(self._on_rec_event)
        self.sig_opened_file.connect(self._on_file_opened)
        self.sig_summary_ok.connect(self._summary_done)
        self.sig_summary_fail.connect(self._on_summary_fail)
        self.sig_summary_step.connect(self._summary_step)
        self.sig_transcribe_ok.connect(self._on_transcribe_ok)
        self.sig_transcribe_fail.connect(self._on_transcribe_fail)
        self.sig_warm_done.connect(self._arm_sum_asr_release)
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._tick)

        self._build_ui()
        self._panel.sig_files_dropped.connect(self._on_files_dropped)   # 拖文件=手动摘要
        self._build_tray()
        self._refresh_devices()
        self._set_status("就绪：点“开始录音”，录制电脑正在播放的声音。")

        # 单实例唤醒：其他实例启动时经命名管道请求本实例显示窗口
        if self._ipc_server is not None:
            self._ipc_server.newConnection.connect(self._on_ipc_connection)
            if self._ipc_server.hasPendingConnections():
                QTimer.singleShot(0, self._on_ipc_connection)

        # 启动预热已移除（2026-10-25 用户方案 A+B）：不再在开机时加载模型，
        # 改为"停止录音后"预热子进程（见 _stop_rec），主进程始终 ~72MB。

    # ---------------- UI ----------------

    def _build_ui(self) -> None:
        # 2026-10-18：AiPanel 是**独立顶层窗口**，绝不能 addWidget 进布局——
        # 放进去会变成子控件（move 相对父窗口、超出父边界即被裁剪），
        # 表现就是"面板只能出现在固定区域、拖动时内容被裁"（用户 16:41 反馈）。
        self._panel = AiPanel({
            "panel_width": int(self.cfg.get("panel_width", 600) or 600),
        })
        self._panel.place_default()
        self._panel.show()

        # 主窗口只作逻辑载体（托盘/IPC/信号），不显示；旧控件放这里（隐藏）
        root = QWidget()
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)

        # ---- 旧控件区（隐藏，仅保留逻辑）----
        hidden = QWidget()
        hv = QVBoxLayout(hidden)
        hv.setContentsMargins(0, 0, 0, 0)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("设备"))
        self.combo_device = QComboBox()
        self.combo_device.currentIndexChanged.connect(self._on_device_changed)
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.setFixedWidth(64)
        self.btn_refresh.clicked.connect(self._refresh_devices)
        row1.addWidget(self.combo_device, 1)
        row1.addWidget(self.btn_refresh)
        hv.addLayout(row1)

        self.meter = LevelMeter()
        hv.addWidget(self.meter)

        self.lbl_time = QLabel("00:00:00")
        self.lbl_time.setObjectName("time")
        self.lbl_time.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hv.addWidget(self.lbl_time)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("格式"))
        self.combo_fmt = QComboBox()
        for label, fmt, br in FORMATS:
            self.combo_fmt.addItem(label, (fmt, br))
        cur_fmt = self.cfg.get("format", "mp3")
        cur_br = int(self.cfg.get("mp3_bitrate", 192) or 192)
        for i, (_l, fmt, br) in enumerate(FORMATS):
            if fmt == cur_fmt and (fmt == "wav" or br == cur_br):
                self.combo_fmt.setCurrentIndex(i)
        self.combo_fmt.currentIndexChanged.connect(self._on_fmt_changed)
        row2.addWidget(self.combo_fmt)
        row2.addStretch(1)
        self.lbl_dir = QLabel()
        self.lbl_dir.setObjectName("status")
        self.btn_dir = QPushButton("输出目录…")
        self.btn_dir.clicked.connect(self._choose_dir)
        self.btn_open = QPushButton("打开")
        self.btn_open.setFixedWidth(56)
        self.btn_open.clicked.connect(self._open_out_dir)
        row2.addWidget(self.lbl_dir)
        row2.addWidget(self.btn_dir)
        row2.addWidget(self.btn_open)
        hv.addLayout(row2)

        self.btn_main = QPushButton("开始录音")
        self.btn_main.setObjectName("main")
        self.btn_main.setFixedHeight(44)
        self.btn_main.clicked.connect(self._toggle)
        hv.addWidget(self.btn_main)

        self.btn_pause = QPushButton("暂停")
        self.btn_pause.setFixedHeight(32)
        self.btn_pause.clicked.connect(self._toggle_pause)
        self.btn_pause.setVisible(False)          # 仅录音期间显示
        hv.addWidget(self.btn_pause)

        # 实时转文字：独立于录音，点了就出字，不产生任何文件
        self.btn_sub = QPushButton("实时转文字（不录音）")
        self.btn_sub.setObjectName("sub")
        self.btn_sub.setCheckable(True)
        self.btn_sub.setFixedHeight(32)
        self.btn_sub.setToolTip(
            "只把系统正在播放的声音实时转成文字贴在屏幕上，不生成任何录音文件。\n"
            "和录音互不影响：可以边录边看，也可以完全不录音只看字。\n"
            "字幕窗：拖动=挪位置，点一下=展开/收起。")
        self.btn_sub.clicked.connect(self._toggle_subtitle)
        hv.addWidget(self.btn_sub)

        srow = QHBoxLayout()
        srow.setSpacing(8)
        self.lbl_status = QLabel("")
        self.lbl_status.setObjectName("status")
        self.lbl_status.setWordWrap(True)
        srow.addWidget(self.lbl_status, 1)
        self.btn_settings = QPushButton("设置")
        self.btn_settings.setObjectName("ghost")
        self.btn_settings.setFixedHeight(24)
        self.btn_settings.setToolTip("实时转文字的 VAD 敏感度、字幕节奏、外观，全部可调")
        self.btn_settings.clicked.connect(self._open_settings)
        srow.addWidget(self.btn_settings, 0, Qt.AlignmentFlag.AlignBottom)
        hv.addLayout(srow)

        hidden.hide()
        v.addWidget(hidden)

        self._update_dir_label()
        self.hide()          # 主窗口本身不显示（界面只有独立的面板窗口）

        # ---- 面板信号 ----
        self._panel.sig_record.connect(self._on_panel_record)
        self._panel.sig_stop_rec.connect(self._toggle)
        self._panel.sig_pause_rec.connect(self._toggle_pause)
        self._panel.sig_subtitle.connect(self._on_panel_subtitle)
        self._panel.sig_settings.connect(self._open_settings)
        self._panel.sig_close.connect(self._on_panel_close)
        self._panel.sig_summary.connect(self._on_panel_summary)

    def _build_tray(self) -> None:
        if not self.tray_ok:
            self.tray = None
            return
        self.tray = QSystemTrayIcon(self._app_icon(), self)
        # ⚠ 菜单挂 parent 并保存为成员引用（无 parent 的局部 QMenu 有悬空风险）
        self._tray_menu = QMenu(self)
        act_show = QAction("显示面板", self)
        act_show.triggered.connect(self._show_normal)
        self.act_toggle = QAction("开始录音", self)
        self.act_toggle.triggered.connect(self._toggle)
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self._quit)
        self._tray_menu.addAction(act_show)
        self._tray_menu.addAction(self.act_toggle)
        self._tray_menu.addSeparator()
        self._tray_menu.addAction(act_quit)
        # 【为什么改为手动弹菜单】2026-10-16 用户实测：Qt 在 Windows 上的自动托盘菜单
        # 出现"点击无响应 + 弹出位置错乱"（QTBUG 族系的自动路径问题）。
        # 现在改为在 activated(Context) 里手动 exec 到鼠标位置（见 _show_tray_menu），
        # 不再调用 setContextMenu，绕开 Qt 的自动定位/激活路径。
        self.tray.setToolTip("AI语音摘要")
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _app_icon(self) -> QIcon:
        ico = app_paths.resource_path("assets", "app.ico")
        if os.path.exists(ico):
            return QIcon(ico)
        return self.style().standardIcon(self.style().StandardPixmap.SP_MediaPlay)

    # ---------------- 数据 / 状态 ----------------

    def _out_dir(self) -> str:
        """录音目录。2026-10-25：绝不落在测试目录里。

        历史教训（两次）：config 的 output_dir 被指到 `dev\\_test_output`，
        用户录完音在「录音」里找不到文件（以为丢了）。这里做兜底：
        只要路径里带 `dev\\` 或 `_test`，一律改回默认「录音」目录并记日志。
        """
        d = self.cfg.get("output_dir") or config_mod.default_output_dir()
        low = str(d).replace("/", "\\").lower()
        if "\\dev\\" in low or "_test" in low:
            try:
                self._logger and self._logger(
                    "output_dir 指向测试目录（%s），已强制改用默认「录音」目录" % d)
            except Exception:                    # noqa: BLE001
                pass
            return config_mod.default_output_dir()
        return d

    def _summary_dir(self) -> str:
        return self.cfg.get("summary_dir") or config_mod.default_summary_dir()

    def _summary_cache_dir(self) -> str:
        return self.cfg.get("summary_cache_dir") or config_mod.default_summary_cache_dir()

    # ---------------- 摘要素材：现场转写（2026-10-24 三功能解耦）----------------

    def _summary_source(self):
        """摘要素材来源判定（摘要自己搞定文字，不依赖任何"转写稿"产物）。

        返回 (kind, payload)：
          ("subtitle", 文本)  字幕窗开着且有内容 → 直接摘要（字幕只做字幕，
                               内容活在窗里，关窗即弃；这里只是"借用"当前内容）
          ("audio", [文件])   本次刚停止的录音文件 → 调用方走现场转写
          ("none", None)      都没有 → 引导用户先录音
        """
        if self._sub_on and self.sub_win is not None:
            try:
                lines = self.sub_win.lines()
            except Exception:                    # noqa: BLE001
                lines = []
            if lines:
                return "subtitle", "\n".join(lines)
        if self._last_files:
            return "audio", list(self._last_files)
        return "none", None

    def _warm_summary_asr_async(self, delay_s: float = 1.0) -> None:
        """停止录音后预热摘要转写（2026-10-25 用户方案 A+B）。

        不再在启动时加载：开机空闲只占 ~72MB（原方案启动 3 秒就把模型拉起来，
        常驻 683MB）。现在改为**录完音**再预热，且预热发生在**子进程**里
        （`AsrClient`）——主进程永不加载 sherpa/CUDA，转写完子进程退出即全部归还。
        3 分钟不摘要由 _arm_sum_asr_release 杀掉子进程。
        """
        def _work():
            threading.Event().wait(max(0.0, delay_s))   # 让保存提示先出来
            try:
                self._asr_client().warm()
                self.sig_warm_done.emit()               # → 主线程挂释放定时器
            except Exception:                  # noqa: BLE001  预热失败不致命
                pass

        threading.Thread(target=_work, daemon=True,
                         name="sum-asr-warmup").start()

    def _asr_client(self):
        """摘要转写子进程客户端（懒建）。"""
        if self._asr_cli is None:
            from .asr_client import AsrClient
            self._asr_cli = AsrClient(
                model_type=str(self.cfg.get("asr_model") or "paraformer"),
                provider=self.cfg.get("asr_provider", "auto"),
                model_dir=self.cfg.get("asr_model_dir") or "")
        return self._asr_cli

    def _summary_asr(self):
        """现场转写用的 Asr（懒建 + 复用；主线程 _arm_sum_asr_release 负责定时释放）。

        实测：模型冷加载 228MB 约 30 秒（首次），热缓存下 60 秒音频只需 ~4 秒。
        复用可让连续摘要秒开；闲置 3 分钟由主线程释放，内存不常驻。
        """
        if self._sum_asr is None:
            from .asr import Asr
            self._sum_asr = Asr(model_dir=self.cfg.get("asr_model_dir") or None,
                                model_type=str(self.cfg.get("asr_model") or "paraformer"),
                                provider=self.cfg.get("asr_provider", "auto"))
        return self._sum_asr

    def _arm_sum_asr_release(self, delay_ms: int = 180000) -> None:
        """主线程调用：delay_ms 后释放摘要用的 Asr（释放 ~300MB 内存）。"""
        try:
            if getattr(self, "_sum_asr_timer", None) is None:
                self._sum_asr_timer = QTimer(self)
                self._sum_asr_timer.setSingleShot(True)
                self._sum_asr_timer.timeout.connect(self._release_sum_asr)
            self._sum_asr_timer.start(max(1000, int(delay_ms)))
        except Exception:                        # noqa: BLE001
            pass

    def _release_sum_asr(self) -> None:
        """闲置释放：杀转写子进程（归还全部内存/显存）+ 释放保底用的进程内 Asr。

        2026-10-25：转写/摘要进行中不释放——长音频（30 分钟）转写可能超过
        3 分钟闲置窗口，定时器到点若正在用就顺延，避免把干活中的子进程杀掉。
        """
        if getattr(self, "_trans_busy", False) or getattr(self, "_summary_busy", False):
            self._arm_sum_asr_release()
            return
        try:
            if getattr(self, "_sum_asr_timer", None) is not None:
                self._sum_asr_timer.stop()       # 已释放，别再触发
        except Exception:                        # noqa: BLE001
            pass
        cli = getattr(self, "_asr_cli", None)
        if cli is not None:
            self._asr_cli = None
            # 异步停子进程（stop 里最多 wait 2s，别卡 GUI 线程）
            threading.Thread(target=cli.stop, daemon=True,
                             name="asr-cli-stop").start()
        if self._sum_asr is not None:
            self._sum_asr = None
            try:
                import gc
                gc.collect()                     # 让 sherpa-onnx session 真正归还
            except Exception:                    # noqa: BLE001
                pass

    def _start_transcribe_job(self, files) -> None:
        """现场转写本次录音（后台线程；卡片 busy + 文案进度）。"""
        if self._trans_busy:
            return
        self._trans_busy = True
        self._panel.set_card_state("summary", "busy")
        self._set_status("正在转写本次录音…")
        # 正在干活：撤掉闲置释放定时器（长音频转写可能超过 3 分钟）
        try:
            if getattr(self, "_sum_asr_timer", None) is not None:
                self._sum_asr_timer.stop()
        except Exception:                        # noqa: BLE001
            pass
        threading.Thread(target=self._transcribe_work, args=(list(files),),
                         daemon=True).start()

    def _transcribe_work(self, files) -> None:
        """worker：子进程转写 → LLM 起标题 → 落摘要缓存。

        2026-10-25 方案 B：转写在 `asr_worker` 子进程里做（主进程不加载模型，
        子进程退出即归还内存/显存）；子进程不可用时回退主进程内转写（仅保底）。
        """
        try:
            from . import naming
            from .asr import probe_audio_seconds
            secs = probe_audio_seconds(files)      # 只读元信息，轻量
            self.sig_summary_step.emit(
                "正在转写本次录音（音频 %.0f 秒，约需 %.0f 秒；首次需加载模型会慢些）…"
                % (secs, max(2.0, secs / 8.0)))
            try:
                text, _dec, audio_secs = self._asr_client().transcribe(files)
                if audio_secs:
                    secs = audio_secs
            except Exception as e:                   # noqa: BLE001  子进程异常 → 主进程保底
                self.sig_summary_step.emit("子进程转写不可用（%s），改用主进程转写…"
                                           % str(e)[:60])
                from .asr import read_audio_files
                audio, secs2 = read_audio_files(files)
                if audio is None or not len(audio):
                    self.sig_transcribe_fail.emit("读不到本次录音的音频内容")
                    return
                secs = secs2
                text = (self._summary_asr().transcribe(audio, skip_vad=True) or "").strip()
            text = (text or "").strip()
            if not text:
                self.sig_transcribe_fail.emit("本次录音没有识别出文字（可能太短或没人说话）")
                return
            # 2026-10-26：转写稿分段（标点由子进程里的标点模型补，这里只切段）。
            # 落盘的转写稿和送给 LLM 的文本都用分段版，读起来不再是"一堵墙"。
            text = _paragraphs(text)
            # 命名：LLM 起标题（失败静默回退"未命名"，用户 Q2/Q10）
            title = ""
            try:
                from .llm import LlmClient, name_title
                cfg = self._llm_cfg()
                if cfg:
                    title = name_title(LlmClient(cfg), text)
            except Exception:                    # noqa: BLE001
                title = ""
            if not title:                        # LLM 失败/未配置 → 首句兜底
                title = _fallback_title(text)
            # 落摘要缓存（时间在前，用户 Q1=B）：<时间>_<标题>.md
            try:
                cd = self._summary_cache_dir()
                os.makedirs(cd, exist_ok=True)
                path = os.path.join(cd, naming.cache_filename(title))
                with open(path, "w", encoding="utf-8") as f:
                    f.write("# %s\n\n> 来源音频：%s\n> 转写时间：%s\n> 字数：%d\n\n%s\n"
                            % (title or "未命名", os.path.basename(files[0]),
                               QDateTime.currentDateTime().toString("yyyy-MM-dd hh:mm:ss"),
                               len(text), text))
                self._cleanup_summary_cache()    # 用户 Q4：保留 N 天后自动清理
            except Exception:                    # noqa: BLE001  缓存落盘失败不影响摘要
                pass
            self.sig_transcribe_ok.emit(text, title, os.path.basename(files[0]))
        except Exception as e:                   # noqa: BLE001
            self.sig_transcribe_fail.emit("转写失败：%s" % e)

    def _on_transcribe_ok(self, text: str, title: str, src: str) -> None:
        self._trans_busy = False
        self._sum_cache = (tuple(self._last_files or ()), text, title)
        # 2026-10-25：转写拿到文字后 worker 就没用了（后续摘要走缓存文本，
        # 不再需要 ASR）——立刻让它退出，别挂着 1.1GB 等 3 分钟。
        self._release_sum_asr()
        self._panel.toast("转写完成（%d 字），正在生成摘要…" % len(text), 4000)
        self._start_summary(text, auto=False, src=src, title=title)

    def _on_transcribe_fail(self, msg: str) -> None:
        self._trans_busy = False
        self._release_sum_asr()                  # 失败也不必留着 worker
        self._panel.set_card_state("summary", "idle")
        # 2026-10-25 用户要求：转写失败（如"没识别出文字"）也要给三个动作按钮，
        # 方便直接去看录音/摘要/转写目录排查。
        self._show_folder_actions(msg, warn=True)

    def _cleanup_summary_cache(self) -> None:
        """摘要缓存按"保留天数"清理过期文件（用户 Q4=7 天，可设置）。"""
        try:
            from . import naming
            naming.cleanup_cache(self._summary_cache_dir(),
                                 int(self.cfg.get("summary_cache_keep_days", 7) or 7))
        except Exception:                        # noqa: BLE001
            pass

    def _update_dir_label(self) -> None:
        d = self._out_dir()
        parts = d.replace("/", "\\").rstrip("\\").split("\\")
        short = "\\".join(parts[-2:]) if len(parts) >= 2 else d
        self.lbl_dir.setText(short)

    def _set_status(self, text: str, warn: bool = False, toast: bool = True) -> None:
        self.lbl_status.setObjectName("statusWarn" if warn else "status")
        self.lbl_status.style().unpolish(self.lbl_status)
        self.lbl_status.style().polish(self.lbl_status)
        self.lbl_status.setText(text)
        panel = getattr(self, "_panel", None)
        if panel is not None and toast:
            panel.toast(text, 3500, warn=warn)

    def _refresh_devices(self) -> None:
        if self._recording:
            return
        self.combo_device.blockSignals(True)
        self.combo_device.clear()
        self.combo_device.addItem("（自动跟随系统默认输出）", None)
        for i, name, ch, sr in devices.list_loopback_devices():
            self.combo_device.addItem("%s   |   %d Hz  %dch" % (name.replace(" [Loopback]", ""), sr, ch), i)
        want = self.cfg.get("device_index")
        if want is not None:
            idx = self.combo_device.findData(want)
            if idx >= 0:
                self.combo_device.setCurrentIndex(idx)
        self.combo_device.blockSignals(False)

    def _on_device_changed(self) -> None:
        self.cfg["device_index"] = self.combo_device.currentData()
        config_mod.save(self.cfg)
        if self._sub_on:                    # 字幕开着就按新设备重开（不打断用户）
            self._stop_subtitle()
            self._toggle_subtitle(True)

    def _on_fmt_changed(self) -> None:
        data = self.combo_fmt.currentData()
        if data:
            fmt, br = data
            self.cfg["format"] = fmt
            self.cfg["mp3_bitrate"] = int(br or 192)
            config_mod.save(self.cfg)

    def _choose_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择录音保存目录", self._out_dir())
        if d:
            self.cfg["output_dir"] = d.replace("/", "\\")
            config_mod.save(self.cfg)
            self._update_dir_label()

    def _open_out_dir(self) -> None:
        d = self._out_dir()
        try:
            os.makedirs(d, exist_ok=True)
            os.startfile(d)  # noqa: S606  Windows 打开资源管理器
        except Exception as e:  # noqa: BLE001
            self._set_status("打开目录失败：%s" % e, warn=True)

    # ---------------- 录音控制 ----------------

    @_guard_qt_callback
    # ---------------- 实时转文字（不录音） ----------------
    def _ensure_sub_win(self):
        if self.sub_win is None:
            c = self.cfg
            self.sub_win = SubtitleWindow({
                "lines": int(c.get("subtitle_lines", 3) or 3),
                "lines_expanded": int(c.get("subtitle_lines_expanded", 8) or 8),
                "width": int(c.get("subtitle_width", 640) or 640),
                "font_size": int(c.get("subtitle_font_size", 15) or 15),
                "opacity": 1.0,   # 2026-10-19 用户要求：字幕窗不透明
                "typewriter_ms": int(c.get("typewriter_ms", 30) or 30),
                "capture_exclude": bool(c.get("subtitle_capture_exclude", True)),
                "x": int(c.get("subtitle_x", -1) or -1),
                "y": int(c.get("subtitle_y", -1) or -1),
            })
            self.sub_win.restore_position()
            self.sub_win.closed.connect(self._stop_subtitle)   # 点 × → 主界面按钮一起复位
        return self.sub_win

    def _drop_sub_win(self):
        """设置改了外观 → 旧窗口的尺寸/字号/透明度不会自己变，销毁重建。"""
        if self.sub_win is not None:
            try:
                self.sub_win.close()
            except Exception:                   # noqa: BLE001
                pass
            self.sub_win = None

    @_guard_qt_callback
    def _open_settings(self) -> None:
        dlg = SettingsDialog(self.cfg, self)
        # 2026-10-25 方案①：「保存模型」落盘后立即同步进运行内存（不用等全局保存）
        dlg.sig_llm_saved.connect(self._on_llm_saved)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.result_cfg:
            return
        new_cfg = dlg.result_cfg
        changed = [k for k, v in new_cfg.items() if self.cfg.get(k) != v]
        self.cfg.update(new_cfg)
        config_mod.save(self.cfg)
        self._set_status("设置已保存")
        # 引擎/窗口只在启动时读配置 → 相关键变了才重启字幕；
        # llm_* / auto_* / *_dir / summary_* 改了不重启，避免清掉正在跑的字幕与转写
        def _need_restart(k):
            return not (k.startswith("llm_") or k.startswith("auto_")
                        or k.endswith("_dir") or k.startswith("summary_"))
        if self._sub_on and any(_need_restart(k) for k in changed):
            self._stop_subtitle()
            self._drop_sub_win()
            self._toggle_subtitle(True)

    @_guard_qt_callback
    def _on_llm_saved(self, new_cfg: dict) -> None:
        """「保存模型」即时生效（2026-10-25 方案①）。

        设置对话框的「保存模型」已立即落盘 config.json；这里只把 LLM 相关键同步进
        运行内存——**不全量 update**，避免冲掉设置里改到一半的其他项。
        摘要链路每次现读 self.cfg（_llm_cfg）→ 下一次摘要就是用新配置。
        """
        if not isinstance(new_cfg, dict):
            return
        for k in ("llm_profiles", "llm_active"):
            if k in new_cfg:
                self.cfg[k] = new_cfg[k]
        self._set_status("LLM 配置已保存并生效（当前模型：%s）"
                         % (self.cfg.get("llm_active") or "（未选）"))

    @_guard_qt_callback
    def _toggle_subtitle(self, on=None) -> None:
        on = (not self._sub_on) if on is None else bool(on)
        if not on:
            self._stop_subtitle()
            return
        if self._sub_on and self.sub_engine is not None:
            # 已经在转（多半是录音时自动开的）→ 只把字幕窗显示出来，不重开引擎
            win = self._ensure_sub_win()
            win.show()
            win.raise_()
            win.apply_capture_exclude(bool(self.cfg.get("subtitle_capture_exclude", True)))
            self._sub_auto = False
            return
        win = self._ensure_sub_win()
        win.clear("等待系统声音…")
        win.show()
        win.raise_()
        win.apply_capture_exclude(bool(self.cfg.get("subtitle_capture_exclude", True)))

        _spid, _spname, _smuted = self._pick_capture_target()
        eng = SubtitleEngine(self.combo_device.currentData(), self.cfg, process_pid=_spid)
        eng.line.connect(self._on_sub_line)
        eng.partial.connect(self._on_sub_partial)
        eng.status.connect(lambda t: self._set_status(t, toast=False))
        eng.failed.connect(self._on_sub_failed)
        self.sub_engine = eng
        if not eng.start():
            self.sub_engine = None
            win.hide()
            self._sub_on = False
            self.btn_sub.setChecked(False)
            self._panel.set_card_state("subtitle", "idle")
            return
        self._sub_on = True
        self.btn_sub.setChecked(True)     # 程序内调用（如改完设置自动重启）时也要同步高亮
        self.btn_sub.setText("关闭实时字幕")
        self._panel.set_card_state("subtitle", "active")   # 卡片：字幕已开启
        if _spid:
            self._panel.toast("系统静音 → 字幕按进程听：%s（无视系统静音）"
                              % (_spname or "#%d" % _spid), 4500)
        elif _smuted:
            self._panel.toast("系统是静音的，且当前没有应用在放声——字幕收不到声音。",
                              5000, warn=True)

    def _stop_subtitle(self) -> None:
        # 2026-10-24 三功能解耦：字幕只负责显示，关字幕即停引擎
        # （不再有"录音中关窗但后台继续转写落稿"的概念）
        if self.sub_engine is not None:
            try:
                self.sub_engine.stop()
            except Exception:                   # noqa: BLE001
                pass
            self.sub_engine = None
        if self.sub_win is not None:
            try:
                p = self.sub_win.pos()
                self.cfg["subtitle_x"] = int(p.x())
                self.cfg["subtitle_y"] = int(p.y())
                config_mod.save(self.cfg)
            except Exception:                   # noqa: BLE001
                pass
            self.sub_win.hide()
        self._sub_on = False
        self._sub_auto = False
        self.btn_sub.setChecked(False)
        self.btn_sub.setText("实时转文字（不录音）")
        pnl = getattr(self, "_panel", None)          # 退出流程里面板可能已销毁
        if pnl is not None:
            pnl.set_card_state("subtitle", "idle")   # 卡片：回到未开启

    # ---------------- LLM 摘要 ----------------

    def _llm_cfg(self) -> dict:
        """当前激活的 LLM profile → LlmClient 扁平配置（2026-10-23 多 LLM）。

        没有 profile（没配过 / 全删了）→ 返回 {}，调用方按"未配置"处理。
        """
        from .llm import active_profile, profile_client_cfg
        prof = active_profile(self.cfg)
        if not prof:
            return {}
        cc = profile_client_cfg(prof)
        # 2026-10-26：摘要提示词的 {读者}/{用途} 变量（全局配置，留空用默认）
        cc["summary_reader"] = str(self.cfg.get("summary_reader") or "")
        cc["summary_purpose"] = str(self.cfg.get("summary_purpose") or "")
        return cc

    @_guard_qt_callback
    def _on_panel_summary(self) -> None:
        """面板「开始 AI 摘要」：摘要自己搞定文字（2026-10-24 三功能解耦）。

        来源优先级：字幕窗当前内容 > 本次刚停止的录音（现场转写）> 无。
        """
        if self._summary_busy:
            self._panel.toast("摘要正在生成中，请稍候…", 2500)
            return
        if self._trans_busy:
            self._panel.toast("正在转写本次录音，转完会自动生成摘要…", 3000)
            return
        if self._recording:
            self._panel.toast("录音中——停止后可直接点摘要。", 3500)
            return
        kind, payload = self._summary_source()
        if kind == "none":
            # 2026-10-25 用户要求：直接点摘要也要给三个动作按钮（不是一条会消失的提示）
            # 2026-10-26：颜色改为与其他提示一致的白色（warn=False），
            # 这只是"暂时没内容"的普通提示，不是错误。
            self._show_folder_actions(
                "还没有可摘要的内容：先录音（停止后可直接摘要）或开启字幕。")
            return
        if kind == "subtitle":
            # 2026-10-26：字幕来源同样没传 title（也会落成「未命名」）——
            # 用首句兜底，保证文件名能看出内容。
            self._start_summary(payload, auto=False, src="字幕",
                                title=_fallback_title(payload))
            return
        # kind == "audio"：本次录音 → 有内存缓存就复用，否则现场转写
        files = payload
        if self._sum_cache and self._sum_cache[0] == tuple(files):
            self._start_summary(self._sum_cache[1], auto=False,
                                src="本次录音（缓存）", title=self._sum_cache[2])
            return
        self._start_transcribe_job(files)

    def _start_summary(self, text: str, auto: bool = False, src: str = "",
                       title: str = "") -> None:
        """统一的摘要入口（手动都走这里；title 用于摘要文件命名）。"""
        text = (text or "").strip()
        if not text:
            self._set_status("转写文本为空，没有可摘要的内容", warn=True)
            return
        if self._summary_busy:
            return
        from .llm import active_profile
        prof = active_profile(self.cfg)
        if prof is None:
            self._set_status("还没配 LLM（设置 → LLM 摘要 → 新建），摘要生成不了", warn=True)
            self._panel.toast("摘要需要先在设置里配一个 LLM。", 4500, warn=True)
            return
        if not prof.get("api_key", ""):
            self._set_status("当前 LLM「%s」没填 API Key（设置 → LLM 摘要）" % prof["name"],
                             warn=True)
            self._panel.toast("「%s」还没填 API Key。" % prof["name"], 4500, warn=True)
            return
        shown = dict(self.cfg.get("llm_confirm_shown") or {})
        if not shown.get(prof["name"], False):
            # 每个 profile 首次摘要各自确认一次（URL 不同，隐私意义不同；用户 Q3）
            if not msgbox.ask(
                    self, "发送确认",
                    "将把本次转写（约 %d 字）发往「%s」：\n%s\n\n继续生成摘要？"
                    % (len(text), prof["name"], prof.get("base_url") or "（未配置）")):
                return
            shown[prof["name"]] = True
            self.cfg["llm_confirm_shown"] = shown
            config_mod.save(self.cfg)
        self._summary_busy = True
        self._summary_auto = bool(auto)
        self._set_status("正在用「%s」生成摘要（约 10~60 秒）…" % prof["name"])
        self._panel.set_card_state("summary", "busy")     # 卡片：处理中
        threading.Thread(target=self._summary_work, args=(text, src, title),
                         daemon=True).start()

    def _summary_step(self, msg: str) -> None:
        self._set_status(msg)

    def _summary_work(self, text: str, src: str = "", title: str = "") -> None:
        try:
            from . import naming
            from .llm import LlmClient, summarize_long
            client = LlmClient(self._llm_cfg())
            chunk = int(self.cfg.get("summary_chunk_chars", 6000) or 0)
            seg = int(self.cfg.get("summary_segment_minutes", 20) or 0)
            out = summarize_long(
                client, text, chunk_chars=chunk, segment_minutes=seg,
                progress=lambda m: self.sig_summary_step.emit(m))
            if not (out or "").strip():
                # 2026-10-26：模型返回空正文时**不再写空文件**（曾出现只有页脚的
                # 249 字节"空摘要"）。走失败分支，让用户看到真实原因。
                self.sig_summary_fail.emit(
                    "摘要为空：模型没有返回正文（推理模型可能把 token 全用在思考上了，"
                    "请在设置里调大 max_tokens 或关闭思考模式）。")
                return
            d = self._summary_dir()
            os.makedirs(d, exist_ok=True)
            now = QDateTime.currentDateTime()
            # 命名（用户 Q1=B/Q3：与缓存同名口径，时间在前 + 标题 + _摘要）
            path = os.path.join(d, naming.summary_filename(title))
            with open(path, "w", encoding="utf-8") as f:
                # 2026-10-24 用户要求：摘要文件只放摘要，不再附原始转写全文——
                # 音频来源的转写稿已在「摘要缓存」目录落盘（见 _transcribe_work），
                # 两份文件各存一份全文只会越滚越大；这里只留一行来源说明。
                if src == "字幕":
                    note = "素材来源：字幕窗口（转写 %d 字；字幕内容未落缓存，关窗即弃）" % len(text)
                else:
                    note = "素材来源：%s（转写 %d 字；全文见「摘要缓存」目录）" % (
                        src or "本次录音", len(text))
                # 2026-10-25 用户要求：素材来源下面再加一行，写明由哪个大模型生成
                try:
                    from .llm import active_profile
                    prof = active_profile(self.cfg) or {}
                except Exception:                      # noqa: BLE001
                    prof = {}
                model_line = "生成模型：%s" % (prof.get("model") or "未配置")
                if prof.get("name") and prof.get("name") != prof.get("model"):
                    model_line = "生成模型：%s（%s）" % (prof["name"], prof.get("model") or "未配置")
                f.write("# 摘要：%s（%s）\n\n%s\n\n> %s\n> %s\n"
                        % (title or "未命名", now.toString("yyyy-MM-dd hh:mm:ss"),
                           out, note, model_line))
            self.sig_summary_ok.emit(path, out)      # 跨线程：worker → GUI 主线程
        except Exception as e:  # noqa: BLE001
            self.sig_summary_fail.emit("摘要失败：%s" % e)

    @_guard_qt_callback
    def _on_file_opened(self, exe: str) -> None:
        """文件已交由某程序启动（主线程）：只补提示，**不再降置顶**。

        2026-10-26：`_open_path` 已在启动前降过一次；这里再降会叠加第二个
        8 秒定时器，导致"置顶消失很久"。降置顶只做一次。
        """
        if exe and exe.lower().endswith("notepad.exe"):
            self._panel.toast("已用记事本打开（.md 没有可用的编辑器关联）", 4500)

    _SWP_NOSIZE = 0x0001
    _SWP_NOMOVE = 0x0002
    _SWP_NOACTIVATE = 0x0010

    def _set_topmost(self, on: bool) -> bool:
        """用 Win32 切换面板置顶（2026-10-26）。

        为什么不用 Qt 的 setWindowFlag(WindowStaysOnTopHint)：
        它会让 Qt **销毁并重建原生窗口**（内部必须 hide+show），表现为窗口
        "闪一下"；8 秒后恢复时再闪一次，且置顶常来不及生效。
        HWND_TOPMOST / HWND_NOTOPMOST 只改扩展样式（WS_EX_TOPMOST），
        不重建、不闪、立即生效。返回是否成功。
        """
        try:
            import ctypes
            hwnd = int(self._panel.winId())        # 首调用会创建原生窗口
            if not hwnd:
                return False
            flag = -1 if on else -2                # HWND_TOPMOST / HWND_NOTOPMOST
            ok = ctypes.windll.user32.SetWindowPos(
                ctypes.c_void_p(hwnd), ctypes.c_void_p(flag), 0, 0, 0, 0,
                self._SWP_NOMOVE | self._SWP_NOSIZE | self._SWP_NOACTIVATE)
            return bool(ok)
        except Exception:                          # noqa: BLE001
            return False

    def _lower_panel_then_restore(self) -> None:
        """打开外部窗口前临时取消置顶（2026-10-26）。

        面板是 WindowStaysOnTopHint 常驻置顶，新打开的 Typora / 记事本 /
        资源管理器都会被它挡住 —— 用户点了"打开摘要"却什么都看不到，以为没反应。
        这里临时降下来并压到底层，8 秒后恢复（Typora 冷启动要 2~4 秒，
        1.5 秒就恢复会把刚起来的窗口又盖住 —— 2026-10-26 实测踩到）。
        """
        if not self._set_topmost(False):       # Win32：无闪烁
            try:                               # 兜底：Qt 方式（会闪，但功能正确）
                self._panel.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, False)
                self._panel.show()
            except Exception:                  # noqa: BLE001
                pass
        QTimer.singleShot(8000, self._restore_panel_top)

    def _restore_panel_top(self) -> None:
        if not self._set_topmost(True):        # Win32：无闪烁
            try:
                self._panel.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
                self._panel.show()
            except Exception:                  # noqa: BLE001
                pass
        try:
            self._panel.raise_()
        except Exception:                      # noqa: BLE001
            pass

    def _open_path(self, path: str) -> None:
        """用系统默认应用/资源管理器打开路径（2026-10-25 摘要完成三按钮用）。

        2026-10-26 修"点了没反应"：
          ① .md 没关联程序时 os.startfile 会弹"选择应用"对话框，而面板置顶
             把对话框挡住了 → 改用记事本兜底打开；
          ② 打开前临时取消面板置顶，让新窗口看得见。
        """
        try:
            if os.path.isdir(path):            # 目录 → 资源管理器（2026-10-26）
                self._open_dir(path)
                return
            self._lower_panel_then_restore()   # 先让位（主线程立即生效），再启动程序
            threading.Thread(target=self._open_path_worker, args=(path,),
                             daemon=True, name="open-file").start()
        except Exception as e:  # noqa: BLE001
            self._set_status("打开失败：%s" % e, warn=True)

    def _open_path_worker(self, path: str) -> None:
        """后台打开文件：按优先级试，**验活失败就换下一个**（2026-10-26）。

        为什么不能只 Popen 一次：
          · `HKCR\\.md` 在本机指向 **WorkBuddy**（实测）→ os.startfile 把 md
            交给 WorkBuddy，用户以为"没反应"；
          · 本机 **Typora 起不来**（带/不带参数都秒退，返回码 0/1）→ 只 Popen
            会静默失败，用户还是看不到东西。
        所以：按 首选列表 → 注册表命令 → 记事本 的顺序逐个尝试；
        每个都"启动后 1.5 秒验活"（还活着 = 成功；秒退但存在同名进程 = 已转交
        给已有实例，也算成功），全失败才走 os.startfile 兜底。
        """
        import subprocess
        ext = os.path.splitext(path)[1].lower()
        candidates = list(self.PREFERRED_OPENERS.get(ext, []))
        cmd_exe = self._open_command_exe(ext)
        if cmd_exe:
            candidates.append(cmd_exe)
        candidates.append(r"C:\Windows\notepad.exe")
        for exe in candidates:
            if not exe or not os.path.isfile(exe):
                continue
            try:
                p = subprocess.Popen([exe, os.path.normpath(path)],
                                     env=electron_safe_env())
            except Exception:                          # noqa: BLE001
                continue
            time.sleep(1.5)
            # 验活：进程还在（None）或返回 0 都算成功。
            # 注意不能用"tasklist 里有同名进程"当成功条件——Typora 这类
            # 单实例应用会把文件转交给已有实例后**退出**，但本机实测 Typora
            # 返回码为 1（真起不来），必须据此回退到下一个候选（2026-10-26）。
            rc = p.poll()
            if rc is None or rc == 0:
                # 窗口调度必须回主线程（setWindowFlag/show 跨线程调用不生效，
                # 2026-10-26 实测：worker 里直接调，降置顶静默失效）
                self.sig_opened_file.emit(exe)
                hint = os.path.splitext(os.path.basename(path))[0][:20]
                focus_window_by_title(hint, 6.0)   # 纯 win32 轮询，线程安全
                return
            # rc 非 0 → 这个程序打不开，继续试下一个
        try:                                           # 最后兜底：交给系统
            os.startfile(path)  # noqa: S606
            self.sig_opened_file.emit("os.startfile")
        except Exception as e:                         # noqa: BLE001
            self._set_status("打开失败：%s" % e, warn=True)

    def _proc_alive(self, exe_name: str) -> bool:
        """是否已有同名进程在跑（判断"秒退是转交给已有实例"）。"""
        try:
            import subprocess
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq %s" % exe_name,
                                  "/FO", "CSV", "/NH"], capture_output=True)
            txt = out.stdout.decode("gbk", errors="replace")
            return exe_name.lower() in txt.lower()
        except Exception:                              # noqa: BLE001
            return False

    #: 各格式的"编辑器"优先级（2026-10-26）。
    #  为什么需要：`HKCR\.md` 在本机指向 **WorkBuddy**（实测），os.startfile 会把
    #  md 交给 WorkBuddy，用户以为"没反应"。这里显式优先用真正的编辑器打开。
    PREFERRED_OPENERS = {
        ".md": [r"D:\Program Files\Typora\Typora.exe",
                r"C:\Program Files\Typora\Typora.exe",
                r"C:\Program Files (x86)\Typora\Typora.exe"],
        ".txt": [r"C:\Windows\notepad.exe"],
        ".log": [r"C:\Windows\notepad.exe"],
    }

    def _pick_opener(self, ext: str) -> str:
        """选出用于打开该扩展名的程序路径；都不适用则返回 ""（走 os.startfile）。"""
        for p in self.PREFERRED_OPENERS.get(ext, []):
            if os.path.isfile(p):
                return p
        return self._open_command_exe(ext)

    def _open_command_exe(self, ext: str) -> str:
        """注册表里声明的打开命令行 → 抽出可执行文件路径（抽不到返回 ""）。"""
        cmd = self._open_command(ext)
        if not cmd:
            return ""
        s = cmd.replace('"%1"', "").replace("%1", "").strip() \
               .replace('"', "")
        # 形如 `D:\Program Files\X\X.exe` 或带参数：取第一段看起来像 exe 的
        for tok in s.split():
            if tok.lower().endswith(".exe") and os.path.isfile(tok):
                if "workbuddy" in tok.lower():
                    continue    # 宿主程序不是 md 编辑器（2026-10-26 实测：
                                # HKCR\.md 指向 WorkBuddy.md → 点了"没反应"）
                return tok
        if s and os.path.isfile(s):
            if "workbuddy" not in s.lower():
                return s
        return ""

    # ---------------- 手动摘要（2026-10-25 用户要求） ----------------
    AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".wma"}
    TEXT_EXTS = {".md", ".txt", ".markdown", ".log"}

    def _manual_summary(self) -> None:
        """手动摘要：选一个音频（先转写再摘要）或文本（直接摘要）文件。"""
        audio = " ".join("*%s" % e for e in sorted(self.AUDIO_EXTS))
        text = " ".join("*%s" % e for e in sorted(self.TEXT_EXTS))
        path, _sel = QFileDialog.getOpenFileName(
            self, "手动摘要：选择音频或文本文件", self._out_dir(),
            "音频/文本 (%s %s);;音频 (%s);;文本 (%s);;所有文件 (*)"
            % (audio, text, audio, text))
        if path:
            self._summarize_path(path)

    def _on_files_dropped(self, paths) -> None:
        """拖文件进面板（2026-10-25）：一次只处理一个。"""
        paths = [p for p in (paths or []) if os.path.isfile(p)]
        if not paths:
            return
        if len(paths) > 1:
            self._panel.toast("一次只能拖一个文件，已用第一个：%s"
                              % os.path.basename(paths[0]), 4000, warn=True)
        self._summarize_path(paths[0])

    def _title_from_source(self, path: str, text: str) -> str:
        """从素材文件推断摘要标题（2026-10-26）。

        【为什么需要】手动摘要（拖/选文件）以前只传 `src=文件名`、**不传 title**，
        落盘时 `summary_filename("")` 恒为「未命名」——这就是 16:24/16:25 两份
        文件名是"未命名"的根因（素材其实是带标题的转写稿）。
        取标题的顺序：
          ① 内容首行的 Markdown 标题（`# 摘要：xxx（时间）` / `# xxx`）；
          ② 文件名去掉「时间戳前缀」与「_摘要/_转写/_字幕」后缀。
        """
        try:
            first = (text or "").strip().split(chr(10))[0].strip()
            m = _MD_TITLE_RE.match(first or "")
            if m:
                t = m.group(1).strip()
                t = re.sub(r"^摘要[：:]\s*", "", t)
                t = _TITLE_TS_RE.sub("", t)
                t = t.strip()
                if t:
                    return t[:30]
        except Exception:                      # noqa: BLE001
            pass
        try:
            base = os.path.splitext(os.path.basename(path or ""))[0]
            base = re.sub(r"^\d{8}-\d{6}_", "", base)      # 去 20261026-160317_
            base = re.sub(r"(_摘要|_转写|_字幕)$", "", base)  # 去后缀
            return base.strip()[:30]
        except Exception:                      # noqa: BLE001
            return ""

    def _summarize_path(self, path: str) -> None:
        """按扩展名分流：音频 → 子进程转写后再摘要；文本 → 直接摘要。"""
        if self._recording:
            self._panel.toast("录音中——先停止录音，再手动摘要。", 4000, warn=True)
            return
        if self._trans_busy or self._summary_busy:
            self._panel.toast("正在处理上一条，请稍候…", 3000)
            return
        ext = os.path.splitext(path)[1].lower()
        name = os.path.basename(path)
        if ext in self.AUDIO_EXTS:
            # 当作"本次素材"：缓存键、来源标注都用它
            self._last_files = [path]
            self._set_status("手动摘要：正在转写 %s…" % name)
            self._start_transcribe_job([path])
            return
        if ext in self.TEXT_EXTS:
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except Exception as e:               # noqa: BLE001
                self._set_status("读不了这个文本文件：%s" % e, warn=True)
                return
            if not text.strip():
                self._set_status("文本文件是空的：%s" % name, warn=True)
                return
            # 2026-10-26：手动摘要以前不传 title → 文件名恒为「未命名」。
            # 这里从素材首行标题 / 文件名推断，保证摘要文件名有意义。
            self._start_summary(text, auto=False, src=name,
                                title=self._title_from_source(path, text))
            return
        self._panel.toast("不支持这种文件：%s（要音频或文本）" % (ext or "无扩展名"),
                          4500, warn=True)

    def _open_dir(self, d: str) -> None:
        """打开目录（不存在就先建）。三个"打开…"按钮共用。"""
        try:
            os.makedirs(d, exist_ok=True)
            os.startfile(d)  # noqa: S606  Windows 打开资源管理器
        except Exception as e:  # noqa: BLE001
            self._set_status("打开目录失败：%s" % e, warn=True)

    def _scan_last_summary(self) -> str:
        """返回摘要目录里最新的 .md（无则 ""）——摘要持久化的读取端。"""
        try:
            import glob
            d = self._summary_dir()
            files = glob.glob(os.path.join(d, "*.md"))
            if not files:
                return ""
            files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
            return files[0]
        except Exception:                      # noqa: BLE001
            return ""

    def _open_last_summary(self) -> None:
        """打开上一轮生成的摘要文件（2026-10-26 用户要求的新按钮）。

        摘要文件路径存在 _last_summary_path（_summary_done 落盘时记录）；
        没有就提示，不弹窗。打开走 _open_path（Typora 优先 + 回退链）。
        """
        p = getattr(self, "_last_summary_path", "")
        if not p or not os.path.isfile(p):
            p = self._scan_last_summary()      # 惰性兜底：目录里还有就直接用
            self._last_summary_path = p
        if p and os.path.isfile(p):
            self._open_path(p)
            return
        self._panel.toast("还没有生成过摘要——先点摘要或录一段。", 4000, warn=True)

    def _show_folder_actions(self, text: str, warn: bool = False) -> None:
        """没素材 / 转写失败时，也给三个动作按钮（2026-10-25 用户要求）。

        与"摘要完成"那组同一套 toast_actions：位置/尺寸一致（26px、贴底 6px），
        高度同样 226 ↔ 186。失败场景没有"摘要文件"可开，所以三个按钮都是目录。
        注意顺序：先写状态栏（toast=False）——否则 _set_status 的默认 toast 会
        走 _clear_actions()，把刚建的按钮和加高一起撤掉（2026-10-25 踩过）。
        """
        self._set_status(text, warn=warn, toast=False)
        self._panel.toast_actions(
            text,
            [("打开录音", lambda: self._open_dir(self._out_dir())),
             ("打开摘要", self._open_last_summary),          # 打开上一轮摘要文件
             ("打开文件夹", lambda: self._open_dir(self._summary_dir())),
             ("手动摘要", self._manual_summary)],
            warn=warn)

    def _summary_done(self, path: str, out: str) -> None:
        self._summary_busy = False
        self._last_summary_path = path
        self._panel.flash_card("summary", "done", 2000)   # 卡片：刚生成完
        name = os.path.basename(path)
        # 2026-10-25：状态栏先写（toast=False）——_set_status 默认会弹普通 toast，
        # 而 toast() 内部 _clear_actions() 会把下面刚建的动作按钮和加高一起撤掉
        # （实测：按钮出现又瞬间消失、高度弹回 186）。顺序必须是"先状态栏、后按钮"。
        self._set_status("摘要已生成：%s" % path, toast=False)
        # 2026-10-25 用户要求：摘要完成后给三个动作按钮——位置/尺寸与"录完音"
        # 那组完全一致（同一个 toast_actions：贴底 6px、26 高；高度由
        # _ensure_height(226) / 收起时缩回 186 统一管理，不会再抖）。
        self._panel.toast_actions(
            "摘要已生成：%s" % name,
            [("打开摘要", lambda: self._open_path(path)),
             # 2026-10-26 修：目录必须走 _open_dir（os.startfile(dir) → 资源管理器）。
             # 原来用 _open_path 会进入"文件打开器"候选链（Typora→记事本），
             # 记事本打不开目录 → 表现为"点了不是打开文件夹"。
             ("打开文件夹",
              lambda p=path: self._open_dir(os.path.dirname(p) or self._summary_dir())),
             ("手动摘要", self._manual_summary)])

    def _on_summary_fail(self, msg: str) -> None:
        self._summary_busy = False
        self._panel.set_card_state("summary", "idle")     # 卡片：失败 → 复位
        self._set_status(msg, warn=True)
        if self._summary_auto:
            self._panel.toast(msg, 6000, warn=True)

    @_guard_qt_callback
    def _on_sub_line(self, txt: str) -> None:
        """字幕定版行 → 只进字幕窗（2026-10-24 三功能解耦：字幕不落稿）。"""
        if self.sub_win is not None:
            self.sub_win.add_line(txt)

    @_guard_qt_callback
    def _on_sub_partial(self, txt: str) -> None:
        if self.sub_win is not None:
            self.sub_win.set_partial(txt)

    @_guard_qt_callback
    def _on_sub_failed(self, msg: str) -> None:
        self._set_status("实时转文字失败：%s" % msg, warn=True)
        self._stop_subtitle()

    # ---------------- AI 摘记面板事件 ----------------

    @_guard_qt_callback
    def _on_panel_record(self) -> None:
        """「开始应用录音」卡片：同一个按钮切换（2026-10-23 Q6=A）。

        闲时点 = 开始录音（卡片变「■ 录音中 HH:MM:SS」）；
        录音中再点 = 停止并保存。全程不弹窗——独立录音窗已移除。
        """
        self._toggle()

    @_guard_qt_callback
    def _on_panel_subtitle(self) -> None:
        """「开启字幕」：开字幕；面板保持显示（不再自动收起）；已在运行时只提示。"""
        if self._sub_on:
            self._panel.toast("字幕已在运行——点字幕窗右上角 × 可关闭。", 3000)
            return
        self._toggle_subtitle(True)

    @_guard_qt_callback
    def _on_panel_close(self) -> None:
        """「×」：面板收进托盘（托盘菜单可再唤出）。"""
        self._panel.hide()

    @_guard_qt_callback
    def _toggle(self) -> None:
        if self._recording:
            self._stop_rec()
        else:
            self._start_rec()

    @_guard_qt_callback
    def _toggle_pause(self) -> None:
        """暂停 / 继续（计时与文件冻结；继续后时间轴无缝衔接）。"""
        if self.rec is None:
            return
        state = self.rec.snapshot()["state"]
        hms = _fmt_hms(self.rec.snapshot()["seconds"])
        if state == "paused":
            self.rec.resume()
            self.btn_pause.setText("暂停")
            self.meter.set_active(True)
            # 2026-10-25：底部控制条 ← 「录音中」+ [暂停][停止]
            self._panel.set_rec_controls("recording", "录音中 %s" % hms)
            self._set_status("已继续录音。")
        elif state == "recording":
            self.rec.pause()
            self.btn_pause.setText("继续")
            self.meter.set_active(False)
            # 2026-10-25：底部控制条 ← 「已暂停」+ [继续][停止]
            self._panel.set_rec_controls("paused", "已暂停 %s" % hms)
            self._set_status("已暂停（计时与文件已冻结，点“继续”接着录）。")

    def _pick_capture_target(self):
        """采集目标选择：**有应用在放声就按应用录**（2026-10-25 用户拍板）。

        为什么改：设备回环（WASAPI loopback）采的是端点混音输出，**系统音量会
        直接压低录音电平**（实测一段 -14LUFS 素材被录成 -40dBFS RMS）。进程回环
        （ApplicationLoopback）取的是**端点音量之前**的信号，系统音量再低、
        甚至静音都不影响（2026-10-24 实测静音下峰值 0.985）。

        返回 (pid, 应用名, 是否静音)：
          · record_by_app=True 且有应用在放声 → (pid, 名字, muted)：只录那个应用；
            目标应用中途退出时 Recorder 会自动换最响的应用，没有则回退设备回环；
          · 没有应用在放声 → (0, "", muted)：设备回环（跟系统混音）。
          · record_by_app=False → 永远设备回环（老行为，可在设置里关）。
        """
        try:
            from . import process_loopback as pl
            muted, _vol = pl.system_muted()
            if bool(self.cfg.get("record_by_app", True)):
                procs = pl.active_audio_procs()
                me = os.getpid()
                procs = [p for p in procs if int(p[0]) != me]     # 别录自己
                if procs:
                    pid, pname, _peak = procs[0]
                    return int(pid), pname, muted
            return 0, "", muted
        except Exception as e:                   # noqa: BLE001  探测失败 → 老路径
            import sys
            print("_pick_capture_target 失败（回退设备回环）：%s: %s"
                  % (type(e).__name__, e), file=sys.stderr)
            return 0, "", False

    def _start_rec(self) -> None:
        data = self.combo_fmt.currentData() or ("mp3", 192)
        fmt, br = data
        proc_pid, proc_name, muted = self._pick_capture_target()
        cfg = RecorderConfig(
            device_index=self.combo_device.currentData(),
            formats=[fmt],
            mp3_bitrate=int(br or 192),
            out_dir=self._out_dir(),
            segment_minutes=int(self.cfg.get("segment_minutes", 120) or 0),
            follow_default=bool(self.cfg.get("follow_default", True)),
            process_pid=proc_pid,
            # 2026-10-25：录音端自动增益（很小声时放大再存盘，而不是只在转写时放大）
            auto_gain=bool(self.cfg.get("record_auto_gain", True)),
            # 2026-10-26：人声增强档位（off / mild / strong，设置里三选一）
            voice_boost=_voice_boost_mode(self.cfg.get("record_voice_boost")),
        )
        try:
            self.rec = Recorder(cfg, on_event=self._rec_event_bridge)
            self.rec.start()
        except Exception as e:  # noqa: BLE001
            self.rec = None
            self._set_status("无法开始录音：%s" % e, warn=True)
            return
        self._recording = True
        self._panel.set_card_state("record", "active")   # 卡片：已开启
        self._panel.set_record_time("00:00:00")          # 卡片文案：■ 录音中 00:00:00
        self.btn_main.setText("停止并保存")
        self.btn_main.setProperty("recording", True)
        self.btn_main.style().unpolish(self.btn_main)
        self.btn_main.style().polish(self.btn_main)
        self.combo_device.setEnabled(False)
        self.combo_fmt.setEnabled(False)
        self.btn_refresh.setEnabled(False)
        self.lbl_time.setText("00:00:00")
        self._timer.start()
        self.meter.set_active(True)
        self.btn_pause.setText("暂停")
        self.btn_pause.setVisible(True)
        if self.tray:
            self.act_toggle.setText("停止录音")
            self.tray.setToolTip("AI语音摘要 · 录音中 00:00:00")
        snap = self.rec.snapshot()
        if proc_pid:
            self._set_status("正在录音：%s（按应用录制，系统音量不影响电平）"
                             % (proc_name or "#%d" % proc_pid))
        else:
            self._set_status("正在录音：%s" % snap.get("device_name", ""))
        if proc_pid:
            if muted:
                self._panel.toast("系统静音 → 已按进程录制：%s（无视系统静音）"
                                  % (proc_name or "#%d" % proc_pid), 5000)
            else:
                self._panel.toast("按应用录制：%s（取音量之前的信号，系统音量再低也不影响）"
                                  % (proc_name or "#%d" % proc_pid), 4000)
        elif muted:
            self._panel.toast("系统是静音的，且当前没有应用在放声——这次录不到声音。"
                              "先播放/打开要录的声音再点录音。", 6000, warn=True)
        # 2026-10-25 用户需求：录音中底部出 [暂停][停止] 控制条（带计时）；
        # 静音警告并入控制条文字（随后 toast 会被控制条覆盖，警告不丢）
        tip = "录音中 00:00:00"
        if proc_pid:
            tip += "（按进程录制，无视系统静音）"
        elif muted:
            tip += "（系统静音，可能录不到声音）"
        self._panel.set_rec_controls("recording", tip)

    def _stop_rec(self) -> None:
        if self.rec is None:
            return
        # 2026-10-25：先收起录音控制条（无论后续走哪个分支，暂停/停止按钮都要退出）
        self._panel.set_rec_controls("none", "")
        self._timer.stop()
        r = self.rec.stop()
        self.rec = None
        self._recording = False
        self.btn_main.setText("开始录音")
        self.btn_main.setProperty("recording", False)
        self.btn_main.style().unpolish(self.btn_main)
        self.btn_main.style().polish(self.btn_main)
        self.combo_device.setEnabled(True)
        self.combo_fmt.setEnabled(True)
        self.btn_refresh.setEnabled(True)
        self.meter.set_active(False)
        self.btn_pause.setVisible(False)
        self._panel.flash_card("record", "done", 1600)    # 卡片：刚保存完（1.6s 后回落）
        self._panel.set_record_idle()                     # 卡片文案回到「● 开始应用录音」
        self.lbl_time.setText("00:00:00")
        if self.tray:
            self.act_toggle.setText("开始录音")
            self.tray.setToolTip("AI语音摘要 · 空闲")
        if r and r.get("files"):
            files = list(r["files"])
            self._last_files = files
            name = os.path.basename(files[0])
            if len(files) > 1:
                name += "（等 %d 个文件）" % len(files)
            gap = r.get("gap_seconds", 0.0)
            note = "，补齐静音缺口 %.1f 秒" % gap if gap > 0.5 else ""
            # 2026-10-25 用户方案 A：录完音才预热（子进程加载模型），
            # 开机空闲不再占内存；用户点摘要时模型已就绪。
            self._warm_summary_asr_async(delay_s=1.0)
            # 2026-10-23 Q6=A：独立录音窗已移除，"已保存"态的动作按钮搬到面板 toast
            self._panel.toast_actions(
                "已保存：%s（时长 %.1f 秒%s）" % (name, r["seconds"], note),
                [("打开目录", self._open_out_dir),
                 ("改名…", self._rename_last_files),
                 # 丢弃是破坏性操作：点完就地收起（其余按钮点完保留，
                 # 2026-10-25 用户要求"不要点一下就全消失"）
                 ("丢弃", self._discard_last_files, False)])
        else:
            self._set_status("已停止。")

        # 2026-10-24 三功能解耦：录音只负责存音频。
        # 转写/摘要不再在停止时触发——点「开始 AI 摘要」时摘要自己现场转写。
        if self._sub_auto:
            self._stop_subtitle()

    # ---------------- 停止后的"保存 / 丢弃"流程 ----------------

    # （2026-10-23：独立录音窗已移除。"已保存"态的 打开目录 / 改名… / 丢弃
    #   改为面板 toast 动作按钮，见 _stop_rec → panel.toast_actions）

    @_guard_qt_callback
    def _rename_last_files(self) -> None:
        files = getattr(self, "_last_files", None) or []
        if not files:
            return
        new_path = self._ask_save_name(files[0])
        if not new_path:
            self._panel.toast("已保留原文件名。", 2500)
            return
        out = self._rename_record_files(files, new_path)
        self._last_files = out
        if out:
            self._panel.toast("已改名：%s" % os.path.basename(out[0]), 3000)

    @_guard_qt_callback
    def _discard_last_files(self) -> None:
        files = getattr(self, "_last_files", None) or []
        if not files:
            self._panel.toast("没有可丢弃的文件。", 2500, warn=True)
            return
        self._discard_files(files)
        self._last_files = []
        self._set_status("本次录音已丢弃（文件已移入回收站，可还原）。")

    def _ask_save_or_discard(self, files, seconds) -> list:
        """停止后询问"保存还是丢弃"。

        返回：保存后的文件列表（可能已改名）；丢弃则返回 []（文件移入回收站）。
        对话框与文件操作分离（_ask_save_name / _rename_record_files / _discard_files），
        便于自动化测试直接覆盖各分支。
        """
        # 2026-10-26 修复：重构对话框与文件操作分离时漏掉了 extra 的定义，
        # 导致停止录音后弹保存框时 NameError（_guard_qt_callback 吞掉了，表现为
        # "停止后没有保存提示"）。
        extra = "，共 %d 个文件" % len(files) if len(files) > 1 else ""
        box = msgbox.choose(
            self, "录音已停止",
            "本次录音已停止（时长 %s%s）。\n\n要保存吗？"
            % (_fmt_hms(seconds), extra),
            [("保存（可改文件名）…", "ok"), ("不保存（丢弃）", "danger")])
        if box == "不保存（丢弃）":
            self._discard_files(files)
            return []
        new_path = self._ask_save_name(files[0])
        if new_path:
            return self._rename_record_files(files, new_path)
        return files                        # 取消改名 → 保留原名

    def _ask_save_name(self, first_file) -> str:
        """弹"另存为"对话框（可改目录与文件名）；取消返回空串。

        扩展名强制与源文件一致——内容格式不随改名变化（避免改成 .wav 后打不开）。
        """
        ext = os.path.splitext(first_file)[1] or ".mp3"
        filt = "音频文件 (*%s);;所有文件 (*)" % ext
        new_path, _sel = QFileDialog.getSaveFileName(
            self, "保存录音（可修改文件名）", first_file, filt)
        return new_path or ""

    def _rename_record_files(self, files, new_path) -> list:
        """把本次录制的文件改名到 new_path（分卷的 _pXX 后缀同步改名）；失败保留原名。"""
        stem = os.path.splitext(new_path)[0] or new_path
        ext = os.path.splitext(files[0])[1] or ".mp3"
        out = []
        try:
            for i, f in enumerate(files):
                if i == 0:
                    target = stem + ext
                else:
                    m = re.search(r"_p\d+$", os.path.splitext(os.path.basename(f))[0])
                    suffix = m.group(0) if m else ("_%d" % (i + 1))
                    target = stem + suffix + ext
                if os.path.abspath(target) == os.path.abspath(f):
                    out.append(f)
                    continue
                shutil.move(f, target)      # 跨盘/跨目录安全
                out.append(target)
        except OSError as e:
            self._set_status("重命名失败（已保留原文件名）：%s" % e, warn=True)
            return files
        return out

    def _discard_files(self, files) -> None:
        """丢弃录音：移入回收站（可还原）——不永久删除。"""
        for f in files:
            try:
                _send_to_recycle_bin(f)
            except Exception as e:  # noqa: BLE001
                self._set_status("丢弃失败（文件仍在原处）：%s" % e, warn=True)

    # ---------------- 事件桥（Recorder 线程 -> GUI 线程） ----------------

    def _rec_event_bridge(self, kind, payload) -> None:
        self.sig_event.emit(kind, payload)

    @_guard_qt_callback
    def _on_rec_event(self, kind, payload) -> None:
        if kind == "error":
            self._set_status("⚠ %s" % payload, warn=True)
        elif kind == "device_switched":
            name = payload.get("name") if isinstance(payload, dict) else payload
            self._set_status("已跟随默认输出切换 → %s（切换空档已自动补齐）" % name)
        elif kind == "device_changed":
            name = payload.get("name") if isinstance(payload, dict) else payload
            self._set_status("检测到默认输出变化 → %s" % name)
        elif kind == "capture_recovered":
            name = payload.get("name") if isinstance(payload, dict) else payload
            self._set_status("采集流已自动恢复 → %s" % name)
        elif kind == "segment":
            idx = payload.get("index") if isinstance(payload, dict) else payload
            self._set_status("已自动分卷：第 %s 段" % idx)

    # ---------------- 定时刷新 ----------------

    @_guard_qt_callback
    def _tick(self) -> None:
        if self.rec is None:
            return
        snap = self.rec.snapshot()
        self.lbl_time.setText(_fmt_hms(snap["seconds"]))
        self.meter.set_levels(_dbfs(snap["level_rms"]), _dbfs(snap["level_peak"]))
        if self._recording:
            # Q6=A：录音卡片实时显示「■ 录音中 HH:MM:SS」
            self._panel.set_record_time(_fmt_hms(snap["seconds"]))
            # 2026-10-25：底部控制条同步计时（暂停时文案为「已暂停」）
            _label = "已暂停" if snap.get("state") == "paused" else "录音中"
            self._panel.update_rec_text("%s %s" % (_label, _fmt_hms(snap["seconds"])))
        if self.tray and self._recording:
            self.tray.setToolTip("AI语音摘要 · 录音中 %s" % _fmt_hms(snap["seconds"]))

    # ---------------- 单实例（命名管道） ----------------

    def _on_ipc_connection(self):
        """收到"新实例请求显示窗口"：读出请求、回显窗口。"""
        srv = self.sender() or self._ipc_server
        if srv is None:
            return
        while srv.hasPendingConnections():
            conn = srv.nextPendingConnection()
            try:
                conn.waitForReadyRead(200)
                conn.readAll()
            except Exception:  # noqa: BLE001
                pass
            finally:
                conn.disconnectFromServer()
                conn.deleteLater()
        if self._logger:
            try:
                self._logger("收到新实例请求：显示主窗口")
            except Exception:  # noqa: BLE001
                pass
        self._show_normal()

    # ---------------- 托盘 / 窗口行为 ----------------

    @_guard_qt_callback
    def _tray_activated(self, reason) -> None:
        if self._logger:
            try:
                self._logger("托盘激活：%s" % reason.name)
            except Exception:  # noqa: BLE001
                pass
        if reason == QSystemTrayIcon.ActivationReason.Context:
            self._show_tray_menu()
        elif reason in (QSystemTrayIcon.ActivationReason.Trigger,
                        QSystemTrayIcon.ActivationReason.DoubleClick):
            self._show_normal()

    @_guard_qt_callback
    def _show_tray_menu(self) -> None:
        """在托盘图标上方弹出菜单（菜单右下角对齐鼠标）。

        定位规则参照 Win32 托盘菜单的标准做法（TrackPopupMenu 的
        TPM_RIGHTALIGN | TPM_BOTTOMALIGN：菜单的右边缘/底边对齐给定坐标点），
        等价于"菜单左上角 = 鼠标坐标 − (菜单宽, 菜单高)"，再做屏幕可用区域收拢：
          - 任务栏在屏幕下缘（最常见）时：菜单出现在图标上方、右缘与图标区域对齐；
          - 不会再像之前那样固定在"鼠标的右上角"（Qt 自动弹窗的翻转行为所致）。

        【为什么用 popup + 前台增强，而不是 exec】2026-10-16 矩阵实验实测：
          · 主窗口隐藏（托盘态）时应用没有"可激活窗口"，menu.exec() 会在
            0.01 秒内直接返回、菜单被系统自动关闭 —— 这正是"托盘菜单点了
            没用"的根本机制；
          · popup() 后**立即**用 AttachThreadInput + SetForegroundWindow
            （见 _boost_menu_foreground）把菜单窗口抢到前台 → 菜单保持可见、
            点击正常响应（隐藏/可见两种状态均实测通过）。
        """
        menu = self._tray_menu
        hint = menu.sizeHint()
        pos = QCursor.pos()
        x = pos.x() - hint.width()
        y = pos.y() - hint.height() - 6          # 与鼠标/图标留一点间距
        screen = QApplication.screenAt(pos) or QApplication.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            x = max(avail.left(), min(x, avail.right() - hint.width()))
            y = max(avail.top(), min(y, avail.bottom() - hint.height()))
        menu.popup(QPoint(x, y))
        self._boost_menu_foreground(menu)

    @staticmethod
    def _boost_menu_foreground(menu) -> None:
        """让菜单窗口立即获得前台（托盘态/无激活窗口场景下保住菜单）。

        原理：Windows 前台锁默认不允许后台进程抢前台，但先 AttachThreadInput
        到当前前台线程、再 SetForegroundWindow 即可（经典手法，用完立即解除
        attach）。失败无害——真实托盘右键场景自带输入许可。
        """
        if os.name != "nt":
            return
        try:
            import ctypes
            u32 = ctypes.windll.user32
            k32 = ctypes.windll.kernel32
            hwnd = int(menu.winId())
            fg = u32.GetForegroundWindow()
            tid_fg = u32.GetWindowThreadProcessId(fg, None) if fg else 0
            tid_me = k32.GetCurrentThreadId()
            attached = False
            if tid_fg and tid_fg != tid_me:
                attached = bool(u32.AttachThreadInput(tid_me, tid_fg, True))
            try:
                u32.SetForegroundWindow(hwnd)
            finally:
                if attached:
                    u32.AttachThreadInput(tid_me, tid_fg, False)
        except Exception:  # noqa: BLE001
            pass

    @_guard_qt_callback
    def _show_normal(self) -> None:
        # 界面是独立面板窗口；主窗口只是逻辑载体（2026-10-18）
        panel = getattr(self, "_panel", None)
        if panel is not None:
            self._restore_panel_top()          # 强制恢复置顶：置顶窗口必然可见
            panel.show()
            panel.raise_()
            panel.activateWindow()

    @_guard_qt_callback
    def _quit(self) -> None:
        if self._recording:
            # 提示统一下方（2026-10-18）：退出时不弹确认框，避免误操作丢录音
            self._panel.toast("正在录音——请先点录音卡片「停止并保存」再退出。", 4000, warn=True)
            return
        if self._sub_on:
            self._stop_subtitle()          # 退出前把字幕引擎和窗口收干净
        if self.tray:
            self.tray.hide()
        panel = getattr(self, "_panel", None)
        if panel is not None:
            try:
                panel.close()
            except Exception:  # noqa: BLE001
                pass
        from PyQt6.QtWidgets import QApplication
        QApplication.instance().quit()

    @_guard_qt_callback
    def closeEvent(self, ev):  # noqa: N802
        if self.tray_ok and self.tray is not None:
            ev.ignore()
            # 静默收进托盘（不再弹系统通知——2026-10-16 用户要求去掉）
            self.hide()
        else:
            if self._recording:
                ev.ignore()
                self._quit()
            else:
                ev.accept()


    def _has_assoc(self, ext: str) -> bool:
        """该扩展名在注册表里有没有可用的"打开命令"（2026-10-26）。

        统一走 `_open_command()`，它会把 HKCR / UserChoice 依次解析成命令行；
        解析不到才算"没有关联"。
        """
        return bool(self._open_command(ext))

    def _open_command(self, ext: str) -> str:
        """把扩展名解析成具体命令行（含程序路径），解析不到返回 ""（2026-10-26）。

        为什么不用 `os.startfile`：它只认注册表当前关联。实测（2026-10-26）：
        `HKCR\\.md` 被关联到 **WorkBuddy.md**（本会话宿主），于是
        `os.startfile(md)` 把文件交给了 WorkBuddy —— 用户看到的"点了没反应"
        （Typora 根本没被调起）。用户要的是**用真正处理该格式的编辑器**打开。
        解析顺序：
          ① `HKCU ...\\FileExts\\.<ext>\\UserChoice` 的 Progid（用户显式设置）
          ② `HKCR\\.<ext>` 默认值 → Progid
          ③ 逐层下钻 Progid\\shell\\open\\command，取命令行
        返回的命令行里含 `%1` 占位符，调用方用文件名替换。
        """
        if not ext:
            return ""
        import winreg
        progids = []
        try:                                   # ① 用户显式设置
            k = (r"Software\Microsoft\Windows\CurrentVersion\Explorer"
                 r"\FileExts\%s\UserChoice" % ext)
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, k) as hk:
                v, _ = winreg.QueryValueEx(hk, "Progid")
                if v:
                    progids.append(str(v))
        except Exception:                      # noqa: BLE001
            pass
        try:                                   # ② 经典关联
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, ext) as hk:
                v, _ = winreg.QueryValueEx(hk, "")
                if v:
                    progids.append(str(v))
        except Exception:                      # noqa: BLE001
            pass
        for pid in progids:
            for path in (r"%s\shell\open\command" % pid,
                         r"Applications\%s\shell\open\command" % pid):
                try:
                    with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, path) as hk:
                        cmd, _ = winreg.QueryValueEx(hk, "")
                        if cmd and str(cmd).strip():
                            return str(cmd).strip()
                except Exception:              # noqa: BLE001
                    continue
        return ""


class LevelMeter(QWidget):
    """实时电平表：RMS 渐变色条 + Peak 白刻度（-60..0 dBFS 映射 0..100%）。

    空闲（未录音）时显示用途提示文字，录音中显示动态电平。
    """

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(26)
        self.setMaximumHeight(26)
        self._rms = -120.0
        self._peak = -120.0
        self._idle = True

    def set_active(self, active: bool) -> None:
        """录音状态切换：空闲时显示提示文字，录音时显示动态电平。"""
        self._idle = not active
        if not active:
            self._rms = self._peak = -120.0
        self.update()

    def set_levels(self, rms_dbfs: float, peak_dbfs: float) -> None:
        self._rms = rms_dbfs
        self._peak = peak_dbfs
        self.update()

    @staticmethod
    def _pct(db: float) -> float:
        return max(0.0, min(100.0, (db + 60.0) / 60.0 * 100.0))

    def paintEvent(self, ev):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#1E2129"))
        p.drawRoundedRect(0, 0, w, h, 6, 6)
        if self._idle:
            # 空闲态：显示用途提示（消除"这条黑块是什么"的困惑——2026-10-16 用户反馈）
            p.setPen(QColor("#8E97A6"))
            f = p.font()
            f.setPixelSize(11)
            p.setFont(f)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "实时电平 · 录音时显示")
            p.end()
            return
        rms_w = int(w * self._pct(self._rms) / 100.0)
        if rms_w > 0:
            g = QLinearGradient(0, 0, w, 0)
            g.setColorAt(0.0, QColor("#3FBF6B"))
            g.setColorAt(0.65, QColor("#E8C86A"))
            g.setColorAt(1.0, QColor("#E0563F"))
            p.setBrush(g)
            p.drawRoundedRect(0, 0, max(rms_w, 8), h, 6, 6)
        pk_x = int(w * self._pct(self._peak) / 100.0)
        if pk_x > 0:
            p.setPen(QPen(QColor("#F2F2F2"), 2))
            p.drawLine(min(pk_x, w - 2), 3, min(pk_x, w - 2), h - 3)
        p.end()
