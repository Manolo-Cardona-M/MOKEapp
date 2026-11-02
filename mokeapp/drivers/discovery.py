"""Find the instruments connected to this PC (for the Setup dialog): VISA resources and NI-DAQmx devices."""
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class DaqInfo:
    name: str
    product_type: str
    serial: str = ""
    ao_channels: list[str] = field(default_factory=list)
    ai_channels: list[str] = field(default_factory=list)
    ao_limit_V: float = 10.0
    terminals: list[str] = field(default_factory=lambda: ["DIFF", "RSE", "NRSE"])

    def describe(self) -> str:
        return (f"{self.product_type} (S/N {self.serial}): {len(self.ao_channels)} AO (±{self.ao_limit_V:g} V), "
                f"{len(self.ai_channels)} AI, inputs {'/'.join(self.terminals)}")


def visa_resources(simulate: bool = False) -> list[str]:
    """Instrument addresses VISA can see (GPIB, USB, serial, LAN), without the bare interfaces."""
    if simulate:
        return ["GPIB0::26::INSTR", "USB0::0x05E6::0x6500::04400000::INSTR", "ASRL1::INSTR"]
    import pyvisa
    rm = pyvisa.ResourceManager()
    try:
        return [r for r in rm.list_resources("?*") if not r.endswith("::INTFC")]
    finally:
        rm.close()


def visa_identify(address: str, timeout_ms: int = 3000, simulate: bool = False) -> str:
    """*IDN? of the instrument at `address` (raises if nothing answers). Leaves it in local mode."""
    if simulate:
        if address.startswith("USB0::0x05E6::0x6500"):
            return "KEITHLEY INSTRUMENTS,MODEL DMM6500,04400000,1.7.12b"
        if address.startswith("GPIB0::26"):
            return "SIMULATED,MODEL 2000,0,0"
        raise OSError(f"Nothing answers at {address}")
    import pyvisa
    rm = pyvisa.ResourceManager()
    try:
        inst = rm.open_resource(address)
        try:
            inst.timeout = timeout_ms
            return inst.query("*IDN?").strip()
        finally:
            try:
                inst.control_ren(pyvisa.constants.RENLineOperation.address_gtl)
            except Exception:
                pass
            inst.close()
    finally:
        rm.close()


def daq_devices(simulate: bool = False) -> list[DaqInfo]:
    """NI-DAQmx devices with their channels, AO range and supported input configurations."""
    if simulate:
        return [DaqInfo("Dev1", "USB-6002", "0", ["Dev1/ao0", "Dev1/ao1"], [f"Dev1/ai{k}" for k in range(8)],
                        10.0, ["DIFF", "RSE"]),
                DaqInfo("Dev2", "USB-6421", "1", ["Dev2/ao0", "Dev2/ao1"], [f"Dev2/ai{k}" for k in range(16)],
                        10.0, ["DIFF", "RSE", "NRSE"])]
    import nidaqmx.system
    from mokeapp.drivers.nidaq import device_ao_limit
    out = []
    for d in nidaqmx.system.System.local().devices:
        try:
            info = DaqInfo(d.name, d.product_type, str(d.serial_num or ""),
                           [c.name for c in d.ao_physical_chans], [c.name for c in d.ai_physical_chans],
                           device_ao_limit(d))
            if d.ai_physical_chans:
                try:
                    cfgs = d.ai_physical_chans[0].ai_term_cfgs
                    names = [c.name for c in cfgs if c.name in ("DIFF", "RSE", "NRSE")]
                    if names:
                        info.terminals = names
                except Exception:
                    log.debug("Input configurations of %s unknown", d.name, exc_info=True)
            out.append(info)
        except Exception:
            log.warning("Could not read NI device %s", getattr(d, "name", "?"), exc_info=True)
    return out
