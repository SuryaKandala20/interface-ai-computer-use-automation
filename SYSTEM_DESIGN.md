# System Design

This is the technical reference for how the system is put together: the parts, how data moves between them, what gets stored where, and how it would run in production. The reasoning behind the main choices is in [REPORT.md](REPORT.md), and how to run it is in [README.md](README.md).

## 1. Overview

The system lets an AI agent operate back-office apps that have no API. It works in two phases:

1. **Discovery (once).** An LLM works through a goal on the live app. Every action it takes is recorded, and the recording is compiled into a **capability**: a typed, versioned JSON file describing the flow.
2. **Replay (every time after that).** A plain executor runs the capability with new inputs. There's no model involved, so it's fast, cheap and does the same thing every time. When it hits something it can't handle, it hands the live session to a human.

```mermaid
flowchart LR
    Agent["Calling AI agent"] -->|"invoke by tool name + inputs"| Replay
    Goal["Goal in plain English"] --> Discovery
    Discovery["Discovery (LLM)"] -->|trajectory| Compiler
    Compiler -->|"capability (draft)"| Registry[("Capability store")]
    Reviewer["Human reviewer"] -->|approve| Registry
    Registry -->|"approved capability"| Replay["Replay engine (no LLM)"]
    Replay <-->|"observe / act"| App["Legacy app"]
    Discovery <-->|"observe / act"| App
    Replay -->|"stuck / risky step"| Console["Operator console"]
    Console <-->|"same live session"| App
    Replay -->|"RunResult"| Agent
```

## 2. Components

| Component | File | What it does | Uses |
|---|---|---|---|
| Surface interface | `cua/surface/base.py` | Defines `Observation` and `Control`, and the `Surface` protocol (observe, click, fill, read, screenshot...). Everything above it only sees these types. | nothing |
| Web surface | `cua/surface/web.py`, `perceive.js` | Playwright implementation. Injects a script into every frame that lists controls by role, visible label and table row/column. Also blocks off-allowlist requests and records human clicks. | Playwright |
| Locator | `cua/locator.py` | Finds a target in an observation and checks conditions. Plain functions, no browser. | schema |
| Schema | `cua/schema.py` | The capability format plus AppProfile and TenantBinding. Validates itself on load. | Pydantic |
| Discovery agent | `cua/discover.py` | The LLM loop: observe, ask the model for one tool call, check policy, act, record. Detects when it's stuck. | Anthropic SDK, surface, policy, redactor, control |
| Compiler | `cua/compiler.py` | Turns the trajectory into a capability: locators, templates, checkpoints, leak check. | schema, locator |
| Replay engine | `cua/replay.py` | Runs a capability step by step, applies outcome rules and recoveries, escalates to a human. | surface, locator, policy, redactor, control |
| Result contract | `cua/result.py` | `RunResult` and the error codes returned to the caller. | Pydantic |
| Policy | `cua/policy.py` | Allowlist for URLs and actions, classifies risky actions. | config |
| Redactor | `cua/redact.py` | Masks secrets and PII in logs, model input, screenshots and artifacts. | nothing |
| Evidence recorder | `cua/evidence.py` | Writes the JSONL event log, screenshots and DOM snapshots for a run. | redactor |
| Control / handoff | `cua/control.py` | Lease (who is in control) and intervention requests. | filesystem |
| Operator console | `cua/operator.py` | Web page where a human claims a request, sees and drives the live browser, and hands back. | FastAPI, Playwright over CDP |
| Tenancy | `cua/tenancy.py` | Loads config, applies tenant overrides, resolves secret references, fills templates. | schema |
| CLI | `cua/cli.py` | Entry point for every command. | everything |
| Add-ons | `cua/stability.py`, `cua/learn.py` | Stability check over N replays; turning a human fix into a proposed rule. | replay, schema |
| Mock target | `mockapp/app.py` | Legacy-style core banking app with two tenants and fault injection. | FastAPI |

## 3. Layers

The key boundary is the surface interface. Nothing above it knows it's talking to a browser.

