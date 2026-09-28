import asyncio
import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
_original_path = list(sys.path)
sys.path.insert(0, str(ROOT / "scripts"))
import analyze_excel

sys.path[:] = _original_path


class ExcelUploadTests(unittest.TestCase):
    def invoke(self, raw, *, modify=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sales.xlsx"
            path.write_bytes(raw)
            evidence = {
                "workbooks": [{"filename": path.name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}],
                "errors": [],
                "tool_calls": [{"tool": "inspect_excel_workbook", "ok": True}],
            }
            if modify:
                modify(evidence)
            hop = {"peer": "analysis-agent", "transport": "responses", "ok": True,
                   "status": "completed", "excel_analysis": evidence}
            client = AsyncMock()
            client.send.return_value = Mock(text='```json\n' + json.dumps(
                {"marker": "A2A-HOP-LOG", "hops": [hop]}
            ) + '\n```')
            with patch.object(sys, "argv", ["analyze_excel.py", str(path), "--question", "Inspect my sheets"]), \
                    patch.object(analyze_excel, "load_env", return_value={"FOUNDRY_PROJECT_ENDPOINT": "https://example.test"}), \
                    patch.object(analyze_excel, "FoundryResponsesClient", return_value=client), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                return asyncio.run(analyze_excel.main()), client

    def test_upload_sends_file_and_checks_receipt(self):
        status, client = self.invoke(b"synthetic bytes")
        self.assertEqual(status, 0)
        self.assertEqual(client.send.call_args.args[0], "supervisor-agent")
        self.assertEqual(client.send.call_args.args[1][1]["type"], "input_file")
        client.aclose.assert_awaited_once()

    def test_wrong_digest_is_failure(self):
        status, _ = self.invoke(b"synthetic bytes", modify=lambda evidence: evidence["workbooks"][0].update(sha256="bad"))
        self.assertEqual(status, 1)

    def test_tool_failure_is_failure(self):
        status, _ = self.invoke(b"synthetic bytes", modify=lambda evidence: evidence["tool_calls"][0].update(ok=False))
        self.assertEqual(status, 1)

    def test_corrected_tool_attempt_is_a_warning_not_a_failure(self):
        status, _ = self.invoke(b"synthetic bytes", modify=lambda evidence: evidence["tool_calls"].insert(
            0, {"tool": "aggregate_excel", "ok": False, "error": "Unknown worksheet: 'sales'."}
        ))
        self.assertEqual(status, 0)

    def test_explicit_upload_error_is_failure(self):
        status, _ = self.invoke(b"synthetic bytes", modify=lambda evidence: evidence["errors"].append("Invalid workbook"))
        self.assertEqual(status, 1)

    def test_oversized_or_empty_upload_fails_before_cloud_call(self):
        for raw in (b"", b"x" * 6):
            with self.subTest(length=len(raw)), patch.object(
                analyze_excel, "MAX_WORKBOOK_BYTES", 5
            ), self.assertRaises(SystemExit):
                self.invoke(raw)


if __name__ == "__main__":
    unittest.main()
