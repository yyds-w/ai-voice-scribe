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
from PyQt6.QtGui import (QBrush, QColor, QConicalGradient, QFont,
                         QLinearGradient, QPainter, QPen, QPixmap, QPolygonF)
from PyQt6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel,
                             QPushButton, QVBoxLayout, QWidget)

from . import app_paths

PANEL_W = 560          # 面板尺寸：对标实机 ≈3.3:1 的宽扁浮窗
# 2026-10-25 用户给定的状态表（只长不缩，任一时刻都不会"缩回去再撑开"）：
#   启动空闲 186 → 开始录音 226 → 计时/停止/三选项/选项收起/普通提示 全部 226。
# 226 = 186 + 40（动作按钮行高度），在"开始录音"那一下一次性长高，之后永不回缩。
PANEL_H = 186          # 启动空闲高度（未录过音时紧凑显示）
PANEL_H_REC = 226      # 开始录音后的固定高度（含动作按钮行）——只增不减
ICON_SIDE = 72         # 卡片图标显示边长（图标为"圆盘+字形"整体，源图 324px）

ACTION_GAP_BOTTOM = 6    # 动作按钮行距窗口底边的缝（用户 2026-10-23 定：6px）
FG_TOAST = "#C9D1DC"     # "已保存"提示行文字色：比 FG_DIM 亮一档（用户定）


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

