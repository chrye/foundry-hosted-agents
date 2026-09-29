# Why five agents?

Recorded deployment on **2026-09-28**: `project-a2a-poc`, account
`foundry-fha-kxusxjc5tkc3y`.

| Agent | Kind / version | Role |
|---|---|---|
| `supervisor-agent` | hosted / 5 | Python orchestration with `ask_agent`, `list_agents`, `probe_part_support` and a hop log |
| `research-agent` | hosted / 3 | Research from supplied content/model knowledge; receipt evidence; no browsing tool |
| `analysis-agent` | hosted / 4 | Dataset analysis, four explicit Excel tools, receipt and calculation evidence |
| `research-agent-a2a` | prompt / 1 | Independent research persona, model and instructions only |
| `analysis-agent-a2a` | prompt / 1 | Independent quantitative persona, model and instructions only |

The hosted agents run Python 3.13 and deploy through [azure.yaml](./azure.yaml).
[deploy_a2a_frontends.py](./scripts/deploy_a2a_frontends.py) creates versions of the two
prompt agents, with no tools, and enables both Responses and A2A on their endpoints.

**Why the extra two?** The tested Foundry gateway refused hosted A2A targets
(`-32099 HostedAgentNotSupported`). Prompt targets completed text-only A2A Tasks.
They let the POC demonstrate A2A without claiming that it invokes the hosted Python code.
Native A2A data/file parts were rejected (`-32005`); this is a measured Foundry limitation,
not a restriction of every A2A implementation.

**They are not proxies.** Calling a `-a2a` agent does not call its hosted namesake or its
Excel tools. Both research cards advertise `research-brief`, so the model can select
either for a text task. Check `peer` in the hop log to see who answered.

**Discovery and files.** The supervisor reads project agents and their cards, excludes
itself using `FOUNDRY_AGENT_NAME`, and caches the directory for 5 minutes.
`list_agents` and an unknown-name lookup refresh it. No peer map is configured.
Ordinary delegation sends uploads only to a selected peer with the relevant skill tag:
`file` for non-workbook attachments, `excel` for `.xlsx`. Currently research has `file`,
analysis has both, and the prompt agents have neither. These tags control forwarding,
not file-format support or trust; prompt-agent multipart Responses support was not tested.

See the [README](./README.md) for exact Excel limits, verification results and the plan
to retire the front-ends after hosted inbound A2A support is reverified.

## Call flow

This illustrates the recorded deployment's normal paths, not a guaranteed tool order.
It starts with a supervisor request; the proof scripts also call specialists directly.

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
        alt ask_agent auto, no eligible attachments (hosted research example)
            opt No remembered refusal for this peer
                S->>F: A2A message/send at card URL
                F-->>S: -32099 HostedAgentNotSupported<br/>(remember refusal for this process)
            end
            S->>R: Responses: text
            R-->>S: answer + A2A-PART-INVENTORY
        else ask_agent auto with eligible workbook (hosted analysis example)
            S->>A: Responses: text + the attachments<br/>its card accepts (file / excel tags)
            Note over A: its model calls explicit<br/>Python tools for .xlsx
            A-->>S: analysis + A2A-PART-INVENTORY<br/>+ EXCEL-ANALYSIS
        else ask_agent to a prompt agent (A2A)
            S->>P: blocking message/send at card URL (text only)
            P-->>S: completed Task + text artifact
            Note over S,P: ask_agent withholds uploads on this A2A path.<br/>tasks/get polling exists for pending Tasks (unit-tested only).
        else list_agents or probe_part_support
            Note over S,P: list_agents: refreshes the directory.<br/>probe_part_support: synthetic JSON and CSV,<br/>not caller uploads. Native A2A parts are rejected<br/>(-32005). Responses files are delivered.
        end
    end

    S->>M: latest tool results
    M-->>S: final answer,<br/>sections attributed per agent
    Note over S: appends A2A-HOP-LOG<br/>(discovery + hops)<br/>to the final stream update
    S-->>C: [supervisor] answer<br/>+ A2A-HOP-LOG
```

</details>

### Reading the evidence

- `auto` tries A2A only when no eligible attachments need forwarding and no refusal is
  remembered. After a hosted refusal, it uses Responses. Explicit `a2a` never falls back;
  explicit `responses` bypasses A2A.
- The research-before-analysis order is an instruction, not an enforced dependency.
  Calls may run concurrently.
- Discovery is an initial per-turn snapshot, not a live health guarantee. Card-fetch
  failures omit the peer with a warning; directory-listing failures appear in the hop log.
- The supervisor appends discovery and hops; hosted specialists report received content.
  Analysis adds workbook hashes and tool results. These are application diagnostics,
  not signed attestations or proof of caller isolation.
