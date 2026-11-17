"""Magnet heating, estimated live from the Kepco monitors (no Qt code).

Copper's resistance rises about 0.39 % per °C, so R = V/I at the Kepco monitors is a thermometer for the
coil. A single reading is biased by the coil's inductance: while |I| is rising the current lags the
voltage and V/I reads high, while it falls V/I reads low (±2-3 % on P-MOKE's iron-core magnet at 30 ms
settle). Over a whole cycle the two cancel, so the estimate is a trimmed mean over one cycle's points
that carry enough current (the trim also drops a bad monitor capture).
"""
import math
from collections import deque
from dataclasses import dataclass

COPPER_TCR_PER_C = 0.00393     # fractional resistance change per °C, copper near room temperature
MIN_WINDOW = 15                # points in a sweep's running estimate, at least
TRIM = 0.1                     # fraction dropped at each end before averaging
MIN_CURRENT_FRACTION = 0.3     # of the run's peak current: lower currents give too noisy an R


@dataclass
class HeatState:
    r_ohm: float = math.nan         # running estimate of V/I (NaN until a point has enough current)
    rise: float = math.nan          # fractional rise over the cold R
    peak_rise: float = math.nan     # highest rise seen in this run
    level: str = "none"             # none | ok | warm | hot

    @property
    def delta_c(self) -> float:
        """Estimated temperature rise of the coil over the cold reading (°C)."""
        return self.rise / COPPER_TCR_PER_C

    @property
    def peak_delta_c(self) -> float:
        return self.peak_rise / COPPER_TCR_PER_C


def trimmed_mean(values) -> float:
    v = sorted(values)
    k = max(1, int(TRIM * len(v))) if len(v) >= 5 else 0
    v = v[k:len(v) - k]
    return sum(v) / len(v)


class MagnetHeat:
    def __init__(self, r_cold_ohm: float, min_current_A: float, warm_frac: float, hot_frac: float,
                 window: int = MIN_WINDOW):
        """r_cold_ohm: the coil's resistance when cold (diagnostics, else the setup's nominal R).
        window: points averaged. Sweeps: one cycle (at least MIN_WINDOW), so rising and falling currents
        cancel; a steady DC current (manual control) has no inductive bias and needs only a few."""
        self.r_cold = r_cold_ohm
        self.min_current_A = min_current_A
        self.warm_frac = warm_frac
        self.hot_frac = hot_frac
        self._r: deque[float] = deque(maxlen=max(3, window))
        self.state = HeatState()

    def add(self, volts: float, amps: float) -> HeatState:
        """One monitor reading; returns the updated estimate (unchanged when the current is too low)."""
        if not (math.isfinite(volts) and math.isfinite(amps)) or abs(amps) < self.min_current_A:
            return self.state
        self._r.append(volts / amps)
        r = trimmed_mean(self._r)
        rise = r / self.r_cold - 1
        peak = rise if math.isnan(self.state.peak_rise) else max(self.state.peak_rise, rise)
        level = "hot" if rise > self.hot_frac else ("warm" if rise > self.warm_frac else "ok")
        self.state = HeatState(r, rise, peak, level)
        return self.state
