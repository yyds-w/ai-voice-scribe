"""字幕悬浮窗 —— 置顶、无边框、半透明、可拖动、点一下展开、可锁定穿透、可防截屏。

刻意独立成文件：main.py 已经 3200 行 / 170KB，窗口行为不该再往里塞。
主程序只负责「喂文本」（add_line）和「开关状态」（set_locked），其余全部封装在这里。

交互（对标手机实时字幕框）：
  · 拖动         —— 按住任意位置拖
  · 点一下        —— 在「常驻 N 行」和「展开 M 行」之间切换
  · 锁定（穿透）  —— 鼠标穿透，点不到它；从主界面的锁按钮解锁
  · 防截屏        —— SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)：
                    录屏 / 屏幕共享里看不到这个窗，自己屏幕上照常显示
"""
import ctypes
import html
from collections import deque
from ctypes import wintypes

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (QColor, QFont, QFontMetrics, QTextCharFormat,
                         QTextCursor)
from PyQt6.QtWidgets import (QApplication, QFrame, QPushButton, QTextEdit,
                             QVBoxLayout, QWidget)

# Windows 10 2004+ 才有 WDA_EXCLUDEFROMCAPTURE；老系统调用会失败，静默降级
WDA_NONE = 0x00000000
WDA_EXCLUDEFROMCAPTURE = 0x00000011

BG_RGBA = "rgba(73, 78, 93, 240)"        # #494E5D（用户 23:45 指定，与录音窗同色）
BORDER = "#1AFFFFFF"
FG = "#E8ECF2"                          # 正文（与主面板同款近白偏冷）
FG_PARTIAL = "#A9B2C0"                  # 活跃行（正在说、还会变）
FG_IDLE = "#808A99"                     # 占位文字（更淡）


