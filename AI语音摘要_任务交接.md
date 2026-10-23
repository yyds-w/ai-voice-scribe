# AI语音摘要 · 任务交接（接手先读本文件）
> 生成于 2026-10-18，基于当时磁盘状态。

## 一、这是什么
Windows 系统声音录音工具（PyQt6 + WASAPI 环回 + lameenc）：录音（mp3/wav、断流补零、设备热切换、120min 分卷、暂停/继续、停止后保存/丢弃）+ 实时字幕（流式 Paraformer 草稿 0.59s/版 + SenseVoice 定版，双模型、可回退）+ LLM 摘要（OpenAI 兼容 /chat/completions）。用户入口：桌面「AI语音摘要.lnk」（pythonw + run.py，单实例）。

## 二、文件
**交付物（改这里）**：`D:\Program Files\AI语音摘要\`
- `sysrec\gui.py`（40.8KB，10-18 13:33）主窗口+托盘+摘要链路
- `sysrec\subtitle.py`（11.4KB，11:49）字幕引擎：流式草稿 / 能量 VAD 断句 / SenseVoice 定版 / 流式故障自动回退
- `sysrec\subtitle_window.py`（17.1KB，13:31）悬浮窗：增量打字机 / 滚动条 / 点击展开 / 摘要按钮 / 防截屏
- `sysrec\stream_asr.py`（2.8KB，11:48）流式 Paraformer 封装（OnlineRecognizer）
- `sysrec\asr.py`（8.7KB，11:33）SenseVoice 封装（transcribe(skip_vad=)）
- `sysrec\llm.py`（4.9KB，13:24）LLM 客户端（urllib；401/429/5xx/超时/断网分类）
- `sysrec\config.py`（3.9KB）+ `config.json`（956B，13:45，llm_* 8 键）
- `sysrec\settings_dialog.py`（14.2KB，13:27）设置面板（VAD/节奏/外观 + LLM 卡片+测试连接）
- `run.py`（6.8KB）启动器：单实例 IPC / 日志 / 图标 / excepthook
- `models\streaming-paraformer\`（encoder.int8.onnx 158MB + decoder.int8.onnx 71MB + tokens.txt）
- `models\sense-voice-small\`（model.int8.onnx 239MB + tokens.txt）

**权威源（勿改）**：`D:\Program Files\AI面试软件\`（stt.py 参数参考；`fix_terms` 术语表动态导入源——**只读**）。

**备份**：`D:\ProgramFilesTemps\WorkBudddy\备份\AI语音摘要-20261018-加字幕前\`（改前 gui/recorder）、`D:\ProgramFilesTemps\WorkBudddy\备份\AI语音摘要-20261018-流式版\`（asr/subtitle/stream_asr/subtitle_window）。

**脚本/测试**：`D:\Program Files\AI语音摘要\dev\`：`_t_llm_client.py`（12 项）、`_t_llm_gui.py`（8 项）、`_t_subtitle_engine.py`、`_t_subtitle_gui.py`、`_t_sub_scroll.py`（6 项）、`_smoke_gui.py`、`_t_traymenu_full.py`、`_t_pause_save.py`（9 项）、`_mk_test_audio.py`（SAPI 合成中文测试音频，不发声）；产物收纳于 `dev\_test_output\`。

## 三、已拍板口径（勿改）
1. 【已拍板】双模型冲突：定版时 **SenseVoice 无条件覆盖流式草稿**（两模型均无置信度、LLM 仲裁太慢；用户 12:47 拍板）。
2. 【已拍板】LLM 摘要走 OpenAI 兼容 `/chat/completions`（默认 DeepSeek；Ollama/通义/智谱/Kimi 同构）；Key 存 config.json 明文，环境变量 `SYSREC_LLM_KEY` 优先覆盖。
3. 【已拍板】摘要发送前弹隐私确认（告知文本发往哪个 URL），`llm_confirm_shown` 只问一次。
4. 【已拍板】改 LLM 配置**不重启字幕**（`_open_settings` 仅对非 llm_ 键变化重建窗口/引擎）。
5. 【已拍板】字幕纠错方案（序号对齐 + 不过检绝不写回）**已设计未施工**；用户 13:50 拍板"先不做，只要步骤预设+主流对标"。
6. 【已拍板】冲突优先级：**磁盘最新版 > 会话记忆 > 旧备份**。
7. 【已拍板】断句参数由用户在设置面板自调（推荐定版静音 1.2s / 单段上限 15s；config 现值 0.7 / 8.0——**用户尚未改，接手时先问**）。
8. 【口径澄清 · 2026-10-21 用户纠错】`segment_minutes`（config 现值 120）是**录音分卷**（Recorder 按帧切文件、`_pXX` 后缀），**不是转写分段参数**；转写/摘要分段是 **`summary_chunk_chars`**（按字符数 map-reduce）与转写稿的"每 5 分钟锚点"。三者互不相干，**任何转写链路都不得读 `segment_minutes`**。

## 四、风格/约束
- 交付纪律：交付前跑 `delivery-no-pseudoblock` 闸门（静默自检、不贴快照）；附文件简报；改前备份到 `D:\ProgramFilesTemps\WorkBudddy\备份\`（**禁放 C 盘用户目录**）。
- 测试纪律：全自动（SAPI 合成音频 / QTest 合成事件 / 本地 mock 服务器）；**离屏测试全绿 ≠ 真实平台无 bug**（焦点/命中测试差异需专门覆盖）。
- UI 配色：米白 #FAF7F0 + 炭黑 #2E2B26（2026-10-18 方案 A，另一会话落地，勿回退）。
- 输出风格：先结论、量化数据、表格、🟢🔴⚪🟡。

## 五、当前状态
- **已完成**（全部经测试并重启生效）：录音全链路（mp3/wav、断流补零、设备热切换、分卷、暂停/继续、停止后"保存（可改名）/丢弃（回收站）"）；单实例（命名管道，重复双击唤醒已有窗）；托盘菜单（手动 popup + AttachThreadInput 抢前台）；深色主题下拉修复；实时字幕（流式草稿间隔中位数 0.59s、前缀零回改、定版 SenseVoice 兜底、流式故障自动回退旧路径）；字幕窗（滚轮翻历史、点击展开 93→218px、摘要按钮、防截屏）；LLM 摘要 P0（客户端 12/12 + GUI 8/8 + 三重回归全绿，13:36 重启）。
- **进行中**：无。
- **待用户决策**：
  1. 字幕纠错 P0（S0–S8 约 800 行；S0+S1 做完即有"导出 SRT"可用）——是否开工 / 从哪阶段起；
  2. LLM 摘要 P1：Prompt 模板面板内编辑（现改 `llm.py` 的 DEFAULT_PROMPT）/ 流式输出 / token 用量展示
     （~~长文本 map-reduce 分段~~ → **2026-10-20 已完成**，见 §八）；
  3. 专名纠错（热词表 + LLM）是否纳入 P1。

## 六、编辑纪律（铁律）
1. 基于磁盘最新版就地改，禁止跑脚本整篇重生成。
2. 批量改前先备份到 `D:\ProgramFilesTemps\WorkBudddy\备份\<任务>-<日期>-<主题>\`。
3. `sysrec/` 改动后跑 §二 脚本栏相关测试，**全绿才交付**。
4. 面试软件目录只读；删文件先授权；**Qt 回调内异常必须 try/except**；诊断脚本 print 必须 `flush=True`。
5. 重启生效：PowerShell 精确杀 CommandLine 含"AI语音摘要"的 pythonw.exe → `cmd //c start "" "AI语音摘要.lnk"`。

