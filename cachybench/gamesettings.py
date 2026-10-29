"""Write a game's graphics settings before launch so every run is measured
under the same, declared configuration.

Games keep their settings in wildly different places, so each supported title
has a small *handler* that knows where its settings file lives inside the Proton
prefix, how to parse it, and how to change an option without corrupting the
rest. Handlers are registered in ``HANDLERS`` by a short key that the TOML
``settings.handler`` field selects.

The lifecycle mirrors the MangoHud.conf handling in ``runners``: the user's own
file is backed up before the first run and restored when the target finishes,
including on Ctrl-C or a crash.

A game must be closed while its settings file is written. Most titles also
rewrite the file on exit and silently reset anything they consider invalid for
the hardware, so ``verify`` re-reads the file after each run and reports what
did not stick.
"""

from __future__ import annotations

import contextlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from . import runners


class SettingsError(RuntimeError):
    pass


@dataclass
class Option:
    """One configurable value as the game's file describes it."""

    name: str
    group: str
    value: Any
    kind: str = ""
    allowed: list[Any] = field(default_factory=list)
    min_value: Any = None
    max_value: Any = None
    # A list whose members the game only works out at runtime (resolutions the
    # display supports, upscalers the GPU offers). The file carries no
    # ``values`` for these, so validation has to wait for ``verify``.
    dynamic: bool = False

    @property
    def qualified(self) -> str:
        return f"{self.group}/{self.name}" if self.group else self.name


@dataclass
class Change:
    option: Option
    old: Any
    new: Any


# --------------------------------------------------------------------------
# Locating the Proton prefix
# --------------------------------------------------------------------------


def compatdata_dir(appid: int) -> Path | None:
    """The Proton prefix root for an appid, searched across every library.

    Steam normally keeps ``compatdata`` beside ``common`` in the library the
    game is installed in, but a game moved between drives can leave its prefix
    behind, so every library is checked.
    """
    for library in runners.steam_libraries():
        candidate = library / "compatdata" / str(appid)
        if (candidate / "pfx").is_dir():
            return candidate
    return None


def windows_local_appdata(appid: int) -> Path | None:
    prefix = compatdata_dir(appid)
    if prefix is None:
        return None
    return prefix / "pfx" / "drive_c" / "users" / "steamuser" / "AppData" / "Local"


# --------------------------------------------------------------------------
# Value coercion shared by handlers
# --------------------------------------------------------------------------


def coerce(option: Option, requested: Any) -> Any:
    """Turn a TOML value into what the game's file expects for this option.

    The match is deliberately forgiving on the way in (``"off"`` for a bool,
    ``"high"`` for ``"High"``) and strict on the way out: the returned value has
    the same Python type as the current one and, where the file lists allowed
    values, is one of them spelled exactly as the game spells it.
    """
    current = option.value
    label = option.qualified

    if isinstance(current, bool):
        if isinstance(requested, bool):
            value: Any = requested
        elif isinstance(requested, str) and requested.strip().lower() in ("true", "on", "yes", "1"):
            value = True
        elif isinstance(requested, str) and requested.strip().lower() in ("false", "off", "no", "0"):
            value = False
        elif isinstance(requested, (int, float)) and requested in (0, 1):
            value = bool(requested)
        else:
            raise SettingsError(f"{label}: expected true/false, got {requested!r}")
    elif isinstance(current, int) and not isinstance(current, bool):
        if isinstance(requested, bool) or not isinstance(requested, (int, float, str)):
            raise SettingsError(f"{label}: expected an integer, got {requested!r}")
        try:
            value = int(requested)
        except ValueError:
            raise SettingsError(f"{label}: expected an integer, got {requested!r}") from None
    elif isinstance(current, float):
        if isinstance(requested, bool) or not isinstance(requested, (int, float, str)):
            raise SettingsError(f"{label}: expected a number, got {requested!r}")
        try:
            value = float(requested)
        except ValueError:
            raise SettingsError(f"{label}: expected a number, got {requested!r}") from None
    elif isinstance(current, str):
        value = str(requested)
    else:
        # Lists or objects: pass through untouched and let the game judge.
        value = requested

    if option.allowed:
        if value in option.allowed:
            return value
        if isinstance(value, str):
            for candidate in option.allowed:
                if isinstance(candidate, str) and candidate.lower() == value.lower():
                    return candidate
        raise SettingsError(
            f"{label}: {value!r} is not one of the values the game offers on this "
            f"machine: {option.allowed}"
        )

    if option.min_value is not None and isinstance(value, (int, float)) and value < option.min_value:
        raise SettingsError(f"{label}: {value} is below the minimum {option.min_value}")
    if option.max_value is not None and isinstance(value, (int, float)) and value > option.max_value:
        raise SettingsError(f"{label}: {value} is above the maximum {option.max_value}")
    return value


