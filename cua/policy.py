"""Guardrails: what automation is allowed to touch and do.

Checked in two separate places so one bug can't get around it:
  1. network: every browser request (agent, replay, or a human during a handoff)
     goes through url_allowed in a route handler
  2. actions: every agent/replay action goes through check_action first
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from .surface.base import Control


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_origins: list[str]
    allowed_path_patterns: list[str] = Field(default_factory=lambda: [r"^/"])
    blocked_path_patterns: list[str] = Field(default_factory=list)
    allowed_actions: list[str] = Field(default_factory=lambda: ["navigate", "fill", "select", "click", "extract"])
    irreversible_patterns: list[str] = Field(
        default_factory=list, description="Regexes over control names that commit a change."
    )
    irreversible_mode: Literal["require_approval", "block"] = "require_approval"
    max_discovery_steps: int = 30
    max_discovery_seconds: int = 600

    @classmethod
    def load(cls, path: str | Path) -> "Policy":
        return cls.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))

    # --- network ---
    def url_allowed(self, url: str) -> tuple[bool, str]:
        if url.startswith(("about:", "data:")):
            return True, "internal"
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self.allowed_origins:
            return False, f"origin {origin} not in allowlist"
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        for pat in self.blocked_path_patterns:
            if re.search(pat, path):
                return False, f"path matches blocked pattern {pat}"
        if not any(re.search(p, path) for p in self.allowed_path_patterns):
            return False, "path not in allowlist"
        return True, "allowed"

    # --- actions ---
    def classify(self, control: Optional[Control], extra_patterns: list[str] = ()) -> str:
        if control is None or control.role not in ("button", "link"):
            return "safe"
        for pat in [*self.irreversible_patterns, *extra_patterns]:
            if re.search(pat, control.name or "", re.I):
                return "irreversible"
        return "safe"

    def check_action(
        self,
        action: str,
        *,
        control: Optional[Control] = None,
        url: Optional[str] = None,
        declared_risk: str = "safe",
        extra_patterns: list[str] = (),
    ) -> "Decision":
        if action not in self.allowed_actions:
            return Decision(False, "safe", False, f"action '{action}' not allowed by policy")
        if url is not None:
            ok, why = self.url_allowed(url)
            if not ok:
                return Decision(False, "safe", False, why)
        risk = "irreversible" if "irreversible" in (declared_risk, self.classify(control, extra_patterns)) else "safe"
        if risk == "irreversible":
            if self.irreversible_mode == "block":
                return Decision(False, risk, False, "irreversible actions are blocked by policy")
            return Decision(True, risk, True, "irreversible action requires human approval")
        return Decision(True, risk, False, "allowed")


@dataclass
class Decision:
    allowed: bool
    risk: str
    needs_approval: bool
    reason: str
