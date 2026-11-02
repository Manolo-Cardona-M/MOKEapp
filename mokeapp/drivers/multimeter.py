"""SCPI multimeter reading the photodetector voltage (GPIB, USB or serial through VISA).

The command set is chosen from the model the meter reports in *IDN? (setup `profile: auto`), or set by
hand. The Keithley 2000-family profile is the code tested on MOKE Lab 1; the others follow the
manufacturers' SCPI manuals and are checked on site (Run diagnostics shows any command error).
"""
import logging
import threading
import time
from dataclasses import dataclass

import pyvisa

from mokeapp.config import KeithleyConfig

log = logging.getLogger(__name__)

LINE_FREQ_HZ = 50       # Chile; 1 PLC = 20 ms
CONNECT_ATTEMPTS = 3    # the first GPIB open after a previous session sometimes fails with VI_ERROR_BERR


@dataclass(frozen=True)
class Profile:
    key: str
    name: str
    models: tuple[str, ...]             # substrings of the *IDN? model field
    arm: tuple[str, ...]                # one-shot triggering, sent on connect and after a bus clear
    function: str
    range_cmd: str                      # {v}
    range_auto: str
    nplc_cmd: str                       # {n}
    filter_on: tuple[str, ...] | None   # {c}; None = no repeat filter on this model
    filter_off: tuple[str, ...] | None
    terminal_query: str | None          # front/rear input switch; None = cannot be read
    front_reply: str = ""
    release: tuple[str, ...] = ()       # sent on close, before Go To Local
    read: str = ":READ?"
    error_query: str = ":SYST:ERR?"

    @property
    def has_filter(self) -> bool:
        return self.filter_on is not None

    @property
    def has_terminal(self) -> bool:
        return self.terminal_query is not None


PROFILES = {
    p.key: p for p in (
        Profile("k2000", "Keithley 2000 family", ("2000", "2001", "2002", "2010", "2015", "2016"),
                # without :INIT:CONT OFF every :READ? raises -213 (and later -350)
                ("*CLS", ":INIT:CONT OFF", ":ABOR"), ":SENS:FUNC 'VOLT:DC'",
                ":SENS:VOLT:DC:RANG {v}", ":SENS:VOLT:DC:RANG:AUTO ON", ":SENS:VOLT:DC:NPLC {n}",
                (":SENS:VOLT:DC:AVER:TCON REP", ":SENS:VOLT:DC:AVER:COUN {c}", ":SENS:VOLT:DC:AVER:STAT ON"),
                (":SENS:VOLT:DC:AVER:STAT OFF",), ":SYST:FRSW?", "1", (":INIT:CONT ON",)),
        Profile("dmm6500", "Keithley DMM6500 / DMM7510 / DAQ6510 (SCPI mode)", ("DMM6500", "DMM7510", "DAQ6510"),
                ("*CLS", ":ABOR"), ':SENS:FUNC "VOLT:DC"',
                ":SENS:VOLT:DC:RANG {v}", ":SENS:VOLT:DC:RANG:AUTO ON", ":SENS:VOLT:DC:NPLC {n}",
                (":SENS:VOLT:DC:AVER:TCON REP", ":SENS:VOLT:DC:AVER:COUN {c}", ":SENS:VOLT:DC:AVER ON"),
                (":SENS:VOLT:DC:AVER OFF",), ":ROUT:TERM?", "FRON"),
        Profile("k2100", "Keithley 2100 / 2110", ("2100", "2110"),
                ("*CLS",), "CONF:VOLT:DC", "VOLT:DC:RANG {v}", "VOLT:DC:RANG:AUTO ON", "VOLT:DC:NPLC {n}",
                None, None, None, read="READ?", error_query="SYST:ERR?"),
        Profile("scpi", "Generic SCPI multimeter", (),
                ("*CLS",), "CONF:VOLT:DC", "VOLT:DC:RANG {v}", "VOLT:DC:RANG:AUTO ON", "VOLT:DC:NPLC {n}",
                None, None, None, read="READ?", error_query="SYST:ERR?"),
    )
}


def detect_profile(idn: str) -> str:
    """Profile key for the model in an *IDN? reply ('scpi' if unknown)."""
    parts = [p.strip().upper() for p in idn.split(",")]
    model = parts[1] if len(parts) > 1 else idn.upper()
    model = model.replace("MODEL", "").strip()
    for p in PROFILES.values():
        if any(model == m or model.startswith(m) for m in p.models):
            return p.key
    return "scpi"


def estimate_read_time(nplc: float, filter: str, filter_count: int) -> float:
    """Approximate seconds per reading, fitted to the Lab 1 Keithley 2000 on 2026-10-29.

    Measured: NPLC 1 → 0.042 s, NPLC 10 → 0.63 s, NPLC 1 + repeat 10 → 0.28 s,
    NPLC 5 + repeat 10 → 3.1 s, NPLC 10 + repeat 10 → 8.4 s. Autozero makes it grow faster than NPLC.
    Other models are similar in order of magnitude; measure on site if it matters.
    """
    plc = nplc / LINE_FREQ_HZ
    if filter == "repeat":
        return filter_count * plc * (1 + 0.33 * nplc) + 0.01
    return plc * (1 + 0.22 * nplc) + 0.02


