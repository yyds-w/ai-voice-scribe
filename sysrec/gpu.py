# -*- coding: utf-8 -*-
"""GPU（CUDA）provider 支持：DLL 注册 + provider 决策（2026-10-24 用户要求③）。

背景：sherpa-onnx 的 CUDA 预编译轮子（1.13.8+cuda12.cudnn9）只自带
onnxruntime_providers_cuda.dll；cuBLAS/cuDNN/cuFFT/cudart 运行时要从
NVIDIA 的 pip 包（nvidia-cublas-cu12 / nvidia-cudnn-cu12 / nvidia-cufft-cu12
/ nvidia-cuda-runtime-cu12）里找。**缺任何一个（如 cufft）ORT 都会
静默回退 CPU 且误报第一个缺失项**，所以"用没用上 GPU"必须靠事前探测。

2026-10-24 实测的三个加载语义结论（都验证过）：
  1. os.add_dll_directory 对 ORT 的 provider 加载**无效**（它用
     LOAD_WITH_ALTERED_SEARCH_PATH，不吃 AddDllDirectory 用户目录）；
  2. os.environ["PATH"]=... 也无效（只改 CRT 副本，不动 LoadLibrary
     实际查询的进程环境块）；
  3. **只有 ctypes SetEnvironmentVariableW("PATH", ...) 真改进程环境块，
     ORT 的 provider 加载才找得到 nvidia DLL**（冒烟实测 CUDA 生效：
     GPU 功耗 5W→19W、显存 +1300MB）。

对外两个入口：
  · ensure_cuda_dlls()   进程启动早期调用一次（幂等）：注册 DLL 目录 + 置 PATH
  · resolve_provider()   "auto" → 装齐了且 sherpa 是 cuda 构建 → "cuda"，否则 "cpu"
"""
from __future__ import annotations

import glob
import os
import sys

_registered = False


def _site_packages() -> str:
    """定位当前解释器的 site-packages（借 numpy；没有就退回本包上级）。"""
    try:
        import numpy
        return os.path.dirname(os.path.dirname(os.path.abspath(numpy.__file__)))
    except Exception:                              # noqa: BLE001
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _nvidia_bin_dirs() -> list:
    return sorted(glob.glob(os.path.join(_site_packages(), "nvidia", "*", "bin")))


def ensure_cuda_dlls() -> int:
    """让本进程能加载 CUDA provider DLL（幂等，启动早期调用）。

    返回注册的 DLL 目录个数；0 = 没装 nvidia 运行时包（= 没装 CUDA 轮子生态）。
    """
    global _registered
    bins = _nvidia_bin_dirs()
    if not bins:
        return 0
    if not _registered:
        for d in bins:
            try:
                os.add_dll_directory(d)             # 对普通 LoadLibrary 仍有用
            except Exception:                       # noqa: BLE001
                pass
        try:
            import ctypes
            # 关键一步：真改进程环境块，ORT 的 altered-search 加载才认 PATH
            ctypes.windll.kernel32.SetEnvironmentVariableW(
                "PATH", os.pathsep.join(bins) + os.pathsep
                + os.environ.get("PATH", ""))
        except Exception:                           # noqa: BLE001
            pass
        _registered = True
    return len(bins)


def _sherpa_build_has_cuda() -> bool:
    """已装的 sherpa-onnx 是不是 CUDA 构建（版本号带 +cuda 后缀）。"""
    try:
        import sherpa_onnx
        return "cuda" in (sherpa_onnx.__version__ or "").lower()
    except Exception:                               # noqa: BLE001
        return False


def _cublas_loadable() -> bool:
    """cuBLAS 12 DLL 能否加载（nvidia 运行时包装齐的最低限证明）。"""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        ctypes.WinDLL("cublasLt64_12.dll")
        return True
    except Exception:                               # noqa: BLE001
        return False


def cuda_usable() -> bool:
    """CUDA 是否真能用：运行时 DLL 装齐 + sherpa 本身是 CUDA 构建。"""
    if not ensure_cuda_dlls():
        return False
    return _cublas_loadable() and _sherpa_build_has_cuda()


def resolve_provider(pref: str = "auto") -> str:
    """配置值 → 实际 provider。

    pref: "auto"（默认，探测）/ "cuda"（强制；运行时缺失时 sherpa 会静默
    回退 CPU，见 startup.log 的 provider 行）/ "cpu"（强制）。
    """
    p = (pref or "auto").strip().lower()
    if p in ("cuda", "cpu"):
        return p
    return "cuda" if cuda_usable() else "cpu"
