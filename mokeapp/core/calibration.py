"""Field calibration of the magnet: B (mT, Hall probe) as a polynomial of the magnet current (A).

Every saved calibration is one YAML file in the app's `calibrations/` folder (independent of the
setup file). Recalibrating adds a new file, so the history is never lost; `active.yaml` says which
calibration is in use, and any older file can be loaded and used again.
"""
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import yaml

from mokeapp.core.processing import apply_calibration

log = logging.getLogger(__name__)

ACTIVE_FILE = "active.yaml"
UNIT = "mT"
MT_TO_OE = 10.0     # 1 mT of μ0H = 10 Oe of H (in air)
DEGREE_NAMES = {1: "linear", 2: "polynomial 2", 3: "polynomial 3"}


class CalibrationError(Exception):
    pass


@dataclass
class CoilCalibration:
    coil: str
    date: str                                   # ISO timestamp of the fit
    degree: int
    coefficients: list[float]                   # B = c0 + c1*I + c2*I^2 + ...  (mT, I in A)
    r2: float
    max_residual_mT: float
    current_range: list[float]                  # [min, max] calibrated current (A)
    points: list[list[float]] = field(default_factory=list)   # [[I_A, B_mT], ...]
    note: str = ""
    through_origin: bool = False                # fit forced to B = 0 at I = 0
    current_source: str = "measured"            # which current the points used: measured | programmed

    def field(self, current):
        return apply_calibration(current, self.coefficients)

    def field_for(self, measured, programmed):
        """Field from the same kind of current the coil was calibrated with (measured monitor current
        or programmed V/R). Falls back to the programmed current when no measured one exists."""
        if self.current_source == "measured" and measured is not None:
            m = np.asarray(measured, dtype=float)
            if m.size and np.all(np.isfinite(m)):
                return self.field(m)
        return self.field(programmed)

    def equation(self) -> str:
        terms = [] if self.through_origin else [f"{self.coefficients[0]:+.4g}"]
        for k, c in enumerate(self.coefficients[1:], start=1):
            terms.append(f"{c:+.4g}*I" + (f"^{k}" if k > 1 else ""))
        return "B[mT] = " + " ".join(terms).lstrip("+")

    @property
    def slope(self) -> float:
        """mT per A at I = 0."""
        return self.coefficients[1]

    def in_range(self, current: float) -> bool:
        lo, hi = self.current_range
        span = hi - lo
        return lo - 0.02 * span <= current <= hi + 0.02 * span

    def summary(self) -> str:
        return (f"{self.coil} · {DEGREE_NAMES.get(self.degree, self.degree)} · {self.slope:.4g} mT/A · "
                f"{self.date[:10]}")

    def header(self) -> dict:
        """What goes in a CSV header (no raw points)."""
        return {"coil": self.coil, "date": self.date, "model": DEGREE_NAMES.get(self.degree, str(self.degree)),
                "equation": self.equation(), "coefficients": [float(c) for c in self.coefficients],
                "r2": round(self.r2, 6), "current_range_A": [float(c) for c in self.current_range],
                "through_origin": self.through_origin, "current": CURRENT_SOURCES[self.current_source],
                "unit": UNIT}


CURRENT_SOURCES = {"measured": "measured magnet current (Kepco monitor)",
                   "programmed": "programmed current V/R"}


