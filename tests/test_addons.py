"""Tests for the add-ons: learning from handoffs, and the stability check."""

from __future__ import annotations

import threading
import time

import pytest

from cua import stability
from cua.control import HandoffBroker, Intervention, Resolution
from cua.learn import LearnError, accept, propose
from cua.policy import Policy
from cua.schema import now_utc
from cua.tenancy import load_profile
from test_e2e import CONTROL, artifact, chaos, run  # noqa: F401  (artifact is a fixture)

PROFILE = load_profile("config/profiles/ledgerline-core-7.json")
POLICY = Policy.load("config/policy.json")


def approved(cap):
    return cap.model_copy(update={"approval": cap.approval.model_copy(
        update={"status": "approved", "reviewed_by": "test", "reviewed_at": now_utc()})})


def fake_intervention(cap, clicks, mode="step_done", heading="Concurrent Session Warning"):
    return Intervention(
        id="iv-test", run_id="r", kind="takeover", status="resolved", subject=cap.ref, step_id="click_select",
        reason_code="CHECKPOINT_FAILED", reason="x",
        observed={"frames": {"main": {"path": "/t/fvcu/member", "headings": [heading]}}},
        human_actions=[{"source": "recorder", "type": "click", "role": "button", "name": n, "frame": "main"}
                       for n in clicks],
        resolution=Resolution(mode=mode, operator="op"),
    )


# --- learning: unit ---
def test_propose_single_safe_click(artifact):
    p = propose(fake_intervention(artifact, ["Continue Anyway"]), artifact, policy=POLICY, profile=PROFILE)
    assert p.rule.kind == "recoverable" and p.rule.applies_to == ["click_select"]
    assert p.rule.when.text == "Concurrent Session Warning"
    assert p.rule.recovery.action == "dismiss" and p.rule.recovery.target.name == "Continue Anyway"


@pytest.mark.parametrize("clicks,mode,match", [
    (["Submit Request"], "step_done", "irreversible"),
    (["Continue Anyway", "Return"], "step_done", "single-click"),
    (["Continue Anyway"], "abort", "abort"),
])
def test_propose_refuses_unsafe_or_ambiguous(artifact, clicks, mode, match):
    with pytest.raises(LearnError, match=match):
        propose(fake_intervention(artifact, clicks, mode), artifact, policy=POLICY, profile=PROFILE)


def test_accept_makes_new_draft_version(artifact):
    base = approved(artifact).sealed()
    p = propose(fake_intervention(base, ["Continue Anyway"]), base, policy=POLICY, profile=PROFILE)
    new = accept(p, base, reviewer="rev")
    assert new.version == "1.1.0" and new.approval.status == "draft"
    assert new.fingerprint != base.fingerprint and p.rule.id in {r.id for r in new.outcomes}
    assert [s.id for s in new.steps] == [s.id for s in base.steps]  # steps untouched


# --- learning: end to end ---
def test_human_fix_becomes_automatic_recovery(artifact, mockapp):
    """Human fixes it once, we learn a rule from that, and v1.1.0 handles it on its own."""
    from playwright.sync_api import sync_playwright

    base = approved(artifact).sealed()
    broker = HandoffBroker(CONTROL)
    before = {i.id for i in broker.list()}

    def operator():
        deadline = time.time() + 60
        while time.time() < deadline:
            new = [i for i in broker.list({"open"}) if i.id not in before]
            if new:
                iv = broker.claim(new[0].id, "operator.test")
                with sync_playwright() as pw:
                    page = pw.chromium.connect_over_cdp(iv.live_endpoint).contexts[0].pages[0]
                    next(f for f in page.frames if f.name == "main").get_by_role(
                        "button", name="Continue Anyway").click()
                    time.sleep(1.0)
                broker.resolve(iv.id, "step_done", "operator.test")
                return
            time.sleep(0.3)

    t = threading.Thread(target=operator, daemon=True)
    t.start()
    chaos(mockapp, "unknown_dialog_once")
    try:
        r1, _ = run(base, {"member_id": "10235"}, on_failure="escalate", operator_timeout_s=90)
    finally:
        chaos(mockapp, None)
    t.join(5)
    assert r1.status == "succeeded" and r1.interventions

    proposal = propose(broker.get(r1.interventions[0]["id"]), base, policy=POLICY, profile=PROFILE)
    v11 = approved(accept(proposal, base, reviewer="rev"))

    chaos(mockapp, "unknown_dialog_once")
    try:
        r2, _ = run(v11, {"member_id": "10235"})  # no escalation this time, nobody is around to help
    finally:
        chaos(mockapp, None)
    assert r2.status == "succeeded", r2.error
    assert [x["code"] for x in r2.recoveries] == ["CONCURRENT_SESSION_WARNING"] and not r2.interventions


# --- stability ---
def test_stability_consistent_and_gates(artifact):
    cap = approved(artifact)
    report = stability.measure(cap, 3, lambda: run(cap, {"member_id": "10235"})[0])
    assert report["consistent"] and report["success_rate"] == 1.0 and report["runs"] == 3
    assert len(report["outputs_seen"]) == 1  # same answer every run
    assert stability.gate(cap, 0.9, report)[0]
    changed = cap.model_copy(update={"title": "edited"})
    assert not stability.gate(changed, 0.9, report)[0]          # report only counts for the exact same content
    assert not stability.gate(cap, 0.9, None)[0]


def test_stability_refuses_side_effects(artifact):
    risky = artifact.model_copy(update={"side_effects": "irreversible"})
    with pytest.raises(stability.StabilityError):
        stability.measure(risky, 3, lambda: None)
