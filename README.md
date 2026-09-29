# Foundry Hosted Agents — Sprint 1 POC

Three Python hosted agents: a supervisor, research specialist and analysis specialist.
The supervisor discovers peers from **A2A agent cards**, rather than a configured name map.
In the tested Foundry deployment, **text works over A2A to two prompt-agent front-ends**;
JSON, CSV and Excel uploads use the **Responses API**.

Results below are from **2026-09-28**, in `project-a2a-poc`
(`foundry-fha-kxusxjc5tkc3y`, `swedencentral`, `gpt-5.4-mini`).
Platform limitations describe that deployment and date, not all A2A implementations.
Sprint 2 items are unimplemented or unverified.
After the setup in §6, reproduce with:

```powershell
& $python .\scripts\prove_a2a_parts.py --excel    # exits non-zero if any assertion fails
```

---

## 1. Sprint 1 scorecard

> **Goal.** Prove we can have a supervisor, a research agent and a data analysis agent; that
> they can perform A2A communication via agent card; and that we can do this via the
> Responses API supporting text part, data part and file parts.

| # | Requirement | Status | How it was proven |
|---|---|---|---|
| R1 | **Supervisor agent** | ✅ Achieved | `supervisor-agent` deployed as a hosted agent; delegated the tested research and analysis requests |
| R2 | **Research agent** | ✅ Achieved | `research-agent` deployed as a hosted agent; produces briefs using supplied content and model knowledge; no live web search is configured |
| R3 | **Data analysis agent** | ✅ Achieved | CSV analysis plus Python-computed grouped totals and cross-sheet comparisons from a supplied Excel workbook |
| R4 | **A2A communication** | ✅ Achieved | A blocking `message/send` to each **prompt front-end** returned a completed Task with a text artifact |
| R5 | **…via agent card** | ✅ Achieved | Runtime project listing and card discovery; model selection by skills; A2A endpoint read from the card (§3) |
| R6 | **Via the Responses API** | ✅ Achieved | All three hosted agents serve the Responses protocol; the supervisor is driven entirely through it |
| R7 | **Text part** | ✅ Achieved | A2A text artifacts and Responses `received_as: text` evidence |
| R8 | **Data part** | 🟡 Partial | JSON sent as Responses `input_file` arrives as application `data` content with its structure intact. Native A2A data parts are rejected (`-32005`, §5.1) |
| R9 | **File part** | 🟡 Partial | Responses delivers CSV content and unchanged `.xlsx` bytes with correct multi-sheet results. Native A2A file parts are rejected (`-32005`, §5.1) |
| — | *Hosted agent as an A2A **target*** | ❌ **Blocked by platform** | `-32099 HostedAgentNotSupported` — see §5 |

**R1–R7 achieved; R8/R9 partial.** The harness asserts both successful delivery and the
expected platform rejections. A passing rejection check does **not** mean the capability
is supported.

### Recorded verification

Deployed versions in the recorded run: supervisor **5**, research **3**, analysis **4**;
both prompt front-ends **1**.

| Check group | Result | Evidence |
|---|---|---|
| Cards and direct transport checks | 11/11 | Five published cards, completed prompt-agent A2A Tasks, expected rejections, hosted JSON/CSV receipts and sample-answer checks |
| Supervisor transport checks | 5/5 | Four peers discovered using its managed identity, self excluded, JSON and CSV received by both hosted specialists |
| Direct Excel analysis | 8/8 | Workbook byte length/SHA256, `Sales` and `Targets`, all four tools and exact expected results |
| Excel through the supervisor | 9/9 | The same checks plus successful forwarding to analysis |
| Offline regression tests | 123/123 on Python 3.13.15 and 3.14.3 | Transport, discovery, workbook parsing/calculation and proof-validation tests |

Total: **33/33 live checks**. The final verdict groups the 17 Excel checks into one row.
Excel totals were **176,500 actual / 180,000 target / -3,500 difference / 98.0556%
attainment**. The workbook-upload command and the four manual scenarios in §6 also passed.

