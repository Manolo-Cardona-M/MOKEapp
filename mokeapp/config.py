"""Setup configuration: load and validate config/<setup>.yaml."""
import logging
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

import yaml

from mokeapp.core.calibration import CalibrationError, CalibrationLibrary, CoilCalibration

log = logging.getLogger(__name__)


class ConfigError(Exception):
    pass


@dataclass
class KeithleyConfig:
    """The multimeter reading the photodetector (YAML section `multimeter`; `keithley` in older files)."""
    address: str = "GPIB0::26::INSTR"
    profile: str = "auto"           # auto = chosen from *IDN?; or k2000, dmm6500, k2100, scpi
    terminals: str = "rear"         # where the photodetector is plugged: rear | front | any
    timeout_ms: int = 5000
    range_V: float | str = 10.0
    nplc: float = 1.0
    filter: str = "off"
    filter_count: int = 10


@dataclass
class DaqConfig:
    device: str = "Dev1"
    model: str = "USB-6002"
    ao_channel: str = "Dev1/ao0"
    # Kepco rear monitors wired to the DAQ: they measure the real magnet current and voltage and enable
    # the safety checks. Without them the current is the programmed one, V/R.
    monitors: bool = True
    ai_current: str = "Dev1/ai0"
    ai_voltage: str = "Dev1/ai1"
    ai_terminal: str = "DIFF"
    ai_rate: float = 5000.0
    ai_samples: int = 50
    # optional Hall probe (gaussmeter analog output); empty channel = not installed
    ai_hall: str = ""
    hall_mT_per_V: float = 0.0
    hall_offset_mT: float = 0.0

    @property
    def hall_enabled(self) -> bool:
        return bool(self.ai_hall) and self.hall_mT_per_V != 0

    def hall_mT(self, volts: float) -> float:
        return self.hall_offset_mT + self.hall_mT_per_V * volts


@dataclass
class KepcoConfig:
    model: str = "BOP 50-8M"
    gain: float = -5.0
    v_max: float = 50.0
    i_max: float = 8.0
    current_monitor_A_per_V: float = 0.8
    voltage_monitor_V_per_V: float = 5.0
    magnet_resistance_ohm: float = 2.19
    slew_rate_V_per_s: float = 100.0


@dataclass
class SafetyConfig:
    current_tolerance_A: float = 0.3
    current_tolerance_frac: float = 0.4
    min_current_for_checks_A: float = 0.2
    resistance_warn_frac: float = 0.10
    resistance_stop_frac: float = 0.30


@dataclass
class Defaults:
    waveform: str = "triangle"
    amplitude_V: float = 2.5
    points_per_cycle: int = 200
    settle_ms: float = 50.0
    cycles: int = 5


