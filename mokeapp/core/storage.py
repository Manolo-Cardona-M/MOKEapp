"""CSV files: '#' metadata header lines (YAML), then a normal CSV table.

Columns for the horizontal axis are tagged ' [X]' (magnet current and, with a field calibration, the
field in mT) and the vertical one ' [Y]' (photodetector signal). The header's `columns:` block explains
every column in plain English for whoever receives the file.
"""
import csv
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
import yaml

X_TAG = " [X]"
Y_TAG = " [Y]"
COL_POINT = "point"
COL_TIME = "time_s"
COL_CYCLE = "cycle"
COL_VPROG = "V_programmed_V"
COL_VKEPCO = "V_kepco_V"
COL_CURRENT = "I_magnet_A" + X_TAG     # measured at the Kepco current monitor
COL_ISET = "I_set_A" + X_TAG           # programmed: V_programmed / nominal R
COL_SIGNAL = "signal_V" + Y_TAG
COL_FIELD = "B_mT" + X_TAG
COL_FIELD_OE = "H_Oe" + X_TAG    # same field in oersted: 10 × B_mT
COL_HALL = "B_hall_mT" + X_TAG     # field measured live by a Hall probe on the DAQ (when one is wired)
COL_HALL_OE = "H_hall_Oe" + X_TAG
FIELD_COLUMNS = (COL_FIELD, COL_FIELD_OE, COL_HALL, COL_HALL_OE)

MONITOR_COLUMNS = [COL_VKEPCO, COL_CURRENT]   # only when the Kepco monitors are wired


def raw_columns(monitors: bool = True) -> list[str]:
    return [COL_POINT, COL_TIME, COL_CYCLE, COL_VPROG, COL_ISET] + (MONITOR_COLUMNS if monitors else []) \
        + [COL_SIGNAL]


def avg_columns(monitors: bool = True) -> list[str]:
    return [COL_POINT, COL_VPROG, COL_ISET] + (MONITOR_COLUMNS if monitors else []) + [COL_SIGNAL]


RAW_COLUMNS = raw_columns()
AVG_COLUMNS = avg_columns()

_DESCRIPTIONS = {
    COL_POINT: "Point index within the cycle.",
    COL_TIME: "Seconds since the start of the measurement.",
    COL_CYCLE: "Cycle number (0 = first).",
    COL_VPROG: "Magnet voltage requested by the program (V).",
    COL_VKEPCO: "Magnet voltage measured at the Kepco voltage monitor (V).",
    COL_CURRENT: "X AXIS (raw): magnet current measured at the Kepco current monitor (A).",
    COL_ISET: "X AXIS (raw): programmed magnet current, V_programmed / nominal magnet R "
              "(kepco.magnet_resistance_ohm in the setup), A.",
    COL_SIGNAL: "Y AXIS: photodetector voltage read by the Keithley 2000 (V), i.e. the Kerr signal.",
}


def describe_columns(columns: list[str], calibration=None) -> dict:
    """Plain-English meaning of each column, for the CSV header."""
    out = {}
    for c in columns:
        if c == COL_FIELD and calibration is not None:
            out[c] = (f"X AXIS (calibrated): magnetic field (mT) from the Hall-probe calibration of coil "
                      f"'{calibration.coil}' ({calibration.date[:10]}): {calibration.equation()}, "
                      f"I = {getattr(calibration, 'current_source', 'measured')} current in A. "
                      f"Calibrated for I from {calibration.current_range[0]:.3g} to "
                      f"{calibration.current_range[1]:.3g} A.")
        elif c == COL_FIELD_OE:
            out[c] = "X AXIS (calibrated): the same field in oersted, H[Oe] = 10 * B[mT] (in air)."
        elif c == COL_HALL:
            out[c] = ("X AXIS (measured): magnetic field (mT) read live at every point from the Hall probe's "
                      "analog output on the DAQ (see hall_probe).")
        elif c == COL_HALL_OE:
            out[c] = "X AXIS (measured): the Hall probe field in oersted, H[Oe] = 10 * B_hall[mT]."
        else:
            out[c] = _DESCRIPTIONS.get(c, "")
    return out


