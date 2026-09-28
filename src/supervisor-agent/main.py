"""Supervisor: a Foundry hosted agent that routes work to peers discovered from A2A cards.

Proof surface:
  * discovery  - no peer names are configured: the project's agent list is read at runtime
                 and each agent's published **A2A agent card** describes what it can do
  * selection  - the model picks a peer from the card catalog (skills, tags, examples)
  * A2A        - text goes first to the JSONRPC interface on the card (`message/send` +
                 `tasks/get`); Foundry refuses hosted targets, which then fall back to...
  * Responses  - `input_text` / `input_file` on the peer's agent endpoint. Attachments always
                 use it, and only reach peers whose card skills advertise support for them
  * evidence   - a deterministic discovery record and hop log are appended to every reply
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

import httpx
from agent_framework import Agent, AgentContext, AgentMiddleware, AgentResponse, Content, Message
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv
from pydantic import Field

from a2a_client import A2AError, A2AProtocolError, FoundryA2AClient, data_part, file_part_bytes, text_part
from a2a_parts import INVENTORY_MARKER
from agent_directory import AgentDirectory, Peer
from responses_client import FoundryResponsesClient, ResponsesError, text_content
from turn_state import (
    attachment_summary,
    capture_a2a_attachments,
    capture_responses_attachments,
    get_responses_attachments,
    hop_log_block,
    record_hop,
    reset_hop_log,
    set_attachments,
    set_discovery,
    with_hop_log_evidence,
)
from xlsx_attachments import EXCEL_ANALYSIS_MARKER, XLSX_MEDIA_TYPE, workbook_model_messages

load_dotenv()
logger = logging.getLogger(__name__)

_DISCOVERY_SOURCE = "GET {project}/agents + each agent's /endpoint/protocols/a2a/agentCard/v1.0"
_HOSTED_REFUSAL = "HostedAgentNotSupported"

_a2a: FoundryA2AClient | None = None
_responses: FoundryResponsesClient | None = None
_directory: AgentDirectory | None = None
_lock = asyncio.Lock()
# What each peer's A2A endpoint did when called ("accepted" / "refused"), learned at runtime.
_a2a_inbound: dict[str, str] = {}
_A2A_INBOUND_TEXT = {
    "accepted": "accepted",
    "refused": "refused by the platform; ask_agent reaches this agent over Responses instead",
}


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


async def _get_directory() -> AgentDirectory:
    global _directory
    if _directory is None:
        a2a, _ = await _clients()
        async with _lock:
            if _directory is None:
                self_name = os.getenv("FOUNDRY_AGENT_NAME")
                if not self_name:
                    logger.warning("FOUNDRY_AGENT_NAME is not set; the supervisor cannot exclude itself.")
                _directory = AgentDirectory(a2a, self_name=self_name)
    return _directory


def _catalog(peers: dict[str, Peer], *, a2a_status: bool = False) -> list[dict[str, Any]]:
    """Card summaries for the model; `list_agents` adds what each A2A endpoint did so far.

    The per-turn catalog leaves the A2A status out: a hosted agent the platform refused over
    A2A is still reachable over Responses, and a bare "refused" steered the model away from it.
    """
    catalog = []
    for name, peer in sorted(peers.items()):
        entry = peer.summary()
        if a2a_status:
            entry["a2a_inbound"] = _A2A_INBOUND_TEXT.get(_a2a_inbound.get(name, ""), "not yet attempted")
        catalog.append(entry)
    return catalog


# --------------------------------------------------------------------------------------
# Delegation
# --------------------------------------------------------------------------------------


def _is_workbook(part: dict[str, Any]) -> bool:
    return (str(part.get("filename", "")).lower().endswith(".xlsx")
            or str(part.get("file_data", "")).startswith(f"data:{XLSX_MEDIA_TYPE};"))


def _attachment_name(part: dict[str, Any]) -> str:
    return str(part.get("filename") or part.get("type") or "attachment")


def _deliverable(peer: Peer, attachments: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Split attachments into those the peer's card advertises support for, and the rest."""
    sent: list[dict[str, Any]] = []
    withheld: list[str] = []
    for part in attachments:
        if peer.accepts_workbooks if _is_workbook(part) else peer.accepts_files:
            sent.append(part)
        else:
            withheld.append(_attachment_name(part))
    return sent, withheld


