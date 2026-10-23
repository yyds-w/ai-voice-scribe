# -*- coding: utf-8 -*-
"""命令行录音（验收 / 排查用；正式使用请双击"AI语音摘要.lnk"）。

用法：
    python -m sysrec.cli --list-devices
    python -m sysrec.cli --seconds 14 --format mp3,wav --out D:\\some\\dir
    python -m sysrec.cli                    # 录到按 Ctrl+C 停止
"""
from __future__ import annotations

import argparse
import math
import sys
import time

from . import devices
from .recorder import Recorder, RecorderConfig


def _dbfs(x: float) -> float:
    return 20.0 * math.log10(x) if x > 1e-9 else float("-inf")


def _fmt_hms(seconds: float) -> str:
    s = int(seconds)
    return "%02d:%02d:%02d" % (s // 3600, (s % 3600) // 60, s % 60)


def cmd_list_devices() -> int:
    print("回环录音设备（录制“电脑正在播放的声音”）：")
    devs = devices.list_loopback_devices()
    if not devs:
        print("  （无可用回环设备）")
        return 1
    cur, _cur_name = devices.default_loopback_device()
    for i, name, ch, sr in devs:
        mark = "   ← 当前默认输出" if i == cur else ""
        print("  [%d] %s | %d Hz | %dch%s" % (i, name, sr, ch, mark))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="sysrec.cli", description="AI语音摘要 · 命令行录音")
    ap.add_argument("--list-devices", action="store_true", help="列出回环设备后退出")
    ap.add_argument("--device", type=int, default=None,
                    help="回环设备索引（默认自动跟随系统默认输出）")
    ap.add_argument("--seconds", type=float, default=0.0, help="录制时长（秒）；0 = 直到 Ctrl+C")
    ap.add_argument("--format", default="mp3", help="输出格式：mp3 / wav / mp3,wav")
    ap.add_argument("--bitrate", type=int, default=192, help="MP3 码率（kbps）")
    ap.add_argument("--out", default="", help="输出目录（默认 <工具目录>\\录音）")
    ap.add_argument("--segment", type=int, default=120, help="自动分卷分钟数；0 = 不分卷")
    args = ap.parse_args(argv)

    if args.list_devices:
        return cmd_list_devices()

    fmts = [f.strip() for f in args.format.split(",") if f.strip()]
    cfg = RecorderConfig(
        device_index=args.device,
        formats=fmts,
        mp3_bitrate=args.bitrate,
        out_dir=args.out,
        segment_minutes=args.segment,
    )

    def on_event(kind, payload):
        if kind in ("device_switched", "capture_recovered", "device_changed", "error"):
            print("\n[事件] %s: %s" % (kind, payload), flush=True)

    rec = Recorder(cfg, on_event=on_event)
    rec.start()
    snap = rec.snapshot()
    print("开始录音：%s | %d Hz / %dch → %s" %
          (snap["device_name"], snap["sample_rate"], snap["channels"], ", ".join(fmts)),
          flush=True)
    for f in snap["files"]:
        print("  输出：%s" % f, flush=True)

    t0 = time.time()
    tty = sys.stdout.isatty()
    last_line = 0.0
    try:
        while True:
            time.sleep(0.5)
            snap = rec.snapshot()
            now = time.time()
            if tty:
                sys.stdout.write(
                    "\r  已录 %s | RMS %6.1f dBFS | Peak %6.1f dBFS%s      "
                    % (_fmt_hms(snap["seconds"]), _dbfs(snap["level_rms"]),
                       _dbfs(snap["level_peak"]),
                       ("  | 补零 %.1fs" % snap["gap_seconds"]) if snap["gap_seconds"] > 0.5 else ""))
                sys.stdout.flush()
            elif now - last_line >= 2.0:
                last_line = now
                print("  已录 %.1fs | RMS %.1f dBFS | Peak %.1f dBFS%s"
                      % (snap["seconds"], _dbfs(snap["level_rms"]), _dbfs(snap["level_peak"]),
                         (" | 补零 %.1fs" % snap["gap_seconds"]) if snap["gap_seconds"] > 0.5 else ""),
                      flush=True)
            if args.seconds and (time.time() - t0) >= args.seconds:
                break
    except KeyboardInterrupt:
        if tty:
            print()
        print("收到 Ctrl+C，正在停止…", flush=True)

    if tty:
        print()
    result = rec.stop()
    if result is None:
        print("（没有在录音）")
        return 1
    print("录音结束：时长 %.1f 秒 | 采样率 %d Hz / %dch | 补零 %.2f 秒"
          % (result["seconds"], result["sample_rate"], result["channels"], result["gap_seconds"]))
    for f in result["files"]:
        print("  已保存：%s" % f)
    return 0


if __name__ == "__main__":
    sys.exit(main())
