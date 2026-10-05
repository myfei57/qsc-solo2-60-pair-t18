"""Backwash controller.

A wash is a fixed chain of steps (:data:`waterplant.backwash.report.STEPS`)
and the chain is enforced by *feedback*, never by timers:

* a step only moves forward once the device reports the demanded position
  (valve limit switch or pump auxiliary); an elapsed timer can never promote a
  step, it can only mark it as failed when its deadline passes;
* the drain valve is never commanded open unless the isolation valve has
  independently been read back fully closed;
* while the drain is not confirmed closed, a clearwell level at or below the
  safety line trips the interlock: pump stop, drain close, run aborted. The
  bed stays isolated and the wash must restart from the first step;
* a timed out step is a partial failure: outputs freeze in the last confirmed
  safe state and the run holds at that step. Re-triggering the same bed
  resumes from that step; commands already confirmed are not repeated and no
  duplicate audit rows are written.

Every state transition is persisted in the run document before its audit
entries are appended, and recovery reconciles the two, so an action and its
wash record cannot silently diverge.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Callable

from waterplant.audit.auditor import AUDIT_KEY
from waterplant.clearwell import Well
from waterplant.filter import Bank
from waterplant.store.commands import append_command, load_commands
from waterplant.store.epoch import load_float, save_float
from waterplant.store.store import Store

from .field import (
    PUMP_RUNNING,
    PUMP_STOPPED,
    VALVE_CLOSED,
    VALVE_OPEN,
    FieldBus,
)
from .report import (
    DRAIN_CLOSE,
    DRAIN_OPEN,
    EVENT_ABORT,
    EVENT_COMMAND,
    EVENT_COMPLETE,
    EVENT_CONFIRM,
    EVENT_GUARD,
    EVENT_HOLD,
    EVENT_INTERLOCK,
    EVENT_REJECT,
    EVENT_RESUME,
    EVENT_START,
    FLUSH,
    ISOLATE,
    PHASE_ABORTING,
    PHASE_COMMAND,
    PHASE_CONFIRMED,
    PHASE_DONE,
    PHASE_HELD,
    RESTORE,
    RUN_ABORTED,
    RUN_COMPLETE,
    RUN_RUNNING,
    STEPS,
    BackwashState,
    step_catalog,
)
from .simfield import SimFieldBus

COMMAND_KEY = "backwash:commands"
ACTIVE_KEY = "backwash:active"
HISTORY_KEY = "backwash:history"
SAFETY_KEY = "backwash:safety-level"
LAST_DRAIN_KEY = "backwash:last-drain"
SPILL_KEY = "backwash:spill"
HISTORY_LIMIT = 50

DEFAULT_VALVE_TIMEOUT = 60.0
DEFAULT_PUMP_TIMEOUT = 30.0
DEFAULT_FLUSH_SECONDS = 60.0
DEFAULT_SAFETY_LEVEL = 2.0


class Controller:
    """Sequences backwash steps against the filter bank and field devices."""

    def __init__(
        self,
        bank: Bank,
        store: Store,
        field: FieldBus | None = None,
        well: Well | None = None,
        clock: Callable[[], float] | None = None,
        valve_timeout: float = DEFAULT_VALVE_TIMEOUT,
        pump_timeout: float = DEFAULT_PUMP_TIMEOUT,
        flush_seconds: float = DEFAULT_FLUSH_SECONDS,
    ) -> None:
        self._bank = bank
        self._store = store
        self._field = field or SimFieldBus(travel_seconds=0.0)
        self._well = well or Well(store)
        self._clock = clock or time.time
        self._valve_timeout = valve_timeout
        self._pump_timeout = pump_timeout
        self._flush_seconds = flush_seconds

    # ------------------------------------------------------------------
    # configuration
    # ------------------------------------------------------------------

    def safety_level(self) -> float:
        value, present = load_float(self._store, SAFETY_KEY)
        return value if present else DEFAULT_SAFETY_LEVEL

    def set_safety_level(self, level: float) -> float:
        if level < 0:
            raise ValueError("safety level must be non-negative")
        save_float(self._store, SAFETY_KEY, level)
        return level

    def flush_seconds(self) -> float:
        return self._flush_seconds

    @property
    def field(self) -> FieldBus:
        return self._field

    # ------------------------------------------------------------------
    # start / resume / abort (operator triggers, all idempotent)
    # ------------------------------------------------------------------

    def start(self, bed_id: str, request_id: str = "") -> dict[str, object]:
        """Start a wash, or idempotently return/resume the one already running.

        Re-triggering a bed with a running wash is a no-op while it is still
        waiting on feedback; if it is holding after a timeout the same trigger
        resumes it from the held step. Starting a different bed while one is
        running, or starting below the clearwell safety line, is refused.
        """

        self._require_bed(bed_id)
        active = self._load_active()
        if active is not None:
            if active["bed_id"] != bed_id:
                self._external(
                    EVENT_REJECT,
                    bed_id,
                    f"run {active['run_id']} already washing {active['bed_id']}",
                )
                raise ValueError(
                    f"backwash already running for {active['bed_id']}; one wash at a time"
                )
            if active.get("phase") == PHASE_HELD or (
                active.get("abort") and active["abort"].get("held")
            ):
                self._resume(active, request_id)
            return self.poll(bed_id)

        if not self._level_safe():
            reason = (
                f"clearwell level {self._well.level():.4f} at or below safety line "
                f"{self.safety_level():.4f}"
            )
            self._external(EVENT_REJECT, bed_id, reason)
            raise ValueError(f"refusing backwash for {bed_id}: {reason}")

        self._register_field(bed_id)
        run = self._new_run(bed_id, request_id)
        self._event(run, EVENT_START, ISOLATE, "backwash sequence started")
        self._begin_step(run, ISOLATE, self._action_text(ISOLATE), self._valve_timeout)
        return self._pump(run)

    def _resume(self, run: dict[str, object], request_id: str) -> None:
        """Retry a held step: re-arm its deadline and re-issue one command.

        Re-delivery of the same operator request id is a no-op so a retried
        network call cannot re-arm a step a second time.
        """

        if request_id and run.get("resume_of") == request_id:
            return
        now = self._clock()
        step = str(run["step"])
        if run.get("abort") and run["abort"].get("held"):
            abort = run["abort"]
            abort["held"] = False
            abort["needs_command"] = True
            abort["commanded_at"] = now
            abort["deadline"] = now + (
                self._valve_timeout if abort["phase"] == "close_drain" else self._pump_timeout
            )
            scope = "safe shutdown"
        else:
            run["phase"] = PHASE_COMMAND
            run["needs_command"] = True
            run["commanded_at"] = now
            timeout = self._pump_timeout if step == FLUSH else self._valve_timeout
            run["deadline"] = now + timeout
            if step == FLUSH and run.get("flush_phase") == "flushing":
                # Pump dropped out mid flush: re-issue the start and retime.
                run["flush_phase"] = "start"
            scope = step
        run["resume_of"] = request_id
        self._event(run, EVENT_RESUME, step, f"operator retry after held {scope}")

    def poll(self, bed_id: str = "") -> dict[str, object]:
        """Advance the active run with fresh feedback and return its view."""

        active = self._load_active()
        if active is None:
            if bed_id:
                self._require_bed(bed_id)
            return {}
        if bed_id and active["bed_id"] != bed_id:
            raise ValueError(
                f"active run is for {active['bed_id']}, not {bed_id}"
            )
        return self._pump(active)

    def abort(self, bed_id: str, reason: str = "operator abort") -> dict[str, object]:
        """Trip the same safe shutdown the low-level interlock uses."""

        active = self._load_active()
        if active is None or active["bed_id"] != bed_id:
            raise ValueError(f"no active backwash for {bed_id}")
        if active["status"] != RUN_RUNNING:
            return self._run_view(active)
        self._trip_interlock(active, reason)
        return self._pump(active)

    # ------------------------------------------------------------------
    # the state machine: one bounded pump per trigger
    # ------------------------------------------------------------------

    def _pump(self, run: dict[str, object]) -> dict[str, object]:
        """Evaluate fresh feedback repeatedly until the run must wait externally."""

        for _ in range(len(STEPS) * 2 + 4):
            if run["status"] == RUN_ABORTED or run["status"] == RUN_COMPLETE:
                return self._run_view(run)

            if run["status"] == PHASE_ABORTING:
                if not self._pump_abort(run):
                    return self._run_view(run)
                continue

            step = str(run["step"])
            now = self._clock()

            # Interlock: applies for as long as the drain is not shut.
            drain = self._field.drain_position(str(run["bed_id"]))
            if step != ISOLATE and drain != VALVE_CLOSED and not self._level_safe():
                self._trip_interlock(
                    run,
                    f"clearwell level {self._well.level():.4f} at or below safety line "
                    f"{self.safety_level():.4f} during {step}",
                )
                continue

            progressed = False
            if step == ISOLATE:
                progressed = self._pump_valve(
                    run,
                    ISOLATE,
                    VALVE_CLOSED,
                    self._field.close_isolation,
                    self._field.isolation_position,
                    on_confirm=lambda: self._bank.close(str(run["bed_id"])),
                )
            elif step == DRAIN_OPEN:
                progressed = self._pump_drain_open(run)
            elif step == FLUSH:
                progressed = self._pump_flush(run)
            elif step == DRAIN_CLOSE:
                progressed = self._pump_valve(
                    run,
                    DRAIN_CLOSE,
                    VALVE_CLOSED,
                    self._field.close_drain,
                    self._field.drain_position,
                )
            elif step == RESTORE:
                progressed = self._pump_valve(
                    run,
                    RESTORE,
                    VALVE_OPEN,
                    self._field.open_isolation,
                    self._field.isolation_position,
                    on_confirm=lambda: self._bank.open(str(run["bed_id"])),
                )
            else:  # pragma: no cover - defensive
                raise ValueError(f"unknown backwash step {step}")

            if not progressed:
                return self._run_view(run)
        return self._run_view(run)

    def _begin_step(
        self, run: dict[str, object], step: str, action: str, timeout: float
    ) -> None:
        """Record entry into a step and arm its deadline."""

        now = self._clock()
        run["step"] = step
        run["phase"] = PHASE_COMMAND
        run["commanded_at"] = now
        run["deadline"] = now + timeout
        run["drain_open_confirmed"] = run.get("drain_open_confirmed", False)
        self._event(run, EVENT_COMMAND, step, action)
        self._save_active(run)

    def _pump_valve(
        self,
        run: dict[str, object],
        step: str,
        target: str,
        command: Callable[[str], None],
        read_position: Callable[[str], str],
        on_confirm: Callable[[], None] | None = None,
    ) -> bool:
        """Advance one valve step. Returns False when it must keep waiting."""

        bed_id = str(run["bed_id"])
        if run.get("phase") == PHASE_COMMAND and run.get("needs_command", True):
            command(bed_id)
            run["needs_command"] = False
            self._save_active(run)

        position = read_position(bed_id)
        now = self._clock()

        if position == target:
            if run.get("phase") != PHASE_CONFIRMED:
                run["phase"] = PHASE_CONFIRMED
                run["confirmed_at"] = now
                if on_confirm is not None:
                    on_confirm()
                self._event(
                    run,
                    EVENT_CONFIRM,
                    step,
                    f"position feedback {target} received",
                )
                self._advance(run, step)
            return True

        if run.get("phase") != PHASE_HELD and now >= float(run["deadline"]):
            run["phase"] = PHASE_HELD
            self._event(
                run,
                EVENT_HOLD,
                step,
                f"no {target} feedback before {float(run['deadline']):.0f}; "
                f"held at {step}, last feedback {position}",
            )
            self._save_active(run)
        return False

    def _pump_drain_open(self, run: dict[str, object]) -> bool:
        """Open the drain, but only behind an independently confirmed shut bed."""

        bed_id = str(run["bed_id"])
        if run.get("phase") is None or run["step"] != DRAIN_OPEN:
            return False

        if run.get("phase") == PHASE_COMMAND and run.get("needs_command", True):
            isolation = self._field.isolation_position(bed_id)
            if isolation != VALVE_CLOSED:
                # The previous step only advances on closed feedback, so a
                # non-closed reading here means that feedback was lost. Wait
                # out the deadline once; if it is still not shut, hold. The
                # drain command itself is never issued.
                now = self._clock()
                if now < float(run["deadline"]):
                    return False
                run["phase"] = PHASE_HELD
                self._event(
                    run,
                    EVENT_GUARD,
                    DRAIN_OPEN,
                    f"drain open blocked: isolation feedback is {isolation}, "
                    "must read fully closed first",
                )
                self._save_active(run)
                return False
            self._field.open_drain(bed_id)
            run["needs_command"] = False
            self._save_active(run)

        drain = self._field.drain_position(bed_id)
        now = self._clock()
        if drain == VALVE_OPEN:
            run["drain_open_confirmed"] = True
            if run.get("phase") != PHASE_CONFIRMED:
                run["phase"] = PHASE_CONFIRMED
                run["confirmed_at"] = now
                self._event(
                    run,
                    EVENT_CONFIRM,
                    DRAIN_OPEN,
                    "drain valve fully open by position feedback",
                )
                self._advance(run, DRAIN_OPEN)
            return True

        # Defence in depth: drain open while isolation not shut = spill hazard.
        if drain not in (VALVE_CLOSED,):
            if self._field.isolation_position(bed_id) != VALVE_CLOSED:
                self._store.put(SPILL_KEY, "true")
                self._event(
                    run,
                    EVENT_GUARD,
                    DRAIN_OPEN,
                    f"drain feedback {drain} while isolation not closed; spill hazard armed",
                )
                self._save_active(run)
                self._trip_interlock(run, "isolation lost while drain not closed")
                return True

        if run.get("phase") != PHASE_HELD and now >= float(run["deadline"]):
            run["phase"] = PHASE_HELD
            self._event(
                run,
                EVENT_HOLD,
                DRAIN_OPEN,
                f"drain not open before {float(run['deadline']):.0f}; held, feedback {drain}",
            )
            self._save_active(run)
        return False

    def _pump_flush(self, run: dict[str, object]) -> bool:
        """Start pump, hold the flush duration on running feedback, stop pump."""

        bed_id = str(run["bed_id"])
        sub = str(run.get("flush_phase", "start"))
        now = self._clock()

        if sub == "start":
            if run.get("needs_command", True):
                self._field.start_pump(bed_id)
                run["needs_command"] = False
                self._save_active(run)
            status = self._field.pump_status(bed_id)
            if status == PUMP_RUNNING:
                run["flush_phase"] = "flushing"
                run["running_at"] = now
                run["flush_until"] = now + self._flush_seconds
                self._event(
                    run,
                    EVENT_CONFIRM,
                    FLUSH,
                    "pump running by auxiliary feedback; flush timing started",
                )
                self._save_active(run)
                return True
            if run.get("phase") != PHASE_HELD and now >= float(run["deadline"]):
                run["phase"] = PHASE_HELD
                self._event(
                    run,
                    EVENT_HOLD,
                    FLUSH,
                    f"pump did not run before {float(run['deadline']):.0f}; status {status}",
                )
                self._save_active(run)
            return False

        if sub == "flushing":
            status = self._field.pump_status(bed_id)
            if status != PUMP_RUNNING:
                if run.get("phase") != PHASE_HELD:
                    run["phase"] = PHASE_HELD
                    self._event(
                        run,
                        EVENT_HOLD,
                        FLUSH,
                        f"pump feedback {status} mid flush; held, restart pump to resume",
                    )
                    self._save_active(run)
                return False
            if now < float(run["flush_until"]):
                return False
            run["flush_phase"] = "stop"
            run["needs_command"] = True
            run["phase"] = PHASE_COMMAND
            run["commanded_at"] = now
            run["deadline"] = now + self._pump_timeout
            self._event(
                run,
                EVENT_COMMAND,
                FLUSH,
                f"flush duration {self._flush_seconds:.0f}s elapsed; stopping pump",
            )
            self._save_active(run)
            return True

        # stop
        if run.get("needs_command", True):
            self._field.stop_pump(bed_id)
            run["needs_command"] = False
            self._save_active(run)
        status = self._field.pump_status(bed_id)
        if status == PUMP_STOPPED:
            run["phase"] = PHASE_CONFIRMED
            run["confirmed_at"] = now
            self._event(
                run,
                EVENT_CONFIRM,
                FLUSH,
                "pump stopped by auxiliary feedback after flush",
            )
            self._advance(run, FLUSH)
            return True
        if run.get("phase") != PHASE_HELD and now >= float(run["deadline"]):
            run["phase"] = PHASE_HELD
            self._event(
                run,
                EVENT_HOLD,
                FLUSH,
                f"pump did not stop before {float(run['deadline']):.0f}; status {status}",
            )
            self._save_active(run)
        return False

    def _advance(self, run: dict[str, object], completed_step: str) -> None:
        """Move into the next step, or complete the run after restore."""

        if completed_step == RESTORE:
            run["phase"] = PHASE_DONE
            run["status"] = RUN_COMPLETE
            run["ended_at"] = self._clock()
            run["drain_open_confirmed"] = False
            self._event(run, EVENT_COMPLETE, RESTORE, "backwash complete; bed back in service")
            self._finish(run)
            return

        index = STEPS.index(completed_step)
        nxt = STEPS[index + 1]
        timeout = self._pump_timeout if nxt == FLUSH else self._valve_timeout
        if nxt == FLUSH:
            run["flush_phase"] = "start"
        run["needs_command"] = True
        self._begin_step(
            run,
            nxt,
            self._action_text(nxt),
            timeout,
        )

    # ------------------------------------------------------------------
    # interlock abort: stop pump, close drain, then restart-from-scratch
    # ------------------------------------------------------------------

    def _trip_interlock(self, run: dict[str, object], reason: str) -> None:
        bed_id = str(run["bed_id"])
        run["status"] = PHASE_ABORTING
        now = self._clock()
        self._event(run, EVENT_INTERLOCK, str(run.get("step")), reason)
        self._event(
            run,
            EVENT_ABORT,
            str(run.get("step")),
            "safe shutdown: stop pump, close drain; bed held isolated; restart from first step",
        )
        self._field.stop_pump(bed_id)
        run["abort"] = {
            "phase": "stop_pump",
            "commanded_at": now,
            "deadline": now + self._pump_timeout,
            "needs_command": False,
            "held": False,
        }
        self._save_active(run)

    def _pump_abort(self, run: dict[str, object]) -> bool:
        """Drive the abort sequence to its terminal archived state."""

        bed_id = str(run["bed_id"])
        abort = run["abort"]
        now = self._clock()

        if abort["phase"] == "stop_pump":
            if abort.get("needs_command", True):
                self._field.stop_pump(bed_id)
                abort["needs_command"] = False
                self._save_active(run)
            status = self._field.pump_status(bed_id)
            if status == PUMP_STOPPED:
                abort["phase"] = "close_drain"
                abort["held"] = False
                abort["needs_command"] = True
                abort["commanded_at"] = now
                abort["deadline"] = now + self._valve_timeout
                self._event(run, EVENT_ABORT, FLUSH, "pump stopped; closing drain")
                self._save_active(run)
            elif not abort["held"] and now >= float(abort["deadline"]):
                abort["held"] = True
                self._event(
                    run,
                    EVENT_HOLD,
                    FLUSH,
                    f"abort stalled: pump feedback {status}; held in safe shutdown",
                )
                self._save_active(run)
            return True

        if abort.get("needs_command", True):
            drain = self._field.drain_position(bed_id)
            if drain == VALVE_CLOSED:
                pass  # already shut, nothing to command
            else:
                self._field.close_drain(bed_id)
            abort["needs_command"] = False
            self._save_active(run)

        drain = self._field.drain_position(bed_id)
        if drain == VALVE_CLOSED:
            run["status"] = RUN_ABORTED
            run["ended_at"] = now
            run["drain_open_confirmed"] = False
            # The bed stays isolated: bank.close is intentionally not released.
            self._bank.close(bed_id)
            self._event(
                run,
                EVENT_ABORT,
                DRAIN_CLOSE,
                "drain closed; abort complete, bed held isolated, restart required",
            )
            self._finish(run)
            return True

        if not abort["held"] and now >= float(abort["deadline"]):
            abort["held"] = True
            self._event(
                run,
                EVENT_HOLD,
                DRAIN_CLOSE,
                f"abort stalled: drain feedback {drain}; held in safe shutdown",
            )
            self._save_active(run)
        return False

    # ------------------------------------------------------------------
    # persistence and the action/audit consistency contract
    # ------------------------------------------------------------------

    def _new_run(self, bed_id: str, request_id: str) -> dict[str, object]:
        now = self._clock()
        run: dict[str, object] = {
            "run_id": uuid.uuid4().hex,
            "bed_id": bed_id,
            "request_id": request_id,
            "status": RUN_RUNNING,
            "step": ISOLATE,
            "phase": PHASE_COMMAND,
            "needs_command": True,
            "commanded_at": now,
            "deadline": now + self._valve_timeout,
            "started_at": now,
            "ended_at": None,
            "flush_phase": None,
            "drain_open_confirmed": False,
            "abort": None,
            "events": [],
        }
        self._save_active(run)
        return run

    def _event(
        self,
        run: dict[str, object],
        kind: str,
        step: str,
        detail: str,
    ) -> dict[str, object]:
        """Append one step event, persist the run, then mirror it to audit."""

        event = {
            "id": uuid.uuid4().hex,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "at_float": self._clock(),
            "kind": kind,
            "step": step,
            "detail": detail,
        }
        events = list(run.get("events", []))
        events.append(event)
        run["events"] = events
        self._save_active(run)
        self._audit_event(run, event)
        return event

    def _audit_event(self, run: dict[str, object], event: dict[str, object]) -> None:
        """Append the event to the audit log, keyed by event id (deduplicated)."""

        existing = self._audit_ids()
        if event["id"] in existing:
            return
        entry = {
            "id": event["id"],
            "time": event["at"],
            "kind": event["kind"],
            "detail": json.dumps(
                {
                    "run_id": run["run_id"],
                    "bed_id": run["bed_id"],
                    "step": event["step"],
                    "reason": event["detail"],
                },
                ensure_ascii=False,
            ),
        }
        append_command(self._store, AUDIT_KEY, json.dumps(entry, ensure_ascii=False))

    def _audit_ids(self) -> set[str]:
        ids: set[str] = set()
        for raw in load_commands(self._store, AUDIT_KEY):
            try:
                payload = json.loads(raw)
            except ValueError:
                continue
            if isinstance(payload, dict) and payload.get("id"):
                ids.add(str(payload["id"]))
        return ids

    def _reconcile_audit(self) -> None:
        """Re-append run/history audit rows that a crash kept out of the log."""

        docs: list[dict[str, object]] = []
        active = self._load_active()
        if active is not None:
            docs.append(active)
        docs.extend(self._load_history())
        for doc in docs:
            for event in doc.get("events", []):
                if isinstance(event, dict) and event.get("id"):
                    self._audit_event(doc, event)

    def _external(self, kind: str, bed_id: str, detail: str) -> None:
        """Audit an event that happened outside a run (refusals, retries)."""

        at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        event_id = uuid.uuid4().hex
        entry = {
            "id": event_id,
            "time": at,
            "kind": kind,
            "detail": json.dumps(
                {"run_id": None, "bed_id": bed_id, "step": None, "reason": detail},
                ensure_ascii=False,
            ),
        }
        if event_id not in self._audit_ids():
            append_command(self._store, AUDIT_KEY, json.dumps(entry, ensure_ascii=False))

    def _finish(self, run: dict[str, object]) -> None:
        """Archive a terminal run and publish its drain marker."""

        self._store.put(LAST_DRAIN_KEY, str(run["bed_id"]))
        history = self._load_history()
        history.append(run)
        del history[:-HISTORY_LIMIT]
        self._store.put(
            HISTORY_KEY,
            json.dumps([self._jsonable(doc) for doc in history], ensure_ascii=False),
        )
        self._store.delete(ACTIVE_KEY)

    def _save_active(self, run: dict[str, object]) -> None:
        self._store.put(ACTIVE_KEY, json.dumps(self._jsonable(run), ensure_ascii=False))

    def _load_active(self) -> dict[str, object] | None:
        raw, present = self._store.get(ACTIVE_KEY)
        if not present:
            return None
        try:
            doc = json.loads(raw)
        except ValueError:
            return None
        return doc if isinstance(doc, dict) else None

    def _load_history(self) -> list[dict[str, object]]:
        """History holds JSON run documents, not plain strings."""

        raw, present = self._store.get(HISTORY_KEY)
        if not present:
            return []
        try:
            docs = json.loads(raw)
        except ValueError:
            return []
        if not isinstance(docs, list):
            return []
        return [doc for doc in docs if isinstance(doc, dict)]

    @staticmethod
    def _jsonable(doc: dict[str, object]) -> dict[str, object]:
        return json.loads(json.dumps(doc, ensure_ascii=False, default=str))

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _level_safe(self) -> bool:
        return self._well.level() > self.safety_level()

    def _require_bed(self, bed_id: str) -> None:
        if self._bank.bed(bed_id) is None:
            raise ValueError(f"filter bed {bed_id} not found")

    def register_bed(self, bed_id: str) -> None:
        """Provision the field devices the sequence needs for one bed."""

        self._require_bed(bed_id)
        self._register_field(bed_id)

    def _register_field(self, bed_id: str) -> None:
        if isinstance(self._field, SimFieldBus) and not self._known_device(bed_id):
            self._field.register(bed_id)

    def _known_device(self, bed_id: str) -> bool:
        try:
            self._field.isolation_position(bed_id)
        except ValueError:
            return False
        return True

    def _action_text(self, step: str) -> str:
        for spec in step_catalog(self._valve_timeout, self._pump_timeout):
            if spec.name == step:
                return spec.action
        return step

    def _run_view(self, run: dict[str, object] | None) -> dict[str, object]:
        if run is None:
            return {}
        view = self._jsonable(run)
        view["safety_level"] = self.safety_level()
        view["clearwell_level"] = self._well.level()
        view["events"] = list(run.get("events", []))
        return view

    # ------------------------------------------------------------------
    # command queue (legacy console surface, kept safe)
    # ------------------------------------------------------------------

    def enqueue(self, bed_id: str) -> None:
        self._require_bed(bed_id)
        append_command(self._store, COMMAND_KEY, bed_id)

    def replay(self) -> list[str]:
        """Start the head of the queue; only one wash may run at a time."""

        commands = self.command_list()
        if not commands:
            return []
        bed_id = commands[0]
        remaining = commands[1:]
        self.start(bed_id)
        self._store.put(
            COMMAND_KEY, json.dumps(remaining, ensure_ascii=False)
        )
        return [bed_id]

    def recover(self) -> dict[str, object]:
        """Reconcile the persisted run with the audit log after a restart."""

        self._reconcile_audit()
        active = self._load_active()
        return self._run_view(active)

    def command_list(self) -> list[str]:
        return load_commands(self._store, COMMAND_KEY)

    def pending_count(self) -> int:
        return len(self.command_list())

    # ------------------------------------------------------------------
    # bank helpers (unchanged console surface)
    # ------------------------------------------------------------------

    def select(self, zone: int) -> str:
        mapping = self._bank.mapping()
        if zone not in mapping:
            raise ValueError(f"no filter bed for zone {zone}")
        return mapping[zone]

    def order_rotation(self) -> list[str]:
        order = [bed.id for bed in sorted(self._bank.beds(), key=lambda bed: -bed.load)]
        if order:
            self._bank.rotate(order[0])
        return order

    def eligible(self) -> list[str]:
        return [bed.id for bed in self._bank.beds() if not bed.closed]

    def drains(self) -> list[str]:
        """Beds whose drain is open in the live run, by persisted feedback."""

        active = self._load_active()
        if active is None or not active.get("drain_open_confirmed"):
            return []
        return [str(active["bed_id"])]

    def drain_count(self) -> int:
        return len(self.drains())

    def last_drain(self) -> str | None:
        value, present = self._store.get(LAST_DRAIN_KEY)
        return value if present else None

    def spilled(self) -> bool:
        return self._store.get(SPILL_KEY)[1]

    def history(self) -> list[dict[str, object]]:
        return self._load_history()

    def active_run(self) -> dict[str, object]:
        return self._run_view(self._load_active())

    def state(self) -> BackwashState:
        active = self._load_active()
        return BackwashState(
            commands=self.command_list(),
            drains=self.drains(),
            active=self._run_view(active) if active is not None else None,
            history=self._load_history(),
            steps=step_catalog(self._valve_timeout, self._pump_timeout),
            safety_level=self.safety_level(),
            spilled=self.spilled(),
        )

    def describe(self) -> str:
        active = self._load_active()
        if active is None:
            live = "idle"
        else:
            live = (
                f"run={str(active['run_id'])[:8]} bed={active['bed_id']} "
                f"status={active['status']} step={active['step']} phase={active['phase']}"
            )
        return (
            f"backwash commands={len(self.command_list())} "
            f"drains={self.drain_count()} {live}"
        )
