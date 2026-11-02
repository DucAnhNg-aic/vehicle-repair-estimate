import json
import tempfile
import unittest
from pathlib import Path
from openpyxl import Workbook

from claude_fake import MODEL, FakeClaude, message
from cost import cost_from_response
from gemini_fake import MODEL as GEMINI_MODEL, response
from llm import LlmClients
from ocr import RepairEstimate, RepairEstimateItem
from ocr.schema import Document
from repair_estimate_pipeline import new_run_dir, run_repair_estimate_pipeline

OCR_USAGE = {"input_tokens": 1800, "output_tokens": 6500}
# Lần mapping đầu ghi codebook vào prompt cache 1 giờ.
MAP_USAGE = {"input_tokens": 400, "cache_creation_input_tokens": 29600, "cache_read_input_tokens": 0,
             "cache_creation": {"ephemeral_1h_input_tokens": 29600}, "output_tokens": 320}


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
        self.client = FakeClaude()

    def run_vre_pipeline(self, **kwargs):
        return run_repair_estimate_pipeline(
            self.source, model=MODEL, llm=LlmClients(clients={"claude": self.client}), output_dir=self.root / "out",
            codebook_dir=self.root, verbose=False, **kwargs,
        )

    def test_full_run_logs_results_and_costs_per_step(self):
        self.client.replies["ocr"] = message(self.estimate.model_dump(mode="json"), usage=OCR_USAGE)
        self.client.replies["mapping"] = [
            message({"selections": [{"index": 0, "name": "Cản", "item_type": "part"}, {"index": 1, "name": "Cản trước", "item_type": "part"}, {"index": 2, "name": None, "item_type": None}]}, usage=MAP_USAGE),
        ]
        summary = self.run_vre_pipeline()
        self.assertEqual([i["category"] for i in self.client.sent_items()], ["paint", "material", "labor"])
        self.assertTrue(all(set(n) == {"name", "item_type"} for n in self.client.sent_names()))
        self.assertEqual(self.client.call_count(), 2)  # OCR + one mapping
        run_dir = Path(summary["run_dir"])
        self.assertEqual(summary["input_sha256"], __import__("hashlib").sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(summary["status"], "success")
        self.assertEqual(summary["steps"]["ocr"]["items"], 3)
        self.assertEqual(summary["steps"]["mapping"]["methods"], {"claude": 2, "unmatched": 1})
        self.assertEqual(summary["steps"]["mapping"]["llm_calls"], 1)
        self.assertEqual([i["codes"] for i in summary["items"]], [["S1"], ["P1", "P2"], []])
        ocr_cost = cost_from_response({"model": MODEL, "usage": OCR_USAGE}, MODEL)["total_usd"]
        map_cost = cost_from_response({"model": MODEL, "usage": MAP_USAGE}, MODEL)["total_usd"]
        self.assertAlmostEqual(ocr_cost, (1800 * 4 + 6500 * 20) / 1e6)
        self.assertAlmostEqual(map_cost, (400 * 4 + 29600 * 8 + 320 * 20) / 1e6)  # ghi cache 1h = 2x input
        self.assertAlmostEqual(summary["steps"]["ocr"]["cost"]["total_usd"], ocr_cost)
        self.assertAlmostEqual(summary["steps"]["mapping"]["cost"]["total_usd"], map_cost)
        total = summary["total"]["cost"]
        self.assertAlmostEqual(total["total_usd"], ocr_cost + map_cost)
        self.assertEqual((total["calls"], total["priced_calls"], total["complete"]), (2, 2, True))
        for name in ["summary.json", "report.txt", "mapping.json", "mapping.calls.json", "ocr/result.json"]:
            self.assertTrue((run_dir / name).is_file(), name)
        self.assertEqual(json.loads((run_dir / "summary.json").read_text())["status"], "success")
        report = (run_dir / "report.txt").read_text()
        self.assertIn("TỔNG", report)
        self.assertIn("cache đọc 0 · ghi 29,600", report)
        calls = json.loads((run_dir / "mapping.calls.json").read_text())
        self.assertTrue(all(call["cost"]["total_usd"] is not None for call in calls))

    def test_exact_only_makes_single_claude_call(self):
        self.client.replies["ocr"] = message(self.estimate.model_dump(mode="json"), usage=OCR_USAGE)
        summary = self.run_vre_pipeline(exact_only=True)
        self.assertEqual(self.client.call_count(), 1)
        self.assertEqual(summary["steps"]["mapping"]["methods"], {"unmatched": 3})
        self.assertEqual(summary["steps"]["mapping"]["cost"]["calls"], 0)
        self.assertEqual(summary["total"]["cost"]["calls"], 1)

    def test_ocr_failure_still_writes_summary_with_cost(self):
        self.client.replies["ocr"] = message({"invalid": True}, usage=OCR_USAGE)
        with self.assertRaises(ValueError):
            self.run_vre_pipeline()
        summary = json.loads(next((self.root / "out").rglob("summary.json")).read_text())
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["steps"]["ocr"]["status"], "failed")
        self.assertEqual(summary["steps"]["mapping"]["status"], "skipped")
        self.assertEqual(summary["total"]["cost"]["priced_calls"], 1)  # tokens đã tốn vẫn được tính
        self.assertIn("ValidationError", summary["error"])

    def test_failure_summary_lands_in_caller_chosen_run_dir(self):
        self.client.replies["ocr"] = message({"invalid": True}, usage=OCR_USAGE)
        run_dir = new_run_dir(self.root / "out")
        with self.assertRaises(ValueError):
            self.run_vre_pipeline(run_dir=run_dir)
        self.assertEqual(json.loads((run_dir / "summary.json").read_text())["status"], "failed")

    def test_ocr_and_mapping_can_use_different_providers(self):
        """OCR bằng Gemini, mapping bằng Claude: mỗi bước gọi đúng client của provider mình."""
        from unittest.mock import Mock
        gemini = Mock()
        gemini.interactions.create.return_value = response(self.estimate.model_dump(mode="json"))
        self.client.replies["mapping"] = message({"selections": [
            {"index": 0, "name": "Cản", "item_type": "part"}, {"index": 1, "name": None, "item_type": None},
            {"index": 2, "name": None, "item_type": None}]}, usage=MAP_USAGE)
        summary = run_repair_estimate_pipeline(
            self.source, model=GEMINI_MODEL, mapping_model=MODEL,
            llm=LlmClients(clients={"claude": self.client, "gemini": gemini}),
            output_dir=self.root / "out", codebook_dir=self.root, verbose=False)
        self.assertEqual(gemini.interactions.create.call_count, 1)
        gemini.models.generate_content.assert_not_called()
        self.assertEqual((len(self.client.calls["ocr"]), len(self.client.calls["mapping"])), (0, 1))
        self.assertEqual(summary["models"], {"ocr": GEMINI_MODEL, "mapping": MODEL})
        self.assertEqual(summary["steps"]["mapping"]["methods"], {"claude": 1, "unmatched": 2})
        report = (Path(summary["run_dir"]) / "report.txt").read_text()
        self.assertIn("→ Cản", report)

    def test_missing_api_key_of_needed_provider_is_reported(self):
        llm = LlmClients(env={"ANTHROPIC_API_KEY": "k"})
        llm.require(MODEL)
        with self.assertRaisesRegex(RuntimeError, "GEMINI_API_KEY"):
            llm.require(MODEL, GEMINI_MODEL)
        with self.assertRaisesRegex(ValueError, "provider"):
            llm.require("gpt-5")


if __name__ == "__main__":
    unittest.main()