Earlier, on supervisor/analysis **v4**, a messy-workbook run verified formatting-only
cells, a chart sheet, a saved-empty formula result and a totals-row warning; research ran
with the workbook withheld. Totals rows remain included, not automatically deduplicated.

These are fixture-based functional checks, not a general model-quality evaluation or a
guarantee that every future request selects the right tool.

---

## 2. Architecture

```
                         Responses API  (input_text · input_file)
                                    │
                                    ▼
                    ┌───────────────────────────────┐
                    │        supervisor-agent       │   hosted (container)
                    │  discovers · routes · merges  │   peers found at runtime from
                    │         · evidences           │   the project's A2A agent cards
                    └───────────────┬───────────────┘
                                    │
             ┌──────────────────────┴───────────────────────┐
             │                                              │
   Responses protocol                              A2A / JSON-RPC
   text + data + file                              text only, Task result
   hosted-agent payload delivery                  prompt-agent invocation
             │                                              │
             ▼                                              ▼
 ┌────────────────────┐ ┌────────────────────┐   ┌────────────────────────┐
 │   research-agent   │ │   analysis-agent   │   │   research-agent-a2a   │
 │       hosted       │ │       hosted       │   │   analysis-agent-a2a   │
 │  + evidence layer  │ │  + evidence layer  │   │      prompt agents     │
 └────────────────────┘ └────────────────────┘   └────────────────────────┘

 All five agents publish an A2A agent card at
 {project}/agents/{name}/endpoint/protocols/a2a/agentCard/v1.0
 The supervisor lists {project}/agents, reads every card except its own, and routes by
 the cards' skills. An agent without a card is not discoverable.
```

### Why two transports

Agent-card discovery is used **before either transport**. The tested paths differ:

| | Foundry A2A (JSON-RPC) | Responses protocol |
|---|---|---|
| Invocation URL | JSONRPC interface from the card | Per-agent Responses URL built from the project endpoint and discovered name |
| Hosted agent as target | ❌ `-32099` | ✅ |
| Text part | ✅ | ✅ |
| Structured data | ❌ Native `DataPart` rejected (`-32005`) | ✅ JSON via `input_file` |
| File | ❌ Native `FilePart` rejected (`-32005`) | ✅ CSV and `.xlsx` via `input_file` |
| Client behaviour | Blocking `message/send`; polls `tasks/get` only if a Task is still pending | Send with `stream: false`; require `status: completed` |

These tests do not establish image delivery, arbitrary file support, or multipart
Responses support on the prompt front-ends.

### Convergence path

If Foundry enables inbound A2A on hosted targets, retest with a fresh supervisor process
so cached refusals do not hide the change. Auto text routing already tries A2A first.
Retire the prompt front-ends only after that succeeds. Multipart A2A needs separate
verification and forwarding changes; its builders alone are not an implemented file path.

---

## 3. Design

### Components

| Component | Kind | Responsibility |
|---|---|---|
| `supervisor-agent` | hosted | Discovers peers, asks its model to select by skills, forwards eligible attachments and emits a hop log |
| `research-agent` | hosted | Research brief: summary, key facts, assumptions, open questions. Sources must come from supplied material or be qualified; no browsing tool is configured. |
| `analysis-agent` | hosted | Quantitative analysis and explicit Python tools for bounded `.xlsx` tables. Large-file / long-running execution and Code Interpreter remain Sprint 2. |
| `research-agent-a2a` | prompt | A2A front-end for research. Exists because Foundry refused hosted A2A targets (§5.2). |
| `analysis-agent-a2a` | prompt | A2A front-end for analysis. Same reason. |

The prompt front-ends are independent model agents with similar personas, not proxies
that call the hosted specialists. Re-running their deployment script creates new versions.

### Supervisor tools

