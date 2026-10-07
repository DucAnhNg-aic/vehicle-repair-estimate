import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from httpx import ASGITransport, AsyncClient

from repair_estimate_api.app import create_app
from repair_estimate_api.config import ApiSettings


class FakeJob:
    def __init__(self, job_id, status="queued", result=None, meta=None):
        self.id, self._status, self.result = job_id, status, result
        self.meta = meta or {"step": status}
        self.created_at = datetime.now(timezone.utc)
        self.started_at = self.ended_at = None

    def get_status(self, refresh=True):
        return self._status


class FakeQueue:
    def __init__(self):
        self.jobs = {}
        self.connection = SimpleNamespace(ping=lambda: True)
        self.enqueued = None

    def enqueue(self, task, file_path, options, server, **kwargs):
        self.enqueued = (task, file_path, options, server, kwargs)
        job = FakeJob(kwargs["job_id"], meta=kwargs["meta"])
        self.jobs[job.id] = job
        return job

    def fetch_job(self, job_id):
        return self.jobs.get(job_id)


class RepairEstimateApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.queue = FakeQueue()
        self.settings = ApiSettings(
            env_file=self.root / ".env", redis_url="redis://unused", default_model="gemini-3.8-flash",
            allowed_models=frozenset({"gemini-3.8-flash"}), api_key="secret", queue_name="test",
            upload_dir=self.root / "uploads", output_dir=self.root / "outputs", codebook_dir=self.root,
            max_upload_bytes=20, result_ttl_seconds=60, job_timeout_seconds=60,
        )
        patcher = patch("repair_estimate_api.app.get_queue", return_value=self.queue)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = AsyncClient(transport=ASGITransport(app=create_app(self.settings)), base_url="http://test")
        self.headers = {"X-API-Key": "secret"}

    async def asyncTearDown(self):
        await self.client.aclose()

    async def submit(self, options=None, content=b"image"):
        return await self.client.post(
            "/api/v1/repair-estimate/jobs", headers=self.headers,
            files={"file": ("estimate.jpg", content, "image/jpeg")},
            data={"options": json.dumps(options or {})},
        )

    async def test_requires_api_key(self):
        response = await self.client.post(
            "/api/v1/repair-estimate/jobs",
            files={"file": ("estimate.jpg", b"image", "image/jpeg")}, data={"options": "{}"},
        )
        self.assertEqual(response.status_code, 401)

    async def test_enqueue_validates_and_resolves_options(self):
        response = await self.submit({"use_cache": False, "save_mongodb": False})
        self.assertEqual(response.status_code, 202)
        task, file_path, options, server, kwargs = self.queue.enqueued
        self.assertEqual(task, "repair_estimate_api.tasks.process_repair_estimate_job")
        self.assertTrue(Path(file_path).is_file())
        self.assertEqual(options["ocr_model"], "gemini-3.8-flash")
        self.assertEqual(options["mapping_model"], "gemini-3.8-flash")
        self.assertFalse(options["use_cache"])
        self.assertNotIn("env_file", options)
        self.assertEqual(server["env_file"], str(self.settings.env_file))
        self.assertEqual(kwargs["meta"]["sha256"], "6105d6cc76af400325e94d588ce511be5bfdbb73b437dc51eca43917d7a43e3d")

    async def test_rejects_unknown_option_and_model(self):
        self.assertEqual((await self.submit({"output_dir": "/tmp"})).status_code, 422)
        self.assertEqual((await self.submit({"ocr_model": "other"})).status_code, 422)

    async def test_rejects_unsupported_empty_and_large_files(self):
        unsupported = await self.client.post(
            "/api/v1/repair-estimate/jobs", headers=self.headers,
            files={"file": ("estimate.exe", b"x")}, data={"options": "{}"},
        )
        self.assertEqual(unsupported.status_code, 415)
        self.assertEqual((await self.submit(content=b"")).status_code, 422)
        self.assertEqual((await self.submit(content=b"x" * 21)).status_code, 413)
        self.assertEqual(list(self.settings.upload_dir.iterdir()), [])

    async def test_queue_failure_returns_503_and_removes_upload(self):
        self.queue.enqueue = Mock(side_effect=ConnectionError("redis down"))
        response = await self.submit()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "QUEUE_UNAVAILABLE")
        self.assertEqual(list(self.settings.upload_dir.iterdir()), [])

    async def test_status_and_result_contract(self):
        created = (await self.submit()).json()
        job = self.queue.jobs[created["job_id"]]
        self.assertEqual((await self.client.get(created["status_url"], headers=self.headers)).json()["status"], "queued")
        self.assertEqual((await self.client.get(created["result_url"], headers=self.headers)).status_code, 409)
        job._status = "finished"
        job.result = {
            "job_id": job.id,
            "status": "success",
            "document": {},
            "items": [],
            "metrics": {
                "total": {"duration_seconds": 0.1, "cost": {}},
                "ocr": {"duration_seconds": 0.03, "cost": None, "cache_hit": True},
                "mapping": {
                    "duration_seconds": 0.07, "cost": {}, "item_count": 1,
                    "exact_count": 0, "gemini_count": 1, "unmatched_count": 0,
                    "cache_hits": 1, "cache_misses": 0, "gemini_calls": 0,
                },
            },
            "storage": {"saved": True, "run_id": "run-1"},
        }
        self.assertEqual((await self.client.get(created["result_url"], headers=self.headers)).json(), job.result)


if __name__ == "__main__":
    unittest.main()
