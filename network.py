"""
network.py - TCP socket listener.

This module owns only transport concerns: connect, receive line-oriented
telemetry, survive disconnects, and reconnect while the listener is running.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Callable, Optional


class SocketConfig:
    """TCP connection settings."""

    def __init__(
        self,
        host: str,
        port: int,
        buffer_size: int = 4096,
        timeout: float = 5.0,
        reconnect_initial_delay: float = 1.0,
        reconnect_max_delay: float = 30.0,
    ) -> None:
        if not host:
            raise ValueError("host must not be empty")
        if not (0 < int(port) < 65536):
            raise ValueError("port must be between 1 and 65535")
        if buffer_size < 128:
            raise ValueError("buffer_size must be at least 128 bytes")

        self.host = host
        self.port = int(port)
        self.buffer_size = int(buffer_size)
        self.timeout = float(timeout)
        self.reconnect_initial_delay = float(reconnect_initial_delay)
        self.reconnect_max_delay = float(reconnect_max_delay)


class TCPListener:
    """
    Resilient TCP client for GNSS/NMEA streams.

    The field device or simulator is expected to expose a TCP endpoint. This
    listener connects to it, reads complete lines, and calls on_line_received.
    """

    def __init__(self, config: SocketConfig, on_line_received: Callable[[str], None]) -> None:
        self.config = config
        self.on_line_received = on_line_received
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()
        self._connected = False
        self._last_packet_at: Optional[float] = None
        self._last_latency_ms: Optional[float] = None

    def connect(self) -> bool:
        """Open a TCP connection to the configured endpoint."""
        with self._lock:
            self.disconnect()
            try:
                sock = socket.create_connection(
                    (self.config.host, self.config.port),
                    timeout=self.config.timeout,
                )
                sock.settimeout(self.config.timeout)
                self._sock = sock
                self._connected = True
                self._last_packet_at = time.monotonic()
                self._last_latency_ms = None
                return True
            except OSError:
                self._sock = None
                self._connected = False
                return False

    def disconnect(self) -> None:
        """Close the active socket without stopping the listener thread."""
        sock = self._sock
        self._sock = None
        self._connected = False
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def start_listening(self) -> None:
        """Start the background read loop."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._read_loop, name="TCPListener", daemon=True)
        self._thread.start()

    def stop_listening(self) -> None:
        """Stop reading and close the socket."""
        self._stop_event.set()
        self.disconnect()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self.config.timeout + 1.0)

    def _auto_reconnect(self) -> None:
        delay = self.config.reconnect_initial_delay
        while not self._stop_event.is_set():
            if self.connect():
                return
            self._stop_event.wait(delay)
            delay = min(delay * 2.0, self.config.reconnect_max_delay)

    def _read_loop(self) -> None:
        pending = ""
        while not self._stop_event.is_set():
            if not self.is_connected:
                self._auto_reconnect()
                pending = ""
                continue

            sock = self._sock
            if sock is None:
                self._connected = False
                continue

            try:
                chunk = sock.recv(self.config.buffer_size)
                if not chunk:
                    self.disconnect()
                    continue

                now = time.monotonic()
                if self._last_packet_at is not None:
                    self._last_latency_ms = (now - self._last_packet_at) * 1000.0
                self._last_packet_at = now

                pending += chunk.decode("ascii", errors="ignore")
                lines = pending.replace("\r\n", "\n").replace("\r", "\n").split("\n")
                pending = lines.pop() if lines else ""
                for line in lines:
                    line = line.strip()
                    if line:
                        self.on_line_received(line)
            except (ConnectionResetError, ConnectionAbortedError, TimeoutError, socket.timeout, OSError):
                self.disconnect()
            except Exception:
                self.disconnect()

    @property
    def is_connected(self) -> bool:
        """Return whether the transport currently has an open socket."""
        return self._connected and self._sock is not None

    @property
    def latency_ms(self) -> Optional[float]:
        """Return the observed gap between the last two packets in milliseconds."""
        return self._last_latency_ms
