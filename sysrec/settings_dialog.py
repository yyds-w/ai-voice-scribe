# -*- coding: utf-8 -*-
"""实时转文字 · 设置面板。

参数全部写回 config.json（config.DEFAULTS 里都有键才能落盘）。
保存后由主窗口决定是否重启字幕引擎 —— 引擎是一次性读配置的，重启才生效。
"""
from __future__ import annotations

import threading

from PyQt6.QtCore import Qt, QObject, QTimer, pyqtSignal
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFrame,
                             QHBoxLayout, QLabel, QLineEdit, QListWidget,
                             QListWidgetItem, QPlainTextEdit,
                             QPushButton, QScrollArea, QSlider, QVBoxLayout,
                             QWidget)

from . import config as config_mod
from . import llm as llm_mod
from . import msgbox


class _NoWheelSlider(QSlider):
    """滑块：忽略滚轮，避免在可滚动的设置页里悬停误改数值（滚轮交给页面滚动）。"""

    def wheelEvent(self, e):  # noqa: N802
        e.ignore()


class _NoWheelComboBox(QComboBox):
    """下拉框：忽略滚轮，避免悬停时误切选项。"""

    def wheelEvent(self, e):  # noqa: N802
        e.ignore()


# 与主面板（AiPanel）同一套配色
BG = "#10141C"      # 对话框底（主面板外底色）
CARD = "#1B2230"    # 卡片底
BORDER = "#2B3344"  # 描边
TEXT = "#E8ECF2"    # 正文（主面板同款近白偏冷）
MUTED = "#A9B2C0"   # 次文字
INK = "#4FA3E3"     # 强调蓝（主按钮 / 滑块）

# key, 分组, 标签, 最小, 最大, 步长, 单位, 说明
SPECS = [
    ("vad_threshold", "识别敏感度", "VAD 阈值", 0.10, 0.90, 0.01, "",
     "越低越容易把小声当人声，也越容易把空调声、键盘声收进来。默认 0.50；"
     "调到 0.10～0.20 时白噪也可能过闸，满屏乱码就往上调。"),
    ("vad_min_speech", "识别敏感度", "最短人声", 0.10, 1.00, 0.05, "s",
     "VAD 内部判定「这一段是语音」的最短时长。调小 → 一个「嗯」也算话。"),
    ("vad_min_silence", "识别敏感度", "静音容差", 0.05, 0.50, 0.05, "s",
     "VAD 内部判定「这一段结束」的静音时长。调小 → 句子被切得更碎。"),
    ("vad_gate_sec", "识别敏感度", "噪声闸门", 0.10, 1.00, 0.05, "s",
     "整段里少于这么多秒人声就直接丢掉、不送识别。调大 → 更抗噪，但短句可能被吞。"),
    ("seg_min_speech", "识别敏感度", "切段最小人声", 0.20, 1.00, 0.05, "s",
     "一段里至少这么多秒人声才送去识别。调大 → 更抗噪；调小 → 短促词也收。"),
    ("subtitle_max_seg_sec", "字幕节奏", "单段上限", 3.0, 20.0, 0.5, "s",
     "一句话最长说多久就强制收尾（不管有没有停顿）。调大 → 更少被硬切，"
     "但一句要等更久才定版；调小 → 出字更碎。默认 15。"
     "如果连续说话时句子被拦腰切断，就往上加。"),
    ("subtitle_silence_sec", "字幕节奏", "定版静音", 0.30, 2.00, 0.10, "s",
     "说完后静音多久把这句话定版（能量门兜底）。调小 → 出字快，但句子更碎。默认 0.7。"),
    ("subtitle_endpoint_sec", "字幕节奏", "句尾静音", 0.20, 1.00, 0.05, "s",
     "模型判定“这句话说完了”需要的句尾静音（默认 1.0）。2026-10-23 实测：0.4s 太"
     "敏感——句中停顿也被当句尾，每行只剩 2~4 字；1.0s 过滤句中停顿、保留自然句间停顿。"
     "调小 → 句子更碎；调大 → 只认明显的句间停顿。"),
    ("subtitle_ep_min_chars", "字幕节奏", "端点最小字数", 1, 20, 1, "字",
     "端点触发时不够这么多字就不定版（默认 6）——“嗯”“这个”级残句攒进下一句，"
     "挡掉识别初期的不稳定碎片。调大 → 行更少更长；调小 → 更接近原始断句。"),
    ("subtitle_min_line_chars", "字幕节奏", "换行最少字数", 0, 300, 10, "字",
     "**一行至少攒够这么多字才换行**（默认 100，0=不限）。端点/静音/字数兜底在这条"
     "门槛前全部让路，避免字幕碎成一行几个字；停止字幕时不足门槛的尾段也会补上屏。"),
    ("subtitle_max_chars", "字幕节奏", "单段字数", 200, 2000, 100, "字",
     "定版文本达到该字数就强制收尾。防止放音乐 / 噪声时段落无限变长"
     "（字幕一行几千字会卡窗口）。默认 500。"),
    ("subtitle_flush_sec", "字幕节奏", "活跃行刷新", 0.60, 3.00, 0.10, "s",
     "多久刷新一次灰色活跃行。越小越跟手、越费 CPU（默认 1.2，约 11% 单核）。"),
    ("subtitle_first_sec", "字幕节奏", "首字等待", 0.40, 2.00, 0.10, "s",
     "一段刚开口后多久出第一批字。默认 0.8。"),
    ("typewriter_ms", "字幕节奏", "打字机间隔", 10, 80, 1, "ms",
     "每拍多少毫秒吐字。调小 → 吐字更快。默认 30。"),
    ("subtitle_font_size", "字幕外观", "字号", 11, 28, 1, "px",
     "字幕文字大小。默认 15。"),
    ("subtitle_lines", "字幕外观", "常驻行数", 1, 8, 1, "行",
     "不展开时显示几行。默认 3。"),
    ("subtitle_lines_expanded", "字幕外观", "展开行数", 3, 20, 1, "行",
     "点一下字幕窗展开到几行。默认 8。"),
    ("subtitle_width", "字幕外观", "宽度", 320, 900, 10, "px",
     "字幕窗宽度。默认 640（比主面板 560 更宽）。"),
    ("subtitle_opacity", "字幕外观", "不透明度", 0.40, 1.00, 0.02, "",
     "字幕窗整体透明度，1.00 = 完全不透明。默认 0.80（与主面板一致）。"),
]

