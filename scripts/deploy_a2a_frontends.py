"""Provision the prompt-agent A2A front-ends for the two specialists (idempotent).

Foundry's A2A gate refuses hosted agents as targets (`-32099 HostedAgentNotSupported`),
so a real A2A `message/send` hop needs a **prompt** agent on the receiving end. This script
creates (or re-versions) one prompt agent per specialist, publishes its agent card, and
enables `a2a` on its endpoint.

These front-ends deliberately mirror their hosted counterparts' persona. They are the A2A
face of each specialist; the hosted agent remains the one that does file- and data-bearing
work over the Responses protocol.

Usage:
    python scripts/deploy_a2a_frontends.py
    python scripts/deploy_a2a_frontends.py --delete   # tear the front-ends down
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

REPO_ROOT = Path(__file__).resolve().parent.parent

A2A_NOTE = (
    "You are reached over the A2A protocol, which on Foundry is text-only: callers cannot "
    "attach data or file parts. If the caller refers to a dataset or document you cannot "
    "see, say so plainly and answer from the text you were given."
)

FRONT_ENDS = {
    "research-agent-a2a": {
        "description": "A2A front-end for the research specialist: sourced research briefs from a topic or question.",
        "instructions": f"""You are the Research specialist in a multi-agent system.

Given a topic or question, produce a concise research brief in Markdown:

## Summary
Two or three sentences.

## Key facts
Bullets. Include numbers, dates and named entities where known.

## Assumptions

## Open questions

Be factual; if you are unsure, say so rather than guessing.
{A2A_NOTE}
Always start your reply with the line: `[research-agent-a2a]`""",
        "skills": [
            {
                "id": "research-brief",
                "name": "Research brief",
                "description": "Summarise a topic into a summary, key facts, assumptions and open questions.",
                "tags": ["research", "summarisation"],
                "examples": ["Brief me on the EMEA public cloud market in 2026."],
            }
        ],
    },
    "analysis-agent-a2a": {
        "description": "A2A front-end for the data-analysis specialist: metrics, trends and quantified insights.",
        "instructions": f"""You are the Data Analysis specialist in a multi-agent system.

Given a question and whatever figures appear in the text, produce a quantitative analysis
in Markdown:

## Metrics
A Markdown table of the key numbers, with units.

## Trends and comparisons

## Insights
Three to five bullets, each tied to a number.

## Confidence
High / Medium / Low with a one-line justification.

Never invent precise figures; label estimates as estimates.
{A2A_NOTE}
Always start your reply with the line: `[analysis-agent-a2a]`""",
        "skills": [
            {
                "id": "quantitative-review",
                "name": "Quantitative review",
                "description": "Quantify findings and sanity-check the numbers in a research brief.",
                "tags": ["analysis", "validation"],
                "examples": ["Quantify the upside if EMEA closes its 4% gap to target."],
            }
        ],
    },
}


def load_env() -> dict[str, str]:
    env_file = REPO_ROOT / "infra" / "outputs.env"
    if not env_file.exists():
        sys.exit("infra/outputs.env not found. Run ./scripts/deploy-infra.ps1 first.")
    values: dict[str, str] = {}
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip('"')
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delete", action="store_true", help="Delete the front-end agents.")
    parser.add_argument("--force", action="store_true", help="With --delete, also delete live sessions.")
    args = parser.parse_args()
    if args.force and not args.delete:
        parser.error("--force requires --delete")

    env = load_env()
    endpoint = env["FOUNDRY_PROJECT_ENDPOINT"]
    model = env["AZURE_AI_MODEL_DEPLOYMENT_NAME"]

    credential = DefaultAzureCredential()
    project = AIProjectClient(endpoint=endpoint, credential=credential)
    token = credential.get_token("https://ai.azure.com/.default").token
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    print(f"Foundry project: {endpoint}\nModel: {model}\n")

    failed = False
    with credential, project, httpx.Client(timeout=120.0) as http:
        for name, spec in FRONT_ENDS.items():
            if args.delete:
                suffix = "&force=true" if args.force else ""
                response = http.delete(f"{endpoint}/agents/{name}?api-version=v1{suffix}", headers=headers)
                print(f"{name}: delete -> HTTP {response.status_code}")
                if response.status_code >= 400 and response.status_code != 404:
                    failed = True
                    print(f"{name}: deletion failed: {response.text[:400]}", file=sys.stderr)
                continue

            definition = {
                "kind": "prompt",
                "model": model,
                "description": spec["description"],
                "instructions": spec["instructions"],
            }
            created = project.agents.create_version(agent_name=name, definition=definition)
            version = getattr(created, "version", "?")

            patch = {
                "agent_card": {
                    "description": spec["description"],
                    "version": "1.0",
                    "skills": spec["skills"],
                },
                "agent_endpoint": {"protocols": ["responses", "a2a"]},
            }
            response = http.patch(f"{endpoint}/agents/{name}?api-version=v1", headers=headers, json=patch)
            if response.status_code >= 400:
                failed = True
                print(f"{name}: v{version} created, but endpoint PATCH failed "
                      f"({response.status_code}): {response.text[:400]}")
                continue

            card = http.get(
                f"{endpoint}/agents/{name}/endpoint/protocols/a2a/agentCard/v1.0", headers=headers
            )
            if card.status_code >= 400:
                failed = True
                print(f"{name}: v{version}, A2A enabled but card fetch failed "
                      f"({card.status_code}): {card.text[:300]}")
                continue

            payload = card.json()
            interfaces = [i.get("protocolBinding") for i in payload.get("supportedInterfaces") or []]
            if "JSONRPC" not in interfaces or not payload.get("skills"):
                failed = True
                print(f"{name}: card is missing a JSONRPC interface or skills.", file=sys.stderr)
                continue
            print(
                f"{name}: v{version} | A2A enabled | skills="
                f"{[s.get('id') for s in payload.get('skills') or []]} | "
                f"interfaces={sorted(set(interfaces))} | "
                f"inputModes={payload.get('defaultInputModes')}"
            )

    if failed:
        print("One or more front-end operations failed.", file=sys.stderr)
        return 1
    if not args.delete:
        print("\nFront-ends ready. Agent cards:")
        for name in FRONT_ENDS:
            print(f"  {endpoint}/agents/{name}/endpoint/protocols/a2a/agentCard/v1.0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
