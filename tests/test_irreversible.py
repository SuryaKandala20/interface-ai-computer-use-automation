"""Irreversible actions. Discovery waits for a human to approve, the compiled
capability is marked as having side effects, and replay won't press the button
without approval."""

from __future__ import annotations

import threading
import time

import pytest

from cua.cli import make_surface, profile_for, tenant_arg
from cua.compiler import Compiler
from cua.control import HandoffBroker
from cua.discover import DiscoveryAgent
from cua.evidence import RunRecorder
from cua.policy import Policy
from cua.redact import Redactor
from cua.schema import now_utc
from cua.tenancy import resolve_secrets
from scripted_llm import ScriptedClient, balance_script, cell, ref
from test_e2e import CONTROL, run


def subaccount_script():
    login_and_open = balance_script()[:7]
    return login_and_open + [
        lambda o: ("click", {"ref": ref(o, "link", "Open Sub-Account"), "reason": "Start a sub-account"}),
        lambda o: ("select", {"ref": ref(o, "combobox", "Account Type"), "param": "account_type", "reason": "Type"}),
        lambda o: ("fill", {"ref": ref(o, "textbox", "Nickname"), "param": "nickname", "reason": "Nickname"}),
        lambda o: ("fill", {"ref": ref(o, "textbox", "Initial Deposit"), "param": "deposit", "reason": "Deposit"}),
        lambda o: ("click", {"ref": ref(o, "button", "Continue"), "reason": "Review the request"}),
        lambda o: ("click", {"ref": ref(o, "button", "Submit Request"), "reason": "Submit the request"}),
        lambda o: ("extract", {"ref": cell(o, "Confirmation No.", 1), "output_name": "confirmation_number",
                               "output_type": "text", "description": "Confirmation number",
                               "row_label": "Confirmation No.", "reason": "Read confirmation"}),
        lambda o: ("finish", {"status": "success", "summary": "Opened sub-account",
                              "capability_title": "Open sub-account",
                              "capability_description": "Open a new sub-account for a member."}),
    ]


def approve_next(decision: str, broker: HandoffBroker, seen: set[str]):
    def go():
        deadline = time.time() + 60
        while time.time() < deadline:
            for iv in broker.list({"open"}):
                if iv.id not in seen and iv.kind == "approval":
                    broker.claim(iv.id, "supervisor.test")
                    broker.resolve(iv.id, decision, "supervisor.test", "checked with member")
                    return
            time.sleep(0.2)

    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t


@pytest.fixture(scope="module")
def subaccount_cap(mockapp):
    tenant = tenant_arg("fvcu")
    profile = profile_for(tenant.product, tenant.product_version)
    policy = Policy.load("config/policy.json")
    red = Redactor(profile.pii_labels)
    secrets = resolve_secrets(tenant, list(tenant.secrets))
    params = {"member_id": "10234", "account_type": "Money Market", "nickname": "Rainy Day", "deposit": "250.00"}
    broker = HandoffBroker(CONTROL)
    rec = RunRecorder("discovery-scripted", red, root="runs/test")
    surface = make_surface(policy, rec, False)
    t = approve_next("approve", broker, {i.id for i in broker.list()})
    try:
        result = DiscoveryAgent(
            goal="Open a Money Market sub-account", tenant=tenant, profile=profile, policy=policy, surface=surface,
            recorder=rec, broker=broker, redactor=red, params=params, secrets=secrets,
            entry_url=tenant.base_url + "/login", model="scripted-test-double",
            client=ScriptedClient(subaccount_script())).run()
    finally:
        surface.close()
    t.join(2)
    assert result.status == "success", result.summary
    cap = Compiler(tenant=tenant, profile=profile, redactor=red, params=params).compile(
        result, capability_id="ledgerline.member.open_subaccount", version="1.0.0")
    return cap


def test_irreversible_step_is_flagged(subaccount_cap):
    cap = subaccount_cap
    submit = next(s for s in cap.steps if s.id == "click_submit_request")
    assert submit.risk == "irreversible"
    assert cap.side_effects == "irreversible" and cap.idempotent is False
    assert "requires approval" in cap.tool_definition()["description"]
    assert {p.name for p in cap.inputs} == {"member_id", "account_type", "nickname", "deposit"}


def test_replay_requires_approval(subaccount_cap):
    approved = subaccount_cap.model_copy(update={"approval": subaccount_cap.approval.model_copy(
        update={"status": "approved", "reviewed_by": "t", "reviewed_at": now_utc()})})
    inputs = {"member_id": "10235", "account_type": "Holiday Club", "nickname": "Gifts", "deposit": "40.00"}
    r, _ = run(approved, inputs)
    assert r.status == "failed" and r.error.code == "APPROVAL_REQUIRED" and r.error.step_id == "click_submit_request"

    broker = HandoffBroker(CONTROL)
    t = approve_next("deny", broker, {i.id for i in broker.list()})
    r, _ = run(approved, inputs, on_failure="escalate", operator_timeout_s=60)
    t.join(2)
    assert r.error.code == "APPROVAL_DENIED"

    r, _ = run(approved, inputs, approve_irreversible=True)
    assert r.status == "succeeded", r.error
    assert r.outputs["confirmation_number"].startswith("CNF-")


def test_draft_irreversible_never_runs(subaccount_cap):
    inputs = {"member_id": "10235", "account_type": "Holiday Club", "nickname": "Gifts", "deposit": "40.00"}
    r, _ = run(subaccount_cap, inputs, allow_draft=True)
    assert r.error.code == "CAPABILITY_NOT_APPROVED"
