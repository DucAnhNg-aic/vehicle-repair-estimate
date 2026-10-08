import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from openpyxl import Workbook
from ocr import RepairEstimate, RepairEstimateItem, extract_repair_estimate
from ocr.schema import Document
from mapping import CodebookMapper, GeminiMatcher

ROOT = Path(__file__).resolve().parents[1]


def response(payload, status="completed"):
    return SimpleNamespace(text=json.dumps(payload, ensure_ascii=False), candidates=[SimpleNamespace(finish_reason="STOP")], status=status, output_text=json.dumps(payload, ensure_ascii=False),
                           model_dump=lambda **kwargs: {"status": status, "usage": {}})


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        item = {name: None for name in RepairEstimateItem.model_fields}
        item.update(raw_name="Sơn cản", section_type="service", item_name="cản",
                    category="paint", source_evidence="Sơn cản")
        cls.estimate = RepairEstimate.model_validate({
            "document": {name: None for name in Document.model_fields}, "items": [item]})

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        w = Workbook()
        w.active.append(["MA", "TEN"])
        w.active.append(["P1", "Cản trước"])
        w.save(self.root / "part_codebook.xlsx")
        w = Workbook()
        w.active.append(["MA", "TEN", "item_type"])
        for row in [("P1", "Cản trước", "part"), ("L1", "Vè trước trái", "part"), ("L2", "Vè trước trái", "part"),
                    ("R1", "Vè trước phải", "part"), ("D1", "Cánh cửa", "part"),
                    ("S1", "Công tháo lắp", "service"),
                    ("S2", "Công tháo lắp vè trước trái", "service"),
                    ("S3", "Cánh cửa", "service")]:
            w.active.append(row)
        w.save(self.root / "codebook.xlsx")

    def item(self, name, category):
        return self.estimate.items[0].model_copy(update={"raw_name": name, "category": category})

    def test_material_exact_and_work_always_goes_to_gemini(self):
        matcher = Mock()
        matcher.select_many.return_value = [("part", "Vè trước trái")]
        mapper = CodebookMapper(codebook_dir=self.root, matcher=matcher)
        result = mapper.map_item(self.item("Cản trước", "material"))
        self.assertEqual(result.matches[0].code, "P1")
        matcher.select_many.assert_not_called()
        result = mapper.map_item(self.item("Công tháo lắp vè trước trái", "labor"))
        self.assertEqual(result.method, "gemini")
        self.assertEqual(result.codebook, "codebook.xlsx")
        self.assertEqual([m.code for m in result.matches], ["L1", "L2"])
        self.assertTrue(all(m.item_type == "part" for m in result.matches))
        names = matcher.select_many.call_args.args[1]
        self.assertIn("Vè trước trái", names["part"])
        self.assertIn("Công tháo lắp", names["service"])

    def test_typed_results_do_not_expand_into_other_type(self):
        matcher = Mock()
        matcher.select_many.return_value = [("part", "Cánh cửa"), ("service", "Công tháo lắp"), None]
        mapper = CodebookMapper(codebook_dir=self.root, matcher=matcher)
        results = mapper.map_items([self.item("Công sơn cánh cửa", "paint"),
                                   self.item("Công tháo lắp phục vụ sơn", "labor"),
                                   self.item("Sơn bộ phận không có", "paint")])
        self.assertEqual([[r.code for r in x.matches] for x in results], [["D1"], ["S1"], []])
        self.assertEqual(results[2].method, "unmatched")

    def test_batch_order_and_chunks(self):
        matcher = Mock()
        matcher.select_many.side_effect = lambda items, names: [None] * len(items)
        mapper = CodebookMapper(codebook_dir=self.root, matcher=matcher)
        mapper.BATCH_SIZE = 2
        items = [self.item(f"lạ {i}", "paint") for i in range(5)] + [self.item("Cản trước", "material")]
        results = mapper.map_items(items)
        self.assertEqual([len(c.args[0]) for c in matcher.select_many.call_args_list], [2, 2, 1])
        self.assertEqual([r.raw_name for r in results], [i.raw_name for i in items])
        self.assertEqual(results[-1].method, "exact")

    def test_without_matcher_work_is_unmatched(self):
        result = CodebookMapper(codebook_dir=self.root).map_item(self.item("Công tháo lắp", "labor"))
        self.assertEqual(result.method, "unmatched")

    def test_missing_or_invalid_item_type_fails(self):
        w = Workbook()
        w.active.append(["MA", "TEN"])
        w.active.append(["1", "Cản"])
        w.save(self.root / "codebook.xlsx")
        with self.assertRaisesRegex(ValueError, "item_type"):
            CodebookMapper(codebook_dir=self.root)
        w.active.cell(1, 3, "item_type")
        w.active.cell(2, 3, "unknown")
        w.save(self.root / "codebook.xlsx")
        with self.assertRaisesRegex(ValueError, "item_type"):
            CodebookMapper(codebook_dir=self.root)

    def test_work_schema_checks_type_name_and_indexes(self):
        client = Mock()
        matcher = GeminiMatcher(client, "test", context_cache_enabled=False)
        names = {"part": ["Cánh cửa"], "service": ["Công tháo lắp"]}
        items = [self.item("Công sơn cánh cửa", "paint")]
        for selection in [dict(index=0, name="Cánh cửa", item_type="service"),
                          dict(index=0, name="Cánh cửa", item_type=None),
                          dict(index=0, name=None, item_type="part"),
                          dict(index=1, name="Cánh cửa", item_type="part")]:
            client.models.generate_content.return_value = response({"selections": [selection]})
            with self.assertRaises(ValueError):
                matcher.select_many(items, names)
        client.models.generate_content.return_value = response({"selections": [dict(index=0, name="Cánh cửa", item_type="part")]})
        self.assertEqual(matcher.select_many(items, names), [("part", "Cánh cửa")])
        sent = json.loads(client.models.generate_content.call_args.kwargs["contents"])
        self.assertEqual(sent["names"], [{"name": n, "item_type": k} for k, ns in names.items() for n in ns])
        self.assertEqual(sent["items"][0]["raw_name"], "Công sơn cánh cửa")

    def test_material_cannot_select_service(self):
        client = Mock()
        client.models.generate_content.return_value = response({"selections": [
            {"index": 0, "name": "Công tháo lắp", "item_type": "service"}]})
        mapper = CodebookMapper(codebook_dir=self.root, matcher=GeminiMatcher(client, "test", context_cache_enabled=False))
        with self.assertRaisesRegex(ValueError, "Vật tư"):
            mapper.map_item(self.item("phụ tùng lạ", "material"))

    def test_only_common_workbook_is_required(self):
        (self.root / "part_codebook.xlsx").unlink()
        result = CodebookMapper(codebook_dir=self.root).map_item(self.item("Cản trước", "material"))
        self.assertEqual(result.codebook, "codebook.xlsx")
        self.assertEqual(result.matches[0].code, "P1")

    def test_gemini_batch_validation(self):
        client = Mock()
        matcher = GeminiMatcher(client, "test", context_cache_enabled=False)
        items = [self.item("a", "paint"), self.item("b", "paint")]
        sel = lambda *pairs: response({"selections": [{"index": i, "name": n} for i, n in pairs]})
        client.models.generate_content.return_value = sel((0, "invented"), (1, None))
        with self.assertRaises(ValueError):  # tên ngoài codebook
            matcher.select_many(items, ["Sơn cản"])
        client.models.generate_content.return_value = sel((0, "Sơn cản"))
        with self.assertRaises(ValueError):  # thiếu index 1
            matcher.select_many(items, ["Sơn cản"])
        client.models.generate_content.return_value = sel((0, "Sơn cản"), (0, None))
        with self.assertRaises(ValueError):  # trùng index, thiếu index 1
            matcher.select_many(items, ["Sơn cản"])
        client.models.generate_content.return_value = sel((1, None), (0, "Sơn cản"))  # đảo thứ tự vẫn đúng
        self.assertEqual(matcher.select_many(items, ["Sơn cản"]), ["Sơn cản", None])
        self.assertEqual(client.models.generate_content.call_count, 4)
        self.assertEqual(len(matcher.calls), 4)
        self.assertEqual(matcher.calls[-1]["items"], ["a", "b"])
        self.assertEqual(matcher.calls[-1]["status"], "success")

    def test_prompt_per_codebook(self):
        from mapping.prompt import MAPPING_INSTRUCTION
        client = Mock()
        matcher = GeminiMatcher(client, "test", context_cache_enabled=False)
        client.models.generate_content.return_value = response({"selections": [{"index": 0, "name": None}]})
        matcher.select_many([self.item("x", "material")], ["Cản"])
        self.assertEqual(client.models.generate_content.call_args.kwargs["config"].system_instruction, MAPPING_INSTRUCTION)
        client.models.generate_content.return_value = response({"selections": [{"index": 0, "name": None, "item_type": None}]})
        matcher.select_many([self.item("x", "paint")], {"part": ["Cản"], "service": ["Công sơn"]})
        self.assertEqual(client.models.generate_content.call_args.kwargs["config"].system_instruction, MAPPING_INSTRUCTION)
        self.assertEqual([c["prompt"] for c in matcher.calls], ["mapping", "mapping"])

    def test_gemini_single_select_and_null(self):
        client = Mock()
        matcher = GeminiMatcher(client, "test", context_cache_enabled=False)
        client.models.generate_content.return_value = response({"selections": [{"index": 0, "name": None}]})
        self.assertIsNone(matcher.select(self.item("unknown", "paint"), ["Sơn cản"]))
        sent = json.loads(client.models.generate_content.call_args.kwargs["contents"])
        self.assertEqual([i["index"] for i in sent["items"]], [0])

    def test_ocr_api_preserves_artifacts_and_client(self):
        client = Mock()
        client.interactions.create.return_value = response(self.estimate.model_dump(mode="json"))
        source = self.root / "input.md"
        source.write_text("Vehicle Repair Estimate")
        result = extract_repair_estimate(source, model="test", client=client, output_dir=self.root / "ocr")
        self.assertEqual(result, self.estimate)
        client.close.assert_not_called()
        self.assertEqual(len(list((self.root / "ocr").rglob("result.json"))), 1)

    def test_ocr_invalid_response_preserves_failure(self):
        client = Mock()
        client.interactions.create.return_value = response({"invalid": True})
        source = self.root / "input.md"
        source.write_text("Vehicle Repair Estimate")
        with self.assertRaises(ValueError):
            extract_repair_estimate(source, model="test", client=client, output_dir=self.root / "ocr")
        metadata = json.loads(next((self.root / "ocr").rglob("metadata.json")).read_text())
        self.assertEqual(metadata["status"], "failed")
        self.assertTrue(list((self.root / "ocr").rglob("response.json")))

    def test_legacy_estimate_number_is_read_and_written_with_new_name(self):
        payload = self.estimate.model_dump(mode="json")
        payload["document"]["quotation_number"] = payload["document"].pop("estimate_number")
        parsed = RepairEstimate.model_validate(payload)
        document = parsed.model_dump(mode="json")["document"]
        self.assertIn("estimate_number", document)
        self.assertNotIn("quotation_number", document)


if __name__ == "__main__":
    unittest.main()
