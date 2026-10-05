"""Behavioural tests for the control components."""

from __future__ import annotations

import json
import tempfile
import unittest

from waterplant.audit import Auditor
from waterplant.backwash import (
    Controller,
    PUMP_RUNNING,
    PUMP_STOPPED,
    SimFieldBus,
    VALVE_CLOSED,
    VALVE_OPEN,
)
from waterplant.backwash.report import (
    DRAIN_CLOSE,
    DRAIN_OPEN,
    FLUSH,
    ISOLATE,
    PHASE_COMMAND,
    PHASE_HELD,
    RESTORE,
    RUN_ABORTED,
    RUN_COMPLETE,
    RUN_RUNNING,
    STEPS,
)
from waterplant.chlor import Doser as ChlorDoser
from waterplant.chlor import validate_demand
from waterplant.clearwell import Well, validate_level
from waterplant.coag import Doser as CoagDoser
from waterplant.filter import Bank, validate_zone
from waterplant.flow import Calibration, calibrate_meter, validate_factor
from waterplant.intake import FlowRepository, InletController, Sensor, mix, validate_flow
from waterplant.intake import Trend
from waterplant.inventory import Inventory, Lot, validate_quantity
from waterplant.ns import Stage, treatment_line
from waterplant.ph import Stabilizer, validate_ph
from waterplant.quota import Accumulator, Quota, check_quota, validate_amount
from waterplant.reporting import audit_export, to_csv
from waterplant.scheduler import Scheduler, validate_threshold
from waterplant.store import (
    Store,
    append_command,
    append_event,
    advance_epoch,
    clear_commands,
    event_count,
    list_events,
    load_commands,
)
from waterplant.turb import Sampler, verdict_for


class StoreCase(unittest.TestCase):
    def test_round_trip_persists_and_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(f"{tmp}/state.json")
            store.put("alpha", "1")
            store.put("beta", "2")
            self.assertEqual(store.keys(), ["alpha", "beta"])
            self.assertEqual(store.count(), 2)
            self.assertEqual(store.get("alpha"), ("1", True))
            self.assertEqual(store.get("missing"), ("", False))
            store.delete("alpha")
            self.assertEqual(store.keys(), ["beta"])
            reopened = Store.open(f"{tmp}/state.json")
            self.assertEqual(reopened.get("beta"), ("2", True))
            store.clear()
            self.assertEqual(store.count(), 0)

    def test_command_lists_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(f"{tmp}/state.json")
            self.assertEqual(load_commands(store, "list"), [])
            append_command(store, "list", "one")
            append_command(store, "list", "two")
            self.assertEqual(load_commands(store, "list"), ["one", "two"])
            clear_commands(store, "list")
            self.assertEqual(load_commands(store, "list"), [])

    def test_epoch_wraps_and_stays_positive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(f"{tmp}/state.json")
            self.assertEqual(advance_epoch(store, "counter", 40.0, 100.0), 40.0)
            self.assertEqual(advance_epoch(store, "counter", 90.0, 100.0), 30.0)
            self.assertGreaterEqual(advance_epoch(store, "counter", 95.0, 100.0), 0.0)

    def test_history_records_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(f"{tmp}/state.json")
            append_event(store, "events", "seed", "defaults")
            append_event(store, "events", "manual", "operator")
            events = list_events(store, "events")
            self.assertEqual([event.kind for event in events], ["seed", "manual"])
            self.assertEqual(event_count(store, "events"), 2)
            self.assertGreater(events[0].at, 0)


class PipelineCase(unittest.TestCase):
    def test_treatment_line_is_ordered(self) -> None:
        line = treatment_line()
        self.assertEqual(line.count(), 7)
        self.assertTrue(line.contains(Stage.FILTER))
        self.assertTrue(line.before(Stage.INTAKE, Stage.COAG))
        self.assertFalse(line.before(Stage.AUDIT, Stage.COAG))
        self.assertEqual(line.last(), Stage.AUDIT)
        self.assertEqual(len(line.steps()), 7)
        self.assertEqual(line.steps()[0].action, "collect flow")
        self.assertIn("treatment", line.describe())


