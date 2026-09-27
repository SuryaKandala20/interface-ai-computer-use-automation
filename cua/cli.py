"""Command line entry point:  python -m cua <command> ...

  mockapp    run the local legacy target app (LedgerLine Core mock)
  discover   run the LLM on a goal and save a draft capability
  show       human-readable review of an artifact
  approve    mark an artifact approved (binds approval to its fingerprint)
  replay     deterministic replay with inputs (no LLM)
  operator   operator console for escalations and live handoff
  catalog    list capabilities as agent tool definitions
  invoke     call a capability by tool name with JSON args (how an agent would)
  chaos      (test harness) inject a runtime fault into the mock app

add-ons:
  stability        replay N times, report consistency (optional input to approve --min-stability)
  learn            propose a recoverable rule from a resolved human takeover
  accept-proposal  write a new draft version containing the learned rule
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any, Optional

from .control import HandoffBroker
from .evidence import RunRecorder
from .locator import describe_condition, describe_target
from .policy import Policy
from .redact import Redactor
from .schema import AppProfile, Capability, TenantBinding, now_utc
from .tenancy import (
    load_capability, load_dotenv, load_profile, load_tenant, resolve_secrets, save_capability,
)

ROOT = Path.cwd()
CONFIG = ROOT / "config"
CAPS = ROOT / "capabilities"
MOCK_URL = "http://127.0.0.1:8765"


# --- config helpers ---
def tenant_arg(v: str) -> TenantBinding:
    p = Path(v)
    return load_tenant(p if p.suffix == ".json" else CONFIG / "tenants" / f"{v}.json")


def profile_for(product: str, version: str) -> AppProfile:
    major = version.split(".")[0]
    return load_profile(CONFIG / "profiles" / f"{product}-{major}.json")


def kv(items: list[str]) -> dict[str, str]:
    out = {}
    for it in items or []:
        k, _, v = it.partition("=")
        out[k.strip()] = v
    return out


def next_version(cap_id: str) -> str:
    d = CAPS / cap_id
    vs = sorted(tuple(int(x) for x in p.stem.split(".")) for p in d.glob("*.json")) if d.exists() else []
    if not vs:
        return "1.0.0"
    major, minor, _ = vs[-1]
    return f"{major}.{minor + 1}.0"


def make_surface(policy: Policy, rec: RunRecorder, headed: bool):
    from .surface.web import WebSurface

    return WebSurface(headed=headed, url_guard=policy.url_allowed,
                      on_blocked=lambda url, why: rec.event("network_blocked", url=url, reason=why))


def set_chaos(mode: Optional[str]) -> None:
    req = urllib.request.Request(f"{MOCK_URL}/__chaos", data=json.dumps({"mode": mode}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=5).read()


# --- commands ---
def cmd_mockapp(a) -> int:
    from mockapp.app import run

    print(f"LedgerLine Core mock on http://127.0.0.1:{a.port}/t/fvcu/login  (and /t/harbor/login)")
    run(port=a.port)
    return 0


def cmd_discover(a) -> int:
    from .compiler import Compiler, CompileError, write_trajectory
    from .discover import DiscoveryAgent

    tenant = tenant_arg(a.tenant)
    profile = profile_for(tenant.product, tenant.product_version)
    policy = Policy.load(a.policy)
    red = Redactor(profile.pii_labels)
    secrets = resolve_secrets(tenant, list(tenant.secrets))
    for k, v in secrets.items():
        red.register_secret(k, v)
    rec = RunRecorder("discovery", red)
    surface = make_surface(policy, rec, a.headed)
    params = kv(a.param)
    try:
        agent = DiscoveryAgent(goal=a.goal, tenant=tenant, profile=profile, policy=policy, surface=surface,
                               recorder=rec, broker=HandoffBroker(), redactor=red, params=params,
                               secrets=secrets, entry_url=tenant.base_url.rstrip("/") + a.entry,
                               model=a.model, max_steps=a.max_steps)
        result = agent.run()
    finally:
        surface.close()
    write_trajectory(result, rec.dir / "trajectory.json", red)
    print(f"\nDiscovery {result.status}: {red.text(result.summary)}")
    print(f"  evidence: {rec.dir}   llm usage: {result.usage}")
    if result.status != "success":
        rec.close()
        return 1
    try:
        cap = Compiler(tenant=tenant, profile=profile, redactor=red, params=params).compile(
            result, capability_id=a.capability_id, version="0.0.0")
    except CompileError as e:
        rec.event("compile_failed", error=str(e))
        rec.close()
        print(f"  compile failed: {e}")
        return 1
    cap = cap.model_copy(update={"version": a.version or next_version(cap.id)}).sealed()
    out = save_capability(cap, CAPS / cap.id / f"{cap.version}.json")
    save_capability(cap, rec.dir / "capability.json")
    rec.event("capability_emitted", ref=cap.ref, path=str(out), fingerprint=cap.fingerprint)
    rec.close()
    print(f"  capability: {cap.ref} -> {out}  (status: draft)")
    print(f"  review:  python -m cua show {out}")
    print(f"  approve: python -m cua approve {out} --reviewer <you>")
    return 0


def cmd_show(a) -> int:
    cap = load_capability(a.artifact)
    print(render_review(cap))
    return 0


def render_review(cap: Capability) -> str:
    L = [f"{cap.title}  [{cap.ref}]  status={cap.approval.status}",
         f"  {cap.description}",
         f"  app: {cap.app.product} {cap.app.versions} ({cap.app.surface}), profile {cap.app.profile}",
         f"  side effects: {cap.side_effects}   idempotent: {cap.idempotent}   fingerprint: {cap.fingerprint}",
         "  inputs:  " + (", ".join(f"{p.name}:{p.type}{' /' + p.pattern + '/' if p.pattern else ''}" for p in cap.inputs) or "-"),
         "  secrets: " + (", ".join(s.name for s in cap.secrets) or "-"),
         "  outputs: " + (", ".join(f"{o.name}:{o.type} ({o.sensitivity})" for o in cap.outputs) or "-"), "", "  steps:"]
    for i, s in enumerate(cap.steps, 1):
        what = s.url if s.action == "navigate" else describe_target(s.target.primary) if s.target else ""
        val = f" <- {s.value}" if s.value else ""
        ext = f" -> outputs.{s.extract.output} ({s.extract.parse})" if s.extract else ""
        risk = "  !IRREVERSIBLE" if s.risk == "irreversible" else ""
        src = f"  [by {s.source}]" if s.source != "agent" else ""
        L.append(f"   {i:2}. {s.id}: {s.action} {what}{val}{ext}{risk}{src}")
        L.append(f"       intent: {s.intent}")
        if s.target and s.target.fallbacks:
            L.append("       fallbacks: " + "; ".join(describe_target(t) for t in s.target.fallbacks))
        if s.expect:
            L.append(f"       expect: {describe_condition(s.expect)}")
    L.append(f"\n  success: {describe_condition(cap.success)}")
    if cap.outcomes:
        L.append("  capability outcome rules: " + ", ".join(f"{o.code}({o.kind})" for o in cap.outcomes))
    L.append(f"  + shared rules from profile {cap.app.profile}")
    p = cap.provenance
    L.append(f"\n  recorded {p.recorded_at} by {p.recorded_by} ({p.model}) on tenant {p.source_tenant}, "
             f"run {p.discovery_run_id}, human steps: {p.human_steps}")
    if cap.approval.reviewed_by:
        L.append(f"  approved by {cap.approval.reviewed_by} at {cap.approval.reviewed_at}: {cap.approval.notes or ''}")
    return "\n".join(L)


def cmd_approve(a) -> int:
    cap = load_capability(a.artifact)
    if cap.fingerprint and cap.fingerprint != cap.compute_fingerprint():
        print("refusing: artifact was modified after it was sealed; re-seal by re-compiling or bump version")
        return 1
    if a.min_stability is not None:  # optional gate, approve works the same as before without the flag
        from . import stability

        ok, why = stability.gate(cap, a.min_stability, stability.latest_for(cap))
        print(f"stability gate: {why}")
        if not ok:
            print("refusing approval")
            return 1
    cap = cap.sealed()
    cap = cap.model_copy(update={"approval": cap.approval.model_copy(update={
        "status": "approved", "reviewed_by": a.reviewer, "reviewed_at": now_utc(), "notes": a.notes})})
    save_capability(cap, a.artifact)
    print(f"approved {cap.ref} (fingerprint {cap.fingerprint})")
    return 0


def replay(cap: Capability, tenant: TenantBinding, inputs: dict[str, Any], *, on_failure: str = "fail",
           approve_irreversible: bool = False, allow_draft: bool = False, headed: bool = False,
           policy_path: str = str(CONFIG / "policy.json")):
    from .replay import ReplayEngine

    profile = load_profile(CONFIG / "profiles" / f"{cap.app.profile.replace('@', '-')}.json")
    policy = Policy.load(policy_path)
    red = Redactor(profile.pii_labels)
    rec = RunRecorder("replay", red)
    surface = make_surface(policy, rec, headed)
    try:
        engine = ReplayEngine(cap, tenant=tenant, profile=profile, policy=policy, surface=surface, recorder=rec,
                              broker=HandoffBroker(), redactor=red, on_failure=on_failure,  # type: ignore[arg-type]
                              approve_irreversible=approve_irreversible, allow_draft=allow_draft)
        return engine.run(inputs)
    finally:
        surface.close()
        rec.close()


def _emit(result) -> int:
    print(json.dumps(result.model_dump(mode="json"), indent=2))
    return {"succeeded": 0, "business_outcome": 2}.get(result.status, 1)


def cmd_replay(a) -> int:
    if a.chaos:
        set_chaos(a.chaos)
    try:
        r = replay(load_capability(a.artifact), tenant_arg(a.tenant), kv(a.input), on_failure=a.on_failure,
                   approve_irreversible=a.approve_irreversible, allow_draft=a.allow_draft, headed=a.headed,
                   policy_path=a.policy)
    finally:
        if a.chaos:
            set_chaos(None)
    return _emit(r)


def catalog(directory: Path = CAPS) -> dict[str, tuple[Path, Capability]]:
    """Latest approved version of each capability, keyed by tool name."""
    latest: dict[str, tuple[tuple[int, ...], Path, Capability]] = {}
    for p in directory.glob("*/*.json"):
        cap = load_capability(p)
        if cap.approval.status != "approved":
            continue
        v = tuple(int(x) for x in cap.version.split("."))
        name = cap.tool_definition()["name"]
        if name not in latest or v > latest[name][0]:
            latest[name] = (v, p, cap)
    return {k: (p, c) for k, (_, p, c) in latest.items()}


def cmd_catalog(a) -> int:
    tools = []
    for name, (p, cap) in sorted(catalog().items()):
        profile = load_profile(CONFIG / "profiles" / f"{cap.app.profile.replace('@', '-')}.json")
        t = cap.tool_definition()
        codes = sorted({r.code for r in profile.outcomes if r.kind == "business"})
        t["description"] += f" Shared business outcomes: {', '.join(codes)}."
        tools.append(t)
    print(json.dumps(tools, indent=2))
    return 0


def cmd_invoke(a) -> int:
    cat = catalog()
    if a.tool not in cat:
        print(json.dumps({"error": f"unknown or unapproved capability '{a.tool}'", "available": sorted(cat)}))
        return 1
    _, cap = cat[a.tool]
    return _emit(replay(cap, tenant_arg(a.tenant), json.loads(a.args), on_failure=a.on_failure))


def cmd_operator(a) -> int:
    import uvicorn

    from .operator import create_app

    print(f"Operator console on http://127.0.0.1:{a.port}")
    uvicorn.run(create_app(), host="127.0.0.1", port=a.port, log_level="warning")
    return 0


def cmd_chaos(a) -> int:
    set_chaos(None if a.mode == "off" else a.mode)
    print(f"chaos mode: {a.mode}")
    return 0


# --- add-ons ---
def cmd_stability(a) -> int:
    from . import stability

    cap, tenant, inputs = load_capability(a.artifact), tenant_arg(a.tenant), kv(a.input)
    try:
        report = stability.measure(cap, a.runs, lambda: replay(cap, tenant, inputs, allow_draft=True))
    except stability.StabilityError as e:
        print(f"refusing: {e}")
        return 1
    path = stability.save(report)
    print(json.dumps(report, indent=2))
    print(f"\nreport: {path}")
    return 0 if report["consistent"] and report["success_rate"] == 1 else 1


def cmd_learn(a) -> int:
    from .learn import LearnError, propose, save_proposal

    cap = load_capability(a.artifact)
    profile = load_profile(CONFIG / "profiles" / f"{cap.app.profile.replace('@', '-')}.json")
    try:
        p = propose(HandoffBroker().get(a.intervention), cap, policy=Policy.load(a.policy), profile=profile)
    except KeyError:
        print(f"no intervention '{a.intervention}' in runs/_control/interventions")
        return 1
    except LearnError as e:
        print(f"nothing proposed: {e}")
        return 1
    path = save_proposal(p)
    print(p.model_dump_json(indent=2))
    print(f"\nproposal: {path}\nreview it, then: python -m cua accept-proposal {path} {a.artifact} --reviewer <you>")
    return 0


def cmd_accept_proposal(a) -> int:
    from .learn import LearnError, Proposal, accept, save_proposal

    p = Proposal.model_validate_json(Path(a.proposal).read_text(encoding="utf-8"))
    base = load_capability(a.artifact)
    try:
        new = accept(p, base, reviewer=a.reviewer)
    except LearnError as e:
        print(f"refusing: {e}")
        return 1
    out = save_capability(new, CAPS / new.id / f"{new.version}.json")
    save_proposal(p.model_copy(update={"status": "accepted", "accepted_as": new.ref}))
    print(f"wrote {new.ref} (draft) -> {out}")
    print(f"  added rule: {p.rule.id}  when {describe_condition(p.rule.when)} -> dismiss "
          f"{describe_target(p.rule.recovery.target)}")
    print(f"  next: python -m cua show {out}   then   python -m cua approve {out} --reviewer <you>")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    load_dotenv(ROOT / ".env")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="cua", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("mockapp")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(fn=cmd_mockapp)

    s = sub.add_parser("discover")
    s.add_argument("--tenant", required=True)
    s.add_argument("--goal", required=True)
    s.add_argument("--param", action="append", default=[], help="name=value (repeatable)")
    s.add_argument("--entry", default="/login", help="entry path under the tenant base url")
    s.add_argument("--capability-id")
    s.add_argument("--version")
    s.add_argument("--model", default=None)
    s.add_argument("--max-steps", type=int)
    s.add_argument("--headed", action="store_true")
    s.add_argument("--policy", default=str(CONFIG / "policy.json"))
    s.set_defaults(fn=cmd_discover)

    s = sub.add_parser("show")
    s.add_argument("artifact")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("approve")
    s.add_argument("artifact")
    s.add_argument("--reviewer", required=True)
    s.add_argument("--notes")
    s.add_argument("--min-stability", type=float, default=None,
                   help="(add-on) require a stability report for this exact artifact with at least this success rate")
    s.set_defaults(fn=cmd_approve)

    s = sub.add_parser("replay")
    s.add_argument("artifact")
    s.add_argument("--tenant", required=True)
    s.add_argument("--input", action="append", default=[], help="name=value (repeatable)")
    s.add_argument("--on-failure", choices=["fail", "escalate"], default="fail")
    s.add_argument("--approve-irreversible", action="store_true")
    s.add_argument("--allow-draft", action="store_true")
    s.add_argument("--headed", action="store_true")
    s.add_argument("--chaos", help="(test harness) inject a mock-app fault for this run")
    s.add_argument("--policy", default=str(CONFIG / "policy.json"))
    s.set_defaults(fn=cmd_replay)

    s = sub.add_parser("operator")
    s.add_argument("--port", type=int, default=8766)
    s.set_defaults(fn=cmd_operator)

    s = sub.add_parser("catalog")
    s.set_defaults(fn=cmd_catalog)

    s = sub.add_parser("invoke")
    s.add_argument("tool")
    s.add_argument("--tenant", required=True)
    s.add_argument("--args", default="{}")
    s.add_argument("--on-failure", choices=["fail", "escalate"], default="fail")
    s.set_defaults(fn=cmd_invoke)

    s = sub.add_parser("stability", help="(add-on) replay N times and report consistency")
    s.add_argument("artifact")
    s.add_argument("--tenant", required=True)
    s.add_argument("--input", action="append", default=[])
    s.add_argument("--runs", type=int, default=5)
    s.set_defaults(fn=cmd_stability)

    s = sub.add_parser("learn", help="(add-on) propose a rule from a resolved human takeover")
    s.add_argument("intervention")
    s.add_argument("artifact")
    s.add_argument("--policy", default=str(CONFIG / "policy.json"))
    s.set_defaults(fn=cmd_learn)

    s = sub.add_parser("accept-proposal", help="(add-on) new draft version with the learned rule")
    s.add_argument("proposal")
    s.add_argument("artifact")
    s.add_argument("--reviewer", required=True)
    s.set_defaults(fn=cmd_accept_proposal)

    s = sub.add_parser("chaos")
    s.add_argument("mode", choices=["off", "slow", "session_expired_once", "notice_once", "app_error",
                                    "unknown_dialog_once"])
    s.set_defaults(fn=cmd_chaos)

    a = ap.parse_args(argv)
    if a.cmd == "discover" and not a.model:
        a.model = os.environ.get("CUA_MODEL", "")
        if not a.model:
            print("Set CUA_MODEL in .env (the model id to use for discovery), or pass --model.")
            return 2
    return a.fn(a)
