"""Launching benchmarks and knowing when they finish.

The hard part of automating Steam is that ``steam -applaunch`` returns
immediately: it hands the request to the already-running Steam client over IPC,
and the game becomes a child of Steam, not of us. So we cannot wait on a
subprocess. Instead we resolve the app's install directory from its
``appmanifest`` and watch ``/proc`` for processes belonging to it.

The other half is getting MangoHud to log without any per-run manual step.
Environment variables cannot reach a process Steam spawns, but MangoHud reads
``~/.config/MangoHud/MangoHud.conf`` at game start, so the session rewrites that
file before each run and restores the user's own copy on exit.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

MANGOHUD_CONF = Path.home() / ".config" / "MangoHud" / "MangoHud.conf"

STEAM_ROOTS = [
    Path.home() / ".local/share/Steam",
    Path.home() / ".steam/steam",
    Path.home() / ".steam/root",
    Path.home() / ".var/app/com.valvesoftware.Steam/data/Steam",
]


class RunnerError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Steam library discovery
# --------------------------------------------------------------------------


def steam_libraries() -> list[Path]:
    """Every steamapps directory Steam knows about, including extra drives."""
    libraries: list[Path] = []
    seen: set[Path] = set()

    for root in STEAM_ROOTS:
        steamapps = root / "steamapps"
        if steamapps.is_dir():
            resolved = steamapps.resolve()
            if resolved not in seen:
                seen.add(resolved)
                libraries.append(resolved)

        vdf = steamapps / "libraryfolders.vdf"
        if not vdf.is_file():
            continue
        try:
            text = vdf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # The VDF nesting is irrelevant here; every library exposes a "path" key.
        for match in re.finditer(r'"path"\s+"([^"]+)"', text):
            candidate = Path(match.group(1)) / "steamapps"
            if candidate.is_dir():
                resolved = candidate.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    libraries.append(resolved)

    return libraries


def app_install_dir(appid: int) -> Path | None:
    """Resolve an appid to its installed directory via its appmanifest."""
    for library in steam_libraries():
        manifest = library / f"appmanifest_{appid}.acf"
        if not manifest.is_file():
            continue
        try:
            text = manifest.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = re.search(r'"installdir"\s+"([^"]+)"', text)
        if match:
            path = library / "common" / match.group(1)
            if path.is_dir():
                return path
    return None


def app_name(appid: int) -> str | None:
    for library in steam_libraries():
        manifest = library / f"appmanifest_{appid}.acf"
        if manifest.is_file():
            text = manifest.read_text(encoding="utf-8", errors="replace")
            match = re.search(r'"name"\s+"([^"]+)"', text)
            if match:
                return match.group(1)
    return None


def installed_apps() -> list[tuple[int, str]]:
    """Every installed Steam app, for the `list-games` command."""
    apps: dict[int, str] = {}
    for library in steam_libraries():
        for manifest in library.glob("appmanifest_*.acf"):
            try:
                text = manifest.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            appid = re.search(r'"appid"\s+"(\d+)"', text)
            name = re.search(r'"name"\s+"([^"]+)"', text)
            if appid and name:
                apps[int(appid.group(1))] = name.group(1)
    return sorted(apps.items(), key=lambda kv: kv[1].lower())


def steam_running() -> bool:
    return bool(_pids_matching(lambda comm, cmdline, exe: comm == "steam"))


# --------------------------------------------------------------------------
# Process tracking
# --------------------------------------------------------------------------


def _proc_fields(pid: str) -> tuple[str, str, str]:
    """Return (comm, cmdline, resolved exe) for a pid, blanks if unreadable."""
    base = Path("/proc") / pid
    try:
        comm = (base / "comm").read_text(errors="replace").strip()
    except OSError:
        comm = ""
    try:
        cmdline = (base / "cmdline").read_bytes().replace(b"\0", b" ").decode(
            "utf-8", errors="replace"
        )
    except OSError:
        cmdline = ""
    try:
        exe = os.path.realpath(base / "exe")
    except OSError:
        exe = ""
    return comm, cmdline, exe


def _pids_matching(predicate: Callable[[str, str, str], bool]) -> set[int]:
    found: set[int] = set()
    my_pid = os.getpid()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == my_pid:
            continue
        comm, cmdline, exe = _proc_fields(entry.name)
        if not comm and not cmdline:
            continue
        try:
            if predicate(comm, cmdline, exe):
                found.add(pid)
        except OSError:
            continue
    return found


def find_game_processes(install_dir: Path, process_hint: str = "") -> set[int]:
    """Find live processes belonging to a game.

    Native games are matched on their executable path. Proton games run under
    Wine, where ``/proc/pid/exe`` points at the Wine loader rather than the game,
    so the command line is checked too — both in POSIX form and in the
    backslashed ``Z:\\home\\...`` form Wine passes through.
    """
    posix = str(install_dir)
    windows = "Z:" + posix.replace("/", "\\")
    hint = process_hint.lower().strip()

    def predicate(comm: str, cmdline: str, exe: str) -> bool:
        if hint and hint in comm.lower():
            return True
        if exe and exe.startswith(posix + os.sep):
            return True
        if posix in cmdline or windows in cmdline:
            return True
        return False

    return _pids_matching(predicate)


def wait_for_start(
    install_dir: Path,
    timeout_s: int,
    process_hint: str = "",
    poll_s: float = 2.0,
    on_tick: Callable[[float], None] | None = None,
) -> set[int]:
    """Block until the game's processes appear, or time out."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        pids = find_game_processes(install_dir, process_hint)
        if pids:
            return pids
        if on_tick:
            on_tick(deadline - time.monotonic())
        time.sleep(poll_s)
    raise RunnerError(
        f"Game did not start within {timeout_s}s. First launches can be slow "
        "(Proton install, shader compilation) — raise launch_timeout_s, or check "
        "the game actually launches by hand."
    )


