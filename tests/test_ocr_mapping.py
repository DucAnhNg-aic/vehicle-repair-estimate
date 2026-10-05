import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from openpyxl import Workbook
from ocr import Quotation, QuotationItem, extract_document
from ocr.schema import Document
from mapping import CodebookMapper, GeminiMatcher

ROOT = Path(__file__).resolve().parents[1]


def response(payload, status="completed"):
    return SimpleNamespace(status=status, output_text=json.dumps(payload, ensure_ascii=False),
                           model_dump=lambda **kwargs: {"status": status, "usage": {}})


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        item = {name: None for name in QuotationItem.model_fields}
        item.update(raw_name="Sơn cản", section_type="service", item_name="cản",
                    category="paint", source_evidence="Sơn cản")
        cls.quotation = Quotation.model_validate({
            "document": {name: None for name in Document.model_fields}, "items": [item]})

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for prefix, rows in [("service", [("S1", "Sơn cản"), ("S2", "Sơn cản")]),
                             ("part", [("P1", "Sơn cản"), ("P2", "Cản trước")])]:
            w = Workbook()
            w.active.append(["MA", "TEN"])
            for row in rows:
                w.active.append(row)
            w.save(self.root / f"{prefix}_codebook.xlsx")

    def item(self, name, category):
        return self.quotation.items[0].model_copy(update={"raw_name": name, "category": category})

    def test_exact_duplicates_and_routing_without_fallback(self):
        matcher = Mock()
        mapper = CodebookMapper(codebook_dir=self.root, matcher=matcher)
        for category in ["labor", "paint"]:
            result = mapper.map_item(self.item("Sơn cản", category))
            self.assertEqual([m.code for m in result.matches], ["S1", "S2"])
            self.assertEqual(result.method, "exact")
        self.assertEqual(mapper.map_item(self.item("Sơn cản", "material")).matches[0].code, "P1")
        matcher.select_many.assert_not_called()

    def test_fallback_names_and_duplicate_expansion(self):
        matcher = Mock()
        matcher.select_many.return_value = ["Sơn cản"]
        mapper = CodebookMapper(codebook_dir=self.root, matcher=matcher)
        result = mapper.map_item(self.item("sơn lại cản", "paint"))
        self.assertEqual(result.method, "gemini")
        self.assertEqual(len(result.matches), 2)
        self.assertEqual(matcher.select_many.call_args.args[1], ["Sơn cản"])
        matcher.select_many.return_value = [None]
        self.assertEqual(mapper.map_item(self.item("unknown", "material")).method, "unmatched")
        self.assertEqual(matcher.select_many.call_args.args[1], ["Sơn cản", "Cản trước"])
        matcher.select_many.return_value = ["invented"]
        with self.assertRaises(ValueError):
            mapper.map_item(self.item("unknown", "material"))

    def test_batch_sends_one_request_per_codebook_and_keeps_order(self):
        matcher = Mock()
        matcher.select_many.side_effect = lambda items, names: [
            "Sơn cản" if i.raw_name == "sơn lại cản" else None for i in items]
        mapper = CodebookMapper(codebook_dir=self.root, matcher=matcher)
        items = [self.item("sơn lại cản", "paint"), self.item("Sơn cản", "labor"), self.item("không có", "labor"),
                 self.item("Cản trước", "material"), self.item("lạ", "material"), self.item("sơn lại cản", "labor")]
        results = mapper.map_items(items)
        self.assertEqual([r.method for r in results], ["gemini", "exact", "unmatched", "exact", "unmatched", "gemini"])
        self.assertEqual([r.raw_name for r in results], [i.raw_name for i in items])
        self.assertEqual(matcher.select_many.call_count, 2)  # service (3 item) + part (1 item), không gọi lẻ
        service_call, part_call = matcher.select_many.call_args_list
        self.assertEqual([i.raw_name for i in service_call.args[0]], ["sơn lại cản", "không có", "sơn lại cản"])
        self.assertEqual([i.raw_name for i in part_call.args[0]], ["lạ"])

    def test_batch_is_chunked_by_batch_size(self):
        matcher = Mock()
        matcher.select_many.side_effect = lambda items, names: [None] * len(items)
        mapper = CodebookMapper(codebook_dir=self.root, matcher=matcher)
        mapper.BATCH_SIZE = 2
        results = mapper.map_items([self.item(f"lạ {n}", "paint") for n in range(5)])
        self.assertEqual([len(c.args[0]) for c in matcher.select_many.call_args_list], [2, 2, 1])
        self.assertEqual(len(results), 5)

    def test_no_matcher_leaves_unmatched_without_calls(self):
        results = CodebookMapper(codebook_dir=self.root).map_items([self.item("lạ", "paint"), self.item("Sơn cản", "paint")])
        self.assertEqual([r.method for r in results], ["unmatched", "exact"])

    def test_gemini_batch_validation(self):
        client = Mock()
        matcher = GeminiMatcher(client, "test")
        items = [self.item("a", "paint"), self.item("b", "paint")]
        sel = lambda *pairs: response({"selections": [{"index": i, "name": n} for i, n in pairs]})
        client.interactions.create.return_value = sel((0, "invented"), (1, None))
        with self.assertRaises(ValueError):  # tên ngoài codebook
            matcher.select_many(items, ["Sơn cản"])
        client.interactions.create.return_value = sel((0, "Sơn cản"))
        with self.assertRaises(ValueError):  # thiếu index 1
            matcher.select_many(items, ["Sơn cản"])
        client.interactions.create.return_value = sel((0, "Sơn cản"), (0, None))
        with self.assertRaises(ValueError):  # trùng index, thiếu index 1
            matcher.select_many(items, ["Sơn cản"])
        client.interactions.create.return_value = sel((1, None), (0, "Sơn cản"))  # đảo thứ tự vẫn đúng
        self.assertEqual(matcher.select_many(items, ["Sơn cản"]), ["Sơn cản", None])
        self.assertEqual(client.interactions.create.call_count, 4)
        self.assertEqual(len(matcher.calls), 4)
        self.assertEqual(matcher.calls[-1]["items"], ["a", "b"])
        self.assertEqual(matcher.calls[-1]["status"], "success")

    def test_prompt_per_codebook(self):
        from mapping.prompt import PART_INSTRUCTION, SERVICE_INSTRUCTION
        client = Mock()
        matcher = GeminiMatcher(client, "test")
        client.interactions.create.return_value = response({"selections": [{"index": 0, "name": None}]})
        matcher.select_many([self.item("x", "material")], ["Cản"])
        self.assertEqual(client.interactions.create.call_args.kwargs["system_instruction"], PART_INSTRUCTION)
        matcher.select_many([self.item("x", "paint")], ["Sơn cản"])
        self.assertEqual(client.interactions.create.call_args.kwargs["system_instruction"], SERVICE_INSTRUCTION)
        self.assertEqual([c["prompt"] for c in matcher.calls], ["part", "service"])
        self.assertNotEqual(PART_INSTRUCTION, SERVICE_INSTRUCTION)

    def test_gemini_single_select_and_null(self):
        client = Mock()
        matcher = GeminiMatcher(client, "test")
        client.interactions.create.return_value = response({"selections": [{"index": 0, "name": None}]})
        self.assertIsNone(matcher.select(self.item("unknown", "paint"), ["Sơn cản"]))
        sent = json.loads(client.interactions.create.call_args.kwargs["input"])
        self.assertEqual([i["index"] for i in sent["items"]], [0])

    def test_ocr_api_preserves_artifacts_and_client(self):
        client = Mock()
        client.interactions.create.return_value = response(self.quotation.model_dump(mode="json"))
        source = self.root / "input.md"
        source.write_text("Quotation")
        result = extract_document(source, model="test", client=client, output_dir=self.root / "ocr")
        self.assertEqual(result, self.quotation)
        client.close.assert_not_called()
        self.assertEqual(len(list((self.root / "ocr").rglob("result.json"))), 1)

    def test_ocr_invalid_response_preserves_failure(self):
        client = Mock()
        client.interactions.create.return_value = response({"invalid": True})
        source = self.root / "input.md"
        source.write_text("Quotation")
        with self.assertRaises(ValueError):
            extract_document(source, model="test", client=client, output_dir=self.root / "ocr")
        metadata = json.loads(next((self.root / "ocr").rglob("metadata.json")).read_text())
        self.assertEqual(metadata["status"], "failed")
        self.assertTrue(list((self.root / "ocr").rglob("response.json")))


if __name__ == "__main__":
    unittest.main()
