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
            allowed_models=frozenset({"gemini-3.8-flash"}), queue_name="test",
            upload_dir=self.root / "uploads", output_dir=self.root / "outputs", codebook_dir=self.root,
            max_upload_bytes=20, result_ttl_seconds=60, job_timeout_seconds=60,
        )
        patcher = patch("repair_estimate_api.app.get_queue", return_value=self.queue)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = AsyncClient(transport=ASGITransport(app=create_app(self.settings)), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def submit(self, options=None, content=b"image"):
        return await self.client.post(
            "/api/v1/repair-estimate/jobs",
            files={"file": ("estimate.jpg", content, "image/jpeg")},
            data={"options": json.dumps(options or {})},
        )

    async def test_accepts_request_without_api_key(self):
        response = await self.client.post(
            "/api/v1/repair-estimate/jobs",
            files={"file": ("estimate.jpg", b"image", "image/jpeg")}, data={"options": "{}"},
        )
        self.assertEqual(response.status_code, 202)

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

    async def test_thinking_levels_are_forwarded_and_validated(self):
        response = await self.submit({"ocr_thinking_level": "high", "mapping_thinking_level": "low"})
        self.assertEqual(response.status_code, 202)
        options = self.queue.enqueued[2]
        self.assertEqual((options["ocr_thinking_level"], options["mapping_thinking_level"]), ("high", "low"))
        self.assertEqual((await self.submit({"ocr_thinking_level": "extreme"})).status_code, 422)
        self.assertEqual((await self.submit({"mapping_thinking_level": "minimal"})).status_code, 422)

    async def test_demo_config_lists_models_and_levels(self):
        data = (await self.client.get("/api/v1/repair-estimate/demo-config")).json()
        self.assertEqual(data["models"], ["gemini-3.8-flash"])
        self.assertEqual(data["thinking_levels"], ["low", "medium", "high"])
        self.assertTrue(data["show_ai_options"])

    def fake_history(self, rows=None, run=None):
        class Repo:
            def __init__(inner, *args, **kwargs): pass
            def list_history(inner, **kwargs):
                self.history_kwargs = kwargs
                return rows or []
            def get_run(inner, run_id): return run
        patch_repo = patch("repair_estimate_api.app.RepairEstimateRepository", Repo)
        patch_settings = patch("repair_estimate_api.app.MongoSettings")
        patch_repo.start(); patch_settings.start()
        self.addCleanup(patch_repo.stop); self.addCleanup(patch_settings.stop)

    async def test_history_list_returns_utc_summaries(self):
        from datetime import datetime
        self.fake_history(rows=[{
            "run_id": "r1", "created_at": datetime(2026, 10, 8, 6, 0, 0), "status": "success",
            "input": {"file_name": "a.jpg"}, "document": {"vehicle_plate": "30A-1", "estimate_number": "BG1"},
            "models": {"ocr": "m"},
            "metrics": {"total": {"duration_seconds": 5.5, "cost": {"total_usd": 0.07}}, "ocr": {"cache_hit": False},
                        "mapping": {"item_count": 3, "exact_count": 1, "gemini_count": 1, "unmatched_count": 1}},
        }])
        response = await self.client.get("/api/v1/repair-estimate/runs?limit=5&q=30A")
        self.assertEqual(response.status_code, 200)
        run = response.json()["runs"][0]
        self.assertTrue(run["created_at"].endswith("Z"), run["created_at"])  # UTC rõ ràng để UI đổi sang giờ VN
        self.assertEqual((run["vehicle_plate"], run["item_count"], run["cost_usd"]), ("30A-1", 3, 0.07))
        self.assertEqual(self.history_kwargs["search"], "30A")
        self.assertIsNone(response.json()["next_before"])  # ít hơn limit: hết trang
        self.assertEqual((await self.client.get("/api/v1/repair-estimate/runs?limit=0")).status_code, 422)

    async def test_history_detail_and_not_found(self):
        from datetime import datetime
        self.fake_history(run={"run_id": "r1", "created_at": datetime(2026, 10, 8, 6, 0, 0), "status": "success",
                               "document": {}, "items": [], "metrics": {}})
        response = await self.client.get("/api/v1/repair-estimate/runs/r1")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["created_at"].endswith("Z"))

    async def test_history_detail_not_found(self):
        self.fake_history(run=None)
        response = await self.client.get("/api/v1/repair-estimate/runs/zzz")
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (404, "RUN_NOT_FOUND"))

    async def test_rejects_unknown_option_and_model(self):
        self.assertEqual((await self.submit({"output_dir": "/tmp"})).status_code, 422)
        self.assertEqual((await self.submit({"ocr_model": "other"})).status_code, 422)

    async def test_rejects_unsupported_empty_and_large_files(self):
        unsupported = await self.client.post(
            "/api/v1/repair-estimate/jobs",
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
        self.assertEqual((await self.client.get(created["status_url"])).json()["status"], "queued")
        self.assertEqual((await self.client.get(created["result_url"])).status_code, 409)
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
                    "cache_hits": 1, "cache_misses": 0, "gemini_calls": 0, "context_cache": [],
                },
            },
            "storage": {"saved": True, "run_id": "run-1"},
        }
        self.assertEqual((await self.client.get(created["result_url"])).json(), job.result)


if __name__ == "__main__":
    unittest.main()
