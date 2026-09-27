"""Discovery run: the LLM works through the goal on the live app.

The model never touches the browser. It picks one tool per turn and points at
controls using the [ref] ids from the latest (redacted) observation. We resolve the
ref, run the policy check, do the action, and write a trajectory entry with the full
description of the control. The compiler works from that trajectory, not from the
chat transcript.

A few safety points:
  - credentials are passed by name and filled in by us, the model never sees them
  - observations are PII-redacted before they go to the model
  - irreversible actions wait for a human to approve; anything off the allowlist is refused
  - if the agent looks stuck (page not changing, bad refs, or it asks for help) we hand
    the session to a human and record what they do
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import anthropic

from .control import HandoffBroker
from .evidence import RunRecorder
from .policy import Policy
from .redact import Redactor
from .schema import AppProfile, TenantBinding
from .surface.base import Control, Observation, Surface

DEFAULT_MODEL = os.environ.get("CUA_MODEL", "")  # model id comes from .env, see .env.example

SYSTEM_PROMPT = """You are the discovery agent of a computer-use automation system used by credit unions.
You operate a live back-office application (often legacy: framesets, table layouts, no ids) through tools.
Your successful run is compiled into a deterministic, replayable capability, so work like a careful operator
recording a procedure:

- One UI action per tool call, and always say why in `reason` (it becomes the step's documentation).
- Refer to controls only by the [ref] ids of the LATEST observation. Refs change after every action.
- Never type credentials. For sign-on fields call `fill` with `secret` set to one of the available secret names;
  the executor injects the real value.
- When a value comes from the task parameters, call `fill`/`select` with `param` (the parameter name), not literal
  text, so the flow can be re-run with other values.
- Use `extract` on the specific cell that holds each value the goal asks for. Set `row_label` to the stable text
  that identifies that row (e.g. the account description), never the value itself.
- Irreversible actions (submit / post / confirm a change) need human approval; the executor pauses and asks.
  Only take them if the goal requires it.
- If the screen is unexpected, you are stuck, or you are unsure an action is safe, call `request_human`.
- When the goal is met and every requested value is extracted, call `finish` with status "success".
  If the application legitimately answers "no" (e.g. record not found, access denied), finish with
  status "business_outcome". Stay inside the application.
- Observations mask personal data as [PII:<field>]. That is expected; do not try to reveal it."""

TOOLS: list[dict[str, Any]] = [
    {
        "name": "click",
        "description": "Click a button or link.",
        "input_schema": {"type": "object", "properties": {
            "ref": {"type": "string"}, "reason": {"type": "string"}}, "required": ["ref", "reason"]},
    },
    {
        "name": "fill",
        "description": "Type into a text/password field. Provide exactly one of param, secret, text.",
        "input_schema": {"type": "object", "properties": {
            "ref": {"type": "string"},
            "param": {"type": "string", "description": "Name of a task parameter to type."},
            "secret": {"type": "string", "description": "Name of an available secret (credentials)."},
            "text": {"type": "string", "description": "Literal text (only for fixed values)."},
            "reason": {"type": "string"}}, "required": ["ref", "reason"]},
    },
    {
        "name": "select",
        "description": "Choose an option in a dropdown. Provide option (visible text) or param.",
        "input_schema": {"type": "object", "properties": {
            "ref": {"type": "string"}, "option": {"type": "string"}, "param": {"type": "string"},
            "reason": {"type": "string"}}, "required": ["ref", "reason"]},
    },
    {
        "name": "extract",
        "description": "Read the value of a cell/field as a named output of the capability.",
        "input_schema": {"type": "object", "properties": {
            "ref": {"type": "string"},
            "output_name": {"type": "string", "description": "snake_case output name"},
            "output_type": {"type": "string", "enum": ["text", "decimal", "integer"]},
            "description": {"type": "string"},
            "row_label": {"type": "string", "description": "Stable text identifying the row (not the value)."},
            "reason": {"type": "string"}},
            "required": ["ref", "output_name", "output_type", "description", "reason"]},
    },
    {
        "name": "request_human",
        "description": "Pause and hand the live session to a human operator. Use when stuck or unsure.",
        "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
    },
    {
        "name": "finish",
        "description": "End the run.",
        "input_schema": {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["success", "business_outcome", "failed"]},
            "summary": {"type": "string"},
            "capability_title": {"type": "string", "description": "Short title for the reusable capability."},
            "capability_description": {"type": "string",
                                       "description": "What it does and when an agent should call it."},
            "parameter_descriptions": {"type": "object", "additionalProperties": {"type": "string"}},
            "outcome_code": {"type": "string", "description": "UPPER_SNAKE code for a business outcome."}},
            "required": ["status", "summary"]},
    },
]


@dataclass
class TrajStep:
    n: int
    source: str                    # agent | human
    action: str                    # navigate | click | fill | select | extract
    reason: str
    control: Optional[Control]
    before: Observation
    after: Optional[Observation] = None
    value_kind: Optional[str] = None   # param | secret | literal
    value_ref: Optional[str] = None
    extract: Optional[dict[str, Any]] = None
    risk: str = "safe"
    url: Optional[str] = None


@dataclass
class DiscoveryResult:
    status: str                    # success | business_outcome | failed
    summary: str
    trajectory: list[TrajStep]
    finish: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)
    entry_url: str = ""
    model: str = DEFAULT_MODEL
    run_id: str = ""
    secrets_used: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)


def path_of(url: str) -> str:
    return re.sub(r"^[a-z]+://[^/]+", "", url)


def render_observation(obs: Observation) -> str:
    """Text version of an observation for the model. Pass it a redacted one."""
    lines = [f"Top-level URL path: {path_of(obs.url)}"]
    for f in obs.frames:
        ctrls = [c for c in obs.controls if c.frame == f.name]
        if not ctrls and not f.text:
            continue
        lines.append(f"\n== frame '{f.name or 'top'}'  path={path_of(f.url)}  title={f.title!r}")
        if f.headings:
            lines.append(f"headings: {f.headings}")
        for c in ctrls:
            if c.role == "cell":
                continue
            s = f"  [{c.ref}] {c.role} {c.name!r}"
            if c.value:
                s += f" value={c.value!r}"
            if c.options:
                s += f" options={c.options}"
            if not c.enabled:
                s += " (disabled)"
            if c.row and len(c.row) > 1:
                s += f"  (table row: {' | '.join(c.row)})"
            lines.append(s)
        rows: dict[tuple[int, int], list[Control]] = {}
        for c in ctrls:
            if c.role == "cell" and c.table is not None and c.row_index is not None:
                rows.setdefault((c.table, c.row_index), []).append(c)
        if rows:
            lines.append("  table cells:")
            for key in sorted(rows):
                cells = sorted(rows[key], key=lambda c: c.col_index or 0)
                hdr = f" (col headers: {[c.column for c in cells]})" if any(c.column for c in cells) else ""
                lines.append(f"    t{key[0]}r{key[1]}: " + " | ".join(f"[{c.ref}] {c.name}" for c in cells) + hdr)
        lines.append(f"  text: {f.text[:700]}")
    return "\n".join(lines)


class DiscoveryAgent:
    def __init__(
        self,
        *,
        goal: str,
        tenant: TenantBinding,
        profile: AppProfile,
        policy: Policy,
        surface: Surface,
        recorder: RunRecorder,
        broker: HandoffBroker,
        redactor: Redactor,
        params: dict[str, str],
        secrets: dict[str, str],
        entry_url: str,
        model: str = DEFAULT_MODEL,
        max_steps: Optional[int] = None,
        operator_timeout_s: float = 900,
        client: Optional[anthropic.Anthropic] = None,
    ) -> None:
        self.goal, self.tenant, self.profile, self.policy = goal, tenant, profile, policy
        self.surface, self.rec, self.broker, self.redactor = surface, recorder, broker, redactor
        self.params, self.secrets, self.entry_url, self.model = params, secrets, entry_url, model
        self.max_steps = max_steps or policy.max_discovery_steps
        self.operator_timeout_s = operator_timeout_s
        self.client = client or anthropic.Anthropic()
        self.use_fallbacks = os.environ.get("CUA_LLM_FALLBACKS", "1") == "1"
        self.traj: list[TrajStep] = []
        self.outputs: dict[str, str] = {}
        self.secrets_used: set[str] = set()
        self._obs: Optional[Observation] = None
        self._active_iv: Optional[str] = None
        self._human_buffer: list[tuple[str, dict[str, Any]]] = []
        self.usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "llm_calls": 0}

    # --- helpers ---
    def _observe(self) -> Observation:
        obs = self.surface.observe()
        self.redactor.learn(obs)
        self._obs = obs
        return obs

    def _view(self, obs: Observation) -> str:
        return render_observation(self.redactor.observation(obs))

    def _control(self, ref: str) -> Control:
        assert self._obs is not None
        for c in self._obs.controls:
            if c.ref == ref:
                return c
        raise LookupError(f"unknown ref '{ref}' - use a ref from the latest observation")

    def _llm(self, messages: list[dict[str, Any]]):
        kwargs: dict[str, Any] = dict(
            model=self.model, max_tokens=16000, system=SYSTEM_PROMPT, tools=TOOLS,
            tool_choice={"type": "auto", "disable_parallel_tool_use": True},
            thinking={"type": "adaptive"}, cache_control={"type": "ephemeral"}, messages=messages,
        )
        self.usage["llm_calls"] += 1
        if self.use_fallbacks:
            try:
                return self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
            except anthropic.BadRequestError as e:
                if "fallback" not in str(e).lower():
                    raise
                self.use_fallbacks = False
                self.rec.event("llm_fallbacks_disabled", error=str(e)[:200])
        return self.client.messages.create(**kwargs)

    # --- main loop ---
    def run(self) -> DiscoveryResult:
        self.rec.event("discovery_started", goal=self.goal, tenant=self.tenant.tenant_id, model=self.model,
                       params={k: v for k, v in self.params.items()}, secrets=list(self.secrets))
        self.broker.start(self.rec.run_id)
        self.surface.set_recorder(self._on_human_action)
        ok, why = self.policy.url_allowed(self.entry_url)
        if not ok:
            return self._done("failed", f"entry url blocked by policy: {why}")
        self.surface.navigate(self.entry_url)
        self.surface.settle()
        obs = self._observe()
        self.rec.capture(self.surface, "discovery_start", dom=False)
        param_lines = "\n".join(f"  - {k} = {v!r}  (use param '{k}')" for k, v in self.params.items()) or "  (none)"
        intro = (
            f"Goal: {self.goal}\n\nApplication: {self.profile.product} {self.tenant.product_version} "
            f"for tenant '{self.tenant.display_name}'. Entry page already loaded.\n"
            f"Task parameters:\n{param_lines}\n"
            f"Available secrets: {', '.join(self.secrets) or '(none)'}\n\n"
            f"Current observation:\n{self._view(obs)}"
        )
        messages: list[dict[str, Any]] = [{"role": "user", "content": intro}]
        started, no_progress, bad_refs, nudges = time.monotonic(), 0, 0, 0

        for turn in range(1, self.max_steps + 1):
            if time.monotonic() - started > self.policy.max_discovery_seconds:
                return self._done("failed", "discovery timed out (policy.max_discovery_seconds)")
            try:
                resp = self._llm(messages)
            except anthropic.APIError as e:
                return self._done("failed", f"LLM API error: {type(e).__name__}: {str(e)[:300]}")
            u = resp.usage
            self.usage["input_tokens"] += u.input_tokens or 0
            self.usage["output_tokens"] += u.output_tokens or 0
            self.usage["cache_read_input_tokens"] += getattr(u, "cache_read_input_tokens", 0) or 0
            messages.append({"role": "assistant", "content": resp.content})
            texts = [b.text for b in resp.content if b.type == "text" and b.text.strip()]
            calls = [b for b in resp.content if b.type == "tool_use"]
            self.rec.event("llm_turn", turn=turn, stop_reason=resp.stop_reason, model=resp.model,
                           text=" ".join(texts)[:600],
                           tool=calls[0].name if calls else None, tool_input=calls[0].input if calls else None,
                           usage={"in": u.input_tokens, "out": u.output_tokens})
            if resp.stop_reason == "refusal":
                return self._done("failed", "model declined the request")
            if not calls:
                nudges += 1
                if nudges > 2:
                    return self._done("failed", "model stopped calling tools")
                messages.append({"role": "user", "content": "Continue by calling exactly one tool."})
                continue
            call = calls[0]
            args = dict(call.input) if isinstance(call.input, dict) else json.loads(call.input)

            if call.name == "finish":
                messages.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": call.id, "content": "Run ended."}]})
                return self._done(args.get("status", "failed"), args.get("summary", ""), finish=args)

            before = self._obs
            assert before is not None
            try:
                result_text = self._execute(call.name, args)
                is_error = False
            except LookupError as e:
                bad_refs += 1
                result_text, is_error = f"ERROR: {e}", True
            except _Denied as e:
                result_text, is_error = f"DENIED: {e}", True
            except _Aborted as e:
                return self._done("failed", f"operator aborted: {e}")
            after = self._observe()
            if call.name in ("click", "navigate") and not is_error and after.fingerprint() == before.fingerprint():
                no_progress += 1
            elif not is_error:
                no_progress = 0
            stuck_reason = None
            if no_progress >= 3:
                stuck_reason = "NO_PROGRESS: three actions without any change on screen"
            elif bad_refs >= 3:
                stuck_reason = "BAD_REFS: the agent repeatedly referenced controls that do not exist"
            if stuck_reason:
                try:
                    result_text += "\n" + self._handoff(stuck_reason.split(":")[0], stuck_reason)
                except _Aborted as e:
                    return self._done("failed", f"operator aborted: {e}")
                no_progress = bad_refs = 0
                after = self._observe()
            content = f"{result_text}\n\nNew observation:\n{self._view(after)}"
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": call.id, "content": content, "is_error": is_error}]})
        return self._done("failed", f"step budget exhausted ({self.max_steps})")

    # --- tool execution ---
    def _execute(self, name: str, a: dict[str, Any]) -> str:
        reason = str(a.get("reason", ""))[:300]
        self.rec.event("agent_action_requested", tool=name, args=a)
        if name == "request_human":
            return self._handoff("AGENT_REQUESTED", reason)
        if name == "navigate":
            url = a.get("url", "")
            d = self.policy.check_action("navigate", url=url)
            if not d.allowed:
                self.rec.event("policy_blocked", tool=name, reason=d.reason)
                raise _Denied(d.reason)
            before = self._obs
            self.surface.navigate(url)
            self.surface.settle()
            self.traj.append(TrajStep(len(self.traj) + 1, "agent", "navigate", reason, None, before, url=url))
            self.traj[-1].after = self._observe()
            return f"OK: navigated to {path_of(url)}"

        ctrl = self._control(str(a.get("ref")))
        action = {"click": "click", "fill": "fill", "select": "select", "extract": "extract"}.get(name)
        if action is None:
            raise LookupError(f"unknown tool {name}")
        d = self.policy.check_action(action, control=ctrl, extra_patterns=self.profile.irreversible_labels)
        if not d.allowed:
            self.rec.event("policy_blocked", tool=name, control=ctrl.name, reason=d.reason)
            raise _Denied(d.reason)
        if d.needs_approval:
            self._approval(ctrl, reason)
        step = TrajStep(len(self.traj) + 1, "agent", action, reason, ctrl, self._obs, risk=d.risk)  # type: ignore[arg-type]

        if action == "click":
            self.surface.click(ctrl)
            msg = f"OK: clicked {ctrl.role} '{self.redactor.text(ctrl.name)}'"
        elif action in ("fill", "select"):
            kind, ref, value = self._value_for(a, action)
            step.value_kind, step.value_ref = kind, ref
            if action == "fill":
                self.surface.fill(ctrl, value)
            else:
                self.surface.select(ctrl, value)
            shown = f"secret {ref}" if kind == "secret" else f"param {ref}" if kind == "param" else repr(value)
            msg = f"OK: {action} {ctrl.role} '{ctrl.name}' with {shown}"
        else:
            raw = self.surface.read(ctrl)
            out = str(a["output_name"])
            if not re.fullmatch(r"[a-z][a-z0-9_]*", out):
                raise LookupError("output_name must be snake_case")
            self.outputs[out] = raw.strip()
            step.extract = {"output": out, "type": a["output_type"], "description": a.get("description", ""),
                            "row_label": a.get("row_label")}
            msg = f"OK: extracted {out} = {self.redactor.text(raw.strip())!r}"
        self.surface.settle()
        step.after = self._observe()
        self.traj.append(step)
        self.rec.event("action_executed", n=step.n, action=action, reason=reason,
                       control={"role": ctrl.role, "name": ctrl.name, "frame": ctrl.frame,
                                "name_source": ctrl.name_source, "native_id": ctrl.native_id},
                       value_kind=step.value_kind, value_ref=step.value_ref if step.value_kind != "literal" else None,
                       risk=d.risk, page_after=[(f.name, path_of(f.url), f.headings) for f in step.after.frames])
        return msg

    def _value_for(self, a: dict[str, Any], action: str) -> tuple[str, str, str]:
        if a.get("secret"):
            name = str(a["secret"])
            if name not in self.secrets:
                raise LookupError(f"unknown secret '{name}'")
            self.secrets_used.add(name)
            return "secret", name, self.secrets[name]
        if a.get("param"):
            name = str(a["param"])
            if name not in self.params:
                raise LookupError(f"unknown param '{name}'")
            return "param", name, self.params[name]
        text = a.get("text") if action == "fill" else a.get("option")
        if text is None:
            raise LookupError(f"{action} needs one of param/secret/{'text' if action == 'fill' else 'option'}")
        for k, v in self.params.items():  # model typed a param value literally, treat it as the param
            if text == v:
                return "param", k, v
        return "literal", str(text), str(text)

    # --- human in the loop ---
    def _approval(self, ctrl: Control, reason: str) -> None:
        ev = self.rec.capture(self.surface, "approval_request", dom=False)
        intv = self.broker.open(
            run_id=self.rec.run_id, kind="approval", subject=f"discovery: {self.goal}", step_id=None,
            step_intent=reason, reason_code="IRREVERSIBLE_ACTION",
            reason=f"Agent wants to press '{ctrl.name}'. Approve?", observed={"control": ctrl.name},
            evidence=ev, live_endpoint=self.surface.live_endpoint)
        final = self._wait(intv.id, "approval")
        if final.status != "resolved" or not final.resolution or final.resolution.mode != "approve":
            raise _Denied(f"operator did not approve pressing '{ctrl.name}'")

    def _handoff(self, code: str, reason: str) -> str:
        assert self._obs is not None
        ev = self.rec.capture(self.surface, f"handoff_{code.lower()}")
        red = self.redactor.observation(self._obs)
        intv = self.broker.open(
            run_id=self.rec.run_id, kind="takeover", subject=f"discovery: {self.goal}",
            step_id=f"agent_step_{len(self.traj) + 1}", step_intent=None, reason_code=code, reason=reason,
            observed={"frames": {f.name or "top": {"path": path_of(f.url), "headings": f.headings} for f in red.frames}},
            evidence=ev, live_endpoint=self.surface.live_endpoint)
        before = self._obs
        final = self._wait(intv.id, "takeover")
        if final.status == "expired":
            raise _Aborted("no operator responded")
        if final.resolution and final.resolution.mode == "abort":
            raise _Aborted(final.resolution.note or "abort")
        human = self._absorb_human_actions(before)
        note = final.resolution.note if final.resolution else ""
        return (f"A human operator took control and handed it back. They performed {len(human)} action(s): "
                f"{'; '.join(human) or 'none'}. Operator note: {note or '-'}. Continue from the current screen.")

    def _wait(self, iid: str, kind: str):
        self.rec.event("intervention_opened", intervention=iid, kind=kind)
        print(f"\n[escalation] {kind} requested - intervention {iid}. "
              f"Open the operator console (python -m cua operator).", file=sys.stderr, flush=True)
        self._active_iv = iid
        self._human_buffer.clear()
        claimed = {"v": False}

        def tick(i):
            if i.status == "claimed" and not claimed["v"]:
                claimed["v"] = True
                self.rec.event("control_transferred", to=f"human:{i.claimed_by}", intervention=iid)

        try:
            final = self.broker.wait(iid, self.surface.pump, self.operator_timeout_s, tick)
        finally:
            self._active_iv = None
        res = final.resolution
        self.rec.event("control_returned", intervention=iid, status=final.status,
                       resolution=res.mode if res else None, operator=res.operator if res else None,
                       human_actions=len(final.human_actions))
        return final

    def _on_human_action(self, frame: str, payload: dict[str, Any]) -> None:
        if not self._active_iv:
            return
        self._human_buffer.append((frame, payload))
        red = self.redactor.obj({k: payload.get(k) for k in ("type", "role", "name", "value", "row", "column", "native_id")})
        self.broker.record_human_action(self._active_iv, {"source": "recorder", "frame": frame, **red})
        self.rec.event("human_action", intervention=self._active_iv, frame=frame, **red)

    def _absorb_human_actions(self, before: Observation) -> list[str]:
        """Convert the human's recorded clicks and edits into trajectory steps."""
        described = []
        obs_before = before
        for frame, p in self._human_buffer:
            role = p.get("role")
            if not role or role == "cell":
                continue
            ctrl = Control(ref="human", role=role, name=p.get("name") or "", name_source=p.get("name_source") or "",
                           frame=frame, value=p.get("value"), native_id=p.get("native_id"), row=p.get("row"),
                           column=p.get("column"), col_index=p.get("col_index"))
            if p.get("type") == "click" and role in ("button", "link"):
                self.traj.append(TrajStep(len(self.traj) + 1, "human", "click", "performed by human operator",
                                          ctrl, obs_before, risk=self.policy.classify(ctrl)))
                described.append(f"clicked {role} '{self.redactor.text(ctrl.name)}'")
            elif p.get("type") == "change" and role in ("textbox", "password", "combobox"):
                act = "select" if role == "combobox" else "fill"
                kind, ref = ("secret", None) if role == "password" else ("literal", str(p.get("value") or ""))
                if kind == "literal":
                    for k, v in self.params.items():
                        if ref == v:
                            kind, ref = "param", k
                self.traj.append(TrajStep(len(self.traj) + 1, "human", act, "performed by human operator",
                                          ctrl, obs_before, value_kind=kind, value_ref=ref))
                described.append(f"{act} {role} '{ctrl.name}'")
        after = self._observe()
        for s in self.traj:
            if s.source == "human" and s.after is None:
                s.after = after
        self._human_buffer.clear()
        return described

    # --- end ---
    def _done(self, status: str, summary: str, finish: Optional[dict[str, Any]] = None) -> DiscoveryResult:
        self.surface.set_recorder(None)
        cap = self.rec.capture(self.surface, f"discovery_end_{status}")
        self.rec.event("discovery_finished", status=status, summary=summary, steps=len(self.traj),
                       outputs=self.outputs, usage=self.usage, evidence=cap)
        return DiscoveryResult(status=status, summary=summary, trajectory=self.traj, finish=finish or {},
                               outputs=self.outputs, entry_url=self.entry_url, model=self.model,
                               run_id=self.rec.run_id, secrets_used=sorted(self.secrets_used), usage=self.usage)


class _Denied(Exception):
    pass


class _Aborted(Exception):
    pass
