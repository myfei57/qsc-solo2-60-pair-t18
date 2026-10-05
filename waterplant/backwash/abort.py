"""Safe shutdown sequence driven by a process safety trip.

The abort order is the inverse of "making water move": stop the movers first,
then shut every supply that could feed the drain, then close the drain, and
only then return the bed to filtering service. Every abort step is itself
feedback gated, so a stuck drain valve freezes the plant in ``ABORT_HELD``
rather than opening the bed onto an open drain.
"""

from __future__ import annotations

from .field import AIR, CLOSE, DRAIN, ISOLATION, OPEN, RINSE, STOPPED, WASH
from .steps import DEFAULT_DEVICE_TIMEOUT, PumpCommand, Step, ValveCommand

WASH_PUMP = "wash_pump"
AIR_BLOWER = "air_blower"


def abort_steps(device_timeout: float = DEFAULT_DEVICE_TIMEOUT) -> list[Step]:
    """Trip response, mover-first and feedback gated at every move."""

    return [
        Step(
            index=0,
            name="abort-stop-movers",
            reason="safety trip: stop the wash pump and air blower immediately",
            pump_commands=(
                PumpCommand(WASH_PUMP, STOPPED),
                PumpCommand(AIR_BLOWER, STOPPED),
            ),
            confirms=((WASH_PUMP, STOPPED), (AIR_BLOWER, STOPPED)),
            timeout=device_timeout,
        ),
        Step(
            index=1,
            name="abort-close-supplies",
            reason="shut wash water, air and rinse feeds so nothing reaches the drain",
            valve_commands=(
                ValveCommand(WASH, CLOSE),
                ValveCommand(AIR, CLOSE),
                ValveCommand(RINSE, CLOSE),
            ),
            confirms=(
                (WASH_PUMP, STOPPED),
                (WASH, CLOSE),
                (AIR, CLOSE),
                (RINSE, CLOSE),
            ),
            timeout=device_timeout,
        ),
        Step(
            index=2,
            name="abort-close-drain",
            reason="close the drain before the bed can return to service",
            valve_commands=(ValveCommand(DRAIN, CLOSE),),
            confirms=(
                (WASH, CLOSE),
                (AIR, CLOSE),
                (RINSE, CLOSE),
                (DRAIN, CLOSE),
            ),
            timeout=device_timeout,
        ),
        Step(
            index=3,
            name="abort-restore-service",
            reason="return the isolated bed to filtering with the drain shut",
            valve_commands=(ValveCommand(ISOLATION, OPEN),),
            confirms=((DRAIN, CLOSE), (ISOLATION, OPEN)),
            timeout=device_timeout,
        ),
    ]