## 七、坑
完整踩坑记录（12 技术坑 + 口径坑 + 写法坑）在 `.workbuddy/memory/2026-10-18.md`（128 行）。接手前必读的 4 条：
1. **worker 线程里 `QTimer.singleShot` 不触发**（该线程无事件循环）→ 跨线程回主线程必须 pyqtSignal emit（13:36 摘要回调/失败提示就这么瘸过，被测试抓出）。
2. **PyQt6 回调内未捕获异常 = 静默杀进程（exit 127、零输出）**；`_guard_qt_callback` 会把它吞成"没反应"——遇到"功能没反应"先直接构造/直调复现，别信状态栏没报错。
3. **本执行环境过滤注入鼠标点击**（SendInput 返回 1 但应用收不到，普通按钮也点不动）——菜单/点击链一律用 QTest 合成事件测。
4. 枚举/API 笔误先查再写：`Qt.CursorShape.PointingHand` 不存在（PyQt6 为 `PointingHandCursor`）；`QTest.mouseWheel` 不存在（用合成 `QWheelEvent` + `QApplication.sendEvent`）。

## 八、2026-10-20 变更：方案 B（自动转写 → 停止即摘要 → 长会议分段）

### 8.1 新增文件
- `sysrec/transcript.py`（新，~6KB）转写稿：**边转边落盘**，内存全文不封顶。
  - 关键：字幕窗的 `deque(maxlen=200)` 只是**显示缓存**，满了静默丢最早的行；摘要要全文，必须另立容器。
  - 文件格式：`# 转写稿 <时间>` + `设备：…` + 每 **5 分钟**一个 `## [mm:ss]` 锚点。锚点而不是逐行时间戳 = 省 8 倍 token。
  - API：`open/add/close/text/plain` + `Transcript.load(path)`（读回旧稿，会跳过文件头、复现锚点）。
  - `add()` 每行都 `flush()`：崩了也只丢最后一句。

