"""Add-on: learning from human handoffs.

When an operator gets a run unstuck by clicking through a screen the capability
didn't know about, we already have a record of that click (same role/name terms as
the steps). This turns it into a proposed recoverable rule, something like "if this
heading shows up, click that button".

Nothing is applied automatically. Accepting a proposal writes a new version of the
capability that goes back to draft, so someone still has to review and approve it.
Replay, discovery and the schema don't change; a learned rule is just a normal
OutcomeRule.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .control import Intervention
from .policy import Policy
from .redact import Redactor
from .schema import (
    AppProfile, Approval, Capability, OutcomeRule, Recovery, Target, TextVisible, now_utc,
)
from .surface.base import Control

PROPOSALS = Path("proposals")


class LearnError(ValueError):
    pass


class Proposal(BaseModel):
    id: str
    created_at: str
    status: Literal["proposed", "accepted"] = "proposed"
    source_intervention: str
    source_run: str
    capability: str
    capability_fingerprint: Optional[str]
    step_id: str
    operator: Optional[str]
    rationale: str
    rule: OutcomeRule
    accepted_as: Optional[str] = None
    evidence: dict[str, Any] = Field(default_factory=dict)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:40]


def propose(intv: Intervention, cap: Capability, *, policy: Policy, profile: AppProfile) -> Proposal:
    """Build a candidate rule from one resolved takeover. Refuses anything unclear or
    risky instead of guessing."""
    if intv.kind != "takeover":
        raise LearnError("only takeover interventions can be learned from")
    if intv.status != "resolved" or not intv.resolution:
        raise LearnError(f"intervention is {intv.status}; resolve it first")
    if intv.resolution.mode not in ("step_done", "retry_step"):
        raise LearnError(f"operator chose '{intv.resolution.mode}' - nothing to learn from an abort")
    if intv.subject != cap.ref:
        raise LearnError(f"intervention was for {intv.subject}, not {cap.ref}")
    step = next((s for s in cap.steps if s.id == intv.step_id), None)
    if step is None:
        raise LearnError(f"capability has no step '{intv.step_id}'")

    clicks = [a for a in intv.human_actions
              if a.get("source") == "recorder" and a.get("type") == "click" and a.get("role") in ("button", "link")]
    if len(clicks) != 1:
        raise LearnError(f"the operator made {len(clicks)} control clicks; only a single-click fix is learnable "
                         "- author this rule by hand")
    click = clicks[0]
    name, role, frame = click.get("name") or "", click["role"], click.get("frame") or ""
    ctrl = Control(ref="-", role=role, name=name, name_source="", frame=frame)
    if policy.classify(ctrl, profile.irreversible_labels) != "safe":
        raise LearnError(f"'{name}' is classed irreversible - never learned as an automatic dismissal")

    frames = intv.observed.get("frames", {})
    headings = frames.get(frame or "top", {}).get("headings", [])
    expected = getattr(step.expect, "text", None)
    candidates = [h for h in headings if h != expected]
    if not candidates:
        raise LearnError("no distinguishing heading was observed on the blocking screen")
    heading = candidates[0]
    red = Redactor(profile.pii_labels)
    if red.text(heading) != heading or red.text(name) != name:
        raise LearnError("screen text looks like personal data; not learnable")
    known = [r for r in [*cap.outcomes, *profile.outcomes] if getattr(r.when, "text", None) == heading]
    if known:
        raise LearnError(f"a rule for '{heading}' already exists ({known[0].id})")

    code = _slug(heading).upper() or "LEARNED_SCREEN"
    rule = OutcomeRule(
        id=f"learned_{_slug(heading)}", kind="recoverable", code=code,
        when=TextVisible(text=heading, frame=frame or None),
        message=f"Learned from {intv.id}: operator {intv.resolution.operator} cleared '{heading}' "
                f"by clicking {role} '{name}'.",
        applies_to=[step.id],
        recovery=Recovery(action="dismiss", target=Target(role=role, name=name, frame=frame or None), max_attempts=2),
    )
    return Proposal(
        id=f"prop-{intv.id}", created_at=now_utc().isoformat(), source_intervention=intv.id,
        source_run=intv.run_id, capability=cap.ref, capability_fingerprint=cap.fingerprint, step_id=step.id,
        operator=intv.resolution.operator,
        rationale=(f"During step '{step.id}' the run stopped on an unknown screen '{heading}' "
                   f"({intv.reason_code}). The operator resolved it with a single safe click on {role} "
                   f"'{name}' and handed back with '{intv.resolution.mode}'. Proposed: treat that screen "
                   f"as recoverable for this step and dismiss it the same way."),
        rule=rule, evidence=intv.evidence,
    )


def save_proposal(p: Proposal, root: Path = PROPOSALS) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{p.id}.json"
    path.write_text(p.model_dump_json(indent=2), encoding="utf-8")
    return path


def accept(p: Proposal, cap: Capability, *, reviewer: str) -> Capability:
    """New minor version with the learned rule added, set back to draft."""
    if p.capability != cap.ref:
        raise LearnError(f"proposal targets {p.capability}, not {cap.ref}")
    if p.capability_fingerprint and cap.fingerprint and p.capability_fingerprint != cap.fingerprint:
        raise LearnError("capability changed since the proposal was made; re-learn from a fresh intervention")
    major, minor, _ = (int(x) for x in cap.version.split("."))
    data = cap.model_dump(mode="json")
    data["version"] = f"{major}.{minor + 1}.0"
    data["outcomes"] = [*data["outcomes"], p.rule.model_dump(mode="json")]
    data["approval"] = Approval(status="draft", notes=(
        f"Adds learned rule '{p.rule.id}' from {p.source_intervention} (accepted by {reviewer}); "
        f"needs review before unattended use.")).model_dump(mode="json")
    data["fingerprint"] = None
    return Capability.model_validate(data).sealed()
