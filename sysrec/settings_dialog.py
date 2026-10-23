# -*- coding: utf-8 -*-
"""实时转文字 · 设置面板。

参数全部写回 config.json（config.DEFAULTS 里都有键才能落盘）。
保存后由主窗口决定是否重启字幕引擎 —— 引擎是一次性读配置的，重启才生效。
"""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFrame,
                             QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                             QPushButton, QScrollArea, QSlider, QVBoxLayout,
                             QWidget)

from . import config as config_mod


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
     "说完后静音多久把这句话定版。调小 → 出字快，但句子更碎。默认 0.7。"),
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
}

# 这些参数支持"取消限制"：勾选后值写 0，引擎按"只靠停顿断句"处理
DISABLE_SPECS = {
    "subtitle_max_seg_sec": "取消单段上限（只按停顿断句）",
}

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
    if unit in ("ms", "px", "行"):
        return "%d %s" % (round(v), unit)
    return "%.2f %s" % (v, unit)


class SettingsDialog(QDialog):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = dict(cfg)
        self.result_cfg = None
        self._rows = {}                     # key -> (slider, value_lbl, spec, step)

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
        for g in groups:
            bl.addWidget(self._group_card(g, [s for s in SPECS if s[1] == g]))
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

    def _group_card(self, name, specs):
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
        return card

    # ---------- 自动转写 / 自动摘要卡片 ----------
    def _auto_card(self):
        card = QFrame()
        card.setObjectName("card")
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 12, 16, 14)
        v.setSpacing(10)
        h = QLabel("自动转写 / 自动摘要")
        h.setObjectName("grp")
        h.setStyleSheet("color:%s;font-size:13px;" % TEXT)
        v.addWidget(h)
        tip = QLabel("录音一开始就在后台转文字（不弹字幕窗），逐行写进「转写」目录；"
                     "停止录音后自动把文稿交给上面配的模型生成纪要，存进「摘要」目录。"
                     "长会议会自动分段提炼再汇总，不会因为太长而漏掉中段。")
        tip.setObjectName("hint")
        tip.setWordWrap(True)
        v.addWidget(tip)

        for key, label, hint in (
            ("auto_transcribe", "录音时后台自动转文字",
             "只记录文字，不改动录音本身。关掉后录音就只出音频文件。"),
            ("auto_summary", "停止后自动生成摘要",
             "会把本次转写全文发往上面填的 API 地址。不想联网就把这项关掉，"
             "改用面板的「开始 AI 摘要」手动触发。"),
        ):
            cb = QCheckBox(label)
            cb.setChecked(bool(self.cfg.get(key, True)))
            cb.setToolTip(hint)
            v.addWidget(cb)
            self._rows[key] = (cb, None, None, None)

        for key, label, default in (
            ("transcript_dir", "转写目录", config_mod.default_transcript_dir()),
            ("summary_dir", "摘要目录", config_mod.default_summary_dir()),
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
        return card

    # ---------- LLM 摘要卡片 ----------
    def _llm_card(self):
        card = QFrame()
        card.setObjectName("card")
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 12, 16, 14)
        v.setSpacing(10)
        h = QLabel("LLM 摘要")
        h.setObjectName("grp")
        h.setStyleSheet("color:%s;font-size:13px;" % TEXT)
        v.addWidget(h)
        tip = QLabel("配置任意 OpenAI 兼容服务（DeepSeek / 通义 / 智谱 / Kimi / 本地 Ollama），"
                     "字幕窗的「摘要」按钮就会把字幕交给它生成结构化纪要。")
        tip.setObjectName("hint")
        tip.setWordWrap(True)
        v.addWidget(tip)

        rows = (
            ("llm_base_url", "API 地址", "OpenAI 兼容端点，如 https://api.deepseek.com/v1；"
             "本地 Ollama 填 http://localhost:11434/v1", False),
            ("llm_api_key", "API Key", "保存在本机 config.json；也可用环境变量 "
             "SYSREC_LLM_KEY 覆盖", True),
            ("llm_model", "模型名", "如 deepseek-chat / qwen-plus / glm-4-air", False),
        )
        for key, label, hint, secret in rows:
            row = QHBoxLayout()
            row.setSpacing(10)
            lab = QLabel(label)
            lab.setFixedWidth(100)
            row.addWidget(lab)
            edit = QLineEdit(str(self.cfg.get(key, "") or ""))
            edit.setPlaceholderText(hint.split("；")[0])
            edit.setToolTip(hint)
            if secret:
                edit.setEchoMode(QLineEdit.EchoMode.Password)
            row.addWidget(edit, 1)
            v.addLayout(row)
            self._rows[key] = (edit, None, None, None)

        trow = QHBoxLayout()
        tbtn = QPushButton("测试连接")
        tbtn.setToolTip("发一条最小请求验证配置（最多等 20 秒）")
        tbtn.clicked.connect(self._test_llm)
        trow.addWidget(tbtn)
        trow.addStretch(1)
        v.addLayout(trow)
        return card

    def _test_llm(self):
        from .llm import LlmClient, LlmError
        cfg = {k: (self._rows[k][0].text().strip() if k in self._rows else "")
               for k in ("llm_base_url", "llm_api_key", "llm_model")}
        cfg.update({"temperature": self.cfg.get("llm_temperature", 0.3),
                    "max_tokens": self.cfg.get("llm_max_tokens", 2000),
                    "timeout_sec": 20})
        try:
            reply = LlmClient(cfg).test_connection()
            QMessageBox.information(self, "连接成功",
                                    "模型回复：%s" % (reply or "（空回复）"))
        except LlmError as e:
            QMessageBox.warning(self, "连接失败", str(e))
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "连接失败", "%s: %s" % (type(e).__name__, e))

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
            return
        for k, v in patch.items():
            self._set(k, v)

    def _reset(self):
        self._apply_preset("默认平衡")

    def _save(self):
        self.cfg.update(self._collect())
        self.result_cfg = dict(self.cfg)
        self.accept()