def make_path(folder: Path, sample: str, kind: str, when: datetime) -> Path:
    safe = re.sub(r"[^\w\-]+", "_", sample.strip()) or "sample"
    base = f"{when:%Y%m%d_%H%M%S}_{safe}_{kind}"
    path = Path(folder) / f"{base}.csv"
    n = 2
    while path.exists():
        path = Path(folder) / f"{base}_{n}.csv"
        n += 1
    return path


class CsvWriter:
    """Writes rows incrementally and flushes, so a crash loses at most the unflushed rows."""

    def __init__(self, path: Path, metadata: dict, columns: list[str]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.columns = columns
        self.rows = 0
        self._f = open(self.path, "w", newline="", encoding="utf-8")
        for line in yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True).splitlines():
            self._f.write(f"# {line}\n")
        self._w = csv.writer(self._f)
        self._w.writerow(columns)
        self._f.flush()

    def write_rows(self, rows) -> None:
        for row in rows:
            self._w.writerow([_fmt(v) for v in row])
            self.rows += 1
        self._f.flush()

    def close(self) -> None:
        if not self._f.closed:
            self._f.close()


def _fmt(v):
    if isinstance(v, (float, np.floating)):
        return f"{v:.9g}"
    return v


def write_table(path: Path, metadata: dict, columns: list[str], data: np.ndarray) -> None:
    w = CsvWriter(path, metadata, columns)
    try:
        w.write_rows(data.tolist())
    finally:
        w.close()


@dataclass
class Measurement:
    path: Path | None       # None = only in memory (measured with 'Save data' off)
    metadata: dict
    columns: list[str]
    data: np.ndarray

    def column(self, name: str) -> np.ndarray:
        return self.data[:, self.columns.index(name)]

    def has(self, name: str) -> bool:
        return name in self.columns

    @property
    def x_column(self) -> str | None:
        """The raw current column: measured if the file has it, else programmed; the first [X] column
        in files from other tools."""
        for c in (COL_CURRENT, COL_ISET):
            if c in self.columns:
                return c
        return next((c for c in self.columns if c.endswith(X_TAG) and c not in FIELD_COLUMNS
                     and not c.startswith("B_")), None)

    def nominal_resistance(self) -> float | None:
        """The setup's nominal magnet R recorded in the header (to rebuild I_set for older files)."""
        cfg = self.metadata.get("config")
        try:
            return float(cfg["kepco"]["magnet_resistance_ohm"])
        except (TypeError, KeyError, ValueError):
            return None

    def programmed_current(self) -> np.ndarray | None:
        """I_set column, or V_programmed / nominal R for files written before it existed."""
        if COL_ISET in self.columns:
            return self.column(COL_ISET)
        r = self.nominal_resistance()
        if r and COL_VPROG in self.columns:
            return self.column(COL_VPROG) / r
        return None

    @property
    def field_column(self) -> str | None:
        """The field column to plot, in mT: measured by a Hall probe if the file has it, else calibrated."""
        if COL_HALL in self.columns:
            return COL_HALL
        return next((c for c in self.columns if c.startswith("B_") and c.endswith(X_TAG)), None)

    @property
    def y_column(self) -> str | None:
        return next((c for c in self.columns if c.endswith(Y_TAG)), None)


def load_measurement(path: str | Path) -> Measurement:
    path = Path(path)
    meta_lines, body = [], []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("#"):
                meta_lines.append(line[2:] if line.startswith("# ") else line[1:])
            elif line.strip():
                body.append(line)
    if not body:
        raise ValueError(f"{path.name}: no data table found")
    try:
        metadata = yaml.safe_load("".join(meta_lines)) or {}
    except yaml.YAMLError:
        metadata = {"header": "".join(meta_lines)}
    rows = list(csv.reader(body))
    columns = [c.strip() for c in rows[0]]
    try:
        data = np.array([[float(v) for v in r] for r in rows[1:] if r], dtype=float)
    except ValueError as e:
        raise ValueError(f"{path.name}: non-numeric value in data ({e})") from None
    if data.size == 0:
        data = np.empty((0, len(columns)))
    return Measurement(path, metadata if isinstance(metadata, dict) else {}, columns, data)
