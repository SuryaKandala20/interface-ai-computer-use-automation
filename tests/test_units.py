"""Unit tests for the schema, locators, redaction, policy and tenant overrides."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cua.locator import holds, resolve
from cua.policy import Policy
from cua.redact import Redactor
from cua.replay import parse_value
from cua.schema import Capability, Locator, Target, TenantBinding, TextVisible
from cua.surface.base import Control, FrameView, Observation
from cua.tenancy import specialize, version_in_range


def minimal_cap(**over):
    base = {
        "id": "ledgerline.member.demo", "version": "1.0.0", "title": "t", "description": "d",
        "app": {"product": "ledgerline-core", "versions": ">=7.0,<8.0", "surface": "legacy_web",
                "profile": "ledgerline-core@7"},
        "entry_url": "{{tenant.base_url}}/login", "idempotent": True, "side_effects": "none",
        "inputs": [{"name": "member_id", "type": "string", "description": "m"}],
        "outputs": [{"name": "bal", "type": "decimal", "description": "b"}],
        "steps": [
            {"id": "open", "intent": "open", "action": "navigate", "url": "{{tenant.base_url}}/x"},
            {"id": "fill", "intent": "fill", "action": "fill", "value": "{{inputs.member_id}}",
             "target": {"primary": {"role": "textbox", "name": "Member No."}}},
            {"id": "read", "intent": "read", "action": "extract", "extract": {"output": "bal", "parse": "decimal"},
             "target": {"primary": {"role": "cell", "row": "Share Savings", "column": "Available Balance"}}},
        ],
        "success": {"kind": "text_visible", "text": "Summary"},
        "provenance": {"recorded_at": "2026-01-01T00:00:00Z", "recorded_by": "author:test"},
    }
    base.update(over)
    return base


def test_schema_accepts_minimal():
    Capability.model_validate(minimal_cap())


@pytest.mark.parametrize("mutate,msg", [
    (lambda c: c["steps"][1].update(value="{{inputs.nope}}"), "undeclared inputs.nope"),
    (lambda c: c.update(outputs=[]), "extracted but undeclared"),
    (lambda c: c["steps"].append(dict(c["steps"][0])), "unique"),
    (lambda c: c["steps"][1].update(risk="irreversible"), "side_effects"),
    (lambda c: c.update(outcomes=[{"id": "x", "kind": "recoverable", "code": "X", "message": "m",
                                   "when": {"kind": "text_visible", "text": "x"}}]), "recovery is required"),
    (lambda c: c.update(idempotent=False, outcomes=[{"id": "x", "kind": "recoverable", "code": "X", "message": "m",
                        "when": {"kind": "text_visible", "text": "x"}, "recovery": {"action": "restart"}}]), "non-idempotent"),
    (lambda c: c["steps"][2].pop("extract"), "requires extract"),
])
def test_schema_rejects_inconsistent(mutate, msg):
    c = minimal_cap()
    mutate(c)
    with pytest.raises(ValidationError, match=msg):
        Capability.model_validate(c)


def test_fingerprint_ignores_approval_but_not_content():
    cap = Capability.model_validate(minimal_cap()).sealed()
    approved = cap.model_copy(update={"approval": cap.approval.model_copy(update={"status": "approved"})})
    assert approved.compute_fingerprint() == cap.fingerprint
    assert cap.model_copy(update={"title": "x"}).compute_fingerprint() != cap.fingerprint


def test_tool_definition_contract():
    t = Capability.model_validate(minimal_cap()).tool_definition()
    assert t["name"] == "ledgerline__member__demo"
    assert t["input_schema"]["required"] == ["member_id"]


def obs(controls, text="", frame="main"):
    return Observation(url="http://h/t/x", frames=[FrameView(frame, "http://h/t/x", "", ["Summary"], text)],
                       controls=controls)


def C(ref, role, name, **kw):
    return Control(ref=ref, role=role, name=name, name_source="content", frame=kw.pop("frame", "main"), **kw)


def test_locator_prefers_primary_and_reports_fallback():
    o = obs([C("a", "button", "Find", native_id="go")])
    loc = Locator(primary=Target(role="button", name="Search"), fallbacks=[Target(role="button", native_id="go")])
    r = resolve(loc, o)
    assert r.control.ref == "a" and r.used_fallback


def test_locator_never_guesses_on_ambiguity():
    o = obs([C("a", "link", "Select", row=["1", "x"]), C("b", "link", "Select", row=["2", "y"])])
    assert resolve(Locator(primary=Target(role="link", name="Select")), o).ambiguous
    r = resolve(Locator(primary=Target(role="link", name="Select", row="{{inputs.id}}")), o,
                lambda s: s.replace("{{inputs.id}}", "2"))
    assert r.control.ref == "b"


def test_cell_by_row_and_column():
    o = obs([C("x", "cell", "$1.00", row=["S01", "Share Savings", "$1.00"], column="Available Balance", col_index=2),
             C("y", "cell", "$9.00", row=["D10", "Checking", "$9.00"], column="Available Balance", col_index=2)])
    t = Target(role="cell", row="Share Savings", column="Available Balance")
    assert resolve(Locator(primary=t), o).control.ref == "x"


def test_conditions():
    o = obs([], text="ERROR 1107: INVALID")
    assert holds(TextVisible(text="ERROR \\d{4}:", match="regex"), o)
    assert not holds(TextVisible(text="nothing"), o)


def test_redactor_layers():
    r = Redactor(["Member Name", "SSN"])
    r.register_secret("app.password", "hunter2!")
    o = obs([C("n", "cell", "DOE, JANE", row=["Member Name", "DOE, JANE"], col_index=1)])
    r.learn(o)
    out = r.text("pw hunter2! name DOE, JANE ssn 123-45-6789 mail a@b.co phone (541) 555-0142")
    assert "hunter2!" not in out and "DOE, JANE" not in out and "123-45-6789" not in out
    assert "[SECRET:app.password]" in out and "[PII:Member Name]" in out and "***-**-6789" in out
    assert "[EMAIL]" in out and "[PHONE]" in out
    assert r.text("ledgerline.member.x@1.0.0") == "ledgerline.member.x@1.0.0"  # capability refs aren't emails


def test_policy():
    p = Policy.load("config/policy.json")
    assert p.url_allowed("http://127.0.0.1:8765/t/fvcu/login")[0]
    assert not p.url_allowed("https://evil.example/t/fvcu/login")[0]
    assert not p.url_allowed("http://127.0.0.1:8765/__chaos")[0]
    assert not p.url_allowed("http://127.0.0.1:8765/t/fvcu/logout")[0]
    submit = C("s", "button", "Submit Request")
    d = p.check_action("click", control=submit)
    assert d.allowed and d.needs_approval and d.risk == "irreversible"
    assert not p.check_action("download", control=submit).allowed
    assert p.check_action("click", control=C("q", "button", "Search")).risk == "safe"


def test_parse_value():
    assert parse_value("$12,425.18", "decimal") == "12425.18"
    assert parse_value("($5.00)", "decimal") == "-5.00"
    assert parse_value(" 42 ", "integer") == 42


def test_versions_and_specialize():
    assert version_in_range("7.4", ">=7.0,<8.0") and not version_in_range("8.0", ">=7.0,<8.0")
    cap = Capability.model_validate(minimal_cap())
    t = TenantBinding(tenant_id="x", display_name="X", product="ledgerline-core", product_version="7.1",
                      base_url="http://h", overrides={"ledgerline.member.demo": {
                          "steps": {"fill": {"target": {"primary": {"name": "Member #"}}}}}})
    spec, applied = specialize(cap, t)
    assert spec.steps[1].target.primary.name == "Member #" and spec.steps[1].target.primary.role == "textbox"
    assert applied
    with pytest.raises(ValidationError):
        TenantBinding(tenant_id="x", display_name="X", product="p", product_version="1", base_url="h",
                      secrets={"pw": "literal-password"})
