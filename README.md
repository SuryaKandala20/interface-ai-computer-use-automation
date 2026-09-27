# Computer-Use Automation: discover once, replay deterministically

An LLM works out how to do a task in a legacy back-office UI once. The run is compiled into a **typed, versioned, reviewable capability**. Deterministic replay (no model in the loop) then executes that capability with new inputs, separates business outcomes from recoverable conditions and hard failures, and hands the **live session** to a human when it can't safely continue.

- Design write-up: [REPORT.md](REPORT.md)
- System design (components, flows, data model, production design): [SYSTEM_DESIGN.md](SYSTEM_DESIGN.md)
- Proof of both runs: [evidence/INDEX.md](evidence/INDEX.md)

```
goal ──► discovery agent (LLM, tool use) ──► trajectory ──► compiler ──► capability.json (draft)
                 │ observe/act via Surface                                    │ review: cua show
                 ▼                                                            ▼ approve (fingerprint-bound)
          legacy web app  ◄──── replay engine (no LLM) ◄──── inputs ◄──── calling agent (cua invoke)
                 ▲                    │ business outcome / recovery / failure
                 └── operator console ◄┘ escalation: same live session over CDP, then hand back
```

## Setup

Requires Python 3.11 or newer (developed on 3.13, Windows 11).

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env        # demo credentials for the local mock app; add ANTHROPIC_API_KEY for discovery
```

`.env` holds:
- `ANTHROPIC_API_KEY`, needed only for `discover`.
- The mock app's operator credentials, which tenants reference as `env:` secrets. They are never stored in artifacts or logs.

Set `CUA_MODEL` in `.env` to the model id used for discovery (replay never uses a model).

## Demo path

Use three terminals.

```bash
# 1. the target: a deliberately legacy core-banking app (framesets, tables, no ids)
python -m cua mockapp                      # http://127.0.0.1:8765/t/fvcu/login

# 2. the operator console used for escalations and handoff
python -m cua operator                     # http://127.0.0.1:8766

# 3. discover -> review -> approve -> replay
python -m cua discover --tenant fvcu --param member_id=10234 \
    --capability-id ledgerline.member.get_share_savings_balance \
    --goal "Look up member 10234 and read the available balance of their Share Savings account"

python -m cua show    capabilities/ledgerline.member.get_share_savings_balance/1.0.0.json
python -m cua approve capabilities/ledgerline.member.get_share_savings_balance/1.0.0.json --reviewer you

python -m cua replay capabilities/ledgerline.member.get_share_savings_balance/1.0.0.json \
    --tenant fvcu --input member_id=10235                  # succeeded, outputs.available_balance
python -m cua replay ... --tenant fvcu --input member_id=99999                    # business_outcome MEMBER_NOT_FOUND
python -m cua replay ... --tenant fvcu --input member_id=10235 --chaos app_error  # failed APP_ERROR + evidence
python -m cua replay ... --tenant harbor --input member_id=10235                  # 2nd tenant via overrides
python -m cua replay ... --tenant fvcu --input member_id=10235 \
    --chaos unknown_dialog_once --on-failure escalate --headed
#   -> an intervention appears in the console: "Take control", click "Continue Anyway" in the
#      live view (or in the headed window), then "step_done". The replay resumes and finishes.
```

Replay exit codes: `0` succeeded, `2` business outcome, `1` failed. The result JSON on stdout is the contract a calling agent receives.

Agent-facing surface (a stretch goal):
- `python -m cua catalog` prints approved capabilities as tool definitions.
- `python -m cua invoke ledgerline__member__get_share_savings_balance --tenant fvcu --args '{"member_id":"10235"}'` invokes one by name.

`--chaos` is a test-harness switch that makes the mock app inject a runtime fault. The modes are `slow`, `session_expired_once`, `notice_once`, `app_error` and `unknown_dialog_once`. The automation policy never allowlists that endpoint.

### Add-ons (optional; the core flow above doesn't depend on them)

They live in separate modules (`cua/stability.py`, `cua/learn.py`) and add new commands only. The artifact schema, discovery and the replay engine are unchanged.

```bash
CAP=capabilities/ledgerline.member.get_share_savings_balance

