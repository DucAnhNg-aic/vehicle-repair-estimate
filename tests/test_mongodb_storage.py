import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

from storage import MongoSettings, RepairEstimateRepository, StoredInputFile, build_run_document


class MongoStorageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.run_dir = Path(temp.name) / "20261006T010203_000000Z"
        self.run_dir.mkdir()
        (self.run_dir / "summary.json").write_text(json.dumps({
            "status": "success",
            "input": "/data/estimate.jpg",
            "started_at": "2026-10-06T01:02:03+00:00",
            "models": {"ocr": "test", "mapping": "test"},
            "total": {"seconds": 4.2, "cost": {"total_usd": 0.01}},
            "steps": {
                "ocr": {"seconds": 3, "cost": {"total_usd": 0.007}},
                "mapping": {"seconds": 1.2, "cost": {"total_usd": 0.003}},
            },
            "error": None,
        }))
        (self.run_dir / "mapping.json").write_text(json.dumps({
            "document": {"estimate_number": "E-01", "vehicle_plate": "30A12345"},
            "items": [
                {"raw_name": "Sơn cản", "mapping": {"method": "exact", "matches": [{"code": "S1"}]}},
                {"raw_name": "Mục lạ", "mapping": {"method": "unmatched", "matches": []}},
            ],
        }))

    def test_build_run_document(self):
        result = build_run_document(self.run_dir)
        self.assertEqual(result["run_id"], self.run_dir.name)
        self.assertIsInstance(result["created_at"], datetime)
        self.assertEqual(result["updated_by"], "gemini")
        self.assertNotIn("review_required", result)
        self.assertNotIn("mapping_metrics", result)
        self.assertEqual(result["metrics"]["total"], {
            "duration_seconds": 4.2, "cost": {"total_usd": 0.01}})
        self.assertEqual(result["metrics"]["ocr"]["duration_seconds"], 3)
        self.assertEqual(result["metrics"]["ocr"]["cost"]["total_usd"], 0.007)
        self.assertEqual(result["metrics"]["mapping"]["duration_seconds"], 1.2)
        self.assertEqual(result["metrics"]["mapping"]["cost"]["total_usd"], 0.003)
        self.assertEqual(result["metrics"]["mapping"]["item_count"], 2)
        self.assertEqual([item["index"] for item in result["items"]], [0, 1])

    def test_sha256_survives_missing_uploaded_file(self):
        path = self.run_dir / "summary.json"
        payload = json.loads(path.read_text())
        payload["input_sha256"] = "a" * 64
        path.write_text(json.dumps(payload))
        self.assertEqual(build_run_document(self.run_dir)["input"]["sha256"], "a" * 64)

    def test_source_and_original_file_name_are_stored(self):
        source = {"channel": "partner", "client": "acme", "reference_id": "CLAIM-1", "job_id": "j1"}
        result = build_run_document(self.run_dir, source=source, file_name="bao gia.jpg")
        self.assertEqual(result["source"], source)
        self.assertEqual(result["input"]["file_name"], "bao gia.jpg")
        cli = build_run_document(self.run_dir)  # CLI: không có nguồn, tên file lấy từ đường dẫn
        self.assertIsNone(cli["source"])
        self.assertEqual(cli["input"]["file_name"], "estimate.jpg")

    def test_legacy_run_without_digest_remains_readable(self):
        self.assertIsNone(build_run_document(self.run_dir)["input"]["sha256"])

    def test_file_grouping_index_is_not_unique(self):
        client = MagicMock()
        repo = RepairEstimateRepository(MongoSettings("mongodb://unused"), client=client)
        repo.ensure_indexes()
        calls = repo.collection.create_index.call_args_list
        digest_call = next(c for c in calls if c.kwargs.get("name") == "input_sha256")
        self.assertEqual(digest_call.args[0], [("input.sha256", 1)])
        self.assertFalse(digest_call.kwargs.get("unique", False))

    def test_legacy_number_is_normalized_for_mongodb(self):
        path = self.run_dir / "mapping.json"
        payload = json.loads(path.read_text())
        payload["document"]["quotation_number"] = payload["document"].pop("estimate_number")
        path.write_text(json.dumps(payload))
        document = build_run_document(self.run_dir)["document"]
        self.assertEqual(document["estimate_number"], "E-01")
        self.assertNotIn("quotation_number", document)

    def test_save_is_idempotent_upsert(self):
        collection = Mock()
        collection.replace_one.return_value = SimpleNamespace(
            upserted_id="new-id", matched_count=0, modified_count=0
        )
        client = MagicMock()
        client.__getitem__.return_value.__getitem__.return_value = collection
        repository = RepairEstimateRepository(MongoSettings("mongodb://unused"), client=client)
        result = repository.save_run(self.run_dir)
        query, document = collection.replace_one.call_args.args
        self.assertEqual(query, {"run_id": self.run_dir.name})
        self.assertEqual(document["document"]["estimate_number"], "E-01")
        self.assertTrue(collection.replace_one.call_args.kwargs["upsert"])
        self.assertTrue(result["created"])
        repository.close()
        client.close.assert_not_called()

    def test_failed_run_without_mapping_is_stored_with_error(self):
        (self.run_dir / "mapping.json").unlink()
        path = self.run_dir / "summary.json"
        payload = json.loads(path.read_text())
        payload.update(status="failed", error="RuntimeError: Gemini timeout", input_sha256="b" * 64)
        path.write_text(json.dumps(payload))
        result = build_run_document(self.run_dir)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "RuntimeError: Gemini timeout")
        self.assertEqual(result["input"]["sha256"], "b" * 64)
        self.assertEqual((result["document"], result["items"]), ({}, []))

    def test_successful_run_still_requires_mapping(self):
        (self.run_dir / "mapping.json").unlink()
        with self.assertRaisesRegex(ValueError, "mapping.json"):
            build_run_document(self.run_dir)

    def test_record_input_file_upserts_by_sha256(self):
        client = MagicMock()
        repository = RepairEstimateRepository(MongoSettings("mongodb://unused"), client=client)
        stored = StoredInputFile(sha256="c" * 64, size=5, relative_path=f"cc/{'c' * 64}", created=False)
        repository.record_input_file(stored, file_name="Bao Gia.JPG")
        query, update = repository.input_files.update_one.call_args.args
        self.assertEqual(query, {"_id": "c" * 64})
        self.assertTrue(repository.input_files.update_one.call_args.kwargs["upsert"])
        self.assertEqual(update["$inc"], {"run_count": 1})
        self.assertEqual(update["$addToSet"], {"original_names": "Bao Gia.JPG"})
        self.assertEqual(update["$setOnInsert"]["ext"], ".jpg")
        self.assertEqual(update["$setOnInsert"]["mime_type"], "image/jpeg")
        self.assertEqual(update["$setOnInsert"]["storage_path"], f"cc/{'c' * 64}")
        self.assertIn("last_seen_at", update["$set"])

    def test_invalid_mapping_method_is_rejected(self):
        path = self.run_dir / "mapping.json"
        payload = json.loads(path.read_text())
        payload["items"][0]["mapping"]["method"] = "wrong"
        path.write_text(json.dumps(payload))
        with self.assertRaisesRegex(ValueError, "mapping.method"):
            build_run_document(self.run_dir)


if __name__ == "__main__":
    unittest.main()
