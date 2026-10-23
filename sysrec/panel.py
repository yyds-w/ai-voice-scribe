# -*- coding: utf-8 -*-
"""AI 摘记面板（对标一加 11 ColorOS「AI 语音摘记」浮窗）。

深色毛玻璃半透明置顶浮窗，比例 ≈3.3:1（宽扁），三个功能卡片：应用录音 / AI 摘要 / 字幕。
卡片图标取自实机截图（assets/panel_*.png）；缺失时用 QPainter 兜底（白色实心字形 + 深色镂空）。
录音时面板切换为"录音中"状态（计时 + 电平 + 停止/暂停）。
"""
from __future__ import annotations

import math
import os

from PyQt6.QtCore import QPointF, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (QBrush, QColor, QConicalGradient, QLinearGradient,
                         QPainter, QPen, QPixmap, QPolygonF)
from PyQt6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel,
                             QPushButton, QVBoxLayout, QWidget)

from . import app_paths

PANEL_W = 560          # 面板尺寸：对标实机 ≈3.3:1 的宽扁浮窗
PANEL_H = 186          # 2026-10-19：下方提示区高度减半（原 43px → 21px，208 → 186）
REC_PANEL_H = 190      # 录音态需要更高的面板，容纳计时 / 电平 / 按钮
ICON_SIDE = 72         # 卡片图标显示边长（图标为"圆盘+字形"整体，源图 324px）


def _icon_path(name: str) -> str:
    return app_paths.resource_path("assets", "panel_%s.png" % name)


_ICON_CACHE: dict = {}


def panel_icon(name: str):
    """从参考截图提取的图标（assets/panel_*.png）；缺失时返回 None，走手绘兜底。"""
    if name not in _ICON_CACHE:
        pm = QPixmap(_icon_path(name))
        _ICON_CACHE[name] = None if pm.isNull() else pm
    return _ICON_CACHE[name]

BG = "rgba(29, 36, 55, 236)"        # 面板底：实测半透明深蓝（≈#1D2437，随壁纸浮动）
CARD = "#21FFFFFF"   # 卡片底 13%（QColor 不认 rgba() 字符串！必须用 #AARRGGBB）
CARD_HOVER = "#33FFFFFF"   # 20%（悬停提亮底色）
FG = "#E8ECF2"                       # 主文字（实测近白偏冷）
FG_DIM = "#A9B2C0"                   # 次文字（冷灰）
WHITE = "#FFFFFF"                    # 图标 / 字形（实机为纯白）
CHIP = "#26FFFFFF"                   # 右上按钮片 ≈15% 白
DARK_CUT = "#232A38"                 # 兜底图标里模拟"镂空"的深色
BORDER = "#1AFFFFFF"   # 10%（QSS 与 QColor 都能解析 #AARRGGBB）

# ---- 卡片点击后的三种状态（2026-10-20 新增）----
# 语义：active=已开启（持续态）/ busy=处理中（临时态）/ done=刚完成（1.5s 反馈后回落）
# 配色沿用面板既有的"深色底 + 白色图形"语言，只换强调色，不换布局与尺寸。
CARD_ON = "#26FFD98F"        # 已激活：暖金 15% 底（再高会叠成橄榄色）
CARD_ON_HOVER = "#38FFD98F"  # 已激活 + 悬停：22%
BORDER_ON = "#C4FFD98F"      # 已激活：金色描边 77%
FG_ON = "#FFF3D8"            # 已激活：近白微暖（不用纯金——12px 金字压在金底上会糊）
ACCENT_ON = "#FFCE78"        # 已激活：金色指示点 / 图标光环

CARD_BUSY = "#2478B6FF"      # 处理中：青蓝 14% 底
CARD_BUSY_HOVER = "#3578B6FF"
BORDER_BUSY = "#B078B6FF"
FG_BUSY = "#E6F1FF"
ACCENT_BUSY = "#7FC0FF"

CARD_DONE = "#248FD98A"      # 刚完成：柔绿 14% 底
BORDER_DONE = "#B08FD98A"
FG_DONE = "#E8F8E4"
ACCENT_DONE = "#8FE08A"

