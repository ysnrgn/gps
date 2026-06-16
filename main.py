"""
main.py - GNSS Command Center (PyQt6 + QWebEngineView embedded map).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, TextIO, Tuple
from urllib.parse import unquote

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QFrame, QLabel, QLineEdit, QPushButton, QScrollArea, QSizePolicy,
    QStatusBar, QComboBox, QStackedWidget, QDialog,
    QPlainTextEdit,
)
from PyQt6.QtWebEngineCore import QWebEngineSettings
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtCore import Qt, QUrl, QTimer, qInstallMessageHandler
from PyQt6.QtGui import QPainter, QPen, QBrush, QColor, QFont, QKeySequence, QShortcut, QIntValidator

from filter import MovingAverageFilter
from mapping import LiveMap
from network import SocketConfig, TCPListener
from parser import GNSSFix, compute_spherical_midpoint, extract_gpgga, parse_gpgga


# ── Renk sabitleri ────────────────────────────────────────────────────────────
BG        = "#111827"
PANEL_BG  = "#1f2937"
CARD_BG   = "#1a2332"
BORDER    = "#374151"
TEXT      = "#f9fafb"
TEXT_DIM  = "#9ca3af"
GREEN     = "#10b981"
RED       = "#ef4444"
ORANGE    = "#f59e0b"
BLUE_COL  = "#3b82f6"
TEAL      = "#14b8a6"
GRAY      = "#6b7280"

FIX_LABELS = {0: "NO FIX", 1: "GPS", 2: "DGPS", 4: "FIX", 5: "FLOAT"}
COORD_FORMATS = ("DD", "DDM", "DMS", "NMEA")


def qt_message_filter(mode: Any, context: Any, message: str) -> None:
    """Qt'nin işlevi bozmayan QPushButton stylesheet spamini terminalden süzer."""
    if "Could not parse stylesheet of object QPushButton" in message:
        return
    print(message)


# ── Yardımcılar ───────────────────────────────────────────────────────────────

def compute_azimuth(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def _heading_fix_color(q1: int, q2: int) -> Optional[str]:
    """Returns display color for heading based on worst fix quality, None if no-fix."""
    if q1 == 0 or q2 == 0:
        return None
    rank = {4: 4, 5: 3, 2: 2, 1: 1}
    worst = min(rank.get(q1, 0), rank.get(q2, 0))
    if worst >= 4:
        return GREEN
    if worst == 3:
        return ORANGE
    if worst >= 1:
        return "#38bdf8"
    return None


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_008.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _hemisphere(value: float, positive: str, negative: str) -> str:
    return positive if value >= 0 else negative


def format_coordinate(value: float, axis: str, fmt: str) -> str:
    abs_value = abs(value)
    hemi = _hemisphere(value, "N" if axis == "lat" else "E", "S" if axis == "lat" else "W")
    degree_width = 2 if axis == "lat" else 3

    if fmt == "DD":
        return f"{abs_value:.7f}° {hemi}"

    degrees = int(abs_value)
    minutes_full = (abs_value - degrees) * 60.0
    minutes = int(minutes_full)
    seconds = (minutes_full - minutes) * 60.0

    if fmt == "DDM":
        return f"{degrees:0{degree_width}d}° {minutes_full:07.4f}' {hemi}"
    if fmt == "DMS":
        return f"{degrees:0{degree_width}d}° {minutes:02d}' {seconds:05.2f}\" {hemi}"
    if fmt == "NMEA":
        base = degrees * 100.0 + minutes_full
        decimals = 4 if axis == "lat" else 4
        width = 7 if axis == "lat" else 8
        return f"{base:0{width}.{decimals}f} {hemi}"

    return f"{abs_value:.7f}° {hemi}"


# ── Veri yapıları ─────────────────────────────────────────────────────────────

@dataclass
class DeviceEntry:
    name: str
    ip: str
    port: int
    device_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    role: str = "GENERIC"
    status: str = "Offline"
    mode: str = "GPS"
    fix_quality: int = 0
    satellites: int = 0
    hdop: float = 0.0
    fix_type: str = "-"
    latitude: float = 0.0
    longitude: float = 0.0
    altitude: float = 0.0
    last_update_ms: int = 0

    @property
    def has_position(self) -> bool:
        return self.latitude != 0.0 or self.longitude != 0.0

    @property
    def fix_status_text(self) -> str:
        if self.fix_quality == 4:
            return "FIX"
        if self.fix_quality == 5:
            return "FLOAT"
        if self.fix_quality == 2:
            return "DGPS"
        if self.fix_quality == 1:
            return "GPS"
        return "NO FIX"

    @property
    def fix_color(self) -> str:
        if self.fix_quality == 4:
            return GREEN
        if self.fix_quality == 5:
            return ORANGE
        if self.fix_quality in (1, 2):
            return BLUE_COL
        return GRAY


@dataclass
class _PointProxy:
    name: str
    latitude: float
    longitude: float
    has_position: bool = True


@dataclass
class AppState:
    is_connected: bool = False
    is_comparison_active: bool = False
    is_map_paused: bool = False
    is_map_locked: bool = False
    filter_window_size: int = 5
    outlier_threshold_m: float = 50.0
    coord_format: str = "DD"
    primary_source_id: str = "gps1"
    primary_target_id: str = "gps2"
    devices: Dict[str, DeviceEntry] = field(default_factory=dict)
    source_coords: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    flags: List[Dict[str, Any]] = field(default_factory=list)
    targets: List[Dict[str, Any]] = field(default_factory=list)
    raw_midpoint: Optional[Tuple[float, float]] = None
    filtered_midpoint: Optional[Tuple[float, float]] = None
    deviation_m: Optional[float] = None
    last_error: str = ""


@dataclass
class LogRecord:
    timestamp_ms: int
    raw_lat: float
    raw_lon: float
    filtered_lat: float
    filtered_lon: float
    altitude: float
    fix_quality: int
    deviation_m: Optional[float]


# ── CSV Logger ────────────────────────────────────────────────────────────────

class TelemetryLogger:
    headers = ["Timestamp", "Raw_Lat", "Raw_Lon", "Filtered_Lat", "Filtered_Lon",
               "Altitude", "Fix_Quality", "Deviation_m"]

    def __init__(self, output_path: str) -> None:
        self.output_path = Path(output_path)
        self._file: Optional[TextIO] = None
        self._writer: Optional[Any] = None
        self.last_filtered: Optional[Tuple[float, float]] = None

    def open(self) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        file_exists = self.output_path.exists() and self.output_path.stat().st_size > 0
        self._file = self.output_path.open("a", newline="", encoding="utf-8")
        self._writer = csv.writer(self._file)
        if not file_exists:
            writer = self._writer
            assert writer is not None
            writer.writerow(self.headers)
            self._file.flush()

    def write(self, record: LogRecord) -> None:
        if self._writer is None or self._file is None:
            self.open()
        assert self._writer is not None
        assert self._file is not None
        self._writer.writerow([
            record.timestamp_ms,
            f"{record.raw_lat:.10f}", f"{record.raw_lon:.10f}",
            f"{record.filtered_lat:.10f}", f"{record.filtered_lon:.10f}",
            f"{record.altitude:.3f}", record.fix_quality,
            "" if record.deviation_m is None else f"{record.deviation_m:.3f}",
        ])
        self.last_filtered = (record.filtered_lat, record.filtered_lon)
        self._file.flush()
        os.fsync(self._file.fileno())

    def close(self) -> None:
        if self._file is not None:
            self._file.flush()
            self._file.close()
        self._file = None
        self._writer = None

    def comparison_hook(self, legacy_log_path: str) -> Optional[float]:
        if self.last_filtered is None:
            return None
        path = Path(legacy_log_path)
        if not path.exists():
            return None
        try:
            with path.open("r", newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        except (OSError, csv.Error):
            return None
        if not rows:
            return None
        row = rows[-1]
        lat_v = next((row.get(k) for k in ("Filtered_Lat", "Lat", "lat") if row.get(k)), None)
        lon_v = next((row.get(k) for k in ("Filtered_Lon", "Lon", "lon") if row.get(k)), None)
        if lat_v is None or lon_v is None:
            return None
        try:
            return haversine_m(self.last_filtered[0], self.last_filtered[1],
                               float(lat_v), float(lon_v))
        except ValueError:
            return None


class SessionLogger:
    """1 Hz oturum kayıt sistemi — START/STOP kontrollü CSV."""

    HEADERS = [
        "Timestamp", "Oturum_Sn",
        "GPS-1_Ad", "GPS-1_Zaman", "GPS-1_Lat", "GPS-1_Lon", "GPS-1_Fix", "GPS-1_Uydu", "GPS-1_HDOP",
        "GPS-2_Ad", "GPS-2_Zaman", "GPS-2_Lat", "GPS-2_Lon", "GPS-2_Fix", "GPS-2_Uydu", "GPS-2_HDOP",
        "Orta_Cift", "Orta_Lat", "Orta_Lon",
        "Hedef-1_Ad", "Hedef-1_Lat", "Hedef-1_Lon",
        "Hedef-2_Ad", "Hedef-2_Lat", "Hedef-2_Lon",
        "Bayrak-1_Ad", "Bayrak-1_Lat", "Bayrak-1_Lon",
        "Bayrak-2_Ad", "Bayrak-2_Lat", "Bayrak-2_Lon",
        "Heading_Cift", "Heading_Derece",
    ]

    def __init__(self) -> None:
        self._file: Optional[TextIO] = None
        self._writer: Optional[Any] = None
        self._row_count: int = 0
        self._start_time: Optional[float] = None
        self.path: Optional[Path] = None

    @property
    def is_active(self) -> bool:
        return self._file is not None

    @property
    def row_count(self) -> int:
        return self._row_count

    @property
    def elapsed_s(self) -> float:
        return time.time() - self._start_time if self._start_time else 0.0

    def start(self, title: str) -> Path:
        self.stop()
        ts = time.strftime("%Y-%m-%d_%H-%M-%S")
        safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in title).strip() or "oturum"
        self.path = Path(f"{safe}_{ts}.csv")
        self._file = self.path.open("w", newline="", encoding="utf-8-sig")
        self._writer = csv.writer(self._file, delimiter=";")
        self._writer.writerow(self.HEADERS)
        self._file.flush()
        self._row_count = 0
        self._start_time = time.time()
        return self.path

    def stop(self) -> None:
        if self._file:
            self._file.flush()
            self._file.close()
            self._file = None
            self._writer = None
            self._start_time = None

    def write_row(self, row: List[Any]) -> None:
        if self._writer and self._file:
            self._writer.writerow(row)
            self._file.flush()
            self._row_count += 1


# ── Taskbar ───────────────────────────────────────────────────────────────────

class TaskbarManager:
    TBPF_NOPROGRESS = 0
    TBPF_NORMAL = 2
    TBPF_ERROR = 4

    def __init__(self, hwnd: int) -> None:
        self.hwnd = hwnd
        self._taskbar = None
        if platform.system() == "Windows":
            self._init_windows_taskbar()

    def set_connected_state(self) -> None:
        self._set_progress_state(self.TBPF_NORMAL)
        self._set_progress_value(100, 100)

    def set_disconnected_state(self) -> None:
        self._set_progress_state(self.TBPF_ERROR)
        self._set_progress_value(100, 100)

    def clear_overlay(self) -> None:
        self._set_progress_state(self.TBPF_NOPROGRESS)

    def _init_windows_taskbar(self) -> None:
        try:
            import comtypes.client  # type: ignore
            self._taskbar = comtypes.client.CreateObject(
                "{56FDF344-FD6D-11d0-958A-006097C9A090}",
                interface=comtypes.gen.TaskbarLib.ITaskbarList3,
            )
            self._taskbar.HrInit()
        except Exception:
            self._taskbar = None

    def _set_progress_state(self, state: int) -> None:
        if self._taskbar is None:
            return
        try:
            self._taskbar.SetProgressState(self.hwnd, state)
        except Exception:
            pass

    def _set_progress_value(self, completed: int, total: int) -> None:
        if self._taskbar is None:
            return
        try:
            self._taskbar.SetProgressValue(self.hwnd, completed, total)
        except Exception:
            pass


# ── Offline MBTiles Server ────────────────────────────────────────────────────

class MBTilesServer:
    """Serve local MBTiles as XYZ tile URLs for Leaflet."""

    def __init__(self, layers: Dict[str, Path], host: str = "127.0.0.1") -> None:
        self.layers = {name: path.resolve() for name, path in layers.items() if path.exists()}
        self.host = host
        self.port: int = 0
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.metadata: Dict[str, Dict[str, str]] = {}

    def start(self) -> None:
        if not self.layers:
            return
        self._load_metadata()
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: object) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                parent._handle(self)

        self._httpd = ThreadingHTTPServer((self.host, 0), Handler)
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        self._httpd = None
        self._thread = None

    def tile_url(self, layer: str) -> str:
        return f"http://{self.host}:{self.port}/tiles/{layer}/{{z}}/{{x}}/{{y}}.png"

    def layer_info(self, layer: str) -> Dict[str, str]:
        return self.metadata.get(layer, {})

    def _load_metadata(self) -> None:
        self.metadata.clear()
        for name, path in self.layers.items():
            try:
                con = sqlite3.connect(path)
                rows = con.execute("select name,value from metadata").fetchall()
                self.metadata[name] = {str(k): str(v) for k, v in rows}
                con.close()
            except sqlite3.Error:
                self.metadata[name] = {}

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        parts = unquote(handler.path).split("?")[0].strip("/").split("/")
        if len(parts) != 5 or parts[0] != "tiles":
            self._send_status(handler, HTTPStatus.NOT_FOUND)
            return
        _, layer, z_s, x_s, y_file = parts
        path = self.layers.get(layer)
        if path is None:
            self._send_status(handler, HTTPStatus.NOT_FOUND)
            return
        try:
            z = int(z_s)
            x = int(x_s)
            y = int(y_file.split(".")[0])
        except ValueError:
            self._send_status(handler, HTTPStatus.BAD_REQUEST)
            return

        meta = self.metadata.get(layer, {})
        scheme = meta.get("scheme", "xyz").lower()
        tile_row = (2 ** z - 1 - y) if scheme == "tms" else y
        try:
            con = sqlite3.connect(path)
            row = con.execute(
                "select tile_data from tiles where zoom_level=? and tile_column=? and tile_row=?",
                (z, x, tile_row),
            ).fetchone()
            con.close()
        except sqlite3.Error:
            self._send_status(handler, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if row is None:
            self._send_status(handler, HTTPStatus.NOT_FOUND)
            return
        data = bytes(row[0])
        fmt = meta.get("format", "png").lower()
        content_type = "image/jpeg" if fmt in {"jpg", "jpeg"} else "image/png"
        handler.send_response(HTTPStatus.OK)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Cache-Control", "public, max-age=86400")
        handler.send_header("Content-Length", str(len(data)))
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.end_headers()
        handler.wfile.write(data)

    @staticmethod
    def _send_status(handler: BaseHTTPRequestHandler, status: HTTPStatus) -> None:
        handler.send_response(status)
        handler.send_header("Content-Length", "0")
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.end_headers()


# ── Stil yardımcıları ─────────────────────────────────────────────────────────

def label(parent: QWidget, text: str, color: str = TEXT,
          bold: bool = False, size: int = 9) -> QLabel:
    lbl = QLabel(text, parent)
    weight = "bold" if bold else "normal"
    lbl.setStyleSheet(f"color:{color}; font-size:{size}pt; font-weight:{weight};")
    return lbl


def hline(parent: QWidget) -> QFrame:
    line = QFrame(parent)
    line.setFrameShape(QFrame.Shape.HLine)
    line.setStyleSheet(f"background:{BORDER}; border:none; max-height:1px;")
    return line


def badge(parent: QWidget, text: str, bg: str) -> QLabel:
    lbl = QLabel(text, parent)
    lbl.setStyleSheet(
        f"background:{bg}; color:#fff; font-size:7pt; font-weight:bold;"
        f"padding:2px 5px; border-radius:3px;"
    )
    return lbl


# ── Ana Pencere ───────────────────────────────────────────────────────────────

GLOBAL_STYLE = f"""
QMainWindow, QWidget {{ background:{BG}; color:{TEXT}; }}
QLineEdit {{
    background-color:#2d3748; color:{TEXT}; border:1px solid {BORDER};
    border-radius:4px; padding:5px 8px; font-size:9pt;
}}
QLineEdit:focus {{ border:1px solid {TEAL}; }}
QPushButton {{
    background-color:{ORANGE}; color:#000; font-weight:bold; font-size:9pt;
    border:1px solid #d97706;
}}
QPushButton:hover {{ background-color:#e08e09; }}
QScrollArea {{ border:none; background:{PANEL_BG}; }}
QScrollBar:vertical {{
    background:{PANEL_BG}; width:6px; margin:0;
}}
QScrollBar::handle:vertical {{
    background:{BORDER}; border-radius:3px; min-height:20px;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
QStatusBar {{ background:{PANEL_BG}; color:{TEXT_DIM}; font-size:8pt; }}
"""


class DeviceCard(QFrame):
    """4-column device row: STATUS | ID | IP:PORT | MODES — matches reference UI."""

    # Keep columns compact enough to leave room for edit/connect/delete actions.
    _W_STATUS = 44
    _W_ID     = 78
    _W_IP     = 84
    _W_MODES  = 70

    def __init__(
        self,
        dev: DeviceEntry,
        parent: QWidget,
        on_delete: Callable[[str], None],
        on_edit: Callable[[str], None],
        on_toggle_connect: Callable[[str], None],
        is_connected: bool,
    ) -> None:
        super().__init__(parent)
        self._device_id = dev.device_id

        status_text = dev.fix_status_text
        status_color = dev.fix_color

        self.setFixedHeight(54)
        self.setStyleSheet(
            f"background:#1a2332; border-bottom:1px solid #263040;"
        )

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 0, 6, 0)
        row.setSpacing(0)

        # ── STATUS col ─────────────────────────────────────────────────────────
        status_w = QWidget()
        status_w.setFixedWidth(self._W_STATUS)
        sv = QVBoxLayout(status_w)
        sv.setContentsMargins(0, 6, 0, 6)
        sv.setSpacing(2)
        dot = QLabel("●")
        dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dot.setStyleSheet(f"color:{status_color}; font-size:12pt; background:rgba(0,0,0,0);")
        sv.addWidget(dot)
        st_lbl = QLabel(status_text)
        st_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        st_lbl.setStyleSheet(f"color:{status_color}; font-size:7pt; background:rgba(0,0,0,0);")
        sv.addWidget(st_lbl)
        row.addWidget(status_w)

        # ── ID col ─────────────────────────────────────────────────────────────
        id_w = QWidget()
        id_w.setFixedWidth(self._W_ID)
        iv = QVBoxLayout(id_w)
        iv.setContentsMargins(0, 8, 4, 8)
        iv.setSpacing(2)
        name_lbl = QLabel(dev.name)
        name_lbl.setStyleSheet(f"color:{TEXT}; font-size:9pt; font-weight:bold; background:rgba(0,0,0,0);")
        iv.addWidget(name_lbl)
        fix_lbl = QLabel(dev.fix_status_text)
        fix_lbl.setStyleSheet(f"color:{TEXT_DIM}; font-size:7pt; background:rgba(0,0,0,0);")
        iv.addWidget(fix_lbl)
        row.addWidget(id_w)

        # ── IP:PORT col ────────────────────────────────────────────────────────
        ip_w = QWidget()
        ip_w.setFixedWidth(self._W_IP)
        ipv = QVBoxLayout(ip_w)
        ipv.setContentsMargins(0, 0, 4, 0)
        ipv.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        port_text = "-" if dev.port <= 0 else str(dev.port)
        ip_str = "-" if dev.ip == "-" else f"{dev.ip}:{port_text}"
        ip_lbl = QLabel(ip_str)
        ip_lbl.setStyleSheet(f"color:{TEXT_DIM}; font-size:8pt; background:rgba(0,0,0,0);")
        ip_lbl.setMaximumWidth(self._W_IP - 4)
        ipv.addWidget(ip_lbl)
        row.addWidget(ip_w)

        # ── MODES col ──────────────────────────────────────────────────────────
        modes_w = QWidget()
        modes_w.setFixedWidth(self._W_MODES)
        mv = QVBoxLayout(modes_w)
        mv.setContentsMargins(0, 5, 0, 5)
        mv.setSpacing(3)
        mode_color = TEAL if dev.mode == "BASE" else BLUE_COL
        mode_row = QHBoxLayout()
        mode_row.setSpacing(3)
        mode_row.setContentsMargins(0, 0, 0, 0)
        mode_badge = badge(modes_w, dev.mode, mode_color)
        mode_row.addWidget(mode_badge)
        if dev.satellites > 0:
            sat_badge = badge(modes_w, f"{dev.satellites}", "#334155")
            mode_row.addWidget(sat_badge)
        mode_row.addStretch()
        mv.addLayout(mode_row)
        if dev.fix_quality in (4, 5):
            rtk_badge = badge(modes_w, "RTK", GREEN if dev.fix_quality == 4 else ORANGE)
            mv.addWidget(rtk_badge)
        row.addWidget(modes_w)

        actions_w = QWidget()
        actions_w.setFixedWidth(60)
        actions = QHBoxLayout(actions_w)
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(0)

        # ── Edit button ────────────────────────────────────────────────────────
        edit_btn = QPushButton("✏")
        edit_btn.setFixedSize(20, 20)
        edit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        edit_btn.setToolTip(f"{dev.name} düzenle")
        edit_btn.setStyleSheet(
            "QPushButton{background-color:#1a2332;color:#4b5563;font-size:9pt;"
            "border:1px solid #1a2332;}"
            "QPushButton:hover{color:#38bdf8;}"
        )
        edit_btn.clicked.connect(lambda: on_edit(self._device_id))
        actions.addWidget(edit_btn)

        # ── Connect / disconnect button ────────────────────────────────────────
        conn_btn = QPushButton("⏻")
        conn_btn.setFixedSize(20, 20)
        conn_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        conn_btn.setToolTip(f"{dev.name} bağlan/kopar")
        conn_color = GREEN if is_connected else "#6b7280"
        conn_hover = ORANGE if is_connected else "#38bdf8"
        conn_btn.setStyleSheet(
            f"QPushButton{{background-color:#1a2332;color:{conn_color};font-size:10pt;"
            "border:1px solid #1a2332;}}"
            f"QPushButton:hover{{color:{conn_hover};}}"
        )
        conn_btn.clicked.connect(lambda: on_toggle_connect(self._device_id))
        actions.addWidget(conn_btn)

        # ── Delete button ──────────────────────────────────────────────────────
        delete_btn = QPushButton("×")
        delete_btn.setFixedSize(20, 20)
        delete_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        delete_btn.setToolTip(f"{dev.name} sil")
        delete_btn.setStyleSheet(
            "QPushButton{background-color:#1a2332;color:#4b5563;font-size:13pt;"
            "font-weight:bold;border:1px solid #1a2332;}"
            "QPushButton:hover{color:#ef4444;}"
        )
        delete_btn.clicked.connect(lambda: on_delete(self._device_id))
        actions.addWidget(delete_btn)
        row.addWidget(actions_w)



