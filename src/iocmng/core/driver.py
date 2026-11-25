"""Driver source: PVs read from, and written to, a device driver.

A *driver* is any object with this interface (the same one epik8s-pyioc
generates)::

    connect() -> None
    close() -> None
    read(names: list[str]) -> dict[str, value]   # missing or None = not published
    write(name: str, value) -> None
    command(name: str) -> None

Enum and bool values are indexes (0, 1, ...); waveforms are any sequence of
numbers.  The driver is synchronous: :class:`DriverEngine` calls it from one
dedicated thread, one call at a time, so a slow or hung device never blocks
the soft IOC dispatcher, CA clients, or the task's rule evaluation.
"""

from __future__ import annotations

import logging
import math
import queue
import random
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional


class DriverEngine:
    """Run a driver in its own thread: connect, poll, write, reconnect.

    Args:
        driver: the driver object.
        names: PV names read with ``driver.read`` each period.
        period: seconds between reads.
        on_values: called with ``{name: value}`` after each successful read.
        on_connection: called with ``True``/``False`` when the state changes.
        on_error: called with ``(exception, context)`` for every failure.
        retry: seconds between reconnection attempts.
    """

    def __init__(
        self,
        driver: Any,
        names: List[str],
        period: float,
        on_values: Callable[[Dict[str, Any]], None],
        on_connection: Callable[[bool], None],
        on_error: Callable[[Exception, str], None],
        retry: Optional[float] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.driver = driver
        self.names = list(names)
        self.period = max(float(period), 0.01)
        self.retry = float(retry) if retry is not None else max(self.period, 1.0)
        self.on_values = on_values
        self.on_connection = on_connection
        self.on_error = on_error
        self.logger = logger or logging.getLogger("iocmng.driver")
        self.connected = False
        self._requests: "queue.Queue[tuple]" = queue.Queue()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    # ── public API ───────────────────────────────────────────────────

    def start(self, name: str = "driver") -> None:
        self._running = True
        self._thread = threading.Thread(target=self._loop, name=f"{name}-driver", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._running = False
        self._requests.put(("stop", None, None))
        if self._thread and self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=timeout)
        self._close()

    def submit_write(self, name: str, value: Any) -> None:
        """Queue ``driver.write(name, value)``; returns at once."""
        self._requests.put(("write", name, value))

    def submit_command(self, name: str, after: Optional[Callable[[], None]] = None) -> None:
        """Queue ``driver.command(name)``; returns at once.

        *after* runs in the driver thread once the command is done, failed or
        dropped (e.g. to put the command PV back to Idle).
        """
        self._requests.put(("command", name, after))

    # ── worker thread ────────────────────────────────────────────────

    def _loop(self) -> None:
        next_read = 0.0
        while self._running:
            if not self.connected:
                if not self._connect():
                    self._wait(self.retry)
                    continue
                next_read = 0.0
            # Serve writes and commands as they arrive; read when due.
            timeout = max(0.0, next_read - time.monotonic())
            try:
                kind, name, value = self._requests.get(timeout=timeout)
            except queue.Empty:
                kind = None
            if kind == "stop":
                break
            if kind is not None:
                self._serve(kind, name, value)
                continue
            next_read = time.monotonic() + self.period
            self._read()

    def _wait(self, seconds: float) -> None:
        """Sleep, but wake up for stop; drop writes made while disconnected."""
        deadline = time.monotonic() + seconds
        while self._running:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                kind, name, extra = self._requests.get(timeout=remaining)
            except queue.Empty:
                return
            if kind == "stop":
                return
            self.on_error(RuntimeError(f"{kind} {name}: device is not connected"), f"{kind} {name}")
            if kind == "command":
                self._after(extra)

    def _connect(self) -> bool:
        try:
            self.driver.connect()
        except Exception as exc:
            self.on_error(exc, "connect failed")
            return False
        self.connected = True
        self.logger.info("driver connected")
        self.on_connection(True)
        return True

    def _close(self) -> None:
        try:
            self.driver.close()
        except Exception as exc:  # closing a broken connection may fail too
            self.logger.debug("driver close: %s", exc)
        if self.connected:
            self.connected = False
            self.on_connection(False)

    def _read(self) -> None:
        if not self.names:
            return
        try:
            values = self.driver.read(list(self.names))
        except Exception as exc:
            # A failed read means the device is gone: reconnect.
            self.on_error(exc, "device communication failed")
            self._close()
            return
        self.on_values(dict(values or {}))

    def _serve(self, kind: str, name: str, value: Any) -> None:
        try:
            if kind == "write":
                self.driver.write(name, value)
            else:
                self.driver.command(name)
        except Exception as exc:
            # A refused setting is not a lost device: report, stay connected.
            self.on_error(exc, f"{kind} {name} failed")
        finally:
            if kind == "command":
                self._after(value)

    def _after(self, callback: Optional[Callable[[], None]]) -> None:
        if callback is None:
            return
        try:
            callback()
        except Exception as exc:
            self.logger.warning("command completion callback failed: %s", exc)


class SimulatedDriver:
    """A driver that keeps values in memory: the IOC runs with no device.

    Started values come from each PV's ``value``; float readbacks drift a
    little so the IOC looks alive, waveforms hold a sine.
    """

    def __init__(self, specs: Mapping[str, Any]):
        self.specs = dict(specs)
        self.values: Dict[str, Any] = {}
        for name, spec in self.specs.items():
            if spec.type == "waveform":
                self.values[name] = [math.sin(i / 20.0) for i in range(spec.length or 0)]
            elif spec.type == "string":
                self.values[name] = str(spec.value or "")
            elif spec.type == "enum" and isinstance(spec.value, str):
                self.values[name] = spec.choices.index(spec.value) if spec.value in spec.choices else 0
            else:
                self.values[name] = spec.value if spec.value is not None else 0
        self.connected = False
        self.commands: List[str] = []

    def connect(self) -> None:
        self.connected = True

    def close(self) -> None:
        self.connected = False

    def read(self, names: List[str]) -> Dict[str, Any]:
        out = {}
        for name in names:
            value = self.values.get(name)
            spec = self.specs.get(name)
            if spec is not None and spec.type == "float" and spec.access == "ro":
                # 1 % relative drift: alive, and the same order of magnitude
                value = float(value or 0.0) * (1.0 + random.uniform(-0.01, 0.01))
            out[name] = value
        return out

    def write(self, name: str, value: Any) -> None:
        self.values[name] = value

    def command(self, name: str) -> None:
        self.commands.append(name)
