# Foundry Hosted Agents — Sprint 1 POC

Multi-agent system on Microsoft Foundry **hosted agents**: a supervisor, a research agent and
a data analysis agent that discover each other through **agent cards** and exchange
**text, data and file** payloads.

The Sprint 1 results below were measured against the renamed live Foundry project
(`project-a2a-poc`, account `foundry-fha-kxusxjc5tkc3y`, `swedencentral`,
`gpt-5.4-mini`), with the transport proof rerun, Excel support verified, and an
independent peer review of the Excel implementation fixed and re-verified on
2026-09-28. Sprint 2 remains a backlog, not a verified capability.
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
| R4 | **A2A communication** | ✅ Achieved | Live `message/send` → A2A **Task** → `tasks/get` → `completed` with artifacts |
| R5 | **…via agent card** | ✅ Achieved | Every peer is resolved from `…/endpoint/protocols/a2a/agentCard/v1.0`; the transport URL is read from the card's `supportedInterfaces`, never hard-coded |
| R6 | **Via the Responses API** | ✅ Achieved | All three hosted agents serve the Responses protocol; the supervisor is driven entirely through it |
| R7 | **Text part** | ✅ Achieved | Arrives as `received_as: text` |
| R8 | **Data part** | ✅ Achieved | `application/json` arrives as `received_as: data`, media type and JSON structure intact |
| R9 | **File part** | ✅ Achieved | CSV content grounding plus `.xlsx` byte/SHA256 delivery and correct multi-sheet tool results through the supervisor |
| — | *A2A carrying data/file parts* | ❌ **Blocked by platform** | `-32005 Incompatible content types` — see §5 |
| — | *Hosted agent as an A2A **target*** | ❌ **Blocked by platform** | `-32099 HostedAgentNotSupported` — see §5 |

**All nine scorecard requirements are met using the two transports described below.**
This does **not** prove multipart A2A between hosted agents. Two things we attempted are
blocked by Foundry itself; both are documented, asserted in the harness, and have a migration
path the day the platform lifts them.

Latest run — **32/32 live checks PASS** (15 original transport checks + 17 Excel checks),
plus **108/108 offline regression tests**:

```
PASS Agent cards published for every agent
PASS A2A: text part delivered, task ran to completion
PASS A2A: data part rejected by the platform (documented limit)
PASS A2A: file part rejected by the platform (documented limit)
PASS A2A: hosted agents refused as targets (documented limit)
PASS Responses: every specialist response completed
PASS Responses: deterministic evidence block present
PASS Responses: TEXT part delivered
PASS Responses: DATA part delivered
PASS Responses: FILE part delivered
PASS Responses: payload values reached the model
PASS Supervisor answered over Responses API
PASS Supervisor emitted a hop log
PASS Supervisor delegated to both specialists
PASS Supervisor forwarded data + file parts onward
```

All three hosted agents were active: supervisor version **4**, research version **3**,
analysis version **4**; both prompt front-ends remain version **1**. Manual checks also
verified both supervisor-to-prompt A2A hops, the native multipart probe, discovery of
all four peers, and **4% of 50,000 = 2,000**. These are transport/functional checks,
not a general model-quality evaluation.

Excel verification passed **8/8 direct-analysis** and **9/9 supervisor-path** checks.
Both paths received identical workbook bytes, discovered `Sales` and `Targets`, invoked
all four Excel tools, and produced **176,500 actual / 180,000 target / -3,500 difference /
98.0556% attainment**. The standalone workbook-upload command was also verified live.

A peer review reproduced and fixed 11 Excel defects (false errors on normal Excel files,
a crash on corrupt archives, a concurrency bug, silent totals-row double counting, a
grouped-result failure, and research being blocked when a workbook was attached).
After redeploying version 4, a deliberately messy workbook sent through the supervisor
was analysed correctly: formatting beyond the table, a chart sheet, a formula saved as ""
and a `Total` row. The answer kept every warning, and research still ran on the same
turn without receiving the workbook.

---

## 2. Architecture

