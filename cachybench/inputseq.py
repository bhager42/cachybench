"""Scripted keyboard input, for benchmarks that live behind a menu.

Most AAA benchmarks have no command-line entry point, but their menus are the
same on every launch. ``input_sequence`` describes the path through them as a
list of steps, and the session replays it through ``ydotool`` after each
launch:

    input_sequence = [
        "wait 45",          # seconds; covers loading and intro videos
        "key enter",        # a key, or a combination such as "key alt+enter"
        "key down x3",      # the same key three times
        "wait 2",
        "log",              # press MangoHud's Shift+F2 to start the capture
    ]

``ydotool`` writes to ``/dev/uinput``, so the game sees an ordinary keyboard.
That works on Wayland, X11 and under Proton alike, but keys go to whichever
window has focus.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class SequenceError(ValueError):
    pass


# Linux input event codes (linux/input-event-codes.h). ydotool 1.x takes these
# rather than key names.
KEYCODES: dict[str, int] = {
    "esc": 1, "escape": 1,
    **{str(n): 1 + n for n in range(1, 10)}, "0": 11,
    "minus": 12, "equal": 13, "backspace": 14, "tab": 15,
    **dict(zip("qwertyuiop", range(16, 26))),
    "leftbrace": 26, "rightbrace": 27, "enter": 28, "return": 28,
    "ctrl": 29, "leftctrl": 29,
    **dict(zip("asdfghjkl", range(30, 39))),
    "semicolon": 39, "apostrophe": 40, "grave": 41,
    "shift": 42, "leftshift": 42, "backslash": 43,
    **dict(zip("zxcvbnm", range(44, 51))),
    "comma": 51, "dot": 52, "slash": 53, "rightshift": 54,
    "alt": 56, "leftalt": 56, "space": 57, "capslock": 58,
    **{f"f{n}": 58 + n for n in range(1, 11)}, "f11": 87, "f12": 88,
    "rightctrl": 97, "rightalt": 100,
    "home": 102, "up": 103, "pageup": 104, "left": 105, "right": 106,
    "end": 107, "down": 108, "pagedown": 109, "insert": 110, "delete": 111,
    "super": 125, "leftmeta": 125,
}

# MangoHud's default toggle_logging keybind is Shift_L+F2.
LOG_COMBO = ("leftshift", "f2")

# Games poll input once a frame; a press shorter than a frame or two can be
# missed entirely, and menus ignore input while their transitions play.
HOLD_S = 0.08
REPEAT_GAP_S = 0.35


@dataclass(frozen=True)
class Step:
    kind: str  # "wait" | "key" | "log"
    seconds: float = 0.0
    keys: tuple[str, ...] = ()
    count: int = 1

    def describe(self) -> str:
        if self.kind == "wait":
            return f"wait {self.seconds:g}s"
        if self.kind == "log":
            return "start logging (Shift+F2)"
        combo = "+".join(self.keys)
        return combo if self.count == 1 else f"{combo} x{self.count}"


_KEY_RE = re.compile(r"^key\s+(\S+)(?:\s+x(\d+))?$")


def parse(steps: list[str]) -> list[Step]:
    """Turn config strings into steps, rejecting anything unrecognised."""
    if not isinstance(steps, list) or not all(isinstance(s, str) for s in steps):
        raise SequenceError("input_sequence must be a list of strings")

    parsed: list[Step] = []
    for number, raw in enumerate(steps, 1):
        text = " ".join(raw.lower().split())
        where = f"input_sequence step {number} ({raw!r})"

        if text == "log":
            parsed.append(Step("log"))
            continue

        if text.startswith("wait"):
            try:
                seconds = float(text.split(maxsplit=1)[1])
            except (IndexError, ValueError):
                raise SequenceError(f"{where}: use 'wait <seconds>'") from None
            if seconds < 0:
                raise SequenceError(f"{where}: wait cannot be negative")
            parsed.append(Step("wait", seconds=seconds))
            continue

        match = _KEY_RE.match(text)
        if not match:
            raise SequenceError(
                f"{where}: expected 'wait <seconds>', 'key <name>[+<name>] [x<count>]' or 'log'"
            )
        keys = tuple(match.group(1).split("+"))
        unknown = [k for k in keys if k not in KEYCODES]
        if unknown:
            raise SequenceError(
                f"{where}: unknown key(s) {unknown}. Known: {', '.join(sorted(KEYCODES))}"
            )
        count = int(match.group(2) or 1)
        if count < 1:
            raise SequenceError(f"{where}: count must be at least 1")
        parsed.append(Step("key", keys=keys, count=count))

    return parsed


def total_wait(steps: list[Step]) -> float:
    """Roughly how long the sequence takes to play, for the time estimate."""
    seconds = 0.0
    for step in steps:
        if step.kind == "wait":
            seconds += step.seconds
        elif step.kind == "key":
            seconds += step.count * (HOLD_S + REPEAT_GAP_S)
        else:
            seconds += HOLD_S + REPEAT_GAP_S
    return seconds


# --------------------------------------------------------------------------
# Sending keys
# --------------------------------------------------------------------------


def ydotool_socket() -> Path | None:
    """Where ydotoold is listening, or None if it does not appear to be running."""
    candidates = []
    if os.environ.get("YDOTOOL_SOCKET"):
        candidates.append(Path(os.environ["YDOTOOL_SOCKET"]))
    if os.environ.get("XDG_RUNTIME_DIR"):
        candidates.append(Path(os.environ["XDG_RUNTIME_DIR"]) / ".ydotool_socket")
    candidates.append(Path("/tmp/.ydotool_socket"))
    for candidate in candidates:
        if candidate.is_socket():
            return candidate
    return None


def ydotool_key(events: list[str]) -> None:
    """Send raw ``code:state`` events, e.g. ``["42:1", "60:1"]``."""
    if not shutil.which("ydotool"):
        raise SequenceError("'ydotool' is not on PATH — sudo pacman -S ydotool")
    env = dict(os.environ)
    socket = ydotool_socket()
    if socket is not None:
        env["YDOTOOL_SOCKET"] = str(socket)
    proc = subprocess.run(
        ["ydotool", "key", *events], capture_output=True, text=True, timeout=10, env=env
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise SequenceError(
            "ydotool failed"
            + (f": {detail[-1]}" if detail else "")
            + " — is ydotoold running? systemctl --user enable --now ydotool"
        )


def press(keys: tuple[str, ...], send: Callable[[list[str]], None] = ydotool_key) -> None:
    """Hold a combination briefly, always releasing what was pressed."""
    codes = [KEYCODES[k] for k in keys]
    send([f"{c}:1" for c in codes])
    try:
        time.sleep(HOLD_S)
    finally:
        # Release in reverse so modifiers lift last, as a person would.
        send([f"{c}:0" for c in reversed(codes)])


def play(
    steps: list[Step],
    alive: Callable[[], bool],
    send: Callable[[list[str]], None] = ydotool_key,
    on_step: Callable[[str], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Replay a sequence into the focused window.

    Stops with SequenceError if the game goes away part-way, so a crash during
    loading does not turn into keypresses landing on the desktop.
    """
    for number, step in enumerate(steps, 1):
        if on_step:
            on_step(f"[{number}/{len(steps)}] {step.describe()}")
        if step.kind == "wait":
            # Sleep in short slices so a crash is noticed during long waits.
            left = step.seconds
            while left > 0:
                chunk = min(left, 2.0)
                sleep(chunk)
                left -= chunk
                if not alive():
                    raise SequenceError(f"game exited during step {number} ({step.describe()})")
            continue

        if not alive():
            raise SequenceError(f"game exited before step {number} ({step.describe()})")
        keys = LOG_COMBO if step.kind == "log" else step.keys
        for i in range(step.count):
            press(keys, send)
            if i < step.count - 1:
                sleep(REPEAT_GAP_S)
        sleep(REPEAT_GAP_S)
