"""cachybench — hands-off CPU/GPU benchmark automation for CachyOS.

Run `cachybench --help` for usage.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import shutil
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import gamesettings, metrics, report, runners, sysinfo
from .config import Config, ConfigError, GameTarget, SyntheticTarget, load as load_config

VERSION = "1.0.0"

# ANSI codes, dropped when stdout is not a terminal so log files stay clean.
_TTY = sys.stdout.isatty()
BOLD = "\033[1m" if _TTY else ""
DIM = "\033[2m" if _TTY else ""
RED = "\033[31m" if _TTY else ""
GREEN = "\033[32m" if _TTY else ""
YELLOW = "\033[33m" if _TTY else ""
BLUE = "\033[34m" if _TTY else ""
RESET = "\033[0m" if _TTY else ""


def log(message: str, prefix: str = "•") -> None:
    print(f"{DIM}[{datetime.now():%H:%M:%S}]{RESET} {prefix} {message}", flush=True)


def status(message: str) -> None:
    """Overwrite a single line, for countdowns and polling."""
    if _TTY:
        print(f"\r\033[K{DIM}    {message}{RESET}", end="", flush=True)


def clear_status() -> None:
    if _TTY:
        print("\r\033[K", end="", flush=True)


def heading(text: str) -> None:
    print(f"\n{BOLD}{BLUE}{text}{RESET}")
    print(f"{DIM}{'─' * min(len(text), 70)}{RESET}", flush=True)


def warn(message: str) -> None:
    log(f"{YELLOW}{message}{RESET}", prefix="!")


def error(message: str) -> None:
    log(f"{RED}{message}{RESET}", prefix="✗")


def ok(message: str) -> None:
    log(f"{GREEN}{message}{RESET}", prefix="✓")


class Interrupted(Exception):
    pass


# --------------------------------------------------------------------------
# Session state
# --------------------------------------------------------------------------


class Session:
    """Accumulates results and flushes to disk after every run.

    Writing eagerly matters: a benchmark session can run for hours, and a crash
    or a Ctrl-C two hours in should not cost you the first ten targets.
    """

    def __init__(self, root: Path, config_path: Path) -> None:
        self.session_id = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        self.dir = root / self.session_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.data: dict[str, Any] = {
            "session_id": self.session_id,
            "cachybench_version": VERSION,
            "config_file": str(config_path),
            "started": datetime.now().isoformat(timespec="seconds"),
            "finished": None,
            "environment": sysinfo.collect(),
            "warnings": sysinfo.warnings(),
            "results": [],
        }
        self.flush()

    @property
    def manifest(self) -> Path:
        return self.dir / "session.json"

    def flush(self) -> None:
        self.manifest.write_text(
            json.dumps(self.data, indent=2, default=str), encoding="utf-8"
        )

    def add(self, result: dict[str, Any]) -> None:
        self.data["results"].append(result)
        self.flush()

    def finish(self) -> None:
        self.data["finished"] = datetime.now().isoformat(timespec="seconds")
        self.flush()


# --------------------------------------------------------------------------
# Log waiting
# --------------------------------------------------------------------------


def wait_for_log(folder: Path, timeout_s: int, stable_s: float = 3.0) -> Path | None:
    """Wait for MangoHud to finish writing a CSV into an empty folder.

    MangoHud buffers samples and flushes when logging stops, so the file
    appearing is close to the signal we want; the stability check guards against
    builds that stream the data out instead.
    """
    deadline = time.monotonic() + timeout_s
    last_size = -1
    stable_since = 0.0

    while time.monotonic() < deadline:
        candidate = runners.newest_log(folder)
        if candidate is not None:
            size = candidate.stat().st_size
            now = time.monotonic()
            if size == last_size and size > 0:
                if stable_since and now - stable_since >= stable_s:
                    return candidate
                if not stable_since:
                    stable_since = now
            else:
                last_size = size
                stable_since = 0.0
            status(f"capturing… {size / 1024:.0f} KiB written")
        else:
            status(f"waiting for log… {deadline - time.monotonic():.0f}s left")
        time.sleep(1)

    clear_status()
    return runners.newest_log(folder)


# --------------------------------------------------------------------------
# Game runs
# --------------------------------------------------------------------------


def run_game_target(
    target: GameTarget, cfg: Config, session: Session, write_conf: Any
) -> dict[str, Any]:
    heading(f"{target.name}  (appid {target.appid}, mode: {target.mode})")

    install_dir = runners.app_install_dir(target.appid)
    result: dict[str, Any] = {
        "kind": "game",
        "name": target.name,
        "appid": target.appid,
        "mode": target.mode,
        "notes": target.notes,
        "runs": [],
        "aggregate": {},
        "errors": [],
    }

    if install_dir is None:
        message = (
            f"App {target.appid} is not installed in any Steam library — skipping. "
            "Install it in Steam, or check the appid."
        )
        error(message)
        result["errors"].append(message)
        return result

    log(f"Install directory: {install_dir}")

    target_dir = session.dir / _slug(target.name)
    target_dir.mkdir(parents=True, exist_ok=True)

    with contextlib.ExitStack() as stack:
        managed: gamesettings.ManagedSettings | None = None
        if target.settings is not None:
            try:
                managed = stack.enter_context(
                    gamesettings.managed_settings(
                        target.settings.handler,
                        target.appid,
                        target.settings.values,
                        target.settings.file,
                    )
                )
                # Validate now so a typo aborts before the first launch, not after.
                planned = managed.handler.plan(managed.path, managed.values)
            except gamesettings.SettingsError as exc:
                error(f"Game settings: {exc}")
                result["errors"].append(f"settings: {exc}")
                return result
            log(f"Settings file: {managed.path}")
            result["settings"] = {
                "handler": target.settings.handler,
                "file": str(managed.path),
                "values": {c.option.qualified: c.new for c in planned},
                "drift": {},
            }
            for change in planned:
                marker = "" if change.old == change.new else f"  {DIM}(was {change.old!r}){RESET}"
                print(f"    {change.option.qualified} = {change.new!r}{marker}")

        _run_game_loop(target, cfg, session, write_conf, result, install_dir, target_dir, managed)

    result["aggregate"] = metrics.aggregate_runs([r["stats"] for r in result["runs"]])
    if result["aggregate"].get("run_spread_pct", 0) > 3:
        warn(
            f"Run-to-run spread is {result['aggregate']['run_spread_pct']}%. "
            "Results this noisy will not resolve small differences."
        )
    return result


def _run_game_loop(
    target: GameTarget,
    cfg: Config,
    session: Session,
    write_conf: Any,
    result: dict[str, Any],
    install_dir: Path,
    target_dir: Path,
    managed: gamesettings.ManagedSettings | None,
) -> None:
    total_runs = target.resolve(cfg.defaults, "runs")
    warmup = target.resolve(cfg.defaults, "warmup_s")
    duration = target.resolve(cfg.defaults, "log_duration_s")
    launch_timeout = target.resolve(cfg.defaults, "launch_timeout_s")
    run_timeout = target.resolve(cfg.defaults, "run_timeout_s")
    cool = target.resolve(cfg.defaults, "cooldown_s")
    trim_start = target.resolve(cfg.defaults, "trim_start_s")
    trim_end = target.resolve(cfg.defaults, "trim_end_s")

    for run_index in range(1, total_runs + 1):
        log(f"{BOLD}Run {run_index}/{total_runs}{RESET}")
        run_dir = target_dir / f"run{run_index}"
        if run_dir.exists():
            shutil.rmtree(run_dir)
        run_dir.mkdir(parents=True)

        # Manual mode leaves logging to the in-game keybind, because the delay
        # between launch and the benchmark actually starting is unpredictable.
        autostart = 0 if target.mode == "manual" else warmup
        write_conf(run_dir, autostart, duration, cfg.defaults.mangohud_extra)

        if managed is not None:
            try:
                managed.apply()
            except gamesettings.SettingsError as exc:
                error(f"Could not write game settings: {exc}")
                result["errors"].append(f"run {run_index}: settings: {exc}")
                break

        try:
            runners.launch_steam_app(target.appid, target.launch_args)
        except runners.RunnerError as exc:
            error(str(exc))
            result["errors"].append(str(exc))
            break

        try:
            log("Launching… waiting for the game process to appear")
            runners.wait_for_start(
                install_dir,
                launch_timeout,
                target.process_hint,
                on_tick=lambda left: status(f"waiting for launch… {left:.0f}s left"),
            )
            clear_status()
            ok("Game is running")

            if target.mode == "builtin":
                log(f"Waiting for the built-in benchmark to finish (timeout {run_timeout}s)")
                exited = runners.wait_for_exit(
                    install_dir,
                    run_timeout,
                    target.process_hint,
                    on_tick=lambda left: status(f"benchmark running… {left:.0f}s left"),
                )
                clear_status()
                if not exited:
                    warn("Timed out waiting for exit; closing the game")
                    runners.terminate_game(install_dir, target.process_hint)
                log_path = runners.newest_log(run_dir)

            elif target.mode == "manual":
                print(
                    f"\n  {YELLOW}Manual step:{RESET} start the in-game benchmark, then press "
                    f"{BOLD}Shift+F2{RESET} to begin logging.\n"
                    f"  Logging stops automatically after {duration}s."
                )
                log_path = wait_for_log(run_dir, run_timeout)
                clear_status()
                runners.terminate_game(install_dir, target.process_hint)

            else:  # timed
                budget = warmup + duration + 120
                log(f"Capturing {duration}s after a {warmup}s warm-up")
                log_path = wait_for_log(run_dir, min(budget, run_timeout))
                clear_status()
                runners.terminate_game(install_dir, target.process_hint)

        except runners.RunnerError as exc:
            clear_status()
            error(str(exc))
            result["errors"].append(f"run {run_index}: {exc}")
            runners.terminate_game(install_dir, target.process_hint)
            continue
        except KeyboardInterrupt:
            clear_status()
            runners.terminate_game(install_dir, target.process_hint)
            raise Interrupted from None

        if managed is not None and not _settings_held(managed, target, run_index, result):
            break

        if log_path is None or not log_path.exists():
            message = (
                f"run {run_index}: MangoHud produced no log. Confirm the overlay is "
                "actually attaching — see 'cachybench doctor'."
            )
            error(message)
            result["errors"].append(message)
        else:
            try:
                frame_log = metrics.parse_mangohud_csv(log_path)
                stats = metrics.summarize(frame_log, trim_start, trim_end)
                if stats.get("frames", 0) < 100:
                    warn(f"Only {stats.get('frames', 0)} frames captured — treat this run with suspicion")
                result["runs"].append(
                    {
                        "index": run_index,
                        "log": str(log_path.relative_to(session.dir)),
                        "recorded": datetime.now().isoformat(timespec="seconds"),
                        "stats": stats,
                        "timeline": metrics.per_second_series(frame_log, trim_start),
                    }
                )
                ok(
                    f"{stats['frames']} frames · avg {stats['avg_fps']} FPS · "
                    f"1% low {stats.get('fps_1_low')} · {stats['duration_s']}s"
                )
            except metrics.LogParseError as exc:
                error(str(exc))
                result["errors"].append(f"run {run_index}: {exc}")

        session.flush()

        if run_index < total_runs and cool:
            runners.cooldown(cool, cfg.defaults.cooldown_temp_c, on_tick=status)
            clear_status()


def _settings_held(
    managed: gamesettings.ManagedSettings, target: GameTarget, run_index: int, result: dict[str, Any]
) -> bool:
    """Re-read the game's file after it closed and report anything it reset.

    Games validate settings against the hardware on start-up and quietly fall
    back — a resolution the display cannot do, a ray-tracing mode the GPU lacks.
    The run was then measured under different settings than the config claims,
    which is exactly the kind of thing a benchmark must not hide.
    """
    try:
        drift = managed.verify()
    except gamesettings.SettingsError as exc:
        warn(f"Could not re-read game settings after the run: {exc}")
        return True
    if not drift:
        return True

    strict = target.settings is not None and target.settings.strict
    for name, (wanted, actual) in drift.items():
        message = f"run {run_index}: game reset {name} from {wanted!r} to {actual!r}"
        (error if strict else warn)(message)
        result["errors"].append(message)
        result.setdefault("settings", {}).setdefault("drift", {})[name] = {
            "wanted": wanted,
            "actual": actual,
        }
    if strict:
        error("settings.strict is set — abandoning this target")
        return False
    warn("The values above were not in effect during the run; results are tagged in the report.")
    return True


def run_synthetic_target(
    target: SyntheticTarget, cfg: Config, session: Session
) -> dict[str, Any]:
    heading(f"{target.name}  (synthetic)")

    result: dict[str, Any] = {
        "kind": "synthetic",
        "name": target.name,
        "command": target.command,
        "notes": target.notes,
        "runs": [],
        "errors": [],
    }

    binary = target.command.strip().split()[0]
    if not shutil.which(binary) and not Path(binary).exists():
        message = f"'{binary}' not found on PATH — skipping {target.name}"
        error(message)
        result["errors"].append(message)
        return result

    total_runs = target.resolve(cfg.defaults, "runs")
    timeout = target.resolve(cfg.defaults, "run_timeout_s")
    cool = target.resolve(cfg.defaults, "cooldown_s")

    target_dir = session.dir / _slug(target.name)
    target_dir.mkdir(parents=True, exist_ok=True)

    for run_index in range(1, total_runs + 1):
        log(f"{BOLD}Run {run_index}/{total_runs}{RESET}: {target.command}")
        outcome = runners.run_synthetic(
            target.name,
            target.command,
            timeout,
            target.parser,
            target.pattern,
            target.cwd,
            target.env,
        )
        (target_dir / f"run{run_index}.stdout.txt").write_text(
            outcome.stdout + "\n--- stderr ---\n" + outcome.stderr, encoding="utf-8"
        )

        if outcome.returncode != 0:
            warn(f"Exited with code {outcome.returncode}")
        if outcome.metrics:
            ok(
                " · ".join(f"{k}: {v:g}" for k, v in outcome.metrics.items())
                + f" ({outcome.duration_s}s)"
            )
        else:
            warn(
                "No score parsed. Set `parser` or a custom `pattern` for this "
                f"benchmark; raw output saved to {target_dir / f'run{run_index}.stdout.txt'}"
            )

        result["runs"].append(
            {
                "index": run_index,
                "metrics": outcome.metrics,
                "duration_s": outcome.duration_s,
                "returncode": outcome.returncode,
            }
        )
        session.flush()

        if run_index < total_runs and cool:
            runners.cooldown(min(cool, 30), on_tick=status)
            clear_status()

    return result


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name).strip("_")


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    targets = cfg.enabled_targets(args.only)

    if args.runs:
        for target in targets:
            target.runs = args.runs

    heading("Plan")
    total_seconds = 0
    for target in targets:
        runs = target.resolve(cfg.defaults, "runs")
        if isinstance(target, GameTarget):
            per_run = (
                target.resolve(cfg.defaults, "warmup_s")
                + target.resolve(cfg.defaults, "log_duration_s")
                + 90
            )
            detail = f"{target.mode}, appid {target.appid}"
        else:
            per_run = 120
            detail = target.command[:50]
        estimate = runs * (per_run + target.resolve(cfg.defaults, "cooldown_s"))
        total_seconds += estimate
        print(f"  {target.name:<34} {runs} run(s)  ~{estimate // 60}m   {DIM}{detail}{RESET}")
    print(f"\n  {BOLD}Estimated total: ~{total_seconds // 60} minutes{RESET}")

    for issue in sysinfo.warnings():
        warn(issue)

    if args.dry_run:
        log("Dry run — nothing executed.")
        return 0

    if any(isinstance(t, GameTarget) for t in targets):
        if not runners.steam_running():
            error("Steam is not running. Start it (and log in) first.")
            return 1
        if args.restart_steam:
            log("Restarting Steam with MANGOHUD=1 so the overlay attaches to Vulkan titles")
            runners.restart_steam_with_mangohud()
            ok("Steam is back up")

    session = Session(Path(cfg.defaults.results_dir), cfg.path)
    log(f"Session directory: {session.dir}")

    interrupted = False
    with runners.managed_mangohud_conf() as write_conf:
        try:
            for target in targets:
                if isinstance(target, GameTarget):
                    session.add(run_game_target(target, cfg, session, write_conf))
                else:
                    session.add(run_synthetic_target(target, cfg, session))
        except (KeyboardInterrupt, Interrupted):
            clear_status()
            interrupted = True
            warn("Interrupted — writing a report from what completed")

    session.finish()

    heading("Report")
    out = args.output or (session.dir / f"benchmarks_{session.session_id}.xlsx")
    try:
        path = report.build_workbook(session.data, out)
        ok(f"Workbook: {path}")
        ok(f"Raw logs: {session.dir}")
    except Exception as exc:  # noqa: BLE001 - never lose the raw data over a report bug
        error(f"Failed to build the workbook: {exc}")
        log(f"Raw results are intact. Retry with: cachybench report {session.dir}")
        return 1

    if shutil.which("notify-send"):
        import subprocess

        subprocess.run(
            ["notify-send", "cachybench", f"Benchmark session finished\n{path}"],
            check=False,
        )

    return 1 if interrupted else 0


def cmd_report(args: argparse.Namespace) -> int:
    session_dir = Path(args.session_dir)
    if not session_dir.is_dir():
        error(f"Not a directory: {session_dir}")
        return 1
    try:
        path = report.build_from_session_dir(session_dir, args.output)
    except FileNotFoundError as exc:
        error(str(exc))
        return 1
    ok(f"Workbook: {path}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    heading("Configured targets")
    for target in cfg.targets:
        mark = f"{GREEN}on {RESET}" if target.enabled else f"{DIM}off{RESET}"
        kind = target.kind
        detail = (
            f"appid {target.appid}, mode {target.mode}"
            if isinstance(target, GameTarget)
            else target.command[:60]
        )
        print(f"  [{mark}] {target.name:<34} {DIM}{kind:<10}{detail}{RESET}")
    return 0


def cmd_list_games(args: argparse.Namespace) -> int:
    apps = runners.installed_apps()
    if not apps:
        error("No Steam libraries found. Is Steam installed under this user?")
        return 1
    heading(f"Installed Steam apps ({len(apps)})")
    needle = (args.filter or "").lower()
    for appid, name in apps:
        if needle and needle not in name.lower():
            continue
        print(f"  {appid:<10} {name}")
    print(f"\n{DIM}Copy an appid into your config as `appid = <number>`.{RESET}")
    return 0


def cmd_settings(args: argparse.Namespace) -> int:
    """List the options a game's settings file exposes, with current values.

    This is how you find the exact names and allowed values to put under
    ``[game.settings.values]`` — they vary by title, version and hardware.
    """
    cfg = load_config(args.config)
    matches = [g for g in cfg.games if args.target.lower() in g.name.lower()]
    if len(matches) != 1:
        names = ", ".join(g.name for g in matches) or "none"
        error(f"'{args.target}' must match exactly one [[game]]; matched: {names}")
        return 1
    game = matches[0]

    handler_key = args.handler or (game.settings.handler if game.settings else "")
    if not handler_key:
        error(
            f"{game.name} has no [game.settings] block. Pass --handler, e.g. "
            f"--handler {next(iter(gamesettings.HANDLERS))}"
        )
        return 1

    try:
        handler = gamesettings.get_handler(handler_key)
        path = gamesettings.settings_path(
            handler, game.appid, args.file or (game.settings.file if game.settings else "")
        )
        options = handler.read(path)
    except gamesettings.SettingsError as exc:
        error(str(exc))
        return 1

    heading(f"{game.name} — {path}")
    needle = (args.filter or "").lower()
    configured = {}
    if game.settings:
        try:
            configured = {
                c.option.qualified: c.new for c in handler.plan(path, game.settings.values)
            }
        except gamesettings.SettingsError as exc:
            warn(f"Configured values do not validate: {exc}")

    current_group = None
    shown = 0
    for option in options:
        if needle and needle not in option.qualified.lower():
            continue
        if option.group != current_group:
            current_group = option.group
            print(f"\n  {BOLD}{option.group}{RESET}")
        wanted = configured.get(option.qualified)
        mark = ""
        if wanted is not None:
            mark = (
                f"  {GREEN}← configured{RESET}"
                if wanted == option.value
                else f"  {YELLOW}← configured {wanted!r}{RESET}"
            )
        detail = ""
        if option.allowed:
            detail = f"  {DIM}one of {option.allowed}{RESET}"
        elif option.min_value is not None or option.max_value is not None:
            detail = f"  {DIM}{option.min_value}..{option.max_value}{RESET}"
        print(f"    {option.name:<34} = {option.value!r}{mark}{detail}")
        shown += 1

    if not shown:
        warn(f"No options matched '{args.filter}'")
    print(
        f"\n{DIM}Use the bare name under [game.settings.values], or group/Name if a "
        f"name appears in more than one group.{RESET}"
    )
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    heading("Environment check")
    problems = 0

    def check(label: str, passed: bool, detail: str = "", fatal: bool = True) -> None:
        nonlocal problems
        if passed:
            print(f"  {GREEN}✓{RESET} {label}{f' {DIM}({detail}){RESET}' if detail else ''}")
        else:
            mark = f"{RED}✗{RESET}" if fatal else f"{YELLOW}!{RESET}"
            print(f"  {mark} {label}{f' — {detail}' if detail else ''}")
            if fatal:
                problems += 1

    check("Python 3.11+", sys.version_info >= (3, 11), f"found {sys.version.split()[0]}")
    try:
        import openpyxl

        check("openpyxl", True, openpyxl.__version__)
    except ImportError:
        check("openpyxl", False, "pip install --user openpyxl")

    check("mangohud on PATH", bool(shutil.which("mangohud")), "sudo pacman -S mangohud")
    check("steam on PATH", bool(shutil.which("steam")), "sudo pacman -S steam")
    check("Steam is running", runners.steam_running(), "start Steam and log in")

    libraries = runners.steam_libraries()
    check("Steam libraries found", bool(libraries), f"{len(libraries)} found")
    for library in libraries:
        print(f"      {DIM}{library}{RESET}")

    check(
        "MangoHud config is writable",
        _writable(runners.MANGOHUD_CONF.parent),
        str(runners.MANGOHUD_CONF.parent),
    )
    check(
        "GPU temperature readable",
        runners.gpu_temperature() is not None,
        f"{runners.gpu_temperature()}C" if runners.gpu_temperature() else "cooldown_temp_c will be ignored",
        fatal=False,
    )
    check(
        "lm_sensors present",
        bool(shutil.which("sensors")),
        "optional: sudo pacman -S lm_sensors",
        fatal=False,
    )

    if args.config and Path(args.config).exists():
        try:
            cfg = load_config(args.config)
            check("Config parses", True, f"{len(cfg.targets)} target(s)")
            for game in cfg.games:
                if not game.enabled:
                    continue
                install = runners.app_install_dir(game.appid)
                check(
                    f"  {game.name}",
                    install is not None,
                    str(install) if install else f"appid {game.appid} not installed",
                    fatal=False,
                )
                if game.settings is not None:
                    passed, detail = _check_settings(game)
                    check(f"    settings ({game.settings.handler})", passed, detail, fatal=False)
        except ConfigError as exc:
            check("Config parses", False, str(exc))

    heading("System state")
    for key, value in sysinfo.collect().items():
        print(f"  {key:<26} {value}")

    issues = sysinfo.warnings()
    if issues:
        heading("Warnings")
        for issue in issues:
            print(f"  {YELLOW}!{RESET} {issue}")

    print()
    if problems:
        error(f"{problems} blocking problem(s). Fix these before running.")
        return 1
    ok("Ready to benchmark.")
    return 0


def _check_settings(game: GameTarget) -> tuple[bool, str]:
    """Resolve the settings file and validate every configured value, writing nothing."""
    assert game.settings is not None
    try:
        handler = gamesettings.get_handler(game.settings.handler)
        path = gamesettings.settings_path(handler, game.appid, game.settings.file)
        if not path.is_file():
            return False, f"{path} missing — launch the game once by hand"
        planned = handler.plan(path, game.settings.values)
    except gamesettings.SettingsError as exc:
        return False, str(exc)
    return True, f"{len(planned)} value(s) valid"


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".cachybench-write-test"
        probe.write_text("x")
        probe.unlink()
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cachybench",
        description="Automated CPU/GPU benchmarking for CachyOS, with Excel output.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  cachybench doctor                     check the machine is ready\n"
            "  cachybench list-games --filter cyber  find a Steam appid\n"
            "  cachybench run --dry-run              show the plan and time estimate\n"
            "  cachybench run                        run everything enabled\n"
            "  cachybench run --only 'Tomb Raider'   run one target\n"
            "  cachybench settings Cyberpunk         list a game's settings options\n"
            "  cachybench report results/2026-08-30_142211\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"cachybench {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="execute the benchmark session")
    run.add_argument("-c", "--config", default="benchmarks.toml")
    run.add_argument("-o", "--output", help="workbook path (default: inside the session dir)")
    run.add_argument("--only", nargs="+", metavar="NAME", help="substring match on target names")
    run.add_argument("--runs", type=int, help="override the run count for every target")
    run.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    run.add_argument(
        "--restart-steam",
        action="store_true",
        help="restart Steam with MANGOHUD=1 so Vulkan titles get the overlay automatically",
    )
    run.set_defaults(func=cmd_run)

    rep = sub.add_parser("report", help="rebuild a workbook from a session directory")
    rep.add_argument("session_dir")
    rep.add_argument("-o", "--output")
    rep.set_defaults(func=cmd_report)

    lst = sub.add_parser("list", help="show configured targets")
    lst.add_argument("-c", "--config", default="benchmarks.toml")
    lst.set_defaults(func=cmd_list)

    games = sub.add_parser("list-games", help="show installed Steam apps and their appids")
    games.add_argument("--filter", help="substring match on the game name")
    games.set_defaults(func=cmd_list_games)

    settings = sub.add_parser(
        "settings", help="list the options in a game's settings file and their current values"
    )
    settings.add_argument("target", help="substring match on one [[game]] name")
    settings.add_argument("-c", "--config", default="benchmarks.toml")
    settings.add_argument("--filter", help="substring match on option names")
    settings.add_argument(
        "--handler",
        choices=sorted(gamesettings.HANDLERS),
        help="file format to use when the game has no [game.settings] block",
    )
    settings.add_argument("--file", help="explicit path to the settings file")
    settings.set_defaults(func=cmd_settings)

    doc = sub.add_parser("doctor", help="check dependencies and system readiness")
    doc.add_argument("-c", "--config", default="benchmarks.toml")
    doc.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        error(str(exc))
        return 2
    except KeyboardInterrupt:
        clear_status()
        warn("Cancelled.")
        return 130
    except BrokenPipeError:
        # Output was piped into something that exited early (`| head`). Detach
        # stdout so the interpreter does not report the same error again at exit.
        import os

        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


if __name__ == "__main__":
    sys.exit(main())