The model selects peers and questions from the catalog; Python applies the requested
transport and attachment policy.

| Tool | Transport | Notes |
|---|---|---|
| `ask_agent(agent, question, transport="auto")` | auto / A2A / Responses | Delegates to a discovered name using the policy below; rejects unknown peers |
| `list_agents()` | — | Refreshes the catalog; returns kinds, skills, attachment eligibility and learned A2A status |
| `probe_part_support(a2a_agent, responses_agent)` | both | Sends **synthetic** JSON and CSV via native A2A parts and Responses files. It tests rejection/delivery, not the caller's uploads |

For `ask_agent`:
1. `auto` with eligible attachments uses Responses. Otherwise it tries A2A.
2. An A2A `HostedAgentNotSupported` refusal triggers Responses fallback. Later `auto`
   calls to that peer skip A2A for the process lifetime. Other errors do not trigger fallback.
3. Explicit `a2a` sends text only, records withheld attachments and never falls back.
   Explicit `responses` bypasses A2A but still filters attachments.

The model is instructed to use the probe for part-support questions and not to inline
attachments into questions; those are prompting rules, not deterministic tool selection.
Python forwards captured attachments. Only `.xlsx` is restricted to the latest user
message; other attachments present in supplied conversation history may be forwarded again.
Workbook bytes stay in application state and are replaced by metadata before model calls.

### Card-based routing

No per-peer names or URLs are configured in the supervisor.
[agent_directory.py](src/supervisor-agent/agent_directory.py) builds its catalog:

1. `GET {project}/agents?api-version=v1`, paged with `after=<last_id>`, lists the
   project's agents with their kind (`hosted` / `prompt`).
2. For every agent except the supervisor itself (the platform-provided
   `FOUNDRY_AGENT_NAME`), it fetches `…/endpoint/protocols/a2a/agentCard/v1.0`.
   Unavailable cards are skipped with a logged warning, including HTTP/authentication
   failures; a missing peer does not necessarily mean A2A is disabled.
3. Each turn, middleware gives the model a compact catalog: name, kind, card description,
   skills with tags and examples, and attachment eligibility. The hop log records this
   initial discovery snapshot, or the error if the agent listing fails.
4. Discovery is cached for 5 minutes. An unknown name or `list_agents` forces a refresh,
   making new or edited cards available without a supervisor redeploy. Card refresh does
   not clear learned A2A refusals.

The host supplies `FOUNDRY_AGENT_NAME` for self-exclusion. For direct local Python runs,
set it as shown in [`.env.example`](src/supervisor-agent/.env.example).
The model's per-turn catalog omits learned A2A status to avoid treating a refused A2A
endpoint as an unreachable agent; `list_agents` reports the distinction explicitly.

**Attachment convention:** the tested cards advertise only text input modes, and the
`agentCard` block in `azure.yaml` has no input-mode field. This POC therefore uses skill
tags for Responses forwarding: `excel` for workbooks and `file` for other captured
attachments. Tags make a **selected** peer eligible; they neither broadcast uploads nor
prove that it can process a format. The model is instructed to prefer an eligible peer
with matching skills.

**Trust and cost:** all readable peer cards are eligible for selection. There is no
owner allow-list, and self-declared tags are not authorization. Keep project agent
publishers trusted. The routing catalog adds about 2,800 characters (four agents) to every
supervisor model call. Learning a hosted refusal costs one A2A round trip before fallback.

### Excel analysis (without Code Interpreter)

```
caller uploads .xlsx
  -> supervisor forwards the workbook bytes unchanged over Responses
  -> analysis reads the workbook with openpyxl and computes with explicit Python tools
  -> model explains those computed results
```

