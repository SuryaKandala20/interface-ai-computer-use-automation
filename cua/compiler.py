"""Turns a discovery trajectory into a Capability.

This is where the recording becomes reusable:
  - locators use role + visible label (row/column for table cells), with a form
    field name fallback and one that ignores the frame. Data (names, balances, the
    member number) is never used to find things; rows are found by a stable label
    or by the input parameter instead.
  - values get parameterised: task params become {{inputs.x}}, credentials
    {{secrets.x}}, the tenant host {{tenant.base_url}}.
  - every click gets a checkpoint, the heading (or URL) that showed up after it, so
    replay checks it actually got there.
  - at the end we refuse to write the artifact if any PII or secret ended up in it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from .discover import DiscoveryResult, TrajStep, path_of
from .locator import candidates
from .redact import Redactor
from .schema import (
    AllOf, AppProfile, AppRef, Capability, Condition, ControlPresent, Extraction, Locator, OutputSpec,
    ParamSpec, Provenance, SecretSpec, Step, Target, TenantBinding, TextVisible, UrlMatches, now_utc,
)
from .surface.base import Control, Observation

MONEYISH = re.compile(r"^[\s$()\-+]*[\d,]+(\.\d+)?[\s)]*$")


class CompileError(ValueError):
    pass


def slug(s: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:n] or "x"


class Compiler:
    def __init__(self, *, tenant: TenantBinding, profile: AppProfile, redactor: Redactor, params: dict[str, str]):
        self.tenant, self.profile, self.redactor, self.params = tenant, profile, redactor, params
        self.by_value = {v: k for k, v in params.items() if v}
        self.base = tenant.base_url.rstrip("/")

    # --- templating ---
    def _is_data(self, text: str) -> bool:
        """Is this text data (PII, a param value, an amount) rather than part of the UI?"""
        t = (text or "").strip()
        return (not t) or self.redactor.text(t) != t or t in self.by_value or bool(MONEYISH.match(t))

    def _tmpl(self, text: str) -> str:
        for v, k in sorted(self.by_value.items(), key=lambda kv: -len(kv[0])):
            text = text.replace(v, "{{inputs.%s}}" % k)
        return text

    def _url(self, url: str) -> str:
        if url.startswith(self.base):
            url = "{{tenant.base_url}}" + url[len(self.base):]
        return self._tmpl(url)

    # --- locators ---
    def _row_anchor(self, c: Control, hint: Optional[str]) -> Optional[str]:
        row = c.row or []
        if hint and hint in row and not self._is_data(hint):
            return hint
        for v in row:  # a row containing our input value is the best anchor, it's what we searched for
            if v in self.by_value:
                return "{{inputs.%s}}" % self.by_value[v]
        for v in row:
            if v != c.name and not self._is_data(v):
                return v
        return None

    def _locator(self, c: Control, obs: Observation, row_hint: Optional[str] = None) -> Locator:
        frame = c.frame or None
        fallbacks: list[Target] = []
        if c.role == "cell" or self._is_data(c.name):
            anchor = self._row_anchor(c, row_hint)
            if anchor is None:
                raise CompileError(f"cannot anchor data control '{self.redactor.text(c.name)}' to a stable row label")
            prim = Target(role=c.role, frame=frame, row=anchor,
                          column=c.column if c.column else None,
                          column_index=None if c.column else c.col_index)
            why = (f"Value cell located by its row label '{anchor}' and "
                   f"{'column header ' + repr(c.column) if c.column else 'position ' + str(c.col_index)}; "
                   "cell content is data and never used for targeting.")
        else:
            prim = Target(role=c.role, name=c.name, frame=frame)
            why = f"{c.role} identified by its {c.name_source} '{c.name}' - what an operator sees; legacy page has no ids/test-ids."
            in_param_row = next((v for v in (c.row or []) if v in self.by_value), None)
            if in_param_row:
                prim = prim.model_copy(update={"row": "{{inputs.%s}}" % self.by_value[in_param_row]})
                why += " Scoped to the table row containing the input parameter."
        render = self._render_example
        n = len(candidates(prim, obs, render))
        if n > 1 and prim.row is None:
            anchor = self._row_anchor(c, row_hint)
            if anchor:
                prim = prim.model_copy(update={"row": anchor})
                n = len(candidates(prim, obs, render))
                why += f" Disambiguated by row '{anchor}'."
        if n > 1:
            matches = candidates(prim, obs, render)
            idx = next((i for i, m in enumerate(matches) if m.ref == c.ref), 0)
            prim = prim.model_copy(update={"nth": idx})
            why += f" Still ambiguous ({n} matches) - pinned to occurrence #{idx}; reviewer should confirm."
        if c.native_id and c.role not in ("cell",):
            fallbacks.append(Target(role=c.role, native_id=c.native_id, frame=frame, row=prim.row))
            why += f" Fallback: form field name '{c.native_id}' (part of the server's form contract, stable across skins)."
        if frame:
            fallbacks.append(prim.model_copy(update={"frame": None}))
            why += " Last fallback ignores the frame, tolerating layout (frameset) changes."
        return Locator(primary=prim, fallbacks=fallbacks, rationale=why)

    def _render_example(self, s: str) -> str:
        return re.sub(r"\{\{\s*inputs\.(\w+)\s*\}\}", lambda m: self.params.get(m.group(1), ""), s)

    # --- checkpoints ---
    def _expect(self, s: TrajStep, nxt: Optional[TrajStep]) -> Optional[Condition]:
        """Checkpoint for a click/navigate: a heading that appeared because of it.
        We look in the frame the next step uses first, so banner or menu frames don't
        end up as checkpoints."""
        if s.action not in ("click", "navigate") or s.after is None:
            return None
        before = {f.name: set(f.headings) for f in s.before.frames}
        prefer = [x.control.frame for x in (nxt, s) if x is not None and x.control is not None]
        frames = sorted(s.after.frames, key=lambda f: prefer.index(f.name) if f.name in prefer else len(prefer))
        brand = self.tenant.display_name.casefold()
        for f in frames:
            new = [h for h in f.headings if h not in before.get(f.name, set())
                   and not self._is_data(h) and brand not in h.casefold()]
            if new:
                return TextVisible(text=self._tmpl(new[0]), frame=f.name or None)
        bpaths = {f.name: path_of(f.url) for f in s.before.frames}
        for f in frames:
            p = path_of(f.url)
            if p != bpaths.get(f.name) and f.name:
                pat = re.escape(self._tmpl(p.split("?")[0])).replace(r"\{\{", "{{").replace(r"\}\}", "}}")
                return UrlMatches(pattern=pat + r"(\?|$)", frame=f.name or None)
        return None

    # --- main ---
    def compile(self, d: DiscoveryResult, *, capability_id: Optional[str], version: str) -> Capability:
        if d.status != "success":
            raise CompileError(f"only successful runs compile (status={d.status})")
        fin = d.finish
        title = fin.get("capability_title") or "Recorded capability"
        cap_id = capability_id or f"{self.profile.product}.{slug(title)}"
        steps: list[Step] = [Step(
            id="open_entry", intent="Open the application sign-on page", action="navigate",
            url=self._url(d.entry_url), source="agent",
            expect=None,
        )]
        used_ids = {"open_entry"}
        outputs: list[OutputSpec] = []
        secrets_used = set(d.secrets_used)
        traj = d.trajectory
        for k, s in enumerate(traj):
            nxt = traj[k + 1] if k + 1 < len(traj) else None
            if s.action == "navigate":
                step = Step(id=self._sid("navigate", path_of(s.url or ""), used_ids), intent=self._intent(s),
                            action="navigate", url=self._url(s.url or ""), expect=self._expect(s, nxt), source=s.source)  # type: ignore[arg-type]
                steps.append(step)
                continue
            c = s.control
            assert c is not None
            row_hint = (s.extract or {}).get("row_label")
            loc = self._locator(c, s.before, row_hint)
            value = None
            if s.action in ("fill", "select"):
                if s.value_kind == "param":
                    value = "{{inputs.%s}}" % s.value_ref
                elif s.value_kind == "secret":
                    name = s.value_ref or self._password_secret(secrets_used)
                    secrets_used.add(name)
                    value = "{{secrets.%s}}" % name
                else:
                    value = self._tmpl(s.value_ref or "")
            ex = None
            if s.action == "extract":
                e = s.extract or {}
                ex = Extraction(output=e["output"], parse=e["type"])
                raw = d.outputs.get(e["output"], "")
                outputs.append(OutputSpec(
                    name=e["output"], type={"text": "string"}.get(e["type"], e["type"]),
                    description=e.get("description") or e["output"],
                    sensitivity="pii" if self.redactor.text(raw) != raw else "internal"))
            label = c.name if not self._is_data(c.name) else (row_hint or c.column or c.role)
            steps.append(Step(
                id=self._sid(s.action, label, used_ids), intent=self._intent(s), action=s.action,  # type: ignore[arg-type]
                target=loc, value=value, extract=ex, risk=s.risk, expect=self._expect(s, nxt), source=s.source,  # type: ignore[arg-type]
            ))
        # the entry step's checkpoint is just that the sign-on form is showing
        steps[0] = steps[0].model_copy(update={"expect": self._entry_expect(d)})
        final_checks: list[Condition] = [st.expect for st in steps if st.expect is not None][-1:]
        final_checks += [ControlPresent(target=st.target.primary) for st in steps if st.extract and st.target]
        if not final_checks:
            raise CompileError("no checkpoint could be derived for the success condition")
        side = "irreversible" if any(st.risk == "irreversible" for st in steps) else "none"
        pdesc = fin.get("parameter_descriptions") or {}
        cap = Capability(
            id=cap_id, version=version, title=title,
            description=fin.get("capability_description") or d.summary,
            app=AppRef(product=self.profile.product, versions=self.profile.versions, surface="legacy_web",
                       profile=self.profile.id),
            entry_url=self._url(d.entry_url), idempotent=side == "none", side_effects=side,
            inputs=[self._param_spec(k, v, pdesc.get(k)) for k, v in self.params.items()],
            secrets=[SecretSpec(name=n, description=f"Tenant-bound credential '{n}' (resolved at run time)")
                     for n in sorted(secrets_used)],
            outputs=outputs, steps=steps,
            success=final_checks[0] if len(final_checks) == 1 else AllOf(conditions=final_checks),
            provenance=Provenance(
                recorded_at=now_utc(), recorded_by="discovery-agent", model=d.model, discovery_run_id=d.run_id,
                source_tenant=self.tenant.tenant_id, goal=self.redactor.text(self._tmpl_goal(fin, d)),
                human_steps=sum(1 for s in d.trajectory if s.source == "human")),
        )
        cap = cap.sealed()
        self._leak_check(cap)
        return cap

    def _entry_expect(self, d: DiscoveryResult) -> Optional[Condition]:
        first = d.trajectory[0] if d.trajectory else None
        if first and first.control is not None:
            return ControlPresent(target=Target(role=first.control.role, name=first.control.name))
        return None

    def _tmpl_goal(self, fin: dict[str, Any], d: DiscoveryResult) -> str:
        return self._tmpl(fin.get("summary") or d.summary)

    def _password_secret(self, used: set[str]) -> str:
        pw = [n for n in used if "pass" in n]
        if len(pw) == 1:
            return pw[0]
        raise CompileError("human typed into a password field; map it to a secret during review")

    def _param_spec(self, name: str, example: str, desc: Optional[str]) -> ParamSpec:
        if re.fullmatch(r"\d+", example):
            return ParamSpec(name=name, type="string", pattern=r"^\d{1,12}$", example=None,
                             description=desc or f"{name} (digits)")
        if re.fullmatch(r"-?\d+\.\d+", example):
            return ParamSpec(name=name, type="decimal", description=desc or name)
        return ParamSpec(name=name, type="string", description=desc or name)

    def _intent(self, s: TrajStep) -> str:
        return self.redactor.text(self._tmpl(s.reason or s.action))[:160]

    @staticmethod
    def _sid(action: str, label: str, used: set[str]) -> str:
        base = f"{action}_{slug(label, 32)}"
        sid, n = base, 2
        while sid in used:
            sid, n = f"{base}_{n}", n + 1
        used.add(sid)
        return sid

    def _leak_check(self, cap: Capability) -> None:
        blob = cap.model_dump_json()
        if self.redactor.text(blob) != blob:
            leaked = [t for v, t in self.redactor._exact.items() if v in blob]
            raise CompileError(f"artifact would persist sensitive data ({leaked or 'pattern match'}); refusing")


def write_trajectory(d: DiscoveryResult, path: Path, redactor: Redactor) -> None:
    """Save the redacted trajectory next to the run as evidence. It never goes in the artifact."""
    def ctrl(c: Optional[Control]):
        return None if c is None else c.to_dict()

    data = [{
        "n": s.n, "source": s.source, "action": s.action, "reason": s.reason, "control": ctrl(s.control),
        "value_kind": s.value_kind, "value_ref": s.value_ref if s.value_kind != "literal" else "[literal]",
        "extract": s.extract, "risk": s.risk, "url": s.url,
        "before": [(f.name, path_of(f.url), f.headings) for f in s.before.frames],
        "after": [(f.name, path_of(f.url), f.headings) for f in s.after.frames] if s.after else None,
    } for s in d.trajectory]
    path.write_text(json.dumps(redactor.obj(data), indent=2), encoding="utf-8")
