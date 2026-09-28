"""Host resources as this process sees them: CPU, memory, disks. Inside Docker that is the Docker host / VM."""
from __future__ import annotations

import os
import platform
import shutil
import socket
import time
from pathlib import Path
from typing import Any

import psutil

from ..config import get_settings

GB = 1024 ** 3


def _os_name() -> str:
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return f"{platform.system()} {platform.release()}".strip()


def _level(pct: float) -> str:
    return "high" if pct >= get_settings().monitor_warn_pct else "ok"


def _disk_specs() -> list[tuple[str, str]]:
    s = get_settings()
    specs = [(s.data_dir, "Data")]  # uploads, rejects, SQLite/DuckDB files
    for item in s.monitor_disks.split(","):
        item = item.strip()
        if item:
            path, _, label = item.partition(":")
            specs.append((path.strip(), label.strip() or path.strip()))
    return specs


def snapshot() -> dict[str, Any]:
    cpu = psutil.cpu_percent(interval=0.25)
    vm = psutil.virtual_memory()
    used = vm.total - vm.available
    mem_pct = used / vm.total * 100 if vm.total else 0.0
    load = list(os.getloadavg()) if hasattr(os, "getloadavg") else None
    disks, missing = [], []
    for path, label in _disk_specs():
        try:
            du = shutil.disk_usage(path)
        except OSError:
            missing.append(path)
            continue
        pct = du.used / du.total * 100 if du.total else 0.0
        disks.append({"label": label, "path": path, "total_gb": round(du.total / GB, 1), "used_gb": round(du.used / GB, 1),
                      "percent": round(pct, 1), "level": _level(pct)})
    return {
        "hostname": socket.gethostname(), "os": _os_name(), "vcpus": psutil.cpu_count(logical=True),
        "warn_at_pct": get_settings().monitor_warn_pct, "boot_time": psutil.boot_time(), "sampled_at": time.time(),
        "cpu": {"percent": round(cpu, 1), "level": _level(cpu), "load_avg": [round(x, 2) for x in load] if load else None},
        "memory": {"total_gb": round(vm.total / GB, 1), "used_gb": round(used / GB, 1), "percent": round(mem_pct, 1),
                   "level": _level(mem_pct)},
        "disks": disks, "disks_unavailable": missing,
    }
