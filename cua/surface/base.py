"""The surface interface: the line between "how we see and act on an app" and "the
recorded flow".

Everything above this (locators, conditions, replay, discovery, the compiler) only
deals with Observation and Control. A surface adapter turns a real UI into those:

  - WebSurface (built): Playwright plus an injected perception script, handles frames
  - DesktopSurface (not built): Windows UI Automation tree to Controls (role, name,
    AutomationId as native_id, rows/columns from grid patterns)
  - TerminalSurface (not built): 3270/5250 screen fields to Controls
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol


@dataclass
class Control:
    ref: str                       # only valid for the observation it came from
    role: str
    name: str
    name_source: str               # aria-label | label | content | visual-label | ...
    frame: str                     # "" = top level
    value: Optional[str] = None
    options: Optional[list[str]] = None
    enabled: bool = True
    native_id: Optional[str] = None
    row: Optional[list[str]] = None
    column: Optional[str] = None
    col_index: Optional[int] = None
    table: Optional[int] = None
    row_index: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, [], "")}


@dataclass
class FrameView:
    name: str
    url: str
    title: str
    headings: list[str]
    text: str


@dataclass
class Observation:
    url: str
    frames: list[FrameView]
    controls: list[Control]
    screenshot: Optional[bytes] = field(default=None, repr=False)

    def frame(self, name: Optional[str]) -> list[FrameView]:
        return [f for f in self.frames if name is None or f.name == name]

    def headings(self, frame: Optional[str] = None) -> list[str]:
        return [h for f in self.frame(frame) for h in f.headings]

    def fingerprint(self) -> str:
        """Rough page identity, used to tell whether an action changed anything."""
        parts = [f"{f.name}|{f.url}|{'/'.join(f.headings)}" for f in self.frames]
        parts += [f"{c.frame}|{c.role}|{c.name}|{c.value}" for c in self.controls if c.role != "cell"]
        return hashlib.sha1("\n".join(parts).encode()).hexdigest()[:12]


RecordCallback = Callable[[str, dict[str, Any]], None]


class Surface(Protocol):
    def observe(self) -> Observation: ...
    def navigate(self, url: str) -> None: ...
    def click(self, control: Control) -> None: ...
    def fill(self, control: Control, value: str) -> None: ...
    def select(self, control: Control, option: str) -> None: ...
    def read(self, control: Control) -> str: ...
    def settle(self, timeout_ms: int = 5000) -> None: ...
    def pump(self, ms: int) -> None: ...
    def screenshot(self, path: Path, mask_texts: list[str], mask_patterns: list[Any] = ()) -> None: ...
    def dom_snapshot(self, path: Path, redact: Callable[[str], str]) -> None: ...
    def set_recorder(self, cb: Optional[RecordCallback]) -> None: ...
    @property
    def live_endpoint(self) -> Optional[str]: ...
    def close(self) -> None: ...
