"""Redaction for anything we save or send to the model.

Three passes, in this order:
  1. secrets: the exact credential values for this run become [SECRET:<name>]
  2. PII by field: values shown next to a PII label from the vendor profile
     ("Member Name", "SSN", ...) become [PII:<label>]
  3. PII by pattern: SSN, card, email and phone shapes anywhere in the text

Pass 2 is the important one. No regex will spot "DOE, JANE A" as a name, but the
page tells us it's the Member Name.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .surface.base import Observation

PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b\d{3}-\d{2}-(\d{4})\b"), r"***-**-\1"),                      # SSN, keep last 4
    (re.compile(r"\b(?:\d[ -]?){12,15}(\d{4})\b"), r"[CARD ****\1]"),            # card number
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}(?![\w.])"), "[EMAIL]"),
    (re.compile(r"\(\d{3}\)\s?\d{3}-\d{4}"), "[PHONE]"),
    (re.compile(r"(?i)\b(bearer|token|session|sessid)[=:]\s*[\w.-]{8,}"), r"\1=[TOKEN]"),
]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().rstrip(":").casefold()


class Redactor:
    def __init__(self, pii_labels: Iterable[str] = ()) -> None:
        self.pii_labels = {_norm(x) for x in pii_labels}
        self._exact: dict[str, str] = {}

    def register_secret(self, name: str, value: str) -> None:
        if value:
            self._exact[value] = f"[SECRET:{name}]"

    def register_value(self, value: str, label: str) -> None:
        if value and len(value.strip()) >= 3:
            self._exact.setdefault(value.strip(), f"[PII:{label}]")

    def learn(self, obs: Observation) -> None:
        """Remember values that sit next to PII labels (key/value rows or PII columns)."""
        for c in obs.controls:
            if c.role != "cell" or not c.row:
                continue
            label = c.row[0] if c.col_index and c.col_index > 0 else None
            if label and _norm(label) in self.pii_labels:
                self.register_value(c.name, label.rstrip(":"))
            elif c.column and _norm(c.column) in self.pii_labels:
                self.register_value(c.name, c.column)

    def text(self, s: str) -> str:
        if not s:
            return s
        for value in sorted(self._exact, key=len, reverse=True):
            s = s.replace(value, self._exact[value])
        for pat, repl in PATTERNS:
            s = pat.sub(repl, s)
        return s

    def obj(self, o: Any) -> Any:
        if isinstance(o, str):
            return self.text(o)
        if isinstance(o, dict):
            return {k: self.obj(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [self.obj(v) for v in o]
        return o

    def sensitive_strings(self) -> list[str]:
        """Exact on-screen values to black out in screenshots."""
        return [v for v, tag in self._exact.items() if tag.startswith("[PII")]

    def screen_patterns(self) -> list[re.Pattern[str]]:
        """Patterns to black out in screenshots as well (SSN, card, email, phone)."""
        return [p for p, _ in PATTERNS[:4]]

    def observation(self, obs: Observation) -> Observation:
        """Redacted copy of an observation. This is what the model and the logs see."""
        from dataclasses import replace

        return Observation(
            url=obs.url,
            frames=[replace(f, text=self.text(f.text), headings=[self.text(h) for h in f.headings]) for f in obs.frames],
            controls=[
                replace(c, name=self.text(c.name), value=self.text(c.value) if c.value else c.value,
                        row=[self.text(x) for x in c.row] if c.row else c.row)
                for c in obs.controls
            ],
        )