```
                         Responses API  (input_text · input_file · input_image)
                                    │
                                    ▼
                    ┌───────────────────────────────┐
                    │        supervisor-agent       │   hosted (container)
                    │  routes · merges · evidences  │
                    └───────────────┬───────────────┘
                                    │
             ┌──────────────────────┴───────────────────────┐
             │                                              │
   Responses protocol                              A2A / JSON-RPC
   text + data + file                              text only, async Task
   (the working payload path)                      (the agent-card path)
             │                                              │
             ▼                                              ▼
 ┌────────────────────┐ ┌────────────────────┐   ┌────────────────────────┐
 │   research-agent   │ │   analysis-agent   │   │   research-agent-a2a   │
 │       hosted       │ │       hosted       │   │   analysis-agent-a2a   │
 │  + evidence layer  │ │  + evidence layer  │   │      prompt agents     │
 └────────────────────┘ └────────────────────┘   └────────────────────────┘

 All five agents publish an A2A agent card and are discoverable at
 {project}/agents/{name}/endpoint/protocols/a2a/agentCard/v1.0
```

### Why two transports

This is the central design decision, and it is forced by the platform rather than chosen.

Foundry today gives you **agent-card discovery + A2A invocation** on one side, and
**multi-part payloads** on the other — but not both on the same path:

| | A2A (JSON-RPC) | Responses protocol |
|---|---|---|
| Discovery via agent card | ✅ | ✖ (direct endpoint) |
| Hosted agent as inbound A2A target | ❌ `-32099` | ✅ |
| Text part | ✅ | ✅ |
| Data part | ❌ `-32005` | ✅ |
| File part | ❌ `-32005` | ✅ |
| Execution model | async **Task** (submit → poll) | request/response |

A2A message file part returns:
```
{
  "code": -32005,
  "message": "Incompatible content types",
  "data": {
    "contentType": "file"
  }
}
```
The rejection happens at the platform gateway, before the receiving agent can process the file.

So Sprint 1 uses each transport for what it can actually do:

- **A2A** proves R4/R5 — real `message/send` (text-only calls) against prompt-agent front-ends, discovered
  through their published cards, following the full Task lifecycle.
- **Responses** proves R6–R9 — hosted specialist to hosted specialist, carrying text, data
  and file parts (file/data-bearing calls), which is where the real work happens.

The supervisor speaks **both**, and labels every delegation with the transport it used.

### Convergence path

The specialists already publish agent cards and [`a2a_client.py`](src/supervisor-agent/a2a_client.py)
already implements the Task lifecycle used here. If Foundry enables A2A on hosted targets,
retest the capability before repointing `_A2A_PEERS` and retiring the prompt front-ends.
Multipart A2A support is a separate gate and must also be retested.

---

## 3. Design

### Components

| Component | Kind | Responsibility |
|---|---|---|
| `supervisor-agent` | hosted | Decomposes work, picks specialists and transports, forwards attachments, merges answers, emits the hop log. Instructions delegate substantive research/analysis; routing is model-driven. |
| `research-agent` | hosted | Research brief: summary, key facts, assumptions, open questions. Sources must come from supplied material or be qualified; no browsing tool is configured. |
| `analysis-agent` | hosted | Quantitative analysis and explicit Python tools for bounded `.xlsx` tables. Large-file / long-running execution and Code Interpreter remain Sprint 2. |
| `research-agent-a2a` | prompt | A2A front-end for research. Exists only because A2A refuses hosted targets. |
| `analysis-agent-a2a` | prompt | A2A front-end for analysis. Same reason. |

The prompt front-ends are independent model agents with similar personas, not proxies
that call the hosted specialists.

### Supervisor tools

The model chooses *who* and *what to ask*; the harness owns *how it travels*.

| Tool | Transport | Notes |
|---|---|---|
| `ask_research` | Responses | Forwards text and non-Excel attachments; withholds `.xlsx` and records it in the hop log |
| `ask_analysis` | Responses | Auto-forwards this turn's attachments, including binary `.xlsx` |
| `ask_over_a2a` | A2A | Text only; reports any attachment it had to leave behind |
| `list_specialists` | — | Returns each peer's published agent card |
| `probe_part_support` | both | Sends the same text+data+file payload down both paths and reports what each accepted |

Attachments are not copied into model-generated tool arguments. They remain available
to the receiving agent/model. Middleware captures them once per
turn into a `ContextVar`, projects them into **both** dialects (A2A `FilePart`/`DataPart` and
Responses `input_file`/`input_image`), and the tools forward the right projection.
For Excel specifically, middleware keeps the binary workbook in application state and
replaces it with metadata before either agent calls the model. The model receives tool
results, not raw workbook bytes.

### Excel analysis (without Code Interpreter)

