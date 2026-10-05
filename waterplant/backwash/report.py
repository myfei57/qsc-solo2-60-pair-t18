"""Backwash run state and console projections.

Recovery policy is encoded in the terminal statuses:

* ``HELD``        -- a device watchdog expired. This is a *partial failure*: the
  plant is frozen on the current step, nothing later ever opened. After the
  field is repaired the operator resumes **from the same step** (the step is
  retried, never skipped), and a resume is idempotent.
* ``TRIPPED``     -- a process safety trip, e.g. the clearwell fell below its
  safe level while the drain was open. The feedback-gated safe-shutdown
  sequence ran (movers off, supplies closed, drain closed, bed back in
  service). Resuming a tripped run is refused: a fresh run starts again from
  step zero, because a process hazard invalidates any mid-point continuation.
* ``ABORT_HELD``  -- the safe-shutdown sequence itself could not complete
  (another stuck device). The plant is frozen mid-recovery and demands manual
  intervention; it is never auto-advanced and only the shutdown continues.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

#: No run in progress.
IDLE = "IDLE"
#: Normal sequencing.
RUNNING = "RUNNING"
#: Watchdog expired on a step; frozen on that step, resumable in place.
HELD = "HELD"
#: Safety trip (clearwell low); safe-shutdown completed; restart from step 0.
TRIPPED = "TRIPPED"
#: The safety shutdown sequence itself stalled; manual intervention required.
ABORT_HELD = "ABORT_HELD"
#: Pre-start gate refused (e.g. clearwell already below the safe level).
REJECTED = "REJECTED"
#: Every step reached its feedback; the bed is back in service.
COMPLETE = "COMPLETE"

TERMINAL = (TRIPPED, ABORT_HELD, REJECTED, COMPLETE)
RESUMABLE = (HELD,)
ACTIVE = (RUNNING, HELD)
#: Statuses that still owe a (possibly multi-pulse) safe-shutdown sequence.
SHUTTING_DOWN = (TRIPPED, ABORT_HELD)


@dataclass
class RunState:
    """Persisted progress of one backwash run.

    Every state-changing event carries a monotonic ``version`` and is appended
    to the journal *before* this document is rewritten, so after a crash the
    journal and the run state cannot disagree: the higher-versioned view wins
    and the sequencer reconciles against actual field feedback on the next
    pulse. ``issued_at``/``feedback_at``/``hold_since`` are monotonic seconds
    measured by the injected clock; they bound waits, they never prove
    completion.
    """

    run_id: str
    bed_id: str
    status: str = RUNNING
    step_index: int = 0
    issued_at: float = 0.0
    feedback_at: float = 0.0
    hold_since: float = 0.0
    started_at: float = 0.0
    updated_at: float = 0.0
    attempt: int = 0
    abort_index: int = 0
    abort_issued_at: float = 0.0
    version: int = 0
    reason: str = ""
    detail: str = ""
    #: Monotonic set of audit event ids already mirrored for this run.
    journal: list[str] = field(default_factory=list)
    history: list[dict[str, object]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "RunState":
        return cls(
            run_id=str(payload.get("run_id", "")),
            bed_id=str(payload.get("bed_id", "")),
            status=str(payload.get("status", RUNNING)),
            step_index=int(payload.get("step_index", 0)),
            issued_at=float(payload.get("issued_at", 0.0)),
            feedback_at=float(payload.get("feedback_at", 0.0)),
            hold_since=float(payload.get("hold_since", 0.0)),
            started_at=float(payload.get("started_at", 0.0)),
            updated_at=float(payload.get("updated_at", 0.0)),
            attempt=int(payload.get("attempt", 0)),
            abort_index=int(payload.get("abort_index", 0)),
            abort_issued_at=float(payload.get("abort_issued_at", 0.0)),
            version=int(payload.get("version", 0)),
            reason=str(payload.get("reason", "")),
            detail=str(payload.get("detail", "")),
            journal=[str(item) for item in payload.get("journal", [])],
            history=[dict(item) for item in payload.get("history", [])],
        )


@dataclass(frozen=True)
class BackwashState:
    """Console projection: active run plus the legacy queue/drain counters."""

    commands: list[str]
    drains: list[str]
    run: dict[str, object] | None
    history: list[dict[str, object]]

    def as_dict(self) -> dict[str, object]:
        return {
            "commands": self.commands,
            "drains": self.drains,
            "run": self.run,
            "history": self.history,
        }
