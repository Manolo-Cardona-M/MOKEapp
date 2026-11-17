"""Magnet voltage waveforms. Every cycle starts at -amplitude (negative saturation), like the LabVIEW VI."""
from dataclasses import dataclass

import numpy as np

WAVEFORMS = ("triangle", "sine", "square")


def make_waveform(kind: str, amplitude: float, points: int) -> np.ndarray:
    if kind not in WAVEFORMS:
        raise ValueError(f"Unknown waveform {kind!r}")
    if points < 4:
        raise ValueError("At least 4 points per cycle are needed")
    phase = np.arange(points) / points
    if kind == "triangle":
        shape = 1.0 - 4.0 * np.abs(phase - 0.5)
    elif kind == "sine":
        shape = -np.cos(2 * np.pi * phase)
    else:
        shape = np.where(phase < 0.5, -1.0, 1.0)
    return amplitude * shape


# ------------------------------------------------------------------ variable intervals
@dataclass
class Interval:
    """One stretch of the sweep, in magnet volts: `points` per branch from x_from to x_to (both ends
    included), each read after `settle_ms`. Consecutive intervals share their boundary point, which is read
    after the longer of their two settle times."""
    x_from: float
    x_to: float
    points: int
    settle_ms: float


def validate_intervals(intervals: list[Interval], max_volts: float | None = None) -> str:
    """'' if the intervals make a sweep, else why not (shown to the user)."""
    if not intervals:
        return "Add at least one interval."
    for n, iv in enumerate(intervals, 1):
        if iv.points < 2:
            return f"Interval {n}: at least 2 points."
        if iv.settle_ms < 0:
            return f"Interval {n}: the settle time cannot be negative."
        if not iv.x_to > iv.x_from:
            return f"Interval {n}: 'To' must be larger than 'From'."
        if n > 1 and abs(iv.x_from - intervals[n - 2].x_to) > 1e-9 * max(1.0, abs(iv.x_from)):
            return f"Interval {n} must start where interval {n - 1} ends."
    if max_volts is not None and max(abs(intervals[0].x_from), abs(intervals[-1].x_to)) > max_volts + 1e-9:
        return f"The sweep goes beyond ±{max_volts:.1f} V, what the Kepco and the DAQ can deliver."
    return ""


def make_interval_cycle(intervals: list[Interval]) -> tuple[np.ndarray, np.ndarray]:
    """(magnet volts, settle seconds) for one cycle: up through the intervals, then back down through the same
    points. Starts at the lowest point like make_waveform; turning points are not repeated, so cycles chain."""
    xs, settles = [], []
    for n, iv in enumerate(intervals):
        x = np.linspace(iv.x_from, iv.x_to, iv.points)
        if n:
            x = x[1:]               # shared boundary: already the last point of the previous interval,
            settles[-1][-1] = max(settles[-1][-1], iv.settle_ms / 1000.0)   # which gets the slower settle
        xs.append(x)
        settles.append(np.full(len(x), iv.settle_ms / 1000.0))
    up, s_up = np.concatenate(xs), np.concatenate(settles)
    return np.concatenate([up, up[-2:0:-1]]), np.concatenate([s_up, s_up[-2:0:-1]])