class HeadingCard(QFrame):
    """Harita üzeri baş açısı overlay kartı — bağımsız cihaz çifti seçimi."""

    def __init__(
        self,
        parent: QWidget,
        src: str,
        dst: str,
        on_add: Callable,
        on_remove: "Callable[[HeadingCard], None]",
        get_devices: "Callable[[], List[Tuple[str, str]]]",
    ) -> None:
        super().__init__(parent)
        self.src = src
        self.dst = dst
        self._get_devices = get_devices

        self.paused: bool = False
        self.setFixedSize(192, 108)
        self.setStyleSheet(
            f"QFrame{{background:rgba(15,23,32,220);border:1px solid {BORDER};"
            "border-radius:5px;}"
        )

        vl = QVBoxLayout(self)
        vl.setContentsMargins(8, 6, 8, 6)
        vl.setSpacing(2)

        hdr = QHBoxLayout()
        hdr.setContentsMargins(0, 0, 0, 0)
        hdr.setSpacing(2)
        title_lbl = QLabel("BAŞ AÇISI")
        title_lbl.setStyleSheet(
            f"color:{TEXT_DIM};font-size:8pt;font-weight:bold;background:rgba(0,0,0,0);"
        )
        hdr.addWidget(title_lbl)
        hdr.addStretch()

        self._pause_btn = QPushButton("⏸")
        self._pause_btn.setFixedSize(22, 18)
        self._pause_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._pause_btn.setStyleSheet(
            "QPushButton{background-color:#1a2332;color:#4b5563;font-size:8pt;"
            "border:1px solid #1a2332;border-radius:2px;}"
            "QPushButton:hover{color:#facc15;}"
        )
        self._pause_btn.setToolTip("Hesabı duraklat / devam ettir")
        self._pause_btn.clicked.connect(self._toggle_pause)
        hdr.addWidget(self._pause_btn)

        x_btn = QPushButton("×")
        x_btn.setFixedSize(18, 18)
        x_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        x_btn.setStyleSheet(
            "QPushButton{background-color:#1a2332;color:#6b7280;font-size:12pt;"
            "border:1px solid #1a2332;}"
            "QPushButton:hover{color:#ef4444;}"
        )
        x_btn.clicked.connect(lambda: on_remove(self))
        hdr.addWidget(x_btn)
        vl.addLayout(hdr)

        self._heading_lbl = QLabel("--°")
        self._heading_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._heading_lbl.setStyleSheet(
            "color:#38bdf8;font-size:26pt;font-weight:bold;background:rgba(0,0,0,0);"
        )
        vl.addWidget(self._heading_lbl)

        pair_row = QHBoxLayout()
        pair_row.setContentsMargins(0, 0, 0, 0)
        pair_row.setSpacing(0)
        self._pair_lbl = QLabel(self._pair_text())
        self._pair_lbl.setStyleSheet(
            f"color:{TEXT_DIM};font-size:8pt;background:rgba(0,0,0,0);"
        )
        pair_row.addWidget(self._pair_lbl)
        pair_row.addStretch()
        cfg_btn = QPushButton("⚙")
        cfg_btn.setFixedSize(18, 18)
        cfg_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cfg_btn.setStyleSheet(
            "QPushButton{background-color:#1a2332;color:#4b5563;font-size:9pt;"
            "border:1px solid #1a2332;}"
            "QPushButton:hover{color:#38bdf8;}"
        )
        cfg_btn.clicked.connect(self._open_pair_dialog)
        pair_row.addWidget(cfg_btn)
        vl.addLayout(pair_row)

        add_btn = QPushButton("+ Yeni Kart")
        add_btn.setFixedHeight(17)
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.setStyleSheet(
            "QPushButton{background-color:#1a2332;color:#4b5563;font-size:7pt;"
            "border:1px solid #374151;}"
            "QPushButton:hover{color:#38bdf8;}"
        )
        add_btn.clicked.connect(on_add)
        vl.addWidget(add_btn)

    def set_heading(self, value: Optional[float], color: str = "#38bdf8") -> None:
        if self.paused:
            return
        if value is None:
            self._heading_lbl.setText("--°")
            self._heading_lbl.setStyleSheet(
                f"color:{TEXT_DIM};font-size:20pt;font-weight:bold;background:rgba(0,0,0,0);"
            )
        else:
            self._heading_lbl.setText(f"{value:.1f}°")
            self._heading_lbl.setStyleSheet(
                f"color:{color};font-size:20pt;font-weight:bold;background:rgba(0,0,0,0);"
            )

    def set_status(self, msg: str, color: str = ORANGE) -> None:
        if self.paused:
            return
        self._heading_lbl.setText(msg)
        self._heading_lbl.setStyleSheet(
            f"color:{color};font-size:11pt;font-weight:bold;background:rgba(0,0,0,0);"
        )

    def _toggle_pause(self) -> None:
        self.paused = not self.paused
        win = self.window()
        if isinstance(win, MainWindow) and win.app:
            win.app._markers_dirty = True
        if self.paused:
            self._pause_btn.setText("▶")
            self._pause_btn.setStyleSheet(
                "QPushButton{background-color:#7c2d12;color:#fb923c;font-size:8pt;"
                "border:1px solid #ea580c;border-radius:2px;}"
                "QPushButton:hover{color:#fde68a;}"
            )
            self._heading_lbl.setText("⏸ --°")
            self._heading_lbl.setStyleSheet(
                f"color:{TEXT_DIM};font-size:16pt;font-weight:bold;background:rgba(0,0,0,0);"
            )
        else:
            self._pause_btn.setText("⏸")
            self._pause_btn.setStyleSheet(
                "QPushButton{background-color:#1a2332;color:#4b5563;font-size:8pt;"
                "border:1px solid #1a2332;border-radius:2px;}"
                "QPushButton:hover{color:#facc15;}"
            )

    def refresh_labels(self) -> None:
        self._pair_lbl.setText(self._pair_text())

    def _label_for(self, device_id: str) -> str:
        for did, name in self._get_devices():
            if did == device_id:
                return name
        return device_id

    def _pair_text(self) -> str:
        return f"{self._label_for(self.src)} → {self._label_for(self.dst)}"

    def _open_pair_dialog(self) -> None:
        devices = self._get_devices()
        if len(devices) < 2:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Baş Açısı Kaynağı")
        dlg.setFixedSize(240, 165)
        dlg.setStyleSheet(
            f"QDialog{{background:#1f2937;color:{TEXT};}}"
            f"QLabel{{color:{TEXT_DIM};font-size:8pt;}}"
            f"QComboBox{{background-color:#2d3748;color:{TEXT};border:1px solid {BORDER};"
            f"border-radius:4px;padding:5px 8px;}}"
        )
        vl = QVBoxLayout(dlg)
        vl.setSpacing(6)
        vl.addWidget(QLabel("A — Kaynak"))
        src_cb = QComboBox()
        for device_id, name in devices:
            src_cb.addItem(name, device_id)
        src_index = src_cb.findData(self.src)
        if src_index >= 0:
            src_cb.setCurrentIndex(src_index)
        vl.addWidget(src_cb)
        vl.addWidget(QLabel("B — Hedef"))
        dst_cb = QComboBox()
        for device_id, name in devices:
            dst_cb.addItem(name, device_id)
        dst_index = dst_cb.findData(self.dst)
        if dst_index >= 0:
            dst_cb.setCurrentIndex(dst_index)
        vl.addWidget(dst_cb)

        btn_row = QHBoxLayout()
        rev_btn = QPushButton("⇄ Ters")
        rev_btn.setStyleSheet(
            f"background-color:#202b36;color:{TEXT_DIM};font-size:8pt;"
            f"border:1px solid {BORDER};border-radius:4px;padding:4px 8px;"
        )
        def _swap() -> None:
            a, b = src_cb.currentIndex(), dst_cb.currentIndex()
            src_cb.setCurrentIndex(b)
            dst_cb.setCurrentIndex(a)
        rev_btn.clicked.connect(_swap)
        btn_row.addWidget(rev_btn)
        apply_btn = QPushButton("Uygula")
        apply_btn.setStyleSheet(
            f"background-color:#1d4ed8;color:#fff;font-size:8pt;"
            f"border:none;border-radius:4px;padding:4px 12px;"
        )
        apply_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(apply_btn)
        vl.addLayout(btn_row)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_src = str(src_cb.currentData())
            new_dst = str(dst_cb.currentData())
            if new_src != new_dst:
                self.src = new_src
                self.dst = new_dst
                self._pair_lbl.setText(self._pair_text())