```mermaid
flowchart TB
    subgraph Entry["Entry points"]
        CLI["CLI: discover / replay / invoke / approve / ..."]
        Console["Operator console"]
    end
    subgraph Core["Core (surface-independent)"]
        Disc["Discovery agent"] --> Comp["Compiler"]
        Rep["Replay engine"]
        Loc["Locator + conditions"]
        Pol["Policy"]
        Red["Redactor"]
        Ctl["Control lease + interventions"]
        Sch["Schema: Capability, AppProfile, TenantBinding"]
    end
    subgraph Surf["Surface interface: Observation / Control"]
        Web["WebSurface (built)"]
        Desk["DesktopSurface (designed)"]
        Term["TerminalSurface (designed)"]
    end
    CLI --> Disc
    CLI --> Rep
    Console --> Ctl
    Disc --> Loc
    Rep --> Loc
    Disc --> Pol
    Rep --> Pol
    Disc --> Red
    Rep --> Red
    Rep --> Ctl
    Disc --> Ctl
    Disc --> Web
    Rep --> Web
    Web -.-> Desk
    Web -.-> Term
```

Adding a desktop app means writing a new surface that returns the same `Control` objects (role, name, native id, row/column). The capability format, replay engine and compiler stay the same.

## 4. Main flows

### 4.1 Discovery

```mermaid
sequenceDiagram
    participant U as Operator (CLI)
    participant D as Discovery agent
    participant S as WebSurface
    participant R as Redactor
    participant L as LLM
    participant P as Policy
    participant C as Compiler
    U->>D: goal, params, tenant
    D->>S: open entry URL
    loop until finish or budget runs out
        D->>S: observe()
        S-->>D: controls (with refs), headings, text
        D->>R: redact observation
        D->>L: redacted observation
        L-->>D: one tool call (click / fill / select / extract / finish / request_human)
        D->>P: check action + risk
        alt irreversible
            D->>D: open approval request, wait for human
        end
        D->>S: perform action (secrets filled in here, never by the model)
        D->>D: append trajectory entry (control, before/after page)
    end
    D->>C: trajectory
    C-->>U: capability JSON (draft)
```

The model only ever sees a redacted text view and refers to controls by short-lived refs like `f3e0`. The trajectory stores the full description of each control (role, label, frame, table row), which is what the compiler turns into durable locators.

### 4.2 Replay

```mermaid
sequenceDiagram
    participant A as Caller (agent or CLI)
    participant E as Replay engine
    participant S as WebSurface
    participant O as Outcome rules
    participant H as Handoff broker
    A->>E: capability + tenant + inputs
    E->>E: preflight (approved? fingerprint ok? inputs valid? secrets found?)
    loop each step
        E->>H: does automation hold the lease?
        loop until target found or timeout
            E->>S: observe()
            E->>O: any rule match? (business / recoverable / failure)
            E->>E: resolve locator (primary, then fallbacks)
        end
        E->>S: act (click / fill + read back / select / extract)
        loop until checkpoint holds or timeout
            E->>S: observe()
            E->>O: any rule match?
            E->>E: check step.expect
        end
    end
    E->>E: check final success condition
    E-->>A: RunResult (succeeded / business_outcome / failed)
```

Outcome rules are checked on every poll, before the checkpoint. A recoverable match triggers its recovery (dismiss, restart or wait), a business match ends the run with that outcome, and a failure match ends it with an error and evidence.

### 4.3 Human handoff

```mermaid
sequenceDiagram
    participant E as Replay engine
    participant B as Handoff broker (files)
    participant C as Operator console
    participant Op as Human operator
    participant S as Live browser
    E->>S: screenshot (masked)
    E->>B: open intervention (reason, step, screen summary, CDP endpoint)
    Note over B: lease = awaiting_human
    Op->>C: sees request
    Op->>C: Take control
    C->>B: claim
    Note over B: lease = human
    C->>S: attach over CDP (same session)
    Op->>C: click / type on the live view
    C->>S: input events
    S-->>E: in-page recorder reports the human's clicks
    E->>B: append human actions
    Op->>C: step_done / retry_step / abort
    C->>B: resolve
    Note over B: lease = automation
    E->>E: re-check the step's checkpoint, then continue
```

