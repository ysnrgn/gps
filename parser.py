"""
parser.py - NMEA-0183 parser and spherical midpoint math.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
import time
from typing import List, Optional, Tuple


GPGGA_RE = re.compile(r"\$(?:GP|GN|GA|GB|GL)GGA,[^*\r\n]*(?:\*[0-9A-Fa-f]{2})?")


@dataclass
class GNSSFix:
    """Parsed location from one GNSS receiver."""

    timestamp_ms: int
    latitude: float
    longitude: float
    altitude: float
    fix_quality: int
    satellites: int
    hdop: float
    raw_sentence: str
    nmea_utc: str = ""  # "HH:MM:SS" — GPGGA fields[1]'den UTC saat


@dataclass
class Midpoint:
    """Spherical midpoint computed from two or more source fixes."""

    latitude: float
    longitude: float
    source_count: int


def validate_checksum(sentence: str) -> bool:
    """Validate the XOR based NMEA checksum."""
    sentence = sentence.strip()
    if not sentence.startswith("$") or "*" not in sentence:
        return False

    body, checksum_text = sentence[1:].split("*", 1)
    checksum_text = checksum_text[:2]
    if len(checksum_text) != 2:
        return False

    checksum = 0
    for char in body:
        checksum ^= ord(char)

    try:
        expected = int(checksum_text, 16)
    except ValueError:
        return False
    return checksum == expected


def extract_gpgga(buffer: str) -> List[str]:
    """Extract GGA sentences from a raw text buffer."""
    sentences: List[str] = []
    for match in GPGGA_RE.finditer(buffer):
        sentence = match.group(0).strip()
        if "*" in sentence:
            if validate_checksum(sentence):
                sentences.append(sentence)
        else:
            sentences.append(sentence)
    return sentences


def parse_gpgga(sentence: str) -> Optional[GNSSFix]:
    """Parse a GGA sentence into a GNSSFix."""
    sentence = sentence.strip()
    if "*" in sentence and not validate_checksum(sentence):
        return None

    payload = sentence[1:] if sentence.startswith("$") else sentence
    payload = payload.split("*", 1)[0]
    fields = payload.split(",")
    if len(fields) < 10 or not fields[0].endswith("GGA"):
        return None

    try:
        lat = nmea_to_decimal(fields[2], fields[3])
        lon = nmea_to_decimal(fields[4], fields[5])
        fix_quality = int(fields[6] or 0)
        satellites = int(fields[7] or 0)
        hdop = float(fields[8]) if fields[8] else 0.0
        altitude = float(fields[9]) if fields[9] else 0.0
    except (ValueError, IndexError):
        return None

    if fix_quality <= 0:
        return None

    nmea_utc = ""
    raw_time = fields[1].split(".")[0] if fields[1] else ""
    if len(raw_time) >= 6:
        nmea_utc = f"{raw_time[0:2]}:{raw_time[2:4]}:{raw_time[4:6]}"

    return GNSSFix(
        timestamp_ms=int(time.time() * 1000),
        latitude=lat,
        longitude=lon,
        altitude=altitude,
        fix_quality=fix_quality,
        satellites=satellites,
        hdop=hdop,
        raw_sentence=sentence,
        nmea_utc=nmea_utc,
    )


def nmea_to_decimal(raw: str, direction: str) -> float:
    """Convert NMEA DDMM.MMMM / DDDMM.MMMM into signed decimal degrees."""
    if not raw or direction not in {"N", "S", "E", "W"}:
        raise ValueError("invalid NMEA coordinate")

    value = float(raw)
    degrees = int(value // 100)
    minutes = value - (degrees * 100)
    if not (0.0 <= minutes < 60.0):
        raise ValueError("invalid NMEA minute component")

    decimal = degrees + (minutes / 60.0)
    if direction in {"S", "W"}:
        decimal *= -1.0
    return decimal


def compute_spherical_midpoint(fixes: List[GNSSFix]) -> Optional[Midpoint]:
    """Compute the true geographic midpoint on a unit sphere."""
    if len(fixes) < 2:
        return None

    x_sum = y_sum = z_sum = 0.0
    for fix in fixes:
        x, y, z = _to_cartesian(fix.latitude, fix.longitude)
        x_sum += x
        y_sum += y
        z_sum += z

    count = float(len(fixes))
    x_avg = x_sum / count
    y_avg = y_sum / count
    z_avg = z_sum / count

    if math.isclose(x_avg, 0.0, abs_tol=1e-15) and math.isclose(y_avg, 0.0, abs_tol=1e-15):
        return None

    lat, lon = _from_cartesian(x_avg, y_avg, z_avg)
    return Midpoint(latitude=lat, longitude=lon, source_count=len(fixes))


def _to_cartesian(lat_deg: float, lon_deg: float) -> Tuple[float, float, float]:
    """Convert latitude/longitude degrees into unit-sphere X/Y/Z."""
    lat_rad = math.radians(lat_deg)
    lon_rad = math.radians(lon_deg)
    x = math.cos(lat_rad) * math.cos(lon_rad)
    y = math.cos(lat_rad) * math.sin(lon_rad)
    z = math.sin(lat_rad)
    return x, y, z


def _from_cartesian(x: float, y: float, z: float) -> Tuple[float, float]:
    """Convert unit-sphere X/Y/Z back into latitude/longitude degrees."""
    hyp = math.hypot(x, y)
    lat = math.degrees(math.atan2(z, hyp))
    lon = math.degrees(math.atan2(y, x))
    return lat, lon