def resolve_option(options: list[Option], key: str) -> Option:
    """Find an option by bare name or by ``group/Name`` path.

    A bare name that exists in more than one group is ambiguous, and the error
    lists the qualified alternatives so the config can be made precise.
    """
    key = key.strip().strip("/")
    if "/" in key:
        group, _, name = key.rpartition("/")
        group = "/" + group
        for option in options:
            if option.name == name and option.group == group:
                return option
        raise SettingsError(f"No option '{name}' in group '{group}'")

    matches = [o for o in options if o.name == key]
    if not matches:
        lowered = [o for o in options if o.name.lower() == key.lower()]
        if len(lowered) == 1:
            return lowered[0]
        raise SettingsError(
            f"No option named '{key}' in the settings file. Run "
            "'cachybench settings <target>' to list the names the game uses."
        )
    if len(matches) > 1:
        raise SettingsError(
            f"'{key}' exists in several groups; use one of: "
            + ", ".join(o.qualified for o in matches)
        )
    return matches[0]


# --------------------------------------------------------------------------
# Handler interface
# --------------------------------------------------------------------------


class Handler:
    """Base class. Subclasses implement the four file-format methods."""

    key = ""
    description = ""

    def default_path(self, appid: int) -> Path | None:
        raise NotImplementedError

    def read(self, path: Path) -> list[Option]:
        raise NotImplementedError

    def write(self, path: Path, changes: list[Change]) -> None:
        raise NotImplementedError

    def coerce(self, option: Option, requested: Any) -> Any:
        return coerce(option, requested)

    # Shared behaviour -----------------------------------------------------

    def plan(self, path: Path, values: dict[str, Any]) -> list[Change]:
        """Resolve and validate every requested value without touching the file."""
        options = self.read(path)
        changes: list[Change] = []
        seen: set[str] = set()
        for key, requested in values.items():
            option = resolve_option(options, key)
            if option.qualified in seen:
                raise SettingsError(f"{option.qualified} is set more than once")
            seen.add(option.qualified)
            changes.append(Change(option, option.value, self.coerce(option, requested)))
        return changes

    def apply(self, path: Path, values: dict[str, Any]) -> list[Change]:
        changes = self.plan(path, values)
        self.write(path, changes)
        return changes

    def verify(self, path: Path, values: dict[str, Any]) -> dict[str, tuple[Any, Any]]:
        """Return ``{qualified_name: (wanted, actual)}`` for anything that drifted."""
        drift: dict[str, tuple[Any, Any]] = {}
        for change in self.plan(path, values):
            if change.old != change.new:
                drift[change.option.qualified] = (change.new, change.old)
        return drift


# --------------------------------------------------------------------------
# Cyberpunk 2077
# --------------------------------------------------------------------------


