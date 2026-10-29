# cachybench

Automated CPU and GPU benchmarking for CachyOS. Runs Steam games and
command-line benchmarks in sequence, captures frame timing and hardware
telemetry with MangoHud, and writes an Excel workbook with a tab per game and
native charts.

One hard dependency (`openpyxl`); everything else is the standard library.

```
cachybench doctor       # check the machine is ready
cachybench list-games   # find Steam appids
cachybench run          # go make coffee
cachybench settings Cyberpunk   # list the options a game's settings file exposes
```

---

## Install

```bash
sudo pacman -S mangohud lib32-mangohud python-openpyxl
# optional synthetic benchmarks used by the sample config
sudo pacman -S p7zip sysbench stress-ng glmark2 vkmark

git clone <wherever you put this> ~/cachybench && cd ~/cachybench
./cachybench.py doctor
```

`doctor` checks dependencies, finds your Steam libraries, verifies each
configured appid is actually installed, and flags anything that will make
results noisy (wrong CPU governor, high background load, battery power).

For a `cachybench` command on your PATH:

```bash
mkdir -p ~/.local/bin && ln -s ~/cachybench/cachybench.py ~/.local/bin/cachybench
```

---

## Making MangoHud attach to your games

This is the one thing worth understanding before you start, because it is the
only part that can silently produce empty results.

`steam -applaunch` does not spawn the game as a child process — it hands the
request to the running Steam client over IPC, and Steam spawns the game itself.
Environment variables set by this script therefore cannot reach the game. Pick
one of these:

**Option A — restart Steam with the overlay enabled (recommended)**

```bash
cachybench run --restart-steam
```

This shuts Steam down and brings it back with `MANGOHUD=1` in its environment,
which every child process inherits. That covers all Vulkan titles, which
includes essentially everything running under Proton (DXVK and VKD3D both
present as Vulkan). Do it once per boot.

**Option B — per-game launch options**

In Steam, right-click the game → Properties → Launch Options:

```
mangohud %command%
```

More typing, but it is the only thing that works for OpenGL-only titles, and it
survives Steam restarts.

Either way, the script owns `~/.config/MangoHud/MangoHud.conf` while it runs. It
backs up your existing file, rewrites it before each run to point logging at
that run's output folder, and restores your original afterwards — including if
you Ctrl-C or the script crashes.

---

## The three run modes

`mode` on each `[[game]]` decides how much you have to do.

| Mode | What happens | Hands-off? |
|---|---|---|
| `builtin` | Game is launched with a benchmark flag, runs, and exits on its own. The script waits for the process to disappear. | Fully |
| `timed` | Script logs a fixed window, then closes the game. | Fully |
| `manual` | Script launches the game; you navigate to the benchmark and press **Shift+F2**; logging stops itself after `log_duration_s` and the script closes the game. | One keypress per run |

Being straight about this: **most AAA games with a built-in benchmark are
`manual`.** Cyberpunk 2077, Shadow of the Tomb Raider, Hitman and the rest put
their benchmark behind a settings menu with no command-line entry point, and no
script can navigate a menu reliably. What you get is still a large improvement —
launching, timing, cooldown, log collection, statistics and reporting are all
automated, and your involvement is one keypress instead of a stopwatch and a
spreadsheet.

If you want a session you can genuinely walk away from, build it out of
`synthetic` entries and any `builtin` games you have. Those need nothing at all.

