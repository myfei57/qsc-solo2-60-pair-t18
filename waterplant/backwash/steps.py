"""Declarative backwash sequence.

Each step is a pure description: which devices it commands, which feedback must
be observed to call it done, how long the field is allowed to take, and (for the
wash hold steps) how long the flow must be held after the running feedback is
in.

The ordering that prevents media loss is *structural*:

* the drain only opens in a step whose permit requires the bed reported
  isolated, and the bed only returns to service in a step whose permit requires
  the drain reported shut;
* air scour is fully stopped and confirmed before wash water is ever admitted;
* the rinse inlet closes and is confirmed before the drain closes, and the
  drain closes and is confirmed before the bed opens back to the clearwell.

Every step while the drain is open samples the clearwell level (the whole
"排污过程中" window), so a low level trips the sequence regardless of phase.
"""

from __future__ import annotations

from dataclasses import dataclass

from .field import AIR, CLOSE, DRAIN, ISOLATION, OPEN, RINSE, RUNNING, STOPPED, WASH

WASH_PUMP = "wash_pump"
AIR_BLOWER = "air_blower"

#: Maximum seconds a device may take between command and matching feedback.
DEFAULT_DEVICE_TIMEOUT = 30.0

#: How long each fluidised step is held once the running feedback is observed.
DEFAULT_DRAIN_DOWN_SECONDS = 60.0
DEFAULT_AIR_SCOUR_SECONDS = 120.0
DEFAULT_WASH_SECONDS = 300.0
DEFAULT_SETTLE_SECONDS = 60.0
DEFAULT_RINSE_SECONDS = 180.0


@dataclass(frozen=True)
class ValveCommand:
    valve: str
    target: str


@dataclass(frozen=True)
class PumpCommand:
    pump: str
    target: str


@dataclass(frozen=True)
class Step:
    """One ordered phase of a backwash.

    ``confirms`` lists the feedback pairs that must ALL be observed before the
    step may complete. Devices the step itself commands start away from their
    target (that is the move); devices the step does *not* command must already
    read at their target or the step is refused up front (hard interlock).

    ``timeout`` bounds how long feedback may take but never, by itself,
    completes a step. ``hold`` marks the wash phases: after feedback is
    confirmed the phase is held for ``hold_seconds`` while feedback and the
    clearwell permit continue to be checked on every tick.
    """

    index: int
    name: str
    reason: str
    valve_commands: tuple[ValveCommand, ...] = ()
    pump_commands: tuple[PumpCommand, ...] = ()
    confirms: tuple[tuple[str, str], ...] = ()
    timeout: float = DEFAULT_DEVICE_TIMEOUT
    hold: bool = False
    hold_seconds: float = 0.0
    #: True while the drain is open: the clearwell level is sampled each tick.
    clearwell_guarded: bool = False

    def commanded_valves(self) -> dict[str, str]:
        return {item.valve: item.target for item in self.valve_commands}

    def commanded_pumps(self) -> dict[str, str]:
        return {item.pump: item.target for item in self.pump_commands}


def _v(valve: str, target: str) -> tuple[str, str]:
    return (valve, target)


