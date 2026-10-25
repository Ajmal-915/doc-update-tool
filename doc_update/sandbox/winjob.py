"""Windows Job Object helpers (memory limit, kill-on-close, UI restrictions).

Only imported on Windows.  The process is created suspended, assigned to the
job, and only then resumed, so it never runs outside the limits.
"""

from __future__ import annotations

import ctypes
import subprocess
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")

JobObjectBasicUIRestrictions = 4
JobObjectExtendedLimitInformation = 9

JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

JOB_OBJECT_UILIMIT_HANDLES = 0x0001
JOB_OBJECT_UILIMIT_READCLIPBOARD = 0x0002
JOB_OBJECT_UILIMIT_WRITECLIPBOARD = 0x0004
JOB_OBJECT_UILIMIT_SYSTEMPARAMETERS = 0x0008
JOB_OBJECT_UILIMIT_DISPLAYSETTINGS = 0x0010
JOB_OBJECT_UILIMIT_GLOBALATOMS = 0x0020
JOB_OBJECT_UILIMIT_DESKTOP = 0x0040
JOB_OBJECT_UILIMIT_EXITWINDOWS = 0x0080

CREATE_SUSPENDED = 0x00000004
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class JOBOBJECT_BASIC_UI_RESTRICTIONS(ctypes.Structure):
    _fields_ = [("UIRestrictionsClass", wintypes.DWORD)]


kernel32.CreateJobObjectW.restype = wintypes.HANDLE
kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
kernel32.SetInformationJobObject.restype = wintypes.BOOL
kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
kernel32.QueryInformationJobObject.restype = wintypes.BOOL
kernel32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                               ctypes.POINTER(wintypes.DWORD)]
kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
kernel32.TerminateJobObject.restype = wintypes.BOOL
kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
ntdll.NtResumeProcess.restype = ctypes.c_long
ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]


def _check(ok, what):
    if not ok:
        raise OSError(ctypes.get_last_error(), f"{what} failed")


class JobObject:
    def __init__(self, memory_mb: int, max_processes: int = 4):
        self.memory_limit = int(memory_mb) * 1024 * 1024
        self.handle = kernel32.CreateJobObjectW(None, None)
        _check(self.handle, "CreateJobObjectW")
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_PROCESS_MEMORY | JOB_OBJECT_LIMIT_JOB_MEMORY | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        )
        # the venv python.exe is a small launcher that starts the real interpreter as a child
        info.BasicLimitInformation.ActiveProcessLimit = max_processes
        info.ProcessMemoryLimit = self.memory_limit
        info.JobMemoryLimit = self.memory_limit + 64 * 1024 * 1024
        _check(kernel32.SetInformationJobObject(self.handle, JobObjectExtendedLimitInformation,
                                                ctypes.byref(info), ctypes.sizeof(info)), "SetInformationJobObject")
        ui = JOBOBJECT_BASIC_UI_RESTRICTIONS()
        ui.UIRestrictionsClass = (
            JOB_OBJECT_UILIMIT_HANDLES | JOB_OBJECT_UILIMIT_READCLIPBOARD | JOB_OBJECT_UILIMIT_WRITECLIPBOARD
            | JOB_OBJECT_UILIMIT_SYSTEMPARAMETERS | JOB_OBJECT_UILIMIT_DISPLAYSETTINGS | JOB_OBJECT_UILIMIT_GLOBALATOMS
            | JOB_OBJECT_UILIMIT_DESKTOP | JOB_OBJECT_UILIMIT_EXITWINDOWS
        )
        kernel32.SetInformationJobObject(self.handle, JobObjectBasicUIRestrictions, ctypes.byref(ui), ctypes.sizeof(ui))

    def start(self, args: list[str], cwd: str, env: dict) -> subprocess.Popen:
        proc = subprocess.Popen(
            args, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=CREATE_SUSPENDED | CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP, close_fds=True,
        )
        handle = wintypes.HANDLE(int(proc._handle))  # noqa: SLF001 - the process handle is needed here
        if not kernel32.AssignProcessToJobObject(self.handle, handle):
            err = ctypes.get_last_error()
            proc.kill()
            raise OSError(err, "AssignProcessToJobObject failed")
        status = ntdll.NtResumeProcess(handle)
        if status != 0:
            self.terminate()
            raise OSError(status, "NtResumeProcess failed")
        return proc

    def peak_memory(self) -> int:
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        ret = wintypes.DWORD(0)
        if kernel32.QueryInformationJobObject(self.handle, JobObjectExtendedLimitInformation, ctypes.byref(info),
                                              ctypes.sizeof(info), ctypes.byref(ret)):
            return int(max(info.PeakProcessMemoryUsed, info.PeakJobMemoryUsed))
        return 0

    def terminate(self) -> None:
        kernel32.TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        if self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = None
