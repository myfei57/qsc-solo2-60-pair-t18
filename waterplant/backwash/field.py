"""Field I/O boundary for the backwash sequencer.

The old controller treated ``bank.close()`` as "the bed is closed". On a real
plant a command is only a request: the valve can stick, the signal can fail and
the feedback limit switch is what proves the device actually got there.

Every device therefore has **two independent positions**:

* ``commanded`` -- the last position the controller requested ("OPEN"/"CLOSE");
* ``actual``    -- the position reported back by the field ("OPEN"/"CLOSE").

A step only completes when ``actual`` reaches the position the step requires.
Time never advances a step; it only trips the watchdog when ``actual`` fails to
follow ``commanded`` before the deadline.

:class:`SimulatedField` is the in-memory plant used by the service and the
tests: healthy devices echo the command on the next :meth:`pump`, devices can
be stuck or have their feedback forced to simulate partial failure, and the
clearwell level is an independent live process signal that can drop mid-wash.
"""

from __future__ import annotations

import threading
from typing import Protocol

OPEN = "OPEN"
CLOSE = "CLOSE"
RUNNING = "RUNNING"
STOPPED = "STOPPED"

# Field devices belonging to one bed.
ISOLATION = "isolation"      # bed isolation valve (filtering <-> isolated)
DRAIN = "drain"              # drain / blowdown valve to the sewer
WASH = "wash"                # backwash water supply valve (clearwell side)
AIR = "air"                  # air scour supply valve
RINSE = "rinse"              # forward rinse / inlet valve

BED_VALVES = (ISOLATION, DRAIN, WASH, AIR, RINSE)
PUMPS = ("wash_pump", "air_blower")

VALVE_TARGETS = {OPEN, CLOSE}
PUMP_TARGETS = {RUNNING, STOPPED}


class Field(Protocol):
    """Hardware surface the sequencer drives and reads back."""

    def command_valve(self, bed_id: str, valve: str, target: str) -> None: ...

    def valve_command(self, bed_id: str, valve: str) -> str: ...

    def valve_position(self, bed_id: str, valve: str) -> str: ...

    def command_pump(self, pump: str, target: str) -> None: ...

    def pump_command(self, pump: str) -> str: ...

    def pump_state(self, pump: str) -> str: ...

    def clearwell_level(self) -> float: ...

    def set_clearwell_level(self, level: float) -> None: ...

    def pump(self) -> None:
        """Let the simulated field react once to outstanding commands."""


def require_valve_target(target: str) -> None:
    if target not in VALVE_TARGETS:
        raise ValueError(f"valve target must be OPEN or CLOSE, got {target!r}")


def require_pump_target(target: str) -> None:
    if target not in PUMP_TARGETS:
        raise ValueError(f"pump target must be RUNNING or STOPPED, got {target!r}")


class _Valve:
    """One valve with a commanded side and a reported side that can disagree."""

    def __init__(self, name: str, fail_position: str = CLOSE) -> None:
        self.name = name
        self.fail_position = fail_position
        self.commanded = fail_position
        self.actual = fail_position
        # When stuck the physical valve no longer follows new commands; the
        # feedback therefore freezes wherever it was.
        self.stuck = False
        # Armed by seize(): the command is recorded but, on the first command
        # that would move the valve away from its current position, the
        # mechanism freezes instead of following it.
        self.seize_on_move = False
        # When True the feedback line lies (signal failure) even though the
        # mechanism moved. The sequencer must treat unknown feedback as unsafe.
        self.feedback_force: str | None = None

    def command(self, target: str) -> None:
        """Accept a valve command.

        A healthy actuator reaches the target immediately in this simulated
        plant; a stuck one leaves mechanism and feedback frozen. Real field I/O
        settles asynchronously and is polled on the next cycle, so
        :meth:`SimulatedField.pump` stays the explicit resample hook -- but a
        healthy in-memory device never fakes travel time that could be mistaken
        for a real watchdog timeout.
        """

        if self.seize_on_move and target != self.actual:
            self.commanded = target
            self.actual = self.actual  # mechanism freezes mid-travel
            self.stuck = True
            self.seize_on_move = False
            return
        self.commanded = target
        if not self.stuck:
            self.actual = target

    def settle(self) -> None:
        if not self.stuck:
            self.actual = self.commanded

    def reported(self) -> str:
        return self.feedback_force if self.feedback_force is not None else self.actual


