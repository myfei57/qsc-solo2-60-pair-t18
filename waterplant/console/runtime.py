"""Wiring of every control component behind the console."""

from __future__ import annotations

from waterplant.audit import Auditor
from waterplant.backwash import Controller, default_field
from waterplant.backwash.clock import ManualClock
from waterplant.chlor import Doser as ChlorDoser
from waterplant.clearwell import Well
from waterplant.coag import Doser as CoagDoser
from waterplant.filter import Bank
from waterplant.flow import Calibration
from waterplant.intake import FlowRepository, InletController, Trend
from waterplant.inventory import Inventory
from waterplant.ph import Stabilizer
from waterplant.quota import Accumulator
from waterplant.scheduler import Scheduler
from waterplant.store import Store
from waterplant.turb import Sampler


class Runtime:
    """Owns one instance of each control component for a single store."""

    def __init__(self, store: Store) -> None:
        bank = Bank()
        coag_doser = CoagDoser(store)
        self.store = store
        self.flow_repository = FlowRepository(store)
        self.inlet = InletController()
        self.outlet = InletController()
        self.coag_doser = coag_doser
        self.chlor_doser = ChlorDoser(store)
        self.bank = bank
        # The console runs against a manually advanced simulation clock so the
        # fixed wash/rinse hold durations can be exercised without real waits.
        # Production wiring injects a SystemClock instead.
        self.clock = ManualClock()
        self.field = default_field()
        self.backwash = Controller(bank, store, field=self.field, clock=self.clock)
        self.sampler = Sampler(coag_doser)
        self.calibration = Calibration(store)
        self.well = Well(store)
        self.accumulator = Accumulator(store)
        self.auditor = Auditor(store)
        self.stabilizer = Stabilizer(store)
        self.scheduler = Scheduler(store)
        self.trend = Trend(store)
        self.inventory = Inventory(store)
