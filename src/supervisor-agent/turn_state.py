"""Turn-scoped state shared between the supervisor's middleware and its tools.

The model picks *who* to ask and *what* to ask (a text part). The attachments the caller
sent on this turn are forwarded by the tools, without being copied into model-generated
tool arguments. Each inbound content object is projected twice, because the two
transports speak different dialects:

  * A2A       -> DataPart / FilePart  (built, but Foundry rejects non-text parts today)
  * Responses -> input_file / input_image  (what actually carries payloads right now)
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

from agent_framework import Content, Message

from a2a_client import data_part, file_part_bytes, file_part_uri
from xlsx_attachments import is_xlsx, workbook_name

_a2a_attachments: ContextVar[list[dict[str, Any]]] = ContextVar("a2a_attachments", default=[])
_responses_attachments: ContextVar[list[dict[str, Any]]] = ContextVar("responses_attachments", default=[])
_hop_log: ContextVar[list[dict[str, Any]]] = ContextVar("a2a_hop_log", default=[])

HOP_LOG_MARKER = "A2A-HOP-LOG"


def _looks_like_json(media_type: str | None) -> bool:
    return bool(media_type) and ("json" in media_type.lower())


def _decode_data_uri(uri: str) -> tuple[str | None, bytes | None]:
    if not uri.startswith("data:"):
        return None, None
    header, _, payload = uri.partition(",")
    media_type = header[len("data:") :].removesuffix(";base64") or None
    if ";base64" in header:
        try:
            return media_type, base64.b64decode(payload)
        except ValueError:
            return media_type, None
    return media_type, payload.encode("utf-8")


def _name_of(content: Content) -> str | None:
    extra = dict(content.additional_properties or {})
    return extra.get("filename") or extra.get("name") or content.name


_FLATTENED_FILE = re.compile(r"^\[File: (?P<name>[^\]]+)\]\n(?P<body>.*)$", re.DOTALL)


def _unflatten_file(content: Content) -> tuple[str, str] | None:
    """Recover a filename and body from a file the host flattened into text.

    Foundry's Responses adapter decodes `input_file` payloads whose media type is `text/*`
    and hands them to the agent as a text content prefixed with `[File: <name>]`. Without
    this, a supervisor forwarding "attachments" would find none and silently drop the file.
    """
    if content.type != "text" or not content.text:
        return None
    match = _FLATTENED_FILE.match(content.text)
    if not match:
        return None
    return match.group("name"), match.group("body")


def content_to_a2a_part(content: Content) -> dict[str, Any] | None:
    """Map one inbound content object to the A2A part that best preserves it."""
    name = _name_of(content)

    if flattened := _unflatten_file(content):
        filename, body = flattened
        return file_part_bytes(filename, "text/plain", body.encode("utf-8"))

    if content.type in ("data", "uri"):
        uri = str(content.uri or "")
        if uri.startswith("data:"):
            media_type, raw = _decode_data_uri(uri)
            media_type = content.media_type or media_type or "application/octet-stream"
            if raw is None:
                return None
            if _looks_like_json(media_type):
                try:
                    return data_part(json.loads(raw.decode("utf-8")))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
            return file_part_bytes(name or "attachment", media_type, raw)
        if uri.startswith("http"):
            return file_part_uri(
                name or uri.rsplit("/", 1)[-1] or "attachment",
                content.media_type or "application/octet-stream",
                uri,
            )
        return None

    if content.type == "hosted_file":
        return data_part(
            {
                "foundry_hosted_file": {
                    "file_id": content.file_id,
                    "filename": name,
                    "media_type": content.media_type,
                }
            }
        )

    return None


def content_to_responses_part(content: Content) -> dict[str, Any] | None:
    """Map one inbound content object to a Responses input content part."""
    name = workbook_name(content) if is_xlsx(content) else _name_of(content)

    if flattened := _unflatten_file(content):
        filename, body = flattened
        encoded = base64.b64encode(body.encode("utf-8")).decode("ascii")
        return {
            "type": "input_file",
            "filename": filename,
            "file_data": f"data:text/plain;base64,{encoded}",
        }

    if content.type in ("data", "uri"):
        uri = str(content.uri or "")
        media_type = content.media_type or ""
        if not uri:
            return None
        if media_type.startswith("image/"):
            return {"type": "input_image", "image_url": uri}
        if uri.startswith("data:"):
            return {"type": "input_file", "filename": name or "attachment", "file_data": uri}
        return {"type": "input_file", "file_url": uri}

    if content.type == "hosted_file" and content.file_id:
        return {"type": "input_file", "file_id": content.file_id, "filename": name}

    return None


def capture_a2a_attachments(messages: list[Message]) -> list[dict[str, Any]]:
    return _capture(messages, content_to_a2a_part)


def capture_responses_attachments(messages: list[Message]) -> list[dict[str, Any]]:
    return _capture(messages, content_to_responses_part)


def _capture(
    messages: list[Message], project: Callable[[Content], dict[str, Any] | None]
) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    latest_user = next(
        (message for message in reversed(messages) if str(message.role) in ("user", "Role.USER")),
        None,
    )
    for message in messages:
        if str(message.role) not in ("user", "Role.USER"):
            continue
        for content in message.contents:
            if is_xlsx(content) and message is not latest_user:
                continue
            part = project(content)
            if part is not None:
                parts.append(part)
    return parts


def set_attachments(a2a: list[dict[str, Any]], responses: list[dict[str, Any]]) -> None:
    _a2a_attachments.set(a2a)
    _responses_attachments.set(responses)


def get_a2a_attachments() -> list[dict[str, Any]]:
    return list(_a2a_attachments.get())


def get_responses_attachments() -> list[dict[str, Any]]:
    return list(_responses_attachments.get())


def attachment_summary() -> list[dict[str, Any]]:
    """A short, model-safe description of what is queued for forwarding."""
    summary: list[dict[str, Any]] = []
    for part in _responses_attachments.get():
        summary.append(
            {
                "type": part.get("type"),
                "filename": part.get("filename"),
                "inline": bool(part.get("file_data")),
            }
        )
    return summary


def reset_hop_log() -> None:
    _hop_log.set([])


def record_hop(hop: dict[str, Any]) -> None:
    _hop_log.get().append(hop)


def hop_log_block() -> str:
    """Render this turn's delegations as a fenced block the proof harness parses."""
    payload = json.dumps({"marker": HOP_LOG_MARKER, "hops": _hop_log.get()}, indent=2)
    return f"```json\n{payload}\n```"


def with_hop_log_evidence(agent):
    """Wrap an agent so every streamed reply ends with this turn's A2A/Responses hop log.

    The Foundry host always invokes agents with ``stream=True``, so mutating the aggregated
    response never reaches the client; expanding the terminal update does.
    """
    from agent_framework import AgentResponse, AgentResponseUpdate, Content

    inner_run = agent.run

    def run(messages: Any = None, *, stream: bool = False, **kwargs: Any) -> Any:
        result = inner_run(messages, stream=stream, **kwargs)
        if not stream:
            return result

        def expand(update: AgentResponseUpdate) -> list[AgentResponseUpdate]:
            if update.finish_reason is None:
                return [update]
            if not _hop_log.get():
                return [update]
            evidence = AgentResponseUpdate(
                contents=[Content.from_text("\n\n" + hop_log_block())],
                role="assistant",
                message_id=update.message_id,
                response_id=update.response_id,
            )
            return [evidence, update]

        return result.flat_map(expand, AgentResponse.from_updates)

    agent.run = run
    return agent
