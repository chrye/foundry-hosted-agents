"""Prove .xlsx byte delivery and Python-computed multi-sheet results over Responses.

Usage:
    python scripts/prove_excel.py
    python scripts/prove_excel.py --local-url http://127.0.0.1:8098 --skip-supervisor
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "supervisor-agent"))

from prove_a2a_parts import (
    HOP_LOG_MARKER,
    INVENTORY_MARKER,
    extract_marked_json,
    load_env,
    model_answer,
    verdict,
)
from responses_client import (
    FoundryResponsesClient,
    ResponsesError,
    ResponsesReply,
    file_content,
    text_content,
)
from xlsx_attachments import EXCEL_ANALYSIS_MARKER, XLSX_MEDIA_TYPE

FILENAME = "regional-sales.xlsx"
SALES = [
    ["EMEA", 700, 28000],
    ["EMEA", 500, 20000],
    ["AMER", 1000, 42500],
    ["AMER", 850, 37000],
    ["APAC", 600, 19200],
    ["APAC", 340, 12000],
    ["LATAM", 200, 10000],
    ["LATAM", 180, 7800],
]
TARGETS = [["EMEA", 50000], ["AMER", 75000], ["APAC", 35000], ["LATAM", 20000]]
EXPECTED_ACTUALS = {"EMEA": 48000, "AMER": 79500, "APAC": 31200, "LATAM": 17800}
EXPECTED_TOTALS = {"actual": 176500, "target": 180000, "difference": -3500, "attainment_pct": 98.0556}
PROMPT = (
    "Use the analysis agent's explicit Excel tools on the attached workbook. First call "
    "inspect_excel_workbook, then read_excel_rows for the first two data rows of Sales "
    "(offset 0, limit 2). Call aggregate_excel to sum revenue on Sales grouped by region. "
    "Call compare_excel_sheets with actual_sheet Sales, target_sheet Targets, key_column "
    "region, actual_column revenue and target_column target. Explain the computed regional "
    "actuals, targets, differences and attainment percentages, including overall totals and "
    "the overall difference. Do not calculate from just the preview. Do not repeat diagnostic "
    "JSON blocks; the application adds them. Do not use research, A2A or Code Interpreter."
)


def sample_workbook() -> bytes:
    workbook = Workbook()
    sales = workbook.active
    assert sales is not None
    sales.title = "Sales"
    sales.append(["region", "units", "revenue"])
    for row in SALES:
        sales.append(row)
    targets = workbook.create_sheet("Targets")
    targets.append(["region", "target"])
    for row in TARGETS:
        targets.append(row)
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def _successful_call(evidence: dict[str, Any], tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    calls = evidence.get("tool_calls")
    if not isinstance(calls, list):
        return {}
    for call in reversed(calls):
        if not isinstance(call, dict) or call.get("tool") != tool or call.get("ok") is not True:
            continue
        supplied = call.get("arguments")
        if not isinstance(supplied, dict) or any(supplied.get(key) != value for key, value in arguments.items()):
            continue
        result = call.get("result")
        if isinstance(result, dict):
            return result
    return {}


def _rows_by_key(rows: Any, expected_count: int) -> dict[str, dict[str, Any]]:
    if not isinstance(rows, list) or len(rows) != expected_count:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("key"), str) or row["key"] in result:
            return {}
        result[row["key"]] = row
    return result


def excel_checks(reply: ResponsesReply, raw: bytes, *, supervisor: bool) -> dict[str, bool]:
    checks: dict[str, bool] = dict.fromkeys((
        "completed", "workbook_bytes_and_sha256", "both_sheets_and_dimensions",
        "inspect_tool_executed", "read_rows_correct", "grouped_sums_correct",
        "cross_sheet_results_correct", "model_explains_computed_totals",
    ), False)
    checks["completed"] = reply.status == "completed"
    if supervisor:
        hop_log = extract_marked_json(reply.text, HOP_LOG_MARKER) or {}
        hops = hop_log.get("hops")
        valid_hops = [
            hop for hop in hops if isinstance(hop, dict)
            and hop.get("peer") == "analysis-agent"
            and hop.get("transport") == "responses"
            and hop.get("ok") is True and hop.get("status") == "completed"
            and "input_file" in (hop.get("sent_content_types") or [])
        ] if isinstance(hops, list) else []
        checks["supervisor_forwarded_to_analysis"] = bool(valid_hops)
        evidence = valid_hops[-1].get("excel_analysis") if valid_hops else None
        parts = valid_hops[-1].get("received_parts") if valid_hops else None
    else:
        evidence = extract_marked_json(reply.text, EXCEL_ANALYSIS_MARKER)
        parts = (extract_marked_json(reply.text, INVENTORY_MARKER) or {}).get("parts")

    if not isinstance(evidence, dict) or evidence.get("marker") != EXCEL_ANALYSIS_MARKER:
        return checks
    if evidence.get("errors") != []:
        return checks
    workbooks = evidence.get("workbooks")
    workbook = workbooks[0] if isinstance(workbooks, list) and len(workbooks) == 1 else {}
    if not isinstance(workbook, dict):
        return checks
    receipt = {
        "filename": FILENAME, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()
    }
    part_received = isinstance(parts, list) and any(
        isinstance(part, dict) and part.get("filename") == FILENAME
        and part.get("media_type") == XLSX_MEDIA_TYPE and part.get("bytes") == len(raw)
        and part.get("received_as") == "data"
        for part in parts
    )
    checks["workbook_bytes_and_sha256"] = part_received and all(
        workbook.get(key) == value for key, value in receipt.items()
    )
    sheets = workbook.get("sheets")
    dimensions = {
        sheet.get("name"): (sheet.get("rows"), sheet.get("columns"), sheet.get("headers"))
        for sheet in sheets if isinstance(sheet, dict)
    } if isinstance(sheets, list) else {}
    checks["both_sheets_and_dimensions"] = isinstance(sheets, list) and len(sheets) == 2 and dimensions == {
        "Sales": (9, 3, ["region", "units", "revenue"]),
        "Targets": (5, 2, ["region", "target"]),
    } and workbook.get("total_data_rows") == 12 and {
        sheet["name"]: sheet.get("data_rows") for sheet in sheets if isinstance(sheet, dict)
    } == {"Sales": 8, "Targets": 4}
    inspection = _successful_call(evidence, "inspect_excel_workbook", {})
    checks["inspect_tool_executed"] = all(inspection.get(key) == value for key, value in receipt.items()) and (
        inspection.get("sheets") == sheets
    )
    preview = _successful_call(evidence, "read_excel_rows", {"sheet": "Sales", "offset": 0, "limit": 2})
    checks["read_rows_correct"] = (
        preview.get("total_rows") == 8 and preview.get("returned_rows") == 2
        and preview.get("has_more") is True
        and preview.get("rows") == [dict(zip(["region", "units", "revenue"], row)) for row in SALES[:2]]
    )
    aggregate = _successful_call(evidence, "aggregate_excel", {
        "sheet": "Sales", "value_column": "revenue", "operation": "sum", "group_by": "region",
    })
    groups = _rows_by_key(aggregate.get("groups"), 4)
    checks["grouped_sums_correct"] = aggregate.get("rows") == 8 and set(groups) == set(EXPECTED_ACTUALS) and all(
        groups[key].get("value") == value and groups[key].get("count") == 2
        and groups[key].get("blank_count") == 0 for key, value in EXPECTED_ACTUALS.items()
    )
    comparison = _successful_call(evidence, "compare_excel_sheets", {
        "actual_sheet": "Sales", "target_sheet": "Targets", "key_column": "region",
        "actual_column": "revenue", "target_column": "target",
    })
    rows = _rows_by_key(comparison.get("rows"), 4)
    checks["cross_sheet_results_correct"] = set(rows) == set(EXPECTED_ACTUALS) and all(
        rows[key].get("actual") == EXPECTED_ACTUALS[key]
        and rows[key].get("target") == target
        and rows[key].get("difference") == EXPECTED_ACTUALS[key] - target
        and rows[key].get("attainment_pct") == round(EXPECTED_ACTUALS[key] / target * 100, 4)
        for key, target in TARGETS
    ) and comparison.get("totals") == EXPECTED_TOTALS
    answer = model_answer(reply.text)
    checks["model_explains_computed_totals"] = bool(
        re.search(r"\btotal", answer, re.IGNORECASE)
        and re.search(r"(?<![\d.])176[,\s]?500(?:\.0+)?(?!\d|\.\d)", answer)
        and re.search(r"(?<![\d.])180[,\s]?000(?:\.0+)?(?!\d|\.\d)", answer)
        and re.search(r"(?<![\d.])3[,\s]?500(?:\.0+)?(?!\d|\.\d)", answer)
        and re.search(r"(?:[-−]\s*(?:\$|USD\s*)?3[,\s]?500|shortfall|below|under)", answer, re.IGNORECASE)
    )
    return checks


async def run_proof(*, local_url: str | None = None, skip_supervisor: bool = False) -> bool:
    raw = sample_workbook()
    content = [text_content(PROMPT), file_content(FILENAME, XLSX_MEDIA_TYPE, raw)]
    endpoint = load_env()["FOUNDRY_PROJECT_ENDPOINT"] if not local_url else local_url
    remote = FoundryResponsesClient(endpoint) if not local_url else None
    passed = True
    try:
        for agent in ("analysis-agent",) if skip_supervisor else ("analysis-agent", "supervisor-agent"):
            try:
                if remote is not None:
                    reply = await remote.send(agent, content)
                else:
                    async with httpx.AsyncClient(timeout=300) as client:
                        response = await client.post(
                            f"{local_url}/responses",
                            json={"input": [{"role": "user", "content": content}], "stream": False},
                        )
                        response.raise_for_status()
                        reply = ResponsesReply.from_payload(response.json())
                checks = excel_checks(reply, raw, supervisor=agent == "supervisor-agent")
                print(f"\n{agent}:")
                for label, ok in checks.items():
                    print("  " + verdict(ok, label))
                print(model_answer(reply.text))
                if not all(checks.values()):
                    print(reply.text)
                passed = passed and all(checks.values())
            except (ResponsesError, httpx.HTTPError) as exc:
                print(verdict(False, f"{agent}: {exc}"))
                passed = False
    finally:
        if remote is not None:
            await remote.aclose()
    return passed


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-supervisor", action="store_true")
    parser.add_argument("--local-url", help="Analysis-only local Responses server, e.g. http://127.0.0.1:8098")
    args = parser.parse_args()
    if args.local_url:
        url = urlsplit(args.local_url)
        if url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost") or not args.skip_supervisor:
            parser.error("--local-url requires loopback HTTP and --skip-supervisor")
    return 0 if await run_proof(local_url=args.local_url, skip_supervisor=args.skip_supervisor) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