CHECKS = [
    ("record_by_app", "录音", "只录正在出声的应用（绕过系统音量）",
     "开着：检测到有应用在放声就只录那个应用（进程回环，取音量之前的信号）——\n"
     "系统音量调到 1% 甚至静音也能录清楚。关掉：录整个系统的混音（会跟系统音量走）。"),
    ("record_auto_gain", "录音", "录音自动增益",
     "音量很小时自动放大（最多 32 倍）再存盘，峰值超过 0.7 的部分软限幅到 0.95。\n"
     "开着就不会录出 -40dBFS 那种小声文件（实测可到 -14dBFS，接近视频响度）；\n"
     "代价是很安静的段落底噪也会被一起放大（数字底噪，通常听不出来）。"),
    ("auto_gain", "识别敏感度", "小声自动放大",
     "峰值低于 0.2 时先把音量拉到 0.7 再识别。治「面试官声音小」，代价是很安静的环境里可能把底噪一起放大。"),
    ("subtitle_capture_exclude", "字幕外观", "防截屏",
     "开启后录屏 / 屏幕共享里看不到字幕窗（自己屏幕上照常显示）。"),
]

# 下拉选项（目前只有识别模式）
CHOICES = {
    "asr_mode": (
        "识别模式",
        [
            ("单模型 · 仅流式 + 标点（不跳字，推荐）", "stream"),
            ("双模型 · 流式 + SenseVoice 定版重转（更准，会跳字）", "dual"),
        ],
        "单模型：全程只用流式结果，定版只补标点 —— 文字与草稿完全一致，不会跳；代价是技术词准确率略低。\n"
        "双模型：说完一句再用 SenseVoice 整段重转 —— 更准，但定版文字会和草稿不同，看着像跳了一下。",
    ),
    # 人声增强（2026-10-26 用户要求：温和 / 加强 两档可选）——
    # 与识别模式共用同一套下拉渲染/取值代码（_value/_set 已支持 QComboBox），
    # 值直接就是 RecorderConfig.voice_boost 吃的字符串（off/mild/strong）。
    "record_voice_boost": (
        "人声增强",
        [
            ("关闭（保留原始音色）", "off"),
            ("温和 · 2.8kHz +2.1dB（自然）", "mild"),
            ("加强 · 2.8kHz +3.4dB（更清晰）", "strong"),
        ],
        "线性相位 FIR，1kHz 归一到 0dB —— 只改音色、不改响度（响度交给「录音自动增益」）。\n"
        "两档都会：60Hz 以下去低频轰鸣、200Hz 去浑浊、2.8kHz 抬「人声清楚」那一段、7kHz 以上加空气感。\n"
        "加强档中高频更靠前、听感更清晰；嫌尖（齿音重）就选温和档。",
    ),
}

# 这些参数支持"取消限制"：勾选后值写 0，引擎按"只靠停顿断句"处理
DISABLE_SPECS = {
    "subtitle_max_seg_sec": "取消单段上限（只按停顿断句）",
    "subtitle_max_chars": "不限单段字数（放音乐时段落可能变得很长）",
}


class _TestSig(QObject):
    """测试连接的工作线程 → 对话框主线程（2026-10-23：网络差时不再卡死 UI）。"""
    test_done = pyqtSignal(bool, str)          # (ok, message)

PRESETS = {
    "默认平衡": {},
    "小声优先": {
        "vad_threshold": 0.30, "vad_min_speech": 0.15, "vad_gate_sec": 0.20,
        "seg_min_speech": 0.25, "auto_gain": True,
    },
    "抗噪优先": {
        "vad_threshold": 0.65, "vad_min_speech": 0.35, "vad_gate_sec": 0.50,
        "seg_min_speech": 0.50, "auto_gain": True,
    },
    "极速字幕": {
        "subtitle_flush_sec": 0.80, "subtitle_first_sec": 0.60,
        "subtitle_silence_sec": 0.50, "typewriter_ms": 18,
    },
}


