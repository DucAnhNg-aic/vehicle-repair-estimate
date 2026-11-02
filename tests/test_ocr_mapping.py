import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openpyxl import Workbook
from PIL import Image

from claude_fake import MODEL, FakeClaude, message
from gemini_fake import MODEL as GEMINI_MODEL, response, sent_contents
from llm.claude import Backend as ClaudeBackend
from llm.gemini import Backend as GeminiBackend
from ocr import RepairEstimate, RepairEstimateItem, extract_repair_estimate
from ocr.schema import Document
from mapping import CodebookMapper, LlmMatcher

ROOT = Path(__file__).resolve().parents[1]


NAMES = {"part": ["Cản"], "service": []}
NO_MATCH = {"selections": [{"index": 0, "name": None, "item_type": None}]}


def claude_matcher(client, model=MODEL, **kwargs):
    return LlmMatcher(ClaudeBackend(client), model, **kwargs)


def gemini_matcher(client, model=GEMINI_MODEL, **kwargs):
    return LlmMatcher(GeminiBackend(client, context_cache_enabled=False), model, **kwargs)


def mock_matcher(method="claude"):
    matcher = Mock()
    matcher.method = method  # tên provider ghi vào mapping.method
    return matcher


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

    def test_material_exact_and_work_always_goes_to_llm(self):
        matcher = mock_matcher()
        matcher.select_many.return_value = [("part", "Vè trước trái")]
        mapper = CodebookMapper(codebook_dir=self.root, matcher=matcher)
        result = mapper.map_items([self.item("Cản trước", "material")])[0]
        self.assertEqual(result.matches[0].code, "P1")
        matcher.select_many.assert_not_called()
        result = mapper.map_items([self.item("Công tháo lắp vè trước trái", "labor")])[0]
        self.assertEqual(result.method, "claude")
        self.assertEqual(result.codebook, "codebook.xlsx")
        self.assertEqual([m.code for m in result.matches], ["L1", "L2"])
        self.assertTrue(all(m.item_type == "part" for m in result.matches))
        names = matcher.select_many.call_args.args[1]
        self.assertIn("Vè trước trái", names["part"])
        self.assertIn("Công tháo lắp", names["service"])

    def test_typed_results_do_not_expand_into_other_type(self):
        matcher = mock_matcher()
        matcher.select_many.return_value = [("part", "Cánh cửa"), ("service", "Công tháo lắp"), None]
        mapper = CodebookMapper(codebook_dir=self.root, matcher=matcher)
        results = mapper.map_items([self.item("Công sơn cánh cửa", "paint"),
                                   self.item("Công tháo lắp phục vụ sơn", "labor"),
                                   self.item("Sơn bộ phận không có", "paint")])
        self.assertEqual([[r.code for r in x.matches] for x in results], [["D1"], ["S1"], []])
        self.assertEqual(results[2].method, "unmatched")

    def test_batch_order_and_chunks(self):
        matcher = mock_matcher()
        matcher.select_many.side_effect = lambda items, names: [None] * len(items)
        mapper = CodebookMapper(codebook_dir=self.root, matcher=matcher)
        mapper.BATCH_SIZE = 2
        items = [self.item(f"lạ {i}", "paint") for i in range(5)] + [self.item("Cản trước", "material")]
        results = mapper.map_items(items)
        self.assertEqual([len(c.args[0]) for c in matcher.select_many.call_args_list], [2, 2, 1])
        self.assertEqual([r.raw_name for r in results], [i.raw_name for i in items])
        self.assertEqual(results[-1].method, "exact")

    def test_without_matcher_work_is_unmatched(self):
        result = CodebookMapper(codebook_dir=self.root).map_items([self.item("Công tháo lắp", "labor")])[0]
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
        client = FakeClaude()
        matcher = claude_matcher(client)
        names = {"part": ["Cánh cửa"], "service": ["Công tháo lắp"]}
        items = [self.item("Công sơn cánh cửa", "paint")]
        # cặp (item_type, name) không có trong codebook -> null, không làm hỏng cả batch
        client.replies["mapping"] = message({"selections": [dict(index=0, name="Cánh cửa", item_type="service")]})
        self.assertEqual(matcher.select_many(items, names), [None])
        for selection in [dict(index=0, name="Cánh cửa", item_type=None),
                          dict(index=0, name=None, item_type="part"),
                          dict(index=1, name="Cánh cửa", item_type="part")]:
            client.replies["mapping"] = message({"selections": [selection]})
            with self.assertRaises(ValueError):
                matcher.select_many(items, names)
        client.replies["mapping"] = message({"selections": [dict(index=0, name="Cánh cửa", item_type="part")]})
        self.assertEqual(matcher.select_many(items, names), [("part", "Cánh cửa")])
        self.assertEqual(client.sent_names(), [{"name": n, "item_type": k} for k, ns in names.items() for n in ns])
        self.assertEqual(client.sent_items()[0]["raw_name"], "Công sơn cánh cửa")

    def test_material_cannot_select_service(self):
        client = FakeClaude(mapping=message({"selections": [
            {"index": 0, "name": "Công tháo lắp", "item_type": "service"}]}))
        mapper = CodebookMapper(codebook_dir=self.root, matcher=claude_matcher(client))
        result = mapper.map_items([self.item("phụ tùng lạ", "material")])[0]  # sai loại -> chưa khớp, không lỗi cả job
        self.assertEqual((result.method, result.matches), ("unmatched", []))
        self.assertEqual(mapper.matcher.calls[-1]["invalid_selections"][0]["reason"], "vật tư chỉ được map với item_type part")

    def test_only_common_workbook_is_required(self):
        (self.root / "part_codebook.xlsx").unlink()
        result = CodebookMapper(codebook_dir=self.root).map_items([self.item("Cản trước", "material")])[0]
        self.assertEqual(result.codebook, "codebook.xlsx")
        self.assertEqual(result.matches[0].code, "P1")

    def test_llm_batch_validation(self):
        client = FakeClaude()
        matcher = claude_matcher(client)
        items = [self.item("a", "paint"), self.item("b", "paint")]
        sel = lambda *pairs: message({"selections": [{"index": i, "name": n, "item_type": n and "part"} for i, n in pairs]})
        names = {"part": ["Sơn cản"], "service": []}
        client.replies["mapping"] = sel((0, "invented"), (1, None))
        # tên ngoài codebook -> null cho item đó, không làm hỏng cả batch; vẫn ghi lại trong audit
        self.assertEqual(matcher.select_many(items, names), [None, None])
        self.assertEqual(matcher.calls[-1]["invalid_selections"],
                         [{"index": 0, "item": "a", "returned": ("part", "invented"), "reason": "ngoài codebook"}])
        client.replies["mapping"] = sel((0, "Sơn cản"))
        with self.assertRaises(ValueError):  # thiếu index 1
            matcher.select_many(items, names)
        client.replies["mapping"] = sel((0, "Sơn cản"), (0, None))
        with self.assertRaises(ValueError):  # trùng index, thiếu index 1
            matcher.select_many(items, names)
        client.replies["mapping"] = sel((1, None), (0, "Sơn cản"))  # đảo thứ tự vẫn đúng
        self.assertEqual(matcher.select_many(items, names), [("part", "Sơn cản"), None])
        self.assertEqual(len(client.calls["mapping"]), 4)
        self.assertEqual(len(matcher.calls), 4)
        self.assertEqual(matcher.calls[-1]["items"], ["a", "b"])
        self.assertEqual(matcher.calls[-1]["status"], "success")

    def test_prompt_and_codebook_are_cached_system_blocks(self):
        from mapping.prompt import MAPPING_INSTRUCTION
        client = FakeClaude(mapping=message(NO_MATCH))
        matcher = claude_matcher(client)
        matcher.select_many([self.item("x", "material")], NAMES)
        matcher.select_many([self.item("x", "paint")], {"part": ["Cản"], "service": ["Công sơn"]})
        for call in client.calls["mapping"]:
            instruction, codebook = call["system"]
            self.assertEqual(instruction, {"type": "text", "text": MAPPING_INSTRUCTION})
            # breakpoint cache ở khối codebook: mọi request có cùng codebook đọc lại prefix từ cache
            self.assertEqual(codebook["cache_control"], {"type": "ephemeral", "ttl": "1h"})
            self.assertEqual(set(json.loads(call["messages"][0]["content"][0]["text"])), {"items"})
        self.assertEqual(client.sent_names(0), [{"name": "Cản", "item_type": "part"}])
        self.assertEqual([c["prompt"] for c in matcher.calls], ["mapping", "mapping"])

    @patch.dict("os.environ", {"MAPPING_THINKING_LEVEL": ""})  # .env thật có thể đã được nạp bởi test khác
    def test_mapping_effort_and_fallback_follow_model(self):
        reply = message(NO_MATCH)
        client = FakeClaude(mapping=reply)
        claude_matcher(client, "claude-sonnet-5-5").select_many([self.item("x", "paint")], NAMES)
        sent = client.calls["mapping"][-1]
        self.assertEqual(sent["output_config"]["effort"], "low")  # mặc định mapping
        self.assertEqual((sent["fallbacks"], sent["betas"]), ("default", ["server-side-fallback-2026-07-01"]))
        claude_matcher(client, "claude-haiku-5-5", thinking_level="HIGH").select_many([self.item("x", "paint")], NAMES)
        sent = client.calls["mapping"][-1]
        self.assertEqual(sent["output_config"]["effort"], "high")
        self.assertNotIn("fallbacks", sent)  # Haiku 5.5 không có server-side fallback
        with self.assertRaises(ValueError):
            claude_matcher(client, thinking_level="max")

    def test_refused_mapping_fails_without_caching(self):
        client = FakeClaude(mapping=message("", stop_reason="refusal", stop_details={"category": "cyber"}))
        matcher = claude_matcher(client)
        with self.assertRaisesRegex(RuntimeError, "từ chối"):
            matcher.select_many([self.item("x", "paint")], NAMES)
        self.assertEqual(matcher.calls[-1]["status"], "failed")
        self.assertEqual(matcher.calls[-1]["response"]["stop_reason"], "refusal")

    def test_llm_single_select_and_null(self):
        client = FakeClaude(mapping=message(NO_MATCH))
        matcher = claude_matcher(client)
        self.assertEqual(matcher.select_many([self.item("unknown", "paint")], NAMES), [None])
        self.assertEqual([i["index"] for i in client.sent_items()], [0])

    def ocr(self, client, name="input.md", body=b"Vehicle Repair Estimate", **kwargs):
        source = self.root / name
        source.write_bytes(body)
        model = kwargs.pop("model", MODEL)
        backend = (GeminiBackend if model == GEMINI_MODEL else ClaudeBackend)(client)
        return extract_repair_estimate(source, model=model, backend=backend,
                                       output_dir=self.root / "ocr", verbose=False, **kwargs)

    @patch.dict("os.environ", {"OCR_THINKING_LEVEL": ""})
    def test_ocr_api_preserves_artifacts_and_client(self):
        client = FakeClaude(ocr=message(self.estimate.model_dump(mode="json")))
        self.assertEqual(self.ocr(client), self.estimate)
        self.assertFalse(client.closed)
        self.assertEqual(len(list((self.root / "ocr").rglob("result.json"))), 1)
        sent = client.calls["ocr"][0]
        self.assertEqual(sent["messages"][0]["content"][0], {"type": "text", "text": "Vehicle Repair Estimate"})
        self.assertNotIn("effort", sent["output_config"])  # không đặt OCR_THINKING_LEVEL: mặc định của model

    def test_ocr_invalid_response_preserves_failure(self):
        client = FakeClaude(ocr=message({"invalid": True}))
        with self.assertRaises(ValueError):
            self.ocr(client)
        metadata = json.loads(next((self.root / "ocr").rglob("metadata.json")).read_text())
        self.assertEqual(metadata["status"], "failed")
        self.assertTrue(list((self.root / "ocr").rglob("response.json")))

    def test_ocr_truncated_response_fails(self):
        client = FakeClaude(ocr=message('{"document":', stop_reason="max_tokens"))
        with self.assertRaisesRegex(RuntimeError, "max_tokens"):
            self.ocr(client, thinking_level="high")
        self.assertEqual(client.calls["ocr"][0]["output_config"]["effort"], "high")

    def test_ocr_converts_heic_and_oversized_images_to_jpeg(self):
        client = FakeClaude(ocr=message(self.estimate.model_dump(mode="json")))
        heic = Path(self.temp.name) / "src.heic"
        Image.new("RGB", (40, 30), "white").save(heic)
        self.ocr(client, "photo.heic", heic.read_bytes())
        image = client.calls["ocr"][-1]["messages"][0]["content"][0]
        self.assertEqual((image["type"], image["source"]["media_type"]), ("image", "image/jpeg"))
        png = Path(self.temp.name) / "src.png"
        Image.new("RGB", (8100, 20), "white").save(png)  # vượt 8000px mỗi chiều Claude cho phép
        self.ocr(client, "wide.png", png.read_bytes())
        image = client.calls["ocr"][-1]["messages"][0]["content"][0]
        self.assertEqual(image["source"]["media_type"], "image/jpeg")
        decoded = Image.open(io.BytesIO(base64.b64decode(image["source"]["data"])))
        self.assertEqual(max(decoded.size), 2576)
        small = Path(self.temp.name) / "small-src.png"
        Image.new("RGB", (20, 20), "white").save(small)
        self.ocr(client, "small.png", small.read_bytes())  # ảnh Claude nhận được: gửi nguyên
        image = client.calls["ocr"][-1]["messages"][0]["content"][0]
        self.assertEqual(image["source"]["media_type"], "image/png")

    def test_ocr_large_pdf_goes_through_files_api_and_is_deleted(self):
        client = FakeClaude(ocr=message(self.estimate.model_dump(mode="json")))
        client.files.upload.return_value = SimpleNamespace(id="file_1")
        with patch("llm.claude.INLINE_LIMIT", 4):
            self.ocr(client, "bao_gia.pdf", b"%PDF-1.4 fake")
        document = client.calls["ocr"][0]["messages"][0]["content"][0]
        self.assertEqual(document, {"type": "document", "source": {"type": "file", "file_id": "file_1"}})
        client.files.delete.assert_called_once_with("file_1")
        self.ocr(client, "nho.pdf", b"%PDF-1.4 fake")  # dưới ngưỡng: base64 inline
        document = client.calls["ocr"][-1]["messages"][0]["content"][0]
        self.assertEqual(document["source"]["type"], "base64")
        self.assertEqual(client.files.upload.call_count, 1)

    # ---- Provider Gemini: cùng LlmMatcher/extractor, request đi qua generate_content / interactions ----

    def test_gemini_mapping_sends_codebook_and_items_and_marks_method(self):
        from mapping.prompt import MAPPING_INSTRUCTION
        client = Mock()
        client.models.generate_content.return_value = response({"selections": [
            {"index": 0, "name": "Vè trước trái", "item_type": "part"}]})
        mapper = CodebookMapper(codebook_dir=self.root, matcher=gemini_matcher(client, thinking_level="medium"))
        result = mapper.map_items([self.item("Công tháo lắp vè trước trái", "labor")])[0]
        self.assertEqual((result.method, [m.code for m in result.matches]), ("gemini", ["L1", "L2"]))
        sent = sent_contents(client)
        self.assertEqual(set(sent), {"names", "items"})  # không có context cache: gửi kèm codebook
        self.assertEqual(sent["items"][0]["raw_name"], "Công tháo lắp vè trước trái")
        config = client.models.generate_content.call_args.kwargs["config"]
        self.assertEqual(config.system_instruction, MAPPING_INSTRUCTION)
        self.assertEqual(config.thinking_config.thinking_level, "MEDIUM")
        self.assertEqual(mapper.matcher.calls[-1]["provider"], "gemini")

    def test_gemini_batch_validation_and_blocked_response(self):
        client = Mock()
        matcher = gemini_matcher(client)
        items = [self.item("a", "paint"), self.item("b", "paint")]
        sel = lambda *pairs, **kw: response(
            {"selections": [{"index": i, "name": n, "item_type": n and "part"} for i, n in pairs]}, **kw)
        names = {"part": ["Sơn cản"], "service": []}
        client.models.generate_content.return_value = sel((0, "invented"), (1, None))
        self.assertEqual(matcher.select_many(items, names), [None, None])
        self.assertEqual(matcher.calls[-1]["invalid_selections"][0]["reason"], "ngoài codebook")
        client.models.generate_content.return_value = sel((0, "Sơn cản"))
        with self.assertRaises(ValueError):  # thiếu index 1
            matcher.select_many(items, names)
        client.models.generate_content.return_value = sel((0, "Sơn cản"), (1, None), finish_reason="SAFETY")
        with self.assertRaisesRegex(RuntimeError, "SAFETY"):
            matcher.select_many(items, names)
        self.assertEqual(matcher.calls[-1]["status"], "failed")

    def test_gemini_ocr_uses_interactions_and_preserves_artifacts(self):
        client = Mock()
        client.interactions.create.return_value = response(self.estimate.model_dump(mode="json"))
        self.assertEqual(self.ocr(client, model=GEMINI_MODEL, thinking_level="low"), self.estimate)
        client.close.assert_not_called()
        sent = client.interactions.create.call_args.kwargs
        self.assertEqual(sent["input"][1], {"type": "text", "text": "Vehicle Repair Estimate"})
        self.assertEqual(sent["generation_config"], {"thinking_level": "low"})
        self.assertFalse(sent["store"])
        metadata = json.loads(next((self.root / "ocr").rglob("metadata.json")).read_text())
        self.assertEqual((metadata["provider"], metadata["endpoint"]), ("gemini", "/v1beta/interactions"))

    def test_gemini_ocr_incomplete_interaction_fails_with_artifacts(self):
        client = Mock()
        client.interactions.create.return_value = response(self.estimate.model_dump(mode="json"), status="incomplete")
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            self.ocr(client, model=GEMINI_MODEL)
        self.assertTrue(list((self.root / "ocr").rglob("response.json")))

    def test_gemini_large_file_is_uploaded_then_deleted(self):
        client = Mock()
        client.interactions.create.return_value = response(self.estimate.model_dump(mode="json"))
        client.files.upload.return_value = SimpleNamespace(name="files/1", state="ACTIVE", uri="https://files/1")
        with patch("llm.gemini.INLINE_LIMIT", 4):
            self.ocr(client, "bao_gia.pdf", b"%PDF-1.4 fake", model=GEMINI_MODEL)
        media = client.interactions.create.call_args.kwargs["input"][1]
        self.assertEqual(media, {"type": "document", "mime_type": "application/pdf", "uri": "https://files/1"})
        client.files.delete.assert_called_once_with(name="files/1")

    def test_legacy_estimate_number_is_read_and_written_with_new_name(self):
        payload = self.estimate.model_dump(mode="json")
        payload["document"]["quotation_number"] = payload["document"].pop("estimate_number")
        parsed = RepairEstimate.model_validate(payload)
        document = parsed.model_dump(mode="json")["document"]
        self.assertIn("estimate_number", document)
        self.assertNotIn("quotation_number", document)


if __name__ == "__main__":
    unittest.main()