| Analysis tool | Operation |
|---|---|
| `inspect_excel_workbook` | List worksheets, dimensions, first-row headers and warnings; chart sheets are skipped |
| `read_excel_rows` | Read up to 20 nonempty data rows; offsets exclude the header and completely empty rows |
| `aggregate_excel` | Sum, average, min, max or nonblank count over a named column, optionally grouped by another column |
| `compare_excel_sheets` | Sum actuals/targets by a common key across two sheets; compute differences, attainment and totals |

Calculations run in Python. Comparisons sum duplicate keys and reject mismatched key sets.
Numeric operations reject nonnumeric values and skip blanks; aggregates report blank counts.
An all-blank group returns `null` with a warning for numeric grouped aggregates;
ungrouped numeric aggregates and comparisons fail when a group has no numeric values.
`count` counts nonblank values and can return zero. Zero targets have null attainment
with a warning.

Rows with detected totals-like labels remain included and generate a possible-double-counting
warning; the code does not deduplicate them. Evidence records tool arguments, returned
rows/results and errors, not a complete dump of every source cell.

**Sprint 1 contract**

- One inline `.xlsx` workbook per user turn, using `input_file` and base64 `file_data`.
  Reattach it on later turns; old workbook attachments are not silently reused.
- Limits: **5 MiB uploaded bytes**, **20 sheets** (including chart sheets),
  **100,000 cells** across worksheet used-range grids (A1 through the last content row
  and column), plus **20 MiB ZIP-expanded bytes**, **512 archive members** and
  **1,000 result groups**.
  Formatting-only cells, merged ranges and dimension hints do not count or create
  columns. Oversized inputs fail rather than being silently truncated.
- Table operations require unique, nonempty text headers in the first row.
  No arbitrary expressions, macros, shell commands or model-generated Python are executed.
- Formula cells use **saved Excel results**, not formula recalculation. Results may be
  stale; a formula saved as "" is blank; missing cached values needed by an operation
  cause a clear error.
- Chart sheets are skipped with a warning; embedded charts, images and printer settings
  are ignored. `.xls`, `.xlsm`, encrypted workbooks, remote URL/file-ID resolution and
  formatted-report interpretation are not supported by these tools.
- Multi-customer isolation and large/long-running execution are still Sprint 2 concerns.
  The tools keep per-turn state and test concurrent local calls; this is not a claim
  of production tenant isolation.

### The evidence layer

Application-generated JSON blocks describe what the code observed:

| Marker | Producer | Contents |
|---|---|---|
| `A2A-PART-INVENTORY` | Hosted research/analysis middleware | Content types after host conversion, sizes, filenames where available and bounded previews |
| `A2A-HOP-LOG` | Supervisor | Initial discovery snapshot and hops; transport-specific receipts, task IDs, errors, fallback and withheld attachments where applicable |
| `EXCEL-ANALYSIS` | Analysis application | Workbook length/SHA256, worksheet dimensions, tool arguments/results and errors; copied into the supervisor hop as `excel_analysis` |

The proof checks the final marked blocks and removes them before checking sample numbers
in model prose. It does not grade every statement or establish tamper-proof provenance.
The host invokes agents with `stream=True`, so evidence is appended to the terminal stream
update via `ResponseStream.flat_map` ([a2a_parts.py](src/_shared/a2a_parts.py)); mutating
the aggregated response would never reach the client.

### Request flow

Illustrative order only: the model may call analysis before research. The proof requires
both specialists and intact attachments, not a fixed execution order. Agent names below
are what the model chose from the cards in the measured run; none are configured.

```
caller ──input_text + input_file(json) + input_file(csv)──► supervisor
   │
   ├─ middleware: capture attachments → project into both dialects; reset hop log
   ├─ middleware: discover peers (project agent list + A2A cards) → catalog for the model
   ├─ model: ask_agent("research-agent", "review the attachments; how are such sales benchmarked?")
   │     └─ Responses (card accepts files) ─► research-agent ─► brief + PART-INVENTORY
   ├─ model: ask_agent("analysis-agent", "evaluate the CSV against the targets")
   │     └─ Responses (card accepts files) ─► analysis-agent ─► metrics + PART-INVENTORY
   └─ middleware: append HOP-LOG (discovery + hops) to the terminal stream update
                                  │
                                  ▼
                     merged, attributed answer + evidence
```

