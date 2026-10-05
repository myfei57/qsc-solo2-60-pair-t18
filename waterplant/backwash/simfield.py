"""Deterministic in-memory field bus used by the console and the test suite.

A real deployment swaps this for a driver that reads limit switches and pump
auxiliaries. Here devices move on an explicit simulation clock so tests can
reproduce every feedback path:

* a well behaved valve travels for ``travel_seconds`` and then reports the
  position its last command asked for;
* a ``stuck`` device keeps reporting :data:`VALVE_STUCK` (or a pump fault) no
  matter how many commands it receives, which drives the step timeout;
* ``unknown`` models a missing or failed position signal, which is treated as
  "not confirmed" exactly like a stuck device.
"""

from __future__ import annotations

import threading

from .field import (
    PUMP_FAULT,
    PUMP_RUNNING,
    PUMP_STATUSES,
    PUMP_STOPPED,
    PUMP_UNKNOWN,
    VALVE_CLOSED,
    VALVE_MOVING,
    VALVE_OPEN,
    VALVE_POSITIONS,
    VALVE_STUCK,
    VALVE_UNKNOWN,
)

DEFAULT_TRAVEL_SECONDS = 5.0


class _Device:
    """One simulated valve or pump with a feedback mode."""

    def __init__(self, kind: str, travel_seconds: float) -> None:
        self.kind = kind  # "valve" or "pump"
        self.travel_seconds = max(0.0, travel_seconds)
        self.mode = "normal"
        self.reset()

    def reset(self) -> None:
        if self.kind == "valve":
            self.position = VALVE_CLOSED
            self._target = VALVE_CLOSED
        else:
            self.position = PUMP_STOPPED
            self._target = PUMP_STOPPED
        self._elapsed = 0.0
        self.command_count = 0

    def command(self, target: str) -> None:
        self.command_count += 1
        if self.mode == "stuck":
            return
        if self.mode == "unknown":
            return
        if self.position == target:
            self._target = target
            self._elapsed = self.travel_seconds
            return
        self._target = target
        if self.travel_seconds <= 0.0:
            # A fast actuator reads back in position on the very next scan.
            self.position = target
            self._elapsed = 0.0
            return
        if self.kind == "valve":
            self.position = VALVE_MOVING
        self._elapsed = 0.0

    def advance(self, seconds: float) -> None:
        if self.mode == "stuck":
            self.position = VALVE_STUCK if self.kind == "valve" else PUMP_FAULT
            return
        if self.mode == "unknown":
            self.position = VALVE_UNKNOWN if self.kind == "valve" else PUMP_UNKNOWN
            return
        if self.position == self._target:
            return
        self._elapsed += seconds
        if self._elapsed >= self.travel_seconds:
            self.position = self._target


class SimFieldBus:
    """Thread safe simulated valve and pump set for every registered bed."""

    def __init__(self, travel_seconds: float = DEFAULT_TRAVEL_SECONDS) -> None:
        self._travel_seconds = travel_seconds
        self._lock = threading.RLock()
        self._devices: dict[str, dict[str, _Device]] = {}

    def register(self, bed_id: str) -> None:
        """Create the isolation valve, drain valve and pump for one bed."""

        with self._lock:
            self._devices[bed_id] = {
                "isolation": _Device("valve", self._travel_seconds),
                "drain": _Device("valve", self._travel_seconds),
                "pump": _Device("pump", self._travel_seconds),
            }
            self._devices[bed_id]["isolation"].position = VALVE_OPEN

    def _device(self, bed_id: str, name: str) -> _Device:
        try:
            return self._devices[bed_id][name]
        except KeyError as exc:
            raise ValueError(f"field device {name} for filter bed {bed_id} not found") from exc

    def set_mode(self, bed_id: str, name: str, mode: str) -> None:
        """Put a device into ``normal``, ``stuck`` or ``unknown`` feedback mode."""

        with self._lock:
            device = self._device(bed_id, name)
            if mode not in ("normal", "stuck", "unknown"):
                raise ValueError(f"unknown device mode {mode}")
            device.mode = mode
            if mode == "stuck":
                # The fault shows up on the next feedback scan, without
                # waiting for another command or a clock tick.
                device.position = VALVE_STUCK if device.kind == "valve" else PUMP_FAULT
            elif mode == "unknown":
                device.position = VALVE_UNKNOWN if device.kind == "valve" else PUMP_UNKNOWN

    def command_count(self, bed_id: str, name: str) -> int:
        with self._lock:
            return self._device(bed_id, name).command_count

    # --- feedback reads -------------------------------------------------

    def isolation_position(self, bed_id: str) -> str:
        with self._lock:
            return self._device(bed_id, "isolation").position

    def drain_position(self, bed_id: str) -> str:
        with self._lock:
            return self._device(bed_id, "drain").position

    def pump_status(self, bed_id: str) -> str:
        with self._lock:
            return self._device(bed_id, "pump").position

    # --- commands -------------------------------------------------------

    def close_isolation(self, bed_id: str) -> None:
        with self._lock:
            self._device(bed_id, "isolation").command(VALVE_CLOSED)

    def open_isolation(self, bed_id: str) -> None:
        with self._lock:
            self._device(bed_id, "isolation").command(VALVE_OPEN)

    def close_drain(self, bed_id: str) -> None:
        with self._lock:
            self._device(bed_id, "drain").command(VALVE_CLOSED)

    def open_drain(self, bed_id: str) -> None:
        with self._lock:
            self._device(bed_id, "drain").command(VALVE_OPEN)

    def stop_pump(self, bed_id: str) -> None:
        with self._lock:
            self._device(bed_id, "pump").command(PUMP_STOPPED)

    def start_pump(self, bed_id: str) -> None:
        with self._lock:
            self._device(bed_id, "pump").command(PUMP_RUNNING)

    # --- simulation clock ----------------------------------------------

    def tick(self, seconds: float = 0.0) -> None:
        if seconds < 0:
            raise ValueError("tick seconds must be non-negative")
        with self._lock:
            for devices in self._devices.values():
                for device in devices.values():
                    device.advance(seconds)


def is_valve_position(value: str) -> bool:
    return value in VALVE_POSITIONS


def is_pump_status(value: str) -> bool:
    return value in PUMP_STATUSES
