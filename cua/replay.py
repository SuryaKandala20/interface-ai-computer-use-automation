"""Replays a capability step by step. No LLM involved.

For each step we:
  1. check that automation still holds the session
  2. wait for the target (observe, check outcome rules, resolve the locator)
  3. run the policy check (allowlist, action type, irreversible needs approval)
  4. do the action (fills are read back, extracted values are parsed)
  5. wait for the step's checkpoint

Outcome rules are checked on every poll, before the checkpoint. That way a
"NO RECORDS FOUND" page comes back as a business outcome straight away instead of
timing out. Anything we don't recognise that blocks progress is a hard failure, or
goes to a human if on_failure="escalate".
"""

from __future__ import annotations

import re
import sys
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Optional

from .control import ControlError, HandoffBroker
from .evidence import RunRecorder
from .locator import describe_condition, describe_target, holds, resolve
from .policy import Policy
from .redact import Redactor
from .result import ErrorCode, Outcome, RunError, RunResult, StepRecord
from .schema import AppProfile, Capability, OutcomeRule, ParamType, Step, TenantBinding
from .surface.base import Control, Observation, Surface
from .tenancy import CompatibilityError, make_renderer, resolve_secrets, specialize


class Business(Exception):
    def __init__(self, rule: OutcomeRule, step_id: str):
        super().__init__(rule.code)
        self.rule, self.step_id = rule, step_id


class Recover(Exception):
    def __init__(self, rule: OutcomeRule, phase: str):
        super().__init__(rule.code)
        self.rule, self.phase = rule, phase


class HardFail(Exception):
    def __init__(self, code: str, message: str, *, step: Optional[Step] = None,
                 expected: Optional[str] = None, observed: Optional[dict] = None):
        super().__init__(message)
        self.code, self.message, self.step = code, message, step
        self.expected, self.observed = expected, observed or {}
        self.evidence: dict[str, str] = {}


# No point asking a human about these, clicking around in the UI won't fix them.
NOT_ESCALATABLE = {
    ErrorCode.INVALID_INPUT.value, ErrorCode.CAPABILITY_NOT_APPROVED.value,
    ErrorCode.ARTIFACT_INTEGRITY.value, ErrorCode.INCOMPATIBLE_TENANT.value,
    ErrorCode.SECRET_UNAVAILABLE.value, ErrorCode.POLICY_BLOCKED.value,
    ErrorCode.APPROVAL_DENIED.value, ErrorCode.HUMAN_ABORTED.value, ErrorCode.HUMAN_TIMEOUT.value,
}


def parse_value(raw: str, kind: str) -> Any:
    s = raw.strip()
    if kind == "text":
        return s
    neg = s.startswith("(") and s.endswith(")") or s.startswith("-")
    digits = re.sub(r"[^\d.]", "", s)
    try:
        d = Decimal(digits)
    except InvalidOperation:
        raise ValueError(f"cannot parse {kind} from '{raw}'")
    d = -d if neg else d
    return str(d) if kind == "decimal" else int(d)