def fit(coil: str, currents, fields, degree: int = 1, through_origin: bool = False,
        current_source: str = "measured") -> CoilCalibration:
    """Least-squares polynomial B(I). through_origin forces B(0) = 0, i.e. ignores a constant offset
    (Hall probe zero, Earth's field) that would otherwise shift every loop by the same amount."""
    i = np.asarray(currents, dtype=float)
    b = np.asarray(fields, dtype=float)
    if degree not in DEGREE_NAMES:
        raise CalibrationError("Degree must be 1, 2 or 3")
    if current_source not in CURRENT_SOURCES:
        raise CalibrationError(f"Unknown current source {current_source!r}")
    if len(i) != len(b):
        raise CalibrationError("Current and field lists differ in length")
    if len(i) < degree + 2:
        raise CalibrationError(f"A {DEGREE_NAMES[degree]} fit needs at least {degree + 2} points")
    if np.ptp(i) == 0:
        raise CalibrationError("All currents are equal: measure at several currents")
    if through_origin:
        powers = np.column_stack([i ** k for k in range(1, degree + 1)])
        coefs = np.concatenate([[0.0], np.linalg.lstsq(powers, b, rcond=None)[0]])
    else:
        coefs = np.polynomial.polynomial.polyfit(i, b, degree)
    resid = b - apply_calibration(i, coefs)
    ss_tot = float(np.sum((b - b.mean()) ** 2))
    r2 = 1 - float(np.sum(resid ** 2)) / ss_tot if ss_tot > 0 else 0.0
    return CoilCalibration(coil=coil, date=datetime.now().isoformat(timespec="seconds"), degree=degree,
                           coefficients=[float(c) for c in coefs], r2=r2,
                           max_residual_mT=float(np.max(np.abs(resid))),
                           current_range=[float(i.min()), float(i.max())],
                           points=[[float(x), float(y)] for x, y in zip(i, b)], through_origin=through_origin,
                           current_source=current_source)


def read_calibration_file(path: Path) -> list[CoilCalibration]:
    """Calibrations in a file: one (calibrations/ files) or several (the old per-setup libraries)."""
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        if isinstance(raw, dict) and "coils" in raw:        # old library: {active, coils: {coil: [...]}}
            return [CoilCalibration(**c) for entries in (raw["coils"] or {}).values() for c in entries]
        if isinstance(raw, dict) and "coil" in raw:
            return [CoilCalibration(**raw)]
    except Exception as e:
        raise CalibrationError(f"Could not read {Path(path).name}: {e}") from None
    raise CalibrationError(f"{Path(path).name} is not a calibration file")


def write_calibration_file(path: Path, cal: CoilCalibration) -> None:
    tmp = Path(path).with_suffix(".tmp")
    tmp.write_text(f"# MOKEapp field calibration of coil '{cal.coil}', {cal.date}\n# {cal.equation()}  "
                   f"(R² = {cal.r2:.6f}, {cal.current_source} current)\n"
                   + yaml.safe_dump(asdict(cal), sort_keys=False, allow_unicode=True), encoding="utf-8")
    os.replace(tmp, path)       # never leaves a half-written file


