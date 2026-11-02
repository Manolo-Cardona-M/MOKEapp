"""MOKEapp entry point.

    .venv\\Scripts\\python.exe main.py                   the setup last used on this PC
    .venv\\Scripts\\python.exe main.py --sim             simulated instruments
    .venv\\Scripts\\python.exe main.py --config config/moke_lab2.yaml

On a PC with no setup yet, the app opens Instruments → Setup to choose the instruments.
"""
import argparse
import atexit
import logging
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from mokeapp.config import ConfigError, SetupConfig, load_config

ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT / "config"
LAB1 = CONFIG_DIR / "moke_lab1.yaml"


def choose_setup(arg: str | None, prefs: QSettings) -> Path | None:
    """--config, else the setup last used on this PC, else Lab 1's if it exists, else None (new PC)."""
    if arg:
        return Path(arg)
    last = prefs.value("setup_path")
    if last and Path(last).exists():
        return Path(last)
    return LAB1 if LAB1.exists() else None


def new_setup() -> SetupConfig:
    """In-memory setup for a PC without one; the Setup dialog opens and saves it."""
    cfg = SetupConfig(name="New MOKE")
    cfg.calibration_path = ROOT / "calibrations"
    return cfg


def setup_logging() -> Path:
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    path = logs / f"session_{datetime.now():%Y%m%d_%H%M%S}.log"
    handlers = [logging.FileHandler(path, encoding="utf-8")]
    if sys.stderr is not None:  # None when launched with pythonw (desktop shortcut, no console)
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                        handlers=handlers)
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="MOKEapp: MOKE measurement control")
    ap.add_argument("--config", default=None, help="setup file (default: the last one used on this PC)")
    ap.add_argument("--sim", action="store_true", help="use simulated instruments")
    ap.add_argument("--data", default=str(ROOT / "data"), help="default data folder")
    ap.add_argument("--theme", default="light", help="light (default), dark or classic")
    args = ap.parse_args()

    log_path = setup_logging()
    log = logging.getLogger("main")
    if sys.platform == "win32":
        # own taskbar entry and icon instead of Python's
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("USM.MOKEapp")
    app = QApplication(sys.argv)
    app.setApplicationName("MOKEapp")
    app.setWindowIcon(QIcon(str(ROOT / "mokeapp" / "gui" / "assets" / "icon.ico")))
    prefs = QSettings("USM", "MOKEapp")     # per PC and Windows user (registry)
    setup_path = choose_setup(args.config, prefs)
    if setup_path is None:
        cfg = new_setup()
        log.info("No setup on this PC yet: opening the Setup dialog")
    else:
        try:
            cfg = load_config(setup_path)
        except ConfigError as e:
            QMessageBox.critical(None, "Configuration error", f"{e}\n\nFix the file, or start with another "
                                                              f"setup: main.py --config <file>")
            return 1
        prefs.setValue("setup_path", str(Path(cfg.path).resolve()))
    log.info("Starting with %s (simulate=%s), log %s", cfg.path, args.sim, log_path)

    from mokeapp.gui import theme
    from mokeapp.gui.main_window import MainWindow
    theme.apply(app, args.theme)
    win = MainWindow(cfg, args.sim, Path(args.data), prefs=prefs, config_dir=CONFIG_DIR)
    atexit.register(win.state.inst.safe_zero)   # last line of defence

    def excepthook(exc_type, exc, tb):
        log.critical("Unhandled exception", exc_info=(exc_type, exc, tb))
        win.state.inst.safe_zero()
        QMessageBox.critical(win, "Unexpected error", f"{exc_type.__name__}: {exc}\n\n"
                             f"The magnet was set to 0 V. Details in {log_path.name}")
    sys.excepthook = excepthook

    win.showMaximized()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
