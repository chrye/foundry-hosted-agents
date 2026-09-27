"""Deterministic inventory of the content parts a hosted agent actually received.

This is the evidence layer of the POC: the A2A caller declares which part kinds it
sent, and this module reports which content the Foundry A2A -> Responses adapter
actually delivered to the agent. Comparing the two proves (or disproves) part support.

Source of truth lives in ``src/_shared/``; ``scripts/sync-shared.ps1`` copies it into
every agent folder so each container build context stays self-contained.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from contextvars import ContextVar
from typing import Any

from agent_framework import (
    Agent,
    AgentContext,
    AgentMiddleware,
    AgentResponse,
    AgentResponseUpdate,
    Content,
    Message,
)

logger = logging.getLogger(__name__)

INVENTORY_MARKER = "A2A-PART-INVENTORY"
_PREVIEW_CHARS = 200

_current_inventory: ContextVar[list[dict[str, Any]]] = ContextVar("received_parts", default=[])


def _decode_data_uri(uri: str) -> tuple[str | None, int | None, str | None]:
    """Return (media_type, byte_length, utf8_preview) for a ``data:`` URI."""
    if not uri.startswith("data:"):
        return None, None, None
    header, _, payload = uri.partition(",")
    media_type = header[len("data:") :].removesuffix(";base64") or None
    if ";base64" in header:
        try:
            raw = base64.b64decode(payload)
        except ValueError:
            return media_type, None, None
        try:
            preview = raw.decode("utf-8")[:_PREVIEW_CHARS]
        except UnicodeDecodeError:
            preview = None
        return media_type, len(raw), preview
    return media_type, len(payload.encode("utf-8")), payload[:_PREVIEW_CHARS]


def describe_content(content: Content) -> dict[str, Any] | None:
    """Map one Agent Framework content object to a compact, JSON-safe descriptor."""
    extra = dict(content.additional_properties or {})
    filename = extra.get("filename") or extra.get("name") or content.name

    if content.type == "text":
        text = content.text or ""
        return {
            "received_as": "text",
            "chars": len(text),
            "preview": text[:_PREVIEW_CHARS],
        }

    if content.type in ("data", "uri"):
        uri = str(content.uri or "")
        media_type, byte_length, preview = _decode_data_uri(uri)
        descriptor: dict[str, Any] = {
            "received_as": content.type,
            "media_type": content.media_type or media_type,
            "inline": uri.startswith("data:"),
        }
        if not descriptor["inline"]:
            descriptor["uri"] = uri[:_PREVIEW_CHARS]
        if byte_length is not None:
            descriptor["bytes"] = byte_length
        if preview:
            descriptor["preview"] = preview
        if filename:
            descriptor["filename"] = filename
        return descriptor

    if content.type == "hosted_file":
        return {
            "received_as": "hosted_file",
            "file_id": content.file_id,
            "filename": filename,
            "media_type": content.media_type,
        }

    return None


def describe_messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """Inventory every user-supplied content part across the incoming messages."""
    inventory: list[dict[str, Any]] = []
    for message in messages:
        if str(message.role) not in ("user", "Role.USER", "system", "Role.SYSTEM"):
            continue
        for content in message.contents:
            descriptor = describe_content(content)
            if descriptor is not None:
                inventory.append(descriptor)
    return inventory


def inventory_block(inventory: Sequence[dict[str, Any]]) -> str:
    """Render the inventory as a fenced block the proof harness can parse verbatim."""
    payload = json.dumps({"marker": INVENTORY_MARKER, "parts": list(inventory)}, indent=2)
    return f"```json\n{payload}\n```"


class ReceivedPartsMiddleware(AgentMiddleware):
    """Record what actually arrived on the wire and stash it for this turn.

    Pair with :func:`with_received_parts_evidence`, which appends the record to the
    response as a final streamed update. The record is produced here from the inbound
    content objects -- not by the model -- so it is trustworthy evidence of what the
    transport delivered.
    """

    async def process(
        self,
        context: AgentContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        inventory = describe_messages(context.messages)
        _current_inventory.set(inventory)
        logger.info("%s received parts: %s", INVENTORY_MARKER, json.dumps(inventory))

        context.messages.append(
            Message(
                role="system",
                contents=[
                    Content.from_text(
                        "Transport diagnostics for this turn (do not repeat verbatim; a "
                        "machine-readable copy is appended automatically): the caller's "
                        f"payload arrived as {json.dumps(inventory)}. If a part looks like "
                        "structured data or a file, use its contents in your answer and say "
                        "in one sentence which part kinds you were able to read."
                    )
                ],
            )
        )

        await call_next()

        if isinstance(context.result, AgentResponse) and context.result.messages:
            context.result.messages[-1].contents.append(
                Content.from_text("\n\n" + inventory_block(inventory))
            )


def with_received_parts_evidence(agent: Agent) -> Agent:
    """Wrap an agent so every streamed reply ends with the part-inventory block.

    The Foundry host always invokes agents with ``stream=True``, so mutating the
    aggregated ``AgentResponse`` never reaches the client. Expanding the final update
    into ``[update, evidence]`` does.
    """

    inner_run = agent.run

    def run(messages: Any = None, *, stream: bool = False, **kwargs: Any) -> Any:
        result = inner_run(messages, stream=stream, **kwargs)
        if not stream:
            return result

        def expand(update: AgentResponseUpdate) -> list[AgentResponseUpdate]:
            if update.finish_reason is None:
                return [update]
            inventory = _current_inventory.get()
            if not inventory:
                return [update]
            evidence = AgentResponseUpdate(
                contents=[Content.from_text("\n\n" + inventory_block(inventory))],
                role="assistant",
                message_id=update.message_id,
                response_id=update.response_id,
            )
            # Evidence first so the terminal update keeps carrying the finish reason.
            return [evidence, update]

        return result.flat_map(expand, AgentResponse.from_updates)

    agent.run = run  # type: ignore[method-assign]
    return agent