class ReplayEngine:
    def __init__(
        self,
        capability: Capability,
        *,
        tenant: TenantBinding,
        profile: AppProfile,
        policy: Policy,
        surface: Surface,
        recorder: RunRecorder,
        broker: HandoffBroker,
        redactor: Redactor,
        on_failure: Literal["fail", "escalate"] = "fail",
        approve_irreversible: bool = False,
        allow_draft: bool = False,
        operator_timeout_s: float = 900,
    ) -> None:
        self.base = capability
        self.cap = capability
        self.tenant, self.profile, self.policy = tenant, profile, policy
        self.surface, self.rec, self.broker, self.redactor = surface, recorder, broker, redactor
        self.on_failure, self.approve_irreversible, self.allow_draft = on_failure, approve_irreversible, allow_draft
        self.operator_timeout_s = operator_timeout_s
        self.outputs: dict[str, Any] = {}
        self.recoveries: list[dict[str, Any]] = []
        self.interventions: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.steps: dict[str, StepRecord] = {}
        self._active_iv: Optional[str] = None
        self._recover_counts: dict[str, int] = {}
        self.render = lambda s: s

    # --- public ---
    def run(self, inputs: dict[str, Any]) -> RunResult:
        t0 = time.monotonic()
        status, outcome, error = "failed", None, None
        self.rec.event("run_started", capability=self.base.ref, tenant=self.tenant.tenant_id,
                       inputs=self._loggable_inputs(inputs), on_failure=self.on_failure)
        try:
            self._preflight(inputs)
            self.broker.start(self.rec.run_id)
            self.surface.set_recorder(self._on_human_action)
            self._execute()
            self._verify_success()
            status = "succeeded"
        except Business as b:
            status = "business_outcome"
            outcome = Outcome(code=b.rule.code, message=b.rule.message, step_id=b.step_id)
            self.rec.capture(self.surface, f"outcome_{b.rule.code}", dom=False)
        except HardFail as f:
            error = self._error(f)
        except Exception as e:  # callers always get a RunResult back, never a stack trace
            f = HardFail(ErrorCode.INTERNAL.value, f"{type(e).__name__}: {e}")
            f.evidence = self.rec.capture(self.surface, "internal_error")
            error = self._error(f)
        finally:
            self.surface.set_recorder(None)
        result = RunResult(
            run_id=self.rec.run_id, capability=self.base.ref, tenant=self.tenant.tenant_id, status=status,
            outputs=self.outputs if status == "succeeded" else {}, outcome=outcome, error=error,
            recoveries=self.recoveries, interventions=self.interventions, warnings=self.warnings,
            steps=list(self.steps.values()), duration_ms=int((time.monotonic() - t0) * 1000),
            evidence_dir=str(self.rec.dir),
        )
        self.rec.event("run_finished", status=status, outcome=outcome.code if outcome else None,
                       error=error.code if error else None, duration_ms=result.duration_ms)
        self.rec.write_json("result.json", self._persistable(result))
        return result

    # --- preflight (no UI) ---
    def _preflight(self, inputs: dict[str, Any]) -> None:
        cap = self.base
        if cap.fingerprint and cap.fingerprint != cap.compute_fingerprint():
            raise HardFail(ErrorCode.ARTIFACT_INTEGRITY.value,
                           "artifact content differs from the fingerprint it was approved with")
        if cap.approval.status != "approved":
            if not self.allow_draft:
                raise HardFail(ErrorCode.CAPABILITY_NOT_APPROVED.value,
                               f"{cap.ref} is '{cap.approval.status}'; unattended replay requires 'approved'")
            if cap.side_effects == "irreversible":
                raise HardFail(ErrorCode.CAPABILITY_NOT_APPROVED.value,
                               "draft capabilities with irreversible side effects cannot run")
            self.warnings.append("running a draft capability (--allow-draft)")
        try:
            self.cap, applied = specialize(cap, self.tenant)
        except CompatibilityError as e:
            raise HardFail(ErrorCode.INCOMPATIBLE_TENANT.value, str(e))
        if applied:
            self.rec.event("tenant_overrides_applied", overrides=applied)
        self._validate_inputs(inputs)
        try:
            secrets = resolve_secrets(self.tenant, [s.name for s in self.cap.secrets])
        except KeyError as e:
            raise HardFail(ErrorCode.SECRET_UNAVAILABLE.value, str(e))
        for k, v in secrets.items():
            self.redactor.register_secret(k, v)
        for p in self.cap.inputs:
            if p.sensitivity in ("pii", "secret") and p.name in inputs:
                self.redactor.register_value(str(inputs[p.name]), f"input:{p.name}")
        self.rules = [*self.cap.outcomes, *self.profile.outcomes]
        self.render = make_renderer(inputs, secrets, self.tenant)

    def _validate_inputs(self, inputs: dict[str, Any]) -> None:
        specs = {p.name: p for p in self.cap.inputs}
        problems = [f"unknown input '{k}'" for k in inputs if k not in specs]
        for name, p in specs.items():
            if name not in inputs:
                if p.required:
                    problems.append(f"missing required input '{name}'")
                continue
            v = str(inputs[name])
            if p.type == ParamType.integer.value and not re.fullmatch(r"-?\d+", v):
                problems.append(f"'{name}' must be an integer")
            if p.type == ParamType.decimal.value and not re.fullmatch(r"-?\d+(\.\d+)?", v):
                problems.append(f"'{name}' must be a decimal")
            if p.enum and v not in p.enum:
                problems.append(f"'{name}' must be one of {p.enum}")
            if p.pattern and not re.fullmatch(p.pattern, v):
                problems.append(f"'{name}' does not match {p.pattern}")
        if problems:
            raise HardFail(ErrorCode.INVALID_INPUT.value, "; ".join(problems))

    # --- main loop ---
    def _execute(self) -> None:
        steps = self.cap.steps
        index = {s.id: i for i, s in enumerate(steps)}
        i, verify_only, by_human = 0, False, False
        while i < len(steps):
            step = steps[i]
            started = time.monotonic()
            try:
                strategy = self._run_step(step, verify_only)
                rec = self.steps.get(step.id)
                self.steps[step.id] = StepRecord(
                    id=step.id, status="done_by_human" if by_human else "done",
                    attempts=(rec.attempts + 1) if rec else 1,
                    duration_ms=int((time.monotonic() - started) * 1000), locator_strategy=strategy)
                i, verify_only, by_human = i + 1, False, False
            except Recover as r:
                rule = r.rule
                n = self._recover_counts[rule.id] = self._recover_counts.get(rule.id, 0) + 1
                assert rule.recovery is not None
                if n > rule.recovery.max_attempts:
                    raise HardFail(ErrorCode.RECOVERY_EXHAUSTED.value,
                                   f"'{rule.code}' persisted after {rule.recovery.max_attempts} recoveries",
                                   step=step, expected="condition cleared by recovery",
                                   observed={"condition": describe_condition(rule.when)})
                entry = {"code": rule.code, "step_id": step.id, "phase": r.phase,
                         "action": rule.recovery.action, "attempt": n}
                self.recoveries.append(entry)
                self.rec.event("recovery", **entry, message=rule.message)
                if rule.recovery.action == "dismiss":
                    self._dismiss(rule, step)
                    verify_only = r.phase == "post"
                elif rule.recovery.action == "restart":
                    if not self.cap.idempotent:
                        raise HardFail(ErrorCode.RECOVERY_UNSAFE.value,
                                       f"'{rule.code}' needs a restart but {self.cap.ref} is not idempotent",
                                       step=step, expected="idempotent flow")
                    i, verify_only = index.get(rule.recovery.from_step or "", 0), False
                else:  # wait
                    self.surface.pump(1500)
                    verify_only = r.phase == "post"
            except HardFail as f:
                if f.step is None:
                    f.step = step
                if not f.evidence:
                    f.evidence = self.rec.capture(self.surface, f"fail_{step.id}")
                self.rec.event("step_failed", step_id=step.id, code=f.code, message=f.message,
                               expected=f.expected, observed=f.observed)
                if self.on_failure != "escalate" or f.code in NOT_ESCALATABLE:
                    raise
                intv = self._escalate("takeover", step, f.code, f.message, f.observed, f.evidence)
                mode = intv.resolution.mode if intv.resolution else None
                if intv.status == "expired":
                    raise HardFail(ErrorCode.HUMAN_TIMEOUT.value, "no operator resolved the intervention",
                                   step=step)
                if mode == "abort":
                    raise HardFail(ErrorCode.HUMAN_ABORTED.value,
                                   f"operator aborted after: {f.message}", step=step,
                                   expected=f.expected, observed=f.observed)
                verify_only, by_human = (mode == "step_done"), (mode == "step_done")

    def _run_step(self, step: Step, verify_only: bool) -> Optional[int]:
        strategy = None
        if not verify_only:
            try:
                self.broker.assert_automation(self.rec.run_id)
            except ControlError as e:
                raise HardFail(ErrorCode.INTERNAL.value, str(e), step=step)
            self.rec.event("step_started", step_id=step.id, action=step.action, intent=step.intent)
            if step.action == "navigate":
                url = self.render(step.url or "")
                d = self.policy.check_action("navigate", url=url)
                if not d.allowed:
                    raise HardFail(ErrorCode.POLICY_BLOCKED.value, d.reason, step=step)
                try:
                    self.surface.navigate(url)
                except Exception as e:
                    raise HardFail(ErrorCode.NAVIGATION_ERROR.value, str(e)[:300], step=step,
                                   expected=f"page at {step.url} loads")
            else:
                ctrl, strategy = self._await_target(step)
                d = self.policy.check_action(step.action, control=ctrl, declared_risk=step.risk,
                                             extra_patterns=self.profile.irreversible_labels)
                if not d.allowed:
                    raise HardFail(ErrorCode.POLICY_BLOCKED.value, d.reason, step=step)
                if d.needs_approval:
                    self._approve(step, ctrl)
                self._act(step, ctrl)
            self.surface.settle()
        if step.expect is not None:
            self._await(step, step.expect)
        else:
            self._check_rules(step, self._observe(), "post")
        return strategy

    # --- primitives ---
    def _observe(self) -> Observation:
        obs = self.surface.observe()
        self.redactor.learn(obs)
        return obs

    def _check_rules(self, step: Step, obs: Observation, phase: str) -> None:
        for rule in self.rules:
            if rule.applies_to and step.id not in rule.applies_to:
                continue
            if not holds(rule.when, obs, self.render):
                continue
            self.rec.event("condition_detected", step_id=step.id, phase=phase, rule=rule.id,
                           kind=rule.kind, code=rule.code)
            if rule.kind == "business":
                raise Business(rule, step.id)
            if rule.kind == "recoverable":
                raise Recover(rule, phase)
            raise HardFail(rule.code, rule.message, step=step, expected="no application error",
                           observed=self._observed(obs, step))

    def _await_target(self, step: Step) -> tuple[Control, int]:
        assert step.target is not None
        deadline = time.monotonic() + step.timeout_ms / 1000
        while True:
            obs = self._observe()
            self._check_rules(step, obs, "pre")
            res = resolve(step.target, obs, self.render)
            if res.control is not None:
                if res.used_fallback:
                    msg = (f"step {step.id}: primary locator failed, used fallback #{res.strategy} "
                           f"({describe_target(step.target.fallbacks[res.strategy - 1])}) - possible UI drift")
                    self.warnings.append(msg)
                    self.rec.event("locator_fallback_used", step_id=step.id, strategy=res.strategy, counts=res.counts)
                return res.control, res.strategy
            if time.monotonic() > deadline:
                code = ErrorCode.TARGET_AMBIGUOUS if res.ambiguous else ErrorCode.TARGET_NOT_FOUND
                raise HardFail(code.value, f"could not uniquely locate target for '{step.intent}'",
                               step=step, expected=describe_target(step.target.primary),
                               observed={**self._observed(obs, step), "match_counts": list(res.counts)})
            self.surface.pump(300)

    def _await(self, step: Step, cond) -> Observation:
        deadline = time.monotonic() + step.timeout_ms / 1000
        waited = False
        while True:
            obs = self._observe()
            self._check_rules(step, obs, "post")
            if holds(cond, obs, self.render):
                if waited:
                    self.rec.event("checkpoint_passed_after_wait", step_id=step.id)
                return obs
            if time.monotonic() > deadline:
                raise HardFail(ErrorCode.CHECKPOINT_FAILED.value,
                               f"checkpoint not reached after '{step.intent}'", step=step,
                               expected=describe_condition(cond), observed=self._observed(obs, step))
            waited = True
            self.surface.pump(300)

    def _act(self, step: Step, ctrl: Control) -> None:
        shown = None
        if step.action == "click":
            self.surface.click(ctrl)
        elif step.action in ("fill", "select"):
            value = self.render(step.value or "")
            shown = "[SECRET]" if "{{secrets." in (step.value or "") else value
            if step.action == "fill":
                self.surface.fill(ctrl, value)
                if ctrl.role != "password" and self.surface.read(ctrl) != value:
                    raise HardFail(ErrorCode.CHECKPOINT_FAILED.value, "field did not accept the value",
                                   step=step, expected=f"field value == {shown}")
            else:
                self.surface.select(ctrl, value)
        elif step.action == "extract":
            assert step.extract is not None
            raw = self.surface.read(ctrl)
            try:
                self.outputs[step.extract.output] = parse_value(raw, step.extract.parse)
            except ValueError as e:
                raise HardFail(ErrorCode.EXTRACTION_FAILED.value, str(e), step=step,
                               expected=f"{step.extract.parse} value", observed={"raw": raw[:80]})
            spec = next(o for o in self.cap.outputs if o.name == step.extract.output)
            if spec.sensitivity in ("pii", "secret"):
                self.redactor.register_value(raw, f"output:{spec.name}")
        self.rec.event("step_action", step_id=step.id, action=step.action,
                       target=describe_target(step.target.primary) if step.target else None,
                       control={"role": ctrl.role, "name": ctrl.name, "frame": ctrl.frame}, value=shown)

    def _dismiss(self, rule: OutcomeRule, step: Step) -> None:
        assert rule.recovery and rule.recovery.target
        from .schema import Locator

        obs = self._observe()
        res = resolve(Locator(primary=rule.recovery.target), obs, self.render)
        if res.control is None:
            raise HardFail(ErrorCode.RECOVERY_EXHAUSTED.value, f"cannot find control to dismiss '{rule.code}'",
                           step=step, expected=describe_target(rule.recovery.target),
                           observed=self._observed(obs, step))
        self.broker.assert_automation(self.rec.run_id)
        self.surface.click(res.control)
        self.surface.settle()

    def _verify_success(self) -> None:
        pseudo = Step(id="final_checkpoint", intent="verify capability success condition", action="navigate",
                      url="-", timeout_ms=5000)
        self._await(pseudo, self.cap.success)
        missing = [o.name for o in self.cap.outputs if o.name not in self.outputs]
        if missing:
            raise HardFail(ErrorCode.EXTRACTION_FAILED.value, f"outputs not produced: {missing}")
        self.rec.event("success_verified", condition=describe_condition(self.cap.success),
                       outputs=self._loggable_outputs())

    # --- human in the loop ---
    def _approve(self, step: Step, ctrl: Control) -> None:
        if self.approve_irreversible:
            self.rec.event("approval_preauthorized", step_id=step.id, control=ctrl.name)
            return
        if self.on_failure != "escalate":
            raise HardFail(ErrorCode.APPROVAL_REQUIRED.value,
                           f"'{ctrl.name}' is irreversible; re-run with approval or escalation", step=step)
        obs = self._observe()
        ev = self.rec.capture(self.surface, f"approval_{step.id}", dom=False)
        intv = self._escalate("approval", step, "IRREVERSIBLE_ACTION",
                              f"About to press '{ctrl.name}' ({step.intent}). Approve?",
                              self._observed(obs, step), ev)
        if intv.status == "expired":
            raise HardFail(ErrorCode.HUMAN_TIMEOUT.value, "approval not given in time", step=step)
        if not intv.resolution or intv.resolution.mode != "approve":
            raise HardFail(ErrorCode.APPROVAL_DENIED.value, f"operator denied '{ctrl.name}'", step=step)

    def _escalate(self, kind: str, step: Step, code: str, reason: str,
                  observed: dict[str, Any], evidence: dict[str, str]):
        intv = self.broker.open(
            run_id=self.rec.run_id, kind=kind, subject=self.cap.ref, step_id=step.id,
            step_intent=step.intent, reason_code=code, reason=reason,
            observed=self.redactor.obj(observed), evidence=evidence, live_endpoint=self.surface.live_endpoint,
        )
        self.rec.event("intervention_opened", intervention=intv.id, kind=kind, step_id=step.id, reason_code=code)
        print(f"\n[escalation] {kind} requested ({code}) - intervention {intv.id}. "
              f"Open the operator console (python -m cua operator) to take over.", file=sys.stderr, flush=True)
        self._active_iv = intv.id
        seen = {"claimed": False}

        def tick(i):
            if i.status == "claimed" and not seen["claimed"]:
                seen["claimed"] = True
                self.rec.event("control_transferred", to=f"human:{i.claimed_by}", intervention=i.id)

        try:
            final = self.broker.wait(intv.id, self.surface.pump, self.operator_timeout_s, tick)
        finally:
            self._active_iv = None
        res = final.resolution
        self.rec.event("control_returned", intervention=final.id, status=final.status,
                       resolution=res.mode if res else None, operator=res.operator if res else None,
                       note=res.note if res else None, human_actions=len(final.human_actions))
        if final.human_actions:
            self.rec.capture(self.surface, f"after_human_{step.id}", dom=False)
        self.interventions.append({
            "id": final.id, "kind": kind, "reason_code": code, "step_id": step.id, "status": final.status,
            "resolution": res.mode if res else None, "operator": res.operator if res else None,
            "human_actions": final.human_actions,
        })
        return final

    def _on_human_action(self, frame: str, payload: dict[str, Any]) -> None:
        if not self._active_iv:
            return  # the recorder sees our own clicks too, we only want the human's
        lease = self.broker.lease(self.rec.run_id)
        action = self.redactor.obj({
            "source": "recorder", "type": payload.get("type"), "frame": frame, "role": payload.get("role"),
            "name": payload.get("name"), "value": payload.get("value"), "row": payload.get("row"),
            "column": payload.get("column"), "native_id": payload.get("native_id"),
            "while_holder": lease.holder,
        })
        self.broker.record_human_action(self._active_iv, action)
        self.rec.event("human_action", intervention=self._active_iv, **action)

    # --- reporting helpers ---
    def _observed(self, obs: Observation, step: Optional[Step]) -> dict[str, Any]:
        frames = [f for f in obs.frames if f.text or f.headings]
        main = max(frames, key=lambda f: len(f.text), default=None)
        similar = []
        if step and step.target:
            similar = sorted({c.name for c in obs.controls if c.role == step.target.primary.role})[:12]
        return self.redactor.obj({
            "frames": {f.name or "top": {"path": re.sub(r"^[a-z]+://[^/]+", "", f.url), "headings": f.headings}
                       for f in obs.frames},
            "text_excerpt": main.text[:400] if main else "",
            "similar_controls": similar,
        })

    def _error(self, f: HardFail) -> RunError:
        return RunError(code=f.code, message=self.redactor.text(f.message),
                        step_id=f.step.id if f.step else None, step_intent=f.step.intent if f.step else None,
                        expected=f.expected, observed=self.redactor.obj(f.observed), evidence=f.evidence)

    def _loggable_inputs(self, inputs: dict[str, Any]) -> dict[str, Any]:
        sens = {p.name: p.sensitivity for p in self.base.inputs}
        return {k: ("[REDACTED]" if sens.get(k) in ("pii", "secret") else v) for k, v in inputs.items()}

    def _loggable_outputs(self) -> dict[str, Any]:
        sens = {o.name: o.sensitivity for o in self.cap.outputs}
        return {k: ("[REDACTED]" if sens.get(k) in ("pii", "secret") else v) for k, v in self.outputs.items()}

    def _persistable(self, r: RunResult) -> dict[str, Any]:
        d = r.model_dump(mode="json")
        d["outputs"] = self._loggable_outputs() if r.status == "succeeded" else {}
        return d
