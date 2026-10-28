"""Which window is in front, and whether the game runs (Windows only).

Read-only: these calls look at windows and processes, they send nothing.
"""
import ctypes
import os
import sys
from ctypes import wintypes

GAME_EXE = "starcitizen.exe"
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def available():
    return sys.platform == "win32"


def _exe_name(pid):
    """Lower-case executable name of a process, or None."""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return None
        return buf.value.replace("/", "\\").rsplit("\\", 1)[-1].lower()
    finally:
        kernel32.CloseHandle(handle)


def foreground_pid():
    user32 = ctypes.windll.user32
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), ctypes.byref(pid))
    return pid.value


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


TH32CS_SNAPPROCESS = 0x2
INVALID_HANDLE = ctypes.c_void_p(-1).value


def game_pids():
    """Process ids of StarCitizen.exe, from a system snapshot.

    A snapshot gives names without opening the processes, which an
    anti-cheat may refuse."""
    if not available():
        return set()
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE:
        return set()
    out = set()
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(ctypes.c_void_p(snap), ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == GAME_EXE:
                out.add(entry.th32ProcessID)
            ok = kernel32.Process32NextW(ctypes.c_void_p(snap), ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(snap))
    return out


def game_in_front():
    """True when the foreground window belongs to StarCitizen.exe (window
    titles are not enough: the launcher is called "Star Citizen" too)."""
    if not available():
        return False
    pid = foreground_pid()
    name = _exe_name(pid)
    return name == GAME_EXE if name else pid in game_pids()


def we_are_in_front():
    """True when the foreground window is ours (the overlay or its lists)."""
    return available() and foreground_pid() == os.getpid()


def game_running():
    """True while a StarCitizen.exe process exists."""
    return bool(game_pids())
