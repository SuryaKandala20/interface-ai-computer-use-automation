"""End-to-end tests: discovery, compile, approve, then replay against the running
mock app, covering each kind of runtime problem plus a human handoff.

The LLM is swapped for a scripted fake so this runs offline. The real LLM run is in
evidence/.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from cua.cli import make_surface, profile_for, tenant_arg
from cua.compiler import Compiler
from cua.control import HandoffBroker
from cua.discover import DiscoveryAgent
from cua.evidence import RunRecorder
from cua.policy import Policy
from cua.redact import Redactor
from cua.replay import ReplayEngine
from cua.schema import Capability, now_utc
from cua.tenancy import load_profile, resolve_secrets
from scripted_llm import ScriptedClient, balance_script

CONTROL = "runs/test/_control"


def chaos(base: str, mode):
    req = urllib.request.Request(f"{base}/__chaos", data=json.dumps({"mode": mode}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req, timeout=5).read()


@pytest.fixture(scope="module")
def artifact(mockapp) -> Capability:
    tenant = tenant_arg("fvcu")
    profile = profile_for(tenant.product, tenant.product_version)
    policy = Policy.load("config/policy.json")
    red = Redactor(profile.pii_labels)
    secrets = resolve_secrets(tenant, list(tenant.secrets))
    for k, v in secrets.items():
        red.register_secret(k, v)
    rec = RunRecorder("discovery-scripted", red, root="runs/test")
    surface = make_surface(policy, rec, False)
    try:
        agent = DiscoveryAgent(
            goal="Look up member 10234 and read their share savings available balance", tenant=tenant,
            profile=profile, policy=policy, surface=surface, recorder=rec, broker=HandoffBroker(CONTROL),
            redactor=red, params={"member_id": "10234"}, secrets=secrets, entry_url=tenant.base_url + "/login",
            model="scripted-test-double", client=ScriptedClient(balance_script()))
        result = agent.run()
    finally:
        surface.close()
    assert result.status == "success", result.summary
    cap = Compiler(tenant=tenant, profile=profile, redactor=red, params={"member_id": "10234"}).compile(
        result, capability_id="ledgerline.member.get_share_savings_balance", version="1.0.0")
    # nothing from the screen that's PII, and no secrets, in the log
    log = (rec.dir / "events.jsonl").read_text(encoding="utf-8")
    for leaked in ("fv-demo-Pass!", "123-45-6789", "DOE, JANE A", "12 ELM ST"):
        assert leaked not in log
    return cap.model_copy(update={"approval": cap.approval.model_copy(
        update={"status": "approved", "reviewed_by": "test", "reviewed_at": now_utc()})})


def run(cap: Capability, inputs: dict, tenant: str = "fvcu", **kw):
    t = tenant_arg(tenant)
    profile = load_profile(f"config/profiles/{cap.app.profile.replace('@', '-')}.json")
    policy = Policy.load("config/policy.json")
    red = Redactor(profile.pii_labels)
    rec = RunRecorder("replay-test", red, root="runs/test")
    surface = make_surface(policy, rec, False)
    try:
        return ReplayEngine(cap, tenant=t, profile=profile, policy=policy, surface=surface, recorder=rec,
                            broker=HandoffBroker(CONTROL), redactor=red, **kw).run(inputs), rec.dir
    finally:
        surface.close()
        rec.close()


def test_artifact_shape(artifact: Capability):
    ids = [s.id for s in artifact.steps]
    assert ids == ["open_entry", "fill_operator_id", "fill_password", "click_sign_on", "click_member_inquiry",
                   "fill_member_no", "click_search", "click_select", "extract_share_savings"]
    blob = artifact.model_dump_json()
    assert "10234" not in blob and "teller01" not in blob and "12,425" not in blob
    sel = artifact.steps[7].target.primary
    assert sel.row == "{{inputs.member_id}}" and sel.name == "Select"
    ext = artifact.steps[8].target.primary
    assert (ext.row, ext.column) == ("Share Savings", "Available Balance")


def test_replay_success_other_member(artifact):
    r, _ = run(artifact, {"member_id": "10235"})
    assert r.status == "succeeded", r.error
    assert r.outputs == {"available_balance": "3210.55"}


def test_business_outcome_not_found(artifact):
    r, _ = run(artifact, {"member_id": "99999"})
    assert r.status == "business_outcome" and r.outcome.code == "MEMBER_NOT_FOUND"
    assert r.outcome.step_id == "click_search" and r.error is None


def test_business_outcome_permission_denied(artifact):
    r, _ = run(artifact, {"member_id": "10777"})
    assert r.status == "business_outcome" and r.outcome.code == "PERMISSION_DENIED"


def test_invalid_input_rejected_before_ui(artifact):
    r, d = run(artifact, {"member_id": "12ab"})
    assert r.status == "failed" and r.error.code == "INVALID_INPUT" and not r.steps


def test_draft_is_not_runnable_unattended(artifact):
    draft = artifact.model_copy(update={"approval": artifact.approval.model_copy(update={"status": "draft"})})
    r, _ = run(draft, {"member_id": "10235"})
    assert r.error.code == "CAPABILITY_NOT_APPROVED"


def test_tampered_artifact_refused(artifact):
    tampered = artifact.model_copy(update={"title": "something else"})
    r, _ = run(tampered, {"member_id": "10235"})
    assert r.error.code == "ARTIFACT_INTEGRITY"


@pytest.mark.parametrize("mode,code", [("session_expired_once", "SESSION_EXPIRED"),
                                       ("notice_once", "SYSTEM_NOTICE_INTERSTITIAL")])
def test_recoverable_conditions(artifact, mockapp, mode, code):
    chaos(mockapp, mode)
    try:
        r, _ = run(artifact, {"member_id": "10235"})
    finally:
        chaos(mockapp, None)
    assert r.status == "succeeded", r.error
    assert [x["code"] for x in r.recoveries] == [code]


def test_slow_page_is_waited_for(artifact, mockapp):
    chaos(mockapp, "slow")
    try:
        r, _ = run(artifact, {"member_id": "10235"})
    finally:
        chaos(mockapp, None)
    assert r.status == "succeeded" and r.outputs["available_balance"] == "3210.55"


def test_hard_failure_has_evidence(artifact, mockapp):
    chaos(mockapp, "app_error")
    try:
        r, _ = run(artifact, {"member_id": "10235"})
    finally:
        chaos(mockapp, None)
    assert r.status == "failed" and r.error.code == "APP_ERROR" and r.error.step_id == "click_select"
    assert Path(r.error.evidence["screenshot"]).exists() and Path(r.error.evidence["dom_snapshot"]).exists()
    dom = Path(r.error.evidence["dom_snapshot"]).read_text(encoding="utf-8")
    assert "fv-demo-Pass!" not in dom


def test_other_tenant_via_overrides(artifact):
    r, d = run(artifact, {"member_id": "10235"}, tenant="harbor")
    assert r.status == "succeeded", r.error
    assert r.outputs == {"available_balance": "3210.55"}
    assert "tenant_overrides_applied" in (d / "events.jsonl").read_text(encoding="utf-8")


def test_human_takeover_on_unknown_dialog(artifact, mockapp):
    """An unknown dialog makes the checkpoint fail, so replay asks for a human. A scripted
    operator claims it, clicks through the dialog in the same browser over CDP, and
    hands back with step_done. Replay then checks the page and finishes."""
    from playwright.sync_api import sync_playwright

    broker = HandoffBroker(CONTROL)
    before = {i.id for i in broker.list()}
    done = {}

    def operator():
        deadline = time.time() + 60
        while time.time() < deadline:
            new = [i for i in broker.list({"open"}) if i.id not in before]
            if new:
                iv = broker.claim(new[0].id, "operator.test")
                assert broker.lease(iv.run_id).holder == "human"
                with sync_playwright() as p:
                    b = p.chromium.connect_over_cdp(iv.live_endpoint)
                    page = b.contexts[0].pages[0]
                    main = next(f for f in page.frames if f.name == "main")
                    main.get_by_role("button", name="Continue Anyway").click()
                    time.sleep(1.0)
                broker.resolve(iv.id, "step_done", "operator.test", "dismissed concurrency warning")
                done["id"] = iv.id
                return
            time.sleep(0.3)

    t = threading.Thread(target=operator, daemon=True)
    t.start()
    chaos(mockapp, "unknown_dialog_once")
    try:
        r, d = run(artifact, {"member_id": "10235"}, on_failure="escalate", operator_timeout_s=90)
    finally:
        chaos(mockapp, None)
    t.join(5)
    assert r.status == "succeeded", r.error
    assert r.interventions and r.interventions[0]["resolution"] == "step_done"
    acts = r.interventions[0]["human_actions"]
    assert any(a.get("name") == "Continue Anyway" for a in acts), acts
    step = next(s for s in r.steps if s.id == "click_select")
    assert step.status == "done_by_human"
    events = (d / "events.jsonl").read_text(encoding="utf-8")
    assert "control_transferred" in events and "control_returned" in events
