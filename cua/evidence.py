"""Run evidence: a redacted JSONL event log, plus masked screenshots and redacted DOM
snapshots taken at the points that matter."""

from __future__ import annotations

import json
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .redact import Redactor
from .surface.base import Surface


def new_run_id(kind: str) -> str:
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{kind}-{secrets.token_hex(3)}"


class RunRecorder:
    def __init__(self, kind: str, redactor: Redactor, root: str | Path = "runs", run_id: Optional[str] = None):
        self.run_id = run_id or new_run_id(kind)
        self.kind = kind
        self.dir = Path(root) / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.redactor = redactor
        self._seq = 0
        self._t0 = time.monotonic()
        self._log = (self.dir / "events.jsonl").open("a", encoding="utf-8")

    def event(self, type_: str, **data: Any) -> None:
        self._seq += 1
        if "type" in data:  # don't let a payload's own 'type' (e.g. a recorded click) clobber the event type
            data["action_type"] = data.pop("type")
        rec = {
            "seq": self._seq,
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "t_ms": int((time.monotonic() - self._t0) * 1000),
            "run_id": self.run_id,
            "type": type_,
            **self.redactor.obj(data),
        }
        self._log.write(json.dumps(rec, default=str) + "\n")
        self._log.flush()

    def capture(self, surface: Surface, label: str, *, dom: bool = True) -> dict[str, str]:
        """Take a masked screenshot (and redacted DOM). Used on failures and handoffs."""
        out: dict[str, str] = {}
        stem = f"{self._seq:03d}_{label}"
        try:  # re-learn PII from the current screen first, otherwise masking lags behind navigation
            self.redactor.learn(surface.observe())
        except Exception:
            pass
        try:
            png = self.dir / "screens" / f"{stem}.png"
            surface.screenshot(png, self.redactor.sensitive_strings(), self.redactor.screen_patterns())
            out["screenshot"] = str(png)
        except Exception as e:  # a failed screenshot shouldn't take the run down with it
            out["screenshot_error"] = str(e)[:200]
        if dom:
            try:
                html = self.dir / "dom" / f"{stem}.html"
                surface.dom_snapshot(html, self.redactor.text)
                out["dom_snapshot"] = str(html)
            except Exception as e:
                out["dom_error"] = str(e)[:200]
        self.event("evidence_captured", label=label, **out)
        return out

    def write_json(self, name: str, obj: Any) -> Path:
        p = self.dir / name
        p.write_text(json.dumps(self.redactor.obj(obj), indent=2, default=str), encoding="utf-8")
        return p

    def close(self) -> None:
        self._log.close()