def _fmt(key, v, unit):
    if not unit:
        return "%.2f" % v
    if unit in ("ms", "px", "行", "字", "天"):
        return "%d %s" % (round(v), unit)
    return "%.2f %s" % (v, unit)


class SettingsDialog(QDialog):
    # 「保存模型」即时生效（2026-10-25 方案①）：落盘后通知主窗口同步 LLM 配置
    sig_llm_saved = pyqtSignal(dict)

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = dict(cfg)
        self.result_cfg = None
        self._rows = {}                     # key -> (slider, value_lbl, spec, step)
        self._testing = False               # LLM 测试连接进行中（防重复点击）
        self._test_btn = None               # 「测试连接」按钮（线程化后要禁用/还原）
        self._save_btn = None               # 「保存模型」按钮（落盘后短暂显示"已保存 ✓"）
        self._sig = _TestSig()
        self._sig.test_done.connect(self._on_test_done)

        self.setWindowTitle("实时转文字 · 设置")
        self.setMinimumWidth(600)
        self.resize(620, 780)
        self.setStyleSheet(
            # 2026-10-18 用户要求：窗口底色 #494E5D（rgb 73,78,93），透明度与主窗一致
            "QDialog{background:#494E5D;}"
            "QLabel{color:#E8ECF2;font-size:13px;}"
            "QLabel#hint{color:#C6CCD8;font-size:12px;}"
            "QLabel#grp{color:#C6CCD8;font-size:13px;}"
            "QFrame#card{background:rgba(255,255,255,0.08);"
            "border:1px solid rgba(255,255,255,0.14);border-radius:10px;}"
            "QPushButton{background:rgba(255,255,255,0.12);"
            "border:1px solid rgba(255,255,255,0.16);border-radius:8px;"
            "padding:6px 12px;color:#E8ECF2;font-size:13px;}"
            "QPushButton:hover{background:rgba(255,255,255,0.20);}"
            "QPushButton#primary{background:#4FA3E3;border:none;color:#0E1116;font-size:14px;}"
            "QPushButton#primary:hover{background:#6BB6EE;}"
            "QSlider::groove:horizontal{height:4px;background:rgba(255,255,255,0.18);"
            "border-radius:2px;}"
            "QSlider::sub-page:horizontal{background:#4FA3E3;border-radius:2px;}"
            "QSlider::handle:horizontal{background:#E8ECF2;width:14px;height:14px;"
            "margin:-5px 0;border-radius:7px;}"
            "QCheckBox{color:#E8ECF2;font-size:13px;spacing:8px;}"
            "QComboBox{background:rgba(255,255,255,0.10);"
            "border:1px solid rgba(255,255,255,0.16);border-radius:6px;"
            "padding:6px 8px;color:#E8ECF2;font-size:13px;}"
            "QComboBox QAbstractItemView{background:#565B6B;color:#E8ECF2;"
            "selection-background-color:#6B7080;selection-color:#FFFFFF;outline:0;}"
            "QLineEdit{background:rgba(255,255,255,0.10);"
            "border:1px solid rgba(255,255,255,0.16);border-radius:6px;"
            "padding:5px 8px;color:#E8ECF2;font-size:13px;}"
            "QLineEdit:focus{border-color:#4FA3E3;}"
        )
        # 2026-10-19 用户要求：设置窗口不透明（其余窗口仍跟随 panel_opacity）
        self.setWindowOpacity(1.0)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 14)
        root.setSpacing(12)

        title = QLabel("实时转文字 · 设置")
        title.setStyleSheet("color:%s;font-size:17px;" % TEXT)
        root.addWidget(title)
        tip = QLabel("改完点「保存」。字幕开着时会自动重启一次（约 1 秒黑屏），不用手动关。")
        tip.setObjectName("hint")
        root.addWidget(tip)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea{background:transparent;}")
        body = QWidget()
        body.setStyleSheet("background:transparent;")
        bl = QVBoxLayout(body)
        bl.setContentsMargins(0, 0, 6, 0)
        bl.setSpacing(12)

        bl.addWidget(self._mode_card())
        groups = []
        for spec in SPECS:
            if spec[1] not in groups:
                groups.append(spec[1])
        for _k, g, _l, _h in CHECKS:          # 只有复选框、没有滑块的分组也要出卡片
            if g not in groups:
                groups.append(g)
        combos = {k: c for k, c in CHOICES.items() if k != "asr_mode"}   # 其余下拉归属分组
        for _k, (label, _ch, _tip) in combos.items():
            if label not in groups:
                groups.append(label)
        for g in groups:
            bl.addWidget(self._group_card(
                g, [s for s in SPECS if s[1] == g],
                combos=[(k, v) for k, v in combos.items() if v[0] == g]))
        bl.addWidget(self._auto_card())
        bl.addWidget(self._llm_card())
        bl.addStretch(1)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        prow = QHBoxLayout()
        prow.setSpacing(6)
        lb = QLabel("预设")
        lb.setObjectName("grp")
        prow.addWidget(lb)
        for name in PRESETS:
            b = QPushButton(name)
            b.clicked.connect(lambda _=False, n=name: self._apply_preset(n))
            prow.addWidget(b)
        prow.addStretch(1)
        root.addLayout(prow)

        brow = QHBoxLayout()
        reset = QPushButton("恢复默认")
        reset.clicked.connect(self._reset)
        brow.addWidget(reset)
        brow.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        brow.addWidget(cancel)
        ok = QPushButton("保存")
        ok.setObjectName("primary")
        ok.setMinimumWidth(96)
        ok.clicked.connect(self._save)
        brow.addWidget(ok)
        root.addLayout(brow)

    # ---------- 构建 ----------
    def _mode_card(self):
        """识别模式：双模型 / 单模型（下拉，放在最上面，因为它决定整条链路）"""
        card = QFrame()
        card.setObjectName("card")
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 12, 16, 14)
        v.setSpacing(8)
        h = QLabel("识别模式")
        h.setObjectName("grp")
        h.setStyleSheet("color:%s;font-size:13px;" % TEXT)
        v.addWidget(h)

        _label, choices, tip = CHOICES["asr_mode"]
        combo = _NoWheelComboBox()
        for text, val in choices:
            combo.addItem(text, val)
        cur = str(self.cfg.get("asr_mode", "stream"))
        i = combo.findData(cur)
        combo.setCurrentIndex(i if i >= 0 else 0)
        combo.setToolTip(tip)
        v.addWidget(combo)

        hint = QLabel(tip)
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        v.addWidget(hint)

        self._rows["asr_mode"] = (combo, None, None, None)
        return card

    def _group_card(self, name, specs, combos=()):
        card = QFrame()
        card.setObjectName("card")
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 12, 16, 14)
        v.setSpacing(10)
        h = QLabel(name)
        h.setObjectName("grp")
        h.setStyleSheet("color:%s;font-size:13px;" % TEXT)
        v.addWidget(h)

        for key, _g, label, lo, hi, step, unit, hint in specs:
            row = QHBoxLayout()
            row.setSpacing(10)
            lab = QLabel(label)
            lab.setFixedWidth(100)
            row.addWidget(lab)

            n = int(round((hi - lo) / step))
            sl = _NoWheelSlider(Qt.Orientation.Horizontal)
            sl.setRange(0, n)
            cur = float(self.cfg.get(key, lo))
            sl.setValue(max(0, min(n, int(round((cur - lo) / step)))))
            sl.setToolTip(hint)
            row.addWidget(sl, 1)

            val = QLabel(_fmt(key, lo + sl.value() * step, unit))
            val.setFixedWidth(64)
            val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            val.setStyleSheet("color:%s;font-size:12px;" % MUTED)
            row.addWidget(val)

            sl.valueChanged.connect(
                lambda v, k=key, l=lo, s=step, u=unit, lb=val: lb.setText(_fmt(k, l + v * s, u)))
            v.addLayout(row)
            self._rows[key] = (sl, val, (lo, hi, step), unit)

            if key in DISABLE_SPECS:
                off = QCheckBox(DISABLE_SPECS[key])
                off.setToolTip("勾上后这句话不限长，只按停顿（定版静音）收尾；"
                               "内部仍留 60 秒安全线，防止持续噪声把段拉爆。")
                v.addWidget(off)
                self._rows[key + "__off"] = (off, None, None, None)

                def _sync(checked, s=sl, lb=val, k=key, l=lo, st=step, u=unit):
                    s.setEnabled(not checked)
                    lb.setText("不限" if checked else _fmt(k, l + s.value() * st, u))

                off.toggled.connect(_sync)
                try:
                    is_off = float(self.cfg.get(key, 15.0)) <= 0
                except Exception:                       # noqa: BLE001
                    is_off = False
                off.setChecked(is_off)
                sl.setEnabled(not is_off)
                if is_off:
                    val.setText("不限")

        for key, _g, label, hint in CHECKS:
            if _g != name:
                continue
            cb = QCheckBox(label)
            cb.setChecked(bool(self.cfg.get(key, True)))
            cb.setToolTip(hint)
            v.addWidget(cb)
            self._rows[key] = (cb, None, None, None)

        # 归属本卡片的下拉（如「人声增强：关闭 / 温和 / 加强」）
        for key, (label, choices, tip) in combos:
            if label != name:                # 与卡片标题相同就不重复显示（2026-10-26）
                lb = QLabel(label)
                lb.setStyleSheet("color:%s;font-size:13px;" % TEXT)
                v.addWidget(lb)
            combo = _NoWheelComboBox()
            for text, val in choices:
                combo.addItem(text, val)
            i = combo.findData(str(self.cfg.get(key, "")))
            combo.setCurrentIndex(i if i >= 0 else 0)
            combo.setToolTip(tip)
            v.addWidget(combo)
            hint = QLabel(tip)
            hint.setObjectName("hint")
            hint.setWordWrap(True)
            v.addWidget(hint)
            self._rows[key] = (combo, None, None, None)
        return card

    # ---------- 自动转写 / 自动摘要卡片 ----------
    def _auto_card(self):
        card = QFrame()
        card.setObjectName("card")
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 12, 16, 14)
        v.setSpacing(10)
        h = QLabel("摘要输出 / 缓存")
        h.setObjectName("grp")
        h.setStyleSheet("color:%s;font-size:13px;" % TEXT)
        v.addWidget(h)
        tip = QLabel("摘要自己负责转写：点「开始 AI 摘要」时，若本次没有字幕内容，"
                     "会用本地 SenseVoice 把刚录的音频转成文字（离线、免费），"
                     "转写稿存进「摘要缓存」，再交模型生成纪要存进「摘要」。"
                     "缓存文件按保留天数自动清理。")
        tip.setObjectName("hint")
        tip.setWordWrap(True)
        v.addWidget(tip)

        # 2026-10-24 三功能解耦：两个自动开关已删除（录音只录音、字幕只字幕、
        # 摘要自己负责转写），这里只留"摘要"自己的输出位置与缓存策略。
        for key, label, default in (
            ("summary_dir", "摘要目录", config_mod.default_summary_dir()),
            ("summary_cache_dir", "摘要缓存目录",
             config_mod.default_summary_cache_dir()),
        ):
            row = QHBoxLayout()
            row.setSpacing(10)
            lab = QLabel(label)
            lab.setFixedWidth(100)
            row.addWidget(lab)
            ed = QLineEdit(str(self.cfg.get(key, "") or ""))
            ed.setPlaceholderText("留空 = %s" % default)
            ed.setToolTip("留空就用默认目录：%s" % default)
            row.addWidget(ed, 1)
            v.addLayout(row)
            self._rows[key] = (ed, None, None, None)

        # 摘要缓存保留天数（用户 Q4：默认 7 天，过期自动清理）
        key, label, lo, hi, step, unit = ("summary_cache_keep_days", "缓存保留天数",
                                          3, 90, 1, "天")
        row = QHBoxLayout()
        row.setSpacing(10)
        lab = QLabel(label)
        lab.setFixedWidth(100)
        row.addWidget(lab)
        n = int(round((hi - lo) / step))
        sl = _NoWheelSlider(Qt.Orientation.Horizontal)
        sl.setRange(0, n)
        cur = float(self.cfg.get(key, lo))
        sl.setValue(max(0, min(n, int(round((cur - lo) / step)))))
        sl.setToolTip("摘要缓存（自动转写的文字稿）保留多少天后自动删除。默认 7 天。")
        row.addWidget(sl, 1)
        val = QLabel(_fmt(key, lo + sl.value() * step, unit))
        val.setFixedWidth(64)
        val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        val.setStyleSheet("color:%s;font-size:12px;" % MUTED)
        row.addWidget(val)
        sl.valueChanged.connect(
            lambda v_, k=key, l=lo, s=step, u=unit, lb=val: lb.setText(_fmt(k, l + v_ * s, u)))
        v.addLayout(row)
        self._rows[key] = (sl, val, (lo, hi, step), unit)
        return card

    # ---------- LLM 摘要卡片（2026-10-23 多 LLM 管理） ----------
    def _llm_card(self):
        """多 profile 管理：左侧列表（名称+当前标记）+ 右侧字段编辑。

        数据自管理（self._profiles / self._active / self._cur），不进 _rows——
        _save() 末尾统一写回 cfg（见 _save）。字段与 llm.PROFILE_FIELDS 对齐。
        """
        card = QFrame()
        card.setObjectName("card")
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 12, 16, 14)
        v.setSpacing(10)
        h = QLabel("LLM 摘要")
        h.setObjectName("grp")
        h.setStyleSheet("color:%s;font-size:13px;" % TEXT)
        v.addWidget(h)
        tip = QLabel("可保存多个 LLM（DeepSeek / 通义 / 智谱 / Kimi / 本地 Ollama…），"
                     "摘要时用标记了「当前」的那个。每个模型的地址、Key、参数各自独立。")
        tip.setObjectName("hint")
        tip.setWordWrap(True)
        v.addWidget(tip)

        # ---- 数据 ----
        self._profiles = [llm_mod.normalize_profile(p)
                          for p in (self.cfg.get("llm_profiles") or [])
                          if isinstance(p, dict)]
        self._profiles = [p for p in self._profiles if p["name"]]
        self._active = str(self.cfg.get("llm_active") or "")
        if self._active not in [p["name"] for p in self._profiles]:
            self._active = self._profiles[0]["name"] if self._profiles else ""
        self._cur = 0

        body = QHBoxLayout()
        body.setSpacing(12)

        # 左：profile 列表
        self._prof_list = QListWidget()
        self._prof_list.setMaximumWidth(190)
        self._prof_list.setStyleSheet(
            "QListWidget{background:rgba(255,255,255,0.06);border:none;border-radius:8px;}"
            "QListWidget::item{padding:7px 8px;border-radius:6px;color:%s;}"
            "QListWidget::item:selected{background:rgba(79,163,227,0.35);color:#FFFFFF;}" % TEXT)
        self._prof_list.currentRowChanged.connect(self._on_prof_row)
        body.addWidget(self._prof_list)

        # 右：字段编辑
        form = QVBoxLayout()
        form.setSpacing(8)
        self._prof_edits = {}
        for field, label, secret, multi in (
                ("name", "名称", False, False),
                ("base_url", "API 地址", False, False),
                ("api_key", "API Key", True, False),
                ("model", "模型名", False, False),
                ("temperature", "温度", False, False),
                ("max_tokens", "长度上限", False, False),
                ("timeout_sec", "超时(秒)", False, False),
                ("prompt_template", "Prompt", False, True)):
            row = QHBoxLayout()
            row.setSpacing(10)
            lab = QLabel(label)
            lab.setFixedWidth(76)
            row.addWidget(lab)
            if multi:
                ed = QPlainTextEdit()
                ed.setFixedHeight(64)
                ed.setPlaceholderText("留空 = 用内置的会议纪要模板")
            else:
                ed = QLineEdit()
                ed.setPlaceholderText({
                    "name": "随便起，如 DeepSeek",
                    "base_url": "https://api.deepseek.com/v1；本地 Ollama 填 http://localhost:11434/v1",
                    "api_key": "保存在本机 config.json；也可用环境变量 SYSREC_LLM_KEY 覆盖",
                    "model": "deepseek-chat / qwen-plus / glm-4-air …",
                    "temperature": "0.3（低=稳定，适合摘要）",
                    "max_tokens": "2000",
                    "timeout_sec": "60",
                }[field])
                if secret:
                    ed.setEchoMode(QLineEdit.EchoMode.Password)
            ed.setToolTip(ed.placeholderText())
            row.addWidget(ed, 1)
            if field == "prompt_template":
                # 2026-10-23 Q9：Prompt 三档预设（简洁/标准/详细）——选中即填入
                pc = _NoWheelComboBox()
                pc.addItem("预设 ↓", "")
                for k in llm_mod.PROMPT_PRESETS:
                    pc.addItem(k, k)
                pc.setToolTip("选中一项就把对应模板填进左边 Prompt 框（填完可再手改）")
                pc.currentIndexChanged.connect(
                    lambda _i, c=pc, e=ed: self._apply_prompt_preset(c, e))
                row.addWidget(pc)
            form.addLayout(row)
            self._prof_edits[field] = ed
        body.addLayout(form, 1)
        v.addLayout(body, 1)

        # 按钮行：新建 → 删除 → 保存模型（就近落盘，不用记着去点右下角「保存」）
        # → 设为当前 → 测试连接
        brow = QHBoxLayout()
        brow.setSpacing(6)
        for label, slot in (("新建", self._prof_new),
                            ("删除", self._prof_del),
                            ("保存模型", self._prof_save),
                            ("设为当前", self._prof_set_active),
                            ("测试连接", self._test_llm)):
            b = QPushButton(label)
            b.clicked.connect(slot)
            if label == "测试连接":
                self._test_btn = b
                b.setToolTip("发一条最小请求验证配置（最多等 20 秒）")
            elif label == "保存模型":
                self._save_btn = b
                b.setToolTip("把 LLM 配置立即写进 config.json（只存模型，不影响其他设置）")
            brow.addWidget(b)
        brow.addStretch(1)
        v.addLayout(brow)

        self._refresh_prof_list()
        return card

    # ---------- 多 LLM：列表 / 编辑联动 ----------

    def _refresh_prof_list(self):
        """重建列表并加载当前行。

        ⚠ 全程 blockSignals：setCurrentRow 会触发 currentRowChanged →
        _on_prof_row → _flush_prof_edits，而此刻右侧控件可能还没加载数据
        （构造时 / 新建后），会把空值写回 profile 清掉名字。选中行的切换
        只应由用户真实点击触发（blockSignals 不影响用户交互）。
        """
        self._prof_list.blockSignals(True)
        self._prof_list.clear()
        for p in self._profiles:
            label = p["name"] + ("（当前）" if p["name"] == self._active else "")
            item = QListWidgetItem(label)
            item.setToolTip("%s · %s" % (p["model"], p["base_url"]))
            self._prof_list.addItem(item)
        if self._profiles:
            row = max(0, min(self._cur, len(self._profiles) - 1))
            self._prof_list.setCurrentRow(row)
            self._cur = row
        self._prof_list.blockSignals(False)
        self._load_prof_edits()

    def _on_prof_row(self, row):
        if row < 0 or row >= len(self._profiles):
            return
        self._flush_prof_edits()               # 旧行先落盘
        self._cur = row
        self._load_prof_edits()

    def _load_prof_edits(self):
        """把 self._profiles[self._cur] 填进右侧控件；没有 profile 时清空禁用。"""
        p = self._profiles[self._cur] if 0 <= self._cur < len(self._profiles) else None
        for field, ed in self._prof_edits.items():
            val = "" if p is None else p.get(field, "")
            if isinstance(ed, QPlainTextEdit):
                ed.setPlainText("" if val is None else str(val))
            else:
                ed.setText("" if val is None else str(val))
            ed.setEnabled(p is not None)

    def _flush_prof_edits(self):
        """右侧控件 → self._profiles[self._cur]（名称变了要同步 _active）。"""
        if not (0 <= self._cur < len(self._profiles)):
            return
        p = self._profiles[self._cur]
        old_name = p["name"]
        for field, ed in self._prof_edits.items():
            if isinstance(ed, QPlainTextEdit):
                val = ed.toPlainText().strip()
            else:
                val = ed.text().strip()
            p[field] = val
        # 名称变化：active 跟着走（用户改的就是当前这个的名字）
        if p["name"] != old_name:
            if self._active == old_name:
                self._active = p["name"]
            self._refresh_prof_list_labels()

    def _refresh_prof_list_labels(self):
        """只刷文字（不触发 currentRowChanged，避免打断编辑）。"""
        self._prof_list.blockSignals(True)
        for i, p in enumerate(self._profiles):
            label = p["name"] + ("（当前）" if p["name"] == self._active else "")
            item = self._prof_list.item(i)
            if item is not None:
                item.setText(label)
                item.setToolTip("%s · %s" % (p["model"], p["base_url"]))
        self._prof_list.blockSignals(False)

    def _prof_new(self):
        self._flush_prof_edits()
        self._profiles.append(llm_mod.normalize_profile(
            {"name": "新模型 %d" % (len(self._profiles) + 1)}))
        self._cur = len(self._profiles) - 1
        self._refresh_prof_list()
        self._prof_edits["name"].setFocus()
        self._prof_edits["name"].selectAll()

    def _apply_prompt_preset(self, combo, edit):
        """Prompt 预设下拉：选中即把模板填进编辑框，然后下拉回位。"""
        key = combo.currentData()
        if not key:
            return
        tpl = llm_mod.PROMPT_PRESETS.get(key, "")
        if tpl:
            edit.setPlainText(tpl)
        combo.blockSignals(True)
        combo.setCurrentIndex(0)              # 回提示项，避免同一项重复触发
        combo.blockSignals(False)

    def _prof_del(self):
        if not (0 <= self._cur < len(self._profiles)):
            return
        self._flush_prof_edits()
        p = self._profiles[self._cur]
        if not self._confirm("删除确认",
                             "确定删除「%s」（%s）吗？" % (p["name"], p["model"])):
            return
        self._profiles.pop(self._cur)
        if self._active == p["name"]:                      # 删的是当前 → 顺延到第一个
            self._active = self._profiles[0]["name"] if self._profiles else ""
        self._cur = max(0, min(self._cur, len(self._profiles) - 1))
        self._refresh_prof_list()

    def _prof_save(self):
        """「保存模型」：把 LLM 配置立即落盘（2026-10-23 用户需求）。

        解决的真实痛点：新建/编辑完模型后，如果忘记点右下角的总「保存」就关
        窗口，新模型直接丢。这个按钮就近落盘——只写 LLM 配置（profiles +
        active），其他设置保持原值不动（不会把改到一半的滑块意外写盘）。
        校验：名称非空 + 不重名（重名会让 llm_active 指向谁变得不确定）。
        成功反馈：按钮文字短暂变「已保存 ✓」。
        """
        self._flush_prof_edits()
        if not self._profiles:
            msgbox.warn(self, "无法保存", "先「新建」一个 LLM。")
            return
        names = [p["name"] for p in self._profiles]
        if any(not n for n in names):
            msgbox.warn(self, "无法保存", "有模型名称为空——选中它填个名字再保存。")
            return
        dup = sorted({n for n in names if names.count(n) > 1})
        if dup:
            msgbox.warn(self, "无法保存",
                        "模型名称重复：%s——改名后再保存。" % "、".join(dup))
            return
        self.cfg["llm_profiles"] = self._profiles
        # active 为空 / 指向已不存在的名字（刚删干净重来）→ 自动设为当前选中，
        # 否则落盘 llm_active=""，下次启动虽然降级用第一个，但"当前"标记丢了
        names = [p["name"] for p in self._profiles]
        if self._active not in names:
            self._active = self._profiles[self._cur]["name"] \
                if 0 <= self._cur < len(self._profiles) else names[0]
        self.cfg["llm_active"] = self._active
        config_mod.save(self.cfg)                  # 立即落盘（只影响 LLM 相关键）
        # 2026-10-25 方案①：落盘后立即生效——通知主窗口把 LLM 配置同步进运行内存
        # （此前只写文件，主窗口内存要等全局「保存」或重启才换，用户感知"不生效"）
        self.sig_llm_saved.emit(dict(self.cfg))
        if self._save_btn is not None:
            self._save_btn.setText("已保存 ✓")
            QTimer.singleShot(1500, self._restore_save_btn)

    def _restore_save_btn(self):
        if self._save_btn is not None:
            self._save_btn.setText("保存模型")

    def _confirm(self, title: str, text: str) -> bool:
        """是/否确认（独立方法：单测可 patch 掉模态框）。

        2026-10-23：QMessageBox.question 走原生 Windows 消息框会响系统
        "默认响声"（用户明确不想要）→ 换 msgbox.ask（自绘，无声）。
        """
        return msgbox.ask(self, title, text, danger=True)

    def _prof_set_active(self):
        if not (0 <= self._cur < len(self._profiles)):
            return
        self._flush_prof_edits()
        self._active = self._profiles[self._cur]["name"]
        self._refresh_prof_list_labels()

    def _test_llm(self):
        """对当前选中的 profile 发一条最小请求（最多等 20 秒）。

        2026-10-23：网络差时卡死设置窗口（标题变"未响应"）的根因是
        urlopen 同步跑在 GUI 主线程——DNS 解析不受 timeout 控制、响应
        分块时总时长还会累加。现在请求放工作线程，结果经 _sig.test_done
        回主线程弹框；按钮禁用 + "测试中…" 防重复点击起一串请求。
        """
        from .llm import LlmClient, LlmError
        self._flush_prof_edits()
        if not (0 <= self._cur < len(self._profiles)):
            msgbox.warn(self, "无法测试", "先「新建」一个 LLM 再测。")
            return
        if self._testing:
            return
        p = self._profiles[self._cur]
        cfg = llm_mod.profile_client_cfg(p)
        cfg["timeout_sec"] = 20
        self._testing = True
        if self._test_btn is not None:
            self._test_btn.setEnabled(False)
            self._test_btn.setText("测试中…")

        def work():
            try:
                reply = LlmClient(cfg).test_connection()
                self._sig.test_done.emit(
                    True, "「%s」回复：%s" % (p["name"], reply or "（空回复）"))
            except LlmError as e:
                self._sig.test_done.emit(False, str(e))
            except Exception as e:  # noqa: BLE001
                self._sig.test_done.emit(
                    False, "%s: %s" % (type(e).__name__, e))

        threading.Thread(target=work, daemon=True).start()

    def _on_test_done(self, ok: bool, message: str):
        """工作线程回到主线程：还原按钮 + 静音弹窗显示结果。"""
        self._testing = False
        if self._test_btn is not None:
            self._test_btn.setEnabled(True)
            self._test_btn.setText("测试连接")
        if ok:
            msgbox.info(self, "连接成功", message)
        else:
            msgbox.warn(self, "连接失败", message)

    # ---------- 取值 / 赋值 ----------
    def _value(self, key):
        off = self._rows.get(key + "__off")
        if off is not None and off[0].isChecked():
            return 0.0                              # 0 = 取消该限制
        item = self._rows.get(key)
        if not item:
            return self.cfg.get(key)
        widget, _val, meta, _unit = item
        if isinstance(widget, QCheckBox):
            return widget.isChecked()
        if isinstance(widget, QComboBox):
            return widget.currentData()
        if isinstance(widget, QLineEdit):
            return widget.text().strip()
        lo, hi, step = meta
        return round(lo + widget.value() * step, 4)

    def _set(self, key, v):
        off = self._rows.get(key + "__off")
        if off is not None:
            if float(v) <= 0:
                off[0].setChecked(True)             # 联动会禁用滑块并显示"不限"
                return
            off[0].setChecked(False)
        item = self._rows.get(key)
        if not item:
            return
        widget, val, meta, unit = item
        if isinstance(widget, QCheckBox):
            widget.setChecked(bool(v))
            return
        if isinstance(widget, QComboBox):
            i = widget.findData(v)
            if i >= 0:
                widget.setCurrentIndex(i)
            return
        if isinstance(widget, QLineEdit):
            widget.setText("" if v is None else str(v))
            return
        lo, hi, step = meta
        widget.setValue(max(0, min(widget.maximum(), int(round((float(v) - lo) / step)))))
        val.setText(_fmt(key, float(v), unit))

    def _collect(self):
        out = {}
        for key in list(self._rows):
            if key.endswith("__off"):
                continue
            out[key] = self._value(key)
        return out

    def _apply_preset(self, name):
        patch = PRESETS.get(name) or {}
        if not patch:                       # 默认平衡 = 全部回默认
            for key, _g, _l, lo, _hi, _st, _u, _h in SPECS:
                self._set(key, config_mod.DEFAULTS.get(key, lo))
            self._set("asr_mode", config_mod.DEFAULTS.get("asr_mode", "stream"))
            for key, _g, _l, _h in CHECKS:
                self._set(key, config_mod.DEFAULTS.get(key, True))
            # 下拉类也要回到默认（如人声增强：off/mild/strong）
            for key, (_label, choices, _tip) in CHOICES.items():
                if key == "asr_mode":
                    continue
                self._set(key, config_mod.DEFAULTS.get(key, choices[0][1]))
            return
        for k, v in patch.items():
            self._set(k, v)

    def _reset(self):
        self._apply_preset("默认平衡")

    def _save(self):
        self._flush_prof_edits()                 # LLM 多 profile：右侧编辑先落盘
        self.cfg.update(self._collect())
        self.cfg["llm_profiles"] = self._profiles
        self.cfg["llm_active"] = self._active
        self.result_cfg = dict(self.cfg)
        self.accept()
