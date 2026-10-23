# -*- coding: utf-8 -*-
"""静音提示框：替代 QMessageBox（原生 Windows 消息框会响系统"默认响声"）。

2026-10-23 用户要求：弹窗不要带声音。QMessageBox 的静态方法走原生
Windows MessageBox，必然触发系统提示音；程序内可靠静音只有一条路——
不用原生消息框。本项目本来就全自绘（面板 / toast / 设置窗口），原生
弹窗既是风格异物又带声音，这里统一收口成一个小工具。

用法：
    from . import msgbox
    msgbox.ask(parent, "删除确认", "确定删除「X」吗？")        -> bool
    msgbox.info(parent, "连接成功", "模型回复：正常")           -> None
    msgbox.warn(parent, "连接失败", "timeout")                 -> None
    msgbox.choose(parent, "录音已停止", "要保存吗？",
                  [("保存（可改文件名）…", "ok"),
                   ("不保存（丢弃）", "danger")])              -> label 或 None（关窗）

样式与 settings_dialog 一致（深色卡片 + 蓝色主按钮 + 红色危险按钮）。
"""
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPushButton,
                             QVBoxLayout)

# 与 settings_dialog 同一套配色
BG = "#494E5D"
TEXT = "#E8ECF2"
MUTED = "#C6CCD8"
INK = "#4FA3E3"
DANGER = "#B04A3C"


class QuietBox(QDialog):
    """自绘模态提示框（无系统声）。buttons: [(label, kind)]，kind ∈ ok/danger/plain。"""

    def __init__(self, parent=None, title="", text="", buttons=(("确定", "ok"),),
                 min_width=380):
        super().__init__(parent)
        self.chosen = None                       # 被点按钮的 label（关窗 = None）
        self.setObjectName("quietbox")
        self.setWindowTitle(title)
        self.setMinimumWidth(min_width)
        self.setStyleSheet(
            "QDialog#quietbox{background:%s;}"
            "QLabel{color:%s;font-size:13px;}"
            "QLabel#qb_title{font-size:14px;font-weight:600;}"
            "QLabel#qb_text{color:%s;}"
            "QPushButton{background:rgba(255,255,255,0.12);"
            "border:1px solid rgba(255,255,255,0.16);border-radius:8px;"
            "padding:6px 16px;color:%s;font-size:13px;}"
            "QPushButton:hover{background:rgba(255,255,255,0.22);}"
            "QPushButton#qb_ok{background:%s;border:none;color:#0E1116;font-weight:600;}"
            "QPushButton#qb_ok:hover{background:#6BB6EE;}"
            "QPushButton#qb_danger{background:%s;border:none;color:#FFFFFF;}"
            "QPushButton#qb_danger:hover{background:#C05A4A;}"
            % (BG, TEXT, MUTED, TEXT, INK, DANGER))
        self.setWindowModality(Qt.WindowModality.WindowModal)

        v = QVBoxLayout(self)
        v.setContentsMargins(20, 16, 20, 16)
        v.setSpacing(12)
        t = QLabel(title)
        t.setObjectName("qb_title")
        v.addWidget(t)
        body = QLabel(text)
        body.setObjectName("qb_text")
        body.setWordWrap(True)
        body.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)   # 错误信息可复制
        v.addWidget(body)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        for label, kind in buttons:
            b = QPushButton(label)
            if kind in ("ok", "danger"):
                b.setObjectName("qb_ok" if kind == "ok" else "qb_danger")
                b.setDefault(True)
                b.setAutoDefault(True)
            # lambda 捕获 label：点谁记谁；关窗（×）chosen 保持 None
            b.clicked.connect(lambda _=False, lb=label: self._pick(lb))
            row.addWidget(b)
        v.addLayout(row)

    def _pick(self, label):
        self.chosen = label
        self.accept()


def ask(parent, title: str, text: str, danger: bool = False) -> bool:
    """是/否确认（删除等动作用 danger=True 出红色按钮）。"""
    box = QuietBox(parent, title, text,
                   buttons=[("确定", "danger" if danger else "ok"),
                            ("取消", "plain")])
    return box.exec() == QDialog.DialogCode.Accepted


def info(parent, title: str, text: str) -> None:
    """单按钮告知（成功/结果）。"""
    QuietBox(parent, title, text).exec()


def warn(parent, title: str, text: str) -> None:
    """单按钮警示（失败）。与 info 同形，语义区分给调用方读。"""
    info(parent, title, text)


def choose(parent, title: str, text: str, buttons) -> "str | None":
    """多选一：buttons = [(label, kind), ...]，返回被点 label；关窗返回 None。"""
    box = QuietBox(parent, title, text, buttons=buttons)
    if box.exec() == QDialog.DialogCode.Accepted:
        return box.chosen
    return None
