"""Wiring of every control component behind the console."""

from __future__ import annotations

from typing import Callable

from waterplant.audit import Auditor
from waterplant.backwash import Controller
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

    def __init__(
        self,
        store: Store,
        field: object | None = None,
        clock: Callable[[], float] | None = None,
        valve_timeout: float | None = None,
        pump_timeout: float | None = None,
        flush_seconds: float | None = None,
    ) -> None:
        bank = Bank()
        coag_doser = CoagDoser(store)
        self.store = store
        self.flow_repository = FlowRepository(store)
        self.inlet = InletController()
        self.outlet = InletController()
        self.coag_doser = coag_doser
        self.chlor_doser = ChlorDoser(store)
        self.bank = bank
        self.well = Well(store)
        kwargs: dict[str, object] = {"well": self.well}
        if field is not None:
            kwargs["field"] = field
        if clock is not None:
            kwargs["clock"] = clock
        if valve_timeout is not None:
            kwargs["valve_timeout"] = valve_timeout
        if pump_timeout is not None:
            kwargs["pump_timeout"] = pump_timeout
        if flush_seconds is not None:
            kwargs["flush_seconds"] = flush_seconds
        self.backwash = Controller(bank, store, **kwargs)
        self.sampler = Sampler(coag_doser)
        self.calibration = Calibration(store)
        self.accumulator = Accumulator(store)
        self.auditor = Auditor(store)
        self.stabilizer = Stabilizer(store)
        self.scheduler = Scheduler(store)
        self.trend = Trend(store)
        self.inventory = Inventory(store)
