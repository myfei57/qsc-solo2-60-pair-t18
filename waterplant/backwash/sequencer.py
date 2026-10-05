"""Backwash sequencer with feedback gating, watchdogs and safe recovery.

The sequencer replaces "issue commands in order and hope" with a state machine
that only moves when the field proves it is safe to move:

* a step issues its commands once and then waits on **position/running
  feedback** -- a deadline never completes a step, it only freezes it;
* every step's permit is re-evaluated from live feedback before any command is
  sent, so a drain cannot open unless the bed is *reported* isolated right now,
  and a bed cannot return to service unless the drain is *reported* shut;
* the clearwell level is a process permit for the whole drain-open window:
  below the safe line a run is refused before it starts and trips the
  mover-first safe-shutdown sequence mid-wash;
* every state change is appended to an audit journal *before* the run document
  is rewritten, each carrying a monotonic version, so the wash record and the
  control state cannot disagree after a crash;
* resume after a watchdog ``HELD`` retries the **same** step and is idempotent
  (a command is re-sent only when the commanded side no longer matches the
  step target); a safety ``TRIPPED`` run must restart from step zero.

Timeout policy, fixed here on purpose:

* **advancement** is decided *only* by position/running feedback;
* **partial failure** is decided *only* by time -- if the commanded device has
  not produced matching feedback within ``step.timeout`` seconds the run freezes
  on that step (``HELD``) and raises an alarm;
* the wash phases additionally have a fixed **hold duration** that starts only
  after running feedback is observed; losing that feedback during the hold
  freezes the phase rather than waiting the timer out.
"""

from __future__ import annotations

import json
import threading
import uuid
from typing import Iterable

from waterplant.filter import Bank
from waterplant.store.commands import append_command, load_commands
from waterplant.store.store import Store

from .abort import abort_steps
from .clock import Clock, SystemClock
from .field import (
    PUMPS,
    CLOSE,
    OPEN,
    Field,
    SimulatedField,
)
from .report import (
    ABORT_HELD,
    ACTIVE,
    COMPLETE,
    HELD,
    IDLE,
    REJECTED,
    RUNNING,
    SHUTTING_DOWN,
    TRIPPED,
    BackwashState,
    RunState,
)
from .schedule import now_unix
from .steps import Step, build_steps

COMMAND_KEY = "backwash:commands"
DRAIN_KEY_PREFIX = "backwash:drain:"
SPILL_KEY = "backwash:spill"
RUN_KEY = "backwash:active-run"
HISTORY_KEY = "backwash:runs"
JOURNAL_KEY = "backwash:journal"

#: Clearwell level below which drawing/holding wash water is forbidden.
DEFAULT_CLEARWELL_SAFE_LEVEL = 2.0

# Journal event kinds.
EVENT_BEGIN = "begin"
EVENT_COMMAND = "command"
EVENT_CONFIRM = "confirm"
EVENT_ADVANCE = "advance"
EVENT_HOLD = "hold"
EVENT_TRIP = "trip"
EVENT_ABORT_STEP = "abort-step"
EVENT_ABORT_HELD = "abort-held"
EVENT_COMPLETE = "complete"
EVENT_REJECT = "reject"
EVENT_RESUME = "resume"
EVENT_RESUME_ABORT = "resume-abort"