class IntakeCase(unittest.TestCase):
    def test_sensor_payload_defaults(self) -> None:
        sensor = Sensor.from_payload({"flow": 12})
        self.assertEqual(sensor.flow, 12.0)
        self.assertEqual(sensor.turbidity, 0.0)
        self.assertEqual(sensor.as_dict(), {"flow": 12.0, "turbidity": 0.0})

    def test_repository_records_and_reloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(f"{tmp}/state.json")
            repository = FlowRepository(store)
            self.assertEqual(repository.load_flow(), (0.0, False))
            repository.record(Sensor(flow=9.5, turbidity=2.5))
            self.assertEqual(repository.load_flow(), (9.5, True))
            self.assertEqual(repository.turbidity(), 2.5)
            self.assertTrue(repository.state().present)
            self.assertIn("flow=9.5000", repository.describe())

    def test_validation_and_mixing(self) -> None:
        validate_flow(0.0)
        with self.assertRaises(ValueError):
            validate_flow(-1.0)
        with self.assertRaises(ValueError):
            validate_flow(2_000_000.0)
        self.assertEqual(mix([]), 0.0)
        self.assertEqual(mix([2.0, 4.0]), 3.0)

    def test_inlet_controller_never_goes_negative(self) -> None:
        controller = InletController()
        self.assertEqual(controller.raise_level(1.0, 2.0), 3.0)
        self.assertEqual(controller.lower_level(1.0, 5.0), 0.0)


class DosingCase(unittest.TestCase):
    def test_coagulant_dose_follows_persisted_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(f"{tmp}/state.json")
            repository = FlowRepository(store)
            repository.persist_flow(10.0)
            doser = CoagDoser(store)
            self.assertEqual(doser.update_flow_and_dose(20.0), 20.0)
            self.assertEqual(doser.state().persisted_flow, 20.0)
            self.assertIn("coagulant", doser.describe())

    def test_coagulant_ratio_tracks_calibration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(f"{tmp}/state.json")
            doser = CoagDoser(store)
            self.assertEqual(doser.current_ratio(), 1.0)
            Calibration(store).replace(2.0)
            self.assertEqual(doser.current_ratio(), 2.0)
            self.assertEqual(doser.dose_for_flow(3.0), 6.0)
            self.assertEqual(doser.dose_plan(1.0, 1.0), 2.0)

    def test_chlorine_dose_follows_fresh_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(f"{tmp}/state.json")
            doser = ChlorDoser(store)
            self.assertEqual(doser.current_target(), 0.5)
            Well(store).update_residual_demand(1.0)
            self.assertEqual(doser.current_target(), 1.0)
            self.assertEqual(doser.apply_residual(), 1.0)
            self.assertEqual(doser.target_for_demand(0.0), 0.3)
            self.assertIn("chlorine", doser.describe())

    def test_demand_validation(self) -> None:
        validate_demand(0.0)
        with self.assertRaises(ValueError):
            validate_demand(-0.1)
        with self.assertRaises(ValueError):
            validate_demand(2000.0)


class TurbidityCase(unittest.TestCase):
    def test_verdict_and_sampling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(f"{tmp}/state.json")
            sampler = Sampler(CoagDoser(store))
            self.assertEqual(sampler.judge([0.2, 0.4]), 0.0)
            self.assertEqual(sampler.state().last_verdict, 0.0)
            self.assertEqual(sampler.judge([4.0, 6.0]), 5.0)
            self.assertEqual(verdict_for(0.5), 0.0)
            self.assertIn("turbidity", sampler.describe())


class CalibrationCase(unittest.TestCase):
    def test_meter_replacement_updates_factor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(f"{tmp}/state.json")
            calibration = Calibration(store)
            self.assertEqual(calibration.current(), 1.0)
            meter = calibrate_meter("fm-2", 1.5)
            calibration.replace(meter.factor)
            self.assertEqual(calibration.current(), 1.5)
            self.assertEqual(calibration.state().factor, 1.5)
            self.assertEqual(meter.as_dict(), {"serial": "fm-2", "factor": 1.5})
            self.assertIn("factor=1.5000", calibration.describe())

    def test_factor_validation(self) -> None:
        validate_factor(1.0)
        with self.assertRaises(ValueError):
            validate_factor(0.0)
        with self.assertRaises(ValueError):
            validate_factor(500.0)


