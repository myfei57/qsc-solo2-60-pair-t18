"""Backwash controller facade.

The legacy surface (command queue, duty rotation, drain markers) is retained for
the console, but starting a wash no longer fires commands in call order. It
delegates to the feedback-gated :class:`Sequencer`: the bed is proven isolated
before the drain opens, every step needs matching feedback before the next one
is allowed to move, watchdog failures freeze the run in place, and a clearwell
below its safe level refuses or trips the wash.
"""

from __future__ import annotations

from waterplant.filter import Bank
from waterplant.store.commands import append_command, clear_commands, load_commands
from waterplant.store.store import Store

from .clock import Clock
from .field import Field, SimulatedField
from .report import BackwashState, IDLE
from .sequencer import (
    DEFAULT_CLEARWELL_SAFE_LEVEL,
    DRAIN_KEY_PREFIX,
    SPILL_KEY,
    RunState,
    Sequencer,
)

COMMAND_KEY = "backwash:commands"


class Controller:
    """Sequences backwash work against the filter bank via the safe sequencer."""

    def __init__(
        self,
        bank: Bank,
        store: Store,
        field: Field | None = None,
        clock: Clock | None = None,
        safe_level: float = DEFAULT_CLEARWELL_SAFE_LEVEL,
    ) -> None:
        self._bank = bank
        self._store = store
        self._field = field if field is not None else SimulatedField()
        self.sequencer = Sequencer(
            bank, self._field, store, clock=clock, safe_level=safe_level
        )

    # -- safe sequencing ---------------------------------------------------

    def start(self, bed_id: str) -> RunState:
        """Begin (or idempotently return) the feedback-gated run for a bed."""

        return self.sequencer.begin(bed_id)

    def pulse(self) -> RunState:
        return self.sequencer.pulse()

    def resume(self) -> RunState:
        return self.sequencer.resume()

    def resume_abort(self) -> RunState:
        return self.sequencer.resume_abort()

    def reset(self) -> None:
        self.sequencer.reset()

    def status(self) -> str:
        return self.sequencer.status()

    def active_run(self) -> RunState | None:
        return self.sequencer.active_run()

    def journal(self, run_id: str | None = None) -> list[dict[str, object]]:
        return self.sequencer.journal(run_id)

    def run_history(self) -> list[dict[str, object]]:
        return self.sequencer.run_history()

    def safe_level(self) -> float:
        return self.sequencer.safe_level()

    def field(self) -> Field:
        return self._field

    # -- queued work (operator requests, never blindly re-issued) ----------

    def enqueue(self, bed_id: str) -> None:
        append_command(self._store, COMMAND_KEY, bed_id)

    def replay(self) -> list[str]:
        """Start a safe run for the queued beds, one wash at a time.

        Only one bed may be washed at a time, so queued ids are *requests*: the
        first free bed begins its feedback-gated run and the rest remain queued
        until it completes. The returned list is what was still outstanding
        before this call.
        """

        commands = self.command_list()
        if commands and self.sequencer.active_run() is None:
            bed_id = commands[0]
            clear_commands(self._store, COMMAND_KEY)
            for remaining in commands[1:]:
                append_command(self._store, COMMAND_KEY, remaining)
            self.sequencer.begin(bed_id)
        return commands

    def recover(self) -> None:
        """Discard operator requests; an active run itself is journaled."""

        clear_commands(self._store, COMMAND_KEY)

    def command_list(self) -> list[str]:
        return load_commands(self._store, COMMAND_KEY)

    def pending_count(self) -> int:
        return len(self.command_list())

    # -- bank organisation -------------------------------------------------

    def select(self, zone: int) -> str:
        """Resolve the bed that currently occupies a zone."""

        mapping = self._bank.mapping()
        if zone not in mapping:
            raise ValueError(f"no filter bed for zone {zone}")
        return mapping[zone]

    def order_rotation(self) -> list[str]:
        """Rotate duty to the dirtiest bed first."""

        order = [bed.id for bed in sorted(self._bank.beds(), key=lambda bed: -bed.load)]
        if order:
            self._bank.rotate(order[0])
        return order

    def eligible(self) -> list[str]:
        return [bed.id for bed in self._bank.beds() if not bed.closed]

    # -- drain markers (mirror of confirmed physical state) ----------------

    def drains(self) -> list[str]:
        drains: list[str] = []
        for key in self._store.keys():
            if key.startswith(DRAIN_KEY_PREFIX):
                drains.append(key[len(DRAIN_KEY_PREFIX) :])
        return sorted(drains)

    def drain_count(self) -> int:
        return len(self.drains())

    def last_drain(self) -> str | None:
        drains = self.drains()
        return drains[-1] if drains else None

    def spilled(self) -> bool:
        """A spill marker is only ever present from pre-sequencer builds."""

        raw, present = self._store.get(SPILL_KEY)
        return present and raw == "true"

    def current_step(self):
        """Projection of the step an active run is parked on, or None."""

        run = self.sequencer.active_run()
        if run is None:
            return None
        step = self.sequencer.current_step()
        if step is None:
            return {"index": run.step_index, "name": "", "reason": ""}
        return {
            "index": step.index,
            "name": step.name,
            "reason": step.reason,
            "hold_seconds": step.hold_seconds,
            "clearwell_guarded": step.clearwell_guarded,
        }

    def state(self) -> BackwashState:
        run = self.sequencer.active_run()
        return BackwashState(
            commands=self.command_list(),
            drains=self.drains(),
            run=run.as_dict() if run is not None else None,
            history=self.run_history(),
        )

    def describe(self) -> str:
        state = self.state()
        status = state.run["status"] if state.run else IDLE
        step = state.run["step_index"] if state.run else "-"
        return (
            f"backwash status={status} step={step} "
            f"commands={len(state.commands)} drains={len(state.drains)}"
        )
