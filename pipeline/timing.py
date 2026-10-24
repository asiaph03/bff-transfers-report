"""Timings and memory for the validation report. Seconds, row counts and megabytes only: never data."""
from __future__ import annotations

import sys
import time


class StageTimer:
    """Wall-clock seconds per named stage, in the order the stages first ran.

    `lap(name)` credits the time since the previous lap (or since the timer started) to `name`, so a run that
    stops early simply has no laps for the stages it never reached.
    """

    def __init__(self, clock=time.perf_counter):
        self.clock = clock
        self.seconds: dict[str, float] = {}
        self._last = clock()

    def lap(self, name: str) -> None:
        now = self.clock()
        self.seconds[name] = self.seconds.get(name, 0.0) + (now - self._last)
        self._last = now

    def facts(self, fetch: dict[str, dict] | None = None, peak_mb: float | None = None) -> dict[str, str]:
        """Report lines: per-source fetch (seconds, rows, retries), per-stage seconds, peak memory."""
        out: dict[str, str] = {}
        for name, f in (fetch or {}).items():
            retries = f.get("retries", 0)
            parts = [f"{f['seconds']:.1f} s"]
            if f.get("rows") is not None:
                parts.append(f"{f['rows']} rows")
            parts.append(f"{retries} {'retry' if retries == 1 else 'retries'}")
            out[f"fetch {name}"] = ", ".join(parts)
        if fetch:
            out["fetch total"] = f"{sum(f['seconds'] for f in fetch.values()):.1f} s"
        for name, s in self.seconds.items():
            out[f"stage {name}"] = f"{s:.2f} s"
        if peak_mb:
            out["peak memory"] = f"{peak_mb:.0f} MB"
        return out


def peak_memory_mb() -> float | None:
    """Peak resident memory of this process in MB (Windows peak working set, or ru_maxrss elsewhere)."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

        k32, ps = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        ps.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        c = Counters()
        c.cb = ctypes.sizeof(c)
        if not ps.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
            return None
        return c.PeakWorkingSetSize / 2**20
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak / 2**20 if sys.platform == "darwin" else peak / 1024
    except ImportError:
        return None
