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
            w.save(self.root / f"{prefix}_Codebook phụ tùng xe.xlsx")

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
        matcher.select.assert_not_called()

    def test_fallback_names_and_duplicate_expansion(self):
        matcher = Mock()
        matcher.select.return_value = "Sơn cản"
        mapper = CodebookMapper(codebook_dir=self.root, matcher=matcher)
        result = mapper.map_item(self.item("sơn lại cản", "paint"))
        self.assertEqual(result.method, "gemini")
        self.assertEqual(len(result.matches), 2)
        self.assertEqual(matcher.select.call_args.args[1], ["Sơn cản"])
        matcher.select.return_value = None
        self.assertEqual(mapper.map_item(self.item("unknown", "material")).method, "unmatched")
        self.assertEqual(matcher.select.call_args.args[1], ["Sơn cản", "Cản trước"])
        matcher.select.return_value = "invented"
        with self.assertRaises(ValueError):
            mapper.map_item(self.item("unknown", "material"))

    def test_gemini_invalid_and_null(self):
        client = Mock()
        matcher = GeminiMatcher(client, "test")
        client.interactions.create.return_value = response({"name": "invented"})
        with self.assertRaises(ValueError):
            matcher.select(self.item("unknown", "paint"), ["Sơn cản"])
        client.interactions.create.return_value = response({"name": None})
        self.assertIsNone(matcher.select(self.item("unknown", "paint"), ["Sơn cản"]))
        self.assertEqual(len(matcher.calls), 2)

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
