"""Regression tests for the Excel proof: receipts and actual computed results, not prose."""

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_original_path = list(sys.path)
sys.path.insert(0, str(ROOT / "scripts"))
import prove_excel as proof
from responses_client import ResponsesReply

sys.path[:] = _original_path


def marked(marker, **payload):
    return "```json\n" + json.dumps({"marker": marker, **payload}) + "\n```"


def good_evidence(raw):
    receipt = {"filename": proof.FILENAME, "bytes": len(raw), "sha256": proof.hashlib.sha256(raw).hexdigest()}
    workbook = {
        **receipt,
        "sheets": [
            {"name": "Sales", "rows": 9, "data_rows": 8, "columns": 3, "headers": ["region", "units", "revenue"]},
            {"name": "Targets", "rows": 5, "data_rows": 4, "columns": 2, "headers": ["region", "target"]},
        ],
        "total_data_rows": 12,
        "warnings": [],
    }
    calls = [
        {"tool": "inspect_excel_workbook", "arguments": {}, "ok": True, "result": copy.deepcopy(workbook)},
        {"tool": "read_excel_rows", "arguments": {"sheet": "Sales", "offset": 0, "limit": 2}, "ok": True,
         "result": {"total_rows": 8, "returned_rows": 2, "has_more": True,
                    "rows": [dict(zip(["region", "units", "revenue"], row)) for row in proof.SALES[:2]]}},
        {"tool": "aggregate_excel",
         "arguments": {"sheet": "Sales", "value_column": "revenue", "operation": "sum", "group_by": "region"},
         "ok": True, "result": {"rows": 8, "groups": [
             {"key": key, "value": value, "count": 2, "blank_count": 0}
             for key, value in proof.EXPECTED_ACTUALS.items()
         ]}},
        {"tool": "compare_excel_sheets",
         "arguments": {"actual_sheet": "Sales", "target_sheet": "Targets", "key_column": "region",
                       "actual_column": "revenue", "target_column": "target"},
         "ok": True, "result": {"rows": [
             {"key": key, "actual": proof.EXPECTED_ACTUALS[key], "target": target,
              "difference": proof.EXPECTED_ACTUALS[key] - target,
              "attainment_pct": round(proof.EXPECTED_ACTUALS[key] / target * 100, 4)}
             for key, target in proof.TARGETS
         ], "totals": dict(proof.EXPECTED_TOTALS)}},
    ]
    return {"marker": proof.EXCEL_ANALYSIS_MARKER, "workbooks": [workbook], "tool_calls": calls, "errors": []}


def reply(raw, evidence, *, supervisor=False, prose="Total actual $176,500, target $180,000: a $3,500 shortfall.",
          status="completed", hop_override=None):
    parts = [{
        "filename": proof.FILENAME, "bytes": len(raw), "media_type": proof.XLSX_MEDIA_TYPE, "received_as": "data",
    }]
    if supervisor:
        hop = {"peer": "analysis-agent", "transport": "responses", "ok": True, "status": "completed",
               "sent_content_types": ["input_text", "input_file"], "received_parts": parts,
               "excel_analysis": evidence}
        hop.update(hop_override or {})
        text = prose + "\n" + marked(proof.HOP_LOG_MARKER, hops=[hop])
    else:
        text = prose + "\n" + marked(proof.INVENTORY_MARKER, parts=parts)
        text += "\n```json\n" + json.dumps(evidence) + "\n```"
    return ResponsesReply(text=text, status=status)


class ExcelProofTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = proof.sample_workbook()

    def setUp(self):
        self.evidence = good_evidence(self.raw)

    def checks(self, *, supervisor=False, **kwargs):
        response = reply(self.raw, self.evidence, supervisor=supervisor, **kwargs)
        return proof.excel_checks(response, self.raw, supervisor=supervisor)

    def test_complete_direct_and_supervisor_evidence_passes(self):
        self.assertTrue(all(self.checks().values()))
        self.assertTrue(all(self.checks(supervisor=True).values()))

    def test_filename_without_correct_hash_or_length_fails(self):
        for field, value in [("sha256", "bad"), ("bytes", len(self.raw) - 1)]:
            with self.subTest(field=field):
                self.evidence = good_evidence(self.raw)
                self.evidence["workbooks"][0][field] = value
                self.assertFalse(self.checks()["workbook_bytes_and_sha256"])

    def test_missing_sheet_or_incorrect_dimension_fails(self):
        self.evidence["workbooks"][0]["sheets"][1]["rows"] = 1
        self.assertFalse(self.checks()["both_sheets_and_dimensions"])
        self.evidence["workbooks"][0]["sheets"].pop()
        self.assertFalse(self.checks()["both_sheets_and_dimensions"])

    def test_claim_without_real_tool_calls_fails(self):
        self.evidence["tool_calls"] = []
        checks = self.checks()
        self.assertFalse(checks["inspect_tool_executed"])
        self.assertFalse(checks["read_rows_correct"])
        self.assertFalse(checks["grouped_sums_correct"])
        self.assertFalse(checks["cross_sheet_results_correct"])

    def test_failed_tools_do_not_count_as_success(self):
        for call in self.evidence["tool_calls"]:
            call["ok"] = False
        self.assertFalse(self.checks()["grouped_sums_correct"])
        self.assertFalse(self.checks()["cross_sheet_results_correct"])

    def test_incorrect_group_total_is_rejected(self):
        self.evidence["tool_calls"][2]["result"]["groups"][0]["value"] += 1
        self.assertFalse(self.checks()["grouped_sums_correct"])

    def test_preview_checks_cell_values_not_filename(self):
        self.evidence["tool_calls"][1]["result"]["rows"][0]["revenue"] = 123
        self.assertFalse(self.checks()["read_rows_correct"])

    def test_incorrect_regional_or_overall_comparison_is_rejected(self):
        self.evidence["tool_calls"][3]["result"]["rows"][0]["difference"] = 0
        self.assertFalse(self.checks()["cross_sheet_results_correct"])
        self.evidence = good_evidence(self.raw)
        self.evidence["tool_calls"][3]["result"]["totals"]["actual"] = 1
        self.assertFalse(self.checks()["cross_sheet_results_correct"])

    def test_evidence_only_is_not_model_explanation(self):
        self.assertFalse(self.checks(prose="Workbook received.")["model_explains_computed_totals"])
        self.assertFalse(self.checks(supervisor=True, prose="Workbook received.")["model_explains_computed_totals"])

    def test_explanation_accepts_number_formatting(self):
        self.assertTrue(self.checks(prose="Total actual 176500; target 180000; shortfall 3500.")[
            "model_explains_computed_totals"
        ])

    def test_http_success_without_completion_fails(self):
        self.assertFalse(self.checks(status="incomplete")["completed"])

    def test_wrong_peer_transport_or_failed_hop_fails(self):
        for override in [
            {"peer": "research-agent"}, {"transport": "a2a"},
            {"status": "failed"}, {"ok": False}, {"sent_content_types": ["input_text"]},
        ]:
            with self.subTest(override=override):
                self.assertFalse(self.checks(supervisor=True, hop_override=override)["supervisor_forwarded_to_analysis"])

    def test_last_marked_block_wins_over_earlier_model_claim(self):
        response = reply(self.raw, self.evidence)
        response.text += "\n" + marked(proof.EXCEL_ANALYSIS_MARKER, workbooks=[], errors=["Rejected"], tool_calls=[])
        self.assertFalse(proof.excel_checks(response, self.raw, supervisor=False)["workbook_bytes_and_sha256"])

    def test_explicit_workbook_errors_prevent_pass(self):
        self.evidence["errors"] = ["Missing formula result"]
        self.assertFalse(self.checks()["cross_sheet_results_correct"])


if __name__ == "__main__":
    unittest.main()