def build_steps(
    device_timeout: float = DEFAULT_DEVICE_TIMEOUT,
    drain_down_seconds: float = DEFAULT_DRAIN_DOWN_SECONDS,
    air_scour_seconds: float = DEFAULT_AIR_SCOUR_SECONDS,
    wash_seconds: float = DEFAULT_WASH_SECONDS,
    settle_seconds: float = DEFAULT_SETTLE_SECONDS,
    rinse_seconds: float = DEFAULT_RINSE_SECONDS,
) -> list[Step]:
    """The fixed backwash order, dirtiest-water-safe first."""

    gw = True  # drain is open across this whole window
    return [
        Step(
            index=0,
            name="isolate-bed",
            reason="close the bed inlet/outlet before anything downstream moves",
            valve_commands=(ValveCommand(ISOLATION, CLOSE),),
            confirms=(_v(ISOLATION, CLOSE),),
            timeout=device_timeout,
        ),
        Step(
            index=1,
            name="open-drain",
            reason="only a proven-isolated bed may send water down the drain",
            valve_commands=(ValveCommand(DRAIN, OPEN),),
            confirms=(_v(ISOLATION, CLOSE), _v(DRAIN, OPEN)),
            timeout=device_timeout,
            clearwell_guarded=gw,
        ),
        Step(
            index=2,
            name="drain-down",
            reason="lower the bed water level to the wash gutter before scouring",
            confirms=(_v(ISOLATION, CLOSE), _v(DRAIN, OPEN)),
            hold=True,
            hold_seconds=drain_down_seconds,
            timeout=device_timeout,
            clearwell_guarded=gw,
        ),
        Step(
            index=3,
            name="start-air-scour",
            reason="fluidise the media with air while the drain carries it away",
            valve_commands=(ValveCommand(AIR, OPEN),),
            pump_commands=(PumpCommand(AIR_BLOWER, RUNNING),),
            confirms=(
                _v(ISOLATION, CLOSE),
                _v(DRAIN, OPEN),
                _v(AIR, OPEN),
                (AIR_BLOWER, RUNNING),
            ),
            timeout=device_timeout,
            clearwell_guarded=gw,
        ),
        Step(
            index=4,
            name="air-scour",
            reason="hold the air scour for its full duration with the drain open",
            confirms=(_v(ISOLATION, CLOSE), _v(DRAIN, OPEN), (AIR_BLOWER, RUNNING)),
            hold=True,
            hold_seconds=air_scour_seconds,
            timeout=device_timeout,
            clearwell_guarded=gw,
        ),
        Step(
            index=5,
            name="stop-air-scour",
            reason="shut air and stop the blower, and confirm it, before wash water",
            valve_commands=(ValveCommand(AIR, CLOSE),),
            pump_commands=(PumpCommand(AIR_BLOWER, STOPPED),),
            confirms=(
                _v(ISOLATION, CLOSE),
                _v(DRAIN, OPEN),
                _v(AIR, CLOSE),
                (AIR_BLOWER, STOPPED),
            ),
            timeout=device_timeout,
            clearwell_guarded=gw,
        ),
        Step(
            index=6,
            name="start-wash",
            reason="admit clearwell wash water and start the pump only after air is off",
            valve_commands=(ValveCommand(WASH, OPEN),),
            pump_commands=(PumpCommand(WASH_PUMP, RUNNING),),
            confirms=(
                _v(ISOLATION, CLOSE),
                _v(DRAIN, OPEN),
                _v(AIR, CLOSE),
                (AIR_BLOWER, STOPPED),
                _v(WASH, OPEN),
                (WASH_PUMP, RUNNING),
            ),
            timeout=device_timeout,
            clearwell_guarded=gw,
        ),
        Step(
            index=7,
            name="backwash",
            reason="hold upward wash flow until the drain runs clear",
            confirms=(
                _v(ISOLATION, CLOSE),
                _v(DRAIN, OPEN),
                _v(WASH, OPEN),
                (WASH_PUMP, RUNNING),
            ),
            hold=True,
            hold_seconds=wash_seconds,
            timeout=device_timeout,
            clearwell_guarded=gw,
        ),
        Step(
            index=8,
            name="stop-wash",
            reason="stop the wash pump and shut wash water before the bed settles",
            valve_commands=(ValveCommand(WASH, CLOSE),),
            pump_commands=(PumpCommand(WASH_PUMP, STOPPED),),
            confirms=(
                _v(ISOLATION, CLOSE),
                _v(DRAIN, OPEN),
                _v(WASH, CLOSE),
                (WASH_PUMP, STOPPED),
            ),
            timeout=device_timeout,
            clearwell_guarded=gw,
        ),
        Step(
            index=9,
            name="settle",
            reason="let the media re-stratify with no flow before forward rinsing",
            confirms=(_v(ISOLATION, CLOSE), _v(DRAIN, OPEN)),
            hold=True,
            hold_seconds=settle_seconds,
            timeout=device_timeout,
            clearwell_guarded=gw,
        ),
        Step(
            index=10,
            name="start-rinse",
            reason="open the forward rinse path to flush the bed surface to drain",
            valve_commands=(ValveCommand(RINSE, OPEN),),
            confirms=(
                _v(ISOLATION, CLOSE),
                _v(DRAIN, OPEN),
                _v(WASH, CLOSE),
                (WASH_PUMP, STOPPED),
                _v(RINSE, OPEN),
            ),
            timeout=device_timeout,
            clearwell_guarded=gw,
        ),
        Step(
            index=11,
            name="rinse",
            reason="hold forward rinse to drain until the turbidity clears",
            confirms=(
                _v(ISOLATION, CLOSE),
                _v(DRAIN, OPEN),
                _v(RINSE, OPEN),
            ),
            hold=True,
            hold_seconds=rinse_seconds,
            timeout=device_timeout,
            clearwell_guarded=gw,
        ),
        Step(
            index=12,
            name="stop-rinse",
            reason="close the rinse inlet and confirm it before closing the drain",
            valve_commands=(ValveCommand(RINSE, CLOSE),),
            confirms=(_v(DRAIN, OPEN), _v(RINSE, CLOSE)),
            timeout=device_timeout,
        ),
        Step(
            index=13,
            name="close-drain",
            reason="close the drain and confirm it before the bed opens to the clearwell",
            valve_commands=(ValveCommand(DRAIN, CLOSE),),
            confirms=(_v(RINSE, CLOSE), _v(DRAIN, CLOSE)),
            timeout=device_timeout,
        ),
        Step(
            index=14,
            name="restore-service",
            reason="return the isolation valve to filtering, restoring production",
            valve_commands=(ValveCommand(ISOLATION, OPEN),),
            confirms=(_v(DRAIN, CLOSE), _v(ISOLATION, OPEN)),
            timeout=device_timeout,
        ),
    ]
