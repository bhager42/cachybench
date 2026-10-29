"""Parsing and statistics for MangoHud CSV logs.

MangoHud writes a CSV that looks like this (column set varies with which HUD
parameters are enabled, so nothing here may assume fixed positions):

    os,cpu,gpu,ram,kernel,driver,cpuscheduler
    Steam Runtime 3 (sniper),Intel Core i7-8700,NVIDIA RTX 2070 SUPER,24538512,6.18.18,,performance
    fps,frametime,cpu_load,gpu_load,cpu_temp,gpu_temp,gpu_core_clock,...,elapsed
    79.2173,12.6235,57.6412,82,63,72,1905,...,23216195460

Notes that drive the implementation:
  * ``frametime`` is milliseconds, ``elapsed`` is nanoseconds since MangoHud
    initialised (not since logging started), so it must be zero-referenced.
  * The per-row ``fps`` column is instantaneous (1000/frametime). Averaging it
    over-weights fast frames, so average FPS is derived from mean frametime.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Columns that are hardware telemetry rather than frame timing. Each is
# summarised as mean/max if MangoHud happened to log it.
TELEMETRY_COLUMNS = [
    "cpu_load",
    "gpu_load",
    "cpu_temp",
    "gpu_temp",
    "gpu_junction_temp",
    "cpu_power",
    "gpu_power",
    "cpu_mhz",
    "gpu_core_clock",
    "gpu_mem_clock",
    "gpu_vram_used",
    "ram_used",
    "swap_used",
    "process_rss",
]

# Human-readable labels and units for the summary tables.
METRIC_LABELS: dict[str, str] = {
    "frames": "Frames",
    "duration_s": "Duration (s)",
    "avg_fps": "Avg FPS",
    "fps_1_low": "1% Low FPS",
    "fps_0p1_low": "0.1% Low FPS",
    "fps_p1": "1st %ile FPS",
    "fps_p99": "99th %ile FPS",
    "fps_min": "Min FPS",
    "fps_max": "Max FPS",
    "frametime_avg_ms": "Avg Frametime (ms)",
    "frametime_p99_ms": "99th %ile Frametime (ms)",
    "frametime_max_ms": "Max Frametime (ms)",
    "stutter_pct": "Stutter (% frames)",
    "cpu_load_avg": "Avg CPU Load (%)",
    "cpu_load_max": "Max CPU Load (%)",
    "gpu_load_avg": "Avg GPU Load (%)",
    "gpu_load_max": "Max GPU Load (%)",
    "cpu_temp_avg": "Avg CPU Temp (C)",
    "cpu_temp_max": "Max CPU Temp (C)",
    "gpu_temp_avg": "Avg GPU Temp (C)",
    "gpu_temp_max": "Max GPU Temp (C)",
    "gpu_junction_temp_avg": "Avg GPU Hotspot (C)",
    "gpu_junction_temp_max": "Max GPU Hotspot (C)",
    "cpu_power_avg": "Avg CPU Power (W)",
    "cpu_power_max": "Max CPU Power (W)",
    "gpu_power_avg": "Avg GPU Power (W)",
    "gpu_power_max": "Max GPU Power (W)",
    "cpu_mhz_avg": "Avg CPU Clock (MHz)",
    "cpu_mhz_max": "Max CPU Clock (MHz)",
    "gpu_core_clock_avg": "Avg GPU Core Clock (MHz)",
    "gpu_core_clock_max": "Max GPU Core Clock (MHz)",
    "gpu_mem_clock_avg": "Avg GPU Mem Clock (MHz)",
    "gpu_mem_clock_max": "Max GPU Mem Clock (MHz)",
    "gpu_vram_used_avg": "Avg VRAM Used (GiB)",
    "gpu_vram_used_max": "Max VRAM Used (GiB)",
    "ram_used_avg": "Avg RAM Used (GiB)",
    "ram_used_max": "Max RAM Used (GiB)",
}

# Metrics where a bigger number is better. Used to colour the summary sheet.
HIGHER_IS_BETTER = {
    "avg_fps",
    "fps_1_low",
    "fps_0p1_low",
    "fps_p1",
    "fps_p99",
    "fps_min",
    "fps_max",
}


class LogParseError(RuntimeError):
    """Raised when a file does not look like a MangoHud log."""


@dataclass
class FrameLog:
    """A parsed MangoHud CSV."""

    path: Path
    sysinfo: dict[str, str] = field(default_factory=dict)
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, float]] = field(default_factory=list)

    @property
    def frametimes(self) -> list[float]:
        return [r["frametime"] for r in self.rows if r.get("frametime")]

    @property
    def timeline_s(self) -> list[float]:
        """Elapsed seconds, zero-referenced to the first logged frame."""
        if "elapsed" not in self.columns or not self.rows:
            # Fall back to a cumulative sum of frametimes.
            out, total = [], 0.0
            for r in self.rows:
                total += r.get("frametime", 0.0) / 1000.0
                out.append(total)
            return out
        base = self.rows[0]["elapsed"]
        return [(r["elapsed"] - base) / 1e9 for r in self.rows]


def _to_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_mangohud_csv(path: str | Path) -> FrameLog:
    """Read a MangoHud log into a FrameLog.

    The header block is optional and its width varies, so the data header is
    located by looking for the row whose first field is ``fps`` rather than by
    assuming a line number.
    """
    path = Path(path)
    with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
        raw = list(csv.reader(fh))

    if not raw:
        raise LogParseError(f"{path} is empty")

    header_idx = None
    for i, row in enumerate(raw[:10]):
        if row and row[0].strip() == "fps":
            header_idx = i
            break
    if header_idx is None:
        raise LogParseError(f"{path}: no 'fps' header row found; not a MangoHud log?")

    sysinfo: dict[str, str] = {}
    if header_idx >= 2:
        keys, values = raw[header_idx - 2], raw[header_idx - 1]
        sysinfo = {
            k.strip(): v.strip()
            for k, v in zip(keys, values)
            if k.strip() and v.strip()
        }

    columns = [c.strip() for c in raw[header_idx]]
    rows: list[dict[str, float]] = []
    for record in raw[header_idx + 1 :]:
        if not record or len(record) < 2:
            continue
        parsed: dict[str, float] = {}
        for name, cell in zip(columns, record):
            val = _to_float(cell)
            if val is not None:
                parsed[name] = val
        # A row is only usable if it carries a real frametime.
        if parsed.get("frametime", 0.0) > 0:
            rows.append(parsed)

    return FrameLog(path=path, sysinfo=sysinfo, columns=columns, rows=rows)


def percentile(sorted_values: list[float], p: float) -> float | None:
    """Linear-interpolated percentile of an already-sorted list. ``p`` is 0-100."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * (p / 100.0)
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return sorted_values[int(k)]
    return sorted_values[lo] + (k - lo) * (sorted_values[hi] - sorted_values[lo])


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def summarize(log: FrameLog, trim_start_s: float = 0.0, trim_end_s: float = 0.0) -> dict[str, Any]:
    """Reduce a FrameLog to a flat dict of metrics.

    ``trim_start_s`` / ``trim_end_s`` discard frames at each end of the capture,
    which is useful for cutting menu transitions or a benchmark's fade-out even
    when MangoHud's own ``autostart_log`` delay was already applied.
    """
    times = log.timeline_s
    rows = log.rows
    if trim_start_s or trim_end_s:
        if times:
            end = times[-1] - trim_end_s
            keep = [
                i
                for i, t in enumerate(times)
                if t >= trim_start_s and (trim_end_s <= 0 or t <= end)
            ]
            rows = [rows[i] for i in keep]
            times = [times[i] for i in keep]

    frametimes = [r["frametime"] for r in rows if r.get("frametime", 0) > 0]
    if not frametimes:
        return {"frames": 0, "duration_s": 0.0}

    n = len(frametimes)
    ordered = sorted(frametimes)  # ascending: fast frames first, slow frames last
    mean_ft = sum(frametimes) / n
    duration = sum(frametimes) / 1000.0

    def low_of_worst(fraction: float) -> float | None:
        """Average FPS across the slowest ``fraction`` of frames (CapFrameX style)."""
        count = int(n * fraction)
        if count < 1:
            return None
        worst = ordered[-count:]
        return 1000.0 / (sum(worst) / count)

    median_ft = percentile(ordered, 50) or mean_ft
    stutter_frames = sum(1 for ft in frametimes if ft > 2 * median_ft)

    stats: dict[str, Any] = {
        "frames": n,
        "duration_s": round(duration, 2),
        "avg_fps": round(1000.0 / mean_ft, 2),
        "fps_1_low": _round(low_of_worst(0.01)),
        "fps_0p1_low": _round(low_of_worst(0.001)),
        "fps_p1": _round(1000.0 / v if (v := percentile(ordered, 99)) else None),
        "fps_p99": _round(1000.0 / v if (v := percentile(ordered, 1)) else None),
        "fps_min": round(1000.0 / ordered[-1], 2),
        "fps_max": round(1000.0 / ordered[0], 2),
        "frametime_avg_ms": round(mean_ft, 3),
        "frametime_p99_ms": _round(percentile(ordered, 99), 3),
        "frametime_max_ms": round(ordered[-1], 3),
        "stutter_pct": round(100.0 * stutter_frames / n, 3),
    }

    for col in TELEMETRY_COLUMNS:
        values = [r[col] for r in rows if col in r]
        # MangoHud emits a constant 0 for sensors it cannot read; skip those so
        # the report does not imply a 0 W GPU.
        if not values or all(v == 0 for v in values):
            continue
        stats[f"{col}_avg"] = _round(_mean(values))
        stats[f"{col}_max"] = _round(max(values))

    return stats


