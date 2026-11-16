"""Spawn-isolated local formula inference with hard timeout and memory recovery."""
from __future__ import annotations

import multiprocessing as mp
import os
from threading import Event, Thread
from time import monotonic, sleep
from typing import Callable

from .formula_recognition import FormulaProviderUnavailable, provider_from_worker_spec


def run_formula_provider_isolated(
    worker_spec: dict,
    image_bytes: bytes,
    *,
    timeout_seconds: float,
    max_rss_bytes: int,
    cancelled: Callable[[], bool],
) -> dict:
    """Run one provider in a Windows-compatible spawn process.

    Threads cannot safely stop a blocked ONNX call.  This boundary terminates
    the child on timeout, cancellation, crash, or observed RSS excess so the
    main printed-OCR path remains usable.
    """
    context = mp.get_context("spawn")
    parent_connection, child_connection = context.Pipe(duplex=False)
    process = context.Process(target=_formula_worker_entry, args=(child_connection, worker_spec, image_bytes, os.getpid()), daemon=True)
    try:
        process.start()
    except (OSError, RuntimeError) as error:
        # Formula recognition is optional evidence enrichment. Resource
        # pressure must not discard otherwise usable printed OCR for a page.
        parent_connection.close()
        child_connection.close()
        return {"status": "failed", "reason": type(error).__name__}
    child_connection.close()
    kill_on_parent_close = _WindowsKillOnCloseJob.assign(process.pid)
    deadline = monotonic() + timeout_seconds
    peak_rss: int | None = None
    try:
        while monotonic() < deadline:
            if cancelled():
                _terminate(process)
                return {"status": "cancelled", "peak_rss_bytes": peak_rss}
            rss = process_rss_bytes(process.pid)
            peak_rss = max(peak_rss or 0, rss or 0) or None
            if rss is not None and rss > max_rss_bytes:
                _terminate(process)
                return {"status": "memory_limit", "peak_rss_bytes": peak_rss}
            if parent_connection.poll(.02):
                try:
                    payload = parent_connection.recv()
                except EOFError:
                    payload = {"status": "crashed"}
                process.join(.5)
                if process.is_alive():
                    _terminate(process)
                payload["peak_rss_bytes"] = peak_rss
                return payload
            if not process.is_alive():
                process.join()
                return {"status": "crashed", "peak_rss_bytes": peak_rss, "exit_code": process.exitcode}
            sleep(.01)
        _terminate(process)
        return {"status": "timed_out", "peak_rss_bytes": peak_rss}
    finally:
        parent_connection.close()
        if process.is_alive():
            _terminate(process)
        kill_on_parent_close.close()


def _formula_worker_entry(connection, worker_spec: dict, image_bytes: bytes, parent_pid: int) -> None:
    watchdog_stop = Event()
    watchdog = Thread(target=_exit_if_parent_dies, args=(parent_pid, watchdog_stop), name="formula-parent-watchdog", daemon=True)
    watchdog.start()
    try:
        provider = provider_from_worker_spec(worker_spec)
        health = provider.health()
        if not health.get("ready"):
            connection.send({"status": "unavailable", "reason": str(health.get("reason", "provider_not_ready"))})
            return
        prediction = provider.recognize(image_bytes)
        connection.send({"status": "completed", "latex": prediction.latex[:4000], "provider": prediction.provider, "latency_seconds": prediction.latency_seconds})
    except FormulaProviderUnavailable as error:
        connection.send({"status": "unavailable", "reason": str(error)})
    except Exception as error:
        connection.send({"status": "failed", "reason": type(error).__name__})
    finally:
        watchdog_stop.set()
        connection.close()


def _exit_if_parent_dies(parent_pid: int, stopped: Event) -> None:
    while not stopped.wait(.2):
        if not _parent_process_alive(parent_pid):
            # A blocked ONNX call cannot be cooperatively cancelled. Exiting
            # from the watchdog releases this child even if its main thread is
            # still inside native inference.
            os._exit(72)


def _parent_process_alive(parent_pid: int) -> bool:
    if os.name == "nt":
        try:
            import ctypes
            synchronize = 0x00100000
            handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, parent_pid)
            if not handle:
                return False
            try:
                return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == 0x00000102
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except (AttributeError, OSError):
            return False
    try:
        os.kill(parent_pid, 0)
        return True
    except OSError:
        return False


def _terminate(process) -> None:
    if not process.is_alive():
        process.join()
        return
    process.terminate()
    process.join(.5)
    if process.is_alive() and hasattr(process, "kill"):
        process.kill()
        process.join(.5)


def process_rss_bytes(pid: int | None) -> int | None:
    if not pid:
        return None
    try:
        import psutil
        return int(psutil.Process(pid).memory_info().rss)
    except (ImportError, OSError):
        pass
    if os.name == "nt":
        return _windows_process_rss(pid)
    try:
        import subprocess
        completed = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], check=False, capture_output=True, text=True, timeout=.2)
        value = completed.stdout.strip()
        return int(value) * 1024 if value.isdigit() else None
    except (OSError, ValueError):
        return None


def _windows_process_rss(pid: int) -> int | None:
    try:
        import ctypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ]
        handle = ctypes.windll.kernel32.OpenProcess(0x0400 | 0x1000, False, pid)
        if not handle:
            return None
        try:
            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            ok = ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
            return int(counters.WorkingSetSize) if ok else None
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except (AttributeError, OSError):
        return None


class _WindowsKillOnCloseJob:
    """Parent-owned Windows Job Object: handle closure kills formula children."""

    def __init__(self, handle=None):
        self.handle = handle

    @classmethod
    def assign(cls, pid: int | None) -> "_WindowsKillOnCloseJob":
        if os.name != "nt" or not pid:
            return cls()
        try:
            import ctypes

            class LargeInteger(ctypes.Structure):
                _fields_ = [("QuadPart", ctypes.c_longlong)]

            class IoCounters(ctypes.Structure):
                _fields_ = [("ReadOperationCount", ctypes.c_ulonglong), ("WriteOperationCount", ctypes.c_ulonglong), ("OtherOperationCount", ctypes.c_ulonglong), ("ReadTransferCount", ctypes.c_ulonglong), ("WriteTransferCount", ctypes.c_ulonglong), ("OtherTransferCount", ctypes.c_ulonglong)]

            class BasicLimitInformation(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", LargeInteger), ("PerJobUserTimeLimit", LargeInteger), ("LimitFlags", ctypes.c_ulong), ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_ulong), ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_ulong), ("SchedulingClass", ctypes.c_ulong)]

            class ExtendedLimitInformation(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", BasicLimitInformation), ("IoInfo", IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

            job = ctypes.windll.kernel32.CreateJobObjectW(None, None)
            if not job:
                return cls()
            limits = ExtendedLimitInformation()
            limits.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not ctypes.windll.kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                ctypes.windll.kernel32.CloseHandle(job)
                return cls()
            process = ctypes.windll.kernel32.OpenProcess(0x0001 | 0x0100, False, pid)
            if not process:
                ctypes.windll.kernel32.CloseHandle(job)
                return cls()
            try:
                assigned = ctypes.windll.kernel32.AssignProcessToJobObject(job, process)
            finally:
                ctypes.windll.kernel32.CloseHandle(process)
            if not assigned:
                ctypes.windll.kernel32.CloseHandle(job)
                return cls()
            return cls(job)
        except (AttributeError, OSError):
            return cls()

    def close(self) -> None:
        if self.handle:
            try:
                import ctypes
                ctypes.windll.kernel32.CloseHandle(self.handle)
            except (AttributeError, OSError):
                pass
            self.handle = None
