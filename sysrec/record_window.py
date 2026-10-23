# -*- coding: utf-8 -*-
"""录音控制窗（独立子窗口）。

2026-10-18 用户要求：
  · 点「开始应用录音」弹出**全新子窗口**（不在主面板上改态）；
  · 窗口颜色 rgb(73, 78, 93) = #494E5D，透明度与主面板一致（panel_opacity，默认 0.80）；
  · 停止后本窗口进入"已保存"态，**改名… / 打开目录 / 丢弃** 三个动作放在这里。
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QPushButton,
                             QVBoxLayout, QWidget)

BG = "#494E5D"                       # rgb(73, 78, 93)（用户指定）
FG = "#FFFFFF"
FG_DIM = "rgba(255,255,255,0.80)"

BTN = ("QPushButton{background:rgba(255,255,255,0.16);border:none;"
       "border-radius:9px;color:%s;font-size:13px;padding:0 14px;}"
       "QPushButton:hover{background:rgba(255,255,255,0.28);}" % FG)


class _LevelBar(QWidget):
    """录音电平条（白底 + 亮条 + 峰值线）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rms01 = 0.0
        self._peak01 = 0.0
        self.setFixedHeight(8)

    def set_levels(self, rms01: float, peak01: float) -> None:
        self._rms01 = max(0.0, min(1.0, float(rms01)))
        self._peak01 = max(0.0, min(1.0, float(peak01)))
        self.update()

    def paintEvent(self, ev):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("rgba(255,255,255,0.22)"))
        p.drawRoundedRect(0, 0, w, h, h // 2, h // 2)
        rms_w = int(w * self._rms01)
        if rms_w > 0:
            p.setBrush(QColor("rgba(255,255,255,0.92)"))
            p.drawRoundedRect(0, 0, max(rms_w, h), h, h // 2, h // 2)
        pk = int(w * self._peak01)
        if pk > 0:
            p.setPen(QPen(QColor("#FFE08A"), 2))
            p.drawLine(min(pk, w - 2), 0, min(pk, w - 2), h)
        p.end()


class RecordWindow(QWidget):
    """录音控制窗：录音态（计时/电平/暂停/停止）→ 已保存态（改名/打开目录/丢弃）。"""

    sig_pause = pyqtSignal()      # 暂停 / 继续
    sig_stop = pyqtSignal()       # 停止并保存
    sig_rename = pyqtSignal()     # 已保存态：改名…
    sig_open = pyqtSignal()       # 已保存态：打开目录
    sig_discard = pyqtSignal()    # 已保存态：丢弃（回收站）
    sig_closed = pyqtSignal()     # 已保存态：关闭窗口

    def __init__(self, cfg=None):
        super().__init__(None)
        c = cfg or {}
        self._drag_origin = None
        self._drag_moved = False
        self._saved_mode = False
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setWindowTitle("录音中")
        self.setObjectName("RecWin")
        self.setStyleSheet("#RecWin{background:%s;}" % BG)
        # 透明度与主面板一致（2026-10-18 用户要求）
        self.setWindowOpacity(float(c.get("panel_opacity", 0.80) or 0.80))
        self.setFixedSize(340, 206)

        v = QVBoxLayout(self)
        v.setContentsMargins(18, 14, 18, 16)
        v.setSpacing(10)

        # ---- 标题行 ----
        head = QHBoxLayout()
        head.setSpacing(8)
        self._dot = QLabel("●")
        self._dot.setStyleSheet("color:#FF5B4D;font-size:12px;background:transparent;")
        head.addWidget(self._dot)
        self._title = QLabel("录音中")
        self._title.setStyleSheet(
            "color:%s;font-size:14px;font-weight:600;background:transparent;" % FG)
        head.addWidget(self._title)
        head.addStretch(1)
        self._btn_close = QPushButton("×")
        self._btn_close.setFixedSize(24, 24)
        self._btn_close.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_close.setToolTip("停止并保存")
        self._btn_close.setStyleSheet(
            "QPushButton{background:transparent;border:none;color:%s;font-size:16px;}"
            "QPushButton:hover{background:rgba(255,255,255,0.18);border-radius:12px;}"
            % FG_DIM)
        self._btn_close.clicked.connect(self._on_close)
        head.addWidget(self._btn_close)
        v.addLayout(head)

        # ---- 录音态 ----
        self._rec_box = QWidget(self)
        rv = QVBoxLayout(self._rec_box)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(10)
        self._lbl_time = QLabel("00:00:00")
        self._lbl_time.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_time.setStyleSheet(
            "color:%s;font-size:40px;font-weight:600;background:transparent;" % FG)
        rv.addWidget(self._lbl_time)
        self._meter = _LevelBar(self._rec_box)
        rv.addWidget(self._meter)
        row = QHBoxLayout()
        row.setSpacing(10)
        self._btn_pause = QPushButton("暂停")
        self._btn_pause.setFixedHeight(34)
        self._btn_pause.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_pause.setStyleSheet(BTN)
        self._btn_pause.clicked.connect(self.sig_pause)
        self._btn_stop = QPushButton("停止并保存")
        self._btn_stop.setFixedHeight(34)
        self._btn_stop.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_stop.setStyleSheet(
            "QPushButton{background:#9E3B2E;border:none;border-radius:9px;"
            "color:#FFFFFF;font-size:13px;padding:0 16px;}"
            "QPushButton:hover{background:#B04A3C;}")
        self._btn_stop.clicked.connect(self.sig_stop)
        row.addStretch(1)
        row.addWidget(self._btn_pause)
        row.addWidget(self._btn_stop)
        row.addStretch(1)
        rv.addLayout(row)
        v.addWidget(self._rec_box)

        # ---- 已保存态：提示 + 改名/打开目录/丢弃 ----
        self._saved_box = QWidget(self)
        sv = QVBoxLayout(self._saved_box)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(12)
        self._saved_label = QLabel("")
        self._saved_label.setWordWrap(True)
        self._saved_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._saved_label.setStyleSheet(
            "color:%s;font-size:13px;background:transparent;" % FG)
        sv.addWidget(self._saved_label)
        arow = QHBoxLayout()
        arow.setSpacing(8)
        self._btn_rename = QPushButton("改名…")
        self._btn_open = QPushButton("打开目录")
        self._btn_discard = QPushButton("丢弃")
        for b, sig in ((self._btn_rename, self.sig_rename),
                       (self._btn_open, self.sig_open),
                       (self._btn_discard, self.sig_discard)):
            b.setFixedHeight(32)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(BTN)
            b.clicked.connect(sig)
            arow.addWidget(b)
        sv.addLayout(arow)
        self._saved_box.hide()
        v.addWidget(self._saved_box)

    # ---------- 状态 ----------

    def set_time(self, text: str) -> None:
        self._lbl_time.setText(text)

    def set_levels(self, rms01: float, peak01: float) -> None:
        self._meter.set_levels(rms01, peak01)

    def set_paused(self, paused: bool) -> None:
        self._btn_pause.setText("继续" if paused else "暂停")

    def show_saved(self, name: str, seconds: float, note: str = "") -> None:
        """录音停止后进入"已保存"态（动作按钮在这里，2026-10-18 用户要求）。"""
        self._saved_mode = True
        self._dot.hide()
        self._title.setText("已保存")
        self._btn_close.setToolTip("关闭")
        self._rec_box.hide()
        self._saved_label.setText(
            "已保存：%s\n时长 %.1f 秒%s" % (name, seconds, note))
        self._saved_box.show()

    def _on_close(self) -> None:
        if self._saved_mode:
            self.sig_closed.emit()
        else:
            self.sig_stop.emit()

    # ---------- 位置 / 拖动 ----------

    def place_near(self, anchor) -> None:
        if anchor is not None and anchor.isVisible():
            g = anchor.geometry()
            x = g.center().x() - self.width() // 2
            y = g.bottom() + 12
            scr = anchor.screen() or QApplication.primaryScreen()
            av = scr.availableGeometry()
            x = max(av.left() + 8, min(x, av.right() - self.width() - 8))
            y = max(av.top() + 8, min(y, av.bottom() - self.height() - 8))
            self.move(x, y)
        else:
            self.place_default()

    def place_default(self) -> None:
        scr = QApplication.primaryScreen()
        if scr is None:
            return
        av = scr.availableGeometry()
        self.move(av.x() + (av.width() - self.width()) // 2,
                  av.y() + av.height() - self.height() - 160)

    def mousePressEvent(self, e):  # noqa: N802
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_origin = (e.globalPosition().toPoint()
                                 - self.frameGeometry().topLeft())
            self._drag_moved = False
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):  # noqa: N802
        if self._drag_origin is not None and (e.buttons() & Qt.MouseButton.LeftButton):
            self.move(e.globalPosition().toPoint() - self._drag_origin)
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):  # noqa: N802
        self._drag_origin = None
        super().mouseReleaseEvent(e)
