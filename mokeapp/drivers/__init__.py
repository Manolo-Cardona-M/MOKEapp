"""Instrument set for one MOKE setup (real or simulated)."""
import logging

from mokeapp.config import SetupConfig
from mokeapp.drivers.kepco import KepcoBOP

log = logging.getLogger(__name__)


class Instruments:
    def __init__(self, cfg: SetupConfig, simulate: bool = False, sim_lab=None):
        self.cfg = cfg
        self.simulate = simulate
        if simulate:
            from mokeapp.drivers.simulated import SimDaq, SimKeithley, SimLab
            self.sim_lab = sim_lab or SimLab(cfg.kepco, read_delay_s=0.02)
            self.keithley = SimKeithley(cfg.keithley, self.sim_lab)
            self.daq = SimDaq(cfg.daq, self.sim_lab)
        else:
            from mokeapp.drivers.multimeter import ScpiMultimeter
            from mokeapp.drivers.nidaq import NiDaq
            self.sim_lab = None
            self.keithley = ScpiMultimeter(cfg.keithley)
            self.daq = NiDaq(cfg.daq)
        self.kepco = KepcoBOP(cfg.kepco, self.daq, cfg.max_magnet_voltage())
        self.status: dict[str, tuple[bool, str]] = {
            "keithley": (False, "not connected"),
            "daq": (False, "not connected"),
            "kepco": (False, "needs DAQ"),
        }

    @property
    def all_connected(self) -> bool:
        return self.keithley.connected and self.daq.connected

    @property
    def monitors(self) -> bool:
        """The Kepco current/voltage monitors are wired to the DAQ (setup daq.monitors)."""
        return self.cfg.daq.monitors

    def update_limits(self, resistance_ohm: float | None = None) -> None:
        """Max magnet voltage from the Kepco limits, the magnet R and what the DAQ's AO can program."""
        if resistance_ohm:
            self.kepco.resistance_ohm = resistance_ohm
        self.kepco.max_voltage = self.cfg.max_magnet_voltage(self.kepco.resistance_ohm,
                                                             getattr(self.daq, "ao_limit", 10.0))

    @property
    def hall_enabled(self) -> bool:
        """A Hall probe is wired to the DAQ (config daq.ai_hall); not used until configured."""
        return self.cfg.daq.hall_enabled

    def last_hall_mT(self) -> float | None:
        """Field from the Hall probe in the last monitor capture (None without a probe)."""
        v = getattr(self.daq, "last_hall_V", None)
        return None if v is None or not self.hall_enabled else self.cfg.daq.hall_mT(v)

    def connect(self) -> dict[str, tuple[bool, str]]:
        """Connect every instrument independently; a failure in one does not stop the others."""
        try:
            if not self.keithley.connected:
                self.keithley.connect()
            self.status["keithley"] = (True, self.keithley.idn)
        except Exception as e:
            log.exception("Keithley connection failed")
            self.status["keithley"] = (False, f"{type(e).__name__}: {e}")
        try:
            if not self.daq.connected:
                self.daq.connect()
                self.daq.write_ao(0.0)
                self.kepco.output_V = 0.0
                self.update_limits()
            self.status["daq"] = (True, f"{self.daq.product_type}  S/N {self.daq.serial}")
        except Exception as e:
            log.exception("DAQ connection failed")
            self.status["daq"] = (False, f"{type(e).__name__}: {e}")
        if not self.daq.connected:
            self.status["kepco"] = (False, "needs DAQ")
        elif not self.monitors:
            self.status["kepco"] = (True, f"{self.cfg.kepco.model}: monitors not wired, current = V/R "
                                          f"(R = {self.cfg.kepco.magnet_resistance_ohm:g} Ω)")
        else:
            try:
                v, i = self.kepco.read()
                self.status["kepco"] = (True, f"{self.cfg.kepco.model}: {v:+.3f} V, {i:+.3f} A")
            except Exception as e:
                log.exception("Kepco monitor read failed")
                self.status["kepco"] = (False, f"monitor read failed: {e}")
        return self.status

    def safe_zero(self) -> None:
        """Bring the magnet to 0 V. Never raises."""
        if self.daq.connected:
            try:
                self.kepco.ramp_to_zero()
            except Exception:
                log.exception("Ramp to zero failed, writing 0 V directly")
                try:
                    self.daq.write_ao(0.0)
                    self.kepco.output_V = 0.0
                except Exception:
                    # the DAQ is gone (e.g. USB drop-out): forget its tasks so Connect can start fresh
                    log.exception("Direct 0 V write failed, marking the DAQ as disconnected")
                    self.daq.close()
                    self.kepco.output_V = 0.0
                    self.status["daq"] = (False, "connection lost (USB?): press Connect")
                    self.status["kepco"] = (False, "needs DAQ")

    def close(self) -> None:
        self.safe_zero()
        self.daq.close()
        self.keithley.close()
        for k in self.status:
            self.status[k] = (False, "not connected")