class SubtitleWindow(QWidget):
    """半透明置顶字幕条。父窗口为 None，独立于主窗口（最小化主窗也保留）。"""

    closed = pyqtSignal()               # 用户点了右上角 ×（主界面据此复位按钮）

    def __init__(self, cfg=None):
        super().__init__(None)
        c = cfg or {}
        self._cfg = c
        self._max_rows = max(1, int(c.get("lines", 3) or 3))
        self._max_rows_exp = max(self._max_rows, int(c.get("lines_expanded", 8) or 8))
        self._w = max(320, int(c.get("width", 640) or 640))
        self._capture_exclude = bool(c.get("capture_exclude", True))
        self._locked = False
        self._expanded = False
        self._drag_origin = None
        self._drag_moved = False
        self._cache = deque(maxlen=200)
        self._placeholder = "等待系统声音…"
        # 活跃行：识别结果是**整段**返回的（SenseVoice 非自回归，吐不出逐字结果），
        # 这里用打字机把它逐字吐出来。否则每 1~2 秒整段替换一次，看着就是一卡一卡的。
        self._partial = ""                       # 当前已显示的活跃行文本
        self._pt_target = ""                     # 识别给的目标文本（可能比已显示的长）
        self._pt_timer = QTimer(self)
        self._pt_timer.setInterval(max(10, min(120, int(c.get("typewriter_ms", 30) or 30))))
        self._pt_timer.timeout.connect(self._type_tick)

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool)              # Tool：不进任务栏、不进 Alt+Tab
        # 2026-10-18 改：同 AiPanel——Windows 11 上分层半透明窗口移动会裁切，
        # 改用整窗透明度 + 同色深底（frame 的 rgba 底叠在上面）。
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setObjectName("SubtitleWin")
        self.setStyleSheet("#SubtitleWin{background:#494E5D;}")
        self.setWindowTitle("实时字幕")

        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        self._frame = QFrame()
        self._frame.setObjectName("cap")
        self._frame.setStyleSheet(
            "#cap{background:%s;border:1px solid %s;border-radius:10px;}"
            % (BG_RGBA, BORDER))
        shell.addWidget(self._frame)

        box = QVBoxLayout(self._frame)
        box.setContentsMargins(14, 8, 14, 8)
        box.setSpacing(0)
        self._text = QTextEdit()
        self._text.setReadOnly(True)
        self._text.setFrameShape(QFrame.Shape.NoFrame)
        self._text.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._text.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # 关键：文本区不吃鼠标事件，点击直接落到窗口上 → 点一下就展开/收起
        self._text.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._text.setStyleSheet("background:transparent;border:none;color:%s;" % FG)
        self._text.document().setDocumentMargin(0)
        font = QFont("Microsoft YaHei UI", int(c.get("font_size", 15) or 15))
        self._text.setFont(font)
        box.addWidget(self._text)

        # 右上角 ×：关闭字幕。锁定（穿透）时隐藏 —— 那时它自己也点不到
        self._close_btn = QPushButton("\u00d7", self)
        self._close_btn.setFixedSize(18, 18)
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._close_btn.setStyleSheet(
            "QPushButton{background:transparent;border:none;color:%s;"
            "font-size:15px;padding:0;}"
            "QPushButton:hover{color:%s;background:rgba(255,255,255,0.10);border-radius:9px;}"
            % (FG_PARTIAL, FG))
        self._close_btn.setToolTip("关闭字幕（主界面的按钮会一起复位）")
        self._close_btn.clicked.connect(self._on_close)

        self._line_h = QFontMetrics(font).lineSpacing() + 2
        # 2026-10-19 用户要求：字幕窗不透明（与设置窗口一致；主面板/录音窗仍 0.80）
        self.setWindowOpacity(1.0)
        self._apply_size()
        self._render()

    # ---------- 文本 ----------
    def add_line(self, text):
        """追加一行定版字幕（已定版的行永不回改 —— 这是"不跳字"的关键）。"""
        t = " ".join((text or "").split())
        self._pt_timer.stop()                    # 定版 → 打字机收工，活跃行让位
        self._pt_target = ""
        self._partial = ""
        if t and not (self._cache and self._cache[-1] == t):
            self._cache.append(t)                # 相邻重复多半是切段边界重喂了同一句
        self._apply_size()
        self._render()

    def set_partial(self, text):
        """活跃行目标文本（灰字）。逐字吐出，**已经显示出来的字永不回退**。

        2026-10-18 改（用户反馈"说完一句又把前面的字刷掉重写，看着累"）：
          · 旧逻辑：新结果与已显示文本在中间分叉时，从差异处截断重打
            → 视觉上"字消失又长出来"；
          · 新逻辑：已显示的活跃文本**只增不减**——模型想改已显示的字时忽略它
            （把已显示部分锁定为前缀，只接续后面的新内容），
            错字统一留到**定版那一次**纠正（定版行是整句重识别结果）。
        """
        t = " ".join((text or "").split())
        if t == self._pt_target:
            return
        if not t:
            self._pt_target = ""
            self._pt_timer.stop()
            self._partial = ""
            self._apply_size()
            self._render()
            return
        keep = 0
        lim = min(len(self._partial), len(t))
        while keep < lim and self._partial[keep] == t[keep]:
            keep += 1
        if len(t) <= len(self._partial):
            t = self._partial                       # 新结果更短 → 保持已显示（不回退）
        elif keep < len(self._partial):
            t = self._partial + t[len(self._partial):]   # 前半分叉 → 锁定已显示，接续新尾巴
        self._pt_target = t
        self._apply_size()
        self._render()
        if not self._pt_timer.isActive():
            self._pt_timer.start()

    def _type_tick(self):
        """打字机：只在文档末尾**增量追加**新字符（不再每 30ms 重建整篇 HTML）。

        2026-10-18 改：旧实现每 tick 都调用 _render() → setHtml 全量重建
        （富文本解析 + 全篇布局），30ms 一次，是"字幕显示卡"的主因。
        现在只把新增的几个字 insertText 追加到最后一行。
        """
        target = self._pt_target
        shown = self._partial
        if len(shown) >= len(target):
            if shown != target:                      # 尾部被识别修正过：对齐收工（罕见，全量一次）
                self._partial = target
                self._render()
            self._pt_timer.stop()
            return
        gap = len(target) - len(shown)
        step = 4 if gap > 60 else 3 if gap > 25 else 2 if gap > 8 else 1
        add = target[len(shown):len(shown) + step]
        self._partial = target[:len(shown) + step]
        cur = QTextCursor(self._text.document())
        cur.movePosition(QTextCursor.MoveOperation.End)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(FG_PARTIAL))
        cur.insertText(add, fmt)
        # 注意：不要把光标 setTextCursor 给 QTextEdit——那会让它持续抢键盘焦点，
        # 真实平台上影响窗口点击/拖动（2026-10-18 用户反馈"无法点击放大"）

    def clear(self, placeholder=None):
        self._pt_timer.stop()
        self._pt_target = ""
        self._cache.clear()
        self._partial = ""
        if placeholder:
            self._placeholder = placeholder
        self._apply_size()
        self._render()

    def lines(self):
        return list(self._cache)

    # ---------- 布局 ----------
    def _rows(self):
        n = len(self._cache) + (1 if self._partial else 0)
        if self._expanded:
            # 展开就撑满展开行数——内容少时也要有明显高度变化，
            # 否则用户点"展开"看到窗口没动，以为点击失效（2026-10-18 反馈）
            return max(self._max_rows_exp, min(n, self._max_rows_exp))
        # 常驻至少 N 行（空着也留出高度，位置不会跳）
        return max(self._max_rows, min(n, self._max_rows))

    def _apply_size(self):
        rows = self._rows()
        h = rows * self._line_h + 16 + 2        # 上下 padding 8×2 + 边框 2
        self.setFixedSize(self._w, h)
        try:
            self._close_btn.move(self._w - 24, 7)
            self._close_btn.raise_()
        except Exception:                       # noqa: BLE001
            pass

    def _render(self):
        """全量渲染：**历史全部入文档** + 活跃行在末尾。

        历史不再截断到"最后 N 行"——超出窗口高度的部分由垂直滚动条查看
        （2026-10-18 用户要求"可上下滚动的进度条"）。
        频率：定版（一句一次）+ partial（~0.6s 一次）；打字机逐字是增量插入，不走这里。
        """
        if not self._cache and not self._partial:
            self._text.setHtml('<span style="color:%s">%s</span>'
                               % (FG_IDLE, html.escape(self._placeholder)))
        else:
            parts = [html.escape(r) for r in self._cache]
            if self._partial:
                parts.append('<span style="color:%s">%s</span>'
                             % (FG_PARTIAL, html.escape(self._partial)))
            self._text.setHtml("<br>".join(parts))
        self._text.moveCursor(QTextCursor.MoveOperation.End)

    def wheelEvent(self, e):
        """滚轮上下翻历史。

        文本区对鼠标透明（点一下=展开/收起），滚轮事件到不了它，
        这里在窗口层手动转给 QTextEdit 的滚动条。
        """
        bar = self._text.verticalScrollBar()
        if bar is None or bar.maximum() <= 0:
            e.ignore()
            return
        bar.setValue(bar.value() - e.angleDelta().y())
        e.accept()

    # ---------- 位置 ----------
    def restore_position(self):
        """恢复上次位置；没有就默认贴屏幕底部居中（避开任务栏）。"""
        self._apply_size()
        try:
            x = int(self._cfg.get("x", -1))
            y = int(self._cfg.get("y", -1))
        except Exception:
            x = y = -1
        if x < 0 or y < 0:
            self.place_default()
            return
        self.move(x, y)
        self._keep_on_screen()

    def place_default(self):
        scr = QApplication.primaryScreen()
        if scr is None:
            return
        av = scr.availableGeometry()
        self.move(av.x() + (av.width() - self.width()) // 2,
                  av.y() + av.height() - self.height() - 96)

    def _keep_on_screen(self):
        scr = self.screen() or QApplication.primaryScreen()
        if scr is None:
            return
        av = scr.availableGeometry()
        g = self.geometry()
        x = max(av.left(), min(g.x(), av.right() - g.width()))
        y = max(av.top(), min(g.y(), av.bottom() - g.height()))
        if (x, y) != (g.x(), g.y()):
            self.move(x, y)

    def toggle_expand(self):
        self._expanded = not self._expanded
        self._apply_size()
        self._render()
        self._keep_on_screen()

    # ---------- 锁定（鼠标穿透） ----------
    def set_locked(self, on):
        """锁定后窗口不接鼠标（WS_EX_TRANSPARENT），要从主界面的锁按钮解锁。"""
        on = bool(on)
        if on == self._locked:
            return
        self._locked = on
        pos = self.pos()
        self.setWindowFlag(Qt.WindowType.WindowTransparentForInput, on)
        self.show()                              # flag 改动会让窗口重建成隐藏态
        self.move(pos)
        self._apply_size()
        self._close_btn.setVisible(not on)       # 穿透时点不到，藏起来免得误导

    def is_locked(self):
        return self._locked

    def _on_close(self):
        """点右上角 ×：隐藏并通知主窗口（主窗口据此复位"关闭字幕"按钮）。"""
        if self._locked:
            return
        self.hide()
        self.closed.emit()

    # ---------- 防截屏 ----------
    def apply_capture_exclude(self, on=None):
        """让本窗口在截屏/录屏/屏幕共享里不可见（自己屏幕上照常显示）。"""
        on = self._capture_exclude if on is None else bool(on)
        self._capture_exclude = on
        try:
            return bool(ctypes.windll.user32.SetWindowDisplayAffinity(
                wintypes.HWND(int(self.winId())),
                WDA_EXCLUDEFROMCAPTURE if on else WDA_NONE))
        except Exception:
            return False                         # 老系统 / 非 Windows：静默降级

    # ---------- 事件 ----------
    def showEvent(self, e):
        super().showEvent(e)
        # winId 要到窗口真正显示后才稳定，挪到下一轮事件循环设最稳
        QTimer.singleShot(0, lambda: self.apply_capture_exclude(self._capture_exclude))
        try:
            self._close_btn.raise_()
        except Exception:                       # noqa: BLE001
            pass

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_origin = (e.globalPosition().toPoint()
                                 - self.frameGeometry().topLeft())
            self._drag_moved = False
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag_origin is not None and (e.buttons() & Qt.MouseButton.LeftButton):
            p = e.globalPosition().toPoint() - self._drag_origin
            if (p - self.pos()).manhattanLength() > 3:
                self._drag_moved = True
            self.move(p)
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._drag_origin is not None and e.button() == Qt.MouseButton.LeftButton:
            moved = self._drag_moved
            self._drag_origin = None
            self._drag_moved = False
            if not moved:
                self.toggle_expand()             # 点一下 = 展开/收起
            e.accept()
            return
        super().mouseReleaseEvent(e)
