"""Loading config files, and applying a tenant's overrides to a capability."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

from .schema import TEMPLATE_RE, AppProfile, Capability, TenantBinding


def load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_capability(path: str | Path) -> Capability:
    return Capability.model_validate(load_json(path))


def save_capability(cap: Capability, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(cap.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
    return p


def load_tenant(path: str | Path) -> TenantBinding:
    return TenantBinding.model_validate(load_json(path))


def load_profile(path: str | Path) -> AppProfile:
    return AppProfile.model_validate(load_json(path))


def load_dotenv(path: str | Path = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# --- versions ---
def _ver(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def version_in_range(version: str, spec: str) -> bool:
    ops = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b,
           "<": lambda a, b: a < b, "==": lambda a, b: a == b}
    v = _ver(version)
    for clause in (c.strip() for c in spec.split(",") if c.strip()):
        for op in (">=", "<=", "==", ">", "<"):
            if clause.startswith(op):
                bound = _ver(clause[len(op):].strip())
                n = max(len(v), len(bound))
                if not ops[op](v + (0,) * (n - len(v)), bound + (0,) * (n - len(bound))):
                    return False
                break
    return True


# --- specialise ---
def _merge(base: Any, patch: Any) -> Any:
    """JSON merge patch (RFC 7396)."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    out = copy.deepcopy(base) if isinstance(base, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = _merge(out.get(k), v)
    return out


class CompatibilityError(ValueError):
    pass


def specialize(cap: Capability, tenant: TenantBinding) -> tuple[Capability, list[str]]:
    """Apply a tenant's overrides to the base capability. Returns the result plus a
    readable list of what got overridden."""
    if tenant.product != cap.app.product:
        raise CompatibilityError(f"tenant runs {tenant.product}, capability targets {cap.app.product}")
    if not version_in_range(tenant.product_version, cap.app.versions):
        raise CompatibilityError(
            f"tenant {tenant.tenant_id} runs {cap.app.product} {tenant.product_version}, "
            f"capability supports {cap.app.versions}"
        )
    ov = tenant.overrides.get(cap.id)
    if not ov:
        return cap, []
    data = cap.model_dump(mode="json")
    applied = []
    steps = {s["id"]: s for s in data["steps"]}
    for sid, patch in ov.steps.items():
        if sid not in steps:
            raise CompatibilityError(f"override for unknown step '{sid}'")
        steps[sid].update(_merge(steps[sid], patch))
        applied.append(f"step {sid}: {json.dumps(patch)}")
    if ov.success:
        data["success"] = _merge(data["success"], ov.success)
        applied.append("success condition")
    data["outcomes"] = data["outcomes"] + [o.model_dump(mode="json") for o in ov.extra_outcomes]
    data["fingerprint"] = None  # it's a derived copy now, so no fingerprint
    return Capability.model_validate(data), applied


# --- templating ---
def resolve_secrets(tenant: TenantBinding, names: list[str]) -> dict[str, str]:
    out = {}
    for n in names:
        ref = tenant.secrets.get(n)
        if not ref:
            raise KeyError(f"tenant {tenant.tenant_id} has no binding for secret '{n}'")
        kind, _, key = ref.partition(":")
        if kind == "env":
            val = os.environ.get(key)
            if not val:
                raise KeyError(f"secret '{n}' -> env var {key} is not set")
            out[n] = val
        else:
            raise KeyError(f"secret backend '{kind}' not implemented (vault is a documented seam)")
    return out


def make_renderer(inputs: dict[str, Any], secrets: dict[str, str], tenant: TenantBinding):
    ctx = {"inputs": {k: str(v) for k, v in inputs.items()}, "secrets": secrets,
           "tenant": {"base_url": tenant.base_url.rstrip("/")}}

    def render(s: str) -> str:
        return TEMPLATE_RE.sub(lambda m: ctx[m.group(1)][m.group(2)], s)

    return render
