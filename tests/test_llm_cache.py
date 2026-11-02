import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from openpyxl import Workbook

from claude_fake import MODEL, FakeClaude, message
from llm_cache import TwoTierCache, mapping_context, mapping_key, ocr_key
from llm import LlmClients
from llm.claude import Backend as ClaudeBackend
from mapping import LlmMatcher
from ocr import RepairEstimate, RepairEstimateItem, extract_repair_estimate
from ocr.schema import Document
from repair_estimate_pipeline import run_repair_estimate_pipeline

def estimate(*items):
    rows = []
    for raw_name, category in items:
        row = {name: None for name in RepairEstimateItem.model_fields}
        row.update(raw_name=raw_name, section_type="service", item_name=raw_name,
                   category=category, source_evidence=raw_name)
        rows.append(row)
    return RepairEstimate.model_validate({"document": {name: None for name in Document.model_fields}, "items": rows})


class MemoryCache:
    def __init__(self):
        self.data = {}
        self.sets = 0

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value):
        self.sets += 1
        self.data[key] = value


class FakeRedis:
    def __init__(self, fail=False):
        self.data, self.fail, self.ttls = {}, fail, {}

    def get(self, key):
        if self.fail:
            raise ConnectionError("redis down")
        return self.data.get(key)

    def set(self, key, value, ex=None):
        if self.fail:
            raise ConnectionError("redis down")
        self.data[key], self.ttls[key] = value, ex


class FakeCollection:
    def __init__(self, fail=False):
        self.docs, self.fail = {}, fail

    def find_one(self, query):
        if self.fail:
            raise TimeoutError("mongo down")
        return self.docs.get(query["_id"])

    def replace_one(self, query, doc, upsert=False):
        if self.fail:
            raise TimeoutError("mongo down")
        self.docs[query["_id"]] = doc