def _with_withheld_note(answer: str, peer: str, withheld: list[str]) -> str:
    if not withheld:
        return answer
    return answer + (
        f"\n\n_Note: not sent to '{peer}': {', '.join(withheld)}. A2A hops carry text only, and "
        "Responses hops carry only what the agent card accepts (skills tagged `file`, or `excel` "
        "for workbooks). Choose a discovered agent whose card accepts them._"
    )


async def _delegate_responses(
    peer: Peer,
    question: str,
    attachments: list[dict[str, Any]],
    withheld: list[str],
    **evidence: Any,
) -> str:
    """Call a peer's agent endpoint over the Responses protocol with the attachments it accepts."""
    _, responses = await _clients()
    content = [text_content(question), *attachments]

    hop: dict[str, Any] = {
        "peer": peer.name,
        "transport": "responses",
        "url": responses.endpoint_url(peer.name),
        "card_url": peer.card_url,
        "sent_content_types": [c["type"] for c in content],
        **evidence,
    }
    if withheld:
        hop["withheld_attachments"] = withheld
    try:
        reply = await responses.send(peer.name, content)
    except ResponsesError as exc:
        hop["ok"] = False
        hop["error"] = str(exc)[:600]
        record_hop(hop)
        logger.warning("Responses delegation to %s failed: %s", peer.name, exc)
        return f"The call to '{peer.name}' failed: {exc}"

    hop["ok"] = True
    hop["status"] = reply.status
    hop["received_content_types"] = reply.content_types
    hop["reply_chars"] = len(reply.text)
    hop["received_parts"] = _received_parts(reply.text)
    if any(_is_workbook(part) for part in attachments):
        excel = _received_excel_analysis(reply.text)
        if excel is None:
            logger.warning("%s response is missing %s evidence", peer.name, EXCEL_ANALYSIS_MARKER)
        else:
            hop["excel_analysis"] = excel
    record_hop(hop)
    return _with_withheld_note(reply.text or "(the peer returned no text)", peer.name, withheld)


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


def _received_excel_analysis(text: str) -> dict[str, Any] | None:
    for block in reversed(re.findall(r"```json\s*(.*?)```", text, re.DOTALL)):
        try:
            payload = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("marker") == EXCEL_ANALYSIS_MARKER:
            return payload
    return None


async def _delegate_a2a(
    peer: str, question: str, extra_parts: list[dict[str, Any]] | None = None
) -> tuple[str, dict[str, Any]]:
    """Call a peer over A2A at the JSONRPC interface its agent card advertises."""
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
        if exc.reason == _HOSTED_REFUSAL:
            _a2a_inbound[peer] = "refused"
        return f"The A2A call to '{peer}' was rejected ({exc.reason}): {exc}", hop
    except A2AError as exc:
        hop["ok"] = False
        hop["error"] = str(exc)[:600]
        record_hop(hop)
        return f"The A2A call to '{peer}' failed: {exc}", hop

    _a2a_inbound[peer] = "accepted"
    hop["ok"] = True
    hop["received_part_kinds"] = reply.part_kinds
    hop["reply_chars"] = len(reply.text)
    if reply.task_id:
        hop["task_id"] = reply.task_id
    record_hop(hop)
    return reply.text or "(the peer returned no text)", hop


# --------------------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------------------


async def list_agents() -> str:
    """List every agent discovered from this project's A2A agent cards, refreshed now.

    Use when asked who you can reach or what they can do. Each entry shows the card's
    description, skills, which attachments the card accepts, and what its A2A endpoint did.
    """
    peers = await (await _get_directory()).peers(refresh=True)
    return json.dumps(_catalog(peers, a2a_status=True), indent=2)


