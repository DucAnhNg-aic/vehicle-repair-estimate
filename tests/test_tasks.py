import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from repair_estimate_api import tasks


class ProcessJobTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.upload = root / "abc123.jpg"
        self.upload.write_bytes(b"image")
        self.run_dir = root / "run-1"
        self.run_dir.mkdir()
        (self.run_dir / "mapping.json").write_text(json.dumps({"document": {}, "items": []}))
        self.repository = MagicMock()
        self.repository.__enter__.return_value = self.repository
        self.repository.save_run.return_value = {"run_id": "run-1"}
        self.store_dir = root / "input_files"
        self.job = SimpleNamespace(id="abc123", save_meta=lambda: None, meta={
            "channel": "partner", "client": "acme", "reference_id": "CLAIM-1", "file_name": "bao gia.jpg"})
        summary = {"run_dir": str(self.run_dir), "steps": {"ocr": {}, "mapping": {"methods": {}}}}
        self.pipeline = MagicMock(return_value=summary)
        for target, value in (
            ("load_env", MagicMock()), ("MongoSettings", MagicMock()), ("TwoTierCache", MagicMock()),
            ("RepairEstimateRepository", MagicMock(return_value=self.repository)), ("genai", MagicMock()),
            ("run_repair_estimate_pipeline", self.pipeline),
            ("new_run_dir", MagicMock(return_value=self.run_dir)),
            ("get_current_job", MagicMock(return_value=self.job)),
        ):
            patcher = patch.object(tasks, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = patch.dict("os.environ", {"GEMINI_API_KEY": "test"})
        env.start()
        self.addCleanup(env.stop)

    def run_job(self, save_mongodb=True):
        options = {"save_mongodb": save_mongodb, "use_cache": False, "timeout_seconds": 10,
                   "ocr_model": "m", "mapping_model": "m"}
        return tasks.process_repair_estimate_job(str(self.upload), options, {
            "env_file": "/unused/.env", "output_dir": "/unused", "codebook_dir": "/unused",
            "input_store_dir": str(self.store_dir)})

    def test_saves_origin_and_original_file_name_then_removes_upload(self):
        result = self.run_job()
        self.assertEqual(result["storage"], {"saved": True, "run_id": "run-1"})
        self.repository.save_run.assert_called_once_with(
            str(self.run_dir), file_name="bao gia.jpg",
            source={"channel": "partner", "client": "acme", "reference_id": "CLAIM-1", "job_id": "abc123"})
        self.assertFalse(self.upload.exists())

    def test_input_file_is_kept_by_hash_and_recorded(self):
        self.run_job()
        stored = self.repository.record_input_file.call_args.args[0]
        self.assertEqual(self.repository.record_input_file.call_args.kwargs, {"file_name": "bao gia.jpg"})
        self.assertEqual((self.store_dir / stored.relative_path).read_bytes(), b"image")
        self.assertEqual(self.pipeline.call_args.kwargs["run_dir"], self.run_dir)

    def test_input_file_is_not_kept_without_mongodb(self):
        self.run_job(save_mongodb=False)
        self.assertFalse(self.store_dir.exists())
        self.repository.record_input_file.assert_not_called()

    def test_failed_run_is_saved_then_error_is_raised(self):
        (self.run_dir / "summary.json").write_text("{}")
        self.pipeline.side_effect = RuntimeError("Gemini timeout")
        with self.assertRaisesRegex(RuntimeError, "Gemini timeout"):
            self.run_job()
        self.repository.save_run.assert_called_once()
        self.assertEqual(self.repository.save_run.call_args.args, (self.run_dir,))
        self.assertTrue(self.repository.record_input_file.called)  # file gốc vẫn được giữ
        self.assertFalse(self.upload.exists())

    def test_storage_error_does_not_hide_pipeline_error(self):
        (self.run_dir / "summary.json").write_text("{}")
        self.pipeline.side_effect = RuntimeError("Gemini timeout")
        self.repository.save_run.side_effect = ValueError("mongo down")
        with self.assertRaisesRegex(RuntimeError, "Gemini timeout"), self.assertLogs(tasks.logger, "ERROR"):
            self.run_job()


if __name__ == "__main__":
    unittest.main()