---

## 4. Layout

```
├── azure.yaml                    3 hosted agent services + ai-project; protocols,
│                                 agentEndpoint and agentCard per agent
├── infra/main.bicep              Foundry account, project, model, Log Analytics,
│                                 App Insights, RBAC — deterministic names
├── scripts/
│   ├── deploy-infra.ps1          Provision infra; also grants agent identities RBAC
│   ├── deploy_a2a_frontends.py   Create/patch the two prompt A2A front-ends
│   ├── lock-agents.ps1           Regenerate uv.lock and normalise it to public PyPI
│   ├── sync-shared.ps1           Fan src/_shared out into each agent folder
│   ├── prove_a2a_parts.py        Original transport proof; --excel also runs workbook proof
│   ├── prove_excel.py           Direct and supervisor multi-sheet delivery/calculation proof
│   └── analyze_excel.py         Upload a local workbook and question to the supervisor
├── tests/                       Offline transport, card-routing, proof and Excel regression tests
└── src/
    ├── _shared/                 Evidence and workbook-attachment helpers, copied per agent
    ├── supervisor-agent/
    │   ├── main.py               Agent, tools, turn middleware, instructions
    │   ├── agent_directory.py    Runtime peer discovery: project agent list + A2A cards
    │   ├── a2a_client.py         Agent listing, card fetch, message/send, tasks/get, typed errors
    │   ├── responses_client.py   Peer-to-peer Responses client (carries the payloads)
    │   └── turn_state.py         Attachment capture/projection, discovery evidence + hop log
    ├── research-agent/main.py
    └── analysis-agent/
        ├── main.py              Model instructions and four explicit Excel tools
        ├── excel_tools.py       Turn-scoped workbook capture and tool execution evidence
        └── excel_workbook.py    Bounded parsing and deterministic spreadsheet calculations
```

Each agent directory is a self-contained build context (Foundry packages it independently),
so `src/_shared` is the single source of truth and `sync-shared.ps1` fans it out.
`sync-shared.ps1 -Check` and `lock-agents.ps1 -Check` fail when copies or locks are stale.
No CI is configured, so run them explicitly.

---

## 5. What was **not** achieved, and why

The tested Foundry gateway returned the errors below. These are deployment observations,
not restrictions of the A2A part model itself.

### 5.1 Foundry rejected A2A data and file parts

```
POST …/agents/research-agent-a2a/endpoint/protocols/a2a?api-version=v1
{"method":"message/send","params":{"message":{"parts":[
  {"kind":"text","text":"probe"},
  {"kind":"data","data":{…}}]}}}

→ {"error":{"code":-32005,"message":"Incompatible content types",
            "data":{"contentType":"data"}}}
```

Identical result with `{"kind":"file"}` (`contentType: "file"`). Consistent with every agent
card advertising `defaultInputModes: ["text"]`.

R8/R9 therefore remain partial. `ask_agent` uses Responses for eligible attachments;
explicit A2A calls withhold them. The diagnostic probe intentionally sends synthetic
non-text parts to reproduce the rejection. The proof also tests data and file separately.

### 5.2 Foundry refused hosted agents as A2A *targets*

```
POST …/agents/research-agent/endpoint/protocols/a2a?api-version=v1

→ {"error":{"code":-32099,
   "data":{"code":"HostedAgentNotSupported",
           "detail":"…not supported for hosted-agent target 'research-agent'.
                     Use a prompt agent as the A2A target."}}}
```

Publishing a card and making **outbound** A2A calls did not imply inbound support: the
supervisor's calls to the prompt front-ends passed, while Foundry rejected inbound calls to
both hosted specialists. Auto routing falls back to Responses on this refusal.