async def ask_agent(
    agent: Annotated[str, Field(description="Exact name of a discovered agent from the agent-card catalog.")],
    question: Annotated[str, Field(description="The sub-task for that agent. Never paste attachment contents here.")],
    transport: Annotated[
        Literal["auto", "a2a", "responses"],
        Field(description=(
            "'auto' sends text over A2A using the agent card and falls back to Responses when the "
            "platform refuses a hosted target; attachments always use Responses. Use 'a2a' only "
            "when the caller explicitly asks for an A2A hop."
        )),
    ] = "auto",
) -> str:
    """Delegate a sub-task to an agent chosen from the discovered agent cards.

    Attachments the caller sent this turn are forwarded automatically, but only to an agent
    whose card skills advertise support for them.
    """
    directory = await _get_directory()
    peer = await directory.get(agent)
    if peer is None:
        known = ", ".join(sorted(await directory.peers())) or "none"
        return f"No discovered agent is named '{agent}'. Discovered agents: {known}."

    turn_attachments = get_responses_attachments()
    attachments, withheld = _deliverable(peer, turn_attachments)
    if transport == "responses" or (transport == "auto" and attachments):
        return await _delegate_responses(peer, question, attachments, withheld)
    if transport == "auto" and _a2a_inbound.get(peer.name) == "refused":
        return await _delegate_responses(
            peer, question, attachments, withheld,
            a2a_skipped="the platform already refused A2A for this hosted agent",
        )

    answer, hop = await _delegate_a2a(peer.name, question)
    if transport == "auto" and hop.get("error_reason") == _HOSTED_REFUSAL:
        hop["fallback"] = "responses"
        return await _delegate_responses(
            peer, question, attachments, withheld,
            fallback_from={"transport": "a2a", "error_code": hop.get("error_code"),
                           "error_reason": _HOSTED_REFUSAL},
        )
    left_behind = [_attachment_name(part) for part in turn_attachments]
    if left_behind:
        hop["withheld_attachments"] = left_behind
    return _with_withheld_note(answer, peer.name, left_behind)


async def probe_part_support(
    a2a_agent: Annotated[str, Field(description="Discovered agent to receive the payload over A2A.")],
    responses_agent: Annotated[
        str, Field(description="Discovered agent to receive the same payload over Responses.")
    ],
) -> str:
    """Compare part support across both transports by sending the same payload to each.

    Sends text + structured data + a CSV file over A2A to one discovered agent and over
    Responses to another, and reports what each accepted. Always use this when asked whether
    A2A supports files or other non-text parts: asking an agent about it is not evidence.
    Choose an agent that can receive A2A and a related agent whose card accepts files.
    """
    directory = await _get_directory()
    a2a_peer, responses_peer = await directory.get(a2a_agent), await directory.get(responses_agent)
    if a2a_peer is None or responses_peer is None:
        missing = [name for name, peer in ((a2a_agent, a2a_peer), (responses_agent, responses_peer))
                   if peer is None]
        known = ", ".join(sorted(await directory.peers())) or "none"
        return f"Not discovered: {', '.join(missing)}. Discovered agents: {known}."

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

    a2a_result, _ = await _delegate_a2a(
        a2a_peer.name,
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
        "peer": responses_peer.name,
        "transport": "responses",
        "probe": True,
        "url": responses.endpoint_url(responses_peer.name),
        "card_url": responses_peer.card_url,
        "sent_content_types": [part["type"] for part in content],
    }
    try:
        reply = await responses.send(responses_peer.name, content)
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
        f"### A2A leg (`{a2a_peer.name}`, reached through its agent card)\n{a2a_result}\n\n"
        f"### Responses leg (`{responses_peer.name}`, agent endpoint)\n{responses_result}\n\n"
        f"The `{INVENTORY_MARKER}` block in the Responses reply is generated by the "
        f"agent's middleware, not by its model."
    )


# --------------------------------------------------------------------------------------
# Agent
# --------------------------------------------------------------------------------------