# Stability signal: replay N times, check the capability behaves identically every time
python -m cua stability $CAP/1.0.0.json --tenant fvcu --input member_id=10235 --runs 5
python -m cua approve   $CAP/1.0.0.json --reviewer you --min-stability 0.9    # optional gate, bound to the fingerprint

# Learn from a human handoff: turn an operator's fix into a reviewed rule
python -m cua replay $CAP/1.0.0.json --tenant fvcu --input member_id=10235 \
    --chaos unknown_dialog_once --on-failure escalate --headed      # resolve it in the console (step_done)
python -m cua learn <intervention-id> $CAP/1.0.0.json                # writes proposals/prop-<id>.json
python -m cua accept-proposal proposals/prop-<id>.json $CAP/1.0.0.json --reviewer you   # -> 1.1.0, draft
python -m cua stability $CAP/1.1.0.json --tenant fvcu --input member_id=10235 --runs 5
python -m cua approve   $CAP/1.1.0.json --reviewer you --min-stability 0.9
python -m cua replay $CAP/1.1.0.json --tenant fvcu --input member_id=10235 --chaos unknown_dialog_once
#   -> succeeds with recoveries: CONCURRENT_SESSION_WARNING and no human needed
```

The intervention id is printed in the replay's `[escalation]` line and in its result JSON.

The learner only proposes a rule from a single, safe (non-irreversible) click, and never applies it itself. Accepting a proposal creates a new version that is back in draft, so it must be reviewed and approved like any other change.

### Reproduce all evidence

```bash
python scripts/run_evidence.py --discover   # real LLM discovery (~cents) + the full replay matrix -> evidence/
python scripts/run_evidence.py              # replay matrix only, against the existing capability
python scripts/run_addon_evidence.py        # add-on evidence (stability + learning loop), no LLM
```

## Running without live services

- **No API key needed for replay.** An approved capability from a real discovery run is committed in `capabilities/` and `evidence/`. Every `replay`, `catalog` and `invoke` command works offline against the local mock app.
- **Tests:** run `pytest` (about 3 minutes, 42 tests). It covers unit tests for the schema contract, locators, redaction, policy and tenancy. It also runs end-to-end discovery → compile → replay against the live mock app, with every runtime-condition class, tenant overrides, irreversible-action approval, and a human takeover over CDP. In tests the LLM is replaced by `tests/scripted_llm.py`, a clearly labelled test double. The genuine LLM run is in `evidence/`.

## Layout

| Path | What |
|---|---|
| `cua/schema.py` | Capability artifact schema v1.0 (plus AppProfile and TenantBinding) |
| `cua/surface/` | Surface seam: `base.py` (Control/Observation protocol), `web.py` (Playwright), `perceive.js` |
| `cua/discover.py` | LLM observe→decide→act loop (tool use), stuck detection, handoff |
| `cua/compiler.py` | trajectory → capability: locators, parameterisation, checkpoints, leak check |
| `cua/replay.py` | deterministic executor, outcome taxonomy, recoveries, escalation |
| `cua/locator.py` | surface-neutral target resolution and condition evaluation |
| `cua/policy.py`, `cua/redact.py` | guardrails: allowlist, risk classes, PII/secret redaction |
| `cua/control.py`, `cua/operator.py` | control lease + interventions; operator console over CDP |
| `cua/stability.py`, `cua/learn.py` | add-ons: multi-run stability signal; learning rules from human handoffs |
| `proposals/` | learned-rule proposals awaiting or after review |
| `config/` | `policy.json`, vendor profile `profiles/ledgerline-core-7.json`, tenants `fvcu`, `harbor` |
| `mockapp/app.py` | the proxy target: legacy "LedgerLine Core" with two tenant variants and fault injection |
| `capabilities/` | saved artifacts, `<id>/<semver>.json` |
| `evidence/` | discovery + replay runs (logs, results, masked screenshots, redacted DOM) |