class Sequencer:
    """Drives one backwash run at a time through its feedback-gated steps."""

    def __init__(
        self,
        bank: Bank,
        field: Field,
        store: Store,
        clock: Clock | None = None,
        safe_level: float = DEFAULT_CLEARWELL_SAFE_LEVEL,
    ) -> None:
        self._bank = bank
        self._field = field
        self._store = store
        self._clock = clock or SystemClock()
        self._safe_level = float(safe_level)
        self._steps = build_steps()
        self._abort_steps = abort_steps()
        self._lock = threading.RLock()
        self._run = self._load_run()

    # -- lifecycle ---------------------------------------------------------

    def begin(self, bed_id: str) -> RunState:
        """Start a run for ``bed_id``.

        Re-triggering while a run for the same bed is active is idempotent: the
        existing run is returned untouched. A run for a different bed, or a
        terminal run, blocks a new start (terminal runs must be :meth:`reset`
        first; a tripped run then restarts from step zero by definition).
        """

        with self._lock:
            self._require_bed(bed_id)
            active = self.active_run()
            if active is not None:
                if active.bed_id == bed_id:
                    return active
                raise ValueError(
                    f"backwash {active.run_id} for {active.bed_id} is in progress"
                )

            level = self._field.clearwell_level()
            if level < self._safe_level:
                rejected = RunState(
                    run_id=self._new_id(),
                    bed_id=bed_id,
                    status=REJECTED,
                    started_at=self._now(),
                    reason="clearwell below safe level",
                    detail=f"level={level:.3f} safe={self._safe_level:.3f}",
                )
                self._record(rejected, EVENT_REJECT, rejected.reason, rejected.detail)
                self._store.put(RUN_KEY, json.dumps(rejected.as_dict(), ensure_ascii=False))
                self._archive(rejected)
                return rejected

            run = RunState(
                run_id=self._new_id(),
                bed_id=bed_id,
                status=RUNNING,
                started_at=self._now(),
                reason="operator start",
            )
            self._record(run, EVENT_BEGIN, "backwash started", f"bed={bed_id}")
            self._persist(run)
            self._run = run
            self.pulse()
            return run

    def resume(self) -> RunState:
        """Retry the held step after the field has been repaired.

        Only ``HELD`` is resumable, and it retries **in place**. ``TRIPPED`` /
        ``ABORT_HELD`` are refused here -- the former restarts from step zero
        via a fresh run, the latter continues only via :meth:`resume_abort`.
        """

        with self._lock:
            run = self._require_run()
            if run.status == HELD:
                run.status = RUNNING
                run.attempt += 1
                run.issued_at = 0.0
                run.feedback_at = 0.0
                run.hold_since = 0.0
                run.reason = "operator resume after repair"
                run.detail = ""
                self._record(
                    run,
                    EVENT_RESUME,
                    "resumed in place",
                    f"step={self._steps[run.step_index].name} attempt={run.attempt}",
                )
                self._persist(run)
                self.pulse()
                return run
            if run.status == RUNNING:
                self.pulse()
                return run
            raise ValueError(
                f"run {run.run_id} status {run.status} cannot be resumed in place; "
                "a safety trip restarts from step 0"
            )

    def resume_abort(self) -> RunState:
        """Continue a stalled safe-shutdown sequence after manual intervention."""

        with self._lock:
            run = self._require_run()
            if run.status != ABORT_HELD:
                raise ValueError(f"run {run.run_id} is {run.status}, not abort-held")
            run.status = TRIPPED
            run.abort_issued_at = 0.0
            run.reason = "shutdown resumed after manual intervention"
            self._record(
                run,
                EVENT_RESUME_ABORT,
                "safe shutdown resumed",
                f"abort_index={run.abort_index}",
            )
            self._persist(run)
            return self._pulse_trip(run)

    def reset(self) -> None:
        """Clear a terminal run after the plant has been made safe."""

        with self._lock:
            run = self.active_run()
            if run is None:
                return
            if run.status in ACTIVE:
                raise ValueError(f"run {run.run_id} is {run.status}; resolve it first")
            self._store.delete(RUN_KEY)
            self._run = None

    def pulse(self) -> RunState:
        """Advance the active run as far as current feedback allows.

        Safe to call repeatedly (polling loop, console request, recovery after
        a crash): it never commands a device already at the step target, never
        records a duplicate confirmation, and reconciles purely from feedback.
        """

        with self._lock:
            run = self.active_run()
            if run is None:
                return self._idle()
            if run.status == HELD:
                return run
            if run.status in SHUTTING_DOWN:
                return self._pulse_trip(run)
            if run.status == RUNNING:
                return self._pulse_normal(run)
            return run

    # -- normal sequence ---------------------------------------------------

    def _pulse_normal(self, run: RunState) -> RunState:
        step = self._steps[run.step_index]

        level_trip = self._guard_clearwell(run, step)
        if level_trip is not None:
            return level_trip

        missing_permit = self._missing_prerequisites(step, run)
        if missing_permit:
            return self._hold(
                run,
                step,
                "interlock not satisfied before issuing command",
                f"step={step.name} missing={missing_permit}",
            )

        moved = self._issue_step_commands(run, step)
        now = self._now()

        # Feedback is the only thing that completes a step.
        if not self._feedback_matches(step.confirms, run.bed_id):
            if run.issued_at == 0.0:
                run.issued_at = now
                self._persist(run)
            elif now - run.issued_at > step.timeout:
                return self._hold(
                    run,
                    step,
                    "feedback watchdog expired",
                    f"step={step.name} waiting_on="
                    f"{self._missing_feedback(step, run)} after={step.timeout:.0f}s",
                )
            return run

        if run.feedback_at == 0.0:
            run.feedback_at = now
            self._record(
                run,
                EVENT_CONFIRM,
                "step feedback confirmed",
                f"step={step.name} feedback={self._feedback_label(step.confirms)}",
            )
            self._mirror_physical_state(run)
            self._persist(run)

        if step.hold:
            if run.hold_since == 0.0:
                run.hold_since = now
                self._persist(run)
            if not self._feedback_matches(step.confirms, run.bed_id):
                # A running-wash signal dropped mid-hold: freeze immediately;
                # the timer must not mask a stopped pump or closed valve.
                return self._hold(
                    run,
                    step,
                    "feedback lost during hold",
                    f"step={step.name} lost={self._missing_feedback(step, run)}",
                )
            if now - run.hold_since < step.hold_seconds:
                return run
            self._record(
                run,
                EVENT_ADVANCE,
                "hold duration elapsed with feedback present",
                f"step={step.name} hold_seconds={step.hold_seconds:.0f}",
            )
        else:
            self._record(
                run,
                EVENT_ADVANCE,
                "step complete",
                f"step={step.name} index={step.index}",
            )

        run.step_index += 1
        run.issued_at = 0.0
        run.feedback_at = 0.0
        run.hold_since = 0.0
        if run.step_index >= len(self._steps):
            return self._complete(run)
        self._persist(run)
        return self._pulse_normal(run)

    def _issue_step_commands(self, run: RunState, step: Step) -> bool:
        """Send the step's commands idempotently; return True if any moved."""

        moved = False
        for valve, target in step.commanded_valves().items():
            if self._field.valve_command(run.bed_id, valve) != target:
                self._field.command_valve(run.bed_id, valve, target)
                moved = True
                self._record(
                    run,
                    EVENT_COMMAND,
                    step.reason,
                    f"valve={valve} target={target} step={step.name}",
                )
        for pump, target in step.commanded_pumps().items():
            if self._field.pump_command(pump) != target:
                self._field.command_pump(pump, target)
                moved = True
                self._record(
                    run,
                    EVENT_COMMAND,
                    step.reason,
                    f"pump={pump} target={target} step={step.name}",
                )
        return moved

    # -- gating ------------------------------------------------------------

    def _missing_prerequisites(self, step: Step, run: RunState) -> list[str]:
        """Feedback that must already hold before this step may command.

        A device the step itself commands is allowed to start away from its
        target -- that is the move. Every other required device must already
        read at its target position right now, or the step is refused.
        """

        missing: list[str] = []
        valves = step.commanded_valves()
        pumps = step.commanded_pumps()
        for device, required in step.confirms:
            actual = self._device_feedback(run.bed_id, device)
            if actual == required:
                continue
            demanded = valves.get(device) if device not in PUMPS else pumps.get(device)
            if demanded is None:
                missing.append(f"{device}:{actual}!={required}")
        return missing

    def _feedback_matches(self, confirms: Iterable[tuple[str, str]], bed_id: str) -> bool:
        return all(
            self._device_feedback(bed_id, device) == required for device, required in confirms
        )

    def _missing_feedback(self, step: Step, run: RunState) -> str:
        missing = [
            f"{device}:{self._device_feedback(run.bed_id, device)}->{required}"
            for device, required in step.confirms
            if self._device_feedback(run.bed_id, device) != required
        ]
        return ",".join(missing)

    def _device_feedback(self, bed_id: str, device: str) -> str:
        if device in PUMPS:
            return self._field.pump_state(device)
        return self._field.valve_position(bed_id, device)

    def _feedback_label(self, confirms: Iterable[tuple[str, str]]) -> str:
        return ",".join(f"{device}={required}" for device, required in confirms)

    def _guard_clearwell(self, run: RunState, step: Step) -> RunState | None:
        if not step.clearwell_guarded:
            return None
        level = self._field.clearwell_level()
        if level >= self._safe_level:
            return None
        return self._trip(
            run,
            "clearwell below safe level while drain is open",
            f"step={step.name} level={level:.3f} safe={self._safe_level:.3f}",
        )

    # -- failure states ----------------------------------------------------

    def _hold(self, run: RunState, step: Step, reason: str, detail: str) -> RunState:
        run.status = HELD
        run.reason = reason
        run.detail = detail
        self._record(run, EVENT_HOLD, reason, detail)
        self._persist(run)
        return run

    def _trip(self, run: RunState, reason: str, detail: str) -> RunState:
        run.status = TRIPPED
        run.abort_index = 0
        run.abort_issued_at = 0.0
        run.reason = reason
        run.detail = detail
        self._record(run, EVENT_TRIP, reason, detail)
        self._persist(run)
        return self._pulse_trip(run)

    def _pulse_trip(self, run: RunState) -> RunState:
        """Drive the feedback-gated mover-first safe-shutdown sequence."""

        while run.abort_index < len(self._abort_steps):
            step = self._abort_steps[run.abort_index]

            missing = self._missing_prerequisites(step, run)
            if missing:
                return self._abort_hold(
                    run, step, f"interlock not satisfied during shutdown: {missing}"
                )

            moved = self._issue_step_commands(run, step)
            now = self._now()
            if not self._feedback_matches(step.confirms, run.bed_id):
                if run.abort_issued_at == 0.0 or moved:
                    run.abort_issued_at = now
                    self._persist(run)
                    return run
                if now - run.abort_issued_at > step.timeout:
                    return self._abort_hold(
                        run,
                        step,
                        "shutdown watchdog expired waiting on "
                        f"{self._missing_feedback(step, run)}",
                    )
                return run

            self._record(
                run, EVENT_ABORT_STEP, "shutdown step confirmed", f"step={step.name}"
            )
            self._mirror_physical_state(run)
            run.abort_index += 1
            run.abort_issued_at = 0.0
            self._persist(run)

        run.status = TRIPPED
        run.detail = f"{run.detail}; safe shutdown complete; restart from step 0"
        self._persist(run)
        self._archive(run)
        return run

    def _abort_hold(self, run: RunState, step: Step, reason: str) -> RunState:
        run.status = ABORT_HELD
        run.reason = reason
        run.detail = f"abort_step={step.name}"
        self._record(run, EVENT_ABORT_HELD, reason, f"abort_step={step.name}")
        self._persist(run)
        return run

    def _complete(self, run: RunState) -> RunState:
        run.status = COMPLETE
        run.reason = "all steps confirmed; bed returned to service"
        self._record(run, EVENT_COMPLETE, "backwash complete", f"bed={run.bed_id}")
        self._persist(run)
        self._archive(run)
        return run

    # -- persistence / journal --------------------------------------------

    def _record(self, run: RunState, kind: str, reason: str, detail: str) -> dict[str, object]:
        run.version += 1
        event = {
            "id": self._new_id(),
            "version": run.version,
            "at": now_unix(),
            "mono": self._now(),
            "run_id": run.run_id,
            "bed_id": run.bed_id,
            "status": run.status,
            "kind": kind,
            "step": run.step_index,
            "abort_index": run.abort_index,
            "reason": reason,
            "detail": detail,
        }
        # WAL ordering: the reason/action/confirmation is durable before the
        # run document that points at it is rewritten.
        append_command(self._store, JOURNAL_KEY, json.dumps(event, ensure_ascii=False))
        run.journal.append(event["id"])
        run.history.append(event)
        return event

    def _persist(self, run: RunState) -> None:
        run.updated_at = self._now()
        self._store.put(RUN_KEY, json.dumps(run.as_dict(), ensure_ascii=False))
        self._run = run

    def _archive(self, run: RunState) -> None:
        append_command(self._store, HISTORY_KEY, json.dumps(run.as_dict(), ensure_ascii=False))
        self._store.delete(RUN_KEY)
        self._run = None

    def _load_run(self) -> RunState | None:
        raw, present = self._store.get(RUN_KEY)
        if not present:
            return None
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        return RunState.from_dict(payload)

    # -- physical mirror ---------------------------------------------------

    def _mirror_physical_state(self, run: RunState) -> None:
        """Keep the legacy bank/drain projections truthful to feedback.

        The marker is only written when the drain is *reported* open against a
        *reported* isolated bed, and removed only when both the drain shut and
        the bed open are confirmed -- it can never again describe a spill.
        """

        drain = self._field.valve_position(run.bed_id, "drain")
        isolation = self._field.valve_position(run.bed_id, "isolation")
        key = f"{DRAIN_KEY_PREFIX}{run.bed_id}"
        if drain == OPEN:
            if isolation == CLOSE:
                self._bank.close(run.bed_id)
                self._store.put(key, str(now_unix()))
        elif drain == CLOSE:
            if isolation == OPEN:
                self._bank.open(run.bed_id)
            self._store.delete(key)

    # -- queries -----------------------------------------------------------

    def active_run(self) -> RunState | None:
        return self._run

    def status(self) -> str:
        return self._run.status if self._run else IDLE

    def current_step(self) -> Step | None:
        if self._run is None or self._run.status not in ACTIVE:
            return None
        if self._run.step_index < len(self._steps):
            return self._steps[self._run.step_index]
        return None

    def journal(self, run_id: str | None = None) -> list[dict[str, object]]:
        events: list[dict[str, object]] = []
        for item in load_commands(self._store, JOURNAL_KEY):
            try:
                event = json.loads(item)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if run_id is None or event.get("run_id") == run_id:
                events.append(event)
        return events

    def run_history(self) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        for item in load_commands(self._store, HISTORY_KEY):
            try:
                record = json.loads(item)
            except ValueError:
                continue
            if isinstance(record, dict):
                records.append(record)
        return records

    def safe_level(self) -> float:
        return self._safe_level

    def steps(self) -> list[Step]:
        return list(self._steps)

    def field(self) -> Field:
        return self._field

    def bank(self) -> Bank:
        return self._bank

    # -- internals ---------------------------------------------------------

    def _idle(self) -> RunState:
        return RunState(run_id="", bed_id="", status=IDLE)

    def _require_run(self) -> RunState:
        if self._run is None:
            raise ValueError("no backwash run is active")
        return self._run

    def _require_bed(self, bed_id: str) -> None:
        if self._bank.bed(bed_id) is None:
            raise ValueError(f"filter bed {bed_id} not found")

    def _now(self) -> float:
        return self._clock.now()

    def _new_id(self) -> str:
        return uuid.uuid4().hex


def default_field() -> SimulatedField:
    """The one simulated plant per runtime; shared for feedback display."""

    return SimulatedField()


__all__ = [
    "Sequencer",
    "RunState",
    "BackwashState",
    "default_field",
    "RUN_KEY",
    "HISTORY_KEY",
    "JOURNAL_KEY",
    "DRAIN_KEY_PREFIX",
    "SPILL_KEY",
    "DEFAULT_CLEARWELL_SAFE_LEVEL",
]
