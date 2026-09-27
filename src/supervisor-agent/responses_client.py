"""Client for calling a sibling Foundry **hosted** agent over the Responses protocol.

Why this exists alongside `a2a_client`: Foundry's A2A gate is text-only and refuses
hosted-agent targets (`-32099 HostedAgentNotSupported`). The Responses protocol is the
transport that actually carries `input_text` + `input_file` + `input_image` to a hosted
agent today, so the supervisor uses it for any delegation that involves attachments.

Endpoint: ``{project_endpoint}/agents/{name}/endpoint/protocols/openai/responses?api-version=v1``
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from azure.identity import DefaultAzureCredential

logger = logging.getLogger(__name__)

SCOPE = "https://ai.azure.com/.default"
RESPONSES_PATH = "endpoint/protocols/openai/responses"

# Headers the platform injects per request; the container must forward them on outbound
# calls to Foundry services so caller context resolves server-side.
FORWARD_HEADERS = ("x-agent-foundry-call-id", "x-agent-user-id", "x-ms-user-identity")


@dataclass
class ResponsesReply:
    """Flattened view of a Responses call to a peer agent."""

    text: str = ""
    content_types: list[str] = field(default_factory=list)
    status: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ResponsesReply":
        reply = cls(raw=payload, status=payload.get("status"))
        chunks: list[str] = []
        for item in payload.get("output") or []:
            for content in item.get("content") or []:
                kind = str(content.get("type"))
                reply.content_types.append(kind)
                if kind in ("output_text", "text") and content.get("text"):
                    chunks.append(str(content["text"]))
        reply.text = "\n\n".join(chunks).strip()
        return reply


class ResponsesError(RuntimeError):
    """A Responses-protocol call to a peer agent failed."""


def text_content(text: str) -> dict[str, Any]:
    return {"type": "input_text", "text": text}


def file_content(filename: str, media_type: str, payload: bytes) -> dict[str, Any]:
    import base64

    return {
        "type": "input_file",
        "filename": filename,
        "file_data": f"data:{media_type};base64,{base64.b64encode(payload).decode('ascii')}",
    }


def file_url_content(file_url: str) -> dict[str, Any]:
    return {"type": "input_file", "file_url": file_url}


class FoundryResponsesClient:
    """Calls sibling hosted agents over the Responses protocol."""

    def __init__(
        self,
        project_endpoint: str,
        *,
        credential: DefaultAzureCredential | None = None,
        timeout: float = 300.0,
    ) -> None:
        self._project_endpoint = project_endpoint.rstrip("/")
        self._credential = credential or DefaultAzureCredential()
        self._client = httpx.AsyncClient(timeout=timeout)
        self._token: str | None = None
        self._expires_on: float = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _headers(self, forward: dict[str, str] | None = None) -> dict[str, str]:
        if self._token is None or time.time() > self._expires_on - 300:
            # Sync credential + worker thread: keeps aiohttp out of the dependency set.
            token = await asyncio.to_thread(self._credential.get_token, SCOPE)
            self._token = token.token
            self._expires_on = float(token.expires_on)
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        for key, value in (forward or {}).items():
            if key.lower() in FORWARD_HEADERS and value:
                headers[key] = value
        return headers

    def endpoint_url(self, agent_name: str) -> str:
        return f"{self._project_endpoint}/agents/{agent_name}/{RESPONSES_PATH}?api-version=v1"

    async def send(
        self,
        agent_name: str,
        content: list[dict[str, Any]],
        *,
        forward_headers: dict[str, str] | None = None,
    ) -> ResponsesReply:
        """Send one user message, built from explicit Responses content parts."""
        url = self.endpoint_url(agent_name)
        body = {"input": [{"role": "user", "content": content}], "stream": False}

        logger.info(
            "Responses -> %s content=%s", agent_name, [c.get("type") for c in content]
        )
        response = await self._client.post(url, json=body, headers=await self._headers(forward_headers))
        if response.status_code >= 400:
            raise ResponsesError(
                f"Responses call to '{agent_name}' failed ({response.status_code}): "
                f"{response.text[:1000]}"
            )
        payload = response.json()
        if payload.get("status") != "completed":
            raise ResponsesError(
                f"Responses call to '{agent_name}' did not complete "
                f"(status={payload.get('status')}): {str(payload.get('error'))[:500]}"
            )
        return ResponsesReply.from_payload(payload)
