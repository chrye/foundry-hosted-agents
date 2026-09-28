"""Workbook bytes, tool execution, and evidence across the Responses delegation path."""

import asyncio
import base64
import hashlib
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
from agent_framework import (
    Agent,
    AgentContext,
    AgentResponse,
    AgentResponseUpdate,
    Content,
    Message,
    ResponseStream,
)
from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]
_original_path = list(sys.path)
sys.path[:0] = [str(ROOT / "src" / "analysis-agent"), str(ROOT / "src" / "supervisor-agent")]

import excel_tools
from a2a_parts import ReceivedPartsMiddleware, describe_messages, inventory_block
from responses_client import FoundryResponsesClient, ResponsesReply
from turn_state import (
    capture_a2a_attachments,
    capture_responses_attachments,
    hop_log_block,
)
from xlsx_attachments import (
    MAX_WORKBOOK_BYTES,
    XLSX_MEDIA_TYPE,
    WorkbookInputError,
    current_workbook_contents,
    read_workbook_attachment,
    workbook_model_messages,
)

_spec = importlib.util.spec_from_file_location(
    "excel_test_supervisor", ROOT / "src" / "supervisor-agent" / "main.py"
)
assert _spec is not None and _spec.loader is not None
supervisor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(supervisor)
sys.path[:] = _original_path


def workbook_bytes(actual=30):
    workbook = Workbook()
    sales = workbook.active
    assert sales is not None
    sales.title = "Sales"
    sales.append(["region", "revenue"])
    sales.append(["EMEA", 10])
    sales.append(["EMEA", actual - 10])
    targets = workbook.create_sheet("Targets")
    targets.append(["region", "target"])
    targets.append(["EMEA", 50])
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def workbook_message(payload, filename="sales.xlsx"):
    return Message(role="user", contents=[
        Content.from_text("Compare sales to targets using Excel tools."),
        Content.from_data(payload, XLSX_MEDIA_TYPE, additional_properties={"filename": filename}),
    ])


def context(messages, *, stream=False):
    return AgentContext(agent=Mock(spec=Agent), messages=messages, stream=stream)


async def compute():
    await excel_tools.inspect_excel_workbook()
    await excel_tools.read_excel_rows("Sales", limit=1)
    await excel_tools.aggregate_excel("Sales", "revenue", "sum", "region")
    return json.loads(await excel_tools.compare_excel_sheets(
        "Sales", "Targets", "region", "revenue", "target"
    ))