class SupervisorTurnMiddleware(AgentMiddleware):
    """Capture attachments, publish the discovered card catalog, then append the hop log."""

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
        context.messages = workbook_model_messages(
            context.messages,
            "Delegate workbook questions with ask_agent to a discovered agent whose card accepts "
            "Excel workbooks. Reattach the workbook on later turns.",
        )

        try:
            peers = await (await _get_directory()).peers()
        except (A2AError, httpx.HTTPError) as exc:
            logger.warning("Agent discovery failed: %s", exc)
            set_discovery({"source": _DISCOVERY_SOURCE, "error": str(exc)[:300], "agents": []})
            catalog = f"Agent discovery failed ({str(exc)[:300]}); no peer can be called this turn."
        else:
            set_discovery({
                "source": _DISCOVERY_SOURCE,
                "agents": [
                    {"name": peer.name, "kind": peer.kind, "card_url": peer.card_url,
                     "skills": [skill.get("id") for skill in peer.skills]}
                    for _, peer in sorted(peers.items())
                ],
            })
            catalog = (
                "Agents discovered at runtime from this project's A2A agent cards. Choose by their "
                "skills and use exact names: " + json.dumps(_catalog(peers))
            )
        context.messages.append(Message(role="system", contents=[Content.from_text(catalog)]))

        summary = attachment_summary()
        if summary:
            context.messages.append(
                Message(
                    role="system",
                    contents=[
                        Content.from_text(
                            f"The caller attached {len(summary)} non-text part(s): "
                            f"{json.dumps(summary)}. ask_agent forwards them to the chosen agent "
                            "when its card accepts them. Do not inline their contents into tool "
                            "arguments."
                        )
                    ],
                )
            )

        await call_next()

        if isinstance(context.result, AgentResponse) and context.result.messages:
            context.result.messages[-1].contents.append(
                Content.from_text("\n\n" + hop_log_block())
            )


INSTRUCTIONS = """You are the Supervisor of a multi-agent system running on Microsoft Foundry.

You never answer research or analysis questions yourself. Your peers are not configured in
your code: every turn you receive a catalog of the agents discovered from this Foundry
project's A2A agent cards. Route by what those cards say:
- `ask_agent(agent, question)` -> delegate a sub-task to the discovered agent whose card skills
  (descriptions, tags and examples) best match it. Use exact names from the catalog.
- Keep `transport` at "auto": ask_agent then picks the transport, so every discovered agent is
  reachable. Only when the caller explicitly asks for an A2A hop, use "a2a" and choose an agent
  whose `kind` is not "hosted", because Foundry refuses A2A calls to hosted agents.
- `list_agents` -> when asked who you can reach or what they can do, or to refresh the catalog.
- `probe_part_support(a2a_agent, responses_agent)` -> whenever the caller asks whether A2A (or
  Responses) carries files, data or other non-text parts. Always run it: it sends real parts,
  whereas asking an agent or reading its card is not evidence. Pick an agent that can receive
  A2A (`kind` not "hosted") and a related agent whose card accepts files.

Attachments the caller sent are forwarded automatically, but only to agents whose card
accepts them (`accepts_attachments`). When several discovered agents have the skill a sub-task
needs, prefer the one whose card accepts the caller's attachments so it can ground its answer
in them; for .xlsx workbooks that means an agent that accepts Excel workbooks. Never choose an
agent that lacks the needed skill just because it accepts attachments, and never paste
attachment contents into a tool argument. For Excel, pass the user's sheet/column names and
requested calculations; that agent must compute with its tools, not guess from a filename.
Keep its warnings about saved formula results, missing values, totals rows, unsupported
operations or input limits.

When a request needs both research and analysis, ask the research-oriented agent first and
feed its findings into the analysis question. Never write a section attributed to an agent
you did not actually call - if a delegation failed, say so plainly instead of inventing its
answer.

Compose the replies into one answer and attribute each section to the agent that produced it,
for example `### From <agent-name>`. Start your reply with `[supervisor]`."""


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
        tools=[ask_agent, list_agents, probe_part_support],
        middleware=[SupervisorTurnMiddleware()],
        # History is managed by the hosting infrastructure.
        default_options={"store": False},
    )

    ResponsesHostServer(with_hop_log_evidence(agent)).run()


if __name__ == "__main__":
    main()
