"""Load and validate the TOML benchmark definitions."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


class ConfigError(RuntimeError):
    pass


@dataclass
class Defaults:
    runs: int = 3
    cooldown_s: int = 60
    cooldown_temp_c: int | None = None
    launch_timeout_s: int = 300
    run_timeout_s: int = 1800
    warmup_s: int = 30
    log_duration_s: int = 90
    trim_start_s: float = 0.0
    trim_end_s: float = 0.0
    results_dir: str = "./results"
    mangohud_extra: str = ""


@dataclass
class GameSettings:
    """Graphics settings written into the game's own config file before launch.

    ``handler`` selects the title-specific file format (see ``gamesettings``),
    ``values`` maps option names to the wanted values, ``file`` overrides the
    auto-detected path inside the Proton prefix, and ``strict`` aborts the
    target instead of warning when a value does not survive the first run.
    """

    handler: str
    values: dict[str, Any] = field(default_factory=dict)
    file: str = ""
    strict: bool = False


@dataclass
class GameTarget:
    name: str
    appid: int
    mode: str = "timed"  # "timed" | "builtin" | "manual"
    launch_args: str = ""
    runs: int | None = None
    warmup_s: int | None = None
    log_duration_s: int | None = None
    run_timeout_s: int | None = None
    launch_timeout_s: int | None = None
    cooldown_s: int | None = None
    trim_start_s: float | None = None
    trim_end_s: float | None = None
    process_hint: str = ""
    settings: GameSettings | None = None
    notes: str = ""
    enabled: bool = True
    kind: str = "game"

    def resolve(self, defaults: Defaults, key: str) -> Any:
        value = getattr(self, key, None)
        return getattr(defaults, key) if value is None else value


@dataclass
class SyntheticTarget:
    name: str
    command: str
    parser: str = "auto"
    pattern: str = ""
    unit: str = "score"
    higher_is_better: bool = True
    runs: int | None = None
    run_timeout_s: int | None = None
    cooldown_s: int | None = None
    cwd: str = ""
    env: dict[str, str] = field(default_factory=dict)
    notes: str = ""
    enabled: bool = True
    kind: str = "synthetic"

    def resolve(self, defaults: Defaults, key: str) -> Any:
        value = getattr(self, key, None)
        return getattr(defaults, key) if value is None else value


@dataclass
class Config:
    defaults: Defaults
    games: list[GameTarget]
    synthetics: list[SyntheticTarget]
    path: Path

    @property
    def targets(self) -> list[GameTarget | SyntheticTarget]:
        return [*self.games, *self.synthetics]

    def enabled_targets(self, only: list[str] | None = None) -> list[Any]:
        targets = [t for t in self.targets if t.enabled]
        if only:
            wanted = [o.lower() for o in only]
            targets = [
                t
                for t in targets
                if any(w in t.name.lower() for w in wanted)
            ]
            if not targets:
                raise ConfigError(
                    f"No enabled target matched {only!r}. Run 'cachybench list' to see names."
                )
        return targets


VALID_GAME_MODES = {"timed", "builtin", "manual"}


def _known_fields(cls: type) -> set[str]:
    return set(cls.__dataclass_fields__)  # type: ignore[attr-defined]


def _build(cls: type, data: dict[str, Any], label: str) -> Any:
    allowed = _known_fields(cls) - {"kind"}
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(
            f"{label}: unknown option(s) {sorted(unknown)}. Allowed: {sorted(allowed)}"
        )
    return cls(**data)


def _build_settings(data: Any, label: str) -> GameSettings:
    if not isinstance(data, dict):
        raise ConfigError(f"{label}: must be a table with at least 'handler' and 'values'")
    if "handler" not in data:
        raise ConfigError(f"{label}: 'handler' is required (e.g. \"cyberpunk2077\")")
    values = data.get("values", {})
    if not isinstance(values, dict) or not values:
        raise ConfigError(f"{label}: 'values' must be a non-empty table of option = value")
    for key, value in values.items():
        if isinstance(value, (dict, list)):
            raise ConfigError(f"{label}: '{key}' must be a plain value, not a table or array")
    return _build(GameSettings, data, label)


def load(path: str | Path) -> Config:
    path = Path(path).expanduser()
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")

    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    defaults = _build(Defaults, raw.get("defaults", {}), "[defaults]")

    games: list[GameTarget] = []
    for i, entry in enumerate(raw.get("game", [])):
        label = f"[[game]] #{i + 1} ({entry.get('name', 'unnamed')})"
        if "name" not in entry or "appid" not in entry:
            raise ConfigError(f"{label}: 'name' and 'appid' are required")
        settings_raw = entry.pop("settings", None)
        game = _build(GameTarget, entry, label)
        if settings_raw is not None:
            game.settings = _build_settings(settings_raw, f"{label} [game.settings]")
        if game.mode not in VALID_GAME_MODES:
            raise ConfigError(
                f"{label}: mode '{game.mode}' invalid; use one of {sorted(VALID_GAME_MODES)}"
            )
        games.append(game)

    synthetics: list[SyntheticTarget] = []
    for i, entry in enumerate(raw.get("synthetic", [])):
        label = f"[[synthetic]] #{i + 1} ({entry.get('name', 'unnamed')})"
        if "name" not in entry or "command" not in entry:
            raise ConfigError(f"{label}: 'name' and 'command' are required")
        synthetics.append(_build(SyntheticTarget, entry, label))

    names = [t.name for t in (*games, *synthetics)]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise ConfigError(f"Duplicate target names: {sorted(duplicates)}")

    if not names:
        raise ConfigError(f"{path} defines no [[game]] or [[synthetic]] targets")

    # Absolute, because the path is handed to MangoHud, which resolves relative
    # paths against the *game's* working directory rather than ours.
    defaults.results_dir = os.path.abspath(os.path.expanduser(defaults.results_dir))
    return Config(defaults=defaults, games=games, synthetics=synthetics, path=path)
