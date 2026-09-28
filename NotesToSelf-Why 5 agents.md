In the Foundry portal, I see a list of 5 agents in `project-a2a-poc` (account `foundry-fha-kxusxjc5tkc3y`), as of 2026-09-28:
- supervisor-agent
- analysis-agent
- research-agent
- analysis-agent-a2a
- research-agent-a2a

The two `-a2a` agents are **prompt agents**: just a model plus an instructions string. The
other three are **hosted agents**: the POC's Python code running in containers. The portal's
"Type" column says "Agent" for all five, so it hides this difference. The live agent
definitions show `kind: prompt` for the two `-a2a` agents and `kind: hosted` for the other
three.

**Why they exist:** Foundry won't accept an incoming A2A call to a hosted agent (it returns
`-32099 HostedAgentNotSupported`). To show a real A2A call working (`message/send`, then the
task running to completion), the POC needed prompt agents on the receiving end.
[deploy_a2a_frontends.py](./scripts/deploy_a2a_frontends.py) creates them and turns on A2A
for each.

**How the supervisor finds them:** it has no list of names. Each turn it lists the project's
agents, reads the A2A card of every agent except itself, and its model picks agents by the
skills on those cards. So it sees 4 peers, and the two `-a2a` agents show up as peers in
their own right. `research-agent-a2a` has the same `research-brief` skill as
`research-agent`. In the catalog they differ by description, by `kind` (prompt or hosted),
and by the attachment tags (`file`, `excel`), which only the hosted specialists have.

| | `research-agent-a2a`, `analysis-agent-a2a` | `supervisor-agent`, `research-agent`, `analysis-agent` |
|---|---|---|
| Type (live) | `prompt`: `gpt-5.4-mini` plus instructions, **no tools, no code** | `hosted`: Python 3.13 container running `main.py` (0.5–1 CPU) |
| Created by | `deploy_a2a_frontends.py`; not in `azure.yaml` | `azd deploy` from [azure.yaml](./azure.yaml) |
| Code behind it | None | Middleware that reports exactly what each agent received. `analysis-agent` has 4 Excel tools. `supervisor-agent` has 3 tools (`ask_agent`, `list_agents`, `probe_part_support`) and discovers the others from their cards at runtime |
| Can other agents call it over A2A? | Yes, text only | No (`-32099`) |
| Can it receive files or data? | No. Their cards have no `file` / `excel` tags, so the supervisor never forwards attachments to them | Yes, over the Responses API. The `file` tag on research's card and the `file` + `excel` tags on analysis's card are what make the supervisor forward attachments to them |
| Who calls it | The supervisor's `ask_agent` over A2A: when the caller asks for an A2A hop, or when the model picks one for a text-only sub-task. Also `probe_part_support` and the test script ([prove_a2a_parts.py](./scripts/prove_a2a_parts.py)) | research/analysis: the supervisor's `ask_agent`, the path that does the real work. Text tries A2A first, is refused, and then uses Responses; attachments go straight over Responses. supervisor: the client |
| Card skills | 1 each, no attachment tags: `research-brief`; `quantitative-review` | supervisor: 2; research: 2, incl. `document-review` (`file`, `data`); analysis: 3, incl. `excel-analysis` (`excel`, `file`, `analysis`) |
| Versions | v1; changes only when the script is re-run | New version on every `azd deploy`; now supervisor v5, research v3, analysis v4 |

**The main thing to know: they are not proxies.** Calling `research-agent-a2a` never reaches
`research-agent`. It's a separate model answering from its own copied instructions, and those
instructions differ from the originals:

- The hosted [research-agent](./src/research-agent/main.py#L18) and
  [analysis-agent](./src/analysis-agent/main.py#L20) are told to use attachments, cite sources
  and use the Excel tools.
- The `-a2a` copies are told they only get text.

So their answers can differ from the hosted agents' answers and will drift if either side is
edited. A successful A2A call to them shows that A2A works. It says nothing about how the
hosted specialists behave. When "supervisor talks to research over A2A", it is really talking
to a stand-in for research. With card-based routing, the model can also pick a stand-in for
a text-only sub-task, because both research agents advertise the same skill. The hop log's
`peer` field says which agent actually answered. The [README](./README.md)'s convergence
plan is to retire both once Foundry accepts incoming A2A calls to hosted agents.

**Call flow, starting from the client call:**

![Sequence diagram of the call flow from the client through supervisor-agent to the specialists](./NotesToSelf-call-flow.svg)

<details>
<summary>Mermaid source (the SVG above is rendered from this with Mermaid 11's default theme)</summary>

```mermaid
sequenceDiagram
    autonumber
    actor C as Client<br/>(azd invoke, scripts)
    box rgba(66,133,244,0.12) Hosted agents - Python code in containers
        participant S as supervisor-agent
        participant R as research-agent
        participant A as analysis-agent
    end
    participant F as Foundry project API<br/>(agent list, cards,<br/>A2A gateway)
    participant M as gpt-5.4-mini<br/>(supervisor's model)
    box rgba(255,167,38,0.15) Prompt agents - model + instructions only
        participant P as research-agent-a2a<br/>or analysis-agent-a2a
    end

    Note over C,P: Agent calls go through Foundry's per-agent<br/>endpoints with an Entra token. Hosted agents<br/>call out with their own managed identity.

    C->>S: Responses API:<br/>input_text +<br/>optional input_file<br/>(JSON, CSV, .xlsx)
    Note over S: Middleware: capture attachments,<br/>reset the hop log, swap .xlsx bytes<br/>for metadata before the model sees it

    opt Discovery, cached 5 min (list_agents or an unknown name refreshes it)
        S->>F: GET /agents (paged)
        F-->>S: 5 agents and their kind
        S->>F: GET agentCard/v1.0<br/>for each agent except itself
        F-->>S: 4 cards: skills, tags,<br/>JSONRPC url
    end
    Note over S: Middleware gives the model the card<br/>catalog (skills, tags, accepted<br/>attachments) and logs what it found

    loop Until the model stops calling tools
        S->>M: instructions, card catalog,<br/>history, tool results so far
        M-->>S: next tool call
        alt ask_agent to a hosted agent, text only (research-agent here)
            S->>F: A2A message/send<br/>(first call per process only)
            F-->>S: -32099 HostedAgentNotSupported<br/>(remembered, later calls skip A2A)
            S->>R: Responses: text
            R-->>S: answer + A2A-PART-INVENTORY
        else ask_agent to a hosted agent, with attachments (analysis-agent here)
            S->>A: Responses: text + the attachments<br/>its card accepts (file / excel tags)
            Note over A: its model calls the<br/>Python Excel tools
            A-->>S: analysis + A2A-PART-INVENTORY<br/>+ EXCEL-ANALYSIS
        else ask_agent to a prompt agent (A2A)
            S->>P: message/send (text part only)
            P-->>S: task, state submitted
            loop Until the task completes
                S->>P: tasks/get
                P-->>S: task state, then artifacts
            end
            Note over S,P: Attachments stay behind (A2A is text-only)
        else list_agents or probe_part_support
            Note over S,P: list_agents: re-reads the agent list and all cards.<br/>probe_part_support: sends text + data + file<br/>both ways. A2A rejects it (-32005),<br/>Responses accepts it.
        end
    end

    S->>M: latest tool results
    M-->>S: final answer,<br/>sections attributed per agent
    Note over S: appends A2A-HOP-LOG<br/>(discovery + hops)<br/>to the final stream update
    S-->>C: [supervisor] answer<br/>+ A2A-HOP-LOG
```

</details>

How to read it:

- **Start:** the client only ever calls `supervisor-agent`, over the Responses API.
- **Discovery:** before its model runs, the supervisor lists the project's agents and reads
  every card except its own (cached for 5 minutes). No peer names are configured. The
  hop log's `discovery` block shows what was found.
- **The loop:** each round, the supervisor's model picks an agent from the card catalog and
  a tool, so routing is decided by the model and the cards, not hard-coded. The
  instructions say to call research first when both specialists are needed, but the order
  isn't guaranteed. In the measured runs the model often called both at once.
- **Hosted agents, text only:** `ask_agent` tries A2A first, at the URL on the card. The
  Foundry gateway refuses hosted targets (`-32099`), so the supervisor falls back to
  Responses and remembers the refusal. Later calls in the same process go straight to
  Responses.
- **Attachments:** they always go over Responses, and only to agents whose card skills carry
  the matching tag (`file`, or `excel` for `.xlsx`). Today research gets JSON and CSV but not
  `.xlsx`, analysis gets everything, and the `-a2a` stand-ins get nothing.
- **Prompt agents:** reached over A2A as text only, when the caller asks for an A2A hop or
  the model picks one for a text-only sub-task.
- **End:** the hop log (discovery plus every hop) is added to the final answer before it
  returns to the client.