## 5. Data model

```mermaid
classDiagram
    class Capability {
        id, version, title, description
        app: AppRef
        entry_url
        idempotent, side_effects
        inputs: ParamSpec[]
        secrets: SecretSpec[]
        outputs: OutputSpec[]
        steps: Step[]
        outcomes: OutcomeRule[]
        success: Condition
        approval: Approval
        provenance: Provenance
        fingerprint
    }
    class Step {
        id, intent, action
        target: Locator
        url, value
        extract: Extraction
        risk
        expect: Condition
        timeout_ms, source
    }
    class Locator {
        primary: Target
        fallbacks: Target[]
        rationale
    }
    class Target {
        role, name, name_match
        frame, row, column, column_index
        native_id, nth
    }
    class OutcomeRule {
        id, kind, code
        when: Condition
        applies_to
        recovery: Recovery
    }
    class AppProfile {
        id, product, versions
        outcomes: OutcomeRule[]
        pii_labels, irreversible_labels
    }
    class TenantBinding {
        tenant_id, product, product_version
        base_url
        secrets: name to reference
        overrides: capability id to patch
    }
    class RunResult {
        run_id, capability, tenant, status
        outputs, outcome, error
        recoveries, interventions, warnings, steps
    }
    class Intervention {
        id, run_id, kind, status
        reason_code, step_id, observed, evidence
        live_endpoint, human_actions, resolution
    }
    Capability "1" *-- "many" Step
    Step "1" *-- "0..1" Locator
    Locator "1" *-- "1..many" Target
    Capability "1" *-- "many" OutcomeRule
    AppProfile "1" *-- "many" OutcomeRule
    TenantBinding ..> Capability : overrides
    RunResult ..> Capability : produced by replaying
    RunResult "1" o-- "many" Intervention
```

`Condition` is one of `text_visible`, `control_present`, `url_matches`, `all_of`, `any_of`. Templates (`{{inputs.x}}`, `{{secrets.x}}`, `{{tenant.base_url}}`) can appear in URLs, values, target names and row anchors, and are checked against the declared inputs and secrets when the file is loaded.

**What a run uses:** the base capability, patched with the tenant's overrides, plus the vendor profile's rules and PII labels, plus the tenant's secrets and base URL, plus the caller's inputs.

## 6. State machines

### 6.1 Who controls the session (lease)

```mermaid
stateDiagram-v2
    [*] --> automation
    automation --> awaiting_human: escalate (stuck or risky step)
    awaiting_human --> human: operator claims
    awaiting_human --> automation: request expires
    human --> automation: operator resolves (retry_step / step_done / abort / approve / deny)
```

The executor checks the lease before every action. While it's `awaiting_human` or `human`, automation doesn't touch the page. Only the operator who claimed the request can send input or resolve it.

### 6.2 Capability lifecycle

```mermaid
stateDiagram-v2
    [*] --> draft: compiled from discovery
    draft --> approved: reviewer approves (optionally needs a stability report)
    approved --> draft: accepted learned rule creates a new version
    approved --> deprecated
    note right of approved
        approval is tied to the fingerprint,
        so editing the file blocks replay
    end note
```

A draft can be replayed for testing with `--allow-draft`, but not if it has irreversible steps.

### 6.3 How a replay ends

```mermaid
stateDiagram-v2
    [*] --> running
    running --> running: recoverable condition handled
    running --> succeeded: all steps done and success condition holds
    running --> business_outcome: business rule matched
    running --> failed: failure rule, timeout, policy block, bad input
    running --> waiting_for_human: failure with escalate on, or risky step
    waiting_for_human --> running: retry_step / step_done / approve
    waiting_for_human --> failed: abort / deny / timeout
```

## 7. Storage

Right now everything is files, which is enough for one machine and easy to inspect.

