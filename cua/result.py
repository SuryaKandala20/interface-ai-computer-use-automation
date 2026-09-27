"""What replay returns to the caller.

A run ends in exactly one of three states:

  succeeded         it worked, and outputs match what the capability declares
  business_outcome  the app gave a real answer that isn't the happy path
                    (MEMBER_NOT_FOUND, PERMISSION_DENIED, ...). Not an error, the
                    caller just checks outcome.code
  failed            it couldn't finish. error says which step, what we expected,
                    what we actually saw, and where the screenshots are

Things we recovered from (notice pages, expired sessions, slow loads) don't end the
run. They're listed in recoveries so you can still see they happened.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class ErrorCode(str, Enum):
    INVALID_INPUT = "INVALID_INPUT"                  # bad inputs from the caller, rejected before touching the UI
    CAPABILITY_NOT_APPROVED = "CAPABILITY_NOT_APPROVED"
    ARTIFACT_INTEGRITY = "ARTIFACT_INTEGRITY"        # file was changed after it was approved
    INCOMPATIBLE_TENANT = "INCOMPATIBLE_TENANT"
    SECRET_UNAVAILABLE = "SECRET_UNAVAILABLE"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    APPROVAL_DENIED = "APPROVAL_DENIED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
    TARGET_AMBIGUOUS = "TARGET_AMBIGUOUS"
    CHECKPOINT_FAILED = "CHECKPOINT_FAILED"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    APP_ERROR = "APP_ERROR"                          # the app crashed (matched a failure rule)
    RECOVERY_EXHAUSTED = "RECOVERY_EXHAUSTED"
    RECOVERY_UNSAFE = "RECOVERY_UNSAFE"              # needed a restart but the flow isn't safe to redo
    NAVIGATION_ERROR = "NAVIGATION_ERROR"
    HUMAN_ABORTED = "HUMAN_ABORTED"
    HUMAN_TIMEOUT = "HUMAN_TIMEOUT"
    INTERNAL = "INTERNAL"


class Outcome(BaseModel):
    code: str
    message: str
    step_id: Optional[str] = None


class RunError(BaseModel):
    code: str
    message: str
    step_id: Optional[str] = None
    step_intent: Optional[str] = None
    expected: Optional[str] = None
    observed: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, str] = Field(default_factory=dict)


class StepRecord(BaseModel):
    id: str
    status: Literal["done", "done_by_human", "skipped"]
    attempts: int = 1
    duration_ms: int = 0
    locator_strategy: Optional[int] = None


class RunResult(BaseModel):
    run_id: str
    capability: str
    tenant: str
    status: Literal["succeeded", "business_outcome", "failed"]
    outputs: dict[str, Any] = Field(default_factory=dict)
    outcome: Optional[Outcome] = None
    error: Optional[RunError] = None
    recoveries: list[dict[str, Any]] = Field(default_factory=list)
    interventions: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    steps: list[StepRecord] = Field(default_factory=list)
    duration_ms: int = 0
    evidence_dir: Optional[str] = None