class Cyberpunk2077(Handler):
    """``UserSettings.json`` as written by the 2.x game (file ``version`` 140).

    Shape::

        {"version": 140,
         "data": [{"group_name": "/video/display",
                   "options": [{"name": "Resolution", "type": "string_list",
                                "value": "3440x1440", "index": 23, "is_dynamic": true},
                               {"name": "TextureQuality", "type": "string_list",
                                "value": "High", "index": 2, "default_index": 2,
                                "is_dynamic": false, "values": ["Low", "Medium", "High"]},
                               {"name": "FieldOfView", "type": "float", "value": 80.0,
                                "min_value": 70.0, "max_value": 100.0, "step_value": 5.0},
                               {"name": "RayTracing", "type": "bool", "value": true}]}]}

    Static lists (``is_dynamic`` false) carry ``values`` and an ``index`` into
    them, and both are kept in step here. Dynamic lists — resolution, window
    mode, upscaler modes, VSync — are populated by the game from the hardware,
    so the file lists no candidates; the requested value is written as-is and
    ``verify`` reports afterwards if the game refused it. The game rewrites the
    whole file on exit, so everything except ``value``/``index`` is preserved.
    """

    key = "cyberpunk2077"
    description = "Cyberpunk 2077 (UserSettings.json in the Proton prefix)"
    appid = 1091500
    relative = Path("CD Projekt Red") / "Cyberpunk 2077" / "UserSettings.json"

    def default_path(self, appid: int) -> Path | None:
        local = windows_local_appdata(appid)
        return None if local is None else local / self.relative

    def _load(self, path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise SettingsError(
                f"{path} does not exist. The game writes it on first launch — run "
                "the game once by hand, change any graphics option, and quit."
            ) from None
        except json.JSONDecodeError as exc:
            raise SettingsError(f"{path} is not valid JSON: {exc}") from None

    @staticmethod
    def _walk(document: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
        # ``data``/``group_name`` is what 2.x writes; ``groups``/``name`` is
        # accepted too in case an older or future build uses it.
        groups = document.get("data")
        if not isinstance(groups, list):
            groups = document.get("groups", [])
        for group in groups or []:
            if not isinstance(group, dict):
                continue
            group_path = str(group.get("group_name") or group.get("name") or "")
            for raw in group.get("options", []) or []:
                if isinstance(raw, dict) and "name" in raw:
                    yield group_path, raw

    def read(self, path: Path) -> list[Option]:
        document = self._load(path)
        options: list[Option] = []
        for group_path, raw in self._walk(document):
            kind = str(raw.get("type", ""))
            options.append(
                Option(
                    name=str(raw["name"]),
                    group=group_path,
                    value=raw.get("value"),
                    kind=kind,
                    allowed=list(raw.get("values", []) or []),
                    min_value=raw.get("min_value"),
                    max_value=raw.get("max_value"),
                    dynamic=bool(raw.get("is_dynamic")) or (kind.endswith("_list") and "values" not in raw),
                )
            )
        if not options:
            raise SettingsError(f"{path} contains no recognisable settings groups")
        return options

    def coerce(self, option: Option, requested: Any) -> Any:
        # Some dynamic lists store localisation keys rather than plain words:
        # VSync is "UI-Settings-Video-QualitySetting-Off". Let the config say
        # `VSync = "Off"` by grafting the requested word onto the current key's
        # prefix. verify() catches it if the guess was wrong.
        if (
            option.dynamic
            and isinstance(option.value, str)
            and isinstance(requested, (str, bool))
            and "-" in option.value
        ):
            word = {True: "On", False: "Off"}.get(requested, requested)  # type: ignore[arg-type]
            if isinstance(word, str) and "-" not in word:
                prefix = option.value.rsplit("-", 1)[0]
                return f"{prefix}-{word}"
        return coerce(option, requested)

    def write(self, path: Path, changes: list[Change]) -> None:
        document = self._load(path)
        wanted = {(c.option.group, c.option.name): c.new for c in changes}
        for group_path, raw in self._walk(document):
            key = (group_path, str(raw["name"]))
            if key not in wanted:
                continue
            new = wanted.pop(key)
            raw["value"] = new
            values = raw.get("values")
            if isinstance(values, list) and new in values:
                raw["index"] = values.index(new)
        if wanted:
            missing = ", ".join(f"{g}/{n}" for g, n in wanted)
            raise SettingsError(f"Options vanished between read and write: {missing}")
        # Write via a sibling temp file so a crash mid-write cannot leave the
        # game with a truncated document it would then discard.
        tmp = path.with_suffix(".json.cachybench-tmp")
        tmp.write_text(json.dumps(document, indent=4, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)


HANDLERS: dict[str, Handler] = {h.key: h for h in (Cyberpunk2077(),)}


def get_handler(key: str) -> Handler:
    try:
        return HANDLERS[key]
    except KeyError:
        raise SettingsError(
            f"Unknown settings handler '{key}'. Available: {sorted(HANDLERS)}"
        ) from None


def settings_path(handler: Handler, appid: int, override: str = "") -> Path:
    """Where the game's settings file is, honouring an explicit ``file`` override."""
    if override:
        return Path(override).expanduser()
    path = handler.default_path(appid)
    if path is None:
        raise SettingsError(
            f"No Proton prefix found for appid {appid} in any Steam library. The game "
            "has to have been launched once; or set `file` explicitly."
        )
    return path


# --------------------------------------------------------------------------
# Session lifecycle
# --------------------------------------------------------------------------


@dataclass
class ManagedSettings:
    handler: Handler
    path: Path
    values: dict[str, Any]

    def apply(self) -> list[Change]:
        return self.handler.apply(self.path, self.values)

    def verify(self) -> dict[str, tuple[Any, Any]]:
        return self.handler.verify(self.path, self.values)

    def summary(self) -> dict[str, Any]:
        """The requested values spelled the way the game spells them, for reports."""
        try:
            return {c.option.qualified: c.new for c in self.handler.plan(self.path, self.values)}
        except SettingsError:
            return dict(self.values)


@contextlib.contextmanager
def managed_settings(
    handler_key: str, appid: int, values: dict[str, Any], file_override: str = ""
) -> Iterator[ManagedSettings]:
    """Back up the game's settings file, yield a writer, restore on exit.

    Nothing is written on entry; the caller applies before each launch so a
    game that rewrites its file on exit is put back on spec every run.
    """
    handler = get_handler(handler_key)
    path = settings_path(handler, appid, file_override)
    if not path.is_file():
        raise SettingsError(
            f"Settings file not found: {path}\nRun the game once by hand so it creates "
            "the file, then try again."
        )

    backup = path.with_name(path.name + ".cachybench-backup")
    shutil.copy2(path, backup)
    try:
        yield ManagedSettings(handler, path, values)
    finally:
        try:
            shutil.copy2(backup, path)
            backup.unlink(missing_ok=True)
        except OSError:
            # Leave the backup in place so nothing is lost; the caller reports it.
            pass