class ScpiMultimeter:
    def __init__(self, cfg: KeithleyConfig, resource_manager=pyvisa.ResourceManager):
        self.cfg = cfg
        self.idn = ""
        self.profile: Profile = PROFILES.get(cfg.profile, PROFILES["k2000"])
        self._rm_factory = resource_manager
        self._rm = None
        self._inst = None
        self._lock = threading.Lock()

    @property
    def connected(self) -> bool:
        return self._inst is not None

    @property
    def read_time_s(self) -> float:
        return estimate_read_time(self.cfg.nplc, self.cfg.filter, self.cfg.filter_count)

    def connect(self) -> None:
        for attempt in range(1, CONNECT_ATTEMPTS + 1):
            try:
                self._connect_once()
                return
            except Exception:
                self.close()
                if attempt == CONNECT_ATTEMPTS:
                    raise
                log.warning("Multimeter connection attempt %d failed, retrying", attempt, exc_info=True)
                time.sleep(0.5)

    def _connect_once(self) -> None:
        self._rm = self._rm_factory()
        self._inst = self._rm.open_resource(self.cfg.address)
        self._inst.timeout = self.cfg.timeout_ms
        self.idn = self._inst.query("*IDN?").strip()
        key = detect_profile(self.idn) if self.cfg.profile == "auto" else self.cfg.profile
        if key not in PROFILES:
            raise ValueError(f"Unknown multimeter profile {key!r} in the setup")
        self.profile = PROFILES[key]
        log.info("Multimeter %s: profile %s", self.idn, self.profile.name)
        self._arm()
        self._inst.write(self.profile.function)
        self.configure(self.cfg.range_V, self.cfg.nplc, self.cfg.filter, self.cfg.filter_count)

    def _arm(self) -> None:
        for cmd in self.profile.arm:
            self._inst.write(cmd)

    def configure(self, range_V, nplc: float, filter: str, filter_count: int) -> None:
        p = self.profile
        if filter == "repeat" and not p.has_filter:
            log.warning("%s has no repeat filter: filter off", p.name)
            filter = "off"
        with self._lock:
            if range_V == "auto":
                self._inst.write(p.range_auto)
            else:
                self._inst.write(p.range_cmd.format(v=float(range_V)))
            self._inst.write(p.nplc_cmd.format(n=float(nplc)))
            if p.has_filter:
                for cmd in (p.filter_on if filter == "repeat" else p.filter_off):
                    self._inst.write(cmd.format(c=int(filter_count)))
            # slow settings (e.g. NPLC 10 + repeat filter of 10 = seconds per reading) must not time out
            read_ms = estimate_read_time(nplc, filter, filter_count) * 1000
            self._inst.timeout = max(self.cfg.timeout_ms, int(2 * read_ms + 2000))
        self.cfg.range_V, self.cfg.nplc = range_V, nplc
        self.cfg.filter, self.cfg.filter_count = filter, filter_count

    def read(self) -> float:
        with self._lock:
            try:
                return float(self._inst.query(self.profile.read).split(",")[0])
            except pyvisa.errors.VisaIOError:
                # a timed-out/aborted transfer leaves the bus mid-message; clear it so the next read works
                log.warning("Multimeter read failed, clearing the instrument", exc_info=True)
                try:
                    self._inst.clear()
                    self._arm()
                except Exception:
                    log.exception("Multimeter clear failed")
                raise

    def input_terminal(self) -> str | None:
        """'FRONT' or 'REAR', or None when this model cannot report it."""
        if not self.profile.has_terminal:
            return None
        with self._lock:
            reply = self._inst.query(self.profile.terminal_query).strip().strip('"').upper()
        return "FRONT" if reply == self.profile.front_reply else "REAR"

    def errors(self) -> list[str]:
        """Drain the error queue."""
        out = []
        with self._lock:
            for _ in range(30):
                err = self._inst.query(self.profile.error_query).strip()
                if err.startswith("0,") or err.startswith("+0,"):
                    break
                out.append(err)
        return out

    def close(self) -> None:
        if self._inst is not None:
            try:
                with self._lock:
                    for cmd in self.profile.release:     # 2000: front panel measures continuously again
                        self._inst.write(cmd)
                    # Go To Local (over GPIB; :SYST:LOC is RS-232 only and raises -221 on a 2000)
                    self._inst.control_ren(pyvisa.constants.RENLineOperation.address_gtl)
            except Exception:
                pass
            try:
                self._inst.close()
            except Exception:
                pass
        if self._rm is not None:
            try:
                self._rm.close()
            except Exception:
                pass
        self._inst = None
        self._rm = None