```
caller uploads .xlsx
  -> supervisor forwards unchanged input_file over Responses
  -> analysis reads the workbook with openpyxl and computes with explicit Python tools
  -> model explains those computed results
```

| Analysis tool | Operation |
|---|---|
| `inspect_excel_workbook` | Discover all sheets, dimensions, first-row headers and formula-cache warnings |
| `read_excel_rows` | Read an explicit page of up to 20 data rows; report total rows and whether more remain |
| `aggregate_excel` | Sum, average, min, max or nonblank count over a named column, optionally grouped by another column |
| `compare_excel_sheets` | Sum actuals/targets by a common key across two sheets; compute differences, attainment and totals |

Numeric operations run in Python, not by asking the model to calculate from a preview.
Duplicate keys are summed; mismatched key sets and invalid numeric values fail explicitly.
Blank numeric cells are excluded; aggregates report their excluded counts. In grouped
results, a group with no numeric values is `null` with a warning, while the other groups
are still computed; ungrouped results and comparisons without numbers fail explicitly
rather than reporting 0. A zero target has null attainment plus a warning.
Rows labelled like totals (`Total`, `Grand Total`, `EMEA Total`, `Subtotal`) stay in
calculations but are named in a warning, because they may double-count detail rows.
Source values, tool arguments, results and errors remain visible in the evidence.

**Sprint 1 contract**

- One inline `.xlsx` workbook per user turn, using `input_file` and base64 `file_data`.
  Reattach it on later turns; old workbook attachments are not silently reused.
- Limits: **5 MiB uploaded bytes**, **20 sheets**, **100,000 cells** across all sheets'
  used ranges (A1 to the last cell holding a value or formula), plus **20 MiB
  ZIP-expanded bytes**, **512 archive members** and **1,000 result groups**.
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

A POC where the model *claims* the file arrived proves nothing. Machine-readable blocks
are generated by middleware from the actual objects on the wire:

**`A2A-PART-INVENTORY`** — emitted by each specialist, describing what it received:

```json
{ "marker": "A2A-PART-INVENTORY",
  "parts": [
    { "received_as": "text", "chars": 153, "preview": "Part-support probe. Using the attached…" },
    { "received_as": "data", "media_type": "application/json", "bytes": 143,
      "filename": "targets.json", "preview": "{\"quarter\": \"FY26Q1\", \"targets\": {…}}" },
    { "received_as": "text", "chars": 111, "preview": "[File: regional-sales.csv]\nregion,units,revenue\nEMEA,1200,48000…" }
  ] }
```

**`A2A-HOP-LOG`** — emitted by the supervisor, one entry per delegation: peer, transport,
URL, part kinds sent and received, task id, and any protocol error code.

**`EXCEL-ANALYSIS`** — emitted by the analysis application: workbook byte length and SHA256,
all sheet dimensions, each Excel tool's arguments/results, and explicit failures. The
supervisor copies this evidence into its analysis hop as `excel_analysis`.

The harness selects the final marked evidence blocks, checks the actual sample JSON and CSV,
and checks model grounding separately after removing diagnostics. HTTP success alone,
arbitrary protocol errors, and evidence echoes do not count as successful delivery.
This is functional evidence, not tamper-proof attestation for untrusted inputs.

> **Implementation note.** The Foundry host always invokes agents with `stream=True`, so
> mutating the aggregated `AgentResponse` — or using `stream_result_hooks` — never reaches the
> client. The evidence is injected by expanding the terminal stream update into
> `[evidence, update]` via `ResponseStream.flat_map`. This cost real debugging time; see
> `with_received_parts_evidence` in [a2a_parts.py](src/_shared/a2a_parts.py).

### Request flow

Illustrative order only: the model may call analysis before research. The proof requires
both specialists and intact attachments, not a fixed execution order.

