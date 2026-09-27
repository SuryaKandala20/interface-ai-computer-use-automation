# Design Report: Computer-Use Automation System

Target: a mock core-banking app built to look legacy (`mockapp/`): framesets, table layouts, no ids, labels in the cell next to each input, two tenants, and fault injection. Flows: search a member and read a balance; open a sub-account (irreversible). Diagrams: [SYSTEM_DESIGN.md](SYSTEM_DESIGN.md).

## 1. Architecture

Parts: a surface (reads/drives the UI), a discovery agent (LLM), a compiler, a replay engine (no model), and a handoff broker with an operator console.

- **The screen is read as labelled controls, not CSS or pixels.** An injected script lists each control as role + visible label (on legacy pages, the neighbouring cell), with table row/column for data cells. Code above the `Surface` interface sees only these, so a desktop adapter is a new class, not a rewrite.
- **The model decides, the executor acts.** The LLM (Anthropic API, Opus-tier, set with `CUA_MODEL`, chosen for reliable multi-step tool use) makes one tool call per turn and points at controls by short-lived refs. The executor checks policy, types credentials itself (the model only names them) and records the full control description. The model sees redacted text only.
- **Discovery and compiling are separate** (the artifact keeps provenance, not the transcript), and **state is in files**, not a database: fine for one machine, and the models carry over later.

## 2. Artifact schema

A capability (`cua/schema.py`, Pydantic-validated) is a contract first: id, semver version, typed `inputs` (with patterns), `secrets` (names only), typed `outputs`, `side_effects`, `idempotent`, `steps`, `outcomes`, a `success` condition, `approval`, `provenance` and a `fingerprint`. One step:

```jsonc
{"id": "click_select", "action": "click", "intent": "Open the member record",
 "target": {"primary": {"role": "link", "name": "Select", "row": "{{inputs.member_id}}"},
            "fallbacks": [...], "rationale": "..."},
 "expect": {"kind": "text_visible", "text": "Member Account Summary"}}
```

- **The contract tells a calling agent what it needs, gets and risks**; `cua catalog` exposes it as a tool definition.
- **Locators say what a person sees**, with fallbacks (the form field name, part of the server's POST contract, and a frame-agnostic version) and a rationale. **Data is never used to find things**: rows are anchored on a stable label ("Share Savings") or on the input, never on a name or balance.
- **Every click has a checkpoint** (the heading it caused, in the frame the next step uses), and **outcome rules are data** (condition, kind, code, bounded recovery), with product-wide ones in the vendor profile.
- **Reviewable and tamper-evident**: `cua show` prints a review, approval is bound to the sha256 fingerprint, and the compiler refuses to write any PII or secret it saw.

## 3. Determinism & error handling

Each step: check the lease → wait for the target → policy check → act (fills read back, extracts parsed) → wait for the checkpoint. There are no sleeps, only "wait until true or time out", so slow pages pass instead of flaking. A locator matching more than one control is skipped, never guessed; using a fallback is logged as drift. Outcome rules run on every poll *before* the checkpoint, so "NO RECORDS FOUND" returns at once instead of timing out.

| Class | Examples | Handling | Caller sees |
|---|---|---|---|
| Business | MEMBER_NOT_FOUND, PERMISSION_DENIED | stop | `business_outcome` + code |
| Recoverable | expired session, notice, slow page | restart, dismiss or wait | `succeeded` + `recoveries` |
| Hard failure | APP_ERROR, CHECKPOINT_FAILED | stop, screenshot, escalate | `failed` + step, expected, observed |

Bad inputs and unapproved or edited artifacts are rejected before touching the UI. Evidence `03`–`06` show each class.

## 4. Heterogeneity & multi-tenant

**Surfaces.** Legacy web is built, framesets included. Desktop (UI Automation maps ControlType, Name and AutomationId straight onto role, name and `native_id`), 3270/5250 and image-only (vision) screens plug in behind the same interface; nothing in steps or locators is DOM-specific.

**Tenants.** One artifact per product, not per tenant, in three layers: **vendor profile** (error screens, PII labels), **base capability** (tenant-neutral), and **tenant binding** (URL, secret references, version, merge-patch overrides by step id). Harbor relabels five things and the same artifact runs there (evidence `08`).

**Drift.** Version ranges are checked first; fallback warnings and failures grouped by (capability, tenant, step) show whether one tenant or a vendor release changed, and scheduled stability runs act as canaries.

## 5. Escalation & handoff

**Detect and route.** Discovery escalates when the agent asks, after three actions that change nothing, on repeated bad refs, or before an irreversible action; replay escalates on UI-fixable failures and approvals. The request carries the step, reason, a redacted screen summary, a masked screenshot and the live browser's CDP endpoint.

**Control and hand-back.** A lease moves `automation → awaiting_human → human → automation`; the executor checks it before every action. The console claims the request and attaches over CDP to **the same browser**, taking input only from whoever claimed it, and the in-page recorder logs the human's clicks in the same role/name terms as the steps. The operator returns control with `retry_step`, `step_done` (the engine re-checks the checkpoint rather than trusting the human), `abort`, or `approve`/`deny`. Evidence `07` shows the loop; only login and screen streaming are mocked.

## 6. Safety

- **Allowlist in two places**: a browser route filter blocks any request outside `policy.json` (agent, replay or human), and every action passes `check_action`, which also limits action types.
- **Risky actions** (declared by the step, or matching a policy/profile pattern like submit, post, transfer) need human approval: live in discovery (evidence `02`), per-run or live in replay. Drafts with side effects never run, and restart is refused on non-idempotent flows. I chose approval over blocking because the system must do real work, and it matches the maker-checker control banks already use.
- **Data**: secrets are config references typed in at the last moment. Redaction (exact secrets, PII by on-screen label, SSN/card/email/phone patterns) covers model input, logs, DOM, screenshots and results.

**Limits:** a committing "OK" button isn't caught by patterns; unlabelled PII can reach log excerpts; the console shows the raw screen; the CDP port is unauthenticated; evidence isn't encrypted. Page text could steer the model in discovery, but the policy gate limits it and replay has no model.

## 7. Cuts

**Left out:** desktop/terminal adapters (designed only), vision fallback, operator auth, streamed co-browsing, database and queue, vault, encryption at rest, and LLM recovery in replay (it would put a model back in production).

**Added:** agent tool catalog, tenant overrides, `cua stability` (evidence `09`), `cua learn` (evidence `10`). **Next:** vendor-wide learned rules, drift canaries, a desktop adapter.
