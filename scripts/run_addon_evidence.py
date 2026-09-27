"""Evidence for the two add-ons. Run it after run_evidence.py; no LLM is used.

  09 stability   replay the approved capability N times and save the report, then
                 approve with the optional --min-stability gate
  10 learning    unknown dialog, human takeover in the console, `cua learn` makes a
                 proposal, `cua accept-proposal` writes v1.1.0 as a draft, stability
                 run on v1.1.0, gated approval, and then the same problem gets handled
                 automatically without a human
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from run_evidence import BAL_ID, CONSOLE, ENV, EVID, PY, ROOT, Operator, cua, wait_port  # noqa: E402


def result_of(out: str) -> dict:
    return json.loads(out[out.index("{"): out.rindex("}") + 1])


def main() -> int:
    procs = []
    for url, cmd in (("http://127.0.0.1:8765/t/fvcu/login", "mockapp"), (CONSOLE + "/api/interventions", "operator")):
        try:
            wait_port(url, tries=2)
        except RuntimeError:
            procs.append(subprocess.Popen([PY, "-m", "cua", cmd], cwd=ROOT, env=ENV))
            wait_port(url)
    base = ROOT / "capabilities" / BAL_ID / "1.0.0.json"
    rows = []
    try:
        # 09: stability check, then approval using the gate
        code, out = cua("stability", str(base), "--tenant", "fvcu", "--input", "member_id=10235", "--runs", "5",
                        log=EVID / "09_stability_v1.0.0.cli.txt")
        rep = Path(re.search(r"report: (\S+)", out).group(1))
        shutil.copy(ROOT / rep, EVID / "09_stability_report_v1.0.0.json")
        r = json.loads(rep.read_text(encoding="utf-8"))
        rows.append(("09_stability_v1.0.0", "5 replays behave identically",
                     f"consistent={r['consistent']} / success_rate={r['success_rate']} / "
                     f"median {r['duration_ms']['median']} ms"))
        code, out = cua("approve", str(base), "--reviewer", "reviewer.akhan", "--min-stability", "0.9",
                        "--notes", "Re-approved with stability gate", log=EVID / "09_approve_with_stability_gate.cli.txt")
        rows.append(("09_approve_with_stability_gate", "approval passes the optional gate",
                     "approved" if code == 0 else "refused"))

        # 10: learn from a human takeover
        op = Operator("takeover", "step_done", click="Continue Anyway", note="dismissed concurrent-session warning")
        op.start()
        code, out = cua("replay", str(base), "--tenant", "fvcu", "--input", "member_id=10235",
                        "--chaos", "unknown_dialog_once", "--on-failure", "escalate",
                        log=EVID / "10a_takeover_to_learn_from.cli.txt")
        op.join(5)
        res = result_of(out)
        iv = res["interventions"][0]["id"]
        rows.append(("10a_takeover_to_learn_from", "unknown dialog needs a human",
                     f"{res['status']} / human resolution: {res['interventions'][0]['resolution']}"))

        code, out = cua("learn", iv, str(base), log=EVID / "10b_learn_proposal.cli.txt")
        prop = Path(re.search(r"proposal: (\S+)", out).group(1))
        rows.append(("10b_learn_proposal", "proposal derived from the human's single safe click",
                     "proposed" if code == 0 else "no proposal"))

        code, out = cua("accept-proposal", str(prop), str(base), "--reviewer", "reviewer.akhan",
                        log=EVID / "10c_accept_proposal.cli.txt")
        v11 = ROOT / "capabilities" / BAL_ID / "1.1.0.json"
        cua("show", str(v11), log=EVID / "10c_capability_review_v1.1.0.txt")
        shutil.copy(ROOT / prop, EVID / "10b_proposal.json")
        rows.append(("10c_accept_proposal", "new version 1.1.0, back to draft", "draft v1.1.0 written" if code == 0 else "refused"))

        code, out = cua("approve", str(v11), "--reviewer", "reviewer.akhan", "--min-stability", "0.9",
                        log=EVID / "10d_approve_v1.1.0_without_stability.cli.txt")
        rows.append(("10d_approve_v1.1.0_without_stability", "gate refuses: no report for new content",
                     "refused" if code != 0 else "approved (unexpected)"))
        code, out = cua("stability", str(v11), "--tenant", "fvcu", "--input", "member_id=10235", "--runs", "5",
                        log=EVID / "10e_stability_v1.1.0.cli.txt")
        code, out = cua("approve", str(v11), "--reviewer", "reviewer.akhan", "--min-stability", "0.9",
                        "--notes", "Reviewed learned rule learned_concurrent_session_warning",
                        log=EVID / "10f_approve_v1.1.0.cli.txt")
        shutil.copy(v11, EVID / "capability_get_share_savings_balance.v1.1.0.approved.json")
        rows.append(("10f_approve_v1.1.0", "approved after stability passes", "approved" if code == 0 else "refused"))

        code, out = cua("replay", str(v11), "--tenant", "fvcu", "--input", "member_id=10235",
                        "--chaos", "unknown_dialog_once", log=EVID / "10g_replay_v1.1.0_auto_recovers.cli.txt")
        res = result_of(out)
        shutil.copytree(ROOT / res["evidence_dir"], EVID / "10g_replay_v1.1.0_auto_recovers", dirs_exist_ok=True)
        rows.append(("10g_replay_v1.1.0_auto_recovers", "same fault, no human: recovers automatically",
                     f"{res['status']} / recoveries: {', '.join(x['code'] for x in res['recoveries'])} / "
                     f"interventions: {len(res['interventions'])}"))
    finally:
        for p in procs:
            p.terminate()

    idx = EVID / "INDEX.md"
    text = idx.read_text(encoding="utf-8").split("\n## Add-ons")[0].rstrip() + "\n"
    text += ("\n## Add-ons (stability signal, learning from human handoffs)\n\n"
             "Generated by `python scripts/run_addon_evidence.py`. No LLM involved.\n\n"
             "| run | expected | actual |\n|---|---|---|\n")
    text += "".join(f"| `{n}` | {e} | {s} |\n" for n, e, s in rows)
    idx.write_text(text, encoding="utf-8")
    for n, e, s in rows:
        print(f"  {n:40} {s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