```
caller ──input_text + input_file(json) + input_file(csv)──► supervisor
   │
   ├─ middleware: capture attachments → project into both dialects; reset hop log
   ├─ model: ask_research("how are regional cloud sales benchmarked?")
   │     └─ Responses ─► research-agent ─► brief + PART-INVENTORY
   ├─ model: ask_analysis("evaluate the CSV against the targets")
   │     └─ Responses ─► analysis-agent ─► metrics + PART-INVENTORY
   └─ middleware: append HOP-LOG to the terminal stream update
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
│                                 App Insights, RBAC — fully idempotent
├── scripts/
│   ├── deploy-infra.ps1          Provision infra; also grants agent identities RBAC
│   ├── deploy_a2a_frontends.py   Create/patch the two prompt A2A front-ends
│   ├── lock-agents.ps1           Regenerate uv.lock and normalise it to public PyPI
│   ├── sync-shared.ps1           Fan src/_shared out into each agent folder
│   ├── prove_a2a_parts.py        Original transport proof; --excel also runs workbook proof
│   ├── prove_excel.py           Direct and supervisor multi-sheet delivery/calculation proof
│   └── analyze_excel.py         Upload a local workbook and question to the supervisor
├── tests/                       Offline transport, proof and Excel regression tests
└── src/
    ├── _shared/                 Evidence and workbook-attachment helpers, copied per agent
    ├── supervisor-agent/
    │   ├── main.py               Agent, tools, turn middleware, instructions
    │   ├── a2a_client.py         Card discovery, message/send, tasks/get, typed errors
    │   ├── responses_client.py   Peer-to-peer Responses client (carries the payloads)
    │   └── turn_state.py         Attachment capture/projection + hop log
    ├── research-agent/main.py
    └── analysis-agent/
        ├── main.py              Model instructions and four explicit Excel tools
        ├── excel_tools.py       Turn-scoped workbook capture and tool execution evidence
        └── excel_workbook.py    Bounded parsing and deterministic spreadsheet calculations
```

Each agent directory is a self-contained build context (Foundry packages it independently),
so `src/_shared` is the single source of truth and `sync-shared.ps1` fans it out.
`sync-shared.ps1 -Check` and `lock-agents.ps1 -Check` fail the build on drift.

---

## 5. What was **not** achieved, and why

Both gaps are platform limitations with reproducible error codes. Neither is a sprint
requirement — both were attempts to go further.

### 5.1 A2A cannot carry data or file parts

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

**Impact:** none on Sprint 1 — R7/R8/R9 are satisfied over the Responses transport.
**Workaround:** `ask_over_a2a` reports what it had to leave behind rather than dropping it
silently. The A2A `FilePart`/`DataPart` builders are written and unit-tested, ready for the day
the gate accepts them.

### 5.2 Hosted agents cannot be A2A *targets*

```
POST …/agents/research-agent/endpoint/protocols/a2a?api-version=v1

→ {"error":{"code":-32099,
   "data":{"code":"HostedAgentNotSupported",
           "detail":"…not supported for hosted-agent target 'research-agent'.
                     Use a prompt agent as the A2A target."}}}
```

Note the asymmetry: a hosted agent publishes a perfectly valid card and can make **outbound**
A2A calls — `azure-ai-agentserver-core` even documents forwarding `x-agent-foundry-call-id` /
`x-agent-user-id` on outbound calls to *"Storage, Toolboxes/MCP proxy, A2A"*. Only the
**inbound** gate is missing.

**Impact:** a real A2A hop needs a prompt agent on the receiving end, hence the two front-ends.
**Workaround:** front-ends mirror their hosted counterparts' persona and are provisioned by one
repeatable script. Re-running it intentionally creates new versions under the same names.
Retire them only after revalidating platform support.

### 5.3 Behaviours worth knowing (not failures)

| Behaviour | Consequence |
|---|---|
| The tested `text/csv` attachment is **flattened into text**, prefixed `[File: <name>]` | A supervisor looking only for file objects would drop it. `_unflatten_file` in [turn_state.py](src/supervisor-agent/turn_state.py) reconstructs it so the payload survives the extra hop. The tested `application/json` remains distinct data; image delivery was not part of this verification. |
| A2A is an **async Task** model, not request/response | `message/send` returns `state: submitted`; you must poll `tasks/get`. Already implemented — and it is the natural hook for Sprint 2's long-running work. |
| Hosted agents reject the project-level `/responses` route | `bad_request: "Hosted agents can only be called through the agent endpoint"`. Use `…/agents/<name>/endpoint/protocols/openai/responses?api-version=v1`. |
| Deleting an agent with live sessions returns **409** | Append `&force=true` to cascade-delete its sessions. |

### 5.4 Explicitly out of scope for Sprint 1

Code Interpreter, arbitrary code/process execution, long-running execution,
large files (3K × 300 / ~75 MB), state
save/restore, conversation isolation, concurrency/SKU/cold-start measurements, BCDR and
geo constraints. See §8.

---

## 6. Running it

### Prerequisites

