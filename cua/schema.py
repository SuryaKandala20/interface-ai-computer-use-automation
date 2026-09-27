"""Schema for capability artifacts (v1.0).

A capability is what an agent calls: a typed, versioned description of a UI flow
that we can replay without the LLM. It doesn't keep the model transcript (just
where it came from), the tenant's URL or credentials (those come from the
TenantBinding at run time), or anything DOM specific (targets are role + label +
table position, and the surface adapter resolves them).

AppProfile and TenantBinding live here too, since the three always get loaded
together.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "1.0"
TEMPLATE_RE = re.compile(r"\{\{\s*(inputs|secrets|tenant)\.([a-zA-Z0-9_.]+)\s*\}\}")
SEMVER_RE = r"^\d+\.\d+\.\d+$"
IDENT_RE = r"^[a-z][a-z0-9_]*$"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)


# --- contract types ---
class ParamType(str, Enum):
    string = "string"
    integer = "integer"
    decimal = "decimal"
    boolean = "boolean"
    enum = "enum"


class Sensitivity(str, Enum):
    """How a value is treated in logs. public/internal can be logged, pii gets masked,
    secret is never written anywhere or shown to the model."""

    public = "public"
    internal = "internal"
    pii = "pii"
    secret = "secret"


class ParamSpec(Strict):
    name: str = Field(pattern=IDENT_RE)
    type: ParamType
    description: str
    required: bool = True
    pattern: Optional[str] = Field(None, description="Regex the value must fully match (checked before any UI action).")
    enum: Optional[list[str]] = None
    sensitivity: Sensitivity = Sensitivity.internal
    example: Optional[str] = Field(None, description="Synthetic example only - never a real value.")

    def json_schema(self) -> dict[str, Any]:
        base: dict[str, Any] = {"description": self.description}
        if self.type == ParamType.enum.value:
            base.update(type="string", enum=self.enum or [])
        elif self.type == ParamType.decimal.value:
            base.update(type="string", pattern=r"^-?\d+(\.\d+)?$")
        else:
            base["type"] = {"integer": "integer", "boolean": "boolean"}.get(self.type, "string")
        if self.pattern and self.type == ParamType.string.value:
            base["pattern"] = self.pattern
        return base


class SecretSpec(Strict):
    """A credential the flow needs. We only store the name here. The real value is
    looked up per tenant when the flow runs and typed in by the executor, not the model."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_.]*$")
    description: str


class OutputType(str, Enum):
    string = "string"
    decimal = "decimal"
    integer = "integer"


class OutputSpec(Strict):
    name: str = Field(pattern=IDENT_RE)
    type: OutputType
    description: str
    sensitivity: Sensitivity = Sensitivity.internal


# --- targeting ---
class Role(str, Enum):
    textbox = "textbox"
    password = "password"
    button = "button"
    link = "link"
    combobox = "combobox"
    checkbox = "checkbox"
    radio = "radio"
    cell = "cell"


class Target(Strict):
    """One control, described the way an operator would say it: "the Search button",
    "the textbox labelled Member No.", "the Available Balance cell in the Share Savings
    row". Every field that is set has to match.

    A target only resolves if exactly one control matches (or nth picks one). If
    several match we fail rather than guess. See locator.py.
    """

    role: Role
    name: Optional[str] = Field(None, description="Accessible name or visual label (template allowed).")
    name_match: Literal["exact", "contains", "regex"] = "exact"
    frame: Optional[str] = Field(None, description="Frame/window path, e.g. 'main'. None = top level.")
    row: Optional[str] = Field(None, description="Text of any cell in the same table row (template allowed).")
    column: Optional[str] = Field(None, description="Column header text for table cells.")
    column_index: Optional[int] = Field(
        None, ge=0, description="Cell position in its row, for header-less key/value tables."
    )
    native_id: Optional[str] = Field(
        None, description="Surface-native identifier: web form field name, UIA AutomationId, ..."
    )
    nth: Optional[int] = Field(None, ge=0, description="Disambiguate identical matches (last resort).")


class Locator(Strict):
    """Primary target first, then fallbacks. When a fallback gets used we log a
    warning, since it usually means the UI changed."""

    primary: Target
    fallbacks: list[Target] = Field(default_factory=list)
    rationale: str = ""


# --- conditions ---
class TextVisible(Strict):
    kind: Literal["text_visible"] = "text_visible"
    text: str
    match: Literal["contains", "regex"] = "contains"
    frame: Optional[str] = None


class ControlPresent(Strict):
    kind: Literal["control_present"] = "control_present"
    target: Target


class UrlMatches(Strict):
    kind: Literal["url_matches"] = "url_matches"
    pattern: str = Field(description="Regex over the URL path+query of the frame.")
    frame: Optional[str] = None


class AllOf(Strict):
    kind: Literal["all_of"] = "all_of"
    conditions: list["Condition"]