def wait_for_exit(
    install_dir: Path,
    timeout_s: int,
    process_hint: str = "",
    poll_s: float = 2.0,
    on_tick: Callable[[float], None] | None = None,
) -> bool:
    """Block until the game's processes are gone. Returns False on timeout.

    Two consecutive empty polls are required, because Proton games briefly show
    no matching process while handing off between the launcher and the game.
    """
    deadline = time.monotonic() + timeout_s
    empty_polls = 0
    while time.monotonic() < deadline:
        if find_game_processes(install_dir, process_hint):
            empty_polls = 0
        else:
            empty_polls += 1
            if empty_polls >= 2:
                return True
        if on_tick:
            on_tick(deadline - time.monotonic())
        time.sleep(poll_s)
    return False


def terminate_game(install_dir: Path, process_hint: str = "", grace_s: int = 15) -> None:
    """Ask a game to quit, then insist."""
    pids = find_game_processes(install_dir, process_hint)
    for pid in pids:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)

    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not find_game_processes(install_dir, process_hint):
            return
        time.sleep(1)

    for pid in find_game_processes(install_dir, process_hint):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)
    time.sleep(2)


# --------------------------------------------------------------------------
# MangoHud configuration
# --------------------------------------------------------------------------


def build_mangohud_conf(output_folder: Path, autostart_s: int, duration_s: int, extra: str = "") -> str:
    """Compose a MangoHud.conf that logs a fixed window with no keypress.

    ``autostart_log`` skips the first N seconds so shader compilation and menu
    fade-ins do not pollute the capture; ``log_duration`` stops the log on its
    own so the window is identical across runs.

    ``autostart_s=0`` means no autostart at all: the log waits for the
    toggle_logging keybind. Manual mode relies on this, so the option must be
    left out rather than clamped to 1 — MangoHud treats any positive value as
    "start logging N seconds after init", and a run that starts logging in the
    menus is a run that closes the game before the benchmark begins.
    """
    lines = [
        "# Written by cachybench. Your original config was backed up alongside it.",
        f"output_folder={output_folder.resolve()}",
        *([f"autostart_log={autostart_s}"] if autostart_s > 0 else []),
        f"log_duration={duration_s}",
        "log_interval=0",
        "fps",
        "frametime",
        # The graph costs a little to draw every frame and adds nothing to the
        # log, but the rest of these must stay enabled: MangoHud only logs the
        # columns whose HUD parameters are switched on.
        "frame_timing=0",
        "cpu_stats",
        "cpu_temp",
        "cpu_power",
        "cpu_mhz",
        "gpu_stats",
        "gpu_temp",
        "gpu_junction_temp",
        "gpu_core_clock",
        "gpu_mem_clock",
        "gpu_power",
        "vram",
        "ram",
        "swap",
        # The overlay stays visible on purpose: in manual mode it is the only
        # confirmation that Shift+F2 actually started the log. Add
        # `no_display=1` to mangohud_extra if you would rather not see it.
    ]
    if extra:
        lines.extend(part.strip() for part in extra.split(",") if part.strip())
    return "\n".join(lines) + "\n"


