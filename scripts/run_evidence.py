"""Builds the evidence/ folder.

  1. starts the mock app and the operator console as separate processes
  2. with --discover, runs the real LLM discovery for the balance goal (needs
     ANTHROPIC_API_KEY), and the sub-account goal where approval is given in the console
  3. approves the artifact and runs one replay per requirement through the CLI:
     success, business outcome (not found), recoverable (session expiry),
     hard failure (app crash), human takeover, second tenant
  4. copies each run folder into evidence/ and writes evidence/INDEX.md

In the takeover and approval cases the "operator" is a small script calling the
console's HTTP API (claim, act on the live session, hand back), standing in for a
person clicking in the console.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
EVID = ROOT / "evidence"
CONSOLE = "http://127.0.0.1:8766"
BAL_ID = "ledgerline.member.get_share_savings_balance"
SUB_ID = "ledgerline.member.open_subaccount"
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8"}


def http(method: str, url: str, body: dict | None = None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"null")


def wait_port(url: str, tries: int = 60) -> None:
    for _ in range(tries):
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except urllib.error.HTTPError:
            return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError(f"{url} did not come up")


def cua(*args: str, log: Path | None = None) -> tuple[int, str]:
    p = subprocess.run([PY, "-m", "cua", *args], cwd=ROOT, env=ENV, capture_output=True, text=True, encoding="utf-8")
    if log:
        log.write_text(f"$ python -m cua {' '.join(args)}\n\n--- stdout ---\n{p.stdout}\n--- stderr ---\n{p.stderr}",
                       encoding="utf-8")
    return p.returncode, p.stdout + p.stderr


class Operator(threading.Thread):
    """Scripted operator. Waits for the next new intervention, claims it in the console,
    optionally clicks something on the live session, then resolves it."""

    def __init__(self, kind: str, resolution: str, click: str | None = None, note: str = ""):
        super().__init__(daemon=True)
        self.kind, self.resolution, self.click, self.note = kind, resolution, click, note
        self.seen = {i["id"] for i in http("GET", f"{CONSOLE}/api/interventions")}
        self.handled: dict | None = None

    def run(self) -> None:
        deadline = time.time() + 300
        while time.time() < deadline:
            for i in http("GET", f"{CONSOLE}/api/interventions"):
                if i["id"] in self.seen or i["status"] != "open" or i["kind"] != self.kind:
                    continue
                me = {"operator": "operator.jlee"}
                http("POST", f"{CONSOLE}/api/i/{i['id']}/claim", me)
                time.sleep(1.0)
                urllib.request.urlretrieve(f"{CONSOLE}/api/i/{i['id']}/live.png?operator=operator.jlee",
                                           EVID / f"operator_live_view_{i['id']}.png")
                if self.click:
                    http("POST", f"{CONSOLE}/api/i/{i['id']}/click_text", {**me, "text": self.click})
                    time.sleep(1.5)
                http("POST", f"{CONSOLE}/api/i/{i['id']}/resolve", {**me, "mode": self.resolution, "note": self.note})
                self.handled = i
                return
            time.sleep(0.5)


def run_dir_from(output: str) -> Path | None:
    for line in output.splitlines():
        if '"evidence_dir"' in line:
            return ROOT / json.loads("{" + line.strip().rstrip(",") + "}")["evidence_dir"]
        if line.strip().startswith("evidence:"):
            return ROOT / line.split("evidence:")[1].split()[0]
    return None


def collect(name: str, output: str, rows: list, expect: str) -> None:
    src = run_dir_from(output)
    dst = EVID / name
    if src and src.exists():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    status = "?"
    try:
        res = json.loads((dst / "result.json").read_text(encoding="utf-8"))
        status = res["status"] + (f" / {res['outcome']['code']}" if res.get("outcome") else "") + \
            (f" / {res['error']['code']}" if res.get("error") else "") + \
            (f" / recoveries: {', '.join(r['code'] for r in res['recoveries'])}" if res.get("recoveries") else "") + \
            (f" / interventions: {', '.join(i['resolution'] or '-' for i in res['interventions'])}" if res.get("interventions") else "")
    except FileNotFoundError:  # discovery runs have no result.json, so read the event log
        status = "see events.jsonl"
        ev = dst / "events.jsonl"
        if ev.exists():
            for line in ev.read_text(encoding="utf-8").splitlines():
                e = json.loads(line)
                if e["type"] == "discovery_finished":
                    status = f"{e['status']} / {e['steps']} steps / {e['usage']['llm_calls']} LLM calls"
                elif e["type"] == "capability_emitted":
                    status += f" / emitted {e['ref']}"
                elif e["type"] == "control_returned":
                    status += f" / human: {e['resolution']}"
    rows.append((name, expect, status))
    print(f"  {name:32} {status}")


def latest(cap_id: str) -> Path:
    files = sorted((ROOT / "capabilities" / cap_id).glob("*.json"),
                   key=lambda p: tuple(int(x) for x in p.stem.split(".")))
    return files[-1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--discover", action="store_true", help="run the real LLM discovery (costs a few cents)")
    ap.add_argument("--no-subaccount", action="store_true")
    a = ap.parse_args()
    EVID.mkdir(exist_ok=True)
    procs = [subprocess.Popen([PY, "-m", "cua", "mockapp"], cwd=ROOT, env=ENV),
             subprocess.Popen([PY, "-m", "cua", "operator"], cwd=ROOT, env=ENV)]
    rows: list[tuple[str, str, str]] = []
    try:
        wait_port("http://127.0.0.1:8765/t/fvcu/login")
        wait_port(CONSOLE + "/api/interventions")
        if a.discover:
            print("discovery (real LLM) ...")
            code, out = cua("discover", "--tenant", "fvcu", "--capability-id", BAL_ID, "--param", "member_id=10234",
                            "--goal", "Look up member 10234 and read the available balance of their Share Savings account",
                            log=EVID / "01_discovery_balance.cli.txt")
            collect("01_discovery_balance", out, rows, "LLM completes goal; draft capability emitted")
            if code != 0:
                print(out[-2000:])
                return 1
            if not a.no_subaccount:
                op = Operator("approval", "approve", note="member confirmed request by phone")
                op.start()
                code, out = cua("discover", "--tenant", "fvcu", "--capability-id", SUB_ID,
                                "--param", "member_id=10234", "--param", "account_type=Money Market",
                                "--param", "nickname=Rainy Day", "--param", "deposit=250.00",
                                "--goal", "Open a new Money Market sub-account for member 10234 with nickname "
                                          "'Rainy Day' and an initial deposit of 250.00, submit it, and read the "
                                          "confirmation number",
                                log=EVID / "02_discovery_subaccount_with_approval.cli.txt")
                op.join(5)
                collect("02_discovery_subaccount_with_approval", out, rows,
                        "LLM pauses at 'Submit Request'; human approves via console")
        bal = latest(BAL_ID)
        shutil.copy(bal, EVID / "capability_get_share_savings_balance.json")
        cua("show", str(bal), log=EVID / "capability_review.txt")
        cua("approve", str(bal), "--reviewer", "reviewer.akhan", "--notes", "Reviewed steps, locators, outputs")
        shutil.copy(bal, EVID / "capability_get_share_savings_balance.approved.json")

        print("replays ...")
        matrix = [
            ("03_replay_success", ["--input", "member_id=10235"], "succeeded with outputs"),
            ("04_replay_business_outcome_not_found", ["--input", "member_id=99999"],
             "business outcome MEMBER_NOT_FOUND (not an error)"),
            ("05_replay_recoverable_session_expired", ["--input", "member_id=10235", "--chaos", "session_expired_once"],
             "recovers (signs on again), succeeds"),
            ("06_replay_hard_failure_app_error", ["--input", "member_id=10235", "--chaos", "app_error"],
             "hard failure APP_ERROR + masked screenshot/redacted DOM"),
        ]
        for name, extra, expect in matrix:
            code, out = cua("replay", str(bal), "--tenant", "fvcu", *extra, log=EVID / f"{name}.cli.txt")
            collect(name, out, rows, expect)

        op = Operator("takeover", "step_done", click="Continue Anyway", note="dismissed concurrent-session warning")
        op.start()
        code, out = cua("replay", str(bal), "--tenant", "fvcu", "--input", "member_id=10235",
                        "--chaos", "unknown_dialog_once", "--on-failure", "escalate",
                        log=EVID / "07_replay_human_takeover.cli.txt")
        op.join(5)
        collect("07_replay_human_takeover", out, rows,
                "unknown dialog -> escalate -> human acts on the live session -> hand back -> resume")
        if op.handled:
            (EVID / "07_intervention_request.json").write_text(json.dumps(op.handled, indent=2), encoding="utf-8")

        code, out = cua("replay", str(bal), "--tenant", "harbor", "--input", "member_id=10235",
                        log=EVID / "08_replay_second_tenant_harbor.cli.txt")
        collect("08_replay_second_tenant_harbor", out, rows, "same artifact + tenant overrides, succeeds")
    finally:
        for p in procs:
            p.terminate()
    lines = ["# Evidence index", "", "Generated by `python scripts/run_evidence.py" + (" --discover" if a.discover else "") + "`.",
             "Each folder is a copied run directory: `events.jsonl` (structured, redacted log), `result.json` "
             "(the contract returned to the caller), `screens/` (PII-masked screenshots), `dom/` (redacted DOM). "
             "`*.cli.txt` is the exact command and its output.", "",
             "| run | expected | actual |", "|---|---|---|"]
    lines += [f"| `{n}` | {e} | {s} |" for n, e, s in rows]
    (EVID / "INDEX.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {EVID / 'INDEX.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
