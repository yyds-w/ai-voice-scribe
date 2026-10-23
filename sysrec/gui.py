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
from .transcript import Transcript
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


class MainWindow(QMainWindow):
    sig_event = pyqtSignal(str, object)
    sig_summary_ok = pyqtSignal(str, str)      # LLM 摘要完成：(path, text)
    sig_summary_fail = pyqtSignal(str)         # LLM 摘要失败：(msg,)
    sig_summary_step = pyqtSignal(str)         # 摘要进度（分段提炼 x/y…）

    def __init__(self, ipc_server=None, logger=None):
        super().__init__()
        self._ipc_server = ipc_server      # 单实例命名管道（由 run.py 传入）
        self._logger = logger              # 可选日志回调（run.py 的 _log）
        self.cfg = config_mod.load()
        self.rec = None
        self._recording = False
        self.tray_ok = QSystemTrayIcon.isSystemTrayAvailable()
        # 实时转文字（字幕窗只是"显示"；转写稿落盘另有一套，见 transcript.py）
        self._sub_on = False
        self._sub_auto = False             # True = 录音时自动开的（不弹字幕窗）
        self.sub_engine = None
        # 转写稿 / 摘要
        self.transcript = None             # 当前这次录音的 Transcript
        self._last_transcript_path = ""    # 本次会话最后一份落盘的转写稿（摘要素材用，
                                           # 不用目录里"最新"的旧稿，见 _on_panel_summary）
        self._summary_busy = False
        self._summary_auto = False         # True = 自动跑的摘要（完成后不弹模态框）
        self._last_summary_path = ""
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
        self.sig_summary_ok.connect(self._summary_done)
        self.sig_summary_fail.connect(self._on_summary_fail)
        self.sig_summary_step.connect(self._summary_step)
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._tick)

        self._build_ui()
        self._build_tray()
        self._refresh_devices()
        self._set_status("就绪：点“开始录音”，录制电脑正在播放的声音。")

        # 单实例唤醒：其他实例启动时经命名管道请求本实例显示窗口
        if self._ipc_server is not None:
            self._ipc_server.newConnection.connect(self._on_ipc_connection)
            if self._ipc_server.hasPendingConnections():
                QTimer.singleShot(0, self._on_ipc_connection)

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
        return self.cfg.get("output_dir") or config_mod.default_output_dir()

    def _transcript_dir(self) -> str:
        return self.cfg.get("transcript_dir") or config_mod.default_transcript_dir()

    def _summary_dir(self) -> str:
        return self.cfg.get("summary_dir") or config_mod.default_summary_dir()

    # ---------------- 转写稿（后台记录）----------------

    def _begin_transcript(self) -> None:
        """开一份新的转写稿（落盘）。上一次的没关就先关掉。"""
        self._finish_transcript()
        try:
            t = Transcript()
            path = Transcript.new_path(self._transcript_dir())
            dev = ""
            try:
                dev = self.combo_device.currentText() if self.combo_device.count() else ""
            except Exception:                     # noqa: BLE001
                dev = ""
            t.open(path, title=("设备：%s" % dev) if dev else "")
            self.transcript = t
        except Exception as e:                    # noqa: BLE001
            self.transcript = None
            self._set_status("转写稿无法落盘：%s（录音照常）" % e, warn=True)

    def _finish_transcript(self) -> str:
        """收尾转写稿，返回全文（给摘要用）。没有则返回 ""。"""
        t = self.transcript
        self.transcript = None
        if t is None:
            return ""
        try:
            info = t.close()
        except Exception:                         # noqa: BLE001
            return ""
        if not info.get("lines"):                 # 一句话都没转出来 → 别留个只有头的空文件
            try:
                os.remove(info.get("path") or "")
            except Exception:                     # noqa: BLE001
                pass
            self._last_transcript_path = ""      # 本次没内容 → 不留给摘要当素材
            return ""
        self._last_transcript_path = info.get("path") or ""   # 本次会话刚落盘的稿
        try:
            return t.text()
        except Exception:                         # noqa: BLE001
            return ""

    def _start_transcribe_bg(self) -> bool:
        """录音时后台自动转文字：引擎照常跑，但**不弹字幕窗**。"""
        if self._sub_on and self.sub_engine is not None:
            return True                           # 用户已经开着字幕了，直接复用
        eng = SubtitleEngine(self.combo_device.currentData(), self.cfg)
        eng.line.connect(self._on_sub_line)
        eng.partial.connect(self._on_sub_partial)
        eng.status.connect(lambda t: self._set_status(t, toast=False))
        eng.failed.connect(self._on_sub_failed)
        self.sub_engine = eng
        if not eng.start():
            self.sub_engine = None
            return False
        self._sub_on = True
        self._sub_auto = True
        self.btn_sub.setChecked(True)
        self.btn_sub.setText("关闭实时字幕")
        self._panel.set_card_state("subtitle", "active")
        return True

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

        eng = SubtitleEngine(self.combo_device.currentData(), self.cfg)
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

    def _stop_subtitle(self) -> None:
        # 录音还在进行时：字幕窗可以关，但后台转写必须继续——
        # 否则转写稿从这一刻起断档，停止后生成的摘要会缺后半场。
        if (self._recording and self.transcript is not None
                and self.transcript.running and self.sub_engine is not None):
            if self.sub_win is not None:
                self.sub_win.hide()
            return
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
        return profile_client_cfg(prof) if prof else {}

    @_guard_qt_callback
    def _on_sub_summary(self) -> None:
        if self.sub_win is None:
            return
        lines = self.sub_win.lines()
        if not lines:
            self._set_status("还没有字幕可摘要（先开实时转文字、说几句话）", warn=True)
            return
        self._start_summary("\n".join(lines), auto=False)

    def _resolve_summary_text(self):
        """摘要素材解析（2026-10-23 用户方案 B：本次没有稿就取消，绝不拿历史旧稿充数）。

        返回 (text, src, reason)：
          reason="live"    = 当前会话内存里的转写稿（录音中/刚停止）
          reason="session" = 本次会话刚落盘的那份（停止时收尾的）
          reason="none"   = 本次没有任何转写稿 → 调用方取消摘要并引导
        刻意不用 `_latest_transcript_path()`（目录里文件名最新的稿）——那是
        别次录音/别天开的字幕留下的历史稿，静默用它就是"摘要内容是上一段音频"
        bug 的根因（Q7=B 关自动转写后，纯录音永远没有本次稿）。
        """
        t = self.transcript
        if t is not None and len(t):
            return t.text(), (t.path or ""), "live"
        p = self._last_transcript_path
        if p:
            try:
                text = Transcript.load(p).text()
            except Exception:                    # noqa: BLE001  文件被删/损坏 → 当没有
                text = ""
            if text.strip():
                return text, p, "session"
        return "", "", "none"

    @_guard_qt_callback
    def _on_panel_summary(self) -> None:
        """面板「开始 AI 摘要」：对**本次**转写稿生成纪要（方案 B）。"""
        if self._summary_busy:
            self._panel.toast("摘要正在生成中，请稍候…", 2500)
            return
        if self._recording:
            self._panel.toast("录音中——停止并保存后会自动生成摘要。", 3500)
            return
        text, src, reason = self._resolve_summary_text()
        if reason == "none":
            # B 方案：本次没有转写稿 → 取消本次摘要 + 正确引导（不碰历史旧稿）
            self._panel.toast(
                "本次录音没有转写稿（设置里「录音时后台自动转文字」当前是关的）。"
                "要生成摘要：开启字幕录音，或在设置里把它打开。", 6000, warn=True)
            return
        self._panel.toast("正在用 %d 字转写稿生成摘要…" % len(text), 4000)
        self._start_summary(text, auto=True, src=src)

    def _start_summary(self, text: str, auto: bool = False, src: str = "") -> None:
        """统一的摘要入口（自动 / 手动都走这里）。"""
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
        threading.Thread(target=self._summary_work, args=(text, src),
                         daemon=True).start()

    def _summary_step(self, msg: str) -> None:
        self._set_status(msg)

    def _summary_work(self, text: str, src: str = "") -> None:
        try:
            from .llm import LlmClient, summarize_long
            client = LlmClient(self._llm_cfg())
            chunk = int(self.cfg.get("summary_chunk_chars", 6000) or 0)
            seg = int(self.cfg.get("summary_segment_minutes", 20) or 0)
            out = summarize_long(
                client, text, chunk_chars=chunk, segment_minutes=seg,
                progress=lambda m: self.sig_summary_step.emit(m))
            d = self._summary_dir()
            os.makedirs(d, exist_ok=True)
            now = QDateTime.currentDateTime()
            path = os.path.join(d, "摘要_%s.md" % now.toString("yyyyMMdd_hhmmss"))
            with open(path, "w", encoding="utf-8") as f:
                f.write("# 摘要（%s）\n\n%s\n\n---\n\n## 原始转写（%d 字）\n\n%s\n"
                        % (now.toString("yyyy-MM-dd hh:mm:ss"), out, len(text), text))
            self.sig_summary_ok.emit(path, out)      # 跨线程：worker → GUI 主线程
        except Exception as e:  # noqa: BLE001
            self.sig_summary_fail.emit("摘要失败：%s" % e)

    def _summary_done(self, path: str, out: str) -> None:
        self._summary_busy = False
        self._last_summary_path = path
        self._panel.flash_card("summary", "done", 2000)   # 卡片：刚生成完
        name = os.path.basename(path)
        if self._summary_auto:
            # 自动摘要不弹模态框打断人，面板轻提示 + 状态栏
            self._panel.toast("摘要已生成：%s（在「摘要」目录）" % name, 6000)
            self._set_status("摘要已生成：%s" % path)
            return
        shown = out[:800] + ("\n……（显示已截断）" if len(out) > 800 else "")
        msgbox.info(self, "摘要已生成",
                    "%s\n\n完整版与原始字幕已存：\n%s" % (shown, path))

    def _on_summary_fail(self, msg: str) -> None:
        self._summary_busy = False
        self._panel.set_card_state("summary", "idle")     # 卡片：失败 → 复位
        self._set_status(msg, warn=True)
        if self._summary_auto:
            self._panel.toast(msg, 6000, warn=True)

    @_guard_qt_callback
    def _on_sub_line(self, txt: str) -> None:
        if self.sub_win is not None:
            self.sub_win.add_line(txt)
        if self.transcript is not None:          # 边转边落盘（不封顶，不受字幕窗 200 行限制）
            try:
                self.transcript.add(txt)
            except Exception:                    # noqa: BLE001
                pass

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
        if state == "paused":
            self.rec.resume()
            self.btn_pause.setText("暂停")
            self.meter.set_active(True)
            self._set_status("已继续录音。")
        elif state == "recording":
            self.rec.pause()
            self.btn_pause.setText("继续")
            self.meter.set_active(False)
            self._set_status("已暂停（计时与文件已冻结，点“继续”接着录）。")

    def _start_rec(self) -> None:
        data = self.combo_fmt.currentData() or ("mp3", 192)
        fmt, br = data
        cfg = RecorderConfig(
            device_index=self.combo_device.currentData(),
            formats=[fmt],
            mp3_bitrate=int(br or 192),
            out_dir=self._out_dir(),
            segment_minutes=int(self.cfg.get("segment_minutes", 120) or 0),
            follow_default=bool(self.cfg.get("follow_default", True)),
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
        self._set_status("正在录音：%s（系统音量即使为 0 也不影响录制）" % snap.get("device_name", ""))

        # 后台转文字：录音一开就把转写稿建起来（不弹字幕窗，只落盘）
        auto_tr = bool(self.cfg.get("auto_transcribe", True))
        if auto_tr or self._sub_on:
            self._begin_transcript()
        if auto_tr:
            if self._start_transcribe_bg():
                self._panel.toast("已开始后台转文字，停止后自动生成摘要。", 4500)
            else:
                self._panel.toast("后台转文字没起来（见状态栏），录音照常。", 5000, warn=True)

    def _stop_rec(self) -> None:
        if self.rec is None:
            return
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
            # 2026-10-23 Q6=A：独立录音窗已移除，"已保存"态的动作按钮搬到面板 toast
            self._panel.toast_actions(
                "已保存：%s（时长 %.1f 秒%s）" % (name, r["seconds"], note),
                [("打开目录", self._open_out_dir),
                 ("改名…", self._rename_last_files),
                 ("丢弃", self._discard_last_files)])
        else:
            self._set_status("已停止。")

        # ---- 收尾：先停后台转写（避免还有行写进来）→ 再关转写稿 → 自动摘要 ----
        if self._sub_auto:
            self._stop_subtitle()
        text = self._finish_transcript()
        if text.strip():
            n = len(text)
            self._panel.toast("转写稿已保存（%d 字）%s" % (
                n, "，正在生成摘要…" if self.cfg.get("auto_summary", True) else ""), 5000)
            if self.cfg.get("auto_summary", True):
                self._start_summary(text, auto=True)

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