@contextlib.contextmanager
def managed_mangohud_conf() -> Iterator[Callable[[Path, int, int, str], None]]:
    """Take temporary ownership of MangoHud.conf, restoring it afterwards.

    Yields a writer so each run can point MangoHud at its own output folder.
    """
    MANGOHUD_CONF.parent.mkdir(parents=True, exist_ok=True)
    backup = MANGOHUD_CONF.with_suffix(".conf.cachybench-backup")
    had_original = MANGOHUD_CONF.exists()
    if had_original:
        shutil.copy2(MANGOHUD_CONF, backup)

    def write(output_folder: Path, autostart_s: int, duration_s: int, extra: str = "") -> None:
        output_folder.mkdir(parents=True, exist_ok=True)
        MANGOHUD_CONF.write_text(
            build_mangohud_conf(output_folder, autostart_s, duration_s, extra),
            encoding="utf-8",
        )

    try:
        yield write
    finally:
        if had_original:
            shutil.copy2(backup, MANGOHUD_CONF)
            backup.unlink(missing_ok=True)
        else:
            MANGOHUD_CONF.unlink(missing_ok=True)


def newest_log(folder: Path) -> Path | None:
    """The per-frame CSV MangoHud just wrote.

    MangoHud 0.8 writes a one-line ``<name>_summary.csv`` alongside the frame
    log, a moment after it, so "newest file" alone would pick the summary. Each
    run gets a fresh folder, so after excluding summaries this is unambiguous.
    """
    csvs = sorted(
        (p for p in folder.glob("*.csv") if not p.stem.endswith("_summary")),
        key=lambda p: p.stat().st_mtime,
    )
    return csvs[-1] if csvs else None


# --------------------------------------------------------------------------
# Launching
# --------------------------------------------------------------------------


def launch_steam_app(appid: int, launch_args: str = "") -> None:
    """Ask the running Steam client to start an app."""
    if not shutil.which("steam"):
        raise RunnerError("'steam' is not on PATH.")
    cmd = ["steam", "-applaunch", str(appid)]
    if launch_args:
        cmd.extend(launch_args.split())
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


