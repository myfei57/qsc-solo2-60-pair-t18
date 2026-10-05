"""Backwash sequencing for the filter bank.

Washes run through a feedback-gated state machine (:class:`Sequencer`) exposed
through the :class:`Controller` facade. A step only advances on confirmed
position/running feedback; watchdog timeouts freeze the run in place; a
clearwell below the safe level refuses the start or trips a mover-first safe
shutdown; every action, confirmation and reason lands in the audit journal.
"""

from .clock import Clock, ManualClock, SystemClock
from .controller import Controller
from .field import CLOSE, OPEN, RUNNING, STOPPED, Field, SimulatedField
from .report import (
    ABORT_HELD,
    COMPLETE,
    HELD,
    IDLE,
    REJECTED,
    TRIPPED,
    BackwashState,
    RunState,
)
from .sequencer import (
    DEFAULT_CLEARWELL_SAFE_LEVEL,
    HISTORY_KEY,
    JOURNAL_KEY,
    RUN_KEY,
    Sequencer,
    default_field,
)

__all__ = [
    "ABORT_HELD",
    "BackwashState",
    "Clock",
    "COMPLETE",
    "Controller",
    "DEFAULT_CLEARWELL_SAFE_LEVEL",
    "Field",
    "HELD",
    "HISTORY_KEY",
    "IDLE",
    "JOURNAL_KEY",
    "ManualClock",
    "OPEN",
    "CLOSE",
    "REJECTED",
    "RUN_KEY",
    "RUNNING",
    "Sequencer",
    "SimulatedField",
    "STOPPED",
    "SystemClock",
    "TRIPPED",
    "RunState",
    "default_field",
]
