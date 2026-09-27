"""Passing control between automation and a human.

Each live session has one controller at a time, kept in a lease file:

    automation -> awaiting_human -> human -> automation
       (we escalate)   (operator claims)  (operator resolves)

- The executor checks the lease before every action, so it can't click anything
  while a human has the session.
- An intervention holds what the operator needs: capability or goal, step, reason,
  a redacted bit of the screen, a masked screenshot, and the CDP endpoint so they
  attach to the same browser instead of opening a new one.
- While the human has control, the in-page recorder logs their clicks and edits
  using the same role/name terms as the recorded steps.

It all goes through JSON files (written atomically). That's enough for one machine
and keeps the executor and the console as separate processes. In production this
would be a queue plus a lease row in a database, with the same states.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, Field

Kind = Literal["takeover", "approval"]
Status = Literal["open", "claimed", "resolved", "expired"]

RESOLUTIONS: dict[str, list[str]] = {
    # takeover: the human fixes things, then says how automation should carry on
    "takeover": ["retry_step", "step_done", "abort"],
    # approval: a yes/no on a risky action
    "approval": ["approve", "deny"],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Resolution(BaseModel):
    mode: str
    operator: str
    note: str = ""
    at: str = Field(default_factory=_now)


class Intervention(BaseModel):
    id: str
    run_id: str
    kind: Kind
    status: Status = "open"
    created_at: str = Field(default_factory=_now)
    subject: str = Field(description="Capability ref or discovery goal")
    step_id: Optional[str] = None
    step_intent: Optional[str] = None
    reason_code: str
    reason: str
    observed: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, str] = Field(default_factory=dict)
    live_endpoint: Optional[str] = None
    allowed_resolutions: list[str] = Field(default_factory=list)
    claimed_by: Optional[str] = None
    claimed_at: Optional[str] = None
    human_actions: list[dict[str, Any]] = Field(default_factory=list)
    resolution: Optional[Resolution] = None


class ControlLease(BaseModel):
    run_id: str
    holder: Literal["automation", "awaiting_human", "human"] = "automation"
    operator: Optional[str] = None
    intervention_id: Optional[str] = None
    version: int = 0
    since: str = Field(default_factory=_now)


class ControlError(RuntimeError):
    pass


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{secrets.token_hex(3)}.tmp")
    tmp.write_text(text, encoding="utf-8")
    for _ in range(20):  # on Windows the reader can hold the file open for a moment
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)
    os.replace(tmp, path)


def _read(path: Path) -> Optional[str]:
    for _ in range(20):
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except PermissionError:
            time.sleep(0.05)
    return None


class HandoffBroker:
    def __init__(self, root: str | Path = "runs/_control") -> None:
        self.root = Path(root)
        (self.root / "interventions").mkdir(parents=True, exist_ok=True)
        (self.root / "leases").mkdir(parents=True, exist_ok=True)

    # --- lease ---
    def _lease_path(self, run_id: str) -> Path:
        return self.root / "leases" / f"{run_id}.json"

    def lease(self, run_id: str) -> ControlLease:
        raw = _read(self._lease_path(run_id))
        return ControlLease.model_validate_json(raw) if raw else ControlLease(run_id=run_id)

    def _set_lease(self, run_id: str, **changes: Any) -> ControlLease:
        cur = self.lease(run_id)
        new = cur.model_copy(update={**changes, "version": cur.version + 1, "since": _now()})
        _atomic_write(self._lease_path(run_id), new.model_dump_json(indent=2))
        return new

    def start(self, run_id: str) -> None:
        self._set_lease(run_id, holder="automation", operator=None, intervention_id=None)

    def assert_automation(self, run_id: str) -> None:
        lease = self.lease(run_id)
        if lease.holder != "automation":
            raise ControlError(f"automation does not hold the session (holder={lease.holder})")

    # --- interventions ---
    def _ipath(self, iid: str) -> Path:
        return self.root / "interventions" / f"{iid}.json"

    def get(self, iid: str) -> Intervention:
        raw = _read(self._ipath(iid))
        if raw is None:
            raise KeyError(iid)
        return Intervention.model_validate_json(raw)

    def _save(self, intv: Intervention) -> None:
        _atomic_write(self._ipath(intv.id), intv.model_dump_json(indent=2))

    def list(self, status: Optional[set[str]] = None) -> list[Intervention]:
        out = []
        for p in sorted((self.root / "interventions").glob("*.json")):
            raw = _read(p)
            if raw:
                i = Intervention.model_validate_json(raw)
                if status is None or i.status in status:
                    out.append(i)
        return out

    def open(self, **fields: Any) -> Intervention:
        iid = f"iv-{datetime.now().strftime('%H%M%S')}-{secrets.token_hex(2)}"
        intv = Intervention(id=iid, allowed_resolutions=RESOLUTIONS[fields["kind"]], **fields)
        self._save(intv)
        self._set_lease(intv.run_id, holder="awaiting_human", intervention_id=iid)
        return intv

    def claim(self, iid: str, operator: str) -> Intervention:
        intv = self.get(iid)
        if intv.status != "open":
            raise ControlError(f"intervention {iid} is {intv.status}")
        intv = intv.model_copy(update={"status": "claimed", "claimed_by": operator, "claimed_at": _now()})
        self._save(intv)
        self._set_lease(intv.run_id, holder="human", operator=operator, intervention_id=iid)
        return intv

    def record_human_action(self, iid: str, action: dict[str, Any]) -> None:
        intv = self.get(iid)
        intv.human_actions.append({"at": _now(), **action})
        self._save(intv)

    def resolve(self, iid: str, mode: str, operator: str, note: str = "") -> Intervention:
        intv = self.get(iid)
        if intv.status not in ("open", "claimed"):
            raise ControlError(f"intervention {iid} is already {intv.status}")
        if mode not in intv.allowed_resolutions:
            raise ControlError(f"'{mode}' not allowed; expected one of {intv.allowed_resolutions}")
        if intv.status == "claimed" and intv.claimed_by != operator:
            raise ControlError(f"held by {intv.claimed_by}; only they can hand control back")
        intv = intv.model_copy(update={"status": "resolved", "resolution": Resolution(mode=mode, operator=operator, note=note)})
        self._save(intv)
        # control only goes back to automation once the human releases it
        self._set_lease(intv.run_id, holder="automation", operator=None, intervention_id=None)
        return intv

    # --- executor side ---
    def wait(
        self,
        iid: str,
        pump: Callable[[int], None],
        timeout_s: float,
        on_tick: Optional[Callable[[Intervention], None]] = None,
    ) -> Intervention:
        """Block until the operator resolves. Keeps pumping browser events so the
        recorder still picks up what the human does."""
        deadline = time.monotonic() + timeout_s
        while True:
            intv = self.get(iid)
            if on_tick:
                on_tick(intv)
            if intv.status == "resolved":
                return intv
            if time.monotonic() > deadline:
                intv = intv.model_copy(update={"status": "expired"})
                self._save(intv)
                self._set_lease(intv.run_id, holder="automation", operator=None, intervention_id=None)
                return intv
            pump(400)
