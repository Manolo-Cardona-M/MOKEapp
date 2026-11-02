"""Loop processing: averaging, drift correction, normalization, coercive field."""
from dataclasses import dataclass

import numpy as np


DRIFT_METHODS = {"off": "Off", "endpoints": "Close each cycle", "plateaus": "Saturation plateaus (robust)"}
PLATEAU_FRACTION = 0.75     # |x| beyond 75 % of the sweep counts as saturated
MIN_PLATEAU_POINTS = 3
OUTLIER_MADS = 4.0          # plateau points further than 4 robust sigmas from the fit are ignored
LOOP_SNR = 5.0              # a Kerr step below 5× the plateau noise is not a loop (laser off, no sample...)


def drift_correct(signal: np.ndarray) -> np.ndarray:
    """Subtract a linear ramp so the cycle ends where it started (LabVIEW 'Ramp by Samples' fix).

    A cycle starts and ends at the same field, so any start/end difference is laser drift.
    """
    signal = np.asarray(signal, dtype=float)
    return signal - np.linspace(0.0, signal[-1] - signal[0], len(signal))


@dataclass
class DriftFit:
    corrected: np.ndarray
    drift: float            # signal change over the cycle due to drift (V)
    step: float | None      # saturation-to-saturation Kerr step (V), if the plateaus were found
    robust: bool            # False = not enough saturated points: fell back to 'close each cycle'
    noise: float = 0.0      # robust scatter of the plateau points around the fit (V)

    @property
    def has_loop(self) -> bool:
        """The Kerr step stands clearly above the noise (else 'step' and the drift ratio mean nothing)."""
        return self.robust and self.step is not None and abs(self.step) > LOOP_SNR * self.noise


def drift_fit_plateaus(signal, x, t=None) -> DriftFit:
    """Linear drift in time, fitted on the saturated parts of the loop only.

    In saturation the magnetization does not change, so the signal there should only depend on which
    side we are on (and on a small linear-in-field background, e.g. Faraday rotation in the optics).
    Fitting  y = a_high·[x high] + a_low·[x low] + c·x + b·t  on those points gives the drift rate b
    from many points spread over the whole cycle, instead of only the first and last point, so noise
    or a jump at the start of the cycle no longer tilts the whole loop.
    """
    y = np.asarray(signal, dtype=float)
    x = np.asarray(x, dtype=float)
    t = np.arange(len(y), dtype=float) if t is None else np.asarray(t, dtype=float)
    xc = x - (x.max() + x.min()) / 2
    half = (x.max() - x.min()) / 2
    high = xc >= PLATEAU_FRACTION * half
    low = xc <= -PLATEAU_FRACTION * half
    if half > 0 and high.sum() >= MIN_PLATEAU_POINTS and low.sum() >= MIN_PLATEAU_POINTS:
        sel = high | low
        design = np.column_stack([high, low, x, t - t[0]]).astype(float)
        keep = sel.copy()
        rank = 0
        for _ in range(3):      # refit without outliers (spikes, a jump when the run starts)
            coef, _, rank, _ = np.linalg.lstsq(design[keep], y[keep], rcond=None)
            resid = y - design @ coef
            mad = np.median(np.abs(resid[keep] - np.median(resid[keep])))
            new_keep = sel & (np.abs(resid) <= max(OUTLIER_MADS * 1.4826 * mad, 1e-12))
            if new_keep.sum() < 2 * MIN_PLATEAU_POINTS or (new_keep == keep).all():
                break
            keep = new_keep
        if rank == 4:
            rate = coef[3]
            corrected = y - rate * (t - t[0])
            step = float(np.median(corrected[high]) - np.median(corrected[low]))
            r = resid[keep]
            noise = float(1.4826 * np.median(np.abs(r - np.median(r))))
            return DriftFit(corrected, float(rate * (t[-1] - t[0])), step, True, noise)
    return DriftFit(drift_correct(y), float(y[-1] - y[0]), None, False)


def correct_drift(method: str, signal, x=None, t=None) -> np.ndarray:
    """Apply one of DRIFT_METHODS ('off', 'endpoints', 'plateaus') to one cycle."""
    if method in (True, "endpoints"):
        return drift_correct(signal)
    if method == "plateaus":
        return drift_fit_plateaus(signal, x, t).corrected
    return np.asarray(signal, dtype=float)


@dataclass
class StabilityReport:
    duration_s: float
    mean_V: float
    drift_V_per_min: float      # slope of a straight-line fit
    noise_V: float              # point-to-point (short-term) noise, rms
    peak_to_peak_V: float

    @property
    def drift_pct_per_min(self) -> float:
        return self.drift_V_per_min / abs(self.mean_V) * 100 if self.mean_V else float("nan")

    def summary(self) -> str:
        return (f"{self.duration_s / 60:.1f} min, mean {self.mean_V:.5f} V, drift {self.drift_V_per_min * 1e3:+.3f} mV/min "
                f"({self.drift_pct_per_min:+.3f} %/min), noise {self.noise_V * 1e3:.3f} mV rms, "
                f"peak-to-peak {self.peak_to_peak_V * 1e3:.3f} mV")


def stability_report(t, signal) -> StabilityReport:
    """Signal recorded with the magnet at 0 V: how much the laser/optics drift and how noisy they are."""
    t = np.asarray(t, dtype=float)
    s = np.asarray(signal, dtype=float)
    slope = np.polyfit(t, s, 1)[0] if len(s) > 2 and np.ptp(t) > 0 else 0.0
    noise = float(np.std(np.diff(s)) / np.sqrt(2)) if len(s) > 2 else 0.0   # not affected by slow drift
    return StabilityReport(float(np.ptp(t)), float(np.mean(s)), float(slope * 60), noise, float(np.ptp(s)))


