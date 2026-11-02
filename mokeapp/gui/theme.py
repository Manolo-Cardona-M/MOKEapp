"""Visual themes: Qt stylesheet + plot colors. `classic` is the original look."""
import tempfile
from pathlib import Path

import pyqtgraph as pg
from PySide6.QtGui import QFont

THEMES = {
    "classic": {
        "styled": False,
        "plot_bg": "w", "plot_fg": "k", "grid": 0.25,
        "live": "#1f6fb4", "prev": "#969696", "avg": "#c0392b", "marker": "#c0392b", "cursor": "#787878",
    },
    "light": {
        "styled": True,
        "bg": "#eef1f5", "card": "#ffffff", "border": "#d9dee5", "text": "#1f2933", "muted": "#66707c",
        "input": "#ffffff", "accent": "#2563eb", "accent_hover": "#1d4ed8", "accent_text": "#ffffff",
        "danger": "#dc2626", "danger_hover": "#b91c1c", "danger_soft": "#f2b8b8", "header": "#ffffff",
        "tab_idle": "#66707c",
        "ok_bg": "#dcfce7", "ok_fg": "#166534", "warn_bg": "#fef3c7", "warn_fg": "#92400e",
        "off_bg": "#e5e7eb", "off_fg": "#4b5563", "sel": "#dbeafe",
        "plot_bg": "#ffffff", "plot_fg": "#3c4650", "grid": 0.15,
        "live": "#2563eb", "prev": "#b8c0cc", "avg": "#e4572e", "marker": "#e4572e", "cursor": "#9aa3ae",
    },
    "dark": {
        "styled": True,
        "bg": "#14171c", "card": "#1d2128", "border": "#2c323c", "text": "#e3e7ed", "muted": "#8b95a3",
        "input": "#252a33", "accent": "#22b8cf", "accent_hover": "#1a9fb3", "accent_text": "#0b1416",
        "danger": "#e5484d", "danger_hover": "#c9353a", "danger_soft": "#5a2a2d", "header": "#1d2128",
        "tab_idle": "#8b95a3",
        "ok_bg": "#12352a", "ok_fg": "#5ee0a0", "warn_bg": "#3d3013", "warn_fg": "#f5c26b",
        "off_bg": "#2a2f38", "off_fg": "#9aa3ae", "sel": "#1f3a44",
        "plot_bg": "#1d2128", "plot_fg": "#aeb7c2", "grid": 0.12,
        "live": "#22b8cf", "prev": "#4b5563", "avg": "#ff8c42", "marker": "#ff8c42", "cursor": "#6b7480",
    },
}

current = THEMES["classic"]

CLASSIC_QSS = """
QPushButton#stopButton { background:#d33f3f; color:white; font-weight:bold; font-size:14px;
                         padding:8px 20px; border-radius:4px; }
QPushButton[role="primary"] { font-weight:bold; padding:6px 18px; }
QPushButton[role="danger"] { background:#d33f3f; color:white; font-weight:bold; padding:6px 18px; }
QPushButton[role="danger"]:disabled { background:#eab0b0; }
QLabel#appTitle, QLabel#appSubtitle { max-height:0px; }
"""