Before setting `launch_args` for a `builtin` entry, check the game on
[PCGamingWiki](https://www.pcgamingwiki.com/) and confirm the flag works by
running it by hand once. A wrong flag usually just launches the game normally,
which looks like a hang.

---

## Configuration

Everything lives in `benchmarks.toml`. `[defaults]` applies to every target;
any target can override any of them.

```toml
[defaults]
runs = 3               # repeats per target
cooldown_s = 60        # idle between runs
cooldown_temp_c = 50   # also wait for the GPU to drop below this
warmup_s = 30          # discarded before logging: shader compilation, fade-ins
log_duration_s = 90    # length of the measured window
launch_timeout_s = 300 # raise this if a first Proton launch times out
results_dir = "./results"

[[game]]
name = "Cyberpunk 2077"
appid = 1091500
mode = "manual"
log_duration_s = 120

[[synthetic]]
name = "7-Zip Compression"
command = "7z b -mmt$(nproc)"
parser = "7zip"
```

Run `cachybench list-games --filter tomb` to find appids.

**Synthetic parsers.** Built in: `7zip`, `sysbench`, `glmark2`, `vkmark`,
`unigine`, `geekbench`, `stress-ng`, `y-cruncher`, `blender`. `parser = "auto"`
tries all of them. For anything else, give a regex — a named group becomes a
column:

```toml
pattern = "Result:\\s+(?P<Throughput>[\\d.]+)\\s+MB/s"
```

Commands run under bash, so `$(nproc)` and friends work. Anything not installed
is skipped with a warning rather than aborting the session.

### Game settings

A `[game.settings]` block writes graphics options into the game's own settings
file before every launch, so each run is measured under exactly the
configuration the config declares rather than whatever was left in the menu
last time. Your original file is backed up and restored when the target
finishes, including on Ctrl-C.

```toml
[[game]]
name = "Cyberpunk 2077"
appid = 1091500
mode = "manual"

[game.settings]
handler = "cyberpunk2077"
strict = false          # true: abandon the target if the game rejects a value

[game.settings.values]
WindowMode = "Fullscreen"
Resolution = "3440x1440"
VSync = "Off"
RayTracing = false
TextureQuality = "High"
RayTracedLighting = "Psycho"
```

Option names and allowed values are whatever the game's file uses, and they
change between versions and machines. List them straight from your install:

```bash
cachybench settings Cyberpunk                       # everything
cachybench settings Cyberpunk --filter /graphics    # one section
```

Use the bare name, or `group/Name` if the same name appears in more than one
group. Case does not matter for names or list values; `on`/`off` work for
booleans. Anything that is not a value the file offers is rejected before the
first launch, and `doctor` runs the same check.

**Games validate settings against the hardware on start-up** and quietly fall
back — a resolution the display cannot do, a ray-tracing mode the GPU lacks.
Resolutions, window modes and upscaler modes are built at runtime, so the file
lists no candidates for them and nothing can be checked in advance. After every
run the file is re-read and anything the game reset is reported, recorded in
`session.json`, and called out on the game's sheet in the workbook. With
`strict = true` the target is abandoned instead.

**Cyberpunk 2077** keeps its file at
`compatdata/1091500/pfx/drive_c/users/steamuser/AppData/Local/CD Projekt Red/Cyberpunk 2077/UserSettings.json`.
The file only exists after the game has been launched once. Note that VSync
stores a localisation key rather than a word; `VSync = "Off"` and
`VSync = "On"` are translated for you. The benchmark itself still has to be
started from the menu, so the entry stays `manual`.

To compare two configurations in one session, add two `[[game]]` entries with
the same appid, different names, and different values.

**Adding a game.** Each title is a small handler in `cachybench/gamesettings.py`
that knows where its file lives in the Proton prefix, how to read it into a
list of options, and how to write changed values back without disturbing the
rest. Subclass `Handler`, implement `default_path`, `read` and `write`, and add
an instance to `HANDLERS`.

**Cooldown matters more than it looks.** Without it, run 3 is measured on a
hotter card than run 1 and your numbers trend downward for reasons that have
nothing to do with what you are testing. `cooldown_temp_c` additionally waits
for the GPU to actually reach a temperature rather than trusting a fixed delay.

---

## Output

```
results/2026-08-30_142211/
├── session.json                       # everything, machine-readable
├── benchmarks_2026-08-30_142211.xlsx  # the report
├── Cyberpunk_2077/run1/*.csv          # raw MangoHud logs, per run
└── 7-Zip_Compression/run1.stdout.txt  # raw benchmark output
```

The workbook has:

- **Summary** — one row per target with medians across runs, a comparison bar
  chart, and a note explaining what each metric means.
- **Environment** — kernel, scheduler, governor, driver versions, GPU power
  state, Proton builds. Compare this between two workbooks before you compare
  any scores.
- **One sheet per game** — every run as a row, a median row driven by real
  `MEDIAN()` formulas, a bar chart of the FPS figures, and a per-second FPS
  timeline with a line chart.
- **Synthetics** — scores from the command-line benchmarks.

Charts are native Excel charts pointed at real cells, so they update if you edit
or extend the data. Nothing is a pasted image.

Reporting is deliberately separate from running. Results are flushed to disk
after every single run, so an interrupted six-hour session still produces a
workbook, and you can rebuild one at any time:

```bash
cachybench report results/2026-08-30_142211
```

---

## About the numbers

**Average FPS** is derived from mean frametime (`1000 / mean_ms`), not by
averaging MangoHud's per-frame FPS column. Averaging FPS over-weights fast
frames: two frames at 1 ms and 4 ms average to 2.5 ms, which is 400 FPS, but
averaging 1000 and 250 FPS gives 625.

**1% Low / 0.1% Low** are the average FPS across the slowest 1% and 0.1% of
frames. These describe how bad the rough moments get and generally matter more
than the average for how a game feels. The 0.1% figure needs at least 1000
frames to mean anything, so keep `log_duration_s` reasonably long.

**Run Spread %** is the gap between your best and worst run as a share of the
mean. Above roughly 3%, run-to-run noise is large enough that small differences
between configurations are not real, and you need more runs or a quieter system.

Some conventions report the 1st-percentile FPS instead; both are in the workbook
(`1% Low FPS` and `1st %ile FPS`) so you can match whatever you are comparing
against.

---

## Getting clean results

```bash
sudo cpupower frequency-set -g performance     # CachyOS often defaults elsewhere
```

Run under `systemd-inhibit` so nothing sleeps or blanks mid-session:

```bash
systemd-inhibit --what=idle:sleep --why="benchmarking" cachybench run
```

Also worth doing: close browsers and chat apps, disable compositor vsync if your
FPS looks pinned to your refresh rate, and prefer exclusive fullscreen over
borderless. `doctor` warns about most of these.

---

## Troubleshooting

**"MangoHud produced no log"** — the overlay is not attaching. Confirm with
`MANGOHUD=1 mangohud vkcube` (you should see the overlay), then use
`--restart-steam` or set `mangohud %command%` in the game's launch options.

**"Game did not start within Ns"** — first Proton launches install runtimes and
compile shaders, which can take a long time. Raise `launch_timeout_s`, and run
the game once by hand first so the shader cache is warm.

**Game launches but the script never notices it** — the process detection
matches on the install directory in the executable path and command line, which
covers native and Proton games. Odd launchers can defeat it. Set
`process_hint = "game.exe"` to match on process name instead; find the name with
`ps -e -o comm` while the game runs.

**Wrong or missing telemetry columns** — MangoHud only logs the columns whose
HUD parameters are enabled, and only reads sensors your driver exposes. Columns
that are entirely zero are dropped from the report rather than shown as a 0 W
GPU. AMD generally exposes more than NVIDIA.

**Fewer frames than expected** — the game probably exited or lost focus before
the logging window finished. Any run under 100 frames is flagged in the output.
