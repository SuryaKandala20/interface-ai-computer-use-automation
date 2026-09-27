"""Finding targets and checking conditions against an Observation.

No browser code in here, just plain functions, so they're easy to test and behave
the same for any surface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

from .schema import AllOf, AnyOf, ControlPresent, Locator, Target, TextVisible, UrlMatches
from .surface.base import Control, Observation

Render = Callable[[str], str]


def _norm(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().casefold()


def _name_ok(t: Target, c: Control, render: Render) -> bool:
    if t.name is None:
        return True
    want = render(t.name)
    if t.name_match == "regex":
        return re.search(want, c.name or "", re.I) is not None
    if t.name_match == "contains":
        return _norm(want) in _norm(c.name)
    return _norm(want) == _norm(c.name)


def matches(t: Target, c: Control, render: Render = lambda s: s) -> bool:
    if t.role != c.role:
        return False
    if t.frame is not None and t.frame != c.frame:
        return False
    if t.native_id is not None and t.native_id != c.native_id:
        return False
    if t.row is not None and _norm(render(t.row)) not in {_norm(x) for x in (c.row or [])}:
        return False
    if t.column is not None and _norm(t.column) != _norm(c.column):
        return False
    if t.column_index is not None and t.column_index != c.col_index:
        return False
    return _name_ok(t, c, render)


def candidates(t: Target, obs: Observation, render: Render = lambda s: s) -> list[Control]:
    return [c for c in obs.controls if matches(t, c, render)]


@dataclass
class Resolution:
    control: Optional[Control]
    strategy: int = -1           # 0 = primary, n = fallbacks[n-1]
    ambiguous: bool = False
    counts: tuple[int, ...] = ()

    @property
    def used_fallback(self) -> bool:
        return self.strategy > 0


def resolve_target(t: Target, obs: Observation, render: Render = lambda s: s) -> tuple[Optional[Control], int]:
    found = candidates(t, obs, render)
    if t.nth is not None:
        return (found[t.nth] if t.nth < len(found) else None), len(found)
    return (found[0] if len(found) == 1 else None), len(found)


def resolve(loc: Locator, obs: Observation, render: Render = lambda s: s) -> Resolution:
    """Try each strategy in order. If one matches more than one control we skip it
    rather than guess, and only report it as ambiguous if nothing else works."""
    counts = []
    ambiguous = False
    for i, t in enumerate([loc.primary, *loc.fallbacks]):
        ctrl, n = resolve_target(t, obs, render)
        counts.append(n)
        if ctrl is not None:
            return Resolution(ctrl, i, False, tuple(counts))
        ambiguous = ambiguous or n > 1
    return Resolution(None, -1, ambiguous, tuple(counts))


def holds(cond, obs: Observation, render: Render = lambda s: s) -> bool:
    if isinstance(cond, TextVisible):
        want = render(cond.text)
        for f in obs.frame(cond.frame):
            if cond.match == "regex":
                if re.search(want, f.text, re.I):
                    return True
            elif _norm(want) in _norm(f.text):
                return True
        return False
    if isinstance(cond, ControlPresent):
        return bool(candidates(cond.target, obs, render))
    if isinstance(cond, UrlMatches):
        pat = render(cond.pattern)
        for f in obs.frame(cond.frame):
            path = re.sub(r"^[a-z]+://[^/]+", "", f.url)
            if re.search(pat, path):
                return True
        return False
    if isinstance(cond, AllOf):
        return all(holds(c, obs, render) for c in cond.conditions)
    if isinstance(cond, AnyOf):
        return any(holds(c, obs, render) for c in cond.conditions)
    raise TypeError(f"unknown condition {cond!r}")


def describe_condition(cond) -> str:
    if isinstance(cond, TextVisible):
        where = f" in frame '{cond.frame}'" if cond.frame else ""
        return f"text '{cond.text}' visible{where}"
    if isinstance(cond, ControlPresent):
        return f"{describe_target(cond.target)} present"
    if isinstance(cond, UrlMatches):
        return f"url matches /{cond.pattern}/"
    if isinstance(cond, AllOf):
        return " AND ".join(describe_condition(c) for c in cond.conditions)
    if isinstance(cond, AnyOf):
        return " OR ".join(describe_condition(c) for c in cond.conditions)
    return repr(cond)


def describe_target(t: Target) -> str:
    bits = [t.role]
    if t.name:
        bits.append(f"'{t.name}'")
    if t.row:
        bits.append(f"in row '{t.row}'")
    if t.column:
        bits.append(f"under '{t.column}'")
    if t.column_index is not None:
        bits.append(f"at column #{t.column_index}")
    if t.native_id:
        bits.append(f"[native_id={t.native_id}]")
    if t.frame:
        bits.append(f"(frame {t.frame})")
    return " ".join(bits)