`azd >= 1.27.1` with the `azure.ai.agents` extension, Azure CLI, and an authenticated
session (`az login`, `azd auth login`). Python 3.13+ and `uv` on PATH. The verification
used azd 1.34.2, local Python 3.14.3, and hosted runtime `python_3_13`.
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

Idempotent: deterministic names, `guid()`-named role assignments, re-runs update in place.
Writes non-secret outputs to `infra/outputs.env`, consumed by the Python scripts, and
imports those outputs into azd. By default it creates/selects `project-a2a-poc-dev`,
sets the project endpoint and enables deployment into that existing project.
Use `-AzdEnvironmentName <name>` to override the environment label.
Use `-WhatIf` to preview an existing resource group without changing it.
Restoring a soft-deleted account requires explicit `-RestoreSoftDeletedAccount`.
Keep the same subscription, naming parameters and agent identities on subsequent runs.

The renamed deployment was successfully reapplied. Its post-deployment what-if had no
resource creates/deletes, but was not an empty diff: Azure returned service-populated
properties and App Insights connection `isSharedToAll` normalization.

### Step 2 — agents

```powershell
.\scripts\sync-shared.ps1
.\scripts\lock-agents.ps1
azd deploy                                  # supervisor, research, analysis
& $python .\scripts\deploy_a2a_frontends.py  # the two prompt A2A front-ends
```

### Step 3 — grant the agents access to each other

Hosted agents call peers with their **own** managed identity, so each needs **Foundry User**
and **Foundry Agent Consumer** on the project:

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
& $python -m unittest discover -s tests -v          # 108 offline regression tests
.\scripts\sync-shared.ps1 -Check
.\scripts\lock-agents.ps1 -Check
& $python .\scripts\prove_a2a_parts.py                    # 4 sections, 15 assertions
& $python .\scripts\prove_a2a_parts.py --skip-supervisor  # transport checks only
& $python .\scripts\prove_excel.py                  # workbook proof only
& $python .\scripts\prove_a2a_parts.py --excel        # original proof plus workbook proof
```

The Excel proof creates an in-memory workbook with `Sales` and `Targets` sheets.
It checks exact byte length and SHA256, both sheets' dimensions, real inspect/read/
aggregate/compare tool calls, and exact regional/overall results on both the direct
analysis path and supervisor delegation. Expected totals are **176,500 actual** versus
**180,000 target**, a **3,500 shortfall**. Those answers are not given in the prompt.
Diagnostics are removed before checking that the model explains the computed totals.
The offline suite additionally covers formula caches (zero, missing and saved-empty
results), formatting-only cells, chart sheets, printer-settings parts, totals rows,
all-blank groups, corrupt/unsupported ZIP structures, oversized workbooks, multiple-sheet
limits, decimal arithmetic, bad headers/keys, explicit tool failures, concurrent parses
and turn state, research routing with workbooks, and streamed evidence.

### Upload your own workbook

```powershell
& $python .\scripts\analyze_excel.py .\sales.xlsx --question `
  "Inspect the sheets, sum revenue on Sales by region, and compare Sales revenue with Targets target using region as the key."
```

This reads your local file and sends it to the configured Foundry supervisor; it does not
create a new storage account or enable Code Interpreter. The command reports failure if
the supervisor does not delegate to analysis or the recorded Excel tool execution fails.

### Try it by hand

```powershell
azd ai agent invoke supervisor-agent --protocol responses --new-conversation `
  "Ask research what drives cloud seasonality, then have analysis quantify a 4% EMEA gap on 50000."
azd ai agent invoke supervisor-agent --protocol responses --new-conversation "Does A2A support file parts? Probe research."
azd ai agent invoke supervisor-agent --protocol responses --new-conversation "Who can you reach, and over which transport?"
azd ai agent invoke supervisor-agent --protocol responses --new-conversation `
  "Use ask_over_a2a to ask research for one cloud seasonality factor, then ask analysis over A2A to quantify 4% of 50000."
```

The sample phrase "cloud seasonality" was interpreted as weather in the measured run.
Specify "public-cloud revenue seasonality" when that is the intended subject. Inspect the
hop log, not just the prose: successful Responses hops must be `completed`; A2A hops
must include successful Task results. The probe intentionally records an A2A rejection.

---

## 7. Gotchas

Each of these cost real debugging time.

