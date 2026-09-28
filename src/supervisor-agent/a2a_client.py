"""Minimal A2A client for calling Foundry hosted agents.

Deliberately hand-written against the A2A JSON-RPC wire format instead of an SDK: the
point of this POC is to control the exact `parts[]` payload that goes on the wire, so
that "does Foundry carry DataPart and FilePart, not just TextPart?" is answered by
observation rather than by an abstraction.

Flow: resolve the peer's **agent card** -> read its `url` (the JSON-RPC transport) ->
POST `message/send` with explicitly constructed parts -> decode the reply's parts.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from azure.identity import DefaultAzureCredential

logger = logging.getLogger(__name__)

A2A_SCOPE = "https://ai.azure.com/.default"
AGENT_CARD_PATH = "endpoint/protocols/a2a/agentCard/v1.0"
A2A_TRANSPORT_PATH = "endpoint/protocols/a2a"

_PENDING_STATES = {"submitted", "working", "input-required", "auth-required"}


# --------------------------------------------------------------------------------------
# Part builders - the exact JSON that goes on the wire.
# --------------------------------------------------------------------------------------


def text_part(text: str) -> dict[str, Any]:
    """A2A TextPart."""
    return {"kind": "text", "text": text}


def data_part(data: dict[str, Any]) -> dict[str, Any]:
    """A2A DataPart - structured JSON, no stringification."""
    return {"kind": "data", "data": data}


def file_part_bytes(name: str, mime_type: str, payload: bytes) -> dict[str, Any]:
    """A2A FilePart carrying inline base64 bytes (FileWithBytes)."""
    return {
        "kind": "file",
        "file": {
            "name": name,
            "mimeType": mime_type,
            "bytes": base64.b64encode(payload).decode("ascii"),
        },
    }


def file_part_uri(name: str, mime_type: str, uri: str) -> dict[str, Any]:
    """A2A FilePart referencing a URI (FileWithUri) - the Phase 2 large-file shape."""
    return {"kind": "file", "file": {"name": name, "mimeType": mime_type, "uri": uri}}


def _part_kind(part: dict[str, Any]) -> str:
    # A2A renamed the discriminator from `type` to `kind`; accept either when reading.
    return str(part.get("kind") or part.get("type") or "unknown")


# --------------------------------------------------------------------------------------
# Reply
# --------------------------------------------------------------------------------------


@dataclass
class A2AReply:
    """Flattened view of an A2A `message/send` result."""

    text: str = ""
    data: list[dict[str, Any]] = field(default_factory=list)
    files: list[dict[str, Any]] = field(default_factory=list)
    part_kinds: list[str] = field(default_factory=list)
    task_id: str | None = None
    context_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_result(cls, result: dict[str, Any]) -> "A2AReply":
        reply = cls(raw=result)
        reply.task_id = result.get("id") if result.get("kind") == "task" else result.get("taskId")
        reply.context_id = result.get("contextId")

        # `message/send` may answer with a Message or a Task; a Task carries its parts
        # under status.message and/or artifacts[].
        buckets: list[list[dict[str, Any]]] = []
        if isinstance(result.get("parts"), list):
            buckets.append(result["parts"])
        status_message = (result.get("status") or {}).get("message") or {}
        if isinstance(status_message.get("parts"), list):
            buckets.append(status_message["parts"])
        for artifact in result.get("artifacts") or []:
            if isinstance(artifact.get("parts"), list):
                buckets.append(artifact["parts"])

        chunks: list[str] = []
        for parts in buckets:
            for part in parts:
                kind = _part_kind(part)
                reply.part_kinds.append(kind)
                if kind == "text" and part.get("text"):
                    chunks.append(str(part["text"]))
                elif kind == "data" and isinstance(part.get("data"), dict):
                    reply.data.append(part["data"])
                elif kind == "file" and isinstance(part.get("file"), dict):
                    reply.files.append(part["file"])

        reply.text = "\n\n".join(chunks).strip()
        return reply


class A2AError(RuntimeError):
    """An A2A call failed at the transport or JSON-RPC layer."""


class A2AProtocolError(A2AError):
    """The peer returned a JSON-RPC error. Carries the code so callers can classify it.

    Codes observed against Foundry:
      -32099 ``HostedAgentNotSupported``  - the target is a hosted agent; A2A needs a prompt agent
      -32099 ``EndpointProtocolNotEnabled`` - `agent_endpoint.protocols` is missing `a2a`
      -32005 ``Incompatible content types`` - a non-text part (`data` / `file`) was rejected
    """

    def __init__(self, message: str, *, code: int | None = None, data: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.data = data or {}

    @property
    def reason(self) -> str:
        return str(self.data.get("code") or self.data.get("contentType") or self.code or "unknown")


# --------------------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------------------


class FoundryA2AClient:
    """Calls sibling Foundry agents over A2A, discovering each peer via its agent card."""

    def __init__(
        self,
        project_endpoint: str,
        *,
        credential: DefaultAzureCredential | None = None,
        timeout: float = 180.0,
    ) -> None:
        self._project_endpoint = project_endpoint.rstrip("/")
        self._credential = credential or DefaultAzureCredential()
        self._client = httpx.AsyncClient(timeout=timeout)
        self._token: str | None = None
        self._token_expires_on: float = 0.0
        self._cards: dict[str, dict[str, Any]] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _auth_header(self) -> dict[str, str]:
        if self._token is None or time.time() > self._token_expires_on - 300:
            # The sync credential keeps the dependency set small (no aiohttp); the blocking
            # token call is pushed to a worker thread so the event loop stays free.
            token = await asyncio.to_thread(self._credential.get_token, A2A_SCOPE)
            self._token = token.token
            self._token_expires_on = float(token.expires_on)
        return {"Authorization": f"Bearer {self._token}"}

    def card_url(self, agent_name: str) -> str:
        return f"{self._project_endpoint}/agents/{agent_name}/{AGENT_CARD_PATH}"

    async def list_project_agents(self, *, page_size: int = 100) -> list[dict[str, Any]]:
        """List every agent in the Foundry project: the registry peers are discovered from."""
        agents: list[dict[str, Any]] = []
        params = {"api-version": "v1", "limit": str(page_size)}
        while True:
            response = await self._client.get(
                f"{self._project_endpoint}/agents", params=params, headers=await self._auth_header()
            )
            if response.status_code >= 400:
                raise A2AError(f"Listing project agents failed ({response.status_code}): {response.text[:500]}")
            payload = response.json()
            agents.extend(item for item in payload.get("data") or [] if isinstance(item, dict))
            last_id = payload.get("last_id")
            if not payload.get("has_more") or not last_id:
                return agents
            if params.get("after") == last_id:
                raise A2AError(f"Listing project agents did not advance past page cursor '{last_id}'.")
            params["after"] = str(last_id)

    async def fetch_agent_card(self, agent_name: str, *, refresh: bool = False) -> dict[str, Any]:
        """GET the peer's agent card. This is the discovery step of the POC."""
        if not refresh and agent_name in self._cards:
            return self._cards[agent_name]

        url = self.card_url(agent_name)
        response = await self._client.get(url, headers=await self._auth_header())
        if response.status_code == 404:
            raise A2AError(
                f"No A2A agent card for '{agent_name}' at {url}. "
                "Is A2A enabled in its agentEndpoint.protocols?"
            )
        if response.status_code >= 400:
            raise A2AError(f"Agent card fetch failed ({response.status_code}): {response.text[:500]}")

        card = response.json()
        self._cards[agent_name] = card
        logger.info(
            "Resolved agent card for %s: %s skill(s), transport=%s",
            agent_name,
            len(card.get("skills") or []),
            card.get("preferredTransport", "JSONRPC"),
        )
        return card

    async def _transport_url(self, agent_name: str) -> str:
        card = await self.fetch_agent_card(agent_name)
        # Foundry publishes transports under `supportedInterfaces`; older A2A cards use `url`.
        for interface in card.get("supportedInterfaces") or []:
            if interface.get("protocolBinding") == "JSONRPC" and interface.get("url"):
                return self._with_api_version(str(interface["url"]))
        url = card.get("url")
        if isinstance(url, str) and url.startswith("http"):
            return self._with_api_version(url)
        raise A2AError(f"Agent card for '{agent_name}' advertises no JSONRPC transport URL.")

    @staticmethod
    def _with_api_version(url: str) -> str:
        return url if "api-version=" in url else f"{url}{'&' if '?' in url else '?'}api-version=v1"

    async def _rpc(self, url: str, method: str, params: dict[str, Any], agent_name: str) -> dict[str, Any]:
        envelope = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": method,
            "params": params,
        }
        headers = {**(await self._auth_header()), "Content-Type": "application/json"}
        response = await self._client.post(url, json=envelope, headers=headers)
        if response.status_code >= 400:
            raise A2AError(
                f"A2A {method} on '{agent_name}' failed ({response.status_code}): "
                f"{response.text[:1000]}"
            )

        payload = response.json()
        if error := payload.get("error"):
            raise A2AProtocolError(
                f"A2A {method} on '{agent_name}' returned error: {error}",
                code=error.get("code"),
                data=error.get("data") or {},
            )

        result = payload.get("result")
        if not isinstance(result, dict):
            raise A2AError(f"A2A {method} on '{agent_name}' returned no result: {str(payload)[:500]}")
        return result

    async def send_message(
        self,
        agent_name: str,
        parts: list[dict[str, Any]],
        *,
        context_id: str | None = None,
        task_id: str | None = None,
        poll_timeout: float = 180.0,
        poll_interval: float = 2.0,
    ) -> A2AReply:
        """Send one A2A message built from explicit parts and return the decoded reply.

        Foundry answers `message/send` with a Task in `submitted` state, so this follows
        the A2A task lifecycle and polls `tasks/get` until it reaches a terminal state.
        """
        url = await self._transport_url(agent_name)
        message: dict[str, Any] = {
            "kind": "message",
            "role": "user",
            "messageId": uuid.uuid4().hex,
            "parts": parts,
        }
        if context_id:
            message["contextId"] = context_id
        if task_id:
            message["taskId"] = task_id

        logger.info(
            "A2A message/send -> %s parts=%s", agent_name, [_part_kind(p) for p in parts]
        )
        result = await self._rpc(
            url,
            "message/send",
            {"message": message, "configuration": {"blocking": True}},
            agent_name,
        )

        deadline = time.time() + poll_timeout
        while (
            result.get("kind") == "task"
            and (result.get("status") or {}).get("state") in _PENDING_STATES
            and result.get("id")
        ):
            if time.time() > deadline:
                raise A2AError(
                    f"A2A task {result.get('id')} on '{agent_name}' did not finish within "
                    f"{poll_timeout:.0f}s (last state "
                    f"{(result.get('status') or {}).get('state')})"
                )
            await asyncio.sleep(poll_interval)
            result = await self._rpc(url, "tasks/get", {"id": result["id"]}, agent_name)

        if result.get("kind") == "task" and (result.get("status") or {}).get("state") != "completed":
            raise A2AError(
                f"A2A task {result.get('id')} on '{agent_name}' ended without completing: "
                f"{str(result.get('status'))[:500]}"
            )
        return A2AReply.from_result(result)
