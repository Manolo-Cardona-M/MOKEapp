"""Magnet voltage waveforms. Every cycle starts at -amplitude (negative saturation), like the LabVIEW VI."""
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
