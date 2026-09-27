"""Playwright surface for web and legacy web apps (framesets too)."""

from __future__ import annotations

import re
import socket
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

from playwright.sync_api import Error as PWError
from playwright.sync_api import Frame, Route, sync_playwright

from .base import Control, FrameView, Observation, RecordCallback

PERCEIVE_JS = (Path(__file__).parent / "perceive.js").read_text(encoding="utf-8")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class WebSurface:
    """One live browser session. Started with a CDP port so the operator console can
    attach to this exact session (same cookies, same page) during a handoff."""

    def __init__(
        self,
        *,
        headed: bool = False,
        url_guard: Optional[Callable[[str], tuple[bool, str]]] = None,
        on_blocked: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self._url_guard = url_guard
        self._on_blocked = on_blocked
        self._recorder: Optional[RecordCallback] = None
        self._frames_by_ref: dict[str, Frame] = {}
        self._cdp_port = free_port()
        self._pw = sync_playwright().start()
        self._profile_dir = tempfile.mkdtemp(prefix="cua-session-")
        self._ctx = self._pw.chromium.launch_persistent_context(
            self._profile_dir,
            headless=not headed,
            viewport={"width": 1280, "height": 800},
            args=[f"--remote-debugging-port={self._cdp_port}"],
        )
        self._ctx.add_init_script(PERCEIVE_JS)
        self._ctx.expose_binding("__cuaRecord", self._on_record)
        self._ctx.route("**/*", self._route)
        self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()

    # --- guardrail hook ---
    def _route(self, route: Route) -> None:
        url = route.request.url
        if self._url_guard:
            ok, reason = self._url_guard(url)
            if not ok:
                if self._on_blocked:
                    self._on_blocked(url, reason)
                route.abort("blockedbyclient")
                return
        route.continue_()

    def _on_record(self, source: dict[str, Any], payload: dict[str, Any]) -> None:
        if self._recorder:
            frame = source.get("frame")
            name = frame.name if frame is not None and frame != self.page.main_frame else ""
            self._recorder(name, payload)

    def set_recorder(self, cb: Optional[RecordCallback]) -> None:
        self._recorder = cb

    @property
    def live_endpoint(self) -> str:
        return f"http://127.0.0.1:{self._cdp_port}"

    # --- perception ---
    def _frame_name(self, f: Frame) -> str:
        return "" if f == self.page.main_frame else (f.name or f"frame{self.page.frames.index(f)}")

    def _perceive_frame(self, f: Frame, prefix: str) -> Optional[dict[str, Any]]:
        last = ""
        for _ in range(4):
            try:
                data = f.evaluate("(p) => window.__cua ? window.__cua.perceive(p) : null", prefix)
                if data is None:  # page loaded before our init script got attached
                    f.evaluate(PERCEIVE_JS)
                    continue
                return data
            except PWError as e:  # page navigated mid-evaluate, try again
                if f.is_detached():
                    return None
                last = str(e)
                time.sleep(0.15)
        return {"controls": [], "headings": [], "title": "", "text": f"<unobservable: {last[:120]}>"}

    def observe(self) -> Observation:
        frames: list[FrameView] = []
        controls: list[Control] = []
        self._frames_by_ref.clear()
        for i, f in enumerate(list(self.page.frames)):
            if f.is_detached():
                continue
            data = self._perceive_frame(f, f"f{i}e")
            if data is None:
                continue
            name = self._frame_name(f)
            frames.append(FrameView(name=name, url=f.url, title=data["title"],
                                    headings=data["headings"], text=data["text"]))
            for c in data["controls"]:
                ctrl = Control(frame=name, **{k: c.get(k) for k in (
                    "ref", "role", "name", "name_source", "value", "options", "enabled",
                    "native_id", "row", "column", "col_index", "table", "row_index")})
                controls.append(ctrl)
                self._frames_by_ref[ctrl.ref] = f
        return Observation(url=self.page.url, frames=frames, controls=controls)

    # --- actions ---
    def _loc(self, c: Control):
        f = self._frames_by_ref.get(c.ref)
        if f is None:
            raise LookupError(f"control {c.ref} is not from the latest observation")
        return f.locator(f'[data-cua-ref="{c.ref}"]')

    def navigate(self, url: str) -> None:
        self.page.goto(url, wait_until="load", timeout=30_000)

    def click(self, control: Control) -> None:
        self._loc(control).click(timeout=5_000)

    def fill(self, control: Control, value: str) -> None:
        self._loc(control).fill(value, timeout=5_000)

    def select(self, control: Control, option: str) -> None:
        self._loc(control).select_option(label=option, timeout=5_000)

    def read(self, control: Control) -> str:
        loc = self._loc(control)
        if control.role in ("textbox", "combobox"):
            return loc.input_value(timeout=5_000)
        return loc.inner_text(timeout=5_000)

    def settle(self, timeout_ms: int = 5000) -> None:
        """Best effort wait for the page to go quiet. Nothing depends on it being right,
        every step still waits for its own checkpoint."""
        self.page.wait_for_timeout(200)
        try:
            self.page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except PWError:
            pass
        for f in list(self.page.frames):
            try:
                f.wait_for_load_state("load", timeout=timeout_ms)
            except PWError:
                pass

    def pump(self, ms: int) -> None:
        """Wait, but keep processing browser events so the recorder still fires."""
        self.page.wait_for_timeout(ms)

    # --- evidence ---
    def screenshot(self, path: Path, mask_texts: list[str], mask_patterns: list[re.Pattern[str]] = ()) -> None:
        masks = []
        for f in self.page.frames:
            for t in mask_texts:
                if t:
                    masks.append(f.get_by_text(t, exact=False))
            for pat in mask_patterns:
                masks.append(f.get_by_text(pat))
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.page.screenshot(path=str(path), mask=masks, full_page=False)
        except PWError:
            self.page.screenshot(path=str(path), full_page=False)

    def dom_snapshot(self, path: Path, redact: Callable[[str], str]) -> None:
        parts = []
        for f in self.page.frames:
            try:
                parts.append(f"<!-- frame '{self._frame_name(f)}' {f.url} -->\n{f.content()}")
            except PWError as e:
                parts.append(f"<!-- frame unavailable: {e} -->")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(redact("\n\n".join(parts)), encoding="utf-8")

    def close(self) -> None:
        try:
            self._ctx.close()
        finally:
            self._pw.stop()