QSS = """
QWidget {{ color:{text}; font-family:"Segoe UI"; font-size:10pt; }}
QMainWindow, QTabWidget > QWidget, QScrollArea, QScrollArea > QWidget > QWidget {{ background:{bg}; }}
QFrame#header {{ background:{header}; border-bottom:1px solid {border}; }}
QLabel#appTitle {{ font-size:15pt; font-weight:600; }}
QLabel#appSubtitle {{ color:{muted}; font-size:9pt; }}
QLabel#statusPill {{ border-radius:12px; padding:4px 14px; font-weight:600; }}
QLabel#statusPill[state="ok"] {{ background:{ok_bg}; color:{ok_fg}; }}
QLabel#statusPill[state="warn"] {{ background:{warn_bg}; color:{warn_fg}; }}
QLabel#statusPill[state="off"] {{ background:{off_bg}; color:{off_fg}; }}

QTabWidget::pane {{ border:none; background:{bg}; }}
QTabBar {{ background:{header}; }}
QTabBar::tab {{ background:transparent; color:{tab_idle}; padding:9px 22px; border:none;
               border-bottom:3px solid transparent; font-weight:600; }}
QTabBar::tab:selected {{ color:{accent}; border-bottom:3px solid {accent}; }}
QTabBar::tab:hover:!selected {{ color:{text}; }}

QGroupBox {{ background:{card}; border:1px solid {border}; border-radius:10px; margin-top:18px;
            padding:8px 10px 4px 10px; }}
QGroupBox::title {{ subcontrol-origin:margin; subcontrol-position:top left; left:4px; padding:0 4px;
                   color:{muted}; font-weight:600; font-size:9pt; }}

QPushButton {{ background:{input}; border:1px solid {border}; border-radius:7px; padding:6px 14px; }}
QPushButton:hover {{ border-color:{accent}; }}
QPushButton:disabled {{ color:{muted}; background:{bg}; border-color:{border}; }}
QPushButton[role="primary"] {{ background:{accent}; color:{accent_text}; border:none; font-weight:600;
                              padding:8px 16px; }}
QPushButton[role="primary"]:hover {{ background:{accent_hover}; }}
QPushButton[role="primary"]:disabled {{ background:{off_bg}; color:{muted}; }}
QPushButton#stopButton {{ background:{danger}; color:white; border:none; font-weight:700; font-size:11pt;
                         padding:9px 22px; border-radius:8px; }}
QPushButton#stopButton:hover {{ background:{danger_hover}; }}
QPushButton#autoscale {{ padding:4px 12px; }}
QPushButton#autoscale:checked {{ background:{sel}; border-color:{accent}; color:{text}; font-weight:600; }}
QPushButton#autoscale:!checked {{ background:{warn_bg}; border-color:{warn_fg}; color:{warn_fg}; font-weight:600; }}
QLabel#hint {{ color:{muted}; }}
QPushButton#segLeft, QPushButton#segMid, QPushButton#segRight {{ padding:4px 12px; }}
QPushButton#segLeft {{ border-top-right-radius:0; border-bottom-right-radius:0; }}
QPushButton#segMid {{ border-radius:0; border-left:none; }}
QPushButton#segRight {{ border-top-left-radius:0; border-bottom-left-radius:0; border-left:none; }}
QPushButton#segLeft:checked, QPushButton#segMid:checked, QPushButton#segRight:checked {{
    background:{accent}; color:{accent_text}; border-color:{accent}; }}
QPushButton[role="danger"] {{ background:{danger}; color:white; border:none; font-weight:700; padding:8px 16px; }}
QPushButton[role="danger"]:hover {{ background:{danger_hover}; }}
QPushButton[role="danger"]:disabled {{ background:{danger_soft}; color:white; }}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit {{ background:{input}; border:1px solid {border};
    border-radius:6px; padding:4px 6px; selection-background-color:{accent}; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{ border-color:{accent}; }}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {{ color:{muted}; }}
QComboBox QAbstractItemView {{ background:{card}; border:1px solid {border}; selection-background-color:{sel};
                              selection-color:{text}; }}

QTableWidget {{ background:{card}; border:1px solid {border}; border-radius:8px; gridline-color:{border}; }}
QHeaderView::section {{ background:{bg}; color:{muted}; border:none; border-bottom:1px solid {border};
                       padding:5px; font-weight:600; }}

QAbstractSpinBox {{ padding-right:20px; }}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{ subcontrol-origin:border; width:18px;
    border:none; background:transparent; }}
QAbstractSpinBox::up-button {{ subcontrol-position:top right; }}
QAbstractSpinBox::down-button {{ subcontrol-position:bottom right; }}
QAbstractSpinBox::up-arrow {{ image:url({icons}/up.svg); width:9px; height:9px; }}
QAbstractSpinBox::down-arrow {{ image:url({icons}/down.svg); width:9px; height:9px; }}
QComboBox::drop-down {{ border:none; width:22px; }}
QComboBox::down-arrow {{ image:url({icons}/down.svg); width:10px; height:10px; }}
QCheckBox::indicator, QGroupBox::indicator {{ width:16px; height:16px; border:1px solid {muted};
    border-radius:4px; background:{input}; }}
QCheckBox::indicator:checked, QGroupBox::indicator:checked {{ background:{accent}; border-color:{accent};
    image:url({icons}/check.svg); }}
QPushButton#browse {{ padding:4px 0px; }}

QScrollBar:vertical {{ background:transparent; width:9px; margin:2px; }}
QScrollBar::handle:vertical {{ background:{border}; border-radius:4px; min-height:30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height:0; width:0; }}
QToolTip {{ background:{card}; color:{text}; border:1px solid {border}; }}
"""


ICON_SVGS = {
    "up": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><path d="M1 7 L5 3 L9 7" '
          'fill="none" stroke="{muted}" stroke-width="1.6" stroke-linecap="round"/></svg>',
    "down": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><path d="M1 3 L5 7 L9 3" '
            'fill="none" stroke="{muted}" stroke-width="1.6" stroke-linecap="round"/></svg>',
    "check": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><path d="M3.5 8.5 L6.5 11.5 '
             'L12.5 4.5" fill="none" stroke="{accent_text}" stroke-width="2.2" stroke-linecap="round" '
             'stroke-linejoin="round"/></svg>',
}


def _write_icons(name: str, colors: dict) -> str:
    """Arrow/check icons in the theme's colors (Qt stylesheets can only reference image files)."""
    folder = Path(tempfile.gettempdir()) / "mokeapp_icons" / name
    folder.mkdir(parents=True, exist_ok=True)
    for icon, svg in ICON_SVGS.items():
        (folder / f"{icon}.svg").write_text(svg.format(**colors), encoding="utf-8")
    return folder.as_posix()


def apply(app, name: str) -> None:
    global current
    if name not in THEMES:
        raise ValueError(f"Unknown theme {name!r}; choose from {', '.join(THEMES)}")
    current = THEMES[name]
    app.setStyle("Fusion")
    if current["styled"]:
        app.setFont(QFont("Segoe UI", 10))
        app.setStyleSheet(QSS.format(icons=_write_icons(name, current), **current))
    else:
        app.setStyleSheet(CLASSIC_QSS)
    pg.setConfigOptions(antialias=True, background=current["plot_bg"], foreground=current["plot_fg"])


def pen(role: str, width: float = 2, **kw):
    return pg.mkPen(current[role], width=width, **kw)


def color(role: str) -> str:
    return current[role]