class AnyOf(Strict):
    kind: Literal["any_of"] = "any_of"
    conditions: list["Condition"]


Condition = Annotated[
    Union[TextVisible, ControlPresent, UrlMatches, AllOf, AnyOf], Field(discriminator="kind")
]
AllOf.model_rebuild()
AnyOf.model_rebuild()


# --- steps ---
class Action(str, Enum):
    navigate = "navigate"
    fill = "fill"
    select = "select"
    click = "click"
    extract = "extract"


class Risk(str, Enum):
    safe = "safe"                  # read-only, or easy to undo (typing, navigating)
    irreversible = "irreversible"  # actually changes data in the core system


class Extraction(Strict):
    output: str = Field(pattern=IDENT_RE)
    parse: Literal["text", "decimal", "integer"] = "text"


class Step(Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    intent: str = Field(description="Plain-language purpose, for reviewers and error messages.")
    action: Action
    target: Optional[Locator] = None
    url: Optional[str] = Field(None, description="navigate only; template allowed.")
    value: Optional[str] = Field(None, description="fill/select only; template allowed.")
    extract: Optional[Extraction] = None
    risk: Risk = Risk.safe
    expect: Optional[Condition] = Field(None, description="Checkpoint that must hold after the action.")
    timeout_ms: int = Field(10_000, ge=500, le=120_000)
    source: Literal["agent", "human", "author"] = "agent"

    @model_validator(mode="after")
    def _shape(self) -> "Step":
        a = self.action
        if a == Action.navigate.value:
            if not self.url:
                raise ValueError(f"step {self.id}: navigate requires url")
        elif self.target is None:
            raise ValueError(f"step {self.id}: {a} requires target")
        if a in (Action.fill.value, Action.select.value) and self.value is None:
            raise ValueError(f"step {self.id}: {a} requires value")
        if a == Action.extract.value and self.extract is None:
            raise ValueError(f"step {self.id}: extract requires extract spec")
        return self


# --- outcomes ---
class OutcomeKind(str, Enum):
    business = "business"        # a real answer the caller has to handle, e.g. no such member
    recoverable = "recoverable"  # we can fix it and keep going (notice page, expired session)
    failure = "failure"          # stop and report it (app crashed)


class Recovery(Strict):
    action: Literal["dismiss", "restart", "wait"]
    target: Optional[Target] = Field(None, description="dismiss: the control that clears the condition.")
    from_step: Optional[str] = Field(
        None, description="restart: step id to re-run from; None = from the first step. Idempotent flows only."
    )
    max_attempts: int = Field(2, ge=1, le=5)


class OutcomeRule(Strict):
    id: str
    kind: OutcomeKind
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    when: Condition
    message: str
    applies_to: Optional[list[str]] = Field(None, description="Step ids; None = every step.")
    recovery: Optional[Recovery] = None

    @model_validator(mode="after")
    def _recovery_iff_recoverable(self) -> "OutcomeRule":
        if (self.kind == OutcomeKind.recoverable.value) != (self.recovery is not None):
            raise ValueError(f"outcome {self.id}: recovery is required iff kind == recoverable")
        return self


# --- capability ---
class AppRef(Strict):
    product: str = Field(description="Vendor product key, shared by every tenant that runs it.")
    versions: str = Field(description="Product versions this capability is valid for, e.g. '>=7.0,<8.0'.")
    surface: Literal["web", "legacy_web", "desktop", "terminal"]
    profile: str = Field(description="AppProfile id providing shared outcome rules / PII fields.")


class Provenance(Strict):
    recorded_at: datetime
    recorded_by: str = Field(description="'discovery-agent' | 'human:<id>' | 'author:<id>'")
    model: Optional[str] = None
    discovery_run_id: Optional[str] = None
    source_tenant: Optional[str] = None
    goal: Optional[str] = Field(None, description="Redacted natural-language goal.")
    human_steps: int = 0


class Approval(Strict):
    status: Literal["draft", "approved", "deprecated"] = "draft"
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    notes: Optional[str] = None


class Capability(Strict):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*(\.[a-z][a-z0-9_]*)+$", description="<product>.<domain>.<verb_noun>")
    version: str = Field(pattern=SEMVER_RE)
    title: str
    description: str = Field(description="What it does / when to call it - written for the calling agent.")
    app: AppRef
    entry_url: str = Field(description="Template, normally '{{tenant.base_url}}/...'.")
    idempotent: bool = Field(description="Safe to re-run from the start (enables 'restart' recovery).")
    side_effects: Literal["none", "irreversible"]
    inputs: list[ParamSpec] = Field(default_factory=list)
    secrets: list[SecretSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    steps: list[Step]
    outcomes: list[OutcomeRule] = Field(default_factory=list)
    success: Condition
    approval: Approval = Field(default_factory=Approval)
    provenance: Provenance
    fingerprint: Optional[str] = Field(None, description="sha256 of the canonical artifact (excl. approval & fingerprint).")

    # sanity checks, so a broken artifact never gets as far as running
    @model_validator(mode="after")
    def _consistency(self) -> "Capability":
        ids = [s.id for s in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("step ids must be unique")
        declared = {
            "inputs": {p.name for p in self.inputs},
            "secrets": {s.name for s in self.secrets},
            "tenant": {"base_url"},
        }
        for text in self._template_strings():
            for ns, name in TEMPLATE_RE.findall(text):
                if name not in declared[ns]:
                    raise ValueError(f"template references undeclared {ns}.{name}")
        extracted = [s.extract.output for s in self.steps if s.extract]
        missing = {o.name for o in self.outputs} - set(extracted)
        if missing:
            raise ValueError(f"outputs never extracted: {sorted(missing)}")
        undeclared = set(extracted) - {o.name for o in self.outputs}
        if undeclared:
            raise ValueError(f"extracted but undeclared outputs: {sorted(undeclared)}")
        for rule in self.outcomes:
            for sid in rule.applies_to or []:
                if sid not in ids:
                    raise ValueError(f"outcome {rule.id} applies_to unknown step {sid}")
            if rule.recovery and rule.recovery.action == "restart":
                if not self.idempotent:
                    raise ValueError(f"outcome {rule.id}: restart recovery on a non-idempotent capability")
                if rule.recovery.from_step and rule.recovery.from_step not in ids:
                    raise ValueError(f"outcome {rule.id}: restart from unknown step")
        irreversible = any(s.risk == Risk.irreversible.value for s in self.steps)
        if irreversible != (self.side_effects == "irreversible"):
            raise ValueError("side_effects must be 'irreversible' iff some step is irreversible")
        return self

    def _template_strings(self) -> list[str]:
        out = [self.entry_url]
        for s in self.steps:
            out += [x for x in (s.url, s.value) if x]
            if s.target:
                for t in [s.target.primary, *s.target.fallbacks]:
                    out += [x for x in (t.name, t.row) if x]
        return out

    # fingerprint and identity
    def compute_fingerprint(self) -> str:
        body = self.model_dump(mode="json", exclude={"approval", "fingerprint"})
        canon = json.dumps(body, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canon.encode()).hexdigest()

    def sealed(self) -> "Capability":
        return self.model_copy(update={"fingerprint": self.compute_fingerprint()})

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    # tool definition for agents
    def tool_definition(self) -> dict[str, Any]:
        """Turn this capability into a tool definition an agent can call."""
        props = {p.name: p.json_schema() for p in self.inputs}
        outs = ", ".join(f"{o.name} ({o.type})" for o in self.outputs) or "nothing"
        biz = sorted({r.code for r in self.outcomes if r.kind == OutcomeKind.business.value})
        desc = f"{self.description}\nReturns: {outs}."
        if biz:
            desc += f" Possible business outcomes: {', '.join(biz)}."
        if self.side_effects == "irreversible":
            desc += " Has irreversible side effects; requires approval."
        return {
            "name": self.id.replace(".", "__").replace("-", "_"),
            "description": desc,
            "input_schema": {
                "type": "object",
                "properties": props,
                "required": [p.name for p in self.inputs if p.required],
                "additionalProperties": False,
            },
        }


# --- shared + tenant config ---
class AppProfile(Strict):
    """What we know about a vendor product that's the same for every tenant running it:
    its error and notice screens, which fields hold PII, and so on. Written once and
    shared by every capability for that product."""

    id: str
    product: str
    versions: str
    outcomes: list[OutcomeRule] = Field(default_factory=list)
    pii_labels: list[str] = Field(default_factory=list, description="Row/field labels whose values are PII.")
    irreversible_labels: list[str] = Field(
        default_factory=list, description="Regexes over control names that commit changes."
    )


class CapabilityOverride(Strict):
    """Per-tenant patch on top of the base artifact, merged by step id (JSON merge
    patch). This is what lets one artifact cover many tenants."""

    steps: dict[str, dict[str, Any]] = Field(default_factory=dict)
    success: Optional[dict[str, Any]] = None
    extra_outcomes: list[OutcomeRule] = Field(default_factory=list)


class TenantBinding(Strict):
    tenant_id: str
    display_name: str
    product: str
    product_version: str
    base_url: str
    secrets: dict[str, str] = Field(
        default_factory=dict, description="secret name -> reference ('env:VAR'). Never a literal."
    )
    overrides: dict[str, CapabilityOverride] = Field(default_factory=dict, description="keyed by capability id")

    @model_validator(mode="after")
    def _no_literal_secrets(self) -> "TenantBinding":
        for k, v in self.secrets.items():
            if not v.startswith(("env:", "vault:")):
                raise ValueError(f"secret {k} must be a reference (env:/vault:), not a literal")
        return self


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)