| Symptom | Cause | Fix |
|---|---|---|
| `azd deploy` → `[CodeError] … Please review your uv.lock`, naming a transitive package | A **private registry or wheel/sdist URL** is unreachable by the Foundry builder; rewriting only `registry` is insufficient | `./scripts/lock-agents.ps1` normalizes the registry and resolves each non-public artifact to public PyPI metadata by exact SHA256. Missing matches fail explicitly. All three locks resolved 108 packages; hosted deployment succeeded after this fix. |
| Same error after normalization | The package name alone does not identify the root cause | Run `lock-agents.ps1 -Check`, inspect build diagnostics and network access. Do not assume it is intermittent; these scripts contain no deployment retry loop. |
| Same error on *every* attempt after editing `.azdignore` | Extra `.azdignore` patterns break the code package | Keep `.azdignore` to `.env.example`. Adding `.venv/`, `__pycache__/`, `*.pyc` failed 5/5 across two agents; reverting fixed it first try. |
| Evidence block missing from a reply | The host streams, so post-hoc mutation never surfaces | Wrap with `with_received_parts_evidence` / `with_hop_log_evidence` (`flat_map` on the terminal update) |
| Supervisor "loses" an attachment | Host flattened a `text/*` file into text | Handled by `_unflatten_file`; keep it in any new forwarding path |
| `bad_request: Hosted agents can only be called through the agent endpoint` | Used project-level `/responses` with `agent_reference` | Use the per-agent endpoint |
| `409 conflict` deleting a prompt front-end | Live or idle-but-unexpired sessions | Only if session destruction is intended, use `deploy_a2a_frontends.py --delete --force` |
| Agent can't reach a peer (401/403) | New managed identity after rename/recreate | Re-run `deploy-infra.ps1 -AgentPrincipalIds …` |

The active azd environment is now `project-a2a-poc-dev`. The old `research-agent-dev`
environment and old `aif-*` / `log-*` / `appi-*` resources were deliberately preserved.
They may still incur charges; renaming is not migration or cleanup.

---

## 8. Sprint 2 backlog

The second half of the whiteboard. Sprint 1 deliberately left the hooks in place.

| Goal | Where it plugs in | What Sprint 1 already gives you |
|---|---|---|
| **Long-running work** | `analysis-agent` | A2A is already an async Task model; `a2a_client.py` already polls `tasks/get` with timeout/backoff |
| **Code Interpreter / general code execution** | `analysis-agent` | Sprint 1 has bounded, explicit Excel tools only. Sandbox execution, generated charts/files and isolation need separate implementation and verification. |
| **Large files** (3K rows × 300 cols, ~75 MB) | both specialists | Inline base64 will not scale — move to `input_file` + `file_url`/`file_id`. `file_part_uri` and the `file_url` branch are already written |
| **State save / restore** | all three | `agent_framework_foundry_hosting` ships `FoundryCheckpointStore` and `FoundryAgentSessionStore` |
| **Conversation isolation** (engagement ID + agent MI) | supervisor | Platform injects `x-agent-foundry-call-id` / `x-agent-user-id`; `responses_client.py` has the forwarding list |
| **Scale** — max concurrency, SKU, memory ceiling, cold start (~10 s target) | infra | `container.resources` in `azure.yaml`; tiers `0.25/0.5Gi`, `1/2Gi`, `2/4Gi` |
| **Cross-Foundry calls / BCDR, geo constraints** (AME/EMEA/APAC) | infra | Single region today; `remote-a2a` connections are the cross-project path |

**Open questions for the Foundry product team**

1. When will hosted agents accept **inbound** A2A? (§5.2)
2. Will the A2A gate ever carry `DataPart` / `FilePart`? (§5.1)
3. Is the `text/*` → text flattening (§5.3) intentional, and can it be opted out of?
4. Can remote-build diagnostics identify an unreachable artifact URL instead of only a package name? (§7)

---

## 9. Tear down

**Destructive and not executed during this verification.** Deleting this resource group
also deletes the preserved old deployment and any unrelated resources in it. Review the
group contents and obtain approval before running these commands. Purge prevents recovery.
Use the same explicit subscription selected in §6.

```powershell
& $python .\scripts\deploy_a2a_frontends.py --delete
# If sessions block deletion, add --force only to intentionally delete those sessions.
az group delete -n rg-foundry-hostedagents --subscription $subscriptionId --yes
az cognitiveservices account purge -g rg-foundry-hostedagents -l swedencentral -n <account-name> --subscription $subscriptionId
```