# ---- 卡片点击后的三种状态（2026-10-20 新增 / 2026-10-24 统一蓝色）----
# 语义：active=已开启（持续态）/ busy=处理中（临时态）/ done=刚完成（1.5s 反馈后回落）
# 配色沿用面板既有的"深色底 + 白色图形"语言，只换强调色，不换布局与尺寸。
# 2026-10-24 用户要求：录音/字幕"启动后"的配色与摘要的蓝色调一致 →
# active 从暖金改为与 busy 同一套青蓝（形状已能区分：实心点 + 整圈光环 = 持续，
# 缺口弧 + 空心圈 = 处理中）。
CARD_ON = "#2678B6FF"        # 已激活：青蓝 15% 底（与 busy 同色系）
CARD_ON_HOVER = "#3878B6FF"  # 已激活 + 悬停：22%
BORDER_ON = "#C478B6FF"      # 已激活：青蓝描边 77%
FG_ON = "#E6F1FF"            # 已激活：冷白（压在蓝底上清晰）
ACCENT_ON = "#7FC0FF"        # 已激活：蓝色指示点 / 图标光环

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
    """摘要兜底：白色实心文档（含灰线）+ 白色星芒。

    2026-10-25 用户指出：原来这里还套了一圈彩虹渐变（_paint_rainbow_ring），
    但 assets 里没有 panel_summary.png，那圈其实是代码画的 —— 与"三个卡片
    都不要外圈"的要求冲突，已去掉（彩虹环函数保留但不再调用，备查）。
    """
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

        # ---- 不再画图标光环（2026-10-25 用户两次反馈：套圈压字、不好看）----
        # active/busy/done 全部只靠 _STATE_STYLE 的配色 + 右上角标记区分：
        # 实心点=已开启 / 空心圈=处理中 / 勾=刚完成。下方文字区完全空出来。
        # （历史：这里曾按状态画整圈光环或 270° 缺口弧，与卡片文字重叠。）

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
    sig_files_dropped = pyqtSignal(list)   # 拖文件进面板 → 手动摘要（2026-10-25）

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
        # 2026-10-25：显式指定中文字体。原来不指定，中文全靠系统逐字回退
        # （应用默认 Segoe UI 没有汉字）——实测同一行里会出现"有的字更粗更高"
        # 的观感（不同回退字体混排）。雅黑 UI 是 Windows 中文界面的标准字体，
        # 子控件继承这个 family；字号仍由各处 stylesheet 控制。
        self.setFont(QFont("Microsoft YaHei UI", 9))
        self.setWindowOpacity(float(c.get("panel_opacity", 0.80) or 0.80))
        self.setWindowTitle("AI 语音摘记")
        self.setAcceptDrops(True)                # 拖音频/文本进来 = 手动摘要
        self.setFixedSize(PANEL_W, PANEL_H)      # 固定尺寸：从启动到退出都不再改（2026-10-25）

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

        # toast 下方的动作按钮行（如"打开目录 / 丢弃"）——有按钮时提示不自动消失。
        # 2026-10-23 第 2 轮：改为 _frame 上的**绝对定位浮层**（不占布局）。
        # 原因：占布局时 Qt 实际留底 22px 且构成不可控（隐藏 rec_box 的 spacing
        # 等混在其中），"贴底缝"调不干净；绝对定位由 _place_actions 钉在底部。
        self._action_box = QWidget(self._frame)
        ar = QHBoxLayout(self._action_box)
        ar.setContentsMargins(0, 0, 0, 0)
        ar.setSpacing(8)
        ar.addStretch(1)
        self._action_layout = ar
        self._action_btns = []
        self._persistent_actions = None   # 常驻按钮组（2026-10-26）：toast 提示
                                          # 收起按钮后自动恢复——"开启AI摘要"后那组
                                          # 按钮要一直显示，不该被流程提示冲掉
        self._rec_mode = None          # 录音控制条模式：recording / paused / None
        self._action_box.hide()

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
        # 2026-10-25 用户状态表：开始录音时一次性长到 226，停止后**不缩回**
        # （原实现 stop 时 setFixedSize(186) → 紧接着动作按钮又撑到 226，一缩一涨）。
        if on:
            self._ensure_height(PANEL_H_REC)
        for b in (self._card_rec, self._card_sum, self._card_sub):
            b.setVisible(not on)
        self._rec_box.setVisible(on)
        self._btn_pause.setText("继续" if paused else "暂停")
        self._keep_on_screen()

    def set_time(self, text: str) -> None:
        self._lbl_time.setText(text)

    def set_levels(self, rms01: float, peak01: float) -> None:
        self._meter.set_levels(rms01, peak01)

    # ---------- 录音卡片：同一个按钮切换状态（2026-10-23 Q6=A）----------

    # 2026-10-24 用户要求：按钮文字前的状态符号（● / ■）一律去掉，只留纯文字
    REC_IDLE_TEXT = "开始应用录音"

    def set_record_time(self, hms: str = "") -> None:
        """录音中：卡片文案固定「正在录音」（2026-10-25 用户要求）。

        计时只在下方控制条显示——原来卡片上也带「录音中 HH:MM:SS」，
        与控制条那行重复（用户截图反馈）。
        """
        self._card_rec.setText("正在录音")

    def set_rec_controls(self, mode: str, text: str) -> None:
        """底部录音控制条（2026-10-25 用户需求）。

        mode:
          "recording" → 「录音中 HH:MM:SS」+ [暂停][停止]
          "paused"    → 「已暂停 HH:MM:SS」+ [继续][停止]
          "none"      → 收起（回到普通提示区）
        计时刷新走 update_rec_text()（只改文字，不重建按钮，避免每秒闪）。
        """
        if mode == "none":
            self._rec_mode = None
            # 2026-10-25 用户要求：停止瞬间不缩回——否则「缩回→三选项再加高」会弹一下；
            # 保持高度，等三选项被点掉/下一条普通提示时再自然还原。
            self._clear_actions(keep_height=True)
            self._toast.hide()
            return
        self._rec_mode = mode
        label = "暂停" if mode == "recording" else "继续"
        self.toast_actions(text, [(label, self.sig_pause_rec.emit),
                                  ("停止", self.sig_stop_rec.emit)])
        self._rec_mode = mode          # toast_actions 开头会清模式，这里再设回

    def update_rec_text(self, text: str) -> None:
        """录音计时刷新：只改提示文字，不动按钮（避免重建闪烁）。"""
        if not getattr(self, "_rec_mode", None):
            return
        self._toast.setText(text)
        self._toast.adjustSize()
        self._place_toast()

    def set_record_idle(self) -> None:
        """空闲：卡片文案回到「开始应用录音」。"""
        self._card_rec.setText(self.REC_IDLE_TEXT)

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
        # 【常驻按钮组】本方法会 _clear_actions() 清掉按钮行；若当前存在
        # "开启AI摘要"那组常驻按钮，清完立即重建——流程提示只顶替文字，
        # 不吞按钮（2026-10-26 修"再次点摘要按钮就消失"）。
        """面板底部轻提示（自动消失，不打断操作）；warn=True 用警示色。

        2026-10-25：录音控制条（暂停/停止）显示期间，普通提示不覆盖它。
        """
        if getattr(self, "_rec_mode", None):
            return
        _persist = bool(getattr(self, "_persistent_actions", None))
        self._clear_actions(keep_height=_persist)
        if _persist:
            # 【常驻按钮组】流程提示不清掉"开启AI摘要"那组按钮：清完立即重建
            # （2026-10-26 修"再次点摘要按钮就消失"）。提示条照常短暂显示。
            self._apply_actions(self._persistent_actions)
        self._toast.setStyleSheet(
            "color:%s;font-size:13px;background:transparent;"
            % ("#E0563F" if warn else FG_DIM))
        self._toast.setText(text)
        self._toast.adjustSize()
        self._toast.show()
        self._place_toast()
        self._toast_timer.start(ms)

    def toast_actions(self, text: str, actions, warn: bool = False) -> None:
        """底部轻提示 + 动作按钮（2026-10-23：独立录音窗移除后，"已保存"态
        的 打开目录 / 改名… / 丢弃 搬到这里——全程不弹窗，Q6=A）。

        actions: [(label, callback), ...]。有按钮时提示不自动消失，
        点任一按钮 / 下一条提示到来时收起。warn=True 用警示色（2026-10-25：
        摘要没素材 / 转写失败时也走这套按钮）。

        2026-10-23 修复：按钮行占布局，186px 的固定高度放不下（曾与「开启
        字幕」卡片重叠 21px，见 _grow_for_actions 注释）。进入时临时加高。
        """
        self._rec_mode = None          # 普通动作调用：退出录音控制条模式
        # 2026-10-25 用户实测"停止瞬间仍会弹"：本方法随后必然 _grow_for_actions 加高，
        # 先缩回再加高就会闪一下——这里保持高度，让高度只增不减。
        self._clear_actions(keep_height=True)
        self._grow_for_actions()
        self._toast.setStyleSheet(
            "color:%s;font-size:13px;background:transparent;"
            % ("#E0563F" if warn else FG_TOAST))
        self._toast.setText(text)
        self._toast.adjustSize()
        self._toast.show()
        self._persistent_actions = list(actions or [])   # 本组即"常驻组"
        self._apply_actions(self._persistent_actions)
        if self._action_btns:
            self._place_actions()              # 贴底定位（绝对定位浮层，第 2 轮）
            self._action_box.show()
            self._toast_timer.stop()          # 有按钮：不自动消失
        self._place_toast()

    def _mk_action(self, cb, keep: bool = True):
        """动作按钮回调。keep=True：点完**保留**按钮行（2026-10-25 用户要求：
        打开摘要后往往还要打开文件夹，点一下就全收起太急）；
        keep=False：破坏性操作（如"丢弃"）才就地收起。"""
        def _go():
            if not keep:
                self._clear_actions()
            try:
                cb()
            except Exception:  # noqa: BLE001
                pass
        return _go

    def _apply_actions(self, actions) -> None:
        """把 [(label, cb[, keep]) ...] 建成底部按钮行（toast_actions 与
        常驻组恢复共用；2026-10-26 从 toast_actions 里抽出）。

        必须自带 `_grow_for_actions()`：重建按钮时不加高，按钮会挤在 186 的
        矮窗里压住卡片（2026-10-26 用户截图实测：窗口缩小 + 红字压按钮）。
        """
        if actions:
            self._grow_for_actions()
        for item in (actions or []):
            label, cb = item[0], item[1]
            keep = bool(item[2]) if len(item) > 2 else True    # 默认点完保留
            b = QPushButton(label, self._frame)
            b.setFixedHeight(26)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(
                "QPushButton{background:rgba(255,255,255,0.16);border:none;"
                "border-radius:8px;color:%s;font-size:12px;padding:0 12px;}"
                "QPushButton:hover{background:rgba(255,255,255,0.28);}" % FG)
            b.clicked.connect(self._mk_action(cb, keep))
            self._action_layout.addWidget(b)
            self._action_btns.append(b)
        if self._action_btns:
            self._place_actions()              # 贴底定位（绝对定位浮层，第 2 轮）
            self._action_box.show()
            self._toast_timer.stop()          # 有按钮：不自动消失

    def _clear_actions(self, keep_height: bool = False) -> None:
        for b in getattr(self, "_action_btns", []):
            b.deleteLater()
        self._action_btns = []
        box = getattr(self, "_action_box", None)
        if box is not None:
            box.hide()
        toast = getattr(self, "_toast", None)
        if toast is not None:
            toast.hide()               # 按钮收起来，提示条一起收（toast() 会重新显示）
        if not keep_height:
            self._shrink_after_actions()

    def _ensure_height(self, target_h: int) -> None:
        """只增不减地调整窗口高度（2026-10-25 用户状态表）。

        顶边不动、向下长；已经是目标高度或更高就什么都不做——
        这是"停止录音不缩回"的唯一保证点：全程序没有任何把高度调小的路径。
        """
        if self.height() >= target_h:
            return
        pos = self.pos()
        self.setFixedSize(PANEL_W, target_h)
        self.move(pos)                    # 顶边锚定（个别平台上 resize 会挪原点）
        try:
            if self._toast.isVisible():
                self._place_toast()       # 高度变了，提示重新居中
        except Exception:                 # noqa: BLE001
            pass

    def _grow_for_actions(self) -> None:
        """动作按钮行需要 226 高度：只增不减地保证（原有调用点保留）。"""
        self._ensure_height(PANEL_H_REC)

    def _shrink_after_actions(self) -> None:
        """动作按钮收起后缩回启动高度 186（2026-10-25 用户状态表）。

        只有"收起选项"这一个刻意动作会变矮（点任一选项 / 下一条普通提示到来）；
        录音停止那一瞬间**不**走这里（`set_recording(False)` 不改尺寸），
        所以不会再出现"停止先缩回、选项又撑开"的闪动。
        """
        if self.height() <= PANEL_H:
            return
        pos = self.pos()
        self.setFixedSize(PANEL_W, PANEL_H)
        self.move(pos)                    # 顶边锚定，底边上收
        try:
            if self._toast.isVisible():
                self._place_toast()       # 高度变了，提示重新居中
        except Exception:                 # noqa: BLE001
            pass

    def _place_actions(self) -> None:
        """把动作按钮行钉在窗口底部：距底边 ACTION_GAP_BOTTOM（6px），高 26。

        与 _place_toast 配套使用——toast 在「卡片底 ↔ 按钮顶」之间垂直居中。
        按钮行是绝对定位浮层（不占布局，见 __init__ 注释），因此这里显式定们。
        """
        box = getattr(self, "_action_box", None)
        if box is None:
            return
        btn_h = 26                       # 与 toast_actions() 里 setFixedHeight(26) 一致
        y = self._frame.height() - ACTION_GAP_BOTTOM - btn_h
        box.setGeometry(0, y, self._frame.width(), btn_h)
        box.raise_()                     # 浮层：保证压在卡片/提示之上

    def _toast_hide(self) -> None:
        """提示条到点收起。**常驻按钮组必须留下**（2026-10-26 用户实测：
        点完按钮 3 秒后整组消失、窗口跟着缩回）。"""
        _persist = bool(getattr(self, "_persistent_actions", None))
        self._clear_actions(keep_height=_persist)
        self._toast.hide()
        if _persist:
            self._apply_actions(self._persistent_actions)

    def _place_toast(self) -> None:
        """把提示放在"卡片以下整个区域（直到窗口底边）"的垂直正中。

        2026-10-19 修正：上一版扣掉了底部 16px 边距，居中点上移了 8px，
        视觉上偏上。现在区域取「卡片底 → 窗口底边」，这才是用户看到的"下方区域"。
        2026-10-23 第 2 轮：有动作按钮时，提示行在「卡片底 ↔ 按钮顶」之间
        **垂直居中**（用户明确要求）；按钮行由 _place_actions 贴底定位。
        """
        try:
            cards_bottom = self._card_rec.geometry().bottom()
            box = getattr(self, "_action_box", None)
            h = self._toast.height()
            if box is not None and box.isVisible():
                btn_top = box.geometry().top()
                # 四舍五入（而非 //2 向下取整）：奇数间隙时上下距差 1px，
                # 用户要求"垂直居中"，差 1px 在 UI 细节上也看得出来
                y = cards_bottom + int((btn_top - cards_bottom - h) / 2 + 0.5)
            else:
                region_bottom = self._frame.height() - 1   # 区域到窗口底边（含原底部边距）
                y = cards_bottom + 1 + max(
                    0, (region_bottom - cards_bottom - h) // 2)
            self._toast.setGeometry(16, y, self._frame.width() - 32, h)
        except Exception:  # noqa: BLE001
            pass

    # ---------- 拖放：把音频/文本拖进面板 = 手动摘要（2026-10-25） ----------

    def dragEnterEvent(self, e):  # noqa: N802
        md = e.mimeData()
        if md is not None and md.hasUrls() and any(
                u.isLocalFile() for u in md.urls()):
            e.acceptProposedAction()
            try:
                self.toast("松开即可摘要这个文件（音频→先转写，文本→直接摘要）", 2500)
            except Exception:                     # noqa: BLE001
                pass
        else:
            e.ignore()

    def dragMoveEvent(self, e):  # noqa: N802
        if e.mimeData() is not None and e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):  # noqa: N802
        md = e.mimeData()
        paths = [u.toLocalFile() for u in md.urls()] if md is not None else []
        paths = [p for p in paths if p and os.path.isfile(p)]
        if not paths:
            e.ignore()
            return
        e.acceptProposedAction()
        self.sig_files_dropped.emit(paths)

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
