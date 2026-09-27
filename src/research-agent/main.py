"""Research specialist: a Foundry hosted agent reachable over Responses and A2A.

Phase 1 role: given a topic plus whatever text / data / file parts the supervisor
forwards, return a research brief and report which part kinds actually arrived.
"""

import os

from a2a_parts import ReceivedPartsMiddleware, with_received_parts_evidence
from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

load_dotenv()

INSTRUCTIONS = """You are the Research specialist in a multi-agent system. You are called
by a supervisor agent over the Responses protocol.

Given a topic or question — and optionally structured data or an attached file — produce a
concise research brief in Markdown with these sections:

## Summary
Two or three sentences.

## Key facts
Bullets. Include numbers, dates and named entities where known.

## Assumptions

## Open questions

Rules:
- Be factual. If you are unsure, say so explicitly rather than guessing.
- Identify the source of factual claims (the supplied data or a named reference). No live
  browsing tool is available; do not imply you checked current sources or invent citations.
- If the caller sent structured data or a file, ground your brief in its actual contents and
  quote at least one concrete value from it.
- Close with one sentence naming the part kinds you were able to read.
- Always start your reply with the line: `[research-agent]`"""


def main() -> None:
    client = FoundryChatClient(
        project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
        credential=DefaultAzureCredential(),
    )

    agent = Agent(
        client=client,
        name="research-agent",
        instructions=INSTRUCTIONS,
        middleware=[ReceivedPartsMiddleware()],
        # History is managed by the hosting infrastructure.
        default_options={"store": False},
    )

    ResponsesHostServer(with_received_parts_evidence(agent)).run()


if __name__ == "__main__":
    main()