### 8.2 改动的文件
- `sysrec/llm.py`：新增 `CHUNK_PROMPT`（分段提炼）/ `MERGE_PROMPT`（合并纪要）/ `_split_chunks()` / `summarize_long()`；
  `chat()` 增加 `max_tokens=` 形参（分段用 900，终稿用配置的 2000）。
  - 为什么要 map-reduce：2 万字≈1.5 万 token 一次塞进去，**中段最容易被忽略**（lost in the middle），待办常整段漏。
- `sysrec/gui.py`：
  - `_begin_transcript()` / `_finish_transcript()` / `_start_transcribe_bg()` / `_latest_transcript_path()`
  - `_start_summary(text, auto, src)` 统一摘要入口（面板按钮 / 停止后自动 / 字幕窗都走它）
  - `_summary_work()` 改走 `summarize_long()`，md 落到 `summary_dir`
  - `sig_summary_step` 新增：分段进度 `x/y` 实时打在状态栏
  - `_stop_rec()` 尾部：先停后台转写 → 关转写稿 → 有文字且 `auto_summary` 就自动摘要
  - `_start_rec()` 尾部：`auto_transcribe` 或字幕已开 → 建转写稿 + 起后台引擎（**不弹字幕窗**）
  - `_on_panel_summary()`：从"施工中"占位改成真链路（本次转写稿 → 否则读最近一份 → 否则提示）
- `sysrec/config.py`：新增 `auto_transcribe` / `auto_summary` / `transcript_dir` / `summary_dir` / `summary_chunk_chars`；
  新增 `default_transcript_dir()`、`default_summary_dir()`。
- `sysrec/settings_dialog.py`：新增「自动转写 / 自动摘要」卡片（两个开关 + 两个目录）；
  顺手修了 **`字幕外观` 分组的"防截屏"复选框从来不显示**的老 bug（`groups` 只从 SPECS 收集，纯 CHECKS 分组被漏掉）。
- `sysrec/subtitle.py`：状态文案去掉"不保存文件"（现在会存转写稿，避免误解）。

### 8.3 两条必须知道的守卫（否则会静默丢数据）
1. **`_stop_subtitle()` 里的"录音中不许停"守卫**：录音进行中且转写稿在写时，关字幕窗只隐藏窗口、**不停引擎**——
   否则从那一刻起转写稿断档，停止后生成的摘要会缺后半场。
2. **空转写稿自动删除**：`_finish_transcript()` 里若 `lines == 0`（一句话都没转出来，比如静音），
   直接删掉那个只有文件头的 md，不给用户留垃圾。

### 8.4 目录约定
| 内容 | 默认位置 | 配置键 |
|---|---|---|
| 音频 | `<工具目录>\录音` | `output_dir` |
| 转写稿 | `<工具目录>\转写\转写_YYYYmmdd_HHMMSS.md` | `transcript_dir` |
| 摘要 | `<工具目录>\摘要\摘要_YYYYmmdd_HHMMSS.md` | `summary_dir` |

摘要 md 结构：`# 摘要（时间）` + LLM 纪要 + `---` + `## 原始转写（N 字）`。

### 8.5 验证记录（2026-10-20）
- 逻辑自测 **26/26 PASS**：转写稿建立/落盘/锚点/读回/最新一份、短稿单次调用、长稿分段+合并+进度回调、摘要落盘、`_summary_busy` 复位、无稿不崩、设置面板注册 4 个新键。
- 生命周期自测 **10/10 PASS**（假引擎注入）：后台转写启动、真实 `line` 信号→转写稿、**录音中关窗不停引擎**、停止后引擎关/卡片回 idle/`_sub_auto` 复位、不重复建引擎。
- 真实录音 6 秒：录音 mp3 落地、转写稿建立、静音时转写稿被自动删（`转写目录: []`）。
- 真实素材 E2E：3 段 TTS wav → SenseVoice → 转写稿 → agnes → md 全通。
  ⚠️ 该素材本身质量差（峰值 0.15，官方样例识别 100% 正确），**不是代码问题**；放大到 0.7/0.9/1.0 均无改善。
- 真实 agnes 调用：短稿单次、长稿 2 段 + 合并均正确；纪要质量可用（主题/要点/待办/风险四段齐全）。

### 8.6 已知边界
- **不能对任意音频文件转写**：`soundfile` 虽报支持 MP3，但本机 libsndfile 1.2.2 读自己录的 mp3 直接报 `System error`；
  无 ffmpeg、无 av/pydub。→ 面板「开始 AI 摘要」只作用于**转写稿**（本次或最近一份），不做"选个 mp3 再转"。
- 转写稿时间轴以**字幕定版时刻**为准（相对开录），暂停/继续不扣除暂停时长，与音频时间轴可能有偏差。
- `llm_prompt_template` 仍为空 = 用内置模板；分段模式不吃这个模板（分段/合并用自己的两条 prompt）。

---

接手先读本文件。

接手口令（复制下面一行到新任务输入框即可启动）：
接手 AI语音摘要_任务交接.md
