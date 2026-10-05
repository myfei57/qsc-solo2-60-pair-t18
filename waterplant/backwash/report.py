"""Backwash projections for the console and the audit trail."""

from __future__ import annotations

from dataclasses import dataclass, field

# Canonical step names, in the only order a wash is allowed to run.
ISOLATE = "isolate"
DRAIN_OPEN = "drain_open"
FLUSH = "flush"
DRAIN_CLOSE = "drain_close"
RESTORE = "restore"

STEPS = (ISOLATE, DRAIN_OPEN, FLUSH, DRAIN_CLOSE, RESTORE)

# What a step is currently doing.
PHASE_COMMAND = "commanded"
PHASE_CONFIRMED = "confirmed"
PHASE_HELD = "held"
PHASE_ABORTING = "aborting"
PHASE_DONE = "done"

# Terminal run outcomes.
RUN_RUNNING = "running"
RUN_COMPLETE = "complete"
RUN_ABORTED = "aborted"

# Audit event kinds.
EVENT_START = "backwash.start"
EVENT_COMMAND = "backwash.command"
EVENT_CONFIRM = "backwash.confirm"
EVENT_HOLD = "backwash.timeout"
EVENT_INTERLOCK = "backwash.interlock"
EVENT_ABORT = "backwash.abort"
EVENT_COMPLETE = "backwash.complete"
EVENT_REJECT = "backwash.reject"
EVENT_RESUME = "backwash.resume"
EVENT_GUARD = "backwash.guard"


@dataclass(frozen=True)
class StepSpec:
    """Fixed safety description of one step, shown verbatim on the console."""

    name: str
    action: str
    confirmation: str
    timeout_seconds: float

    def as_dict(self) -> dict[str, object]:
        return {
            "step": self.name,
            "action": self.action,
            "confirmation": self.confirmation,
            "timeout_seconds": self.timeout_seconds,
        }


def step_catalog(
    valve_timeout: float, pump_timeout: float
) -> list[StepSpec]:
    """The ordered step table with its required position confirmations."""

    return [
        StepSpec(
            ISOLATE,
            "close bed isolation valve",
            "isolation valve reports fully closed",
            valve_timeout,
        ),
        StepSpec(
            DRAIN_OPEN,
            "open drain valve (only with isolation confirmed shut)",
            "drain valve reports fully open",
            valve_timeout,
        ),
        StepSpec(
            FLUSH,
            "run backwash pump for the flush duration",
            "pump aux reports running, then stopped, and duration elapsed",
            pump_timeout,
        ),
        StepSpec(
            DRAIN_CLOSE,
            "close drain valve",
            "drain valve reports fully closed",
            valve_timeout,
        ),
        StepSpec(
            RESTORE,
            "open bed isolation valve back into service",
            "isolation valve reports fully open",
            valve_timeout,
        ),
    ]


@dataclass(frozen=True)
class BackwashState:
    """Queued commands, the live run (if any), history and the step table."""

    commands: list[str]
    drains: list[str]
    active: dict[str, object] | None
    history: list[dict[str, object]] = field(default_factory=list)
    steps: list[StepSpec] = field(default_factory=list)
    safety_level: float = 0.0
    spilled: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "commands": self.commands,
            "drains": self.drains,
            "active": self.active,
            "history": self.history,
            "steps": [spec.as_dict() for spec in self.steps],
            "safety_level": self.safety_level,
            "spilled": self.spilled,
        }
