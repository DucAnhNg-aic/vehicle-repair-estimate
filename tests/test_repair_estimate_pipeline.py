import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openpyxl import Workbook

from cost import cost_from_response
from ocr import RepairEstimate, RepairEstimateItem
from ocr.schema import Document
from repair_estimate_pipeline import run_repair_estimate_pipeline

MODEL = "gemini-3.8-flash"
OCR_USAGE = {"total_input_tokens": 1800, "total_cached_tokens": 0,
             "total_output_tokens": 4000, "total_thought_tokens": 2500}
MAP_USAGE = {"total_input_tokens": 30000, "total_cached_tokens": 0,
             "total_output_tokens": 20, "total_thought_tokens": 300}


def response(payload, usage):
    return SimpleNamespace(text=json.dumps(payload, ensure_ascii=False), candidates=[SimpleNamespace(finish_reason="STOP")], status="completed", output_text=json.dumps(payload, ensure_ascii=False),
                           model_dump=lambda **kwargs: {"status": "completed", "model": MODEL, "usage": usage})


def repair_estimate(*items):
    rows = []
    for raw_name, category in items:
        row = {name: None for name in RepairEstimateItem.model_fields}
        row.update(raw_name=raw_name, section_type="service", item_name=raw_name,
                   category=category, source_evidence=raw_name)
        rows.append(row)
    return RepairEstimate.model_validate({"document": {name: None for name in Document.model_fields}, "items": rows})


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
        w = Workbook()
        w.active.append(["MA", "TEN", "item_type"])
        w.active.append(["S1", "Cản", "part"])
        w.active.append(["P1", "Cản trước", "part"])
        w.active.append(["P2", "Cản trước", "part"])
        w.save(self.root / "codebook.xlsx")
        self.source = self.root / "bao_gia.md"
        self.source.write_text("Báo giá")
        self.estimate = repair_estimate(("Sơn cản", "paint"), ("Nẹp cản trước", "material"), ("Không có", "labor"))
        self.client = Mock()
        self.context_env = patch.dict("os.environ", {"MAPPING_CONTEXT_CACHE_ENABLED": "false"})
        self.context_env.start()
        self.addCleanup(self.context_env.stop)

    def run_vre_pipeline(self, **kwargs):
        return run_repair_estimate_pipeline(
            self.source, model=MODEL, client=self.client, output_dir=self.root / "out",
            codebook_dir=self.root, verbose=False, **kwargs,
        )

    def test_full_run_logs_results_and_costs_per_step(self):
        self.client.interactions.create.return_value = response(self.estimate.model_dump(mode="json"), OCR_USAGE)
        self.client.models.generate_content.side_effect = [
            response({"selections": [{"index": 0, "name": "Cản", "item_type": "part"}, {"index": 1, "name": "Cản trước", "item_type": "part"}, {"index": 2, "name": None, "item_type": None}]}, MAP_USAGE),            # 1 request service: Không có
        ]
        summary = self.run_vre_pipeline()
        sent = json.loads(self.client.models.generate_content.call_args.kwargs["contents"])
        self.assertEqual([i["category"] for i in sent["items"]], ["paint", "material", "labor"])
        self.assertTrue(all(set(n) == {"name", "item_type"} for n in sent["names"]))
        self.assertEqual(self.client.interactions.create.call_count + self.client.models.generate_content.call_count, 2)  # OCR + one mapping
        run_dir = Path(summary["run_dir"])
        self.assertEqual(summary["input_sha256"], __import__("hashlib").sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["steps"]["ocr"]["items"], 3)
        self.assertEqual(summary["steps"]["mapping"]["methods"], {"gemini": 2, "unmatched": 1})
        self.assertEqual(summary["steps"]["mapping"]["gemini_calls"], 1)
        self.assertEqual([i["codes"] for i in summary["items"]], [["S1"], ["P1", "P2"], []])
        ocr_cost = cost_from_response({"model": MODEL, "usage": OCR_USAGE}, MODEL)["total_usd"]
        map_cost = 1 * cost_from_response({"model": MODEL, "usage": MAP_USAGE}, MODEL)["total_usd"]
        self.assertAlmostEqual(summary["steps"]["ocr"]["cost"]["total_usd"], ocr_cost)
        self.assertAlmostEqual(summary["steps"]["mapping"]["cost"]["total_usd"], map_cost)
        total = summary["total"]["cost"]
        self.assertAlmostEqual(total["total_usd"], ocr_cost + map_cost)
        self.assertEqual((total["calls"], total["priced_calls"], total["complete"]), (2, 2, True))
        for name in ["summary.json", "report.txt", "mapping.json", "mapping.calls.json", "ocr/result.json"]:
            self.assertTrue((run_dir / name).is_file(), name)
        self.assertEqual(json.loads((run_dir / "summary.json").read_text())["status"], "success")
        self.assertIn("TỔNG", (run_dir / "report.txt").read_text())
        calls = json.loads((run_dir / "mapping.calls.json").read_text())
        self.assertTrue(all(call["cost"]["total_usd"] is not None for call in calls))

    def test_exact_only_makes_single_gemini_call(self):
        self.client.interactions.create.return_value = response(self.estimate.model_dump(mode="json"), OCR_USAGE)
        summary = self.run_vre_pipeline(exact_only=True)
        self.assertEqual(self.client.interactions.create.call_count, 1)
        self.assertEqual(summary["steps"]["mapping"]["methods"], {"unmatched": 3})
        self.assertEqual(summary["steps"]["mapping"]["cost"]["calls"], 0)
        self.assertEqual(summary["total"]["cost"]["calls"], 1)

    def test_ocr_failure_still_writes_summary_with_cost(self):
        self.client.interactions.create.return_value = response({"invalid": True}, OCR_USAGE)
        with self.assertRaises(ValueError):
            self.run_vre_pipeline()
        summary = json.loads(next((self.root / "out").rglob("summary.json")).read_text())
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["steps"]["ocr"]["status"], "failed")
        self.assertEqual(summary["steps"]["mapping"]["status"], "skipped")
        self.assertEqual(summary["total"]["cost"]["priced_calls"], 1)  # tokens đã tốn vẫn được tính
        self.assertIn("ValidationError", summary["error"])


if __name__ == "__main__":
    unittest.main()
