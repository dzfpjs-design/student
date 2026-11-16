"""Windows-only collection of minimum-PC hardware evidence for local benchmarks."""
from __future__ import annotations

from datetime import datetime, timezone
import ctypes
import os
import platform
import subprocess


COLLECTOR_VERSION = "minimum_pc_environment_v1"


def collect_environment(
    confirm_avx2: bool = False,
    *,
    system_name: str | None = None,
    machine: str | None = None,
    logical_cpus: int | None = None,
    memory_bytes: int | None = None,
    nvme_bytes: int | None = None,
    cpu_flags: set[str] | None = None,
    gpu_devices: list[str] | None = None,
) -> dict:
    """Collect measured host facts; AVX2 stays false unless independently evidenced."""
    system_name = system_name or platform.system()
    if system_name != "Windows":
        raise RuntimeError("minimum-PC hardware evidence must be collected on Windows")
    machine = (machine or platform.machine()).lower()
    architecture = "x64" if machine in {"amd64", "x86_64", "x64"} else machine
    logical_cpus = logical_cpus if logical_cpus is not None else (os.cpu_count() or 0)
    memory_bytes = memory_bytes if memory_bytes is not None else _windows_memory_bytes()
    if nvme_bytes is None:
        nvme_bytes = _powershell_integer("$v=(Get-PhysicalDisk | Where-Object {$_.BusType -eq 'NVMe'} | Measure-Object -Property Size -Sum).Sum; if($null -eq $v){0}else{$v}")
    if gpu_devices is None:
        gpu_devices = _powershell_lines("Get-CimInstance Win32_VideoController | ForEach-Object {$_.Name}")
    if cpu_flags is None:
        cpu_flags = _cpu_flags()
    avx2_by_probe = "avx2" in {flag.lower() for flag in cpu_flags}
    return {
        "platform": "Windows",
        "architecture": architecture,
        "logical_cpus": logical_cpus,
        "avx2": bool(avx2_by_probe or confirm_avx2),
        "avx2_evidence": "cpu_flags" if avx2_by_probe else "operator_confirmed" if confirm_avx2 else "not_verified",
        "memory_gb": round(memory_bytes / (1024 ** 3)),
        "nvme_gb": round(nvme_bytes / (1024 ** 3)),
        "discrete_gpu_required": False,
        "gpu_devices_observed": gpu_devices,
        "collection_provenance": {"collector": COLLECTOR_VERSION, "collected_at": datetime.now(timezone.utc).isoformat(), "host_platform": system_name},
    }


def has_collected_provenance(payload: dict) -> bool:
    provenance = payload.get("collection_provenance")
    return isinstance(provenance, dict) and provenance.get("collector") == COLLECTOR_VERSION and bool(provenance.get("collected_at")) and provenance.get("host_platform") == "Windows"


def _windows_memory_bytes() -> int:
    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    status = MemoryStatusEx()
    status.dwLength = ctypes.sizeof(MemoryStatusEx)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError("GlobalMemoryStatusEx failed")
    return int(status.ullTotalPhys)


def _cpu_flags() -> set[str]:
    try:
        import cpuinfo  # type: ignore
        return set(cpuinfo.get_cpu_info().get("flags", []))
    except Exception:
        return set()


def _powershell_integer(command: str) -> int:
    lines = _powershell_lines(command)
    try:
        return int(lines[-1]) if lines else 0
    except ValueError:
        return 0


def _powershell_lines(command: str) -> list[str]:
    try:
        completed = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command], check=False, capture_output=True, text=True, timeout=15)
        return [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError):
        return []