@contextlib.contextmanager
def inhibit_sleep(why: str) -> Iterator[bool]:
    """Hold a logind idle/sleep inhibitor for the duration of the block.

    A machine that suspends an hour into an unattended session loses the run in
    progress and every target after it. ``systemd-inhibit`` holds the lock for
    as long as its child lives, so the child is a ``sleep`` we kill on exit.
    Yields False when no inhibitor could be taken.
    """
    if not shutil.which("systemd-inhibit"):
        yield False
        return
    try:
        holder = subprocess.Popen(
            [
                "systemd-inhibit",
                "--what=idle:sleep",
                "--who=cachybench",
                f"--why={why}",
                "--mode=block",
                "sleep",
                "infinity",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        yield False
        return
    try:
        time.sleep(0.5)
        yield holder.poll() is None
    finally:
        if holder.poll() is None:
            holder.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                holder.wait(timeout=5)


def restart_steam_with_mangohud(wait_s: int = 45) -> None:
    """Restart Steam with MANGOHUD=1 so every Vulkan game gets the overlay.

    This removes the need to add ``mangohud %command%`` to each game's launch
    options. OpenGL-only titles still need the wrapper.
    """
    if steam_running():
        subprocess.run(
            ["steam", "-shutdown"], check=False, capture_output=True, timeout=30
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and steam_running():
            time.sleep(1)

    env = dict(os.environ, MANGOHUD="1")
    subprocess.Popen(
        ["steam", "-silent"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if steam_running():
            time.sleep(10)  # let the client finish coming up before launching a game
            return
        time.sleep(2)
    raise RunnerError("Steam did not come back up within the timeout.")


# --------------------------------------------------------------------------
# Synthetic benchmarks
# --------------------------------------------------------------------------

# Each parser maps a stdout blob to named numeric results. Adding a benchmark is
# usually just a matter of adding a regex here, or setting `pattern` in the config.
SYNTHETIC_PARSERS: dict[str, dict[str, str]] = {
    "7zip": {"Total MIPS": r"^Tot:(?:\s+\d+)*\s+(\d+)\s*$"},
    "sysbench": {"Events/sec": r"events per second:\s*([\d.]+)"},
    "glmark2": {"Score": r"glmark2 Score:\s*(\d+)"},
    "vkmark": {"Score": r"vkmark Score:\s*(\d+)"},
    "unigine": {"Score": r"^\s*Score:\s*(\d+)", "Avg FPS": r"^\s*FPS:\s*([\d.]+)"},
    "geekbench": {
        "Single-Core": r"Single-Core Score[:\s]+(\d+)",
        "Multi-Core": r"Multi-Core Score[:\s]+(\d+)",
    },
    "stress-ng": {"Bogo ops/s": r"cpu\s+\d+\s+[\d.]+\s+[\d.]+\s+[\d.]+\s+([\d.]+)"},
    "y-cruncher": {"Seconds": r"Total Computation Time:\s*([\d.]+)"},
    "blender": {"Samples/min": r"([\d.]+)\s*samples?/min"},
}


@dataclass
class SyntheticResult:
    name: str
    metrics: dict[str, float]
    duration_s: float
    returncode: int
    stdout: str
    stderr: str


def parse_synthetic_output(text: str, parser: str, pattern: str = "") -> dict[str, float]:
    """Extract numbers from a benchmark's stdout.

    A custom ``pattern`` always wins. ``auto`` tries every built-in parser and
    keeps whatever matches, which covers the common tools without configuration.
    """
    results: dict[str, float] = {}

    if pattern:
        for match in re.finditer(pattern, text, re.MULTILINE):
            groups = match.groupdict()
            if groups:
                for key, value in groups.items():
                    with contextlib.suppress(TypeError, ValueError):
                        results[key] = float(value)
            elif match.groups():
                with contextlib.suppress(ValueError):
                    results["Score"] = float(match.group(1))
        return results

    candidates = (
        SYNTHETIC_PARSERS.values()
        if parser == "auto"
        else [SYNTHETIC_PARSERS.get(parser, {})]
    )
    for spec in candidates:
        for label, regex in spec.items():
            match = re.search(regex, text, re.MULTILINE)
            if match:
                with contextlib.suppress(ValueError):
                    results.setdefault(label, float(match.group(1)))
    return results


def run_synthetic(
    name: str,
    command: str,
    timeout_s: int,
    parser: str = "auto",
    pattern: str = "",
    cwd: str = "",
    env: dict[str, str] | None = None,
) -> SyntheticResult:
    """Run a command-line benchmark and scrape its score."""
    run_env = dict(os.environ)
    if env:
        run_env.update({k: str(v) for k, v in env.items()})

    # Use bash rather than /bin/sh so bashisms in a configured command work as
    # written; sh on some systems is dash, which would silently mangle them.
    shell_bin = shutil.which("bash")

    started = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            shell=True,
            executable=shell_bin,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=cwd or None,
            env=run_env,
        )
        stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        stderr += f"\n[cachybench] timed out after {timeout_s}s"
        code = -1

    duration = time.monotonic() - started
    metrics = parse_synthetic_output(stdout + "\n" + stderr, parser, pattern)
    return SyntheticResult(
        name=name,
        metrics=metrics,
        duration_s=round(duration, 2),
        returncode=code,
        stdout=stdout[-20000:],
        stderr=stderr[-20000:],
    )


# --------------------------------------------------------------------------
# Cooldown
# --------------------------------------------------------------------------


def gpu_temperature() -> float | None:
    """Current GPU temperature in Celsius, from sysfs or nvidia-smi."""
    for card in sorted(Path("/sys/class/drm").glob("card[0-9]*/device/hwmon/hwmon*")):
        temp_file = card / "temp1_input"
        if temp_file.exists():
            try:
                return int(temp_file.read_text().strip()) / 1000.0
            except (OSError, ValueError):
                continue

    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            ).stdout.strip()
            if out:
                return float(out.splitlines()[0])
        except (subprocess.SubprocessError, ValueError, OSError):
            pass
    return None


def cooldown(
    seconds: int,
    target_temp_c: int | None = None,
    max_extra_s: int = 300,
    on_tick: Callable[[str], None] | None = None,
) -> None:
    """Idle between runs so thermals do not drift across the session.

    Without this, run 3 is measured on a hotter card than run 1 and the numbers
    trend downward for reasons that have nothing to do with what changed.
    """
    for remaining in range(seconds, 0, -1):
        if on_tick:
            on_tick(f"cooldown {remaining}s")
        time.sleep(1)

    if target_temp_c is None:
        return

    waited = 0
    while waited < max_extra_s:
        temp = gpu_temperature()
        if temp is None or temp <= target_temp_c:
            return
        if on_tick:
            on_tick(f"waiting for GPU {temp:.0f}C -> {target_temp_c}C")
        time.sleep(5)
        waited += 5