class CalibrationLibrary:
    """All field calibrations of this MOKEapp folder: `calibrations/<coil>_<date>.yaml`, one file each.

    The folder does not depend on the setup file, so renaming or adding a setup keeps the calibrations.
    `active.yaml` names the calibration in use. Files are never deleted: deleting a coil moves its files
    to `calibrations/deleted/`. Old per-setup libraries (config/calibrations_<setup>.yaml) are imported.
    """

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self._files: dict[Path, CoilCalibration] = {}
        self._empty_coils: set[str] = set()      # created with "New coil", no calibration saved yet
        self._active_file: Path | None = None
        self.load()

    @property
    def path(self) -> Path:
        return self.folder

    def load(self) -> None:
        self._files = {}
        if self.folder.is_dir():
            for f in sorted(self.folder.glob("*.yaml")):
                if f.name == ACTIVE_FILE:
                    continue
                try:
                    cals = read_calibration_file(f)
                except CalibrationError as e:
                    log.warning("Skipping %s", e)
                    continue
                if len(cals) == 1:
                    self._files[f] = cals[0]
                else:
                    log.warning("%s holds several calibrations: use 'Load from file' to import it", f.name)
        self._active_file = None
        active = self.folder / ACTIVE_FILE
        if active.exists():
            try:
                name = (yaml.safe_load(active.read_text(encoding="utf-8")) or {}).get("file")
                if name and self.folder / name in self._files:
                    self._active_file = self.folder / name
            except Exception:
                log.warning("Could not read %s", active, exc_info=True)

    def _save_active(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        name = self._active_file.name if self._active_file else None
        (self.folder / ACTIVE_FILE).write_text("# Calibration in use (Calibration tab)\n"
                                               + yaml.safe_dump({"file": name}), encoding="utf-8")

    def coils(self) -> list[str]:
        return sorted({c.coil for c in self._files.values()} | self._empty_coils)

    def history(self, coil: str) -> list[CoilCalibration]:
        """Calibrations of a coil, newest first."""
        return sorted((c for c in self._files.values() if c.coil == coil), key=lambda c: c.date, reverse=True)

    def latest(self, coil: str) -> CoilCalibration | None:
        h = self.history(coil)
        return h[0] if h else None

    def file_of(self, cal: CoilCalibration) -> Path | None:
        return next((f for f, c in self._files.items() if c.coil == cal.coil and c.date == cal.date), None)

    @property
    def active(self) -> CoilCalibration | None:
        return self._files.get(self._active_file) if self._active_file else None

    @property
    def active_coil(self) -> str | None:
        return self.active.coil if self.active else None

    def add_coil(self, coil: str) -> None:
        coil = coil.strip()
        if not coil:
            raise CalibrationError("Coil name is empty")
        self._empty_coils.add(coil)

    def store(self, cal: CoilCalibration) -> Path:
        """Write a calibration to the folder (once: the same coil + date is not duplicated)."""
        existing = self.file_of(cal)
        if existing is not None:
            return existing
        self.folder.mkdir(parents=True, exist_ok=True)
        stem = f"{re.sub(r'[^\w\-]+', '_', cal.coil).strip('_') or 'coil'}_{re.sub(r'\D', '', cal.date)[:14]}"
        f, n = self.folder / f"{stem}.yaml", 2
        while f.exists():
            f, n = self.folder / f"{stem}_{n}.yaml", n + 1
        write_calibration_file(f, cal)
        self._files[f] = cal
        self._empty_coils.discard(cal.coil)
        return f

    def add(self, cal: CoilCalibration, make_active: bool = True) -> Path:
        f = self.store(cal)
        if make_active:
            self._active_file = f
            self._save_active()
        log.info("Calibration saved to %s: %s, %s, R2=%.5f", f.name, cal.summary(), cal.equation(), cal.r2)
        return f

    def use(self, cal: CoilCalibration | None) -> None:
        """Make this exact calibration (e.g. an older one loaded from a file) the one in use."""
        self._active_file = self.store(cal) if cal is not None else None
        self._save_active()
        log.info("Calibration in use: %s", cal.summary() if cal else None)

    def set_active(self, coil: str | None) -> None:
        """Use the newest calibration of `coil` (None = no calibration)."""
        if coil is not None and self.latest(coil) is None:
            raise CalibrationError(f"Coil '{coil}' has no calibration yet")
        self.use(self.latest(coil) if coil else None)

    def delete_coil(self, coil: str) -> None:
        """Move the coil's files to calibrations/deleted/ (recoverable by hand)."""
        trash = self.folder / "deleted"
        for f, c in list(self._files.items()):
            if c.coil == coil:
                trash.mkdir(parents=True, exist_ok=True)
                os.replace(f, trash / f.name)
                del self._files[f]
                if f == self._active_file:
                    self._active_file = None
        self._empty_coils.discard(coil)
        self._save_active()

    def import_legacy(self, config_dir: Path) -> int:
        """Bring in the calibrations of the old per-setup libraries (config/calibrations_*.yaml), which are
        left untouched. If nothing is in use yet, the newest one that was in use there becomes active."""
        imported, was_active = 0, []
        for lib in sorted(Path(config_dir).glob("calibrations_*.yaml")):
            try:
                cals = read_calibration_file(lib)
                active_coil = (yaml.safe_load(lib.read_text(encoding="utf-8")) or {}).get("active")
            except Exception as e:
                log.warning("Old calibration library %s not imported: %s", lib.name, e)
                continue
            for cal in cals:
                if self.file_of(cal) is None:
                    self.store(cal)
                    imported += 1
            mine = sorted((c for c in cals if c.coil == active_coil), key=lambda c: c.date)
            if mine:
                was_active.append(mine[-1])
        if self.active is None and was_active:
            self.use(max(was_active, key=lambda c: c.date))
        if imported:
            log.info("Imported %d calibration(s) from the old per-setup libraries in %s", imported, config_dir)
        return imported
