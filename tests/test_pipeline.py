import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from openpyxl import Workbook

from cost import cost_from_response
from ocr import Quotation, QuotationItem
from ocr.schema import Document
from pipeline import run_pipeline

MODEL = "gemini-3.8-flash"
OCR_USAGE = {"total_input_tokens": 1800, "total_cached_tokens": 0,
             "total_output_tokens": 4000, "total_thought_tokens": 2500}
MAP_USAGE = {"total_input_tokens": 30000, "total_cached_tokens": 0,
             "total_output_tokens": 20, "total_thought_tokens": 300}


def response(payload, usage):
    return SimpleNamespace(status="completed", output_text=json.dumps(payload, ensure_ascii=False),
                           model_dump=lambda **kwargs: {"status": "completed", "model": MODEL, "usage": usage})


def quotation(*items):
    rows = []
    for raw_name, category in items:
        row = {name: None for name in QuotationItem.model_fields}
        row.update(raw_name=raw_name, section_type="service", item_name=raw_name,
                   category=category, source_evidence=raw_name)
        rows.append(row)
    return Quotation.model_validate({"document": {name: None for name in Document.model_fields}, "items": rows})


class PipelineRunTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for prefix, rows in [("service", [("S1", "Sơn cản")]), ("part", [("P1", "Cản trước"), ("P2", "Cản trước")])]:
            w = Workbook()
            w.active.append(["MA", "TEN"])
            for row in rows:
                w.active.append(row)
            w.save(self.root / f"{prefix}_codebook.xlsx")
        self.source = self.root / "bao_gia.md"
        self.source.write_text("Báo giá")
        self.quotation = quotation(("Sơn cản", "paint"), ("Nẹp cản trước", "material"), ("Không có", "labor"))
        self.client = Mock()

    def run_pipeline(self, **kwargs):
        return run_pipeline(self.source, model=MODEL, client=self.client, output_dir=self.root / "out",
                            codebook_dir=self.root, verbose=False, **kwargs)

    def test_full_run_logs_results_and_costs_per_step(self):
        self.client.interactions.create.side_effect = [
            response(self.quotation.model_dump(mode="json"), OCR_USAGE),  # OCR
            response({"name": "Cản trước"}, MAP_USAGE),                    # fallback: Nẹp cản trước
            response({"name": None}, MAP_USAGE),                           # fallback: Không có
        ]
        summary = self.run_pipeline()
        run_dir = Path(summary["run_dir"])
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["steps"]["ocr"]["items"], 3)
        self.assertEqual(summary["steps"]["mapping"]["methods"], {"exact": 1, "gemini": 1, "unmatched": 1})
        self.assertEqual(summary["steps"]["mapping"]["gemini_calls"], 2)
        self.assertEqual([i["codes"] for i in summary["items"]], [["S1"], ["P1", "P2"], []])
        ocr_cost = cost_from_response({"model": MODEL, "usage": OCR_USAGE}, MODEL)["total_usd"]
        map_cost = 2 * cost_from_response({"model": MODEL, "usage": MAP_USAGE}, MODEL)["total_usd"]
        self.assertAlmostEqual(summary["steps"]["ocr"]["cost"]["total_usd"], ocr_cost)
        self.assertAlmostEqual(summary["steps"]["mapping"]["cost"]["total_usd"], map_cost)
        total = summary["total"]["cost"]
        self.assertAlmostEqual(total["total_usd"], ocr_cost + map_cost)
        self.assertEqual((total["calls"], total["priced_calls"], total["complete"]), (3, 3, True))
        for name in ["summary.json", "report.txt", "mapping.json", "mapping.calls.json", "ocr/result.json"]:
            self.assertTrue((run_dir / name).is_file(), name)
        self.assertEqual(json.loads((run_dir / "summary.json").read_text())["status"], "success")
        self.assertIn("TỔNG", (run_dir / "report.txt").read_text())
        calls = json.loads((run_dir / "mapping.calls.json").read_text())
        self.assertTrue(all(call["cost"]["total_usd"] is not None for call in calls))

    def test_exact_only_makes_single_gemini_call(self):
        self.client.interactions.create.return_value = response(self.quotation.model_dump(mode="json"), OCR_USAGE)
        summary = self.run_pipeline(exact_only=True)
        self.assertEqual(self.client.interactions.create.call_count, 1)
        self.assertEqual(summary["steps"]["mapping"]["methods"], {"exact": 1, "unmatched": 2})
        self.assertEqual(summary["steps"]["mapping"]["cost"]["calls"], 0)
        self.assertEqual(summary["total"]["cost"]["calls"], 1)

    def test_ocr_failure_still_writes_summary_with_cost(self):
        self.client.interactions.create.return_value = response({"invalid": True}, OCR_USAGE)
        with self.assertRaises(ValueError):
            self.run_pipeline()
        summary = json.loads(next((self.root / "out").rglob("summary.json")).read_text())
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["steps"]["ocr"]["status"], "failed")
        self.assertEqual(summary["steps"]["mapping"]["status"], "skipped")
        self.assertEqual(summary["total"]["cost"]["priced_calls"], 1)  # tokens đã tốn vẫn được tính
        self.assertIn("ValidationError", summary["error"])


if __name__ == "__main__":
    unittest.main()