### 5.3 Other measured behaviour

| Behaviour | Consequence |
|---|---|
| The tested `text/csv` attachment is **flattened into text**, prefixed `[File: <name>]` | A supervisor looking only for file objects would drop it. `_unflatten_file` in [turn_state.py](src/supervisor-agent/turn_state.py) reconstructs it so the payload survives the extra hop. The tested `application/json` remains distinct data; image delivery was not part of this verification. |
| Blocking A2A calls returned completed Tasks | In the saved supervisor logs, every prompt-agent call finished in one `message/send` request. The `tasks/get` polling path for pending Tasks is covered only by an offline test. |
| Hosted agents reject the project-level `/responses` route | `bad_request: "Hosted agents can only be called through the agent endpoint"`. Use `…/agents/<name>/endpoint/protocols/openai/responses?api-version=v1`. |
| The project agent listing contained no cards | Discovery fetches cards separately and caches them for 5 minutes; a card's availability is not a health check. |
| Deleting an agent with live sessions returns **409** | Append `&force=true` to cascade-delete its sessions. |

### 5.4 Explicitly out of scope for Sprint 1

Code Interpreter, arbitrary code/process execution, long-running execution, large files
(3K × 300 / ~75 MB), state save/restore, conversation isolation, concurrency/SKU/cold-start
measurements, BCDR and geo constraints. See §8.

---

## 6. Running it

### Prerequisites

`azd >= 1.27.1` with `azure.ai.agents >= 1.0.0-beta.9`, Azure CLI, and authenticated
sessions (`az login`, `azd auth login`). Python 3.13+ and `uv` on PATH. The verification
used azd 1.34.2 and hosted runtime `python_3_13`. The proofs ran on local Python 3.13.15
(the environment below), and the tests ran on both 3.13.15 and 3.14.3.
The subscription needs model quota and permission to deploy resources and assign roles.

From the repository root, install the script/test dependencies from the analysis agent's
checked-in lock (it includes the Azure SDK, agent framework and Excel reader):

```powershell
uv sync --project .\src\analysis-agent --locked --python 3.13 --default-index https://pypi.org/simple
$python = Join-Path $PWD 'src\analysis-agent\.venv\Scripts\python.exe'
```

### Step 1 — infrastructure

```powershell
$subscriptionId = '<your-subscription-id>'
.\scripts\deploy-infra.ps1 -ResourceGroupName rg-foundry-hostedagents -SubscriptionId $subscriptionId
```

Idempotent (deterministic names, `guid()`-named role assignments): a re-run on the existing
deployment created and deleted nothing, though what-if still reports service-populated
property diffs. The script writes non-secret outputs to `infra/outputs.env` for the Python
scripts and imports them into azd, creating or selecting `project-a2a-poc-dev` (override
with `-AzdEnvironmentName`) and pointing it at the existing project. `-WhatIf` previews an
existing resource group; restoring a soft-deleted account requires
`-RestoreSoftDeletedAccount`. Keep the same subscription, naming parameters and agent
identities on later runs.

### Step 2 — agents

```powershell
.\scripts\sync-shared.ps1
.\scripts\lock-agents.ps1
azd deploy                                  # supervisor, research, analysis
& $python .\scripts\deploy_a2a_frontends.py  # the two prompt A2A front-ends
```

### Step 3 — grant the agents access to each other

This template grants every hosted agent **Foundry User** and **Foundry Agent Consumer**
on the project. The supervisor uses its identity for listing, card reads and peer calls;
the specialists use theirs for model inference. These grants worked in the recorded run;
the tests do not establish that both roles are necessary for every agent.