class CompassWidget(QWidget):
    """Map overlay compass rose with degree markings and heading needle."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._heading: Optional[float] = None
        self.setFixedSize(130, 130)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    def set_heading(self, heading: Optional[float]) -> None:
        if heading != self._heading:
            self._heading = heading
            self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy = self.width() // 2, self.height() // 2
        r = min(self.width(), self.height()) // 2 - 5

        # Background
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(8, 14, 24, 215)))
        p.drawEllipse(cx - r, cy - r, 2 * r, 2 * r)

        # Outer ring
        p.setPen(QPen(QColor(45, 65, 95), 1.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(cx - r, cy - r, 2 * r, 2 * r)

        # Tick marks — every 10°
        for deg in range(0, 360, 10):
            rad = math.radians(deg)
            is_card = deg % 90 == 0
            is_maj = deg % 30 == 0
            tl = r * (0.18 if is_card else 0.12 if is_maj else 0.07)
            x1 = cx + (r - 2) * math.sin(rad)
            y1 = cy - (r - 2) * math.cos(rad)
            x2 = cx + (r - 2 - tl) * math.sin(rad)
            y2 = cy - (r - 2 - tl) * math.cos(rad)
            col = QColor(167, 139, 250) if is_card else (QColor(90, 110, 150) if is_maj else QColor(40, 55, 80))
            w = 1.8 if is_card else (1.1 if is_maj else 0.7)
            p.setPen(QPen(col, w))
            p.drawLine(int(x1), int(y1), int(x2), int(y2))

        # Major degree labels (30°, 60°, 120° … skip cardinals)
        f_sm = QFont("Consolas", 5)
        p.setFont(f_sm)
        lbl_r = r * 0.52
        for deg in range(30, 360, 30):
            if deg % 90 == 0:
                continue
            rad = math.radians(deg)
            lx = cx + lbl_r * math.sin(rad)
            ly = cy - lbl_r * math.cos(rad)
            p.setPen(QPen(QColor(70, 90, 120)))
            p.drawText(int(lx - 7), int(ly + 3), str(deg))

        # Cardinal labels
        f_card = QFont("Consolas", 8, QFont.Weight.Bold)
        p.setFont(f_card)
        card_r = r * 0.68
        for lbl, deg in (("N", 0), ("E", 90), ("S", 180), ("W", 270)):
            rad = math.radians(deg)
            lx = cx + card_r * math.sin(rad)
            ly = cy - card_r * math.cos(rad)
            col = QColor(167, 139, 250) if lbl == "N" else QColor(140, 165, 200)
            p.setPen(QPen(col))
            p.drawText(int(lx - 5), int(ly + 4), lbl)

        # Needle
        if self._heading is not None:
            rad = math.radians(self._heading)
            tx = cx + r * 0.52 * math.sin(rad)
            ty = cy - r * 0.52 * math.cos(rad)
            bx = cx - r * 0.22 * math.sin(rad)
            by = cy + r * 0.22 * math.cos(rad)
            p.setPen(QPen(QColor(239, 68, 68), 2.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(int(cx), int(cy), int(tx), int(ty))
            p.setPen(QPen(QColor(100, 110, 130), 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(int(cx), int(cy), int(bx), int(by))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(210, 215, 230)))
            p.drawEllipse(cx - 3, cy - 3, 6, 6)
            f_hdg = QFont("Consolas", 7, QFont.Weight.Bold)
            p.setFont(f_hdg)
            p.setPen(QPen(QColor(239, 68, 68)))
            p.drawText(cx - 13, cy + r - 8, f"{self._heading:.0f}°")
        else:
            f_hdg = QFont("Consolas", 8, QFont.Weight.Bold)
            p.setFont(f_hdg)
            p.setPen(QPen(QColor(70, 90, 120)))
            p.drawText(cx - 8, cy + r - 8, "--°")

        p.end()


class MainWindow(QMainWindow):
    def __init__(self, state: AppState) -> None:
        super().__init__()
        self.state = state
        self.app: Optional[Application] = None
        self._map_mtime: float = 0.0
        self._map_js_ready: bool = False
        self._measure_restore_timer: Optional[QTimer] = None
        self._session_title_input: Optional[QLineEdit] = None
        self._session_toggle_btn: Optional[QPushButton] = None
        self._session_status_lbl: Optional[QLabel] = None
        self.name_input: QLineEdit
        self.ip_input: QLineEdit
        self.port_input: QLineEdit

        self.setWindowTitle("GNSS Command Center")
        self.resize(1365, 768)
        self.setMinimumSize(1180, 680)
        self.setStyleSheet(GLOBAL_STYLE)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_title_bar())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(1)
        body.addWidget(self._build_nav_rail())
        body.addWidget(self._build_left_panel())
        body.addWidget(self._build_map_panel(), stretch=1)
        body.addWidget(self._build_right_panel())
        root.addLayout(body, stretch=1)

        self.setStatusBar(self._build_status_bar())

        self._start_timers()

    # ── Başlık çubuğu ─────────────────────────────────────────────────────────

    def _build_title_bar(self) -> QFrame:
        bar = QFrame()
        bar.setFixedHeight(42)
        bar.setStyleSheet(f"background:#121820; border-bottom:1px solid {BORDER};")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 10, 0)
        layout.setSpacing(0)

        logo = QLabel("◉")
        logo.setFixedWidth(44)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setStyleSheet(f"color:{TEXT_DIM}; font-size:15pt; border-right:1px solid {BORDER};")
        layout.addWidget(logo)

        title = QLabel("  GNSS COMMAND CENTER  |  SITE: MAPLETON PROJECT  |")
        title.setStyleSheet(f"color:{TEXT}; font-size:12pt; font-weight:bold; letter-spacing:1px;")
        layout.addWidget(title)

        self._clock_lbl = QLabel("")
        self._clock_lbl.setStyleSheet(f"color:{TEXT_DIM}; font-size:11pt; font-weight:bold;")
        layout.addWidget(self._clock_lbl)
        layout.addStretch()

        self._tab_btns: Dict[str, QPushButton] = {}

        # Koordinat format seçici
        fmt_frame = QFrame()
        fmt_frame.setStyleSheet(f"border-left:1px solid {BORDER};")
        fmt_layout = QHBoxLayout(fmt_frame)
        fmt_layout.setContentsMargins(6, 0, 6, 0)
        fmt_layout.setSpacing(2)
        self._fmt_btns: Dict[str, QPushButton] = {}
        for fmt in COORD_FORMATS:
            fb = QPushButton(fmt)
            fb.setFixedSize(38, 24)
            fb.setCursor(Qt.CursorShape.PointingHandCursor)
            fb.setStyleSheet(self._fmt_style(fmt, fmt == "DD"))
            fb.clicked.connect(lambda _, f=fmt: self._set_coord_format(f))
            self._fmt_btns[fmt] = fb
            fmt_layout.addWidget(fb)
        layout.addWidget(fmt_frame)

        return bar

    @staticmethod
    def _tab_style(name: str, active: bool) -> str:
        if active:
            return (
                f"QPushButton{{background-color:#1a2332; color:{TEXT}; font-size:9pt;"
                f"border:1px solid {BORDER};}}"
            )
        return (
            f"QPushButton{{background-color:{BG}; color:{TEXT_DIM}; font-size:9pt;"
            f"border:1px solid {BORDER};}}"
        )

    @staticmethod
    def _fmt_style(fmt: str, active: bool) -> str:
        if active:
            return (
                f"QPushButton{{background-color:#1d3a5c; color:#38bdf8; font-size:7pt; font-weight:bold;"
                f"border:1px solid #38bdf8;}}"
            )
        return (
            f"QPushButton{{background-color:#1a2332; color:{TEXT_DIM}; font-size:7pt;"
            f"border:1px solid {BORDER};}}"
        )

    def _set_coord_format(self, fmt: str) -> None:
        self.state.coord_format = fmt
        for f, btn in self._fmt_btns.items():
            btn.setStyleSheet(self._fmt_style(f, f == fmt))
        if self.app:
            self.app._markers_dirty = True

    def _switch_tab(self, name: str) -> None:
        nav_map = {"Site": 0, "Network": 1, "Logs": 2, "NOKTALAR": 3}
        idx = nav_map.get(name, 0)
        self._right_stack.setCurrentIndex(idx)
        for t, btn in self._tab_btns.items():
            btn.setStyleSheet(self._nav_btn_style(t, t == name))

    @staticmethod
    def _nav_btn_style(name: str, active: bool) -> str:
        if active:
            return (
                "QPushButton{background:#112240;color:#7dd3fc;font-size:7pt;font-weight:bold;"
                "border:none;border-right:3px solid #38bdf8;padding:2px 0;}"
                "QPushButton:hover{background:#112240;}"
            )
        return (
            f"QPushButton{{background:transparent;color:{TEXT_DIM};font-size:7pt;"
            "border:none;border-right:3px solid transparent;padding:2px 0;}}"
            f"QPushButton:hover{{background:#161f2c;color:{TEXT};}}"
        )

    def _build_nav_rail(self) -> QFrame:
        rail = QFrame()
        rail.setFixedWidth(80)
        rail.setStyleSheet(f"background:#151c25; border-right:1px solid {BORDER};")
        layout = QVBoxLayout(rail)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        _NAV = [
            ("⊞", "Site"),
            ("📡", "Network"),
            ("≡",  "Logs"),
            ("◉",  "NOKTALAR"),
        ]
        for icon, name in _NAV:
            btn = QPushButton(f"{icon}\n{name}")
            btn.setFixedSize(80, 52)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet(self._nav_btn_style(name, name == "Site"))
            btn.clicked.connect(lambda _, t=name: self._switch_tab(t))
            self._tab_btns[name] = btn
            layout.addWidget(btn)

        layout.addStretch()
        return rail

    # ── Sol panel ─────────────────────────────────────────────────────────────

    def _build_left_panel(self) -> QFrame:
        panel = QFrame()
        panel.setFixedWidth(354)
        panel.setStyleSheet(f"background:#202b36; border-right:1px solid {BORDER};")

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        hdr = QLabel("  DEVICE MANAGEMENT")
        hdr.setFixedHeight(38)
        hdr.setStyleSheet(
            f"color:{TEXT}; font-size:10pt; font-weight:bold;"
            f"background:#26323e; padding-left:8px;"
        )
        layout.addWidget(hdr)

        list_header = QFrame()
        list_header.setFixedHeight(34)
        list_header.setStyleSheet(f"background:#2b3743; border-bottom:1px solid {BORDER};")
        list_header_layout = QHBoxLayout(list_header)
        list_header_layout.setContentsMargins(12, 0, 10, 0)
        list_header_layout.setSpacing(8)
        list_title = QLabel("DEVICE LIST")
        list_title.setStyleSheet(f"color:{TEXT}; font-size:10pt; font-weight:bold;")
        list_header_layout.addWidget(list_title)
        list_header_layout.addStretch()
        list_menu = QPushButton("+")
        list_menu.setFixedSize(28, 24)
        list_menu.setStyleSheet(
            f"QPushButton{{background-color:#1a2332; color:{TEXT_DIM}; font-size:15pt;font-weight:bold;"
            f"border:1px solid #1a2332;}}"
            f"QPushButton:hover{{background-color:#2d3748; color:{TEXT};}}"
        )
        list_menu.setCursor(Qt.CursorShape.PointingHandCursor)
        list_menu.clicked.connect(self._toggle_add_panel)
        list_header_layout.addWidget(list_menu)
        layout.addWidget(list_header)

        # Sütun başlıkları
        col_hdr = QFrame()
        col_hdr.setFixedHeight(28)
        col_hdr.setStyleSheet(f"background:#151c25; border-bottom:1px solid {BORDER};")
        col_row = QHBoxLayout(col_hdr)
        col_row.setContentsMargins(8, 0, 6, 0)
        col_row.setSpacing(0)
        for col, width in (
            ("STATUS", DeviceCard._W_STATUS),
            ("ID", DeviceCard._W_ID),
            ("IP/PORT", DeviceCard._W_IP),
            ("MODES", DeviceCard._W_MODES),
        ):
            lbl_w = QLabel(col)
            lbl_w.setFixedWidth(width)
            lbl_w.setStyleSheet(f"color:{TEXT_DIM}; font-size:7pt; font-weight:bold; letter-spacing:0.5px;")
            col_row.addWidget(lbl_w)
        layout.addWidget(col_hdr)

        # Cihaz listesi (kaydırılabilir)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"background:#202b36; border:none;")
        self._device_list_widget = QWidget()
        self._device_list_widget.setStyleSheet(f"background:#202b36;")
        self._device_list_layout = QVBoxLayout(self._device_list_widget)
        self._device_list_layout.setContentsMargins(8, 6, 8, 6)
        self._device_list_layout.setSpacing(4)
        self._device_list_layout.addStretch()
        scroll.setWidget(self._device_list_widget)
        layout.addWidget(scroll, stretch=1)

        self._add_frame = QFrame()
        add_frame = self._add_frame
        add_frame.setStyleSheet(f"background:#182230; border-top:1px solid {BORDER};")
        add_layout = QVBoxLayout(add_frame)
        add_layout.setContentsMargins(10, 8, 10, 10)
        add_layout.setSpacing(5)

        add_layout.addWidget(label(add_frame, "ADD DEVICE", TEXT_DIM, bold=True, size=7))

        add_layout.addWidget(label(add_frame, "Cihaz Adi / ID", TEXT_DIM, size=8))
        self.name_input = QLineEdit(f"GPS-{len(self.state.devices) + 1}")
        self.name_input.setStyleSheet(
            f"background-color:#2d3748; color:{TEXT}; border:1px solid {BORDER};"
            f"border-radius:4px; padding:5px 8px; font-size:9pt;"
        )
        add_layout.addWidget(self.name_input)

        add_layout.addWidget(label(add_frame, "IP Address", TEXT_DIM, size=8))
        self.ip_input = QLineEdit("192.168.1.10")
        self.ip_input.setStyleSheet(
            f"background-color:#2d3748; color:{TEXT}; border:1px solid {BORDER};"
            f"border-radius:4px; padding:5px 8px; font-size:9pt;"
        )
        add_layout.addWidget(self.ip_input)

        add_layout.addWidget(label(add_frame, "Port", TEXT_DIM, size=8))
        self.port_input = QLineEdit("5000")
        self.port_input.setStyleSheet(
            f"background-color:#2d3748; color:{TEXT}; border:1px solid {BORDER};"
            f"border-radius:4px; padding:5px 8px; font-size:9pt;"
        )
        add_layout.addWidget(self.port_input)

        add_btn = QPushButton("ADD")
        add_btn.setStyleSheet(
            f"background-color:{ORANGE}; color:#000; font-weight:bold; font-size:9pt;"
            f"border:1px solid #d97706;"
        )
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.clicked.connect(self._on_add_clicked)
        add_layout.addWidget(add_btn)
        layout.addWidget(add_frame)
        add_frame.hide()

        return panel

    def _on_add_clicked(self) -> None:
        if self.app:
            self.app.connect(self.ip_input.text(), self.port_input.text(), self.name_input.text())

    def _toggle_add_panel(self) -> None:
        if self._add_frame.isVisible():
            self._add_frame.hide()
        else:
            self._add_frame.show()

    def _on_lock_toggled(self, checked: bool) -> None:
        self.state.is_map_locked = checked
        if checked:
            self._lock_btn.setText("🔒")
            self._lock_btn.setStyleSheet(
                f"QPushButton{{background:{ORANGE};color:#000;font-size:16pt;"
                "border:1px solid #d97706;border-radius:6px;}}"
                "QPushButton:hover{background:#f59e0b;}"
            )
        else:
            self._lock_btn.setText("🔓")
            self._lock_btn.setStyleSheet(
                f"QPushButton{{background:#1a2535;color:{GREEN};font-size:16pt;"
                f"border:1px solid {GREEN};border-radius:6px;}}"
                "QPushButton:hover{background:#1e2f40;}"
            )

    # ── Harita paneli ─────────────────────────────────────────────────────────

    def _build_map_panel(self) -> QFrame:
        panel = QFrame()
        panel.setStyleSheet(f"background:#111827;")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._map_wrap = QFrame()
        self._map_wrap.setStyleSheet("background:#0d1117;")
        layout.addWidget(self._map_wrap, stretch=1)

        self._web_view = QWebEngineView(self._map_wrap)
        self._web_view.setStyleSheet("background:#0d1117;")
        self._configure_web_view()
        self._web_view.loadFinished.connect(self._on_map_load_finished)

        toolbar = QFrame(self._map_wrap)
        self._map_toolbar = toolbar
        toolbar.setFixedHeight(52)
        toolbar.setStyleSheet(
            "QFrame {"
            "background:rgba(15,23,32,238);"
            f"border:1px solid {BORDER};"
            "border-radius:5px;"
            "}"
        )
        tb_layout = QHBoxLayout(toolbar)
        tb_layout.setContentsMargins(8, 3, 8, 3)
        tb_layout.setSpacing(4)

        self._active_tool: str = "Seç"
        self._active_map: str = "offline"
        self._tool_btns: Dict[str, QPushButton] = {}
        self._centroid_cycle_idx: int = -1

        _TOOLS: list = [
            ("⇔",  "Ölç",         "Ölç",    self._tool_measure),
            ("↺",  "Anlık",       "Anlık",  self._tool_live_measure),
            ("⚑",  "Bayrak Ekle", "Bayrak", self._tool_mark_flag),
            ("⌖",  "Hedef Nokta", "Hedef",  self._tool_target_point),
            ("📍", "Koordinat",   "Koord",  self._tool_coordinate),
            ("⊕",  "Orta Nokta",  "Orta",   self._tool_centroid),
            ("↖",  "Seç",         "Seç",    None),
            None,
            ("🗺", "Harita 1",    "H.1",    self._tool_map_offline),
            ("🌐", "Harita 2",    "H.2",    self._tool_map_online),
            ("⊞",  "Katman",      "Katman", self._tool_toggle_layer),
            ("⚙",  "Ayarlar",     "Ayar",   None),
        ]
        for item in _TOOLS:
            if item is None:
                sep = QFrame()
                sep.setFrameShape(QFrame.Shape.VLine)
                sep.setFixedWidth(1)
                sep.setFixedHeight(28)
                sep.setStyleSheet(f"background:{BORDER};margin:0 6px;")
                tb_layout.addWidget(sep)
                continue
            icon, lbl_text, short, handler = item
            is_map = lbl_text in ("Harita 1", "Harita 2")
            btn = QPushButton(f"{icon}\n{short}")
            btn.setFixedSize(46, 50)
            btn.setStyleSheet(self._tool_btn_style(lbl_text, lbl_text == "Seç", is_map))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(lbl_text)
            _h = handler
            _t = lbl_text
            if _h:
                btn.clicked.connect(lambda _, t=_t, h=_h: self._activate_tool(t, h))
            else:
                btn.clicked.connect(lambda _, t=_t: self._activate_tool(t))
            self._tool_btns[lbl_text] = btn
            tb_layout.addWidget(btn)

        tb_layout.addSpacing(6)

        self._lock_btn = QPushButton("🔓")
        self._lock_btn.setCheckable(True)
        self._lock_btn.setFixedSize(42, 42)
        self._lock_btn.setStyleSheet(
            f"QPushButton{{background:#1a2535;color:{GREEN};font-size:16pt;"
            f"border:1px solid {GREEN};border-radius:6px;}}"
            f"QPushButton:hover{{background:#1e2f40;}}"
        )
        self._lock_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._lock_btn.clicked.connect(self._on_lock_toggled)
        tb_layout.addWidget(self._lock_btn)

        self._refresh_map_btn_styles()

        # Katman seçimi Leaflet'in sağ üst kontrolünden yapılır. Eski dikey
        # overlay toolbar, katman menüsünün üstüne binip tıklamayı bozuyordu.
        self._map_side_tools = QFrame(self._map_wrap)
        self._map_side_tools.hide()

        # Heading overlay kartları
        self._heading_cards: List[HeadingCard] = []
        first_card = HeadingCard(
            self._map_wrap, self.state.primary_source_id, self.state.primary_target_id,
            on_add=self._add_heading_card,
            on_remove=self._remove_heading_card,
            get_devices=self._device_options,
        )
        first_card.show()
        self._heading_cards.append(first_card)

        # Compass overlay
        self._compass = CompassWidget(self._map_wrap)
        self._compass.show()

        # Zoom / ölçek overlay
        self._zoom_lbl = QLabel("Zoom: --\nÖlçek: --")
        self._zoom_lbl.setParent(self._map_wrap)
        self._zoom_lbl.setFixedSize(110, 38)
        self._zoom_lbl.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._zoom_lbl.setStyleSheet(
            f"background:rgba(15,23,32,220);color:{TEXT};font-size:8pt;"
            f"padding:4px 7px;border-radius:4px;border:1px solid {BORDER};"
        )

        QTimer.singleShot(0, self._layout_map_overlays)

        esc = QShortcut(QKeySequence("Escape"), self)
        esc.activated.connect(self._escape_current_tool)
        return panel

    @staticmethod
    def _tool_btn_style(name: str, active: bool, is_map: bool = False) -> str:
        if active:
            if is_map:
                bg, col, bb = "#0d2e1f", "#4ade80", "#10b981"
            else:
                bg, col, bb = "#0c1e3d", "#7dd3fc", "#3b82f6"
            return (
                f"QPushButton{{background:{bg};color:{col};font-size:13pt;"
                f"border:none;border-bottom:3px solid {bb};}}"
                f"QPushButton:hover{{background:{bg};}}"
            )
        return (
            "QPushButton{background:transparent;color:#64748b;font-size:13pt;"
            "border:none;border-bottom:3px solid transparent;}"
            "QPushButton:hover{background:#1a2332;color:#cbd5e1;"
            "border-bottom:3px solid #374151;}"
        )

    def _activate_tool(self, name: str, handler: Optional[Callable] = None) -> None:
        self._restore_toolbar_events()
        self._active_tool = name
        for t, btn in self._tool_btns.items():
            btn.setStyleSheet(self._tool_btn_style(t, t == name, t in ("Harita 1", "Harita 2")))
        if handler:
            handler()
        else:
            self._clear_map_tool()

    def _restore_toolbar_events(self) -> None:
        if self._measure_restore_timer is not None:
            self._measure_restore_timer.stop()
            self._measure_restore_timer = None
        self._map_toolbar.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)

    def _on_measure_check(self, has_cleanup: bool) -> None:
        if not has_cleanup:
            self._restore_toolbar_events()
            self._active_tool = "Seç"
            for t, btn in self._tool_btns.items():
                btn.setStyleSheet(self._tool_btn_style(t, t == "Seç", t in ("Harita 1", "Harita 2")))

    def _escape_current_tool(self) -> None:
        if self._active_tool not in ("Seç", "Ayarlar", "Katman"):
            self._activate_tool("Seç")

    def _clear_map_tool(self) -> None:
        self._restore_toolbar_events()
        js = (
            "window._gnssLiveMeasureMode=false;"
            "if(window._gnssMap && window._gnssToolCleanup){"
            "window._gnssToolCleanup();"
            "window._gnssToolCleanup=null;"
            "window._gnssMap.getContainer().style.cursor='';"
            "}"
        )
        if p := self._web_view.page():
            p.runJavaScript(js)

    def _tool_live_measure(self) -> None:
        js = (
            "if(window._gnssMap){"
            " if(window._gnssMultiSelect){"
            "  window._gnssMultiSelect.forEach(function(s){"
            "   if(window._gnssRestoreStyle)window._gnssRestoreStyle(s.group);});"
            "  window._gnssMultiSelect=[];"
            " }"
            " if(window._gnssRemoveMeasure)window._gnssRemoveMeasure();"
            " window._gnssLiveMeasureMode=true;"
            " window._gnssMap.getContainer().style.cursor='crosshair';"
            "}"
        )
        if p := self._web_view.page():
            p.runJavaScript(js)

    def _tool_measure(self) -> None:
        js = (
            "if(window._gnssMap){"
            "var m=window._gnssMap;"
            "if(window.gnssEnsureSelectableTools)window.gnssEnsureSelectableTools(m);"
            "if(window._gnssToolCleanup){window._gnssToolCleanup();}"
            "if(!window._gnssMeasurements) window._gnssMeasurements=[];"
            "window._gnssMeasureActive=true;"
            "window._gnssMeasureSnapKey=null;"
            "window._gnssMeasureP1Key=null;"
            "var p1=null;"
            "var tempLine=null;"
            "var startLayer=null;"
            "var liveLabel=null;"
            "var overlayPane=m.getPane('overlayPane'),shadowPane=m.getPane('shadowPane');"
            "var oldOverlayPointer=overlayPane?overlayPane.style.pointerEvents:'';"
            "var oldShadowPointer=shadowPane?shadowPane.style.pointerEvents:'';"
            "if(overlayPane)overlayPane.style.pointerEvents='none';"
            "if(shadowPane)shadowPane.style.pointerEvents='none';"
            "var panel=L.control({position:'topleft'});"
            "panel.onAdd=function(){var d=L.DomUtil.create('div','gnss-ruler-panel');d.innerHTML='<strong>Cetvel</strong>1. koordinati sec';return d;};"
            "panel.addTo(m);"
            "function setPanel(text){try{panel.getContainer().innerHTML='<strong>Cetvel</strong>'+text;}catch(e){}}"
            "function distText(meters){return meters>=1000?(meters/1000).toFixed(3)+' km':meters.toFixed(2)+' m';}"
            "function haversine(a,b){"
            " var R=6371008.8;"
            " var p1=a.lat*Math.PI/180,p2=b.lat*Math.PI/180;"
            " var dp=(b.lat-a.lat)*Math.PI/180,dl=(b.lng-a.lng)*Math.PI/180;"
            " var x=Math.sin(dp/2)*Math.sin(dp/2)+Math.cos(p1)*Math.cos(p2)*Math.sin(dl/2)*Math.sin(dl/2);"
            " return R*2*Math.atan2(Math.sqrt(x),Math.sqrt(1-x));"
            "}"
            "m.getContainer().style.cursor='crosshair';"
            "function finish(){"
            " m.off('click',onClick);"
            " m.off('mousemove',onMove);"
            " if(tempLine){m.removeLayer(tempLine);tempLine=null;}"
            " if(liveLabel){m.removeLayer(liveLabel);liveLabel=null;}"
            " try{m.removeControl(panel);}catch(e){}"
            " if(overlayPane)overlayPane.style.pointerEvents=oldOverlayPointer;"
            " if(shadowPane)shadowPane.style.pointerEvents=oldShadowPointer;"
            " m.getContainer().style.cursor='';"
            " window._gnssMeasureActive=false;"
            " var k1=window._gnssMeasureP1Key,k2=window._gnssMeasureSnapKey;"
            " if(k1&&k2&&window._gnssPmk&&window._gnssPmk[k1]&&window._gnssPmk[k2]){"
            "  window._gnssMultiSelect=[];"
            "  var g1=(window._gnssSelectableGroups||[]).find(function(g){return g.key===k1;});"
            "  var g2=(window._gnssSelectableGroups||[]).find(function(g){return g.key===k2;});"
            "  if(g1){window._gnssHighlightMulti(g1);window._gnssMultiSelect.push({group:g1,key:k1});}"
            "  if(g2){window._gnssHighlightMulti(g2);window._gnssMultiSelect.push({group:g2,key:k2});}"
            "  if(window._gnssLiveUpdateMeasure)window._gnssLiveUpdateMeasure();"
            " }"
            " window._gnssMeasureP1Key=null;window._gnssMeasureSnapKey=null;"
            " window._gnssToolCleanup=null;"
            "}"
            "function onMove(ev){"
            " if(!p1)return;"
            " var p2=ev.latlng;"
            " var d=haversine(p1,p2);"
            " if(!tempLine){tempLine=L.polyline([p1,p2],{color:'#facc15',weight:2,dashArray:'5 4',opacity:0.85,interactive:false}).addTo(m);}else{tempLine.setLatLngs([p1,p2]);}"
            " var mid=L.latLng((p1.lat+p2.lat)/2,(p1.lng+p2.lng)/2);"
            " if(!liveLabel){liveLabel=L.marker(mid,{interactive:false,icon:L.divIcon({className:'gnss-measure-label',html:'<div>'+distText(d)+'</div>',iconSize:null})}).addTo(m);}else{liveLabel.setLatLng(mid);liveLabel.setIcon(L.divIcon({className:'gnss-measure-label',html:'<div>'+distText(d)+'</div>',iconSize:null}));}"
            " setPanel('2. koordinati sec<br>'+distText(d));"
            "}"
            "function onClick(e){"
            " if(e.originalEvent){L.DomEvent.stop(e.originalEvent);}"
            " if(!p1){"
            "  p1=e.latlng;"
            "  window._gnssMeasureP1Key=window._gnssMeasureSnapKey;"
            "  window._gnssMeasureSnapKey=null;"
            "  startLayer=L.circleMarker(p1,{radius:6,color:'#facc15',fillColor:'#facc15',fillOpacity:1,weight:2}).addTo(m);"
            "  window._gnssMeasurements.push(startLayer);"
            "  setPanel('2. koordinati sec');"
            "  return;"
            " }"
            " var p2=e.latlng;"
            " if(tempLine){m.removeLayer(tempLine);tempLine=null;}"
            " if(liveLabel){m.removeLayer(liveLabel);liveLabel=null;}"
            "var dist=haversine(p1,p2);"
            "var distTxt=distText(dist);"
            "var line=L.polyline([p1,p2],{color:'#facc15',weight:4,opacity:1}).addTo(m);"
            "var end=L.circleMarker(p2,{radius:6,color:'#facc15',fillColor:'#facc15',fillOpacity:1,weight:2}).addTo(m);"
            "var mid=L.latLng((p1.lat+p2.lat)/2,(p1.lng+p2.lng)/2);"
            "var label=L.marker(mid,{icon:L.divIcon({className:'gnss-measure-label',html:'<div>'+distTxt+'</div>',iconSize:null})}).addTo(m);"
            "var result=L.popup({closeButton:true,autoClose:false,closeOnClick:false,className:'gnss-command-popup'})"
            ".setLatLng(p2).setContent('<div class=\"gnss-popup-card\"><div class=\"gnss-popup-title\"><span>MESAFE</span><span class=\"gnss-popup-close\">×</span></div><div class=\"gnss-popup-body\"><div class=\"gnss-popup-row\"><span class=\"gnss-popup-key\">Mesafe</span><span class=\"gnss-popup-val\">'+distTxt+'</span></div><div class=\"gnss-popup-row\"><span class=\"gnss-popup-key\">Başlangıç</span><span class=\"gnss-popup-val\">'+p1.lat.toFixed(8)+', '+p1.lng.toFixed(8)+'</span></div><div class=\"gnss-popup-row\"><span class=\"gnss-popup-key\">Bitiş</span><span class=\"gnss-popup-val\">'+p2.lat.toFixed(8)+', '+p2.lng.toFixed(8)+'</span></div></div></div>').openOn(m);"
            "setTimeout(function(){var el=result.getElement();if(!el)return;var x=el.querySelector('.gnss-popup-close');if(x)x.onclick=function(){m.closePopup(result);};},0);"
            "window._gnssMeasurements.push(line);"
            "window._gnssMeasurements.push(end);"
            "window._gnssMeasurements.push(label);"
            "window._gnssRegisterSelectable([startLayer,line,end,label],'measure');"
            " finish();"
            "}"
            "m.on('click',onClick);"
            "m.on('mousemove',onMove);"
            "window._gnssToolCleanup=finish;"
            "}"
        )
        if p := self._web_view.page():
            p.runJavaScript(js)
        # Toolbar'ı mouse event'lerine şeffaf yap — ölçüm sırasında toolbar butonları
        # üzerinde tıklanınca click haritaya geçsin, butona değil
        self._map_toolbar.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        # JS finish() ne zaman çağrıldığını 300ms'de bir kontrol et, bitti mi diye
        if self._measure_restore_timer is None:
            self._measure_restore_timer = QTimer(self)
            self._measure_restore_timer.setInterval(300)
            self._measure_restore_timer.timeout.connect(self._poll_measure_done)
        self._measure_restore_timer.start()

    def _poll_measure_done(self) -> None:
        if p := self._web_view.page():
            p.runJavaScript("!!window._gnssToolCleanup", self._on_measure_check)

    def _tool_mark_flag(self) -> None:
        js = (
            "if(window._gnssMap){"
            "var m=window._gnssMap;"
            "if(window.gnssEnsureSelectableTools)window.gnssEnsureSelectableTools(m);"
            "if(window._gnssToolCleanup){window._gnssToolCleanup();}"
            "if(!window._gnssUserFlags) window._gnssUserFlags=[];"
            "function esc(v){return String(v).replace(/</g,'&lt;').replace(/>/g,'&gt;');}"
            "function flagIcon(){return L.divIcon({className:'gnss-flag-icon',html:'⚑',iconSize:[28,28],iconAnchor:[6,24]});}"
            "function labelIcon(text,cls){return L.divIcon({className:cls,html:text,iconSize:null});}"
            "function fmtCoord(v,axis){if(window._gnssFmtCoord)return window._gnssFmtCoord(v,axis);"
            " var fmt=window._gnssCoordFmt||'DD';"
            " var a=Math.abs(v),h=v>=0?(axis==='lat'?'N':'E'):(axis==='lat'?'S':'W');"
            " var d=Math.floor(a),mf=(a-d)*60,mnt=Math.floor(mf),s=(mf-mnt)*60;"
            " var dw=axis==='lat'?2:3,ds=String(d).padStart(dw,'0');"
            " if(fmt==='DDM')return ds+'° '+mf.toFixed(4).padStart(7,'0')+\"' \"+h;"
            " if(fmt==='DMS')return ds+'° '+String(mnt).padStart(2,'0')+\"' \"+s.toFixed(2).padStart(5,'0')+'\" '+h;"
            " if(fmt==='NMEA'){var base=d*100+mf;return base.toFixed(4).padStart(axis==='lat'?7:8,'0')+' '+h;}"
            " return a.toFixed(7)+'° '+h;}"
            "function parseCoord(str,axis){"
            " if(!str)return NaN;str=str.trim();"
            " if(/^-?\\d+\\.?\\d*$/.test(str))return parseFloat(str);"
            " var neg=/[SW]/i.test(str);"
            " var nums=str.replace(/[°'\"NSEW]/gi,' ').trim().split(/\\s+/).filter(function(p){return p.length>0;}).map(parseFloat).filter(function(n){return !isNaN(n);});"
            " var fmt=window._gnssCoordFmt||'DD';var dec;"
            " if(nums.length===1){if(fmt==='NMEA'){var dv=Math.floor(nums[0]/100);dec=dv+(nums[0]-dv*100)/60;}else dec=nums[0];}"
            " else if(nums.length===2)dec=nums[0]+nums[1]/60;"
            " else if(nums.length===3)dec=nums[0]+nums[1]/60+nums[2]/3600;"
            " else return NaN;"
            " return neg?-dec:dec;}"
            "function normalizeCoordStr(s){return s.replace(/\\u2032/g,\"'\").replace(/\\u2033/g,'\"').replace(/\\(North\\)/gi,'N').replace(/\\(South\\)/gi,'S').replace(/\\(East\\)/gi,'E').replace(/\\(West\\)/gi,'W').replace(/\\r\\n/g,'\\n');}"
            "function parseCoordSmart(s,ax){s=s.trim().replace(/(\\d)-(\\d)/g,'$1 $2');return parseCoord(s,ax);}"
            "function extractAlt(s){var m2=s.match(/(?:Height|Alt(?:itude)?|Y\\u00fckseklik)[\\s\\t:=]+([0-9.]+)/i);return m2?m2[1]:null;}"
            "function tryParsePair(s){"
            " s=normalizeCoordStr(s.trim());"
            " function ok(a,b){return !isNaN(a)&&!isNaN(b)&&Math.abs(a)<=90&&Math.abs(b)<=180;}"
            " var pa,pb;"
            " var lM=s.match(/(?:Lat(?:itude)?|Enlem)[\\s\\t:=]+([^\\n\\r]+)/i);"
            " var oM=s.match(/(?:Lon(?:gitude)?|Boylam)[\\s\\t:=]+([^\\n\\r]+)/i);"
            " if(lM&&oM){pa=parseCoordSmart(lM[1].trim(),'lat');pb=parseCoordSmart(oM[1].trim(),'lon');if(ok(pa,pb))return{lat:pa,lon:pb,alt:extractAlt(s)};}"
            " var bc=s.split(',');if(bc.length===2){pa=parseCoordSmart(bc[0].trim(),'lat');pb=parseCoordSmart(bc[1].trim(),'lon');if(ok(pa,pb))return{lat:pa,lon:pb,alt:null};}"
            " var m2=s.match(/(.+?[NS])\\s+(.+?[EW])/i);if(m2){pa=parseCoordSmart(m2[1].trim(),'lat');pb=parseCoordSmart(m2[2].trim(),'lon');if(ok(pa,pb))return{lat:pa,lon:pb,alt:extractAlt(s)};}"
            " var ns=s.match(/-?\\d+\\.?\\d*/g);if(ns&&ns.length>=2){pa=parseFloat(ns[0]);pb=parseFloat(ns[1]);if(ok(pa,pb))return{lat:pa,lon:pb,alt:ns[2]||null};}"
            " return null;}"
            "function card(name,lat,lon,alt,note){return '<div class=\"gnss-popup-card\"><div class=\"gnss-popup-title\"><span>'+esc(name)+'</span><span class=\"gnss-popup-close\">×</span></div><div class=\"gnss-popup-body\"><div class=\"gnss-popup-row\"><span class=\"gnss-popup-key\">Latitude</span><span class=\"gnss-popup-val\">'+fmtCoord(lat,'lat')+'</span></div><div class=\"gnss-popup-row\"><span class=\"gnss-popup-key\">Longitude</span><span class=\"gnss-popup-val\">'+fmtCoord(lon,'lon')+'</span></div><div class=\"gnss-popup-row\"><span class=\"gnss-popup-key\">Alt</span><span class=\"gnss-popup-val\">'+esc(alt||'-')+'</span></div><div class=\"gnss-popup-row\"><span class=\"gnss-popup-key\">Not</span><span class=\"gnss-popup-val\">'+esc(note||'-')+'</span></div></div></div>';}"
            "function form(lat,lon){var idx=Math.floor(window._gnssUserFlags.length/2)+1;var fmt=window._gnssCoordFmt||'DD';"
            "return '<div class=\"gnss-flag-form-card\"><div class=\"gnss-popup-title\"><span>BAYRAK EKLE</span><span class=\"gnss-popup-close\">×</span></div><div class=\"gnss-flag-form-body\">"
            "<label>Ad<input class=\"gnss-flag-name\" value=\"FLAG-'+String(idx).padStart(2,'0')+'\">"
            "</label><label>Lat ['+fmt+']<input class=\"gnss-flag-lat\" value=\"'+fmtCoord(lat,'lat')+'\"></label>"
            "<label>Lon ['+fmt+']<input class=\"gnss-flag-lon\" value=\"'+fmtCoord(lon,'lon')+'\"></label>"
            "<label>Yükseklik<input class=\"gnss-flag-alt\" value=\"0.0 m\"></label>"
            "<label>Not<input class=\"gnss-flag-note\" value=\"\"></label>"
            "<div class=\"gnss-flag-actions\"><span class=\"gnss-popup-btn gnss-cancel-flag\">İptal</span><span class=\"gnss-popup-btn gnss-add-flag\">Ekle</span></div>"
            "</div></div>';}"
            "function bindPopup(popup){setTimeout(function(){var el=popup.getElement();if(!el)return;el.querySelectorAll('.gnss-popup-close,.gnss-cancel-flag').forEach(function(close){close.onclick=function(){m.closePopup(popup);};});var add=el.querySelector('.gnss-add-flag');if(add)add.onclick=function(){var name=el.querySelector('.gnss-flag-name').value||'FLAG';var lat=parseCoord(el.querySelector('.gnss-flag-lat').value,'lat');var lon=parseCoord(el.querySelector('.gnss-flag-lon').value,'lon');var alt=el.querySelector('.gnss-flag-alt').value;var note=el.querySelector('.gnss-flag-note').value;if(isNaN(lat)||isNaN(lon))return;var marker=L.marker([lat,lon],{icon:flagIcon(),zIndexOffset:1300}).bindPopup(card(name,lat,lon,alt,note),{closeButton:true,autoClose:false,closeOnClick:false,autoPan:false,className:'gnss-command-popup'}).addTo(m);var label=L.marker([lat,lon],{icon:labelIcon(esc(name),'gnss-map-label'),zIndexOffset:1299}).addTo(m);window._gnssUserFlags.push(marker);window._gnssUserFlags.push(label);if(!window._gnssUserFlagData)window._gnssUserFlagData=[];window._gnssUserFlagData.push({name:name,lat:lat,lon:lon,alt:alt,note:note});window._gnssRegisterSelectable([marker,label],'flag','flag:'+name);marker.off('click',marker._openPopup,marker);marker.on('dblclick',function(e){L.DomEvent.stop(e.originalEvent);marker.openPopup();});m.closePopup(popup);};"
            "var _li=el.querySelector('.gnss-flag-lat'),_lo=el.querySelector('.gnss-flag-lon');"
            "if(_li&&_lo){_li.addEventListener('paste',function(ev){var txt=(ev.clipboardData||window.clipboardData).getData('text');var pr=tryParsePair(txt);if(pr){ev.preventDefault();_li.value=fmtCoord(pr.lat,'lat');_lo.value=fmtCoord(pr.lon,'lon');var _la=el.querySelector('.gnss-flag-alt');if(_la&&pr.alt)_la.value=pr.alt+' m';}});}"
            "},0);}"
            "m.getContainer().style.cursor='crosshair';"
            "function finish(){m.off('click',onClick);m.getContainer().style.cursor='';window._gnssToolCleanup=null;}"
            "function onClick(e){if(e.originalEvent){L.DomEvent.stop(e.originalEvent);}var popup=L.popup({closeButton:true,autoClose:false,closeOnClick:false,className:'gnss-command-popup',autoPan:false}).setLatLng(e.latlng).setContent(form(e.latlng.lat,e.latlng.lng)).openOn(m);bindPopup(popup);finish();}"
            "m.on('click',onClick);"
            "window._gnssToolCleanup=finish;"
            "}"
        )
        if p := self._web_view.page():
            p.runJavaScript(js)
        if self._measure_restore_timer is None:
            self._measure_restore_timer = QTimer(self)
            self._measure_restore_timer.setInterval(300)
            self._measure_restore_timer.timeout.connect(self._poll_measure_done)
        self._measure_restore_timer.start()

    def _tool_target_point(self) -> None:
        js = (
            "if(window._gnssMap){"
            "var m=window._gnssMap;"
            "if(window.gnssEnsureSelectableTools)window.gnssEnsureSelectableTools(m);"
            "if(window._gnssToolCleanup){window._gnssToolCleanup();}"
            "if(!window._gnssTargets) window._gnssTargets=[];"
            "m.getContainer().style.cursor='crosshair';"
            "function esc(v){return String(v).replace(/</g,'&lt;').replace(/>/g,'&gt;');}"
            "function targetIcon(){return L.divIcon({className:'gnss-target-icon',html:'',iconSize:[28,28],iconAnchor:[14,14]});}"
            "function fmtCoord(v,axis){if(window._gnssFmtCoord)return window._gnssFmtCoord(v,axis);"
            " var fmt=window._gnssCoordFmt||'DD';"
            " var a=Math.abs(v),h=v>=0?(axis==='lat'?'N':'E'):(axis==='lat'?'S':'W');"
            " var d=Math.floor(a),mf=(a-d)*60,mnt=Math.floor(mf),s=(mf-mnt)*60;"
            " var dw=axis==='lat'?2:3,ds=String(d).padStart(dw,'0');"
            " if(fmt==='DDM')return ds+'° '+mf.toFixed(4).padStart(7,'0')+\"' \"+h;"
            " if(fmt==='DMS')return ds+'° '+String(mnt).padStart(2,'0')+\"' \"+s.toFixed(2).padStart(5,'0')+'\" '+h;"
            " if(fmt==='NMEA'){var base=d*100+mf;return base.toFixed(4).padStart(axis==='lat'?7:8,'0')+' '+h;}"
            " return a.toFixed(7)+'° '+h;}"
            "function parseCoord(str,axis){"
            " if(!str)return NaN;str=str.trim();"
            " if(/^-?\\d+\\.?\\d*$/.test(str))return parseFloat(str);"
            " var neg=/[SW]/i.test(str);"
            " var nums=str.replace(/[°'\"NSEW]/gi,' ').trim().split(/\\s+/).filter(function(p){return p.length>0;}).map(parseFloat).filter(function(n){return !isNaN(n);});"
            " var fmt=window._gnssCoordFmt||'DD';var dec;"
            " if(nums.length===1){if(fmt==='NMEA'){var dv=Math.floor(nums[0]/100);dec=dv+(nums[0]-dv*100)/60;}else dec=nums[0];}"
            " else if(nums.length===2)dec=nums[0]+nums[1]/60;"
            " else if(nums.length===3)dec=nums[0]+nums[1]/60+nums[2]/3600;"
            " else return NaN;"
            " return neg?-dec:dec;}"
            "function normalizeCoordStr(s){return s.replace(/\\u2032/g,\"'\").replace(/\\u2033/g,'\"').replace(/\\(North\\)/gi,'N').replace(/\\(South\\)/gi,'S').replace(/\\(East\\)/gi,'E').replace(/\\(West\\)/gi,'W').replace(/\\r\\n/g,'\\n');}"
            "function parseCoordSmart(s,ax){s=s.trim().replace(/(\\d)-(\\d)/g,'$1 $2');return parseCoord(s,ax);}"
            "function extractAlt(s){var m2=s.match(/(?:Height|Alt(?:itude)?|Y\\u00fckseklik)[\\s\\t:=]+([0-9.]+)/i);return m2?m2[1]:null;}"
            "function tryParsePair(s){"
            " s=normalizeCoordStr(s.trim());"
            " function ok(a,b){return !isNaN(a)&&!isNaN(b)&&Math.abs(a)<=90&&Math.abs(b)<=180;}"
            " var pa,pb;"
            " var lM=s.match(/(?:Lat(?:itude)?|Enlem)[\\s\\t:=]+([^\\n\\r]+)/i);"
            " var oM=s.match(/(?:Lon(?:gitude)?|Boylam)[\\s\\t:=]+([^\\n\\r]+)/i);"
            " if(lM&&oM){pa=parseCoordSmart(lM[1].trim(),'lat');pb=parseCoordSmart(oM[1].trim(),'lon');if(ok(pa,pb))return{lat:pa,lon:pb,alt:extractAlt(s)};}"
            " var bc=s.split(',');if(bc.length===2){pa=parseCoordSmart(bc[0].trim(),'lat');pb=parseCoordSmart(bc[1].trim(),'lon');if(ok(pa,pb))return{lat:pa,lon:pb,alt:null};}"
            " var m2=s.match(/(.+?[NS])\\s+(.+?[EW])/i);if(m2){pa=parseCoordSmart(m2[1].trim(),'lat');pb=parseCoordSmart(m2[2].trim(),'lon');if(ok(pa,pb))return{lat:pa,lon:pb,alt:extractAlt(s)};}"
            " var ns=s.match(/-?\\d+\\.?\\d*/g);if(ns&&ns.length>=2){pa=parseFloat(ns[0]);pb=parseFloat(ns[1]);if(ok(pa,pb))return{lat:pa,lon:pb,alt:ns[2]||null};}"
            " return null;}"
            "function infoCard(name,lat,lon,alt,note){return '<div class=\"gnss-popup-card\"><div class=\"gnss-popup-title\"><span>'+esc(name)+'</span><span class=\"gnss-popup-close\">×</span></div><div class=\"gnss-popup-body\"><div class=\"gnss-popup-row\"><span class=\"gnss-popup-key\">Latitude</span><span class=\"gnss-popup-val\">'+fmtCoord(lat,'lat')+'</span></div><div class=\"gnss-popup-row\"><span class=\"gnss-popup-key\">Longitude</span><span class=\"gnss-popup-val\">'+fmtCoord(lon,'lon')+'</span></div><div class=\"gnss-popup-row\"><span class=\"gnss-popup-key\">Alt</span><span class=\"gnss-popup-val\">'+esc(alt||'-')+'</span></div><div class=\"gnss-popup-row\"><span class=\"gnss-popup-key\">Not</span><span class=\"gnss-popup-val\">'+esc(note||'-')+'</span></div></div></div>';}"
            "function form(lat,lon){var idx=Math.floor(window._gnssTargets.length/2)+1;var fmt=window._gnssCoordFmt||'DD';"
            "return '<div class=\"gnss-flag-form-card\"><div class=\"gnss-popup-title\"><span>HEDEF NOKTA</span><span class=\"gnss-popup-close\">×</span></div><div class=\"gnss-flag-form-body\">"
            "<label>Ad<input class=\"gnss-target-name\" value=\"HEDEF-'+idx+'\">"
            "</label><label>Lat ['+fmt+']<input class=\"gnss-target-lat\" value=\"'+fmtCoord(lat,'lat')+'\"></label>"
            "<label>Lon ['+fmt+']<input class=\"gnss-target-lon\" value=\"'+fmtCoord(lon,'lon')+'\"></label>"
            "<label>Yükseklik<input class=\"gnss-target-alt\" value=\"0.0 m\"></label>"
            "<label>Not<input class=\"gnss-target-note\" value=\"\"></label>"
            "<div class=\"gnss-flag-actions\"><span class=\"gnss-popup-btn gnss-cancel-target\">İptal</span><span class=\"gnss-popup-btn gnss-add-target\">Ekle</span></div>"
            "</div></div>';}"
            "function bindPopup(popup){setTimeout(function(){var el=popup.getElement();if(!el)return;el.querySelectorAll('.gnss-popup-close,.gnss-cancel-target').forEach(function(close){close.onclick=function(){m.closePopup(popup);};});var add=el.querySelector('.gnss-add-target');if(add)add.onclick=function(){var name=el.querySelector('.gnss-target-name').value||'HEDEF';var lat=parseCoord(el.querySelector('.gnss-target-lat').value,'lat');var lon=parseCoord(el.querySelector('.gnss-target-lon').value,'lon');var alt=el.querySelector('.gnss-target-alt').value;var note=el.querySelector('.gnss-target-note').value;if(isNaN(lat)||isNaN(lon))return;var marker=L.marker([lat,lon],{icon:targetIcon(),zIndexOffset:1300}).bindPopup(infoCard(name,lat,lon,alt,note),{closeButton:true,autoClose:false,closeOnClick:false,autoPan:false,className:'gnss-command-popup'}).addTo(m);var label=L.marker([lat,lon],{icon:L.divIcon({className:'gnss-map-label',html:esc(name),iconSize:null}),zIndexOffset:1299}).addTo(m);window._gnssTargets.push(marker);window._gnssTargets.push(label);if(!window._gnssTargetData)window._gnssTargetData=[];window._gnssTargetData.push({name:name,lat:lat,lon:lon,alt:alt,note:note});window._gnssRegisterSelectable([marker,label],'target','target:'+name);marker.off('click',marker._openPopup,marker);marker.on('dblclick',function(e){L.DomEvent.stop(e.originalEvent);marker.openPopup();});m.closePopup(popup);};"
            "var _li=el.querySelector('.gnss-target-lat'),_lo=el.querySelector('.gnss-target-lon');"
            "if(_li&&_lo){_li.addEventListener('paste',function(ev){var txt=(ev.clipboardData||window.clipboardData).getData('text');var pr=tryParsePair(txt);if(pr){ev.preventDefault();_li.value=fmtCoord(pr.lat,'lat');_lo.value=fmtCoord(pr.lon,'lon');var _la=el.querySelector('.gnss-target-alt');if(_la&&pr.alt)_la.value=pr.alt+' m';}});}"
            "},0);}"
            "function finish(){m.off('click',onClick);m.getContainer().style.cursor='';window._gnssToolCleanup=null;}"
            "function onClick(e){if(e.originalEvent){L.DomEvent.stop(e.originalEvent);}var popup=L.popup({closeButton:true,autoClose:false,closeOnClick:false,className:'gnss-command-popup',autoPan:false}).setLatLng(e.latlng).setContent(form(e.latlng.lat,e.latlng.lng)).openOn(m);bindPopup(popup);finish();}"
            "m.on('click',onClick);"
            "window._gnssToolCleanup=finish;"
            "}"
        )
        if p := self._web_view.page():
            p.runJavaScript(js)
        if self._measure_restore_timer is None:
            self._measure_restore_timer = QTimer(self)
            self._measure_restore_timer.setInterval(300)
            self._measure_restore_timer.timeout.connect(self._poll_measure_done)
        self._measure_restore_timer.start()

    def _tool_coordinate(self) -> None:
        js = (
            "if(window._gnssMap){"
            "var m=window._gnssMap;"
            "if(window._gnssToolCleanup){window._gnssToolCleanup();}"
            "m.getContainer().style.cursor='crosshair';"
            "function finish(){m.off('click',onClick);m.getContainer().style.cursor='';window._gnssToolCleanup=null;}"
            "function onClick(e){"
            " if(e.originalEvent){L.DomEvent.stop(e.originalEvent);}"
            " var lat=e.latlng.lat,lon=e.latlng.lng;"
            " var html='<div class=\"gnss-popup-card\"><div class=\"gnss-popup-title\"><span>KOORDİNAT</span><span class=\"gnss-popup-close\">×</span></div><div class=\"gnss-popup-body\"><div class=\"gnss-popup-row\"><span class=\"gnss-popup-key\">Latitude</span><span class=\"gnss-popup-val\">'+lat.toFixed(8)+'</span></div><div class=\"gnss-popup-row\"><span class=\"gnss-popup-key\">Longitude</span><span class=\"gnss-popup-val\">'+lon.toFixed(8)+'</span></div></div></div>';"
            " var popup=L.popup({closeButton:true,autoClose:false,closeOnClick:false,className:'gnss-command-popup',autoPan:false}).setLatLng(e.latlng).setContent(html).openOn(m);"
            " setTimeout(function(){var el=popup.getElement();if(!el)return;var x=el.querySelector('.gnss-popup-close');if(x)x.onclick=function(){m.closePopup(popup);};},0);"
            " finish();"
            "}"
            "m.on('click',onClick);"
            "window._gnssToolCleanup=finish;"
            "}"
        )
        if p := self._web_view.page():
            p.runJavaScript(js)
        if self._measure_restore_timer is None:
            self._measure_restore_timer = QTimer(self)
            self._measure_restore_timer.setInterval(300)
            self._measure_restore_timer.timeout.connect(self._poll_measure_done)
        self._measure_restore_timer.start()

    def _tool_centroid(self) -> None:
        if self.app is None:
            return
        devices = sorted(self.app.latest_fixes.keys())
        if not devices:
            return
        self._centroid_cycle_idx = (self._centroid_cycle_idx + 1) % len(devices)
        name = devices[self._centroid_cycle_idx]
        fix = self.app.latest_fixes[name]
        lat, lon = fix.latitude, fix.longitude
        js = (
            f"if(window._gnssMap){{"
            f"window._gnssMap.setView([{lat},{lon}],Math.max(window._gnssMap.getZoom(),18));"
            f"}}"
        )
        if p := self._web_view.page():
            p.runJavaScript(js)

    def _tool_toggle_layer(self) -> None:
        js = (
            "if(window._gnssMap && typeof window._gnssToggleLayer==='function'){"
            "window._gnssToggleLayer();}"
        )
        if p := self._web_view.page():
            p.runJavaScript(js)

    def _tool_map_online(self) -> None:
        if p := self._web_view.page():
            p.runJavaScript("window._gnssSwitchMap&&window._gnssSwitchMap('online')")
        self._active_map = "online"
        self._refresh_map_btn_styles()

    def _tool_map_offline(self) -> None:
        if p := self._web_view.page():
            p.runJavaScript("window._gnssSwitchMap&&window._gnssSwitchMap('offline')")
        self._active_map = "offline"
        self._refresh_map_btn_styles()

    def _refresh_map_btn_styles(self) -> None:
        is_offline = self._active_map == "offline"
        for name, active in (("Harita 1", is_offline), ("Harita 2", not is_offline)):
            btn = self._tool_btns.get(name)
            if btn:
                btn.setStyleSheet(self._tool_btn_style(name, active, is_map=True))


    def _layout_map_overlays(self) -> None:
        if not hasattr(self, "_map_wrap"):
            return

        width = self._map_wrap.width()
        height = self._map_wrap.height()
        if width <= 0 or height <= 0:
            return

        self._web_view.setGeometry(0, 0, width, height)

        toolbar_width = min(860, max(610, width - 150))
        toolbar_x = max(12, (width - toolbar_width) // 2)
        self._map_toolbar.setGeometry(toolbar_x, 12, toolbar_width, 58)
        self._map_toolbar.raise_()

        self._map_side_tools.hide()

        # Heading overlay kartları — sol taraf, toolbar altı
        card_y = 82
        for hcard in self._heading_cards:
            hcard.setGeometry(12, card_y, 148, 86)
            hcard.raise_()
            card_y += 94

        # Compass overlay — sağ üst, toolbar altı
        self._compass.setGeometry(width - 142, 82, 130, 130)
        self._compass.raise_()

        # Zoom overlay — sol alt köşe
        self._zoom_lbl.setGeometry(12, height - 50, 110, 38)
        self._zoom_lbl.raise_()

    def _configure_web_view(self) -> None:
        settings = self._web_view.settings()
        if settings is None:
            return
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)

    def load_map(self, html_path: Path) -> None:
        if html_path.exists():
            url = QUrl.fromLocalFile(str(html_path.resolve()))
            self._web_view.load(url)
            self._map_mtime = html_path.stat().st_mtime

    def _on_map_load_finished(self, ok: bool) -> None:
        if not ok:
            self.state.last_error = "Map HTML yuklenemedi"
            return
        self.state.last_error = ""
        self._map_js_ready = False
        self._poll_for_map_ready()

    def _poll_for_map_ready(self) -> None:
        if p := self._web_view.page():
            p.runJavaScript("!!window._gnssMap", self._on_map_poll_result)

    def _on_map_poll_result(self, result: object) -> None:
        if result:
            self._mark_map_ready()
        else:
            QTimer.singleShot(300, self._poll_for_map_ready)

    def _mark_map_ready(self) -> None:
        self._map_js_ready = True
        if self.app:
            self.app._markers_dirty = True

    # ── Sağ panel (QStackedWidget) ────────────────────────────────────────────

    def _build_right_panel(self) -> QWidget:
        wrapper = QWidget()
        wrapper.setFixedWidth(260)
        wrapper.setStyleSheet(f"background:#202b36; border-left:1px solid {BORDER};")
        wl = QVBoxLayout(wrapper)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(0)

        self._right_stack = QStackedWidget()
        self._right_stack.setStyleSheet(f"background:#202b36;")
        self._right_stack.addWidget(self._build_site_panel())      # 0 Site
        self._right_stack.addWidget(self._build_network_panel())   # 1 Network
        self._right_stack.addWidget(self._build_logs_panel())      # 2 Logs
        self._right_stack.addWidget(self._build_noktalar_panel())  # 3 NOKTALAR
        wl.addWidget(self._right_stack)
        return wrapper

    def _build_site_panel(self) -> QWidget:
        panel = QWidget()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"background:{PANEL_BG}; border:none;")
        scroll.setWidget(panel)

        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        def sec_hdr(text: str) -> QLabel:
            lbl = QLabel(f"  {text}")
            lbl.setFixedHeight(26)
            lbl.setStyleSheet(
                f"background:#2b3743; color:{TEXT}; font-size:8pt; font-weight:bold;"
                f"border-radius:3px;"
            )
            return lbl

        def data_row(parent: QWidget, key: str, val: str = "-") -> tuple:
            rw = QWidget(parent)
            rw.setStyleSheet(f"background:{CARD_BG}; border-radius:3px;")
            rh = QHBoxLayout(rw)
            rh.setContentsMargins(8, 4, 8, 4)
            rh.setSpacing(6)
            kl = QLabel(key)
            kl.setStyleSheet(f"color:{TEXT_DIM}; font-size:8pt;")
            kl.setFixedWidth(90)
            vl = QLabel(val)
            vl.setStyleSheet(f"color:{TEXT}; font-size:9pt; font-weight:bold;")
            rh.addWidget(kl)
            rh.addWidget(vl)
            rh.addStretch()
            return rw, vl

        layout.addWidget(label(panel, "MEASUREMENTS & ANALYSIS", TEXT, bold=True, size=9))
        layout.addWidget(hline(panel))

        # ── GPS cihazları + seçici ────────────────────────────────
        layout.addWidget(sec_hdr("GPS CİHAZLARI"))
        _cb_style = (
            f"QComboBox{{background:{CARD_BG};color:{TEXT};border:1px solid {BORDER};"
            f"border-radius:4px;padding:2px 6px;font-size:8pt;}}"
            f"QComboBox::drop-down{{border:none;width:14px;}}"
            f"QComboBox QAbstractItemView{{background:#1a2535;color:{TEXT};"
            f"selection-background-color:#1d4ed8;}}"
        )
        self._src_combo = QComboBox()
        self._dst_combo = QComboBox()
        self._src_combo.setStyleSheet(_cb_style)
        self._dst_combo.setStyleSheet(_cb_style)
        self._src_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._dst_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._src_combo.setFixedHeight(26)
        self._dst_combo.setFixedHeight(26)

        sel_row = QWidget(panel)
        sel_row.setStyleSheet(f"background:{CARD_BG}; border-radius:3px;")
        sel_h = QHBoxLayout(sel_row)
        sel_h.setContentsMargins(6, 4, 6, 4)
        sel_h.setSpacing(4)
        _src_lbl = QLabel("Kaynak")
        _src_lbl.setStyleSheet(f"color:{TEXT_DIM};font-size:7pt;")
        _dst_lbl = QLabel("Hedef")
        _dst_lbl.setStyleSheet(f"color:{TEXT_DIM};font-size:7pt;")
        _swap_btn = QPushButton("⇌")
        _swap_btn.setFixedSize(28, 26)
        _swap_btn.setToolTip("Kaynak/Hedef yer değiştir")
        _swap_btn.setStyleSheet(
            "QPushButton{background:#1e3a5f;color:#38bdf8;border:1px solid #2f3b4a;"
            "border-radius:4px;font-size:10pt;}"
            "QPushButton:hover{background:#1e4a7f;}"
        )
        _swap_btn.clicked.connect(self._swap_src_dst)
        sel_h.addWidget(_src_lbl)
        sel_h.addWidget(self._src_combo)
        sel_h.addWidget(_dst_lbl)
        sel_h.addWidget(self._dst_combo)
        sel_h.addWidget(_swap_btn)
        layout.addWidget(sel_row)

        self._meas_src_pid: str = f"device:{self.state.primary_source_id}"
        self._meas_dst_pid: str = f"device:{self.state.primary_target_id}"
        self._src_combo.currentIndexChanged.connect(self._on_src_dst_changed)
        self._dst_combo.currentIndexChanged.connect(self._on_src_dst_changed)

        # ── Mesafe ────────────────────────────────────────────────
        layout.addWidget(sec_hdr("DISTANCE (m)"))
        self._dist_layout = QVBoxLayout()
        self._dist_layout.setSpacing(3)
        layout.addLayout(self._dist_layout)

        # ── Heading ───────────────────────────────────────────────
        layout.addWidget(sec_hdr("HEADING (°)"))
        self._head_layout = QVBoxLayout()
        self._head_layout.setSpacing(3)
        layout.addLayout(self._head_layout)

        # ── Centroid ──────────────────────────────────────────────
        layout.addWidget(sec_hdr("CENTROID (CP-01)  ◉"))
        self._centroid_labels: Dict[str, QLabel] = {}
        for key in ("Lat", "Lon", "Alt"):
            rw, vl = data_row(panel, key)
            self._centroid_labels[key] = vl
            layout.addWidget(rw)

        layout.addStretch()
        return scroll

    def _swap_src_dst(self) -> None:
        si = self._src_combo.currentIndex()
        di = self._dst_combo.currentIndex()
        self._src_combo.blockSignals(True)
        self._dst_combo.blockSignals(True)
        self._src_combo.setCurrentIndex(di)
        self._dst_combo.setCurrentIndex(si)
        self._src_combo.blockSignals(False)
        self._dst_combo.blockSignals(False)
        self._on_src_dst_changed()

    def _on_src_dst_changed(self) -> None:
        src = self._src_combo.currentData()
        dst = self._dst_combo.currentData()
        if src:
            self._meas_src_pid = src
        if dst:
            self._meas_dst_pid = dst
        if src and src.startswith("device:"):
            self.state.primary_source_id = src[7:]
        if dst and dst.startswith("device:"):
            self.state.primary_target_id = dst[7:]
        if self.app:
            self.app._markers_dirty = True

    def _resolve_point(self, pid: str):  # type: ignore[return]
        if pid.startswith("device:"):
            return self.state.devices.get(pid[7:])
        name = pid[pid.index(":") + 1:] if ":" in pid else pid
        if pid.startswith("flag:"):
            for f in self.state.flags:
                if f.get("name") == name:
                    return _PointProxy(name, float(f["lat"]), float(f["lon"]))
        if pid.startswith("target:"):
            for t in self.state.targets:
                if t.get("name") == name:
                    return _PointProxy(name, float(t["lat"]), float(t["lon"]))
        return None

    def _repopulate_combos(self) -> None:
        prev_src = self._meas_src_pid
        prev_dst = self._meas_dst_pid
        for cb in (self._src_combo, self._dst_combo):
            cb.blockSignals(True)
            cb.clear()
        for dev in self.state.devices.values():
            pid = f"device:{dev.device_id}"
            for cb in (self._src_combo, self._dst_combo):
                cb.addItem(dev.name, userData=pid)
        for f in self.state.flags:
            pid = f"flag:{f['name']}"
            for cb in (self._src_combo, self._dst_combo):
                cb.addItem(f"⚑ {f['name']}", userData=pid)
        for t in self.state.targets:
            pid = f"target:{t['name']}"
            for cb in (self._src_combo, self._dst_combo):
                cb.addItem(f"⌖ {t['name']}", userData=pid)
        for cb, pid in ((self._src_combo, prev_src), (self._dst_combo, prev_dst)):
            idx = next((i for i in range(cb.count()) if cb.itemData(i) == pid), 0)
            cb.setCurrentIndex(idx)
        for cb in (self._src_combo, self._dst_combo):
            cb.blockSignals(False)

    def _build_network_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        layout.addWidget(label(panel, "NETWORK STATUS", TEXT, bold=True, size=9))
        layout.addWidget(hline(panel))

        self._net_layout = QVBoxLayout()
        self._net_layout.setSpacing(4)
        layout.addLayout(self._net_layout)
        layout.addStretch()
        return panel

    def _build_logs_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        layout.addWidget(label(panel, "OTURUM KAYDI", TEXT, bold=True, size=9))
        layout.addWidget(hline(panel))

        title_input = QLineEdit()
        title_input.setPlaceholderText("Oturum adı (isteğe bağlı)")
        title_input.setStyleSheet(
            f"background:{CARD_BG};color:{TEXT};border:1px solid {BORDER};"
            "border-radius:4px;padding:5px 8px;font-size:9pt;"
        )
        self._session_title_input = title_input
        layout.addWidget(title_input)

        toggle_btn = QPushButton("▶  Kaydı Başlat")
        toggle_btn.setFixedHeight(32)
        toggle_btn.setStyleSheet(
            "QPushButton{background:#14532d;color:#4ade80;border:1px solid #166534;"
            "border-radius:4px;font-size:9pt;font-weight:bold;}"
            "QPushButton:hover{background:#166534;}"
        )
        toggle_btn.clicked.connect(self._on_session_toggle)
        self._session_toggle_btn = toggle_btn
        layout.addWidget(toggle_btn)

        status_lbl = QLabel("  ⏹ Bekliyor")
        status_lbl.setStyleSheet(f"color:{TEXT_DIM};font-size:8pt;padding:2px 0;")
        self._session_status_lbl = status_lbl
        layout.addWidget(status_lbl)

        layout.addSpacing(6)
        layout.addWidget(hline(panel))
        layout.addWidget(label(panel, "TELEMETRY LOG", TEXT, bold=True, size=9))
        layout.addWidget(hline(panel))

        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setStyleSheet(
            f"background:#0d1117; color:#4ade80; font-family:Consolas,monospace;"
            f"font-size:8pt; border:1px solid {BORDER}; border-radius:4px;"
        )
        layout.addWidget(self._log_view)

        clear_btn = QPushButton("Temizle")
        clear_btn.setStyleSheet(
            f"background-color:#202b36; color:{TEXT_DIM}; font-size:8pt;"
            f"border:1px solid {BORDER}; border-radius:4px; padding:4px;"
        )
        clear_btn.clicked.connect(self._log_view.clear)
        layout.addWidget(clear_btn)
        return panel

    def _build_help_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        layout.addWidget(label(panel, "GNSS COMMAND CENTER", TEXT, bold=True, size=10))
        layout.addWidget(label(panel, "v1.0.0  —  PyQt6 + Leaflet.js", TEXT_DIM, size=8))
        layout.addWidget(hline(panel))

        shortcuts = [
            ("☷  Tıkla", "Cihaz ekle formu"),
            ("×  Tıkla", "Cihaz sil"),
            ("✏  Tıkla", "Cihaz düzenle"),
            ("🔓 / 🔒", "Harita kilidi"),
        ]
        for key, desc in shortcuts:
            row_w = QWidget()
            row = QHBoxLayout(row_w)
            row.setContentsMargins(0, 2, 0, 2)
            row.setSpacing(8)
            k = QLabel(key)
            k.setFixedWidth(80)
            k.setStyleSheet(f"color:#38bdf8; font-size:8pt; font-weight:bold;")
            row.addWidget(k)
            d = QLabel(desc)
            d.setStyleSheet(f"color:{TEXT_DIM}; font-size:8pt;")
            row.addWidget(d)
            row.addStretch()
            layout.addWidget(row_w)

        layout.addStretch()
        return panel

    # ── NOKTALAR panel ────────────────────────────────────────────────────────

    def _build_noktalar_panel(self) -> QWidget:
        panel = QWidget()
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        hdr = QLabel("  NOKTALAR")
        hdr.setFixedHeight(32)
        hdr.setStyleSheet("background:#2b3743; color:#f9fafb; font-size:9pt; font-weight:bold;")
        outer.addWidget(hdr)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("background:#0d1117; border:none;")
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._noktalar_list_widget = QWidget()
        self._noktalar_list_widget.setStyleSheet("background:#0d1117;")
        self._noktalar_list_layout = QVBoxLayout(self._noktalar_list_widget)
        self._noktalar_list_layout.setContentsMargins(0, 0, 0, 0)
        self._noktalar_list_layout.setSpacing(1)
        self._noktalar_list_layout.addStretch()
        scroll.setWidget(self._noktalar_list_widget)
        outer.addWidget(scroll)
        return panel

    def _refresh_noktalar_panel(self) -> None:
        if p := self._web_view.page():
            p.runJavaScript(
                "JSON.stringify({"
                "deleted:Object.keys(window._gnssDeletedRuntime||{}),"
                "flags:(window._gnssUserFlagData||[]),"
                "targets:(window._gnssTargetData||[])"
                "})",
                self._on_noktalar_deleted_data,
            )
        else:
            self._build_noktalar_rows(set(), [], [])

    def _on_noktalar_deleted_data(self, json_str: Optional[str]) -> None:
        import json as _json
        try:
            data = _json.loads(json_str or "{}")
            if isinstance(data, list):
                deleted_keys: set = set(data)
                flags, targets = [], []
            else:
                deleted_keys = set(data.get("deleted", []))
                flags = data.get("flags", [])
                targets = data.get("targets", [])
        except Exception:
            deleted_keys, flags, targets = set(), [], []
        self._build_noktalar_rows(deleted_keys, flags, targets)

    def _build_noktalar_rows(self, deleted_keys: set, flags: list, targets: list) -> None:
        while self._noktalar_list_layout.count() > 1:
            item = self._noktalar_list_layout.takeAt(0)
            if item is not None:
                w = item.widget()
                if w:
                    w.deleteLater()

        insert_pos = 0
        for dev in self.state.devices.values():
            row = self._make_noktalar_gps_row(dev, f"gps:{dev.name}" in deleted_keys)
            self._noktalar_list_layout.insertWidget(insert_pos, row)
            insert_pos += 1

        for f in flags:
            fname = f.get("name", "?")
            fkey = f"flag:{fname}"
            row = self._make_noktalar_point_row(
                fname, f.get("lat", 0), f.get("lon", 0), "#d4a017",
                key=fkey, is_deleted=fkey in deleted_keys,
            )
            self._noktalar_list_layout.insertWidget(insert_pos, row)
            insert_pos += 1

        for t in targets:
            tname = t.get("name", "?")
            tkey = f"target:{tname}"
            row = self._make_noktalar_point_row(
                tname, t.get("lat", 0), t.get("lon", 0), "#e05c5c",
                key=tkey, is_deleted=tkey in deleted_keys,
            )
            self._noktalar_list_layout.insertWidget(insert_pos, row)
            insert_pos += 1

        if self.state.filtered_midpoint:
            lat, lon = self.state.filtered_midpoint
            ckey = "centroid:filtered"
            row = self._make_noktalar_centroid_row(lat, lon, ckey in deleted_keys)
            self._noktalar_list_layout.insertWidget(insert_pos, row)

    def _make_noktalar_gps_row(self, dev, is_deleted: bool) -> QWidget:
        outer = QFrame()
        outer.setFixedHeight(46)
        outer.setStyleSheet("background:#0d1117; border-bottom:1px solid #1a2332;")
        hl = QHBoxLayout(outer)
        hl.setContentsMargins(0, 0, 4, 0)
        hl.setSpacing(0)

        bar = QFrame()
        bar.setFixedWidth(3)
        bar.setStyleSheet(f"background:{'#374151' if is_deleted else dev.fix_color}; border:none;")
        hl.addWidget(bar)
        hl.addSpacing(6)

        info = QWidget()
        info.setStyleSheet("background:transparent;")
        iv = QVBoxLayout(info)
        iv.setContentsMargins(0, 4, 0, 4)
        iv.setSpacing(1)
        sym = "○" if is_deleted else "●"
        nc = TEXT_DIM if is_deleted else TEXT
        name_lbl = QLabel(f"{sym} {dev.name}")
        name_lbl.setStyleSheet(f"color:{nc}; font-size:8pt; font-weight:bold; background:transparent;")
        fc = GRAY if is_deleted else dev.fix_color
        fix_lbl = QLabel(dev.fix_status_text)
        fix_lbl.setStyleSheet(f"color:{fc}; font-size:7pt; background:transparent;")
        iv.addWidget(name_lbl)
        iv.addWidget(fix_lbl)
        hl.addWidget(info, stretch=1)

        if dev.has_position:
            fmt = self.state.coord_format
            coord_lbl = QLabel(
                f"{format_coordinate(dev.latitude, 'lat', fmt)}\n"
                f"{format_coordinate(dev.longitude, 'lon', fmt)}"
            )
            coord_lbl.setStyleSheet(
                f"color:{'#374151' if is_deleted else '#6b9fbf'}; "
                "font-size:6pt; font-family:Consolas,monospace; background:transparent;"
            )
            hl.addWidget(coord_lbl)
            hl.addSpacing(4)

        if dev.has_position:
            lat, lon = dev.latitude, dev.longitude
            pan_btn = QPushButton("◎")
            pan_btn.setFixedSize(22, 22)
            pan_btn.setToolTip("Konuma git")
            pan_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            pan_btn.setStyleSheet(
                f"QPushButton{{background:#1a2332;color:{TEAL};font-size:9pt;"
                "border:1px solid #263040;padding:0;}}"
                "QPushButton:hover{color:#5eead4;}"
            )
            pan_btn.clicked.connect(lambda _, la=lat, lo=lon: self._noktalar_pan_to(la, lo))
            hl.addWidget(pan_btn)
            hl.addSpacing(2)

        restore_btn = QPushButton("↩")
        restore_btn.setFixedSize(22, 22)
        restore_btn.setToolTip("Haritaya geri ekle" if is_deleted else "Haritada mevcut")
        restore_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        rc = GREEN if is_deleted else "#374151"
        restore_btn.setStyleSheet(
            f"QPushButton{{background:#1a2332;color:{rc};font-size:9pt;"
            "border:1px solid #263040;padding:0;}}"
            "QPushButton:hover{color:#4ade80;}"
        )
        key = f"gps:{dev.name}"
        restore_btn.clicked.connect(lambda _, k=key: self._noktalar_restore(k))
        hl.addWidget(restore_btn)
        return outer

    def _make_noktalar_centroid_row(self, lat: float, lon: float, is_deleted: bool = False) -> QWidget:
        outer = QFrame()
        outer.setFixedHeight(46)
        outer.setStyleSheet("background:#0d1117; border-bottom:1px solid #1a2332;")
        hl = QHBoxLayout(outer)
        hl.setContentsMargins(0, 0, 4, 0)
        hl.setSpacing(0)

        bar = QFrame()
        bar.setFixedWidth(3)
        bar.setStyleSheet(f"background:{'#374151' if is_deleted else TEAL}; border:none;")
        hl.addWidget(bar)
        hl.addSpacing(6)

        info = QWidget()
        info.setStyleSheet("background:transparent;")
        iv = QVBoxLayout(info)
        iv.setContentsMargins(0, 4, 0, 4)
        iv.setSpacing(1)
        sym = "○" if is_deleted else "✦"
        nc = TEXT_DIM if is_deleted else TEXT
        name_lbl = QLabel(f"{sym} CP-01")
        name_lbl.setStyleSheet(f"color:{nc}; font-size:8pt; font-weight:bold; background:transparent;")
        type_lbl = QLabel("CENTROID")
        type_lbl.setStyleSheet(f"color:{'#374151' if is_deleted else TEAL}; font-size:7pt; background:transparent;")
        iv.addWidget(name_lbl)
        iv.addWidget(type_lbl)
        hl.addWidget(info, stretch=1)

        fmt = self.state.coord_format
        coord_lbl = QLabel(
            f"{format_coordinate(lat, 'lat', fmt)}\n"
            f"{format_coordinate(lon, 'lon', fmt)}"
        )
        coord_lbl.setStyleSheet(
            f"color:{'#374151' if is_deleted else '#6b9fbf'}; "
            "font-size:6pt; font-family:Consolas,monospace; background:transparent;"
        )
        hl.addWidget(coord_lbl)
        hl.addSpacing(4)

        pan_btn = QPushButton("◎")
        pan_btn.setFixedSize(22, 22)
        pan_btn.setToolTip("Konuma git")
        pan_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        pan_btn.setStyleSheet(
            f"QPushButton{{background:#1a2332;color:{TEAL};font-size:9pt;"
            "border:1px solid #263040;padding:0;}}"
            "QPushButton:hover{color:#5eead4;}"
        )
        pan_btn.clicked.connect(lambda _, la=lat, lo=lon: self._noktalar_pan_to(la, lo))
        hl.addWidget(pan_btn)
        hl.addSpacing(2)

        restore_btn = QPushButton("↩")
        restore_btn.setFixedSize(22, 22)
        restore_btn.setToolTip("Haritaya geri ekle" if is_deleted else "Haritada mevcut")
        restore_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        rc = GREEN if is_deleted else "#374151"
        restore_btn.setStyleSheet(
            f"QPushButton{{background:#1a2332;color:{rc};font-size:9pt;"
            "border:1px solid #263040;padding:0;}}"
            "QPushButton:hover{color:#4ade80;}"
        )
        restore_btn.setEnabled(is_deleted)
        if is_deleted:
            restore_btn.clicked.connect(lambda _=False: self._noktalar_restore("centroid:filtered"))
        hl.addWidget(restore_btn)
        return outer

    def _make_noktalar_point_row(
        self, name: str, lat: float, lon: float, color: str,
        key: str = "", is_deleted: bool = False,
    ) -> QWidget:
        outer = QFrame()
        outer.setFixedHeight(46)
        outer.setStyleSheet("background:#0d1117; border-bottom:1px solid #1a2332;")
        hl = QHBoxLayout(outer)
        hl.setContentsMargins(0, 0, 4, 0)
        hl.setSpacing(0)

        bar = QFrame()
        bar.setFixedWidth(3)
        bar.setStyleSheet(f"background:{'#374151' if is_deleted else color}; border:none;")
        hl.addWidget(bar)
        hl.addSpacing(6)

        info = QWidget()
        info.setStyleSheet("background:transparent;")
        iv = QVBoxLayout(info)
        iv.setContentsMargins(0, 4, 0, 4)
        iv.setSpacing(1)
        sym = "○" if is_deleted else "●"
        nc = TEXT_DIM if is_deleted else TEXT
        name_lbl = QLabel(f"{sym} {name}")
        name_lbl.setStyleSheet(f"color:{nc}; font-size:8pt; font-weight:bold; background:transparent;")
        fmt = self.state.coord_format
        coord_lbl = QLabel(
            f"{format_coordinate(lat, 'lat', fmt)}  "
            f"{format_coordinate(lon, 'lon', fmt)}"
        )
        coord_lbl.setStyleSheet(
            f"color:{'#374151' if is_deleted else '#6b9fbf'}; "
            "font-size:6pt; font-family:Consolas,monospace; background:transparent;"
        )
        iv.addWidget(name_lbl)
        iv.addWidget(coord_lbl)
        hl.addWidget(info, stretch=1)

        pan_btn = QPushButton("◎")
        pan_btn.setFixedSize(22, 22)
        pan_btn.setToolTip("Konuma git")
        pan_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        pan_btn.setStyleSheet(
            "QPushButton{background:#0d1117;color:#4a9eff;border:1px solid #1a2332;"
            "border-radius:4px;font-size:9pt;padding:0;}"
            "QPushButton:hover{background:#1a2332;}"
        )
        pan_btn.clicked.connect(lambda _=False, la=lat, lo=lon: self._noktalar_pan_to(la, lo))
        hl.addWidget(pan_btn)
        hl.addSpacing(2)

        restore_btn = QPushButton("↩")
        restore_btn.setFixedSize(22, 22)
        restore_btn.setToolTip("Haritaya geri ekle" if is_deleted else "Haritada mevcut")
        restore_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        rc = GREEN if is_deleted else "#374151"
        restore_btn.setStyleSheet(
            f"QPushButton{{background:#1a2332;color:{rc};font-size:9pt;"
            "border:1px solid #263040;padding:0;}}"
            "QPushButton:hover{color:#4ade80;}"
        )
        restore_btn.setEnabled(is_deleted)
        if is_deleted and key:
            restore_btn.clicked.connect(lambda _=False, k=key: self._noktalar_restore(k))
        hl.addWidget(restore_btn)
        return outer

    def _noktalar_pan_to(self, lat: float, lon: float) -> None:
        if p := self._web_view.page():
            p.runJavaScript(f"if(window._gnssMap)window._gnssMap.setView([{lat},{lon}],18);")

    def _noktalar_restore(self, key: str) -> None:
        safe = key.replace("'", "\\'")

        if key.startswith("gps:"):
            js = (
                f"delete (window._gnssDeletedRuntime||{{}})['{safe}'];"
                f"if(window._gnssPmk)delete window._gnssPmk['{safe}'];"
            )
        elif key.startswith("centroid:"):
            js = f"delete (window._gnssDeletedRuntime||{{}})['{safe}'];"
        elif key.startswith("flag:"):
            sn = key[5:].replace("'", "\\'")
            js = (
                f"(function(){{"
                f"delete (window._gnssDeletedRuntime||{{}})['{safe}'];"
                f"var d=(window._gnssUserFlagData||[]).find(function(f){{return f.name==='{sn}';}});"
                f"var m=window._gnssMap;if(!d||!m)return;"
                f"var mk=L.marker([d.lat,d.lon],{{icon:L.divIcon({{className:'gnss-flag-icon',"
                f"html:'⚑',iconSize:[28,28],iconAnchor:[6,24]}}),zIndexOffset:1200}}).addTo(m);"
                f"mk.off('click',mk._openPopup,mk);"
                f"mk.on('dblclick',function(e){{L.DomEvent.stop(e.originalEvent);mk.openPopup();}});"
                f"var lbl=L.marker([d.lat,d.lon],{{icon:L.divIcon({{className:'gnss-map-label',"
                f"html:'{sn}',iconSize:null}}),zIndexOffset:1199}}).addTo(m);"
                f"if(!window._gnssUserFlags)window._gnssUserFlags=[];"
                f"window._gnssUserFlags.push(mk);window._gnssUserFlags.push(lbl);"
                f"if(window._gnssRegisterSelectable)window._gnssRegisterSelectable([mk,lbl],'flag','{safe}');"
                f"}})()"
            )
        elif key.startswith("target:"):
            sn = key[7:].replace("'", "\\'")
            js = (
                f"(function(){{"
                f"delete (window._gnssDeletedRuntime||{{}})['{safe}'];"
                f"var d=(window._gnssTargetData||[]).find(function(t){{return t.name==='{sn}';}});"
                f"var m=window._gnssMap;if(!d||!m)return;"
                f"var mk=L.marker([d.lat,d.lon],{{icon:L.divIcon({{className:'gnss-target-icon',"
                f"html:'',iconSize:[28,28],iconAnchor:[14,14]}}),zIndexOffset:1300}}).addTo(m);"
                f"mk.off('click',mk._openPopup,mk);"
                f"mk.on('dblclick',function(e){{L.DomEvent.stop(e.originalEvent);mk.openPopup();}});"
                f"var lbl=L.marker([d.lat,d.lon],{{icon:L.divIcon({{className:'gnss-map-label',"
                f"html:'{sn}',iconSize:null}}),zIndexOffset:1299}}).addTo(m);"
                f"if(!window._gnssTargets)window._gnssTargets=[];"
                f"window._gnssTargets.push(mk);window._gnssTargets.push(lbl);"
                f"if(window._gnssRegisterSelectable)window._gnssRegisterSelectable([mk,lbl],'target','{safe}');"
                f"}})()"
            )
        else:
            return

        if p := self._web_view.page():
            p.runJavaScript(js)

    # ── Status bar ────────────────────────────────────────────────────────────

    def _build_status_bar(self) -> QStatusBar:
        bar = QStatusBar()
        bar.setStyleSheet(f"background:{PANEL_BG}; color:{TEXT_DIM}; font-size:8pt;")
        self._status_lbl = QLabel("Disconnected")
        self._status_lbl.setStyleSheet(f"color:{TEXT_DIM};")
        bar.addWidget(self._status_lbl)
        self._fix_status_lbl = QLabel("")
        self._fix_status_lbl.setStyleSheet(f"color:{TEXT_DIM}; margin-left:16px;")
        bar.addWidget(self._fix_status_lbl)
        return bar

    # ── Zamanlayıcılar ────────────────────────────────────────────────────────

    def _start_timers(self) -> None:
        clock_timer = QTimer(self)
        clock_timer.timeout.connect(self._tick_clock)
        clock_timer.start(1000)
        self._tick_clock()

        ui_timer = QTimer(self)
        ui_timer.timeout.connect(self._refresh_ui)
        ui_timer.start(500)

        map_timer = QTimer(self)
        map_timer.timeout.connect(self._refresh_map)
        map_timer.start(1000)

        zoom_timer = QTimer(self)
        zoom_timer.timeout.connect(self._refresh_zoom)
        zoom_timer.start(2000)

        flag_timer = QTimer(self)
        flag_timer.timeout.connect(self._sync_flags)
        flag_timer.start(3000)

        session_log_timer = QTimer(self)
        session_log_timer.timeout.connect(self._write_session_log)
        session_log_timer.start(1000)

    def _tick_clock(self) -> None:
        self._clock_lbl.setText(time.strftime("%H:%M:%S UTC", time.gmtime()))

    def _refresh_ui(self) -> None:
        # Cihaz kartları
        while self._device_list_layout.count() > 1:
            item = self._device_list_layout.takeAt(0)
            if item is not None:
                widget = item.widget()
                if widget is not None:
                    widget.deleteLater()

        for dev in self.state.devices.values():
            card = DeviceCard(
                dev,
                self._device_list_widget,
                self._delete_device,
                self._open_device_edit_dialog,
                self._toggle_device_connection,
                bool(self.app and dev.device_id in self.app.listeners),
            )
            self._device_list_layout.insertWidget(
                self._device_list_layout.count() - 1, card
            )

        # Kaynak/Hedef combo'larını güncelle
        self._repopulate_combos()

        # Mesafe & Heading
        for layout_ref in (self._dist_layout, self._head_layout):
            while layout_ref.count():
                item = layout_ref.takeAt(0)
                if item is not None:
                    widget = item.widget()
                    if widget is not None:
                        widget.deleteLater()

        d1 = self._resolve_point(getattr(self, "_meas_src_pid", f"device:{self.state.primary_source_id}"))
        d2 = self._resolve_point(getattr(self, "_meas_dst_pid", f"device:{self.state.primary_target_id}"))
        pairs = [(d1, d2)] if (d1 and d2 and d1.has_position and d2.has_position) else []

        for d1, d2 in pairs:
            dist = haversine_m(d1.latitude, d1.longitude, d2.latitude, d2.longitude)
            azim = compute_azimuth(d1.latitude, d1.longitude, d2.latitude, d2.longitude)
            pair_txt = f"{d1.name} → {d2.name}"

            for layout_ref, value_txt in [
                (self._dist_layout, f"{dist:.1f} m"),
                (self._head_layout, f"{azim:.1f}°"),
            ]:
                row_w = QWidget()
                row_w.setStyleSheet(f"background:{PANEL_BG};")
                row = QHBoxLayout(row_w)
                row.setContentsMargins(0, 0, 0, 0)
                row.setSpacing(8)
                k = QLabel(pair_txt)
                k.setStyleSheet(f"color:{TEXT_DIM}; font-size:8pt;")
                k.setFixedWidth(130)
                row.addWidget(k)
                v = QLabel(value_txt)
                v.setStyleSheet(f"color:{TEXT}; font-size:9pt; font-weight:bold;")
                row.addWidget(v)
                row.addStretch()
                layout_ref.addWidget(row_w)

        # Centroid
        if self.state.filtered_midpoint:
            lat, lon = self.state.filtered_midpoint
            fmt = self.state.coord_format
            self._centroid_labels["Lat"].setText(format_coordinate(lat, "lat", fmt))
            self._centroid_labels["Lon"].setText(format_coordinate(lon, "lon", fmt))
            alts = [d.altitude for d in self.state.devices.values() if d.has_position and d.altitude != 0.0]
            alt_txt = f"{sum(alts)/len(alts):.2f} m" if alts else "-"
            self._centroid_labels["Alt"].setText(alt_txt)
        else:
            for lbl in self._centroid_labels.values():
                lbl.setText("-")

        # Network panel
        while self._net_layout.count():
            item = self._net_layout.takeAt(0)
            if item is not None:
                w = item.widget()
                if w is not None:
                    w.deleteLater()
        for dev in self.state.devices.values():
            sc, st = dev.fix_color, dev.fix_status_text
            row_w = QWidget()
            row_w.setStyleSheet(f"background:#1a2332; border-bottom:1px solid #263040;")
            row_w.setFixedHeight(38)
            rr = QHBoxLayout(row_w)
            rr.setContentsMargins(8, 0, 8, 0)
            rr.setSpacing(6)
            dot_l = QLabel("●")
            dot_l.setStyleSheet(f"color:{sc}; font-size:10pt;")
            rr.addWidget(dot_l)
            nm_l = QLabel(dev.name)
            nm_l.setStyleSheet(f"color:{TEXT}; font-size:8pt; font-weight:bold;")
            nm_l.setFixedWidth(54)
            rr.addWidget(nm_l)
            st_l = QLabel(st)
            st_l.setStyleSheet(f"color:{sc}; font-size:7pt;")
            st_l.setFixedWidth(42)
            rr.addWidget(st_l)
            ip_str = "-" if dev.ip == "-" else f"{dev.ip}:{dev.port}"
            ip_l = QLabel(ip_str)
            ip_l.setStyleSheet(f"color:{TEXT_DIM}; font-size:7pt;")
            rr.addWidget(ip_l)
            rr.addStretch()
            self._net_layout.addWidget(row_w)

        # Heading overlay kartları
        for hcard in self._heading_cards:
            hcard.refresh_labels()
            if hcard.src == hcard.dst:
                hcard.set_status("Geçersiz")
                continue
            sd = self.state.devices.get(hcard.src)
            dd = self.state.devices.get(hcard.dst)
            if sd is None or dd is None:
                hcard.set_status("Bekleniyor", ORANGE)
            elif not sd.has_position or not dd.has_position:
                hcard.set_status("Fix yok", GRAY)
            else:
                col = _heading_fix_color(sd.fix_quality, dd.fix_quality)
                if col is None:
                    bad = sd if sd.fix_quality == 0 else dd
                    hcard.set_status(f"Fix yok ({bad.name})", GRAY)
                else:
                    hdg = compute_azimuth(sd.latitude, sd.longitude, dd.latitude, dd.longitude)
                    hcard.set_heading(hdg, col)

        # Compass — ilk heading kart değeriyle güncelle
        if self._heading_cards:
            hc = self._heading_cards[0]
            sd0 = self.state.devices.get(hc.src)
            dd0 = self.state.devices.get(hc.dst)
            if (sd0 and dd0 and sd0.has_position and dd0.has_position
                    and sd0.fix_quality > 0 and dd0.fix_quality > 0):
                self._compass.set_heading(
                    compute_azimuth(sd0.latitude, sd0.longitude, dd0.latitude, dd0.longitude)
                )
            else:
                self._compass.set_heading(None)

        # Popup'tan başaçısı toggle isteği
        if p := self._web_view.page():
            p.runJavaScript(
                "var r=!!window._gnssHeadingToggleReq;window._gnssHeadingToggleReq=false;r;",
                self._on_heading_toggle_req,
            )

        # Anlık mod görsel güncelleme
        if p2 := self._web_view.page():
            p2.runJavaScript(
                "JSON.stringify({lm:!!window._gnssLiveMeasureMode,"
                "cnt:(window._gnssLivePairs||[]).length})",
                self._on_live_mode_state,
            )

        # NOKTALAR paneli (sadece aktif sekmede)
        if self._right_stack.currentIndex() == 3:
            self._refresh_noktalar_panel()

        # Status bar
        txt = "Connected" if self.state.is_connected else "Disconnected"
        if self.state.last_error:
            txt += f"  |  {self.state.last_error}"
        self._status_lbl.setText(txt)
        connected_devs = [d for d in self.state.devices.values() if d.status == "Online"]
        if connected_devs:
            best = max(connected_devs, key=lambda d: d.fix_quality)
            self._fix_status_lbl.setText(f"{best.name}: {best.fix_status_text}  Sat:{best.satellites}")
            self._fix_status_lbl.setStyleSheet(f"color:{best.fix_color}; margin-left:16px;")
        else:
            self._fix_status_lbl.setText("")

    def _on_heading_toggle_req(self, requested: bool) -> None:
        if not requested:
            return
        visible = not self._heading_cards[0].isVisible() if self._heading_cards else True
        for hcard in self._heading_cards:
            hcard.setVisible(visible)
        self._compass.setVisible(visible)

    def _on_live_mode_state(self, json_str: Optional[str]) -> None:
        import json as _j
        try:
            data = _j.loads(json_str or '{}')
            active = bool(data.get("lm", False))
            cnt = int(data.get("cnt", 0))
        except Exception:
            active, cnt = False, 0

        btn = self._tool_btns.get("Anlık")
        if btn is None:
            return
        if active:
            label = f"⇿\nAnlık·{cnt}" if cnt > 0 else "⇿\nAnlık ON"
            btn.setText(label)
            btn.setStyleSheet(
                "QPushButton{background-color:#7c2d12;color:#fb923c;"
                "font-size:8pt;font-weight:bold;border:2px solid #ea580c;}"
                "QPushButton:hover{background-color:#9a3412;}"
            )
        else:
            btn.setText("⇿\nAnlık")
            is_sel = self._active_tool == "Anlık"
            btn.setStyleSheet(self._tool_btn_style("Anlık", is_sel))
            if is_sel:
                self._active_tool = "Seç"
                for t, b in self._tool_btns.items():
                    b.setStyleSheet(self._tool_btn_style(t, t == "Seç"))

    def _delete_device(self, device_name: str) -> None:
        if self.app:
            self.app.remove_device(device_name)

    def _open_device_edit_dialog(self, device_id: str) -> None:
        if self.app is None:
            return
        dev = self.state.devices.get(device_id)
        if dev is None:
            return

        dlg = QDialog(self)
        dlg.setWindowTitle(f"{dev.name} Düzenle")
        dlg.setFixedSize(300, 214)
        dlg.setModal(True)
        dlg.setStyleSheet(
            f"QDialog{{background:#1f2937;}} QLabel{{color:{TEXT_DIM};font-size:8pt;}}"
            f"QLineEdit{{background-color:#2d3748;color:{TEXT};border:1px solid {BORDER};"
            f"border-radius:4px;padding:5px;}}"
        )
        vl = QVBoxLayout(dlg)
        vl.setContentsMargins(12, 10, 12, 12)
        vl.setSpacing(6)
        vl.addWidget(QLabel("Cihaz Adi / ID"))
        name_in = QLineEdit(dev.name)
        name_in.setPlaceholderText("örn: GPS-1")
        name_in.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        vl.addWidget(name_in)
        vl.addWidget(QLabel("IP Adresi"))
        ip_in = QLineEdit(dev.ip)
        ip_in.setPlaceholderText("örn: 192.168.1.100")
        ip_in.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        vl.addWidget(ip_in)
        vl.addWidget(QLabel("Port"))
        port_in = QLineEdit(str(dev.port) if dev.port > 0 else "")
        port_in.setPlaceholderText("1–65535")
        port_in.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        port_in.setValidator(QIntValidator(1, 65535, port_in))
        vl.addWidget(port_in)

        # Field'a tıklayınca tümünü seç
        def _make_focus_handler(w):
            def _h(e):
                w.__class__.focusInEvent(w, e)
                QTimer.singleShot(0, w.selectAll)
            return _h
        for _f in (name_in, ip_in, port_in):
            _f.focusInEvent = _make_focus_handler(_f)  # type: ignore[method-assign]

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel_btn = QPushButton("İptal")
        cancel_btn.setFixedSize(78, 28)
        cancel_btn.setStyleSheet(
            "QPushButton{background-color:#374151;color:#f9fafb;border:none;"
            "border-radius:4px;padding:4px 10px;font-size:8pt;}"
            "QPushButton:hover{background-color:#4b5563;}"
        )
        save_btn = QPushButton("Kaydet")
        save_btn.setFixedSize(86, 28)
        save_btn.setDefault(True)
        save_btn.setStyleSheet(
            "QPushButton{background-color:#1d4ed8;color:#fff;border:none;"
            "border-radius:4px;padding:4px 10px;font-size:8pt;font-weight:bold;}"
            "QPushButton:hover{background-color:#2563eb;}"
        )
        cancel_btn.clicked.connect(dlg.reject)
        save_btn.clicked.connect(dlg.accept)
        name_in.returnPressed.connect(dlg.accept)
        ip_in.returnPressed.connect(dlg.accept)
        port_in.returnPressed.connect(dlg.accept)
        buttons.addWidget(cancel_btn)
        buttons.addWidget(save_btn)
        vl.addLayout(buttons)

        name_in.setFocus()
        QTimer.singleShot(0, name_in.selectAll)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            new_name = name_in.text().strip()
            new_ip = ip_in.text().strip() or dev.ip
            new_port = dev.port
            try:
                p = int(port_in.text())
                if 0 < p < 65536:
                    new_port = p
            except ValueError:
                pass
            if new_name:
                self.app.update_device(device_id, new_name, new_ip, new_port)

    def _toggle_device_connection(self, device_id: str) -> None:
        if self.app:
            self.app.toggle_device_connection(device_id)

    def _device_options(self) -> List[Tuple[str, str]]:
        return [(dev.device_id, dev.name) for dev in self.state.devices.values()]

    def _add_heading_card(self) -> None:
        devices = list(self.state.devices.keys())
        src = devices[0] if devices else self.state.primary_source_id
        dst = devices[1] if len(devices) > 1 else self.state.primary_target_id
        card = HeadingCard(
            self._map_wrap, src, dst,
            on_add=self._add_heading_card,
            on_remove=self._remove_heading_card,
            get_devices=self._device_options,
        )
        card.show()
        self._heading_cards.append(card)
        self._layout_map_overlays()

    def _remove_heading_card(self, card: HeadingCard) -> None:
        if len(self._heading_cards) <= 1:
            return
        self._heading_cards.remove(card)
        card.deleteLater()
        self._layout_map_overlays()

    def _refresh_map(self) -> None:
        if self.app is None or self.state.is_map_paused:
            return
        if not self._map_js_ready:
            return
        if self.app._markers_dirty:
            fmt = self.state.coord_format
            meta = {
                dev.device_id: {
                    "sats": dev.satellites,
                    "fix_type": dev.fix_type,
                    "fix_quality": dev.fix_quality,
                    "hdop": dev.hdop,
                    "alt": dev.altitude,
                    "last_update": dev.last_update_ms,
                    "raw": self.app.latest_fixes[dev.device_id].raw_sentence
                    if self.app is not None and dev.device_id in self.app.latest_fixes
                    else "",
                    "lat_fmt": format_coordinate(dev.latitude, "lat", fmt),
                    "lon_fmt": format_coordinate(dev.longitude, "lon", fmt),
                }
                for dev in self.state.devices.values()
                if dev.has_position
            }
            any_paused = any(c.paused for c in self._heading_cards)
            js = self.app.map.get_update_js(
                pan=not self.state.is_map_locked,
                device_meta=meta,
                coord_format=self.state.coord_format,
                show_baseline=not any_paused,
            )
            page = self._web_view.page()
            if page is not None:
                page.runJavaScript(js)
            self.app._markers_dirty = False

    def _refresh_zoom(self) -> None:
        if not self._map_js_ready:
            return
        page = self._web_view.page()
        if page is None:
            return
        page.runJavaScript(
            "window._gnssMap ? JSON.stringify({z:window._gnssMap.getZoom(),"
            "lat:window._gnssMap.getCenter().lat}) : null",
            self._on_zoom_js_result,
        )

    def _on_zoom_js_result(self, result: object) -> None:
        if not result:
            return
        try:
            data = json.loads(str(result))
            z = int(data["z"])
            lat_rad = math.radians(float(data["lat"]))
            scale_m = 156543.03 * math.cos(lat_rad) / (2 ** z)
            if scale_m >= 1000:
                scale_txt = f"{scale_m/1000:.2f} km/px"
            elif scale_m >= 1:
                scale_txt = f"{scale_m:.1f} m/px"
            else:
                scale_txt = f"{scale_m*100:.0f} cm/px"
            self._zoom_lbl.setText(f"Zoom: {z}\nÖlçek: {scale_txt}")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass

    def _sync_flags(self) -> None:
        if not self._map_js_ready:
            return
        page = self._web_view.page()
        if page is None:
            return
        page.runJavaScript(
            "JSON.stringify({flags:window._gnssUserFlagData||[],targets:window._gnssTargetData||[]})",
            self._on_flags_synced,
        )

    def _on_flags_synced(self, result: object) -> None:
        try:
            data = json.loads(str(result))
            if isinstance(data, dict):
                if isinstance(data.get("flags"), list):
                    self.state.flags = data["flags"]
                if isinstance(data.get("targets"), list):
                    self.state.targets = data["targets"]
            elif isinstance(data, list):
                self.state.flags = data
        except (json.JSONDecodeError, TypeError):
            pass

    def _write_session_log(self) -> None:
        if self.app is None or not self.app.session_logger.is_active:
            return
        import datetime as _dt
        now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        elapsed = int(self.app.session_logger.elapsed_s)

        devs = sorted(
            [d for d in self.state.devices.values() if d.has_position],
            key=lambda d: d.name,
        )[:2]

        gps_cols: List[Any] = []
        for i in range(2):
            if i < len(devs):
                d = devs[i]
                gps_ts = ""
                fixes = self.app.latest_fixes if self.app else {}
                if d.device_id in fixes:
                    fms = fixes[d.device_id].timestamp_ms
                    if fms:
                        gps_ts = _dt.datetime.fromtimestamp(fms / 1000).strftime("%H:%M:%S")
                fix_lbl = FIX_LABELS.get(d.fix_quality, str(d.fix_quality))
                gps_cols += [d.name, gps_ts,
                             f"{d.latitude:.8f}".replace('.', ','),
                             f"{d.longitude:.8f}".replace('.', ','),
                             fix_lbl, d.satellites, f"{d.hdop:.1f}".replace('.', ',')]
            else:
                gps_cols += ["", "", "", "", "", "", ""]

        pair_name = f"{devs[0].name}→{devs[1].name}" if len(devs) >= 2 else ""
        def _flt(v: float) -> str:
            return f"{v:.8f}".replace('.', ',')

        if self.state.filtered_midpoint:
            mlat, mlon = self.state.filtered_midpoint
            mid_cols: List[Any] = [pair_name, _flt(mlat), _flt(mlon)]
        else:
            mid_cols = [pair_name, "", ""]

        tgt_cols: List[Any] = []
        for i in range(2):
            t = self.state.targets[i] if i < len(self.state.targets) else {}
            tgt_cols += ([t.get("name", ""), _flt(float(t.get('lat', 0))), _flt(float(t.get('lon', 0)))]
                         if t else ["", "", ""])

        flg_cols: List[Any] = []
        for i in range(2):
            f = self.state.flags[i] if i < len(self.state.flags) else {}
            flg_cols += ([f.get("name", ""), _flt(float(f.get('lat', 0))), _flt(float(f.get('lon', 0)))]
                         if f else ["", "", ""])

        if len(devs) >= 2:
            hdg = compute_azimuth(devs[0].latitude, devs[0].longitude,
                                  devs[1].latitude, devs[1].longitude)
            hdg_cols: List[Any] = [pair_name, f"{hdg:.2f}".replace('.', ',')]
        else:
            hdg_cols = ["", ""]

        row = [now, elapsed] + gps_cols + mid_cols + tgt_cols + flg_cols + hdg_cols
        self.app.session_logger.write_row(row)
        self._update_session_ui()

    def _on_session_toggle(self) -> None:
        if self.app is None:
            return
        if self.app.session_logger.is_active:
            self.app.session_logger.stop()
        else:
            title = (self._session_title_input.text().strip()
                     if self._session_title_input else "") or "oturum"
            self.app.session_logger.start(title)
        self._update_session_ui()

    def _update_session_ui(self) -> None:
        if self.app is None:
            return
        active = self.app.session_logger.is_active
        if self._session_status_lbl:
            if active:
                cnt = self.app.session_logger.row_count
                mins, secs = divmod(int(self.app.session_logger.elapsed_s), 60)
                self._session_status_lbl.setText(f"  ● Kayıt: {cnt} satır | {mins}:{secs:02d}")
                self._session_status_lbl.setStyleSheet("color:#4ade80;font-size:8pt;padding:2px 0;")
            else:
                self._session_status_lbl.setText("  ⏹ Bekliyor")
                self._session_status_lbl.setStyleSheet(f"color:{TEXT_DIM};font-size:8pt;padding:2px 0;")
        if self._session_toggle_btn:
            if active:
                self._session_toggle_btn.setText("■  Kaydı Durdur")
                self._session_toggle_btn.setStyleSheet(
                    "QPushButton{background:#7c2d12;color:#fb923c;border:1px solid #ea580c;"
                    "border-radius:4px;font-size:9pt;font-weight:bold;}"
                    "QPushButton:hover{background:#9a3412;}"
                )
            else:
                self._session_toggle_btn.setText("▶  Kaydı Başlat")
                self._session_toggle_btn.setStyleSheet(
                    "QPushButton{background:#14532d;color:#4ade80;border:1px solid #166534;"
                    "border-radius:4px;font-size:9pt;font-weight:bold;}"
                    "QPushButton:hover{background:#166534;}"
                )

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self.app:
            self.app.shutdown()
        event.accept()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._layout_map_overlays()


# ── Uygulama ──────────────────────────────────────────────────────────────────

class Application:
    def __init__(self) -> None:
        self.state = AppState()
        self.logger = TelemetryLogger("telemetry_log.csv")
        self.session_logger = SessionLogger()
        self.filter = MovingAverageFilter(window_size=self.state.filter_window_size)
        self.map = LiveMap()
        self.window: Optional[MainWindow] = None
        self.listeners: Dict[str, TCPListener] = {}
        self.taskbar: Optional[TaskbarManager] = None
        self.latest_fixes: Dict[str, GNSSFix] = {}
        self.map_output_path = Path("live_map.html")
        self._markers_dirty: bool = False
        self.tile_server: Optional[MBTilesServer] = None
        self._has_offline: bool = False

    def setup(self, qt_app: QApplication) -> None:
        self.logger.open()
        self._init_real_devices()
        self._setup_offline_maps()
        self.map.initialize_map()
        self.map.render_to_html(str(self.map_output_path))  # Sadece ilk yükleme için

        self.window = MainWindow(self.state)
        self.window.app = self
        self.window.show()
        self.window.load_map(self.map_output_path)

        hwnd = int(self.window.winId())
        self.taskbar = TaskbarManager(hwnd)

    def _setup_offline_maps(self) -> None:
        map_dir = Path("map_data")
        layers = {
            "google": map_dir / "googlemap.mbtiles",
            "navionics": map_dir / "navionics_data.mbtiles",
            "navionics4": map_dir / "Navionics4-15.mbtiles",
        }
        available = {name: path for name, path in layers.items() if path.exists()}
        if not available:
            return
        self.tile_server = MBTilesServer(available)
        self.tile_server.start()
        self._has_offline = "navionics4" in available
        if self.tile_server.port <= 0:
            return
        offline_layers: List[Dict[str, object]] = []
        if "navionics4" in available:
            meta = self.tile_server.layer_info("navionics4")
            max_native = int(meta.get("maxzoom", "11"))
            offline_layers.append({
                "name": "Offline Navionics4-15",
                "url": self.tile_server.tile_url("navionics4"),
                "attribution": "Offline Navionics4-15/SAS.Planet",
                "max_native_zoom": max_native,
                "max_zoom": 22,
                "overlay": False,
                "show": True,
            })
        if "google" in available:
            meta = self.tile_server.layer_info("google")
            max_native = int(meta.get("maxzoom", "11"))
            offline_layers.append({
                "name": "Offline Google Satellite Hybrid",
                "url": self.tile_server.tile_url("google"),
                "attribution": "Offline Google/SAS.Planet",
                "max_native_zoom": max_native,
                "max_zoom": 22,
                "overlay": False,
                "show": False,
            })
        if "navionics" in available:
            meta = self.tile_server.layer_info("navionics")
            max_native = int(meta.get("maxzoom", "11"))
            offline_layers.append({
                "name": "Offline Navionics Deniz",
                "url": self.tile_server.tile_url("navionics"),
                "attribution": "Offline Navionics/SAS.Planet",
                "max_native_zoom": max_native,
                "max_zoom": 22,
                "overlay": False,
                "show": False,
            })
        self.map.set_offline_layers(offline_layers)

    def _init_real_devices(self) -> None:
        """Gerçek GNSS cihazlarını kayıt et ve bağlantıyı başlat."""
        if self.state.devices:
            return

        real = [
            ("gps1", "GPS-1", "188.59.186.241", 4012),
            ("gps2", "GPS-2", "5.26.86.114",    4012),
        ]
        for device_id, name, ip, port in real:
            dev = DeviceEntry(
                name=name,
                ip=ip,
                port=port,
                device_id=device_id,
                status="Connecting",
            )
            self.state.devices[device_id] = dev
            try:
                config = SocketConfig(host=ip, port=port)
                listener = TCPListener(config, lambda line, did=device_id: self._on_nmea_line(did, line))
                self.listeners[device_id] = listener
                listener.start_listening()
            except Exception as exc:
                dev.status = "Offline"
                self.state.last_error = str(exc)

        self.state.is_connected = bool(self.listeners)

    def run(self) -> None:
        import sys
        qInstallMessageHandler(qt_message_filter)
        qt_app = QApplication(sys.argv)
        qt_app.setStyle("Fusion")
        self.setup(qt_app)
        qt_app.exec()

    def shutdown(self) -> None:
        self.disconnect()
        if self.taskbar:
            self.taskbar.clear_overlay()
        if self.tile_server:
            self.tile_server.stop()
        self.logger.close()
        self.session_logger.stop()

    def connect(self, host: str, port_text: str, device_name: str) -> None:
        try:
            config = SocketConfig(host=host.strip(), port=int(port_text))
        except (TypeError, ValueError) as exc:
            self.state.last_error = str(exc)
            return

        display_name = device_name.strip() or f"GPS-{len(self.state.devices) + 1}"
        device_id = self._new_device_id(display_name)
        mode = "BASE" if display_name.lower() == "base" else "GPS"
        self.state.devices[device_id] = DeviceEntry(
            name=display_name,
            ip=host.strip(),
            port=int(port_text),
            device_id=device_id,
            status="Active",
            mode=mode,
        )
        listener = TCPListener(config, lambda line, did=device_id: self._on_nmea_line(did, line))
        self.listeners[device_id] = listener
        listener.start_listening()
        self.state.is_connected = True
        self.state.last_error = ""
        if self.taskbar:
            self.taskbar.set_connected_state()

    def disconnect(self) -> None:
        for listener in self.listeners.values():
            listener.stop_listening()
        self.listeners.clear()
        self.state.is_connected = False
        for dev in self.state.devices.values():
            dev.status = "Offline"
        if self.taskbar:
            self.taskbar.set_disconnected_state()

    def toggle_device_connection(self, device_id: str) -> None:
        dev = self.state.devices.get(device_id)
        if dev is None:
            return

        listener = self.listeners.pop(device_id, None)
        if listener is not None:
            listener.stop_listening()
            dev.status = "Offline"
            self.state.is_connected = bool(self.listeners)
            if not self.state.is_connected and self.taskbar:
                self.taskbar.set_disconnected_state()
            self._markers_dirty = True
            return

        if dev.ip == "-" or dev.port <= 0:
            self.state.last_error = f"{dev.name} icin gecerli IP/Port yok"
            return

        try:
            config = SocketConfig(host=dev.ip, port=dev.port)
        except ValueError as exc:
            self.state.last_error = str(exc)
            return

        new_listener = TCPListener(config, lambda line, did=device_id: self._on_nmea_line(did, line))
        self.listeners[device_id] = new_listener
        new_listener.start_listening()
        dev.status = "Active"
        self.state.is_connected = True
        self.state.last_error = ""
        if self.taskbar:
            self.taskbar.set_connected_state()
        self._markers_dirty = True

    def remove_device(self, device_id: str) -> None:
        listener = self.listeners.pop(device_id, None)
        if listener is not None:
            listener.stop_listening()

        self.state.devices.pop(device_id, None)
        self.state.source_coords.pop(device_id, None)
        self.latest_fixes.pop(device_id, None)
        self._recompute_primary_midpoint()

        self.state.is_connected = bool(self.listeners)
        self._markers_dirty = True
        if not self.state.is_connected and self.taskbar:
            self.taskbar.set_disconnected_state()

    def update_device(self, device_id: str, new_name: str, new_ip: str, new_port: int) -> None:
        dev = self.state.devices.get(device_id)
        if dev is None:
            return

        endpoint_changed = dev.ip != new_ip or dev.port != new_port
        dev.name = new_name
        dev.ip = new_ip
        dev.port = new_port

        if endpoint_changed and device_id in self.listeners:
            old_listener = self.listeners.pop(device_id)
            old_listener.stop_listening()
            if dev.ip != "-" and dev.port > 0:
                config = SocketConfig(host=dev.ip, port=dev.port)
                listener = TCPListener(config, lambda line, did=device_id: self._on_nmea_line(did, line))
                self.listeners[device_id] = listener
                listener.start_listening()
            self.state.is_connected = bool(self.listeners)

        self.map.update_source_markers(self._map_sources())
        self._markers_dirty = True

    def toggle_map_pause(self) -> None:
        self.state.is_map_paused = not self.state.is_map_paused

    def reset_filter(self, window_size: int) -> None:
        self.filter = MovingAverageFilter(window_size=window_size)

    def _new_device_id(self, display_name: str) -> str:
        base = "".join(ch.lower() for ch in display_name if ch.isalnum()) or "gps"
        candidate = base
        idx = 2
        while candidate in self.state.devices:
            candidate = f"{base}{idx}"
            idx += 1
        return candidate

    def _map_sources(self) -> Dict[str, Dict]:
        result = {
            dev.device_id: {
                "name": dev.name,
                "lat":  dev.latitude,
                "lon":  dev.longitude,
            }
            for dev in self.state.devices.values()
            if dev.has_position
        }
        return result

    def _primary_fixes(self) -> List[GNSSFix]:
        return [
            self.latest_fixes[device_id]
            for device_id in (self.state.primary_source_id, self.state.primary_target_id)
            if device_id in self.latest_fixes
        ]

    def _recompute_primary_midpoint(self) -> None:
        midpoint = compute_spherical_midpoint(self._primary_fixes())
        if midpoint is None:
            self.state.raw_midpoint = None
            self.state.filtered_midpoint = None
            self.filter.reset()
            self.map.clear_all_markers()
            self.map.update_source_markers(self._map_sources())
            return

        self.state.raw_midpoint = (midpoint.latitude, midpoint.longitude)
        filtered = self.filter.update(midpoint.latitude, midpoint.longitude)
        self.map.update_source_markers(self._map_sources())
        self.map.update_raw_midpoint(midpoint.latitude, midpoint.longitude)
        if filtered is not None:
            self.state.filtered_midpoint = (filtered.latitude, filtered.longitude)
            self.map.update_filtered_midpoint(filtered.latitude, filtered.longitude)

    def _on_nmea_line(self, device_id: str, line: str) -> None:
        sentences = extract_gpgga(line)
        if not sentences and line.startswith("$"):
            sentences = [line]
        for sentence in sentences:
            fix = parse_gpgga(sentence)
            if fix is not None:
                self._process_fix(device_id, fix)

    def _process_fix(self, device_id: str, fix: GNSSFix) -> None:
        self.state.source_coords[device_id] = (fix.latitude, fix.longitude)
        if device_id in self.state.devices:
            dev = self.state.devices[device_id]
            dev.latitude = fix.latitude
            dev.longitude = fix.longitude
            dev.altitude = fix.altitude
            dev.fix_quality = fix.fix_quality
            dev.satellites = fix.satellites
            dev.hdop = fix.hdop
            dev.fix_type = FIX_LABELS.get(fix.fix_quality, f"Q{fix.fix_quality}")
            dev.status = "Active"
            dev.last_update_ms = fix.timestamp_ms

        self.latest_fixes[device_id] = fix
        midpoint = compute_spherical_midpoint(self._primary_fixes())
        if midpoint is None:
            self.map.update_source_markers(self._map_sources())
            self._markers_dirty = True
            return

        self.state.raw_midpoint = (midpoint.latitude, midpoint.longitude)
        filtered = self.filter.update(midpoint.latitude, midpoint.longitude)
        if filtered is None:
            self.map.update_source_markers(self._map_sources())
            self._markers_dirty = True
            return

        self.state.filtered_midpoint = (filtered.latitude, filtered.longitude)
        self.map.update_source_markers(self._map_sources())
        self.map.update_raw_midpoint(midpoint.latitude, midpoint.longitude)
        self.map.update_filtered_midpoint(filtered.latitude, filtered.longitude)
        self._markers_dirty = True

        if self.state.is_comparison_active:
            self.state.deviation_m = self.logger.comparison_hook("legacy_output.csv")

        self.logger.write(LogRecord(
            timestamp_ms=fix.timestamp_ms,
            raw_lat=midpoint.latitude, raw_lon=midpoint.longitude,
            filtered_lat=filtered.latitude, filtered_lon=filtered.longitude,
            altitude=fix.altitude, fix_quality=fix.fix_quality,
            deviation_m=self.state.deviation_m,
        ))



if __name__ == "__main__":
    app = Application()
    app.run()
