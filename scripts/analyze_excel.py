"""Upload a local .xlsx workbook to the supervisor over Responses."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "supervisor-agent"))

from prove_a2a_parts import extract_marked_json, load_env
from responses_client import (
    FoundryResponsesClient,
    ResponsesError,
    file_content,
    text_content,
)
from xlsx_attachments import MAX_WORKBOOK_BYTES, XLSX_MEDIA_TYPE


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook", type=Path)
    parser.add_argument("--question", required=True, help="Analysis request, with sheet and column names if known.")
    args = parser.parse_args()
    if args.workbook.suffix.lower() != ".xlsx":
        parser.error("Only .xlsx files are supported.")
    try:
        with args.workbook.open("rb") as handle:
            raw = handle.read(MAX_WORKBOOK_BYTES + 1)
    except OSError as exc:
        parser.error(f"Cannot read workbook: {exc}")
    if not raw or len(raw) > MAX_WORKBOOK_BYTES:
        parser.error("Workbook must be nonempty and no larger than 5 MiB.")
    if not args.question.strip():
        parser.error("--question must not be empty.")

    client = FoundryResponsesClient(load_env()["FOUNDRY_PROJECT_ENDPOINT"])
    try:
        reply = await client.send("supervisor-agent", [
            text_content(args.question),
            file_content(args.workbook.name, XLSX_MEDIA_TYPE, raw),
        ])
        print(reply.text)
        hops = (extract_marked_json(reply.text, "A2A-HOP-LOG") or {}).get("hops") or []
        # The supervisor chooses the Excel-capable agent from agent cards, so find it by evidence.
        analysis_hops = [
            hop for hop in hops
            if isinstance(hop, dict) and hop.get("transport") == "responses" and "excel_analysis" in hop
        ]
        if not analysis_hops:
            print("ERROR: No Responses delegation returned Excel analysis evidence.", file=sys.stderr)
            return 1
        for hop in analysis_hops:
            evidence = hop.get("excel_analysis") or {}
            calls = evidence.get("tool_calls") or []
            workbooks = evidence.get("workbooks") or []
            intact = len(workbooks) == 1 and all(
                workbooks[0].get(key) == value for key, value in {
                    "filename": args.workbook.name,
                    "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                }.items()
            )
            if (hop.get("ok") is not True or hop.get("status") != "completed"
                    or not intact or evidence.get("errors")
                    or not any(call.get("ok") is True for call in calls)):
                print("ERROR: Excel delivery or tool execution failed; inspect the response evidence.", file=sys.stderr)
                return 1
            # A failed attempt the model later corrected is not a delivery failure.
            for call in calls:
                if call.get("ok") is not True:
                    print(f"WARNING: {call.get('tool')} attempt failed: {call.get('error')}", file=sys.stderr)
        return 0
    except (ResponsesError, httpx.HTTPError) as exc:
        print(f"ERROR: Workbook request failed: {exc}", file=sys.stderr)
        return 1
    finally:
        await client.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
