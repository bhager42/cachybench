"""Capture the machine state a benchmark run happened under.

A frametime number is meaningless without the context it was produced in, so
every session records kernel, scheduler, governor, driver and GPU power state.
This is what lets you compare a run from today against one from three kernel
updates ago and know whether the difference is real.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


def _run(cmd: list[str], timeout: int = 10) -> str:
    """Run a command, returning stdout or an empty string on any failure."""
    if not shutil.which(cmd[0]):
        return ""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return proc.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def _read(path: str | Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _os_release() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in _read("/etc/os-release").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip().strip('"')
    return out


def _cpu_model() -> str:
    for line in _read("/proc/cpuinfo").splitlines():
        if line.lower().startswith("model name"):
            return line.split(":", 1)[1].strip()
    return platform.processor() or "unknown"


def _cpu_topology() -> str:
    text = _read("/proc/cpuinfo")
    threads = text.count("processor\t:")
    cores = set()
    for block in text.split("\n\n"):
        phys = re.search(r"physical id\s*:\s*(\d+)", block)
        core = re.search(r"core id\s*:\s*(\d+)", block)
        if phys and core:
            cores.add((phys.group(1), core.group(1)))
    if cores and threads:
        return f"{len(cores)}C / {threads}T"
    return f"{threads}T" if threads else "unknown"


def _memory_gib() -> str:
    for line in _read("/proc/meminfo").splitlines():
        if line.startswith("MemTotal:"):
            kib = int(re.sub(r"\D", "", line))
            return f"{kib / 1024 / 1024:.1f} GiB"
    return "unknown"


def _governor() -> str:
    govs = set()
    for path in Path("/sys/devices/system/cpu").glob("cpu*/cpufreq/scaling_governor"):
        value = _read(path)
        if value:
            govs.add(value)
    return ", ".join(sorted(govs)) if govs else "unknown"


def _scaling_driver() -> str:
    return _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_driver") or "unknown"


def _energy_perf_pref() -> str:
    prefs = set()
    for path in Path("/sys/devices/system/cpu").glob(
        "cpu*/cpufreq/energy_performance_preference"
    ):
        value = _read(path)
        if value:
            prefs.add(value)
    return ", ".join(sorted(prefs)) if prefs else "n/a"


def _gpus() -> list[str]:
    out = _run(["lspci", "-mm"])
    gpus = []
    for line in out.splitlines():
        if re.search(r'"(VGA compatible controller|3D controller|Display controller)"', line):
            fields = re.findall(r'"([^"]*)"', line)
            if len(fields) >= 4:
                gpus.append(f"{fields[2]} {fields[3]}")
    return gpus or ["unknown"]


def _amdgpu_state() -> dict[str, str]:
    """Read AMD performance-level knobs, which silently change benchmark results."""
    state: dict[str, str] = {}
    for card in sorted(Path("/sys/class/drm").glob("card[0-9]*/device")):
        if not (card / "power_dpm_force_performance_level").exists():
            continue
        name = card.parent.name
        level = _read(card / "power_dpm_force_performance_level")
        if level:
            state[f"{name} dpm level"] = level
        profile = _read(card / "pp_power_profile_mode")
        for line in profile.splitlines():
            if line.strip().endswith("*"):
                state[f"{name} power profile"] = line.strip().rstrip("*").strip()
                break
    return state


def _nvidia_state() -> dict[str, str]:
    out = _run(
        [
            "nvidia-smi",
            "--query-gpu=name,driver_version,power.limit,clocks.max.graphics",
            "--format=csv,noheader",
        ]
    )
    if not out:
        return {}
    parts = [p.strip() for p in out.splitlines()[0].split(",")]
    keys = ["NVIDIA GPU", "NVIDIA driver", "Power limit", "Max graphics clock"]
    return dict(zip(keys, parts))


def _mesa_version() -> str:
    out = _run(["glxinfo", "-B"])
    for line in out.splitlines():
        if "OpenGL version string" in line:
            return line.split(":", 1)[1].strip()
    return ""


def _vulkan_driver() -> str:
    out = _run(["vulkaninfo", "--summary"], timeout=20)
    driver = version = ""
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("driverName") and not driver:
            driver = stripped.split("=", 1)[-1].strip()
        elif stripped.startswith("driverInfo") and not version:
            version = stripped.split("=", 1)[-1].strip()
    return f"{driver} {version}".strip()


def _proton_versions() -> str:
    """List installed Proton builds, since the runtime is part of the result."""
    names = set()
    for root in (
        Path.home() / ".steam/steam/steamapps/common",
        Path.home() / ".local/share/Steam/steamapps/common",
        Path.home() / ".steam/root/compatibilitytools.d",
        Path.home() / ".local/share/Steam/compatibilitytools.d",
    ):
        if root.is_dir():
            for entry in root.iterdir():
                if entry.is_dir() and "proton" in entry.name.lower():
                    names.add(entry.name)
    return ", ".join(sorted(names)) if names else "none found"


def collect() -> dict[str, Any]:
    """Gather everything worth recording about this machine."""
    release = _os_release()
    info: dict[str, Any] = {
        "Distribution": release.get("PRETTY_NAME", "unknown"),
        "Kernel": platform.release(),
        "Architecture": platform.machine(),
        "CPU": _cpu_model(),
        "CPU topology": _cpu_topology(),
        "Memory": _memory_gib(),
        "GPU(s)": "; ".join(_gpus()),
        "CPU governor": _governor(),
        "CPU scaling driver": _scaling_driver(),
        "Energy perf preference": _energy_perf_pref(),
        "Session type": os.environ.get("XDG_SESSION_TYPE", "unknown"),
        "Desktop": os.environ.get("XDG_CURRENT_DESKTOP", "unknown"),
        "Python": platform.python_version(),
    }

    if mesa := _mesa_version():
        info["OpenGL / Mesa"] = mesa
    if vk := _vulkan_driver():
        info["Vulkan driver"] = vk

    info.update(_amdgpu_state())
    info.update(_nvidia_state())
    info["Proton builds"] = _proton_versions()

    if mangohud := _run(["mangohud", "--version"]):
        info["MangoHud"] = mangohud.splitlines()[0]
    if gamemode := _run(["gamemoded", "--version"]):
        info["GameMode"] = gamemode.splitlines()[0]

    return info


def warnings() -> list[str]:
    """Conditions that will make results noisy or non-comparable."""
    issues = []
    gov = _governor()
    if gov not in ("performance", "unknown") and "performance" not in gov:
        issues.append(
            f"CPU governor is '{gov}'. For repeatable results use: "
            "sudo cpupower frequency-set -g performance"
        )

    load = os.getloadavg()[0]
    cpus = os.cpu_count() or 1
    if load > cpus * 0.25:
        issues.append(
            f"System load is {load:.2f} across {cpus} threads. Close background "
            "tasks before benchmarking."
        )

    if os.environ.get("XDG_SESSION_TYPE") == "wayland":
        issues.append(
            "Wayland session: some compositors force vsync, which caps FPS. "
            "Disable the compositor's vsync or use a fullscreen (not borderless) "
            "window if your numbers look pinned to the refresh rate."
        )

    battery = Path("/sys/class/power_supply/BAT0/status")
    if battery.exists() and _read(battery) not in ("Full", "Charging", ""):
        issues.append("Running on battery. Plug in before benchmarking.")

    return issues
