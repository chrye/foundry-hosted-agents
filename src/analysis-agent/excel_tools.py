"""Turn-scoped Excel tools and application-generated execution evidence."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from agent_framework import (
    Agent,
    AgentContext,
    AgentMiddleware,
    AgentResponse,
    AgentResponseUpdate,
    Content,
    Message,
)
from excel_workbook import ExcelError, ExcelWorkbook
from pydantic import Field
from xlsx_attachments import (
    EXCEL_ANALYSIS_MARKER,
    WorkbookInputError,
    current_workbook_contents,
    read_workbook_attachment,
    workbook_model_messages,
)

logger = logging.getLogger(__name__)


@dataclass
class ExcelTurn:
    workbook: ExcelWorkbook | None = None
    workbooks: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


_excel_turn: ContextVar[ExcelTurn | None] = ContextVar("excel_turn", default=None)


def excel_evidence() -> dict[str, Any] | None:
    turn = _excel_turn.get()
    if turn is None or not (turn.workbooks or turn.errors):
        return None
    return {
        "marker": EXCEL_ANALYSIS_MARKER,
        "workbooks": turn.workbooks,
        "tool_calls": turn.tool_calls,
        "errors": turn.errors,
    }


def _evidence_block() -> str:
    return f"```json\n{json.dumps(excel_evidence(), indent=2, allow_nan=False)}\n```"


class ExcelWorkbookMiddleware(AgentMiddleware):
    async def process(
        self, context: AgentContext, call_next: Callable[[], Awaitable[None]]
    ) -> None:
        turn = ExcelTurn()
        _excel_turn.set(turn)
        contents = current_workbook_contents(context.messages)
        if contents:
            try:
                if len(contents) != 1:
                    raise WorkbookInputError("Attach exactly one .xlsx workbook per turn.")
                attachment = read_workbook_attachment(contents[0])
                turn.workbooks.append(dict(attachment.receipt()))
                turn.workbook = await asyncio.to_thread(
                    ExcelWorkbook, attachment.filename, attachment.data
                )
                turn.workbooks[0] = turn.workbook.inspect()
            except (WorkbookInputError, ExcelError) as exc:
                turn.errors.append(str(exc))
                logger.warning("Excel upload rejected: %s", exc)

        context.messages = workbook_model_messages(
            context.messages,
            "Use the Excel tools on the current workbook, not the model's file reader. "
            "Do not invent cell values. Reattach the workbook on later turns.",
        )
        if contents:
            context.messages.append(
                Message(
                    role="system",
                    contents=[
                        Content.from_text(
                            "Excel upload status (application-generated): "
                            + json.dumps(
                                {"workbooks": turn.workbooks, "errors": turn.errors},
                                allow_nan=False,
                            )
                            + ". Use inspect_excel_workbook before selecting sheets/columns. "
                            "Use aggregate_excel or compare_excel_sheets for calculations. "
                            "Report any errors and cached-formula warnings explicitly."
                        )
                    ],
                )
            )
        await call_next()
        if excel_evidence() and isinstance(context.result, AgentResponse) and context.result.messages:
            context.result.messages[-1].contents.append(
                Content.from_text("\n\n" + _evidence_block())
            )


async def _execute_excel(tool: str, arguments: dict[str, Any]) -> str:
    turn = _excel_turn.get()
    entry: dict[str, Any] = {"tool": tool, "arguments": arguments}
    try:
        if turn is None or turn.workbook is None:
            reason = "; ".join(turn.errors) if turn and turn.errors else (
                "No Excel workbook is attached to this turn. Upload one inline .xlsx file."
            )
            raise ExcelError(reason)
        methods = {
            "inspect_excel_workbook": turn.workbook.inspect,
            "read_excel_rows": turn.workbook.read_rows,
            "aggregate_excel": turn.workbook.aggregate,
            "compare_excel_sheets": turn.workbook.compare,
        }
        result = await asyncio.to_thread(methods[tool], **arguments)
        entry.update(ok=True, result=result)
    except ExcelError as exc:
        entry.update(ok=False, error=str(exc))
        logger.warning("Excel tool %s failed: %s", tool, exc)
    if turn is not None:
        turn.tool_calls.append(entry)
    return json.dumps(entry, allow_nan=False)


async def inspect_excel_workbook() -> str:
    """List every sheet, its dimensions/headers, and formula warnings in the current workbook."""
    return await _execute_excel("inspect_excel_workbook", {})


async def read_excel_rows(
    sheet: Annotated[str, Field(description="Exact worksheet name from inspect_excel_workbook.")],
    offset: Annotated[int, Field(description="Zero-based data-row offset, excluding the header.")] = 0,
    limit: Annotated[int, Field(description="Number of rows to read, from 1 to 20.")] = 10,
) -> str:
    """Read a bounded page of rows from a sheet whose first row contains unique text headers."""
    return await _execute_excel("read_excel_rows", {"sheet": sheet, "offset": offset, "limit": limit})


async def aggregate_excel(
    sheet: Annotated[str, Field(description="Exact worksheet name.")],
    value_column: Annotated[str, Field(description="Exact column header to aggregate.")],
    operation: Annotated[
        Literal["sum", "average", "min", "max", "count"],
        Field(description="Deterministic calculation. Count counts nonblank values."),
    ],
    group_by: Annotated[
        str | None, Field(description="Optional exact column header to group by.")
    ] = None,
) -> str:
    """Compute a column statistic, optionally by group, using all data rows rather than a preview."""
    return await _execute_excel(
        "aggregate_excel",
        {"sheet": sheet, "value_column": value_column, "operation": operation, "group_by": group_by},
    )


async def compare_excel_sheets(
    actual_sheet: Annotated[str, Field(description="Sheet containing actual values.")],
    target_sheet: Annotated[str, Field(description="Sheet containing target values.")],
    key_column: Annotated[str, Field(description="Exact key header present on both sheets.")],
    actual_column: Annotated[str, Field(description="Numeric actual-value header.")],
    target_column: Annotated[str, Field(description="Numeric target-value header.")],
) -> str:
    """Sum each sheet by key, match keys, and compute differences, attainment percentages and totals."""
    return await _execute_excel(
        "compare_excel_sheets",
        {
            "actual_sheet": actual_sheet,
            "target_sheet": target_sheet,
            "key_column": key_column,
            "actual_column": actual_column,
            "target_column": target_column,
        },
    )


EXCEL_TOOLS = [inspect_excel_workbook, read_excel_rows, aggregate_excel, compare_excel_sheets]


def with_excel_evidence(agent: Agent) -> Agent:
    inner_run = agent.run

    def run(messages: Any = None, *, stream: bool = False, **kwargs: Any) -> Any:
        if not stream:
            return inner_run(messages, stream=False, **kwargs)
        result = inner_run(messages, stream=True, **kwargs)

        def expand(update: AgentResponseUpdate) -> list[AgentResponseUpdate]:
            if update.finish_reason is None or excel_evidence() is None:
                return [update]
            evidence = AgentResponseUpdate(
                contents=[Content.from_text("\n\n" + _evidence_block())],
                role="assistant",
                message_id=update.message_id,
                response_id=update.response_id,
            )
            return [evidence, update]

        return result.flat_map(expand, AgentResponse.from_updates)

    agent.run = run
    return agent