| Data | Location today | Production |
|---|---|---|
| Capabilities | `capabilities/<id>/<version>.json` | Registry table keyed by (id, version), with approval status, fingerprint and tenant overrides |
| Vendor profiles, tenants, policy | `config/**/*.json` | Config service or database, versioned |
| Run logs | `runs/<run_id>/events.jsonl` | Append-only event store or log pipeline |
| Screenshots, DOM snapshots | `runs/<run_id>/screens`, `dom` | Encrypted object storage with retention rules |
| Leases, interventions | `runs/_control/leases`, `interventions` | Database rows plus a work queue for operators |
| Learned-rule proposals | `proposals/*.json` | Review queue |
| Stability reports | `runs/stability/*.json` | Metrics store |
| Secrets | `.env`, referenced as `env:NAME` | Vault, referenced as `vault:path` |

Member data is never stored by this system. It lives in the bank's own app; we only see it on screen, mask it, and return the declared outputs to the caller.

## 8. Trust boundaries

```mermaid
flowchart LR
    subgraph Model["LLM provider"]
        LLM["LLM (Anthropic API)"]
    end
    subgraph Ours["Automation host"]
        Disc["Discovery / Replay"]
        Red["Redactor"]
        Logs[("Logs, screenshots, artifacts")]
        Sec["Secret resolver"]
    end
    subgraph Bank["Bank app"]
        App["Legacy app"]
    end
    App -->|"raw screen (PII)"| Disc
    Disc --> Red
    Red -->|"redacted text only"| LLM
    Red -->|"redacted / masked"| Logs
    Sec -->|"typed straight into the app"| App
    Disc -->|"requests checked against allowlist"| App
```

- The model never gets secrets, screenshots or unmasked PII.
- Secrets go from the resolver straight into the app's input fields. They're never in artifacts or logs.
- Every browser request is checked against the allowlist, including requests made while a human is in control.
- Replay has no model, so page text can't steer it.

## 9. Running it in production (design only)

What's built is one process per run on one machine. At the scale in the brief (hundreds of tenants, about 20 apps each), it would look like this:

```mermaid
flowchart LR
    Agents["AI agents"] --> API["Capability API: catalog + invoke"]
    API --> Q[["Run queue (per tenant)"]]
    Q --> W1["Replay worker + browser"]
    Q --> W2["Replay worker + browser"]
    W1 --> Apps["Tenant apps (via tenant network egress)"]
    W2 --> Apps
    W1 --> Store[("Registry, event store, object storage")]
    W2 --> Store
    W1 --> OQ[["Operator queue"]]
    OQ --> Ops["Operator console (auth, routing)"]
    Ops -. "CDP through authenticated proxy" .-> W1
    Disc["Discovery workers"] --> Store
```

- **Workers.** Each replay runs in its own browser in a stateless worker. Throughput scales with the number of workers.
- **Isolation.** Queues and credentials are per tenant, so one tenant's load or outage doesn't affect others, and a worker only holds the secrets it needs for that run.
- **Handoff.** Interventions go to an operator queue with routing and SLAs. The CDP connection goes through an authenticated proxy instead of an open local port.
- **Drift.** Scheduled stability runs per (capability, tenant) act as canaries. Fallback warnings and checkpoint failures are grouped by tenant and step, so it's clear whether one tenant changed or a vendor release changed everyone.
- **Reuse.** One base capability per vendor product and version range, with small per-tenant override patches. Rules learned on several capabilities can move up into the vendor profile so every tenant gets them.

None of this infrastructure is built, on purpose; the brief asks for the design, not the plumbing. The data models and state machines above stay the same when the storage and transport change.

## 10. Extension points

| To add | Change | Nothing else changes |
|---|---|---|
| A new surface (desktop, green screen) | New class implementing `Surface` that returns `Control` objects | schema, compiler, replay, locator |
| A new tenant on an existing product | New `config/tenants/<id>.json` with base URL, secret references, and overrides if labels differ | capability |
| A new known error or notice screen | Add an `OutcomeRule` to the vendor profile (all capabilities) or to one capability | code |
| A new flow | Run discovery with a new goal, review, approve | code |
| A new risky-action word | Add a pattern to `policy.json` or the profile's `irreversible_labels` | code |
