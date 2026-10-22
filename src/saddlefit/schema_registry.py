"""Persisted models whose JSON Schemas are generated into ``schemas/``."""

from __future__ import annotations

from pydantic import BaseModel

from saddlefit.config import Config
from saddlefit.doctor import DoctorRecord
from saddlefit.harness.loop_config import LoopConfig
from saddlefit.harness.proposals import Proposal
from saddlefit.harness.store import HarnessStoreInfo
from saddlefit.sandbox.environment import EnvironmentRecord
from saddlefit.storage.library import ChangeRecord
from saddlefit.storage.runs import RunEvent, RunSummary
from saddlefit.tasks.models import SealedTaskInfo, SuiteLock, TaskMeta

# name -> model; the file is written as schemas/<name>.schema.json
SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "saddlefit-config": Config,
    "change-record": ChangeRecord,
    "doctor-record": DoctorRecord,
    "environment-record": EnvironmentRecord,
    "harness-store": HarnessStoreInfo,
    "loop-config": LoopConfig,
    "proposal": Proposal,
    "run-event": RunEvent,
    "run-summary": RunSummary,
    "sealed-task": SealedTaskInfo,
    "suite-lock": SuiteLock,
    "task-meta": TaskMeta,
}
