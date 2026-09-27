"""Supervisor: a Foundry hosted agent that orchestrates two specialists over two transports.

Phase 1 proof surface:
  * discovery  - every peer is resolved from its published **agent card**
  * A2A        - `message/send` + `tasks/get` against the prompt-agent front-ends
                 (Foundry's A2A gate is text-only and refuses hosted targets)
  * Responses  - `input_text` / `input_file` against the hosted specialists, which is the
                 transport that actually carries data and file payloads today
  * evidence   - a deterministic hop log is appended to every reply by middleware
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from agent_framework import Agent, AgentContext, AgentMiddleware, AgentResponse, Content, Message
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv
from pydantic import Field

from a2a_client import A2AError, A2AProtocolError, FoundryA2AClient, data_part, file_part_bytes, text_part
from a2a_parts import INVENTORY_MARKER
from responses_client import FoundryResponsesClient, ResponsesError, text_content
from turn_state import (
    attachment_summary,
    capture_a2a_attachments,
    capture_responses_attachments,
    get_a2a_attachments,
    get_responses_attachments,
    hop_log_block,
    record_hop,
    reset_hop_log,
    set_attachments,
    with_hop_log_evidence,
)

load_dotenv()
logger = logging.getLogger(__name__)

RESEARCH_AGENT = os.getenv("RESEARCH_AGENT_NAME", "research-agent")
ANALYSIS_AGENT = os.getenv("ANALYSIS_AGENT_NAME", "analysis-agent")
RESEARCH_A2A = os.getenv("RESEARCH_A2A_AGENT_NAME", "research-agent-a2a")
ANALYSIS_A2A = os.getenv("ANALYSIS_A2A_AGENT_NAME", "analysis-agent-a2a")

_A2A_PEERS = {"research": RESEARCH_A2A, "analysis": ANALYSIS_A2A}

_a2a: FoundryA2AClient | None = None
_responses: FoundryResponsesClient | None = None
_lock = asyncio.Lock()


async def _clients() -> tuple[FoundryA2AClient, FoundryResponsesClient]:
    global _a2a, _responses
    if _a2a is None or _responses is None:
        async with _lock:
            endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
            if _a2a is None:
                _a2a = FoundryA2AClient(endpoint)
            if _responses is None:
                _responses = FoundryResponsesClient(endpoint)
    return _a2a, _responses


# --------------------------------------------------------------------------------------
# Delegation
# --------------------------------------------------------------------------------------


async def _delegate_responses(peer: str, question: str) -> str:
    """Call a hosted specialist over the Responses protocol, forwarding attachments."""
    _, responses = await _clients()
    content = [text_content(question), *get_responses_attachments()]

    hop: dict[str, Any] = {
        "peer": peer,
        "transport": "responses",
        "url": responses.endpoint_url(peer),
        "sent_content_types": [c["type"] for c in content],
    }
    try:
        reply = await responses.send(peer, content)
    except ResponsesError as exc:
        hop["ok"] = False
        hop["error"] = str(exc)[:600]
        record_hop(hop)
        logger.warning("Responses delegation to %s failed: %s", peer, exc)
        return f"The call to '{peer}' failed: {exc}"

    hop["ok"] = True
    hop["status"] = reply.status
    hop["received_content_types"] = reply.content_types
    hop["reply_chars"] = len(reply.text)
    hop["received_parts"] = _received_parts(reply.text)
    record_hop(hop)
    return reply.text or "(the specialist returned no text)"


def _received_parts(text: str) -> list[dict[str, Any]]:
    for block in reversed(re.findall(r"```json\s*(.*?)```", text, re.DOTALL)):
        try:
            payload = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("marker") == INVENTORY_MARKER:
            parts = payload.get("parts")
            if isinstance(parts, list) and all(isinstance(part, dict) for part in parts):
                return parts
    logger.warning("Specialist response has no valid %s evidence", INVENTORY_MARKER)
    return []


async def _delegate_a2a(peer: str, question: str, extra_parts: list[dict[str, Any]] | None = None) -> str:
    """Call a peer over A2A, discovering it via its agent card."""
    a2a, _ = await _clients()
    parts = [text_part(question), *(extra_parts or [])]

    hop: dict[str, Any] = {
        "peer": peer,
        "transport": "a2a",
        "card_url": a2a.card_url(peer),
        "sent_part_kinds": [p["kind"] for p in parts],
    }
    try:
        reply = await a2a.send_message(peer, parts)
    except A2AProtocolError as exc:
        hop["ok"] = False
        hop["error_code"] = exc.code
        hop["error_reason"] = exc.reason
        hop["error"] = str(exc)[:400]
        record_hop(hop)
        return f"The A2A call to '{peer}' was rejected ({exc.reason}): {exc}"
    except A2AError as exc:
        hop["ok"] = False
        hop["error"] = str(exc)[:600]
        record_hop(hop)
        return f"The A2A call to '{peer}' failed: {exc}"

    hop["ok"] = True
    hop["received_part_kinds"] = reply.part_kinds
    hop["reply_chars"] = len(reply.text)
    if reply.task_id:
        hop["task_id"] = reply.task_id
    record_hop(hop)
    return reply.text or "(the peer returned no text)"


# --------------------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------------------


async def ask_research(
    question: Annotated[str, Field(description="The research question or topic to investigate.")],
) -> str:
    """Delegate to the research specialist for a sourced research brief.

    Uses the Responses transport, so any data or files the caller attached this turn are
    forwarded automatically. Never paste attachment contents into the question.
    """
    return await _delegate_responses(RESEARCH_AGENT, question)


async def ask_analysis(
    question: Annotated[str, Field(description="The quantitative question to answer.")],
) -> str:
    """Delegate to the data-analysis specialist for metrics, trends and insights.

    Uses the Responses transport, so any data or files the caller attached this turn are
    forwarded automatically. Never paste attachment contents into the question.
    """
    return await _delegate_responses(ANALYSIS_AGENT, question)


async def ask_over_a2a(
    specialist: Annotated[
        str, Field(description="Which specialist to reach over A2A: 'research' or 'analysis'.")
    ],
    question: Annotated[str, Field(description="A text-only question. A2A cannot carry attachments.")],
) -> str:
    """Reach a specialist over the A2A protocol via its published agent card.

    Use when the caller explicitly asks for an A2A hop. This leg is text-only: Foundry's
    A2A gate rejects data and file parts.
    """
    peer = _A2A_PEERS.get(specialist.strip().lower())
    if peer is None:
        return f"Unknown specialist '{specialist}'. Use 'research' or 'analysis'."

    answer = await _delegate_a2a(peer, question)

    # The A2A projection of this turn's attachments exists and is correct, but the gate
    # refuses non-text parts - so say what was left behind instead of dropping it silently.
    dropped = get_a2a_attachments()
    if dropped:
        kinds = sorted({part["kind"] for part in dropped})
        answer += (
            f"\n\n_Note: {len(dropped)} attachment(s) ({', '.join(kinds)}) were not sent on "
            "this hop — Foundry's A2A gate is text-only. Use ask_research or ask_analysis "
            "to deliver them over the Responses transport._"
        )
    return answer


async def list_specialists() -> str:
    """List every peer this supervisor can reach, with the agent card each one publishes."""
    a2a, responses = await _clients()
    out: list[dict[str, Any]] = []

    for name, transport in (
        (RESEARCH_AGENT, "responses"),
        (ANALYSIS_AGENT, "responses"),
        (RESEARCH_A2A, "a2a"),
        (ANALYSIS_A2A, "a2a"),
    ):
        entry: dict[str, Any] = {"name": name, "transport": transport}
        if transport == "responses":
            entry["url"] = responses.endpoint_url(name)
        try:
            card = await a2a.fetch_agent_card(name)
            entry["card"] = {
                "description": card.get("description"),
                "skills": [s.get("id") for s in card.get("skills") or []],
                "defaultInputModes": card.get("defaultInputModes"),
                "interfaces": [i.get("protocolBinding") for i in card.get("supportedInterfaces") or []],
            }
        except A2AError as exc:
            entry["card"] = {"error": str(exc)[:200]}
        out.append(entry)
    return json.dumps(out, indent=2)


async def probe_part_support(
    specialist: Annotated[
        str, Field(description="Which specialist to probe: 'research' or 'analysis'.")
    ] = "research",
) -> str:
    """Compare part support across both transports by sending the same payload to each.

    Sends text + structured data + a CSV file over A2A and over Responses, and reports what
    each transport accepted. Use this when asked whether A2A supports non-text parts.
    """
    key = specialist.strip().lower()
    hosted = {"research": RESEARCH_AGENT, "analysis": ANALYSIS_AGENT}.get(key)
    a2a_peer = _A2A_PEERS.get(key)
    if hosted is None or a2a_peer is None:
        return f"Unknown specialist '{specialist}'. Use 'research' or 'analysis'."

    csv = b"region,units,revenue\nEMEA,1200,48000\nAMER,1850,79500\nAPAC,940,31200\n"
    probe_data = {
        "probe": "a2a-part-support",
        "quarter": "FY26Q1",
        "targets": {"EMEA": 50000, "AMER": 75000, "APAC": 35000},
    }
    question = (
        "Part-support probe. Summarise the attached data and file in two sentences, then "
        "state exactly which part kinds you received."
    )

    a2a_result = await _delegate_a2a(
        a2a_peer,
        question,
        extra_parts=[data_part(probe_data), file_part_bytes("regional-sales.csv", "text/csv", csv)],
    )

    from responses_client import file_content

    _, responses = await _clients()
    content = [
        text_content(question),
        file_content("targets.json", "application/json", json.dumps(probe_data).encode("utf-8")),
        file_content("regional-sales.csv", "text/csv", csv),
    ]
    hop: dict[str, Any] = {
        "peer": hosted,
        "transport": "responses",
        "probe": True,
        "url": responses.endpoint_url(hosted),
        "sent_content_types": [part["type"] for part in content],
    }
    try:
        reply = await responses.send(
            hosted,
            content,
        )
        hop["ok"] = True
        hop["status"] = reply.status
        hop["received_content_types"] = reply.content_types
        hop["received_parts"] = _received_parts(reply.text)
        responses_result = reply.text
    except ResponsesError as exc:
        hop["ok"] = False
        hop["error"] = str(exc)[:400]
        responses_result = f"failed: {exc}"
    record_hop(hop)

    return (
        f"Sent the same text + data + file payload over both transports.\n\n"
        f"### A2A leg (`{a2a_peer}`, discovered via agent card)\n{a2a_result}\n\n"
        f"### Responses leg (`{hosted}`, hosted agent)\n{responses_result}\n\n"
        f"The `{INVENTORY_MARKER}` block in the Responses reply is generated by the "
        f"specialist's middleware, not by its model."
    )


# --------------------------------------------------------------------------------------
# Agent
# --------------------------------------------------------------------------------------


class SupervisorTurnMiddleware(AgentMiddleware):
    """Capture the caller's attachments for forwarding, then append the hop log."""

    async def process(
        self,
        context: AgentContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        set_attachments(
            capture_a2a_attachments(context.messages),
            capture_responses_attachments(context.messages),
        )
        reset_hop_log()

        summary = attachment_summary()
        if summary:
            context.messages.append(
                Message(
                    role="system",
                    contents=[
                        Content.from_text(
                            f"The caller attached {len(summary)} non-text part(s): "
                            f"{json.dumps(summary)}. Calling ask_research or ask_analysis "
                            "forwards them to that specialist automatically. Do not inline "
                            "their contents into tool arguments."
                        )
                    ],
                )
            )

        await call_next()

        if isinstance(context.result, AgentResponse) and context.result.messages:
            context.result.messages[-1].contents.append(
                Content.from_text("\n\n" + hop_log_block())
            )


INSTRUCTIONS = f"""You are the Supervisor of a multi-agent system running on Microsoft Foundry.

You never answer research or analysis questions yourself. You route them:
- `ask_research` -> the research specialist ('{RESEARCH_AGENT}').
- `ask_analysis` -> the data-analysis specialist ('{ANALYSIS_AGENT}'), for anything numeric
  or dataset-shaped.
- `ask_over_a2a` -> the same specialists reached over the A2A protocol via their agent cards.
  Use only when the caller explicitly asks for an A2A hop. Text-only.
- `list_specialists` -> when asked who you can reach or what they can do.
- `probe_part_support` -> when asked whether A2A carries non-text parts.

When a question needs both specialists, call research first and feed its findings into the
analysis question. Never write a section attributed to a specialist you did not actually
call — if a delegation failed, say so plainly instead of inventing its answer.

If the caller attached data or a file, `ask_research` and `ask_analysis` forward it
automatically as native content parts. Never paste attachment contents into a tool argument.

Compose the replies into one answer and attribute each section to the agent that produced it,
for example `### From research-agent`. Start your reply with `[supervisor]`."""


def main() -> None:
    client = FoundryChatClient(
        project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
        credential=DefaultAzureCredential(),
    )

    agent = Agent(
        client=client,
        name="supervisor-agent",
        instructions=INSTRUCTIONS,
        tools=[ask_research, ask_analysis, ask_over_a2a, list_specialists, probe_part_support],
        middleware=[SupervisorTurnMiddleware()],
        # History is managed by the hosting infrastructure.
        default_options={"store": False},
    )

    ResponsesHostServer(with_hop_log_evidence(agent)).run()


if __name__ == "__main__":
    main()