def average_cycles(cycles: list[np.ndarray]) -> np.ndarray:
    return np.mean(np.vstack(cycles), axis=0)


def remove_offset(y: np.ndarray) -> np.ndarray:
    y = np.asarray(y, dtype=float)
    return y - (np.max(y) + np.min(y)) / 2


def normalize(y: np.ndarray) -> np.ndarray:
    """Scale to -1..+1 using robust saturation levels (2nd/98th percentiles)."""
    y = np.asarray(y, dtype=float)
    lo, hi = np.percentile(y, [2, 98])
    if hi == lo:
        return np.zeros_like(y)
    return (y - (hi + lo) / 2) / ((hi - lo) / 2)


def sweep_direction(x: np.ndarray, v_programmed: np.ndarray | None = None) -> np.ndarray:
    """+1 where x is increasing, -1 where decreasing, 0 at turning points.

    The programmed voltage is noise-free, so it gives the direction when available
    (sign-corrected because the Kepco can invert).
    """
    x = np.asarray(x, dtype=float)
    if v_programmed is None:
        ref = x
        sign = 1.0
    else:
        ref = np.asarray(v_programmed, dtype=float)
        c = np.corrcoef(ref, x)[0, 1] if np.std(ref) > 0 and np.std(x) > 0 else 1.0
        sign = 1.0 if c >= 0 else -1.0
    return np.sign(np.gradient(ref)) * sign


def _crossings(x: np.ndarray, y: np.ndarray, level: float) -> list[float]:
    out = []
    d = y - level
    for k in range(len(d) - 1):
        if d[k] == 0:
            out.append(float(x[k]))
        elif d[k] * d[k + 1] < 0:
            out.append(float(x[k] - d[k] * (x[k + 1] - x[k]) / (d[k + 1] - d[k])))
    return out


@dataclass
class LoopResult:
    hc_ascending: float | None
    hc_descending: float | None
    hc: float | None
    shift: float | None
    squareness: float | None


def analyze_loop(x, y, v_programmed=None) -> LoopResult:
    """Coercive fields from the zero crossings (around the loop's mid level) of each branch.

    hc = half the loop width, shift = loop center (exchange bias). Squareness = Mr/Ms, the
    remanence relative to saturation, which is dimensionless and valid with an arbitrary-unit Kerr signal.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    direction = sweep_direction(x, v_programmed)
    mid = (np.percentile(y, 98) + np.percentile(y, 2)) / 2
    half = (np.percentile(y, 98) - np.percentile(y, 2)) / 2

    def branch(sign):
        m = direction == sign
        if m.sum() < 3:
            return None, None
        bx, by = x[m], y[m]
        order = np.argsort(bx)
        bx, by = bx[order], by[order]
        hc = _crossings(bx, by, mid)
        y0 = _crossings(by, bx, 0.0)  # y where x crosses 0 (swap axes)
        return (float(np.median(hc)) if hc else None), (float(np.median(y0)) if y0 else None)

    hc_up, y0_up = branch(1)
    hc_down, y0_down = branch(-1)
    if hc_up is not None and hc_down is not None:
        hc = abs(hc_up - hc_down) / 2
        shift = (hc_up + hc_down) / 2
    else:
        hc = shift = None
    squareness = None
    if y0_up is not None and y0_down is not None and half > 0:
        squareness = abs(y0_down - y0_up) / 2 / half
    return LoopResult(hc_up, hc_down, hc, shift, squareness)


@dataclass
class Symmetrized:
    up: np.ndarray      # indices of the ascending-branch points used, sorted by field
    odd: np.ndarray     # [S_up(H) - S_down(-H)] / 2 at those points: the magnetic (Kerr) loop
    even: np.ndarray    # [S_up(H) + S_down(-H)] / 2: offset, quadratic Kerr, even-in-field artifacts

    def mirror(self, a) -> np.ndarray:
        """A field-like column on the symmetric loop: ascending a(H), then descending -a(H)."""
        au = np.asarray(a, dtype=float)[self.up]
        return np.concatenate([au, -au])


def symmetrize(x, y, v_programmed=None) -> Symmetrized:
    """Odd/even decomposition of one hysteresis loop with respect to the field.

    The magnetization obeys M_up(H) = -M_down(-H). The odd part keeps everything that follows that
    symmetry (linear Kerr signal, also a linear Faraday background); the even part collects what does
    not change sign with the field (constant offset, quadratic MOKE ~ M², optics moved by the magnet).
    Needs a symmetric sweep (±same amplitude) of both branches. By construction the odd loop is
    centred: a real exchange-bias shift is removed too, so keep the raw data for that.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    d = sweep_direction(x, v_programmed)
    up = np.where(d > 0)[0]
    down = np.where(d < 0)[0]
    if len(up) < 3 or len(down) < 3:
        raise ValueError("symmetrization needs both branches of a full loop")
    up = up[np.argsort(x[up])]
    order = np.argsort(x[down])
    xd, yd = x[down][order], y[down][order]
    up = up[(-x[up] >= xd[0]) & (-x[up] <= xd[-1])]    # only fields whose mirror -H was measured
    if len(up) < 3:
        raise ValueError("symmetrization needs a sweep that is symmetric around zero field")
    mirror = np.interp(-x[up], xd, yd)                 # S_down(-H)
    return Symmetrized(up, (y[up] - mirror) / 2, (y[up] + mirror) / 2)


def apply_calibration(current: np.ndarray, coefficients: list[float]) -> np.ndarray:
    """Field = c0 + c1*I + c2*I^2 + ..."""
    return np.polynomial.polynomial.polyval(np.asarray(current, dtype=float), coefficients)
