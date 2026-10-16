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
        self.job = SimpleNamespace(id="abc123", save_meta=lambda: None, meta={
            "channel": "partner", "client": "acme", "reference_id": "CLAIM-1", "file_name": "bao gia.jpg"})
        summary = {"run_dir": str(self.run_dir), "steps": {"ocr": {}, "mapping": {"methods": {}}}}
        for target, value in (
            ("load_env", MagicMock()), ("MongoSettings", MagicMock()), ("TwoTierCache", MagicMock()),
            ("RepairEstimateRepository", MagicMock(return_value=self.repository)), ("genai", MagicMock()),
            ("run_repair_estimate_pipeline", MagicMock(return_value=summary)),
            ("get_current_job", MagicMock(return_value=self.job)),
        ):
            patcher = patch.object(tasks, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = patch.dict("os.environ", {"GEMINI_API_KEY": "test"})
        env.start()
        self.addCleanup(env.stop)

    def run_job(self):
        options = {"save_mongodb": True, "use_cache": False, "timeout_seconds": 10,
                   "ocr_model": "m", "mapping_model": "m"}
        return tasks.process_repair_estimate_job(str(self.upload), options, {
            "env_file": "/unused/.env", "output_dir": "/unused", "codebook_dir": "/unused"})

    def test_saves_origin_and_original_file_name_then_removes_upload(self):
        result = self.run_job()
        self.assertEqual(result["storage"], {"saved": True, "run_id": "run-1"})
        self.repository.save_run.assert_called_once_with(
            str(self.run_dir), file_name="bao gia.jpg",
            source={"channel": "partner", "client": "acme", "reference_id": "CLAIM-1", "job_id": "abc123"})
        self.assertFalse(self.upload.exists())


if __name__ == "__main__":
    unittest.main()