@dataclass
class SetupConfig:
    name: str = "MOKE"
    keithley: KeithleyConfig = field(default_factory=KeithleyConfig)
    daq: DaqConfig = field(default_factory=DaqConfig)
    kepco: KepcoConfig = field(default_factory=KepcoConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    defaults: Defaults = field(default_factory=Defaults)
    path: Path | None = None
    # field calibration library (Calibration tab) and the active coil's calibration, if any
    calibration_path: Path | None = None
    calibration: CoilCalibration | None = None

    def max_magnet_voltage(self, resistance_ohm: float | None = None, ao_limit_V: float = 10.0) -> float:
        """Largest magnet voltage allowed: Kepco voltage limit, current limit, or what the DAQ's analog
        output can program (|gain| × AO range), whichever is lower."""
        r = resistance_ohm or self.kepco.magnet_resistance_ohm
        return min(self.kepco.v_max, self.kepco.i_max * r, abs(self.kepco.gain) * ao_limit_V)

    @property
    def multimeter(self) -> KeithleyConfig:
        return self.keithley

    def to_dict(self) -> dict:
        """Setup snapshot for CSV headers (the calibration has its own header block)."""
        d = asdict(self)
        for key in ("path", "calibration_path", "calibration"):
            d.pop(key)
        d["multimeter"] = d.pop("keithley")
        return d


_SECTIONS = {
    "multimeter": KeithleyConfig,
    "daq": DaqConfig,
    "kepco": KepcoConfig,
    "safety": SafetyConfig,
    "defaults": Defaults,
}
_FIELD_OF_SECTION = {"multimeter": "keithley"}   # dataclass field name, kept for the existing code


SETUP_HEADER = """\
# MOKEapp setup: instruments and limits of one MOKE.
# Edited from the Instruments tab (Setup) or by hand. Start the app with another setup with
#   python main.py --config config/<file>.yaml   (the app also remembers the last one used on this PC)
#
# multimeter: VISA address of the meter reading the photodetector (GPIB0::26::INSTR, USB0::...),
#             profile auto = detected from *IDN?
# daq:        NI-DAQmx device and channels; ao_channel drives the Kepco programming input.
#             monitors: true if the Kepco current/voltage monitors are wired to ai_current/ai_voltage.
#             ai_hall: optional Hall probe analog output (empty = none).
# kepco:      gain = magnet volts per programming volt (negative if inverting), limits, magnet R.
"""


def calibrations_folder(setup_path: Path) -> Path:
    """`calibrations/` next to the `config/` folder holding the setup, i.e. in the app folder. Shared by
    every setup of this MOKEapp, so renaming or adding a setup keeps the calibrations."""
    return Path(setup_path).resolve().parent.parent / "calibrations"


def save_config(cfg: SetupConfig, path: str | Path) -> Path:
    """Write the setup as YAML (atomic replace, so a crash never leaves half a file)."""
    path = Path(path)
    d = cfg.to_dict()
    body = "\n".join(yaml.safe_dump({key: d[key]}, sort_keys=False, allow_unicode=True)
                     for key in ("name", "multimeter", "daq", "kepco", "safety", "defaults"))
    tmp = path.with_suffix(".tmp")
    tmp.write_text(SETUP_HEADER + "\n" + body, encoding="utf-8")
    tmp.replace(path)
    cfg.path = path
    cfg.calibration_path = calibrations_folder(path)
    log.info("Setup saved to %s", path)
    return path


def _build(cls, section: str, raw) -> object:
    if raw is None:
        return cls()
    if not isinstance(raw, dict):
        raise ConfigError(f"[{section}] must be a mapping")
    known = {f.name: f for f in fields(cls)}
    unknown = set(raw) - set(known)
    if unknown:
        raise ConfigError(f"[{section}] unknown keys: {', '.join(sorted(unknown))}")
    values = {}
    for key, value in raw.items():
        default = getattr(cls(), key)
        if isinstance(default, bool) or default is None or isinstance(default, (str, list)):
            values[key] = value
        elif isinstance(default, (int, float)) and not (key == "range_V" and value == "auto"):
            try:
                values[key] = type(default)(value)
            except (TypeError, ValueError):
                raise ConfigError(f"[{section}] {key} must be a number, got {value!r}") from None
        else:
            values[key] = value
    return cls(**values)


def _validate(cfg: SetupConfig) -> None:
    k = cfg.keithley
    if k.filter is False:  # YAML reads an unquoted `off` as False
        k.filter = "off"
    if k.range_V != "auto" and k.range_V not in (0.1, 1, 10, 100, 1000):
        raise ConfigError("[multimeter] range_V must be 0.1, 1, 10, 100, 1000 or auto")
    if k.filter not in ("off", "repeat"):
        raise ConfigError("[multimeter] filter must be off or repeat")
    if not 1 <= k.filter_count <= 100:
        raise ConfigError("[multimeter] filter_count must be 1..100")
    if not k.address.strip():
        raise ConfigError("[multimeter] address is empty: choose the multimeter in Instruments → Setup")
    if k.profile not in ("auto", "k2000", "dmm6500", "k2100", "scpi"):
        raise ConfigError("[multimeter] profile must be auto, k2000, dmm6500, k2100 or scpi")
    if k.terminals not in ("rear", "front", "any"):
        raise ConfigError("[multimeter] terminals must be rear, front or any")
    if not isinstance(cfg.daq.monitors, bool):
        raise ConfigError("[daq] monitors must be true or false")
    if cfg.daq.ai_terminal not in ("DIFF", "RSE", "NRSE"):
        raise ConfigError("[daq] ai_terminal must be DIFF, RSE or NRSE")
    kp = cfg.kepco
    if kp.gain == 0:
        raise ConfigError("[kepco] gain cannot be 0")
    for name in ("v_max", "i_max", "current_monitor_A_per_V", "voltage_monitor_V_per_V",
                 "magnet_resistance_ohm", "slew_rate_V_per_s"):
        if getattr(kp, name) <= 0:
            raise ConfigError(f"[kepco] {name} must be > 0")
    if cfg.defaults.waveform not in ("triangle", "sine", "square"):
        raise ConfigError("[defaults] waveform must be triangle, sine or square")


def load_config(path: str | Path) -> SetupConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {path}: {e}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path.name} is not a setup file")
    if "calibration" in raw:    # old section: calibrations now live in the Calibration tab's library
        log.info("Ignoring the old [calibration] section of %s", path.name)
        raw.pop("calibration")
    if "keithley" in raw:       # older name of the multimeter section
        if "multimeter" in raw:
            raise ConfigError("Use either [multimeter] or the older [keithley], not both")
        raw["multimeter"] = raw.pop("keithley")
    unknown = set(raw) - set(_SECTIONS) - {"name"}
    if unknown:
        raise ConfigError(f"Unknown sections: {', '.join(sorted(unknown))}")
    cfg = SetupConfig(
        name=str(raw.get("name", path.stem)),
        path=path,
        calibration_path=calibrations_folder(path),
        **{_FIELD_OF_SECTION.get(s, s): _build(cls, s, raw.get(s)) for s, cls in _SECTIONS.items()},
    )
    _validate(cfg)
    try:
        library = CalibrationLibrary(cfg.calibration_path)
        library.import_legacy(path.resolve().parent)    # old config/calibrations_<setup>.yaml files
        cfg.calibration = library.active
    except (CalibrationError, OSError) as e:    # a broken library must not stop the app; measure in A
        log.error("%s", e)
    return cfg