class ClearwellCase(unittest.TestCase):
    def test_adjustment_raises_before_lowering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(f"{tmp}/state.json")
            well = Well(store)
            well.set_level(10.0)
            minimum = well.adjust_level(14.0, InletController(), InletController())
            self.assertEqual(minimum, 10.0)
            self.assertEqual(well.level(), 14.0)
            self.assertEqual(well.adjust_level(6.0, InletController(), InletController()), 6.0)
            self.assertEqual(well.state().level, 6.0)
            self.assertIn("clearwell", well.describe())

    def test_level_validation(self) -> None:
        validate_level(0.0)
        with self.assertRaises(ValueError):
            validate_level(-0.5)
        with self.assertRaises(ValueError):
            validate_level(2_000_000.0)


class FilterCase(unittest.TestCase):
    def test_bank_lifecycle_and_rotation(self) -> None:
        bank = Bank()
        bank.add_bed("b1", 1, 5.0)
        bank.add_bed("b2", 2, 9.0)
        bank.add_bed("b3", 3, 1.0)
        self.assertEqual(bank.count(), 3)
        self.assertEqual(bank.bed_ids(), ["b1", "b2", "b3"])
        self.assertEqual(bank.dirtiest(), "b2")
        self.assertEqual(bank.cleanest(), "b3")
        self.assertEqual(bank.total_load(), 15.0)
        bank.close("b1")
        self.assertTrue(bank.is_closed("b1"))
        self.assertEqual(bank.active_count(), 2)
        bank.open("b1")
        self.assertFalse(bank.is_closed("b1"))
        bank.rotate("b2")
        self.assertEqual(bank.on_duty(), "b2")
        bank.reset_closed()
        self.assertEqual(bank.active_count(), 3)
        bank.renumber("b3", 9)
        self.assertEqual(bank.mapping()[9], "b3")
        self.assertEqual(bank.bed_zone("b3"), (9, True))
        self.assertIn("beds=3", bank.describe())
        bank.remove_bed("b1")
        self.assertEqual(bank.count(), 2)

    def test_unknown_bed_raises(self) -> None:
        bank = Bank()
        with self.assertRaises(ValueError):
            bank.close("missing")
        with self.assertRaises(ValueError):
            bank.remove_bed("missing")
        with self.assertRaises(ValueError):
            bank.rotate("missing")
        self.assertEqual(bank.bed("missing"), None)
        self.assertEqual(bank.on_duty(), None)
        self.assertEqual(bank.dirtiest(), "")

    def test_zone_validation(self) -> None:
        validate_zone(1)
        with self.assertRaises(ValueError):
            validate_zone(-1)
        with self.assertRaises(ValueError):
            validate_zone(200_000)


