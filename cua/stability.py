"""Add-on: stability check.

Replays a capability N times with the same inputs (normal replay, no LLM) and
reports whether it did the same thing every time: status, outputs, steps,
recoveries, fallbacks and timings. The report is tied to the capability's
fingerprint, so approve --min-stability can use it without changing how approval
works.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .schema import Capability

REPORTS = Path("runs") / "stability"


class StabilityError(ValueError):
    pass


def measure(cap: Capability, runs: int, replay_once: Callable[[], Any]) -> dict[str, Any]:
    if cap.side_effects != "none":
        raise StabilityError("stability runs are only allowed for side-effect-free capabilities "
                             "(repeating an irreversible flow would repeat its side effects)")
    if runs < 2:
        raise StabilityError("need at least 2 runs")
    results = [replay_once() for _ in range(runs)]
    signatures = [
        (r.status, json.dumps(r.outputs, sort_keys=True), r.outcome.code if r.outcome else None,
         r.error.code if r.error else None, tuple(s.id for s in r.steps))
        for r in results
    ]
    durations = [r.duration_ms for r in results]
    succeeded = sum(r.status == "succeeded" for r in results)
    return {
        "capability": cap.ref,
        "fingerprint": cap.compute_fingerprint(),
        "measured_at": datetime.now().isoformat(timespec="seconds"),
        "runs": runs,
        "statuses": dict(Counter(r.status for r in results)),
        "success_rate": round(succeeded / runs, 3),
        "consistent": len(set(signatures)) == 1,
        "distinct_behaviours": len(set(signatures)),
        "outputs_seen": sorted({json.dumps(r.outputs, sort_keys=True) for r in results}),
        "errors": dict(Counter(r.error.code for r in results if r.error)),
        "recoveries": dict(Counter(x["code"] for r in results for x in r.recoveries)),
        "locator_fallbacks": sum(1 for r in results for w in r.warnings if "fallback" in w),
        "duration_ms": {"min": min(durations), "median": int(statistics.median(durations)), "max": max(durations)},
        "run_ids": [r.run_id for r in results],
    }


def save(report: dict[str, Any], root: Path = REPORTS) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    name = f"{datetime.now():%Y%m%d-%H%M%S}_{report['capability'].replace('@', '_')}.json"
    path = root / name
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return path


def latest_for(cap: Capability, root: Path = REPORTS) -> dict[str, Any] | None:
    fp = cap.compute_fingerprint()
    for p in sorted(root.glob("*.json"), reverse=True) if root.exists() else []:
        rep = json.loads(p.read_text(encoding="utf-8"))
        if rep.get("fingerprint") == fp:
            rep["_path"] = str(p)
            return rep
    return None


def gate(cap: Capability, min_rate: float, report: dict[str, Any] | None) -> tuple[bool, str]:
    if report is None:
        return False, "no stability report for this exact artifact (run: python -m cua stability ...)"
    if report["fingerprint"] != cap.compute_fingerprint():
        return False, "stability report was measured on different artifact content"
    if not report["consistent"]:
        return False, f"behaviour was not consistent across runs ({report['distinct_behaviours']} variants)"
    if report["success_rate"] < min_rate:
        return False, f"success rate {report['success_rate']:.0%} below required {min_rate:.0%}"
    return True, f"{report['runs']} runs, {report['success_rate']:.0%} success, consistent"