```powershell
$values = azd env get-values --output json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw 'Cannot read azd deployment outputs' }
$agentIds = @(
  $values.AGENT_SUPERVISOR_AGENT_INSTANCE_IDENTITY_PRINCIPAL_ID
  $values.AGENT_RESEARCH_AGENT_INSTANCE_IDENTITY_PRINCIPAL_ID
  $values.AGENT_ANALYSIS_AGENT_INSTANCE_IDENTITY_PRINCIPAL_ID
)
if (@($agentIds | Where-Object { [string]::IsNullOrWhiteSpace($_) }).Count) {
  throw 'Missing hosted identity: inspect azd ai agent show for each deployed agent'
}
.\scripts\deploy-infra.ps1 -ResourceGroupName rg-foundry-hostedagents `
  -SubscriptionId $subscriptionId -AgentPrincipalIds $agentIds
```

Object IDs are `instance_identity.principal_id` from
`GET {endpoint}/agents/<name>?api-version=v1`. **Re-run this after any rename** — a renamed
agent gets a new identity.

### Step 4 — prove it

```powershell
& $python -m unittest discover -s tests -v          # 123 offline regression tests
.\scripts\sync-shared.ps1 -Check
.\scripts\lock-agents.ps1 -Check
& $python .\scripts\prove_a2a_parts.py --excel       # full live proof: 16 + 8 + 9 checks

# Optional subsets (each makes live calls)
& $python .\scripts\prove_a2a_parts.py               # transport proof: 16 verdicts
& $python .\scripts\prove_a2a_parts.py --skip-supervisor
& $python .\scripts\prove_excel.py                  # workbook proof: 8 + 9 checks
```

The Excel proof builds an in-memory workbook with `Sales` and `Targets` sheets. On both the
direct and supervisor paths it checks:
- exact bytes and SHA256, and both sheets' dimensions;
- real inspect/read/aggregate/compare tool calls;
- exact results: **176,500 actual / 180,000 target / 3,500 shortfall**, which the prompt does
  not give.

Prose checks look for the expected totals and shortfall after removing diagnostics; they
do not assess all reasoning or citations. The supervisor proof requires the four expected
peers in discovery, self-exclusion and discovered targets for every hop.

The supervisor has no configured peer map, but the proof fixtures and front-end deployment
script intentionally use this POC's agent names. Update those if renaming agents.

The offline suite also covers:
- **Excel:** formula caches (zero, missing, saved-empty), formatting-only cells, chart sheets,
  printer-settings parts, totals rows, all-blank groups, corrupt ZIPs, size/sheet/cell
  limits, decimal arithmetic, bad headers/keys, tool failures, local concurrency, withholding
  workbooks from research, and streamed evidence.
- **Card routing:** discovery with self-exclusion and card-less agents, paging and listing
  errors, refresh on card edits and TTL, unknown-agent rejection, A2A-first with a
  remembered refusal (explicit A2A never falls back), tag-based attachments, a catalog free
  of transport status and card URLs, discovery-failure reporting, and a check that no peer
  names remain in the supervisor.

### Upload your own workbook

```powershell
& $python .\scripts\analyze_excel.py .\sales.xlsx --question `
  "Inspect the sheets, sum revenue on Sales by region, and compare Sales revenue with Targets target using region as the key."
```

This sends the workbook to the project in `infra/outputs.env`; it creates no storage
account or Code Interpreter. Success requires a completed Responses delegation, matching
filename/length/SHA256, no upload errors and **at least one successful Excel tool call**.
Other failed tool attempts are printed as warnings. Exit 0 does **not** verify that every
requested calculation was performed or correct; the fixed workbook proof checks those results.

### Try it by hand

```powershell
azd ai agent invoke supervisor-agent --protocol responses --new-conversation `
  "Ask a research agent what drives public-cloud revenue seasonality, then have an analysis agent quantify a 4% EMEA gap on 50000."
azd ai agent invoke supervisor-agent --protocol responses --new-conversation "Does A2A support file parts? Probe research."
azd ai agent invoke supervisor-agent --protocol responses --new-conversation "Who can you reach, and over which transport?"
azd ai agent invoke supervisor-agent --protocol responses --new-conversation `
  "Use an A2A hop to ask a research agent for one public-cloud revenue seasonality factor, then ask an analysis agent over A2A to quantify 4% of 50000."
```