def _round(value: float | None, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None else None


def per_second_series(log: FrameLog, trim_start_s: float = 0.0) -> list[tuple[int, float]]:
    """Bin frames into one-second buckets of average FPS.

    Raw per-frame data is far too dense to chart (a 60 s capture at 144 FPS is
    ~8600 points), and Excel line charts get unreadable well before that. One
    point per second keeps the shape of the run while staying legible.
    """
    times = log.timeline_s
    buckets: dict[int, list[float]] = {}
    for t, row in zip(times, log.rows):
        if t < trim_start_s:
            continue
        ft = row.get("frametime", 0.0)
        if ft > 0:
            buckets.setdefault(int(t - trim_start_s), []).append(ft)

    series = []
    for second in sorted(buckets):
        fts = buckets[second]
        series.append((second, round(1000.0 / (sum(fts) / len(fts)), 2)))
    return series


def aggregate_runs(run_stats: list[dict[str, Any]]) -> dict[str, Any]:
    """Median across runs, plus a spread figure for the headline metric.

    Median rather than mean because a single run spoiled by a background task or
    a shader-compilation hitch should not drag the result.
    """
    if not run_stats:
        return {}
    keys = {k for s in run_stats for k in s}
    agg: dict[str, Any] = {}
    for key in keys:
        values = [s[key] for s in run_stats if s.get(key) is not None]
        if not values:
            continue
        agg[key] = round(percentile(sorted(values), 50), 3)

    avg_fps = [s["avg_fps"] for s in run_stats if s.get("avg_fps")]
    if len(avg_fps) > 1:
        spread = max(avg_fps) - min(avg_fps)
        agg["run_spread_pct"] = round(100.0 * spread / (sum(avg_fps) / len(avg_fps)), 2)
    else:
        agg["run_spread_pct"] = 0.0
    agg["run_count"] = len(run_stats)
    return agg