class FakeClock:
    """Adjustable monotonic clock for deadline tests."""

    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class BackwashCase(unittest.TestCase):
    def _controller(
        self,
        tmp: str,
        beds: tuple[str, ...] = ("b1",),
        level: float = 10.0,
        travel: float = 0.0,
        valve_timeout: float = 60.0,
        pump_timeout: float = 30.0,
        flush_seconds: float = 0.0,
        safety: float = 2.0,
        clock: FakeClock | None = None,
    ):
        store = Store.open(f"{tmp}/state.json")
        bank = Bank()
        for index, bed_id in enumerate(beds, start=1):
            bank.add_bed(bed_id, index, 4.0)
        well = Well(store)
        well.set_level(level)
        fake_clock = clock or FakeClock()
        field = SimFieldBus(travel_seconds=travel)
        for bed_id in beds:
            field.register(bed_id)
        controller = Controller(
            bank,
            store,
            field=field,
            well=well,
            clock=fake_clock,
            valve_timeout=valve_timeout,
            pump_timeout=pump_timeout,
            flush_seconds=flush_seconds,
        )
        controller.set_safety_level(safety)
        return store, bank, well, field, controller, fake_clock

    def test_happy_path_confirms_every_step_by_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, bank, _, field, controller, _clock = self._controller(tmp)
            run = controller.start("b1")
            # Instant feedback devices: the whole chain completes in one trigger.
            self.assertEqual(run["status"], RUN_COMPLETE)
            self.assertEqual(bank.is_closed("b1"), False)
            self.assertEqual(field.isolation_position("b1"), VALVE_OPEN)
            self.assertEqual(field.drain_position("b1"), VALVE_CLOSED)
            self.assertEqual(field.pump_status("b1"), PUMP_STOPPED)
            kinds = [event["kind"] for event in run["events"]]
            self.assertEqual(
                [event["step"] for event in run["events"] if event["kind"] == "backwash.command"],
                [ISOLATE, DRAIN_OPEN, FLUSH, FLUSH, DRAIN_CLOSE, RESTORE],
            )
            confirm_steps = [
                event["step"] for event in run["events"] if event["kind"] == "backwash.confirm"
            ]
            self.assertEqual(confirm_steps, [ISOLATE, DRAIN_OPEN, FLUSH, FLUSH, DRAIN_CLOSE, RESTORE])
            self.assertIn("backwash.complete", kinds)
            self.assertFalse(controller.spilled())
            self.assertEqual(controller.history()[0]["status"], RUN_COMPLETE)
            self.assertEqual(controller.describe().split()[-1], "idle")

    def test_drain_is_blocked_until_isolation_reports_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, _, field, controller, _clock = self._controller(
                tmp, travel=10.0, valve_timeout=60.0, pump_timeout=60.0
            )
            run = controller.start("b1")
            # Isolation still moving: the drain must never have been commanded.
            self.assertEqual(run["status"], RUN_RUNNING)
            self.assertEqual(run["step"], ISOLATE)
            self.assertEqual(field.drain_position("b1"), VALVE_CLOSED)
            self.assertEqual(field.command_count("b1", "drain"), 0)

            field.tick(10.0)
            run = controller.poll("b1")
            self.assertEqual(run["step"], DRAIN_OPEN)
            self.assertEqual(field.command_count("b1", "drain"), 1)
            field.tick(10.0)  # drain travels open
            run = controller.poll("b1")
            self.assertEqual(run["step"], FLUSH)
            field.tick(10.0)  # pump starts and reaches running feedback
            run = controller.poll("b1")
            self.assertEqual(run["flush_phase"], "stop")
            field.tick(10.0)  # pump stops on feedback
            run = controller.poll("b1")
            self.assertEqual(run["step"], DRAIN_CLOSE)
            field.tick(10.0)  # drain close travels
            run = controller.poll("b1")
            self.assertEqual(run["step"], RESTORE)
            field.tick(10.0)  # restore travels
            run = controller.poll("b1")
            self.assertEqual(run["status"], RUN_COMPLETE)
            self.assertEqual(field.command_count("b1", "drain"), 2)

    def test_isolation_timeout_holds_and_resumes_from_same_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, _, field, controller, clock = self._controller(tmp, valve_timeout=5.0)
            field.set_mode("b1", "isolation", "stuck")
            run = controller.start("b1")
            self.assertEqual(run["step"], ISOLATE)
            self.assertEqual(run["phase"], PHASE_COMMAND)
            self.assertEqual(field.command_count("b1", "isolation"), 1)

            clock.advance(6.0)
            run = controller.poll("b1")
            self.assertEqual(run["phase"], PHASE_HELD)
            self.assertEqual(run["step"], ISOLATE)
            self.assertEqual(field.command_count("b1", "isolation"), 1)
            kinds = [event["kind"] for event in run["events"]]
            self.assertEqual(kinds.count("backwash.timeout"), 1)

            # Repeated polls while held do nothing; the drain stays shut.
            run = controller.poll("b1")
            self.assertEqual(field.command_count("b1", "isolation"), 1)
            self.assertEqual(field.command_count("b1", "drain"), 0)

            # Recovery clears the fault and retries; it resumes, not restarts.
            field.set_mode("b1", "isolation", "normal")
            run = controller.start("b1", request_id="op-1")
            self.assertEqual(run["status"], RUN_COMPLETE)
            # The initial command, one re-issued command on resume, and the
            # final restore command: the bed itself is commanded twice shut.
            self.assertEqual(field.command_count("b1", "isolation"), 3)
            self.assertEqual(sum(1 for e in run["events"] if e["step"] == ISOLATE), 5)
            self.assertIn("backwash.resume", [event["kind"] for event in run["events"]])

            # Re-delivery of the same request id is idempotent.
            run2 = controller.history()[-1]
            self.assertEqual(
                sum(1 for e in run2["events"] if e["kind"] == "backwash.resume"), 1
            )

    def test_held_step_repeated_trigger_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, _, field, controller, clock = self._controller(tmp, valve_timeout=5.0)
            field.set_mode("b1", "isolation", "stuck")
            controller.start("b1")
            clock.advance(6.0)
            controller.poll("b1")
            run = controller.start("b1", request_id="dup")
            run = controller.start("b1", request_id="dup")
            resumes = [e for e in run["events"] if e["kind"] == "backwash.resume"]
            self.assertEqual(len(resumes), 1)

    def test_low_clearwell_level_rejects_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, _, _, controller, _clock = self._controller(tmp, level=1.5, safety=2.0)
            with self.assertRaises(ValueError):
                controller.start("b1")
            self.assertEqual(controller.active_run(), {})

    def test_interlock_aborts_flush_and_restarts_from_first_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, bank, well, field, controller, clock = self._controller(
                tmp, flush_seconds=60.0, pump_timeout=30.0
            )
            run = controller.start("b1")
            # Instant devices: pump starts, flush timing is now armed.
            self.assertEqual(run["step"], FLUSH)
            self.assertEqual(run["flush_phase"], "flushing")
            self.assertEqual(field.pump_status("b1"), PUMP_RUNNING)
            self.assertEqual(field.drain_position("b1"), VALVE_OPEN)

            well.set_level(1.0)  # below the safety line
            run = controller.poll("b1")
            self.assertEqual(run["status"], RUN_ABORTED)
            self.assertEqual(field.pump_status("b1"), PUMP_STOPPED)
            self.assertEqual(field.drain_position("b1"), VALVE_CLOSED)
            # The bed stays isolated after the abort.
            self.assertTrue(bank.is_closed("b1"))
            kinds = [event["kind"] for event in run["events"]]
            self.assertIn("backwash.interlock", kinds)
            aborted = controller.history()[-1]
            self.assertEqual(aborted["status"], RUN_ABORTED)

            # Level still unsafe: cannot start again.
            with self.assertRaises(ValueError):
                controller.start("b1")

            # Level restored: a fresh run starts from the first step and must
            # go through the whole feedback chain again, including the flush.
            well.set_level(10.0)
            run = controller.start("b1")
            self.assertEqual(run["step"], FLUSH)
            self.assertEqual(run["flush_phase"], "flushing")
            clock.advance(61.0)
            run = controller.poll("b1")
            self.assertEqual(run["status"], RUN_COMPLETE)
            self.assertEqual(len(controller.history()), 2)
            self.assertEqual(
                [doc["status"] for doc in controller.history()],
                [RUN_ABORTED, RUN_COMPLETE],
            )

    def test_interlock_during_drain_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, well, field, controller, _clock = self._controller(
                tmp, travel=10.0, valve_timeout=60.0
            )
            controller.start("b1")
            field.tick(10.0)
            run = controller.poll("b1")
            self.assertEqual(run["step"], DRAIN_OPEN)
            field.tick(5.0)  # drain still moving
            well.set_level(0.5)
            run = controller.poll("b1")
            # Abort shutdown commanded; the drain needs travel time to close.
            self.assertEqual(run["status"], "aborting")
            field.tick(10.0)
            run = controller.poll("b1")
            self.assertEqual(run["status"], RUN_ABORTED)
            self.assertEqual(field.drain_position("b1"), VALVE_CLOSED)

    def test_flush_runs_full_duration_then_closes_drain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, _, field, controller, clock = self._controller(tmp, flush_seconds=60.0)
            run = controller.start("b1")
            self.assertEqual(run["status"], RUN_RUNNING)
            self.assertEqual(run["flush_phase"], "flushing")

            clock.advance(30.0)
            run = controller.poll("b1")
            self.assertEqual(run["flush_phase"], "flushing")
            self.assertEqual(field.pump_status("b1"), PUMP_RUNNING)

            clock.advance(31.0)
            run = controller.poll("b1")
            self.assertEqual(run["status"], RUN_COMPLETE)
            self.assertEqual(field.pump_status("b1"), PUMP_STOPPED)

    def test_pump_failure_mid_flush_holds_and_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, _, field, controller, clock = self._controller(
                tmp, flush_seconds=60.0, pump_timeout=30.0
            )
            controller.start("b1")
            # Pump drops to fault while flushing; the run holds regardless of
            # the flush deadline because the running feedback is gone.
            field.set_mode("b1", "pump", "stuck")
            run = controller.poll("b1")
            self.assertEqual(run["step"], FLUSH)
            self.assertEqual(run["phase"], PHASE_HELD)
            self.assertEqual(run["flush_phase"], "flushing")
            self.assertEqual(field.drain_position("b1"), VALVE_OPEN)

            field.set_mode("b1", "pump", "normal")
            run = controller.start("b1", request_id="retry")
            # Pump re-starts and the flush times again from zero.
            self.assertEqual(run["flush_phase"], "flushing")
            clock.advance(61.0)
            run = controller.poll("b1")
            self.assertEqual(run["status"], RUN_COMPLETE)

    def test_abort_stall_holds_and_resumes_safe_shutdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, well, field, controller, clock = self._controller(
                tmp, flush_seconds=60.0, pump_timeout=30.0
            )
            controller.start("b1")
            well.set_level(1.0)
            # The pump ignores the stop command: the abort stalls in the
            # stop_pump phase and holds rather than forcing the drain shut.
            field.set_mode("b1", "pump", "stuck")
            run = controller.poll("b1")
            self.assertEqual(run["status"], "aborting")
            self.assertTrue(run["abort"]["phase"] == "stop_pump")
            clock.advance(31.0)
            run = controller.poll("b1")
            self.assertTrue(run["abort"]["held"])
            self.assertEqual(field.drain_position("b1"), VALVE_OPEN)

            field.set_mode("b1", "pump", "normal")
            run = controller.start("b1", request_id="recover-abort")
            self.assertEqual(run["status"], RUN_ABORTED)
            self.assertEqual(field.pump_status("b1"), PUMP_STOPPED)
            self.assertEqual(field.drain_position("b1"), VALVE_CLOSED)

    def test_audit_trail_and_run_document_stay_consistent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, _, _, _, controller, _clock = self._controller(tmp)
            controller.start("b1")
            raw, present = store.get("backwash:history")
            self.assertTrue(present)
            run_doc = json.loads(raw)[0]
            event_ids = {event["id"] for event in run_doc["events"]}
            from waterplant.audit.auditor import AUDIT_KEY

            audit_ids = set()
            for item in load_commands(store, AUDIT_KEY):
                audit_ids.add(json.loads(item)["id"])
            self.assertTrue(event_ids <= audit_ids)
            # Recovery reconciles without duplicating rows.
            before = len(load_commands(store, AUDIT_KEY))
            controller.recover()
            self.assertEqual(len(load_commands(store, AUDIT_KEY)), before)

    def test_one_wash_at_a_time_and_unknown_bed_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, _, field, controller, _clock = self._controller(tmp, beds=("b1", "b2"))
            with self.assertRaises(ValueError):
                controller.start("missing")
            field.set_mode("b1", "isolation", "stuck")
            controller.start("b1")
            with self.assertRaises(ValueError):
                controller.start("b2")

    def test_queue_replay_starts_one_bed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, bank, _, _, controller, _clock = self._controller(tmp, beds=("b1", "b2"))
            controller.enqueue("b1")
            controller.enqueue("b2")
            self.assertEqual(controller.pending_count(), 2)
            self.assertEqual(controller.replay(), ["b1"])
            self.assertEqual(controller.command_list(), ["b2"])
            # The run completed end to end, so the bed is back in service.
            self.assertFalse(bank.is_closed("b1"))
            self.assertEqual(controller.pending_count(), 1)
            self.assertEqual(controller.replay(), ["b2"])
            self.assertEqual(controller.command_list(), [])
            self.assertFalse(bank.is_closed("b2"))

    def test_recovery_returns_active_run_view(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, _, field, controller, clock = self._controller(tmp, valve_timeout=5.0)
            field.set_mode("b1", "isolation", "stuck")
            controller.start("b1")
            clock.advance(6.0)
            controller.poll("b1")
            view = controller.recover()
            self.assertEqual(view["step"], ISOLATE)
            self.assertEqual(view["phase"], PHASE_HELD)

    def test_rotation_and_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, bank, _, _, controller, _clock = self._controller(tmp, beds=("b1", "b2"))
            bank.set_load("b1", 4.0)
            bank.set_load("b2", 9.0)
            self.assertEqual(controller.order_rotation(), ["b2", "b1"])
            self.assertEqual(bank.on_duty(), "b2")
            self.assertEqual(controller.select(1), "b1")
            with self.assertRaises(ValueError):
                controller.select(7)

    def test_state_catalog_lists_actions_and_confirmations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, _, _, controller, _clock = self._controller(tmp)
            state = controller.state()
            self.assertEqual([spec["step"] for spec in state.as_dict()["steps"]], list(STEPS))
            self.assertIn("fully closed", state.as_dict()["steps"][0]["confirmation"])
            self.assertEqual(state.safety_level, 2.0)


class QuotaCase(unittest.TestCase):
    def test_accumulator_and_remaining(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(f"{tmp}/state.json")
            accumulator = Accumulator(store)
            self.assertEqual(accumulator.add(10.0), 10.0)
            self.assertEqual(accumulator.value(), 10.0)
            self.assertEqual(accumulator.remaining(30.0), 20.0)
            self.assertEqual(accumulator.remaining(5.0), 0.0)
            accumulator.reset()
            self.assertEqual(accumulator.value(), 0.0)
            self.assertIn("quota accumulator", accumulator.describe())

    def test_check_quota_and_validation(self) -> None:
        quota = Quota(chemical="chlorine", limit=100.0)
        self.assertEqual(check_quota(quota.chemical, 40.0, quota.limit), (60.0, True))
        self.assertEqual(check_quota(quota.chemical, 120.0, quota.limit), (-20.0, False))
        self.assertEqual(check_quota(quota.chemical, 5.0, 0.0), (5.0, True))
        validate_amount(1.0)
        with self.assertRaises(ValueError):
            validate_amount(-1.0)


class AuditCase(unittest.TestCase):
    def test_entries_and_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(f"{tmp}/state.json")
            auditor = Auditor(store)
            self.assertEqual(auditor.count(), 0)
            self.assertEqual(auditor.last(), None)
            auditor.record("coagulant", "1.0000")
            auditor.record("chlorine", "0.5000")
            auditor.record("coagulant", "2.0000")
            self.assertEqual(auditor.count(), 3)
            self.assertEqual(auditor.count_by_kind(), {"coagulant": 2, "chlorine": 1})
            self.assertEqual(len(auditor.filter("coagulant")), 2)
            self.assertEqual(auditor.kinds(), ["coagulant", "chlorine"])
            self.assertEqual(auditor.last().kind, "coagulant")
            self.assertEqual(auditor.state().count, 3)
            self.assertEqual(auditor.totals(), {"coagulant": 3.0, "chlorine": 0.5})
            self.assertIn("audit entries=3", auditor.describe())


class PhCase(unittest.TestCase):
    def test_band_verdicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(f"{tmp}/state.json")
            stabilizer = Stabilizer(store)
            self.assertEqual(stabilizer.current(), 7.0)
            self.assertTrue(stabilizer.is_stable())
            self.assertEqual(stabilizer.adjustment(), 0.0)
            self.assertEqual(stabilizer.stabilize().direction, "hold")

            verdict = stabilizer.read(6.0)
            self.assertFalse(verdict.stable)
            self.assertEqual(verdict.direction, "raise")
            self.assertAlmostEqual(verdict.adjustment, 0.5)

            verdict = stabilizer.read(9.0)
            self.assertEqual(verdict.direction, "lower")
            self.assertAlmostEqual(verdict.adjustment, -0.5)
            self.assertIn("ph value", stabilizer.describe())
            self.assertFalse(stabilizer.state().stable)

    def test_ph_validation(self) -> None:
        validate_ph(7.0)
        with self.assertRaises(ValueError):
            validate_ph(-0.1)
        with self.assertRaises(ValueError):
            validate_ph(20.0)


class SchedulerCase(unittest.TestCase):
    def test_plan_and_due_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(f"{tmp}/state.json")
            bank = Bank()
            bank.add_bed("b1", 1, 2.0)
            bank.add_bed("b2", 2, 9.0)
            bank.add_bed("b3", 3, 6.0)
            scheduler = Scheduler(store)
            self.assertEqual(scheduler.threshold(), 6.0)
            self.assertEqual(scheduler.due(bank), ["b2", "b3"])
            plan = scheduler.plan(bank)
            self.assertEqual([entry.bed_id for entry in plan], ["b2", "b3", "b1"])
            self.assertEqual([entry.priority for entry in plan], [1, 2, 3])
            self.assertEqual(plan[0].drain_seconds, 75)
            scheduler.set_threshold(3.0)
            self.assertEqual(scheduler.due(bank), ["b2", "b3"])
            self.assertEqual(scheduler.state(bank).threshold, 3.0)
            self.assertIn("schedule threshold", scheduler.describe(bank))

    def test_threshold_validation(self) -> None:
        validate_threshold(1.0)
        with self.assertRaises(ValueError):
            validate_threshold(0.0)
        with self.assertRaises(ValueError):
            validate_threshold(2_000_000.0)


class TrendCase(unittest.TestCase):
    def test_window_and_statistics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(f"{tmp}/state.json")
            trend = Trend(store)
            self.assertEqual(trend.window(), 12)
            self.assertEqual(trend.stats().samples, 0)
            trend.record(10.0)
            trend.record(20.0)
            stats = trend.record(30.0)
            self.assertEqual(stats.samples, 3)
            self.assertEqual(stats.mean, 20.0)
            self.assertEqual(stats.minimum, 10.0)
            self.assertEqual(stats.maximum, 30.0)
            trend.set_window(2)
            self.assertEqual(trend.values(), [20.0, 30.0])
            self.assertIn("intake trend", trend.describe())
            trend.reset()
            self.assertEqual(trend.values(), [])

    def test_window_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trend = Trend(Store.open(f"{tmp}/state.json"))
            with self.assertRaises(ValueError):
                trend.set_window(0)
            with self.assertRaises(ValueError):
                trend.set_window(5000)


class InventoryCase(unittest.TestCase):
    def test_receive_consume_and_reorder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(f"{tmp}/state.json")
            inventory = Inventory(store)
            self.assertEqual(inventory.balance(), 0.0)
            self.assertEqual(inventory.reorder_level(), 25.0)
            self.assertEqual(inventory.chemicals(), [])

            inventory.receive("chlorine", "L1", 40.0)
            inventory.receive("chlorine", "L2", 30.0)
            inventory.receive("coagulant", "C1", 10.0)
            self.assertEqual(inventory.balance(), 80.0)
            self.assertEqual(inventory.balance("chlorine"), 70.0)
            self.assertEqual(inventory.chemicals(), ["chlorine", "coagulant"])

            inventory.consume("chlorine", 50.0)
            lots = inventory.lots("chlorine")
            self.assertEqual([lot.consumed for lot in lots], [40.0, 10.0])
            self.assertEqual(inventory.balance("chlorine"), 20.0)
            self.assertEqual(inventory.needs_reorder(), ["chlorine", "coagulant"])

            inventory.set_reorder_level(5.0)
            self.assertEqual(inventory.needs_reorder(), [])
            state = inventory.state()
            self.assertEqual(state.reorder_level, 5.0)
            self.assertEqual(len(state.lots), 3)
            self.assertIn("inventory balance", inventory.describe())

    def test_insufficient_stock_and_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inventory = Inventory(Store.open(f"{tmp}/state.json"))
            inventory.receive("coagulant", "C1", 5.0)
            with self.assertRaises(ValueError):
                inventory.consume("coagulant", 6.0)
            with self.assertRaises(ValueError):
                inventory.consume("missing", 1.0)
            self.assertEqual(Lot("x", "y", 3.0, 1.0).remaining(), 2.0)
            validate_quantity(0.0)
            with self.assertRaises(ValueError):
                validate_quantity(-1.0, "level")


class ReportingCase(unittest.TestCase):
    def test_csv_rendering_escapes_fields(self) -> None:
        text = to_csv(["a", "b"], [[1, "plain"], [2, 'has,comma'], [3, 'has"quote']])
        self.assertTrue(text.startswith("a,b\n"))
        self.assertIn('2,"has,comma"', text)
        self.assertIn('3,"has""quote"', text)

    def test_audit_export_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            auditor = Auditor(Store.open(f"{tmp}/state.json"))
            auditor.record("coagulant", "2.0000")
            columns, rows = audit_export(auditor)
            self.assertEqual(columns, ["time", "kind", "detail", "id"])
            self.assertEqual(rows[0][1], "coagulant")
            self.assertEqual(rows[0][2], "2.0000")


if __name__ == "__main__":
    unittest.main()
