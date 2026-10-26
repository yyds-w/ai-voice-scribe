# -*- coding: utf-8 -*-
"""启动器：带日志、出错不闪退（pythonw 零控制台启动也能看到错误）。

按键：双击"AI语音摘要.lnk"走的就是本文件（pythonw.exe run.py，无控制台）。
出错时：写 startup.log + 弹 Windows 原生错误框（无控制台场景）。
"""
import datetime
import os
import sys
import traceback

from sysrec import app_paths

BASE = app_paths.data_path()          # 可写数据根：源码=项目目录；打包后=exe 所在目录
LOG = os.path.join(BASE, "startup.log")
APP_ID = "liuxupeng.system-recorder"      # Windows 任务栏据此把本程序识别为独立应用
IPC_KEY = "liuxupeng.system-recorder.ipc"  # 单实例锁的命名管道名
ICON = app_paths.resource_path("assets", "app.ico")   # 只读资源：打包后在 _internal\assets

_HAS_CONSOLE = True


def _ensure_streams():
    """无控制台（pythonw）时 sys.stdout/stderr 为 None：报错会凭空消失。

    这里把它们重定向到 startup.log，保证异常可追溯。
    """
    global _HAS_CONSOLE
    if sys.stdout is not None and sys.stderr is not None:
        _HAS_CONSOLE = True
        return
    _HAS_CONSOLE = False
    fallback = None
    try:
        fallback = open(LOG, "a", encoding="utf-8", buffering=1)
    except Exception:  # noqa: BLE001
        try:
            fallback = open(os.devnull, "w")
        except Exception:  # noqa: BLE001
            pass
    if sys.stdout is None:
        sys.stdout = fallback
    if sys.stderr is None:
        sys.stderr = fallback


_ensure_streams()


def _log(msg: str) -> None:
    line = "[%s] %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    if _HAS_CONSOLE:
        try:
            print(line, flush=True)
        except Exception:  # noqa: BLE001
            pass
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def _set_app_user_model_id() -> None:
    """让任务栏/Alt+Tab 认它是独立应用（否则显示 Python 图标）。"""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:  # noqa: BLE001
        pass


def _set_console_icon() -> None:
    """有控制台（双击 start.bat）时，把控制台窗口图标也换掉，避免任务栏两条图标。"""
    if os.name != "nt" or not os.path.exists(ICON):
        return
    try:
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.windll.user32
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if not hwnd:
            return
        u32.LoadImageW.restype = wintypes.HANDLE
        h = u32.LoadImageW(None, ICON, 1, 0, 0, 0x0010)  # IMAGE_ICON, LR_LOADFROMFILE
        if h:
            u32.SendMessageW(hwnd, 0x0080, 0, h)   # WM_SETICON, ICON_SMALL
            u32.SendMessageW(hwnd, 0x0080, 1, h)   # WM_SETICON, ICON_BIG
    except Exception:  # noqa: BLE001
        pass


def _install_excepthook() -> None:
    """兜底：未捕获异常先写日志。

    PyQt6 对"Qt 回调里未捕获的 Python 异常"会直接终止进程（qFatal），
    且默认看不到 traceback——这里是最后的记录机会。
    """
    def _hook(exc_type, exc, tb):
        try:
            _log("未捕获异常：\n" + "".join(traceback.format_exception(exc_type, exc, tb)))
        except Exception:  # noqa: BLE001
            pass
    sys.excepthook = _hook


def _single_instance_guard():
    """单实例保护：若已有实例在运行 → 通知它显示窗口并返回 None（本进程应退出）。

    用命名管道（QLocalServer / QLocalSocket）实现。
    【为什么需要】程序可最小化到托盘；没有单实例锁时，重复双击入口会
    开出多个实例、多个托盘图标（2026-10-16 用户反馈）。
    """
    from PyQt6.QtNetwork import QLocalServer, QLocalSocket

    def _notify_existing() -> bool:
        sock = QLocalSocket()
        sock.connectToServer(IPC_KEY)
        if not sock.waitForConnected(400):
            return False
        try:
            sock.write(b"show\n")
            sock.flush()
            sock.waitForBytesWritten(400)
        except Exception:  # noqa: BLE001
            pass
        finally:
            sock.disconnectFromServer()
        return True

    if _notify_existing():
        return None
    server = QLocalServer()
    # 清掉异常退出可能残留的管道名（Windows 上通常随进程自动回收，这里兜底）
    QLocalServer.removeServer(IPC_KEY)
    if server.listen(IPC_KEY):
        return server
    # 极端竞争：两个实例同时判定"无主"，抢名失败的一方再尝试通知一次
    if _notify_existing():
        return None
    _log("单实例监听失败（%s）；本实例继续运行，但不受单实例保护。" % server.errorString())
    return server


def _run() -> int:
    _install_excepthook()
    _set_app_user_model_id()
    _set_console_icon()
    _log("=== 启动 AI语音摘要 ===")

    from PyQt6.QtWidgets import QApplication, QSystemTrayIcon
    from PyQt6.QtGui import QIcon

    app = QApplication(sys.argv)
    app.setApplicationName("AI语音摘要")
    # 托盘可用时：关窗 = 收进托盘，不退出；托盘不可用时：关窗即退出
    app.setQuitOnLastWindowClosed(not QSystemTrayIcon.isSystemTrayAvailable())
    if os.path.exists(ICON):
        app.setWindowIcon(QIcon(ICON))

    server = _single_instance_guard()
    if server is None:
        _log("已有实例在运行：已通知它显示窗口，本实例退出。")
        return 0

    from sysrec.gui import MainWindow
    # 注意：w 必须保留 Python 引用——Qt 窗口无引用会被 GC 回收，
    # 表现为"面板启动后立刻消失"。不要改成丢弃返回值的写法。
    w = MainWindow(ipc_server=server, logger=_log)
    # 2026-10-18：主窗口只作逻辑载体（托盘/IPC）；界面是 MainWindow 内部的独立浮动面板，
    # 构造时已自行 place_default + show。不再显示空的主窗口。
    # w.show()
    _log("面板已就绪")
    rc = app.exec()
    _log("已退出，返回码 %d" % rc)
    return rc


def _fatal_notice(tb: str) -> None:
    """启动失败提示：有控制台等回车；没有控制台弹系统错误框。"""
    if sys.stdin and sys.stdin.isatty():
        try:
            input("\n启动失败，详情已写入 startup.log，按回车退出…")
        except Exception:  # noqa: BLE001
            pass
        return
    try:
        import ctypes
        last = (tb or "").strip().splitlines()
        brief = last[-1][:220] if last else "未知错误"
        ctypes.windll.user32.MessageBoxW(
            None,
            "启动失败。\n\n最后一行错误：\n%s\n\n完整详情已写入：\n%s" % (brief, LOG),
            "AI语音摘要 —— 启动失败", 0x10)
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    # 摘要转写子进程入口（2026-10-25 方案 B）：打包后 exe 靠这个开关以 worker 身份启动，
    # 源码模式下 asr_client 走 `-m sysrec.asr_worker`，不会用到这里。
    if "--asr-worker" in sys.argv:
        from sysrec.asr_worker import main as _asr_worker_main
        sys.exit(_asr_worker_main())
    try:
        sys.exit(_run())
    except Exception:
        tb = traceback.format_exc()
        _log("启动失败:\n" + tb)
        try:
            print(tb)
        except Exception:  # noqa: BLE001
            pass
        _fatal_notice(tb)
        sys.exit(1)