On v5 these produced, respectively: hosted A2A refusal followed by Responses fallback;
a real multipart probe; a catalog listing; and two completed prompt-agent A2A Tasks.
Both arithmetic examples returned **2,000**. Inspect the hop log, not just the prose.
Later auto calls may skip A2A after a cached refusal, and model-selected peers/order can vary.

---

## 7. Gotchas

Observed issues and checks to make before diagnosing a new failure:

| Symptom | Cause | Fix |
|---|---|---|
| `azd deploy` → `[CodeError] … Please review your uv.lock`, naming a transitive package | A **private registry or wheel/sdist URL** is unreachable by the Foundry builder; rewriting only `registry` is insufficient | `./scripts/lock-agents.ps1` normalizes the registry and resolves each non-public artifact to public PyPI metadata by exact SHA256. Missing matches fail explicitly. Hosted deployment succeeded after this fix. |
| Same error after normalization | The package name alone does not identify the root cause | Run `lock-agents.ps1 -Check`, inspect build diagnostics and network access. Do not assume it is intermittent; these scripts contain no deployment retry loop. |
| Code packaging failed after `.azdignore` changes | The tested additions `.venv/`, `__pycache__/`, `*.pyc` broke that deployment workflow | Keep the working `.env.example`-only file unless packaging and deployment are revalidated; this is not a claim that every extra pattern always fails. |
| Evidence block missing from a reply | The host streams, so post-hoc mutation never surfaces | Wrap with `with_received_parts_evidence` / `with_hop_log_evidence` (`flat_map` on the terminal update) |
| Supervisor "loses" an attachment | Host flattened a `text/*` file into text | Handled by `_unflatten_file`; keep it in any new forwarding path |
| `bad_request: Hosted agents can only be called through the agent endpoint` | Used project-level `/responses` with `agent_reference` | Use the per-agent endpoint |
| `409 conflict` deleting a prompt front-end | Live or idle-but-unexpired sessions | Only if session destruction is intended, use `deploy_a2a_frontends.py --delete --force` |
| Agent can't reach a peer (401/403) | Incorrect credential, scope or role; a rename can create a new identity | Inspect the actual error and principal. If roles are missing, supply the current identities to `deploy-infra.ps1`. |
| Discovery fails or a peer is missing | Listing errors are recorded in the hop log; individual card errors only log a warning and omit that peer | Check logs, endpoint and permissions. Do not assume the agent was deleted or A2A disabled. |
| Failed reply with a token-rate-limit error in logs | All five agents share one model deployment limited to **10,000 tokens and 10 requests per minute** (`capacity: 10`), and one multi-hop turn makes several model calls | Pace runs about a minute apart, or raise `modelDeployment.capacity` in `infra/main.bicep` and re-run `deploy-infra.ps1` (needs quota; costs more). An empty reply alone does not identify this cause. |
| A new card changes routing | All readable peer cards are eligible; `file`/`excel` tags allow upload forwarding when selected | Restrict who can publish cards. Tags are not a security boundary. |

The recorded azd environment was `project-a2a-poc-dev`. The old `research-agent-dev`
environment and old `aif-*` / `log-*` / `appi-*` resources were deliberately preserved.
They may still incur charges; renaming is not migration or cleanup.

---

## 8. Tear down

Deleting this resource group also deletes the preserved old deployment and any unrelated resources in it. 

```powershell
& $python .\scripts\deploy_a2a_frontends.py --delete
# If sessions block deletion, add --force only to intentionally delete those sessions.
az group delete -n rg-foundry-hostedagents --subscription $subscriptionId --yes
az cognitiveservices account purge -g rg-foundry-hostedagents -l swedencentral -n '<account-name>' --subscription $subscriptionId
```