class _Pump:
    def __init__(self, name: str) -> None:
        self.name = name
        self.commanded = STOPPED
        self.actual = STOPPED
        self.stuck = False

    def command(self, target: str) -> None:
        self.commanded = target
        if not self.stuck:
            self.actual = target

    def settle(self) -> None:
        if not self.stuck:
            self.actual = self.commanded


class SimulatedField:
    """Thread safe in-memory plant.

    Valve state is created lazily per bed, every valve failing to CLOSE so a bed
    with no history is a safe, sealed vessel. Pumps fail STOPPED.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._valves: dict[tuple[str, str], _Valve] = {}
        self._pumps: dict[str, _Pump] = {name: _Pump(name) for name in PUMPS}
        self._level = 10.0

    # -- driving -----------------------------------------------------------

    def command_valve(self, bed_id: str, valve: str, target: str) -> None:
        require_valve_target(target)
        with self._lock:
            self._valve(bed_id, valve).command(target)

    def command_pump(self, pump: str, target: str) -> None:
        require_pump_target(target)
        with self._lock:
            self._pump(pump).command(target)

    # -- read back ---------------------------------------------------------

    def valve_command(self, bed_id: str, valve: str) -> str:
        with self._lock:
            return self._valve(bed_id, valve).commanded

    def valve_position(self, bed_id: str, valve: str) -> str:
        with self._lock:
            return self._valve(bed_id, valve).reported()

    def pump_command(self, pump: str) -> str:
        with self._lock:
            return self._pump(pump).commanded

    def pump_state(self, pump: str) -> str:
        with self._lock:
            return self._pump(pump).actual

    def clearwell_level(self) -> float:
        with self._lock:
            return self._level

    def set_clearwell_level(self, level: float) -> None:
        with self._lock:
            self._level = float(level)

    # -- simulation / fault injection -------------------------------------

    def pump(self) -> None:
        """Settle every healthy device: actual position follows its command."""

        with self._lock:
            for valve in self._valves.values():
                valve.settle()
            for device in self._pumps.values():
                device.settle()

    def stick_valve(self, bed_id: str, valve: str) -> None:
        with self._lock:
            self._valve(bed_id, valve).stuck = True

    def seize_valve(self, bed_id: str, valve: str) -> None:
        """Arm a fault so the valve seizes on its next move.

        The current position is treated as sound; the next command that asks
        for a different position is recorded but never reached and feedback
        freezes -- the partial-failure signature the watchdog exists for.
        """

        with self._lock:
            device = self._valve(bed_id, valve)
            device.seize_on_move = True

    def free_valve(self, bed_id: str, valve: str) -> None:
        with self._lock:
            valve_device = self._valve(bed_id, valve)
            valve_device.stuck = False
            valve_device.seize_on_move = False
            valve_device.settle()

    def force_valve_feedback(self, bed_id: str, valve: str, position: str | None) -> None:
        with self._lock:
            self._valve(bed_id, valve).feedback_force = position

    def stick_pump(self, pump: str) -> None:
        with self._lock:
            self._pump(pump).stuck = True

    def free_pump(self, pump: str) -> None:
        with self._lock:
            device = self._pump(pump)
            device.stuck = False
            device.settle()

    def bed_devices(self, bed_id: str) -> dict[str, dict[str, str]]:
        """Full commanded/reported picture for one bed, console and tests."""

        with self._lock:
            return {
                valve: {
                    "commanded": self._valve(bed_id, valve).commanded,
                    "reported": self._valve(bed_id, valve).reported(),
                    "stuck": str(self._valve(bed_id, valve).stuck),
                }
                for valve in BED_VALVES
            }

    # -- internals ---------------------------------------------------------

    def _valve(self, bed_id: str, valve: str) -> _Valve:
        key = (bed_id, valve)
        device = self._valves.get(key)
        if device is None:
            fail = OPEN if valve == RINSE else CLOSE
            device = _Valve(valve, fail_position=fail)
            self._valves[key] = device
        return device

    def _pump(self, pump: str) -> _Pump:
        device = self._pumps.get(pump)
        if device is None:
            raise ValueError(f"pump {pump!r} not found")
        return device
