"""
filter.py - Deque backed simple moving average for geographic points.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import logging
import math
from typing import Deque, Optional, Tuple


LOGGER = logging.getLogger(__name__)
EARTH_RADIUS_M = 6371008.8


@dataclass
class FilteredPoint:
    """Filtered coordinate output."""

    latitude: float
    longitude: float
    window_size: int
    sample_count: int


class MovingAverageFilter:
    """SMA filter with Haversine based outlier rejection."""

    def __init__(self, window_size: int = 5, outlier_threshold_m: float = 50.0) -> None:
        if window_size < 1:
            raise ValueError("window_size must be at least 1")
        if outlier_threshold_m <= 0:
            raise ValueError("outlier_threshold_m must be positive")

        self.window_size = int(window_size)
        self.outlier_threshold_m = float(outlier_threshold_m)
        self._points: Deque[Tuple[float, float]] = deque(maxlen=self.window_size)

    def update(self, latitude: float, longitude: float) -> Optional[FilteredPoint]:
        """Add a coordinate and return the current moving average."""
        self._validate_coordinate(latitude, longitude)
        if self._is_outlier(latitude, longitude):
            return None

        self._points.append((latitude, longitude))
        avg = self.current_average
        if avg is None:
            return None

        return FilteredPoint(
            latitude=avg[0],
            longitude=avg[1],
            window_size=self.window_size,
            sample_count=len(self._points),
        )

    def reset(self) -> None:
        """Clear the filter history."""
        self._points.clear()

    @property
    def current_average(self) -> Optional[Tuple[float, float]]:
        """Return the current arithmetic SMA over the local filter window."""
        if not self._points:
            return None
        lat_sum = sum(point[0] for point in self._points)
        lon_sum = sum(point[1] for point in self._points)
        count = len(self._points)
        return lat_sum / count, lon_sum / count

    @property
    def is_warm(self) -> bool:
        """Return True once the window has N samples."""
        return len(self._points) == self.window_size

    def _is_outlier(self, latitude: float, longitude: float) -> bool:
        if not self._points:
            return False

        avg = self.current_average
        if avg is None:
            return False

        distance_m = self._haversine_distance_m(avg[0], avg[1], latitude, longitude)
        if distance_m > self.outlier_threshold_m:
            LOGGER.warning(
                "Rejected outlier coordinate lat=%s lon=%s distance_m=%.3f threshold_m=%.3f",
                latitude,
                longitude,
                distance_m,
                self.outlier_threshold_m,
            )
            return True
        return False

    def _haversine_distance_m(
        self,
        lat1: float,
        lon1: float,
        lat2: float,
        lon2: float,
    ) -> float:
        """Return Haversine distance in meters."""
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        d_phi = math.radians(lat2 - lat1)
        d_lambda = math.radians(lon2 - lon1)

        a = (
            math.sin(d_phi / 2.0) ** 2
            + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2.0) ** 2
        )
        c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
        return EARTH_RADIUS_M * c

    @staticmethod
    def _validate_coordinate(latitude: float, longitude: float) -> None:
        if not (-90.0 <= latitude <= 90.0):
            raise ValueError("latitude out of range")
        if not (-180.0 <= longitude <= 180.0):
            raise ValueError("longitude out of range")
