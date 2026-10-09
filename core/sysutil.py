# -*- coding: utf-8 -*-
"""系统工具：内存监控 + 进程优先级（Windows）。

只依赖标准库 ctypes，非 Windows 或调用失败时安全降级。
"""
import ctypes
import gc
import os

_IS_WINDOWS = os.name == "nt"

PRIORITY_CLASSES = {
    "idle": 0x00000040,
    "below_normal": 0x00004000,
    "normal": 0x00000020,
    "above_normal": 0x00008000,
    "high": 0x00000080,
}


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


if _IS_WINDOWS:
    try:
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        _kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        _kernel32.SetPriorityClass.restype = ctypes.c_int
        _kernel32.SetProcessWorkingSetSize.argtypes = [ctypes.c_void_p,
                                                       ctypes.c_size_t, ctypes.c_size_t]
        _kernel32.SetProcessWorkingSetSize.restype = ctypes.c_int
        _psapi = ctypes.WinDLL("psapi", use_last_error=True)
        _psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p,
                                                ctypes.POINTER(_PROCESS_MEMORY_COUNTERS),
                                                ctypes.c_uint]
        _psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    except Exception:
        _kernel32 = None
        _psapi = None
else:
    _kernel32 = None
    _psapi = None


def memory_mb():
    """当前进程占用内存 (MB)；取不到返回 None。"""
    if not _IS_WINDOWS:
        try:
            import resource
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
        except Exception:
            return None
    if _psapi is None or _kernel32 is None:
        return None
    try:
        counters = _PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        ok = _psapi.GetProcessMemoryInfo(_kernel32.GetCurrentProcess(),
                                         ctypes.byref(counters), counters.cb)
        if ok:
            return counters.WorkingSetSize / 1048576.0
    except Exception:
        pass
    return None


def peak_memory_mb():
    """进程峰值内存 (MB)；取不到返回 None。"""
    if _psapi is None or _kernel32 is None:
        return None
    try:
        counters = _PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        ok = _psapi.GetProcessMemoryInfo(_kernel32.GetCurrentProcess(),
                                         ctypes.byref(counters), counters.cb)
        if ok:
            return counters.PeakWorkingSetSize / 1048576.0
    except Exception:
        pass
    return None


def memory_text():
    mb = memory_mb()
    if mb is None:
        return "内存 --"
    peak = peak_memory_mb()
    if peak and peak > mb * 1.05:
        return "内存 %.0f MB（峰值 %.0f MB）" % (mb, peak)
    return "内存 %.0f MB" % mb


def set_priority(level="below_normal"):
    """把当前进程调到指定优先级，避免抢光机器资源影响你干别的事。"""
    if not _IS_WINDOWS or _kernel32 is None:
        return False
    value = PRIORITY_CLASSES.get(str(level).lower())
    if value is None:
        return False
    try:
        return bool(_kernel32.SetPriorityClass(_kernel32.GetCurrentProcess(), value))
    except Exception:
        return False


def release_memory(torch_module=None, trim_working_set=False):
    """回收内存。

    trim_working_set=True 时会把空闲页真正还给系统（较慢，适合阶段性调用，
    不适合每条录音都做，避免反复换页拖慢速度）。
    """
    gc.collect()
    if torch_module is not None:
        try:
            if torch_module.cuda.is_available():
                torch_module.cuda.empty_cache()
                torch_module.cuda.ipc_collect()
        except Exception:
            pass
    if trim_working_set:
        try:
            if _IS_WINDOWS and _kernel32 is not None:
                _kernel32.SetProcessWorkingSetSize(_kernel32.GetCurrentProcess(),
                                                   ctypes.c_size_t(-1), ctypes.c_size_t(-1))
        except Exception:
            pass
