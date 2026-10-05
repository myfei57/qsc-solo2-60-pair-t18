"""Field input/output for the backwash sequence.

The controller never infers a valve or pump position from the command it
issued. Every confirmation is read back as a separate position/status signal
through this interface. A command says "move", a position says "arrived";
keeping the two apart is what makes "close first, drain second" enforceable.

The two valve actuators are the bed isolation valve and the bed drain valve.
The backwash pump is the only moving piece during the flush itself.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

# Positions reported by a valve actuator.
VALVE_OPEN = "open"
VALVE_CLOSED = "closed"
VALVE_MOVING = "moving"
VALVE_STUCK = "stuck"
VALVE_UNKNOWN = "unknown"

# Status reported by the backwash pump.
PUMP_RUNNING = "running"
PUMP_STOPPED = "stopped"
PUMP_FAULT = "fault"
PUMP_UNKNOWN = "unknown"

VALVE_POSITIONS = (VALVE_OPEN, VALVE_CLOSED, VALVE_MOVING, VALVE_STUCK, VALVE_UNKNOWN)
PUMP_STATUSES = (PUMP_RUNNING, PUMP_STOPPED, PUMP_FAULT, PUMP_UNKNOWN)


@runtime_checkable
class FieldBus(Protocol):
    """The signals the backwash controller is allowed to act on."""

    def isolation_position(self, bed_id: str) -> str:
        """Read back the bed isolation (inlet) valve position."""

    def drain_position(self, bed_id: str) -> str:
        """Read back the drain valve position."""

    def pump_status(self, bed_id: str) -> str:
        """Read back the backwash pump run status."""

    def close_isolation(self, bed_id: str) -> None:
        """Command the bed isolation valve shut."""

    def open_isolation(self, bed_id: str) -> None:
        """Command the bed isolation valve open."""

    def close_drain(self, bed_id: str) -> None:
        """Command the drain valve shut."""

    def open_drain(self, bed_id: str) -> None:
        """Command the drain valve open."""

    def stop_pump(self, bed_id: str) -> None:
        """Command the backwash pump to stop."""

    def start_pump(self, bed_id: str) -> None:
        """Command the backwash pump to run."""

    def tick(self, seconds: float = 0.0) -> None:
        """Advance the simulated field; a real bus treats this as a no-op."""