class ExcelAttachmentTests(unittest.TestCase):
    def test_binary_workbook_survives_both_projections(self):
        raw = workbook_bytes()
        original = workbook_message(raw)
        part = capture_responses_attachments([original])[0]
        self.assertEqual(part["filename"], "sales.xlsx")
        self.assertTrue(part["file_data"].startswith(f"data:{XLSX_MEDIA_TYPE};base64,"))
        self.assertEqual(base64.b64decode(part["file_data"].partition(",")[2]), raw)
        a2a = capture_a2a_attachments([original])[0]
        self.assertEqual(base64.b64decode(a2a["file"]["bytes"]), raw)

    def test_model_sees_metadata_not_binary_and_original_is_not_mutated(self):
        original = workbook_message(workbook_bytes())
        replaced = workbook_model_messages([original], "Use tools.")
        self.assertTrue(all(content.type == "text" for content in replaced[0].contents))
        self.assertIn("sales.xlsx", replaced[0].text)
        self.assertEqual(original.contents[1].type, "data")
        self.assertNotIn("base64", replaced[0].text)

    def test_old_turn_workbooks_are_not_silently_reused(self):
        messages = [workbook_message(workbook_bytes()), Message(role="user", contents=["Next task"])]
        self.assertEqual(current_workbook_contents(messages), [])
        self.assertEqual(capture_responses_attachments(messages), [])

    def test_read_attachment_records_exact_bytes_and_digest(self):
        raw = workbook_bytes()
        attachment = read_workbook_attachment(workbook_message(raw).contents[1])
        self.assertEqual(attachment.data, raw)
        self.assertEqual(attachment.receipt(), {
            "filename": "sales.xlsx", "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()
        })

    def test_invalid_base64_empty_and_oversized_uploads_fail(self):
        for encoded, message in [
            ("%%%", "valid base64"),
            ("", "empty"),
            ("A" * (4 * ((MAX_WORKBOOK_BYTES + 2) // 3) + 4), "5 MiB"),
        ]:
            with self.subTest(error=message):
                content = Content(
                    type="data", uri=f"data:{XLSX_MEDIA_TYPE};base64,{encoded}",
                    media_type=XLSX_MEDIA_TYPE, additional_properties={"filename": "sales.xlsx"},
                )
                with self.assertRaisesRegex(WorkbookInputError, message):
                    read_workbook_attachment(content)

    def test_file_reference_cannot_trigger_network_or_filesystem_reads(self):
        for uri in ("https://example.test/sales.xlsx", "file:///C:/private/sales.xlsx"):
            content = Content(
                type="uri", uri=uri, media_type=XLSX_MEDIA_TYPE,
                additional_properties={"filename": "sales.xlsx"},
            )
            with self.assertRaisesRegex(WorkbookInputError, "inline input_file"):
                read_workbook_attachment(content)


class ExcelToolIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_multisheet_fixture_satisfies_independent_proof(self):
        old_path = list(sys.path)
        try:
            sys.path.insert(0, str(ROOT / "scripts"))
            import prove_excel
            from responses_client import ResponsesReply
        finally:
            sys.path[:] = old_path
        raw = prove_excel.sample_workbook()
        request = context([workbook_message(raw, prove_excel.FILENAME)])

        async def next_call():
            await excel_tools.inspect_excel_workbook()
            await excel_tools.read_excel_rows("Sales", offset=0, limit=2)
            await excel_tools.aggregate_excel("Sales", "revenue", "sum", "region")
            result = json.loads(await excel_tools.compare_excel_sheets(
                "Sales", "Targets", "region", "revenue", "target"
            ))
            self.assertEqual(result["result"]["totals"], prove_excel.EXPECTED_TOTALS)
            request.result = AgentResponse(messages=[Message(role="assistant", contents=[
                "Total actual 176,500 versus target 180,000; shortfall 3,500."
            ])])

        async def excel_call():
            await excel_tools.ExcelWorkbookMiddleware().process(request, next_call)

        await ReceivedPartsMiddleware().process(request, excel_call)
        assert isinstance(request.result, AgentResponse)
        checks = prove_excel.excel_checks(
            ResponsesReply(text=request.result.text, status="completed"), raw, supervisor=False
        )
        self.assertTrue(all(checks.values()), checks)

    async def test_tools_compute_and_emit_evidence_from_received_bytes(self):
        raw = workbook_bytes()
        request = context([workbook_message(raw)])

        async def next_call():
            self.assertTrue(all(part.type == "text" for part in request.messages[0].contents))
            answer = await compute()
            self.assertTrue(answer["ok"])
            self.assertEqual(answer["result"]["totals"]["actual"], 30)
            self.assertEqual(answer["result"]["totals"]["target"], 50)
            self.assertEqual(answer["result"]["totals"]["difference"], -20)
            request.result = AgentResponse(messages=[Message(role="assistant", contents=["30 versus 50."])])

        async def excel_call():
            await excel_tools.ExcelWorkbookMiddleware().process(request, next_call)

        await ReceivedPartsMiddleware().process(request, excel_call)
        evidence = excel_tools.excel_evidence()
        self.assertEqual(evidence["workbooks"][0]["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual({sheet["name"] for sheet in evidence["workbooks"][0]["sheets"]}, {"Sales", "Targets"})
        self.assertEqual(len(evidence["tool_calls"]), 4)
        self.assertTrue(all(call["ok"] for call in evidence["tool_calls"]))
        assert isinstance(request.result, AgentResponse)
        self.assertIn('"marker": "EXCEL-ANALYSIS"', request.result.text)
        self.assertIn('"marker": "A2A-PART-INVENTORY"', request.result.text)

    async def test_corrupt_workbook_and_duplicate_uploads_are_explicit_errors(self):
        for messages in [
            [workbook_message(b"not an xlsx")],
            [Message(role="user", contents=[
                workbook_message(workbook_bytes()).contents[1],
                workbook_message(workbook_bytes()).contents[1],
            ])],
        ]:
            with self.subTest(messages=len(messages[0].contents)):
                request = context(messages)

                async def next_call():
                    result = json.loads(await excel_tools.inspect_excel_workbook())
                    self.assertFalse(result["ok"])
                    self.assertTrue(result["error"])

                with self.assertLogs("excel_tools", level="WARNING"):
                    await excel_tools.ExcelWorkbookMiddleware().process(request, next_call)
                self.assertTrue(excel_tools.excel_evidence()["errors"])

    async def test_tool_error_is_logged_and_recorded_not_replaced_by_zero(self):
        request = context([workbook_message(workbook_bytes())])

        async def next_call():
            with self.assertLogs("excel_tools", level="WARNING"):
                result = json.loads(await excel_tools.aggregate_excel("Missing", "revenue", "sum"))
            self.assertFalse(result["ok"])
            self.assertNotIn("result", result)
            self.assertTrue(excel_tools.excel_evidence()["tool_calls"][-1]["error"])

        await excel_tools.ExcelWorkbookMiddleware().process(request, next_call)

    async def test_concurrent_requests_have_independent_workbooks_and_evidence(self):
        async def run(actual):
            request = context([workbook_message(workbook_bytes(actual))])
            result = {}

            async def next_call():
                await asyncio.sleep(0)
                result.update(await compute())

            await excel_tools.ExcelWorkbookMiddleware().process(request, next_call)
            return result["result"]["totals"]["actual"], excel_tools.excel_evidence()["workbooks"][0]["sha256"]

        first, second = await asyncio.gather(run(30), run(70))
        self.assertEqual((first[0], second[0]), (30, 70))
        self.assertNotEqual(first[1], second[1])

    async def test_next_turn_without_upload_cannot_reuse_last_workbook(self):
        await excel_tools.ExcelWorkbookMiddleware().process(
            context([workbook_message(workbook_bytes())]), AsyncMock()
        )
        messages = [workbook_message(workbook_bytes()), Message(role="user", contents=["Next task"])]
        await excel_tools.ExcelWorkbookMiddleware().process(context(messages), AsyncMock())
        with self.assertLogs("excel_tools", level="WARNING"):
            result = json.loads(await excel_tools.inspect_excel_workbook())
        self.assertFalse(result["ok"])
        self.assertIn("this turn", result["error"])

    async def test_stream_injects_tool_evidence_before_terminal_update(self):
        request = context([workbook_message(workbook_bytes())], stream=True)

        async def updates():
            async def next_call():
                await compute()

            await excel_tools.ExcelWorkbookMiddleware().process(request, next_call)
            yield AgentResponseUpdate(contents=[Content.from_text("Computed.")], role="assistant")
            yield AgentResponseUpdate(finish_reason="stop", role="assistant")

        agent = Mock(spec=Agent)
        agent.run = lambda *args, **kwargs: ResponseStream(updates(), finalizer=AgentResponse.from_updates)
        wrapped = excel_tools.with_excel_evidence(agent)
        emitted = [update async for update in wrapped.run(stream=True)]
        self.assertIsNotNone(emitted[-1].finish_reason)
        self.assertIn('"marker": "EXCEL-ANALYSIS"', emitted[-2].text)
        self.assertIn('"actual": 30', emitted[-2].text)

    async def test_supervisor_responses_hop_delivers_bytes_then_records_real_tool_results(self):
        raw = workbook_bytes()
        supervisor_request = context([workbook_message(raw)])
        received = []

        async def handler(request):
            body = json.loads(request.content)
            part = body["input"][0]["content"][1]
            actual_bytes = base64.b64decode(part["file_data"].partition(",")[2])
            self.assertEqual(actual_bytes, raw)
            self.assertEqual(part["filename"], "sales.xlsx")
            analysis_request = context([workbook_message(actual_bytes, part["filename"])])
            received.extend(describe_messages(analysis_request.messages))

            async def model_call():
                await compute()
                analysis_request.result = AgentResponse(
                    messages=[Message(role="assistant", contents=["30 versus 50."])]
                )

            async def excel_call():
                await excel_tools.ExcelWorkbookMiddleware().process(analysis_request, model_call)

            await ReceivedPartsMiddleware().process(analysis_request, excel_call)
            assert isinstance(analysis_request.result, AgentResponse)
            return httpx.Response(200, json={
                "status": "completed",
                "output": [{"type": "message", "content": [
                    {"type": "output_text", "text": analysis_request.result.text}
                ]}],
            })

        client = FoundryResponsesClient("https://example.test/project")
        await client._client.aclose()
        client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client._headers = AsyncMock(return_value={})
        try:
            async def supervisor_call():
                self.assertTrue(all(part.type == "text" for part in supervisor_request.messages[0].contents))
                await supervisor.ask_analysis("Compare the workbook sheets.")

            with patch.object(supervisor, "_clients", AsyncMock(return_value=(AsyncMock(), client))):
                await supervisor.SupervisorTurnMiddleware().process(supervisor_request, supervisor_call)
            hop = json.loads(hop_log_block().split("```json\n")[1].split("```")[0])["hops"][0]
            self.assertEqual(hop["peer"], "analysis-agent")
            self.assertTrue(hop["ok"])
            self.assertEqual(hop["status"], "completed")
            self.assertEqual(hop["excel_analysis"]["workbooks"][0]["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(hop["excel_analysis"]["tool_calls"][-1]["result"]["totals"]["actual"], 30)
            self.assertEqual(hop["received_parts"], received)
        finally:
            await client.aclose()

    async def test_supervisor_withholds_excel_but_still_asks_research(self):
        message = workbook_message(workbook_bytes())
        message.contents.append(
            Content.from_data(b"a,b\n1,2", "text/csv", additional_properties={"filename": "notes.csv"})
        )
        request = context([message])
        responses = AsyncMock()
        responses.endpoint_url = lambda peer: f"https://example.test/{peer}"
        responses.send.return_value = ResponsesReply(
            text="brief\n" + inventory_block([{"received_as": "text", "chars": 5, "preview": "brief"}]),
            status="completed",
        )

        async def next_call():
            answer = await supervisor.ask_research("What drives revenue seasonality?")
            self.assertIn("not sent to 'research-agent': sales.xlsx", answer)

        with patch.object(supervisor, "_clients", AsyncMock(return_value=(AsyncMock(), responses))):
            await supervisor.SupervisorTurnMiddleware().process(request, next_call)
        sent = responses.send.call_args.args[1]
        self.assertEqual([part["type"] for part in sent], ["input_text", "input_file"])
        self.assertEqual(sent[1]["filename"], "notes.csv")
        hop = json.loads(hop_log_block().split("```json\n")[1].split("```")[0])["hops"][0]
        self.assertEqual((hop["ok"], hop["withheld_attachments"]), (True, ["sales.xlsx"]))
        self.assertNotIn("excel_analysis", hop)


if __name__ == "__main__":
    unittest.main()
