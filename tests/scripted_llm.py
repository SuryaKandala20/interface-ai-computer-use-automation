"""Fake Anthropic client for tests. Not used for the evidence runs.

It reads the observation text the agent sends, finds refs by role/label with a
regex, and returns one tool call per turn. That exercises the real discovery code,
policy checks, recorder and compiler without needing the network.
"""

from __future__ import annotations

import itertools
import re
from types import SimpleNamespace
from typing import Any, Callable, Optional

Step = Callable[[str], tuple[str, dict[str, Any]]]
_ids = itertools.count(1)


def ref(obs: str, role: str, name: str) -> str:
    m = re.search(r"\[(f\d+e\d+)\] " + re.escape(role) + r" '" + re.escape(name) + "'", obs)
    if not m:
        raise AssertionError(f"scripted agent could not find {role} '{name}' in observation:\n{obs[:1500]}")
    return m.group(1)


def cell(obs: str, row_contains: str, col: int) -> str:
    for line in obs.splitlines():
        if re.match(r"\s*t\d+r\d+:", line) and row_contains in line:
            refs = re.findall(r"\[(f\d+e\d+)\]", line)
            return refs[col]
    raise AssertionError(f"no table row containing {row_contains!r}")


def balance_script(row_label: str = "Share Savings") -> list[Step]:
    return [
        lambda o: ("fill", {"ref": ref(o, "textbox", "Operator ID"), "secret": "app.operator_id", "reason": "Enter operator id"}),
        lambda o: ("fill", {"ref": ref(o, "password", "Password"), "secret": "app.password", "reason": "Enter password"}),
        lambda o: ("click", {"ref": ref(o, "button", "Sign On"), "reason": "Sign on"}),
        lambda o: ("click", {"ref": ref(o, "link", "Member Inquiry"), "reason": "Open member inquiry"}),
        lambda o: ("fill", {"ref": ref(o, "textbox", "Member No."), "param": "member_id", "reason": "Enter member number"}),
        lambda o: ("click", {"ref": ref(o, "button", "Search"), "reason": "Search for the member"}),
        lambda o: ("click", {"ref": ref(o, "link", "Select"), "reason": "Open the member record"}),
        lambda o: ("extract", {"ref": cell(o, row_label, 4), "output_name": "available_balance",
                               "output_type": "decimal", "description": "Available balance of the Share Savings account",
                               "row_label": row_label, "reason": "Read the savings available balance"}),
        lambda o: ("finish", {"status": "success", "summary": "Read the Share Savings available balance for member 10234",
                              "capability_title": "Get share savings available balance",
                              "capability_description": "Look up a member by member number and return the available balance of their Share Savings account.",
                              "parameter_descriptions": {"member_id": "Credit union member number (digits)"}}),
    ]


class ScriptedClient:
    def __init__(self, script: list[Step]):
        self.script = list(script)
        self.calls = 0
        self.messages = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    @staticmethod
    def _last_observation(messages: list[dict[str, Any]]) -> str:
        content = messages[-1]["content"]
        if isinstance(content, str):
            return content
        return "\n".join(c["content"] for c in content if isinstance(c, dict) and c.get("type") == "tool_result")

    def _create(self, **kw: Any):
        obs = self._last_observation(kw["messages"])
        step = self.script[self.calls]
        self.calls += 1
        name, args = step(obs)
        block = SimpleNamespace(type="tool_use", id=f"toolu_{next(_ids)}", name=name, input=args)
        return SimpleNamespace(content=[block], stop_reason="tool_use", model="scripted-test-double",
                               usage=SimpleNamespace(input_tokens=0, output_tokens=0, cache_read_input_tokens=0))