class TwoTierCacheTests(unittest.TestCase):
    def make(self, redis=None, collection=None):
        self.warnings = []
        return TwoTierCache(redis, collection, ttl_seconds=60, warn=self.warnings.append)

    def test_set_writes_both_tiers_and_redis_serves_reads(self):
        redis, mongo = FakeRedis(), FakeCollection()
        cache = self.make(redis, mongo)
        cache.set("k", {"name": "A"})
        self.assertEqual(redis.ttls["vre:llm:k"], 60)
        self.assertIn("k", mongo.docs)
        mongo.docs.clear()  # chứng minh lần đọc này đến từ Redis
        self.assertEqual(cache.get("k"), {"name": "A"})

    def test_mongo_hit_is_promoted_to_redis(self):
        redis, mongo = FakeRedis(), FakeCollection()
        cache = self.make(redis, mongo)
        mongo.docs["k"] = {"_id": "k", "value": {"name": "A"},
                           "expires_at": datetime.now(timezone.utc) + timedelta(seconds=30)}
        self.assertEqual(cache.get("k"), {"name": "A"})
        self.assertIn("vre:llm:k", redis.data)
        self.assertLessEqual(redis.ttls["vre:llm:k"], 30)

    def test_naive_datetime_from_pymongo_is_treated_as_utc(self):
        mongo = FakeCollection()
        mongo.docs["k"] = {"_id": "k", "value": {"name": "A"},
                           "expires_at": datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=30)}
        self.assertEqual(self.make(None, mongo).get("k"), {"name": "A"})

    def test_expired_mongo_entry_is_a_miss(self):
        mongo = FakeCollection()
        mongo.docs["k"] = {"_id": "k", "value": {"name": "A"},
                           "expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}
        self.assertIsNone(self.make(None, mongo).get("k"))

    def test_redis_failure_warns_once_then_falls_back_to_mongo(self):
        cache = self.make(FakeRedis(fail=True), FakeCollection())
        cache.set("k", {"name": "A"})
        self.assertEqual(cache.get("k"), {"name": "A"})
        self.assertEqual((cache._redis, cache._collection is not None), (None, True))  # chỉ còn Mongo
        self.assertEqual(len(self.warnings), 1)
        self.assertIn("Redis", self.warnings[0])

    def test_all_tiers_failing_never_raises(self):
        cache = self.make(FakeRedis(fail=True), FakeCollection(fail=True))
        cache.set("k", {"name": "A"})
        self.assertIsNone(cache.get("k"))
        self.assertEqual((cache._redis, cache._collection), (None, None))
        self.assertEqual(len(self.warnings), 2)

    def test_corrupt_redis_value_is_a_miss(self):
        redis = FakeRedis()
        redis.data["vre:llm:k"] = "not json"
        self.assertIsNone(self.make(redis, None).get("k"))


class KeyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.file = Path(temp.name) / "a.md"
        self.file.write_text("báo giá")
        self.base = dict(model="m", prompt="p", user_text="u", schema={"a": 1})

    def test_ocr_key_depends_on_every_input(self):
        base = ocr_key(self.file, **self.base)
        self.assertEqual(base, ocr_key(self.file, **self.base))
        for change in ({"model": "m2"}, {"prompt": "p2"}, {"user_text": "u2"}, {"schema": {"a": 2}}):
            self.assertNotEqual(base, ocr_key(self.file, **{**self.base, **change}), change)
        self.file.write_text("báo giá khác")
        self.assertNotEqual(base, ocr_key(self.file, **self.base))

    def test_ocr_key_depends_on_thinking_level(self):
        base = ocr_key(self.file, **self.base)
        self.assertEqual(base, ocr_key(self.file, **self.base, thinking_level=None))  # không đặt mức: khóa cũ giữ nguyên
        low, medium = (ocr_key(self.file, **self.base, thinking_level=level) for level in ("low", "medium"))
        self.assertEqual(len({base, low, medium}), 3)

    def test_mapping_key_ignores_money_but_not_name_or_codebook(self):
        context = mapping_context(model="m", instructions="i", names=["A", "B"])
        fields = {"raw_name": "Sơn  cản", "category": "paint", "quantity": 1, "unit_price": 10, "raw_code": "X1"}
        key = mapping_key(context, fields)
        self.assertEqual(key, mapping_key(context, {**fields, "quantity": 5, "unit_price": 99, "raw_code": "Y"}))
        self.assertEqual(key, mapping_key(context, {**fields, "raw_name": " Sơn cản "}))  # khoảng trắng
        self.assertNotEqual(key, mapping_key(context, {**fields, "raw_name": "Sơn cản sau"}))
        self.assertNotEqual(key, mapping_key(context, {**fields, "category": "labor"}))
        for change in ({"model": "m2"}, {"instructions": "i2"}, {"names": ["A", "C"]}):
            other = mapping_context(**{"model": "m", "instructions": "i", "names": ["A", "B"], **change})
            self.assertNotEqual(key, mapping_key(other, fields), change)


class MatcherCacheTests(unittest.TestCase):
    NAMES = {"part": ["Sơn cản", "Cản trước"], "service": []}

    def setUp(self):
        self.client, self.cache = FakeClaude(), MemoryCache()
        self.matcher = LlmMatcher(ClaudeBackend(self.client), MODEL, cache=self.cache)
        self.items = estimate(("a", "paint"), ("b", "paint")).items

    def reply(self, *names):
        self.client.replies["mapping"] = message(
            {"selections": [{"index": i, "name": n, "item_type": n and "part"} for i, n in enumerate(names)]})

    def test_invalid_name_becomes_null_and_is_not_cached(self):
        self.reply("Sơn cản", "tự bịa")  # item b: tên ngoài codebook
        self.assertEqual(self.matcher.select_many(self.items, self.NAMES), [("part", "Sơn cản"), None])
        self.assertEqual(len(self.cache.data), 1)  # chỉ kết quả hợp lệ được cache
        self.reply("Cản trước")  # lần sau chỉ hỏi lại item b (a đã cache), không dính null cũ
        self.assertEqual(self.matcher.select_many(self.items, self.NAMES), [("part", "Sơn cản"), ("part", "Cản trước")])
        self.assertEqual(len(self.client.calls["mapping"]), 2)

    def test_work_cache_roundtrip_and_item_type_change(self):
        names = {"part": ["Cánh cửa"], "service": []}
        self.client.replies["mapping"] = message({"selections": [
            {"index": 0, "name": "Cánh cửa", "item_type": "part"},
            {"index": 1, "name": None, "item_type": None}]})
        expected = [("part", "Cánh cửa"), None]
        self.assertEqual(self.matcher.select_many(self.items, names), expected)
        # Redis/Mongo JSON serialization turns tuples into lists.
        self.cache.data = json.loads(json.dumps(self.cache.data))
        self.assertEqual(self.matcher.select_many(self.items, names), expected)
        self.assertEqual(len(self.client.calls["mapping"]), 1)
        self.client.replies["mapping"] = message({"selections": [
            {"index": 0, "name": "Cánh cửa", "item_type": "service"},
            {"index": 1, "name": None, "item_type": None}]})
        self.assertEqual(self.matcher.select_many(self.items, {"part": [], "service": ["Cánh cửa"]}),
                         [("service", "Cánh cửa"), None])
        self.assertEqual(len(self.client.calls["mapping"]), 2)

    def test_second_call_is_served_from_cache_including_null(self):
        self.reply("Sơn cản", None)
        self.assertEqual(self.matcher.select_many(self.items, self.NAMES), [("part", "Sơn cản"), None])
        self.assertEqual(self.matcher.select_many(self.items, self.NAMES), [("part", "Sơn cản"), None])
        self.assertEqual(len(self.client.calls["mapping"]), 1)
        self.assertEqual((self.matcher.cache_hits, self.matcher.cache_misses), (2, 2))
        self.assertEqual(len(self.matcher.calls), 1)

    def test_only_missing_items_are_sent_and_order_is_kept(self):
        self.reply("Sơn cản")
        self.matcher.select_many(self.items[:1], self.NAMES)
        self.reply("Cản trước")
        self.assertEqual(self.matcher.select_many(self.items, self.NAMES), [("part", "Sơn cản"), ("part", "Cản trước")])
        sent = self.client.sent_items()
        self.assertEqual([i["raw_name"] for i in sent], ["b"])
        self.assertEqual(sent[0]["index"], 0)
        self.assertEqual(set(sent[0]), {
            "index", "raw_section", "section_type", "raw_name", "operation",
            "vehicle_part", "item_name", "category",
        })
        self.assertNotIn("source_evidence", sent[0])

    def test_duplicates_in_one_batch_are_sent_once(self):
        self.reply("Sơn cản")
        twin = self.items[0].model_copy(update={"quantity": 9.0})
        self.assertEqual(self.matcher.select_many([self.items[0], twin], self.NAMES), [("part", "Sơn cản"), ("part", "Sơn cản")])
        sent = self.client.sent_items()
        self.assertEqual(len(sent), 1)

    def test_cached_name_outside_codebook_is_ignored(self):
        self.reply("Sơn cản")
        self.matcher.select_many(self.items[:1], self.NAMES)
        self.client.calls["mapping"].clear()
        self.reply("Cản trước")  # codebook không còn "Sơn cản"
        self.assertEqual(self.matcher.select_many(self.items[:1], {"part": ["Cản trước"], "service": []}), [("part", "Cản trước")])
        self.assertEqual(len(self.client.calls["mapping"]), 1)

    def test_failed_or_invalid_response_is_not_cached(self):
        self.reply("Sơn cản")  # thiếu index 1: cấu trúc sai thì vẫn lỗi và không ghi gì vào cache
        with self.assertRaises(ValueError):
            self.matcher.select_many(self.items, self.NAMES)
        self.assertEqual(self.cache.sets, 0)

    def test_without_cache_behaviour_is_unchanged(self):
        matcher = LlmMatcher(ClaudeBackend(self.client), MODEL)
        self.reply("Sơn cản", None)
        matcher.select_many(self.items, self.NAMES)
        matcher.select_many(self.items, self.NAMES)
        self.assertEqual(len(self.client.calls["mapping"]), 2)


class OcrCacheTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "input.md"
        self.source.write_text("Vehicle Repair Estimate")
        self.estimate = estimate(("Sơn cản", "paint"))
        self.client, self.cache = FakeClaude(), MemoryCache()

    def run_ocr(self, name, **kwargs):
        return extract_repair_estimate(self.source, model=MODEL, backend=ClaudeBackend(self.client), cache=self.cache,
                                       run_dir=self.root / name, verbose=False, **kwargs)

    def test_second_run_hits_cache_and_writes_artifacts(self):
        self.client.replies["ocr"] = message(self.estimate.model_dump(mode="json"))
        first = self.run_ocr("r1")
        second = self.run_ocr("r2")
        self.assertEqual(first, second)
        self.assertEqual(len(self.client.calls["ocr"]), 1)
        metadata = json.loads((self.root / "r2" / "metadata.json").read_text())
        self.assertTrue(metadata["cache_hit"])
        self.assertIsNone(metadata["cost"])
        self.assertFalse(json.loads((self.root / "r1" / "metadata.json").read_text())["cache_hit"])
        self.assertEqual((self.root / "r1" / "result.json").read_text(), (self.root / "r2" / "result.json").read_text())

    def test_changed_file_or_model_is_a_miss(self):
        self.client.replies["ocr"] = message(self.estimate.model_dump(mode="json"))
        self.run_ocr("r1")
        self.source.write_text("Nội dung khác")
        self.run_ocr("r2")
        extract_repair_estimate(self.source, model="claude-sonnet-5-5", backend=ClaudeBackend(self.client), cache=self.cache,
                                run_dir=self.root / "r3", verbose=False)
        self.assertEqual(len(self.client.calls["ocr"]), 3)

    def test_invalid_response_is_not_cached(self):
        self.client.replies["ocr"] = message({"invalid": True})
        with self.assertRaises(ValueError):
            self.run_ocr("r1")
        self.assertEqual(self.cache.sets, 0)

    def test_corrupt_cache_entry_falls_back_to_claude(self):
        self.client.replies["ocr"] = message(self.estimate.model_dump(mode="json"))
        self.run_ocr("r1")
        for key in self.cache.data:
            self.cache.data[key] = {"result": {"invalid": True}}
        self.run_ocr("r2")
        self.assertEqual(len(self.client.calls["ocr"]), 2)


class PipelineCacheTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for prefix, rows in [("service", [("S1", "Sơn cản")]), ("part", [("P1", "Cản trước")])]:
            w = Workbook()
            w.active.append(["MA", "TEN"])
            for row in rows:
                w.active.append(row)
            w.save(self.root / f"{prefix}_codebook.xlsx")
        w = Workbook()
        w.active.append(["MA", "TEN", "item_type"])
        w.active.append(["B1", "Cản", "part"])
        w.active.append(["P1", "Cản trước", "part"])
        w.save(self.root / "codebook.xlsx")
        self.source = self.root / "bao_gia.md"
        self.source.write_text("Báo giá")
        self.estimate = estimate(("Sơn cản", "paint"), ("Nẹp cản trước", "material"))
        self.client, self.cache = FakeClaude(), MemoryCache()

    def run_vre(self, **kwargs):
        return run_repair_estimate_pipeline(
            self.source, model=MODEL, llm=LlmClients(clients={"claude": self.client}), output_dir=self.root / "out",
            codebook_dir=self.root, verbose=False, **kwargs)

    def test_second_run_makes_no_claude_calls_and_reports_cache(self):
        self.client.replies["ocr"] = message(self.estimate.model_dump(mode="json"))
        self.client.replies["mapping"] = [
            message({"selections": [{"index": 0, "name": "Cản", "item_type": "part"}, {"index": 1, "name": "Cản trước", "item_type": "part"}]}),
        ]
        first = self.run_vre(cache=self.cache)
        self.assertEqual(len(self.client.calls["ocr"]) + len(self.client.calls["mapping"]), 2)
        self.assertFalse(first["steps"]["ocr"]["cache_hit"])
        second = self.run_vre(cache=self.cache)
        self.assertEqual(len(self.client.calls["ocr"]) + len(self.client.calls["mapping"]), 2)  # không gọi thêm
        self.assertTrue(second["steps"]["ocr"]["cache_hit"])
        self.assertEqual(second["steps"]["mapping"]["cache_hits"], 2)
        self.assertEqual(second["steps"]["mapping"]["llm_calls"], 0)
        self.assertEqual(second["total"]["cost"]["calls"], 0)
        self.assertEqual([i["codes"] for i in second["items"]], [i["codes"] for i in first["items"]])
        self.assertIn("cache", (Path(second["run_dir"]) / "report.txt").read_text())

    def test_no_cache_always_calls_claude(self):
        self.client.replies["ocr"] = message(self.estimate.model_dump(mode="json"))
        self.client.replies["mapping"] = [
            message({"selections": [{"index": 0, "name": None, "item_type": None}, {"index": 1, "name": None, "item_type": None}]}),
            message({"selections": [{"index": 0, "name": None, "item_type": None}, {"index": 1, "name": None, "item_type": None}]}),
        ]
        self.run_vre()
        self.run_vre()
        self.assertEqual(len(self.client.calls["ocr"]) + len(self.client.calls["mapping"]), 4)


if __name__ == "__main__":
    unittest.main()
