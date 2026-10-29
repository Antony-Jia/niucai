"""Windows 进程树管理：Job Object（关闭句柄即整树结束）与进程启动时间，仅标准库 ctypes。

Runner 被强制结束时句柄随之关闭，KILL_ON_JOB_CLOSE 让引擎进程树一并结束，不留孤儿进程。
"""

import ctypes
from ctypes import wintypes

PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in ("r", "w", "o", "rb", "wb", "ob")]


class BASIC_LIMIT(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class EXTENDED_LIMIT(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", BASIC_LIMIT),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def kernel32():
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    library.CreateJobObjectW.restype = wintypes.HANDLE
    library.OpenProcess.restype = wintypes.HANDLE
    library.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    library.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    library.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    library.CloseHandle.argtypes = (wintypes.HANDLE,)
    library.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    library.GetProcessTimes.argtypes = (wintypes.HANDLE, *(ctypes.POINTER(wintypes.FILETIME),) * 4)
    return library


class JobObject:
    """一个 Attempt 一个 Job；引擎进程启动后立即加入，其后代进程自动继承。"""

    def __init__(self):
        self.k32 = kernel32()
        self.handle = self.k32.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
        info = EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.k32.SetInformationJobObject(
            self.handle, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS, ctypes.byref(info), ctypes.sizeof(info)
        ):
            self.close()
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")

    def assign(self, pid):
        process = self.k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not process:
            raise OSError(ctypes.get_last_error(), "OpenProcess failed")
        try:
            if not self.k32.AssignProcessToJobObject(self.handle, process):
                raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
        finally:
            self.k32.CloseHandle(process)

    def terminate(self):
        if self.handle:
            self.k32.TerminateJobObject(self.handle, 1)

    def close(self):
        if self.handle:
            self.k32.CloseHandle(self.handle)
            self.handle = None


def process_start(pid):
    """进程创建时间（FILETIME 整数字符串），与 Linux /proc stat 起始时间同用途：防 PID 复用误杀。"""
    k32 = kernel32()
    process = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not process:
        return None
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not k32.GetProcessTimes(process, *(ctypes.byref(t) for t in times)):
            return None
        return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
    finally:
        k32.CloseHandle(process)
