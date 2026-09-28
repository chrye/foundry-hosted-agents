"""Workbook attachment handling shared by the router and analysis agent."""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

from agent_framework import Content, Message

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_WORKBOOK_BYTES = 5 * 1024 * 1024
EXCEL_ANALYSIS_MARKER = "EXCEL-ANALYSIS"


class WorkbookInputError(ValueError):
    """A workbook cannot be processed within the supported upload contract."""


def workbook_name(content: Content) -> str:
    extra = content.additional_properties or {}
    name = extra.get("filename") or extra.get("name") or content.name
    return str(name) if name else "workbook.xlsx"


def is_xlsx(content: Content) -> bool:
    if content.type not in ("data", "uri", "hosted_file"):
        return False
    extra = content.additional_properties or {}
    name = extra.get("filename") or extra.get("name") or content.name or ""
    media_type = content.media_type or str(content.uri or "").partition(",")[0][5:].partition(";")[0]
    return str(name).lower().endswith(".xlsx") or media_type.lower() == XLSX_MEDIA_TYPE


@dataclass(frozen=True)
class WorkbookAttachment:
    filename: str
    data: bytes

    def receipt(self) -> dict[str, str | int]:
        return {
            "filename": self.filename,
            "bytes": len(self.data),
            "sha256": hashlib.sha256(self.data).hexdigest(),
        }


def read_workbook_attachment(content: Content) -> WorkbookAttachment:
    name = workbook_name(content)
    if not name.lower().endswith(".xlsx"):
        raise WorkbookInputError("Excel uploads must have an .xlsx filename.")
    uri = str(content.uri or "")
    header, separator, encoded = uri.partition(",")
    if content.type not in ("data", "uri") or not separator or not header.startswith("data:"):
        raise WorkbookInputError(
            "Excel requires an inline input_file with base64 file_data; file_id and file_url "
            "references are not supported in Sprint 1."
        )
    if not header.endswith(";base64"):
        raise WorkbookInputError("Excel file_data must be a base64 data URI.")
    if len(encoded) > 4 * ((MAX_WORKBOOK_BYTES + 2) // 3):
        raise WorkbookInputError("Excel workbook exceeds the 5 MiB upload limit.")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise WorkbookInputError("Excel file_data is not valid base64.") from exc
    if not data:
        raise WorkbookInputError("Excel workbook is empty.")
    if len(data) > MAX_WORKBOOK_BYTES:
        raise WorkbookInputError("Excel workbook exceeds the 5 MiB upload limit.")
    return WorkbookAttachment(name, data)


def current_workbook_contents(messages: Sequence[Message]) -> list[Content]:
    for message in reversed(messages):
        if str(message.role) in ("user", "Role.USER"):
            return [content for content in message.contents if is_xlsx(content)]
    return []


def workbook_model_messages(messages: Sequence[Message], instruction: str) -> list[Message]:
    """Keep uploaded workbook bytes in application state, never in model requests."""
    result: list[Message] = []
    for message in messages:
        if not any(is_xlsx(content) for content in message.contents):
            result.append(message)
            continue
        replacement = copy.copy(message)
        replacement.contents = [
            Content.from_text(
                f"Excel attachment: {workbook_name(content)}. Binary workbook content is "
                f"available only to application tools. {instruction}"
            )
            if is_xlsx(content)
            else content
            for content in message.contents
        ]
        result.append(replacement)
    return result
