import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient

from repair_estimate_api.app import create_app
from repair_estimate_api.config import ApiSettings, _partner_api_keys
from test_repair_estimate_api import FakeQueue

KEY = "k" * 32
OTHER_KEY = "o" * 32
RESULT = {
    "job_id": "x", "status": "success",
    "document": {"estimate_number": "1889", "date": None, "vehicle_plate": "50E26280", "vehicle_model": None,
                 "vin": None},
    "items": [{  # đúng như output worker: không có "index"
        "raw_section": None, "section_type": "service", "raw_code": None,
        "raw_name": "Sơn cửa trước Trái", "operation": "paint", "vehicle_part": "cửa trước trái",
        "item_name": "Cửa trước trái", "category": "paint", "unit": "Lần", "quantity": 1.0,
        "unit_price": 500000.0, "amount": 500000.0, "source_evidence": "Sơn cửa trước Trái",
        "mapping": {"raw_name": "Sơn cửa trước Trái", "category": "paint", "codebook": "codebook.xlsx",
                    "method": "gemini",
                    "matches": [{"code": "AIC.000079", "name": "Cánh cửa", "row": 80, "item_type": "part"}]},
    }],
    "metrics": {"total": {"duration_seconds": 50, "cost": {"total_usd": 0.01}}},
    "storage": {"saved": True, "run_id": "run-1"},
}


class PartnerApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.queue = FakeQueue()
        self.settings = ApiSettings(
            env_file=root / ".env", redis_url="redis://unused", default_model="gemini-3.8-flash",
            allowed_models=frozenset({"gemini-3.8-flash"}), queue_name="test",
            upload_dir=root / "uploads", output_dir=root / "outputs", codebook_dir=root,
            max_upload_bytes=20, result_ttl_seconds=60, job_timeout_seconds=60,
            partner_api_keys={KEY: "acme", OTHER_KEY: "other"}, partner_max_wait_seconds=1,
        )
        for patcher in (patch("repair_estimate_api.app.get_queue", return_value=self.queue),
                        patch("repair_estimate_api.partner.POLL_INTERVAL_SECONDS", 0.01)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = AsyncClient(transport=ASGITransport(app=create_app(self.settings)), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()

    def finish_on_enqueue(self, status, result=None):
        enqueue = self.queue.enqueue

        def wrapped(*args, **kwargs):
            job = enqueue(*args, **kwargs)
            job._status, job.result = status, result
            return job
        self.queue.enqueue = wrapped

    async def submit(self, key=KEY, **data):
        headers = {"X-API-Key": key} if key else {}
        return await self.client.post("/partner/v1/estimates", headers=headers,
                                      files={"file": ("bg.jpg", b"image", "image/jpeg")}, data=data)

    async def test_requires_valid_api_key(self):
        for key in (None, "wrong"):
            response = await self.submit(key=key)
            self.assertEqual((response.status_code, response.json()["detail"]["code"]), (401, "INVALID_API_KEY"))
        self.assertIsNone(self.queue.enqueued)

    async def test_disabled_without_configured_keys(self):
        app = create_app(ApiSettings(**{**self.settings.__dict__, "partner_api_keys": {}}))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/partner/v1/estimates/x", headers={"X-API-Key": KEY})
        self.assertEqual(response.json()["detail"]["code"], "PARTNER_API_DISABLED")

    async def test_returns_result_in_one_call_with_public_contract_only(self):
        self.finish_on_enqueue("finished", RESULT)
        response = await self.submit(reference_id="CLAIM-1")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual((body["status"], body["reference_id"]), ("success", "CLAIM-1"))
        self.assertEqual(body["status_url"], f"/partner/v1/estimates/{body['job_id']}")
        self.assertNotIn("metrics", body["result"])  # không lộ chi phí/model
        self.assertEqual(body["result"]["items"][0]["index"], 0)
        mapping = body["result"]["items"][0]["mapping"]
        self.assertEqual(mapping, {"method": "ai", "matches": [
            {"code": "AIC.000079", "name": "Cánh cửa", "item_type": "part"}]})
        options, meta = self.queue.enqueued[2], self.queue.enqueued[4]["meta"]
        self.assertEqual((options["ocr_model"], meta["channel"], meta["client"], meta["reference_id"]),
                         ("gemini-3.8-flash", "partner", "acme", "CLAIM-1"))
        self.assertEqual(meta["file_name"], "bg.jpg")

    async def test_returns_202_when_not_done_and_job_can_be_polled(self):
        response = await self.submit(wait_seconds="0")
        self.assertEqual((response.status_code, response.json()["status"]), (202, "queued"))
        job_id = response.json()["job_id"]
        job = self.queue.jobs[job_id]
        job._status, job.result = "finished", RESULT
        polled = await self.client.get(response.json()["status_url"], headers={"X-API-Key": KEY})
        self.assertEqual((polled.status_code, polled.json()["status"]), (200, "success"))
        other = await self.client.get(f"/partner/v1/estimates/{job_id}", headers={"X-API-Key": OTHER_KEY})
        self.assertEqual(other.status_code, 404)  # khách khác không đọc được job

    async def test_waits_up_to_max_then_202(self):
        response = await self.submit(wait_seconds="999")  # bị hạ về partner_max_wait_seconds=1
        self.assertEqual(response.status_code, 202)

    async def test_failed_job(self):
        self.finish_on_enqueue("failed")
        response = await self.submit()
        self.assertEqual(response.status_code, 500)
        self.assertEqual((response.json()["status"], response.json()["error"]["code"]),
                         ("failed", "PROCESSING_FAILED"))

    async def test_validation_errors_use_code_message_format(self):
        response = await self.submit(wait_seconds="-1")
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (422, "INVALID_REQUEST"))
        unsupported = await self.client.post("/partner/v1/estimates", headers={"X-API-Key": KEY},
                                             files={"file": ("bg.exe", b"x")})
        self.assertEqual(unsupported.status_code, 415)

    async def test_demo_jobs_are_not_visible_to_partner(self):
        created = await self.client.post("/api/v1/repair-estimate/jobs", files={"file": ("e.jpg", b"img")})
        response = await self.client.get(f"/partner/v1/estimates/{created.json()['job_id']}",
                                         headers={"X-API-Key": KEY})
        self.assertEqual(response.status_code, 404)

    async def test_swagger_is_served(self):
        spec = (await self.client.get("/partner/openapi.json")).json()
        self.assertEqual(set(spec["paths"]), {"/v1/estimates", "/v1/estimates/{job_id}"})
        self.assertIn("X-API-Key", str(spec["components"]["securitySchemes"]))
        self.assertEqual((await self.client.get("/partner/docs")).status_code, 200)


class PartnerKeyConfigTests(unittest.TestCase):
    def test_parses_and_validates_keys(self):
        self.assertEqual(_partner_api_keys(f"acme:{KEY}, other:{OTHER_KEY}"), {KEY: "acme", OTHER_KEY: "other"})
        self.assertEqual(_partner_api_keys(""), {})
        for raw in ("acme", "acme:short", f":{KEY}", f"a:{KEY},b:{KEY}"):
            with self.assertRaises(ValueError):
                _partner_api_keys(raw)


if __name__ == "__main__":
    unittest.main()