_STATE_STYLE = {
    "idle": (CARD, CARD_HOVER, BORDER, FG, None),
    "active": (CARD_ON, CARD_ON_HOVER, BORDER_ON, FG_ON, ACCENT_ON),
    "busy": (CARD_BUSY, CARD_BUSY_HOVER, BORDER_BUSY, FG_BUSY, ACCENT_BUSY),
    "done": (CARD_DONE, CARD_DONE, BORDER_DONE, FG_DONE, ACCENT_DONE),
}


# ---------------- 小图标 ----------------

class _WaveIcon(QWidget):
    """标题左侧图标：白色声波（约 5 条高低不等的竖条）+ 右上角星芒（对标实机）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(22, 18)
        # 鼠标透明：点击穿透到面板（按住标题区拖动面板）
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def paintEvent(self, ev):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(WHITE))
        for x, h in ((3, 5), (7, 10), (11, 16), (15, 9), (19, 5)):
            p.drawRoundedRect(x, (18 - h) // 2 + 2, 2, h, 1, 1)
        _paint_star(p, 17, 4, 4)
        p.end()


class _CircleBtn(QPushButton):
    """右上角小按钮（gear / close）：浅色圆角按钮片 + 白色字形（对标实机）。"""

    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        self._kind = kind
        self.setFixedSize(34, 34)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet("background:transparent;border:none;")

    def paintEvent(self, ev):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect()
        c = r.center()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(CHIP))                       # 浅色圆角按钮片
        p.drawRoundedRect(r.adjusted(1, 1, -1, -1), 10, 10)

        pm = panel_icon(self._kind)                    # 实机截图裁出的真实图标
        if pm is not None:
            side = 18
            p.drawPixmap(QRect(c.x() - side // 2, c.y() - side // 2, side, side), pm)
        elif self._kind == "close":
            p.setPen(QPen(QColor(WHITE), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            m = 5
            p.drawLine(c.x() - m, c.y() - m, c.x() + m, c.y() + m)
            p.drawLine(c.x() + m, c.y() - m, c.x() - m, c.y() + m)
        else:
            _paint_gear(p, c.x(), c.y())
        p.end()


def _paint_star(p: QPainter, cx: int, cy: int, r: int) -> None:
    """四角星芒（白色，对标实机标题右上角）。"""
    pts = [QPointF(cx, cy - r), QPointF(cx + r * 0.28, cy - r * 0.28),
           QPointF(cx + r, cy), QPointF(cx + r * 0.28, cy + r * 0.28),
           QPointF(cx, cy + r), QPointF(cx - r * 0.28, cy + r * 0.28),
           QPointF(cx - r, cy), QPointF(cx - r * 0.28, cy - r * 0.28)]
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(WHITE))
    p.drawPolygon(QPolygonF(pts))


def _paint_gear(p: QPainter, cx: int, cy: int) -> None:
    """齿轮兜底：白色圆浑六齿 + 中心镂空（对标实机）。"""
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(WHITE))
    for ang in range(0, 360, 60):
        a = math.radians(ang)
        p.drawEllipse(QRect(int(cx + 6 * math.cos(a)) - 3,
                            int(cy + 6 * math.sin(a)) - 3, 6, 6))
    p.drawEllipse(QRect(cx - 7, cy - 7, 14, 14))
    p.setBrush(QColor(DARK_CUT))
    p.drawEllipse(QRect(cx - 3, cy - 3, 6, 6))


def _paint_rainbow_ring(p: QPainter, cx: int, cy: int, r: int = 27) -> None:
    """细的彩虹环：全光谱 conic 渐变（对标实机“AI 摘要”外圈）。"""
    g = QConicalGradient(QPointF(cx, cy), -90.0)     # QConicalGradient 只收 QPointF
    for ang in range(0, 361, 30):
        g.setColorAt(ang / 360.0, QColor.fromHsvF((ang % 360) / 360.0, 0.72, 1.0))
    p.setPen(QPen(QBrush(g), 3.0))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawEllipse(QRect(cx - r, cy - r, r * 2, r * 2))


def _paint_record_icon(p: QPainter, cx: int, cy: int) -> None:
    """录音兜底：白色实心圆角矩形 + 深色镂空的“录音带”形状（对标实机）。"""
    w, h = 36, 25
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(WHITE))
    p.drawRoundedRect(cx - w // 2, cy - h // 2, w, h, 6, 6)
    p.setBrush(QColor(DARK_CUT))
    p.drawEllipse(cx - 12, cy - 5, 9, 9)
    p.drawEllipse(cx + 3, cy - 5, 9, 9)
    p.drawRoundedRect(cx - 6, cy - 3, 12, 5, 2, 2)


def _paint_subtitle_icon(p: QPainter, cx: int, cy: int) -> None:
    """字幕兜底：白色实心圆角矩形 + 深色镂空的两行字幕条（对标实机）。"""
    w, h = 38, 25
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(WHITE))
    p.drawRoundedRect(cx - w // 2, cy - h // 2, w, h, 6, 6)
    p.setBrush(QColor(DARK_CUT))
    p.drawRoundedRect(cx - 13, cy - 5, 18, 4, 2, 2)
    p.drawRoundedRect(cx + 8, cy - 5, 5, 4, 2, 2)
    p.drawRoundedRect(cx - 13, cy + 3, 12, 4, 2, 2)


def _paint_summary_icon(p: QPainter, cx: int, cy: int) -> None:
    """摘要兜底：彩虹环 + 白色实心文档（含灰线）+ 白色星芒（对标实机）。"""
    _paint_rainbow_ring(p, cx, cy, 27)
    w, h = 26, 30
    dx, dy = cx - w // 2, cy - h // 2
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(WHITE))
    p.drawRoundedRect(dx, dy, w, h, 4, 4)
    p.setBrush(QColor("#9AA3AE"))        # 文档内的灰色文本行
    for i, bw in enumerate((12, 16, 10)):
        p.drawRoundedRect(dx + 5, dy + 9 + i * 6, bw, 2, 1, 1)
    p.setBrush(QColor(DARK_CUT))         # 给星芒留出镂空底
    p.drawRoundedRect(dx + w - 10, dy - 2, 10, 9, 2, 2)
    _paint_star(p, dx + w - 4, dy + 3, 7)


class _CardBtn(QPushButton):
    """功能大卡片：自绘卡片底 + 图标 + 文字。

    2026-10-18：支持「按住拖动 = 移动面板」——QPushButton 子类默认独吞鼠标事件，
    导致用户按在卡片上拖不动面板（反馈"只能固定在那片区域"）。
    判定：按下后移动超过 6px → 进入拖动模式（转发给面板），松手时不算点击。
    """

    def __init__(self, icon: str, text: str, parent=None):
        super().__init__(text, parent)
        self._icon = icon
        self._hover = False
        self._state = "idle"          # idle / active / busy / done
        self._press_pos = None
        self._moved = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(106)
        self.setStyleSheet("background:transparent;border:none;")

    # ---------- 状态 ----------

    def set_state(self, state: str) -> None:
        """切换卡片状态：idle（未点）/ active（已开启）/ busy（处理中）/ done（刚完成）。"""
        if state not in _STATE_STYLE:
            state = "idle"
        if state == self._state:
            return
        self._state = state
        self.update()

    def state(self) -> str:
        return self._state

    def mousePressEvent(self, e):  # noqa: N802
        if e.button() == Qt.MouseButton.LeftButton:
            self._press_pos = e.globalPosition().toPoint()
            self._moved = False
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):  # noqa: N802
        if (self._press_pos is not None
                and (e.buttons() & Qt.MouseButton.LeftButton)):
            if (e.globalPosition().toPoint() - self._press_pos).manhattanLength() > 6:
                if not self._moved:
                    self._moved = True
                    self.setDown(False)          # 取消按下视觉
                win = self.window()
                if win is not None and hasattr(win, "_drag_origin"):
                    if getattr(win, "_drag_origin", None) is None:
                        win._drag_origin = (e.globalPosition().toPoint()
                                            - win.frameGeometry().topLeft())
                        win._drag_moved = False
                    win.move(e.globalPosition().toPoint() - win._drag_origin)
                    e.accept()
                    return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):  # noqa: N802
        moved = self._moved
        self._press_pos = None
        self._moved = False
        if moved:
            win = self.window()
            if win is not None and hasattr(win, "_drag_origin"):
                win._drag_origin = None
                win._drag_moved = False
            e.accept()                            # 拖动过 → 不算点击
            return
        super().mouseReleaseEvent(e)

    def paintEvent(self, ev):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect()
        bg, bg_hover, border, fg, accent = _STATE_STYLE.get(
            self._state, _STATE_STYLE["idle"])
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(bg_hover if self._hover else bg))
        p.drawRoundedRect(r.adjusted(1, 1, -1, -1), 16, 16)
        p.setPen(QPen(QColor(border), 1.6 if self._state != "idle" else 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(r.adjusted(1, 1, -2, -2), 16, 16)
        cx = r.width() // 2
        side = ICON_SIDE
        iy = max(side // 2 + 2, int(r.height() * 0.45))

        # ---- 图标光环：只有"点过"才画，一眼区分未点击 / 已开启 / 处理中 / 刚完成 ----
        if accent is not None:
            ring = QRect(cx - side // 2 - 6, iy - side // 2 - 6, side + 12, side + 12)
            rc = QColor(accent)
            rc.setAlpha(200)      # 光环压到文字区时透出字缝，不做成硬线条
            if self._state == "busy":
                # 缺口圆弧 ≈ 进行中（静态，不引定时器，避免面板常驻刷新）
                p.setPen(QPen(rc, 2.0))
                p.drawArc(ring, 90 * 16, -270 * 16)
            else:
                p.setPen(QPen(rc, 1.6))
                p.drawEllipse(ring)

        pm = panel_icon(self._icon)
        if pm is not None:
            # 实机截图裁出的整体图标（圆盘 + 字形，天然同心同径）；不随点击状态跳动
            p.drawPixmap(QRect(cx - side // 2, iy - side // 2, side, side), pm)
        else:
            # 兜底：中性浅色圆盘 + 白色实心字形（与实机同语言）
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(255, 255, 255, 18))
            p.drawEllipse(QRect(cx - 27, iy - 27, 54, 54))
            if self._icon == "record":
                _paint_record_icon(p, cx, iy)
            elif self._icon == "summary":
                _paint_summary_icon(p, cx, iy)
            else:
                _paint_subtitle_icon(p, cx, iy)

        # ---- 右上角状态标：实心点=已开启 / 空心圈=处理中 / 勾=刚完成 ----
        if self._state != "idle":
            bx, by = r.width() - 20, 17
            p.setPen(Qt.PenStyle.NoPen)
            if self._state == "active":
                p.setBrush(QColor(accent))
                p.drawEllipse(QRect(bx - 4, by - 4, 8, 8))
            elif self._state == "busy":
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QColor(accent), 2.0))
                p.drawEllipse(QRect(bx - 4, by - 4, 8, 8))
            else:                                   # done → 实心圆 + 深色对勾
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(accent))
                p.drawEllipse(QRect(bx - 9, by - 9, 18, 18))
                p.setPen(QPen(QColor("#16202B"), 2.2))
                p.drawLine(bx - 4, by, bx - 1, by + 3)
                p.drawLine(bx - 1, by + 3, bx + 5, by - 4)

        p.setPen(QColor(fg))
        f = p.font()
        f.setPixelSize(12)
        f.setBold(False)                     # 12px 加粗会糊，靠颜色/描边区分就够了
        p.setFont(f)
        p.drawText(QRect(0, r.height() - 26, r.width(), 24),
                   Qt.AlignmentFlag.AlignCenter, self.text())
        p.end()

    def enterEvent(self, e):  # noqa: N802
        self._hover = True
        self.update()

    def leaveEvent(self, e):  # noqa: N802
        self._hover = False
        self.update()


class _MeterBar(QWidget):
    """录音态电平条（自绘：底 + RMS 渐变宽度 + 峰值刻度）。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rms01 = 0.0
        self._peak01 = 0.0
        self.setFixedHeight(8)

    def set_levels(self, rms01: float, peak01: float) -> None:
        self._rms01 = max(0.0, min(1.0, rms01))
        self._peak01 = max(0.0, min(1.0, peak01))
        self.update()

    def paintEvent(self, ev):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("rgba(255,255,255,0.12)"))
        p.drawRoundedRect(0, 0, w, h, h // 2, h // 2)
        rms_w = int(w * self._rms01)
        if rms_w > 0:
            g = QLinearGradient(0.0, 0.0, float(w), 0.0)   # 线性左→右，避免锥形渐变的班驳
            g.setColorAt(0.0, QColor("#3FBF6B"))
            g.setColorAt(0.65, QColor("#E8C86A"))
            g.setColorAt(1.0, QColor("#E0563F"))
            p.setBrush(QBrush(g))
            p.drawRoundedRect(0, 0, max(rms_w, h), h, h // 2, h // 2)
        pk_x = int(w * self._peak01)
        if pk_x > 0:
            p.setPen(QPen(QColor("#F2F2F2"), 2))
            p.drawLine(min(pk_x, w - 2), 0, min(pk_x, w - 2), h)
        p.end()


# ---------------- 主面板 ----------------

class AiPanel(QWidget):
    """AI 摘记浮层面板。"""

    sig_record = pyqtSignal()       # 开始应用录音
    sig_summary = pyqtSignal()      # 开始 AI 摘要
    sig_subtitle = pyqtSignal()     # 开启字幕
    sig_settings = pyqtSignal()     # 齿轮
    sig_close = pyqtSignal()        # ×
    sig_stop_rec = pyqtSignal()     # 录音态：停止并保存
    sig_pause_rec = pyqtSignal()    # 录音态：暂停/继续

    def __init__(self, cfg=None):
        super().__init__(None)
        c = cfg or {}
        self._w = int(c.get("panel_width", 600) or 600)
        self._drag_origin = None
        self._drag_moved = False
        self._recording = False

        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        # 2026-10-18 改：不再用 WA_TranslucentBackground——Windows 11 上移动分层窗口
        # 会出现"内容不跟随/超出部分被裁切"。改用整窗透明度 + 同色底，移动稳定。
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setObjectName("AiPanel")
        self.setStyleSheet("#AiPanel{background:#10141C;}")
        self.setWindowOpacity(float(c.get("panel_opacity", 0.80) or 0.80))
        self.setWindowTitle("AI 语音摘记")
        self.setFixedSize(PANEL_W, PANEL_H)      # 固定尺寸：内容与图标位置都不再随窗口变化而跳
        self._base_h = PANEL_H                   # 常态高度（显示轻提示时会临时加高）

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._frame = QFrame()
        self._frame.setObjectName("panel")
        self._frame.setStyleSheet(
            "#panel{background:%s;border:1px solid %s;border-radius:20px;}" % (BG, BORDER))
        outer.addWidget(self._frame)

        v = QVBoxLayout(self._frame)
        # 2026-10-18 回退：布局参数恢复为"面板尺寸固定"任务完成时的值（12/16、间距 12）。
        # 提示改用浮层（_place_toast）放在底部空白区正中，不再占用布局空间。
        v.setContentsMargins(16, 12, 16, 16)
        v.setSpacing(12)

        # ---- 标题行 ----
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(_WaveIcon(self._frame))
        title = QLabel("AI 语音摘记")
        title.setStyleSheet(
            "color:%s;font-size:15px;font-weight:600;background:transparent;" % FG)
        title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        head.addWidget(title)
        head.addStretch(1)
        self._btn_settings = _CircleBtn("gear", self._frame)
        self._btn_settings.setToolTip("设置")
        self._btn_settings.clicked.connect(self.sig_settings)
        self._btn_close = _CircleBtn("close", self._frame)
        self._btn_close.setToolTip("关闭（收进托盘，托盘可再唤出）")
        self._btn_close.clicked.connect(self.sig_close)
        head.addWidget(self._btn_settings)
        head.addWidget(self._btn_close)
        v.addLayout(head)

        # ---- 三卡片 ----
        cards = QHBoxLayout()
        cards.setSpacing(12)
        self._card_rec = _CardBtn("record", "开始应用录音")
        self._card_rec.clicked.connect(self.sig_record)
        self._card_sum = _CardBtn("summary", "开始 AI 摘要")
        self._card_sum.clicked.connect(self.sig_summary)
        self._card_sub = _CardBtn("subtitle", "开启字幕")
        self._card_sub.clicked.connect(self.sig_subtitle)
        for b in (self._card_rec, self._card_sum, self._card_sub):
            cards.addWidget(b, 1)
        v.addLayout(cards)
        # 2026-10-18：把多余高度显式吸收到底部——否则嵌套的 head 布局会被 Qt 拉高
        # （实测吃掉 41px，卡片被顶到 y=93、底部空白只剩 19px，这就是"贴按钮"的真因）
        v.addStretch(1)

        # ---- 录音中状态区 ----
        self._rec_box = self._build_rec_box(self._frame)
        v.addWidget(self._rec_box)
        self._rec_box.hide()

        # ---- 轻提示：浮层（不占布局），由 _place_toast 放在底部空白区正中 ----
        # 2026-10-18：窗口尺寸与卡片位置完全回到"面板尺寸固定"任务完成时的状态，
        # 提示又能在空白区垂直居中（不挤压布局）。
        self._toast = QLabel(self._frame)
        self._toast.setStyleSheet(
            "color:%s;font-size:12px;background:transparent;" % FG_DIM)
        self._toast.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._toast.hide()

        # toast 下方的动作按钮行（如"打开目录 / 丢弃"）——有按钮时提示不自动消失
        self._action_box = QWidget(self._frame)
        ar = QHBoxLayout(self._action_box)
        ar.setContentsMargins(0, 0, 0, 0)
        ar.setSpacing(8)
        ar.addStretch(1)
        self._action_layout = ar
        self._action_btns = []
        self._action_box.hide()
        v.addWidget(self._action_box)

        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(self._toast_hide)

    # ---------- 录音态 ----------

    def _build_rec_box(self, parent):
        box = QWidget(parent)
        bv = QVBoxLayout(box)
        bv.setContentsMargins(4, 2, 4, 2)
        bv.setSpacing(12)
        self._lbl_time = QLabel("00:00:00")
        self._lbl_time.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._lbl_time.setStyleSheet(
            "color:%s;font-size:38px;font-weight:600;background:transparent;" % FG)
        self._lbl_time.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        bv.addWidget(self._lbl_time)
        self._meter = _MeterBar(box)
        bv.addWidget(self._meter)
        row = QHBoxLayout()
        row.setSpacing(10)
        self._btn_pause = QPushButton("暂停")
        self._btn_pause.setFixedHeight(38)
        self._btn_pause.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_pause.setStyleSheet(
            "QPushButton{background:rgba(255,255,255,0.12);border:none;border-radius:10px;"
            "color:%s;font-size:14px;padding:0 18px;}"
            "QPushButton:hover{background:rgba(255,255,255,0.2);}" % FG)
        self._btn_pause.clicked.connect(self.sig_pause_rec)
        self._btn_stop = QPushButton("停止并保存")
        self._btn_stop.setFixedHeight(38)
        self._btn_stop.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_stop.setStyleSheet(
            "QPushButton{background:#9E3B2E;border:none;border-radius:10px;"
            "color:#FFFFFF;font-size:14px;padding:0 18px;}"
            "QPushButton:hover{background:#B04A3C;}")
        self._btn_stop.clicked.connect(self.sig_stop_rec)
        row.addStretch(1)
        row.addWidget(self._btn_pause)
        row.addWidget(self._btn_stop)
        row.addStretch(1)
        bv.addLayout(row)
        return box

    def set_recording(self, on: bool, paused: bool = False) -> None:
        self._recording = bool(on)
        for b in (self._card_rec, self._card_sum, self._card_sub):
            b.setVisible(not on)
        self._rec_box.setVisible(on)
        self._btn_pause.setText("继续" if paused else "暂停")
        # 录音态内容更高：面板随之加高（窗口为固定尺寸，adjustSize 无效）
        self._base_h = REC_PANEL_H if on else PANEL_H
        self.setFixedSize(PANEL_W, self._base_h)
        self._keep_on_screen()

    def set_time(self, text: str) -> None:
        self._lbl_time.setText(text)

    def set_levels(self, rms01: float, peak01: float) -> None:
        self._meter.set_levels(rms01, peak01)

    # ---------- 卡片状态（2026-10-20）----------

    def card(self, name: str):
        return {"record": self._card_rec, "summary": self._card_sum,
                "subtitle": self._card_sub}.get(name)

    def set_card_state(self, name: str, state: str) -> None:
        """切换卡片状态：idle（未点）/ active（已开启）/ busy（处理中）/ done（刚完成）。"""
        b = self.card(name)
        if b is not None:
            b.set_state(state)

    def flash_card(self, name: str, state: str = "done", ms: int = 1500) -> None:
        """临时亮一下状态（已保存 / 已生成），到点自动回 idle。"""
        b = self.card(name)
        if b is None:
            return
        b.set_state(state)
        timers = getattr(self, "_flash_timers", None)
        if timers is None:
            timers = self._flash_timers = {}
        old = timers.get(name)
        if old is not None:
            old.stop()
        t = QTimer(self)
        t.setSingleShot(True)

        def _back():
            if b.state() == state:      # 中途被别的状态接管过就别回退
                b.set_state("idle")

        t.timeout.connect(_back)
        timers[name] = t
        t.start(max(200, int(ms)))

    def toast(self, text: str, ms: int = 3000, warn: bool = False) -> None:
        """面板底部轻提示（自动消失，不打断操作）；warn=True 用警示色。

        2026-10-18：面板是固定尺寸，把提示条直接塞进布局会挤压卡片、与图标重叠。
        这里在显示期间把面板临时加高「一条提示」的高度，结束后还原。
        """
        self._clear_actions()
        self._toast.setStyleSheet(
            "color:%s;font-size:12px;background:transparent;"
            % ("#E0563F" if warn else FG_DIM))
        self._toast.setText(text)
        self._toast.adjustSize()
        self._toast.show()
        self._place_toast()
        self._toast_timer.start(ms)

    def toast_actions(self, text: str, actions) -> None:
        """兼容壳：动作按钮已迁到录音窗（2026-10-18 用户要求），这里只显示文字。"""
        self.toast(text, 6000)

    def _mk_action(self, cb):
        def _go():
            self._clear_actions()
            try:
                cb()
            except Exception:  # noqa: BLE001
                pass
        return _go

    def _clear_actions(self) -> None:
        for b in getattr(self, "_action_btns", []):
            b.deleteLater()
        self._action_btns = []
        box = getattr(self, "_action_box", None)
        if box is not None:
            box.hide()

    def _toast_hide(self) -> None:
        self._clear_actions()
        self._toast.hide()

    def _place_toast(self) -> None:
        """把提示放在"卡片以下整个区域（直到窗口底边）"的垂直正中。

        2026-10-19 修正：上一版扣掉了底部 16px 边距，居中点上移了 8px，
        视觉上偏上。现在区域取「卡片底 → 窗口底边」，这才是用户看到的"下方区域"。
        """
        try:
            cards_bottom = self._card_rec.geometry().bottom()
            region_top = cards_bottom + 1
            region_bottom = self._frame.height() - 1      # 区域到窗口底边（含原底部边距）
            h = self._toast.height()
            y = region_top + max(0, (region_bottom - region_top + 1 - h) // 2)
            self._toast.setGeometry(16, y, self._frame.width() - 32, h)
        except Exception:  # noqa: BLE001
            pass

    # ---------- 位置 / 拖动 ----------

    def place_default(self) -> None:
        scr = QApplication.primaryScreen()
        if scr is None:
            return
        av = scr.availableGeometry()
        self.move(av.x() + (av.width() - self.width()) // 2,
                  av.y() + av.height() - self.height() - 80)

    def _keep_on_screen(self) -> None:
        scr = self.screen() or QApplication.primaryScreen()
        if scr is None:
            return
        av = scr.availableGeometry()
        g = self.geometry()
        x = max(av.left(), min(g.x(), av.right() - g.width()))
        y = max(av.top(), min(g.y(), av.bottom() - g.height()))
        if (x, y) != (g.x(), g.y()):
            self.move(x, y)

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
            p = e.globalPosition().toPoint() - self._drag_origin
            if (p - self.pos()).manhattanLength() > 3:
                self._drag_moved = True
            self.move(p)
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):  # noqa: N802
        self._drag_origin = None
        self._drag_moved = False
        super().mouseReleaseEvent(e)
