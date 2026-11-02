import unittest
from datetime import date

from cost import cost_from_response, estimate_cost, sum_costs

USAGE = {"total_input_tokens": 1000, "total_cached_tokens": 200,
         "total_output_tokens": 100, "total_thought_tokens": 400}


class CostTests(unittest.TestCase):
    def test_estimate_promo_and_regular_rates(self):
        promo = estimate_cost("gemini-3.8-flash", USAGE, date(2026, 10, 5))
        # 800*0.75 + 200*0.075 + 100*3.75 + 400*3.75, per 1M tokens
        self.assertAlmostEqual(promo["total_usd"], (600 + 15 + 375 + 1500) / 1e6)
        regular = estimate_cost("models/gemini-3.8-flash", USAGE, date(2027, 1, 1))
        self.assertAlmostEqual(regular["total_usd"], 2 * promo["total_usd"])
        self.assertEqual(regular["model"], "gemini-3.8-flash")

    def test_unpriced_reasons(self):
        self.assertIsNone(estimate_cost("unknown-model", USAGE)["total_usd"])
        self.assertIn("usage", estimate_cost("gemini-3.8-flash", None)["reason"])
        self.assertIn("không hợp lệ", estimate_cost("gemini-3.8-flash", {**USAGE, "total_input_tokens": "1"})["reason"])

    def test_cost_from_response_prefers_response_model(self):
        cost = cost_from_response({"model": "gemini-3.6-flash", "usage": USAGE}, "unknown-model")
        self.assertEqual(cost["model"], "gemini-3.6-flash")
        self.assertIsNotNone(cost["total_usd"])
        self.assertIsNone(cost_from_response(None, "unknown-model")["total_usd"])

    def test_sum_costs_counts_priced_and_unpriced(self):
        a = estimate_cost("gemini-3.8-flash", USAGE, date(2026, 10, 5))
        b = estimate_cost("gemini-3.6-flash", USAGE, date(2026, 10, 5))
        total = sum_costs([a, b, estimate_cost("x", USAGE), None])
        self.assertEqual((total["calls"], total["priced_calls"], total["unpriced_calls"]), (3, 2, 1))
        self.assertFalse(total["complete"])
        self.assertAlmostEqual(total["total_usd"], a["total_usd"] + b["total_usd"])
        self.assertEqual(total["tokens"]["total_input_tokens"], 2000)
        empty = sum_costs([])
        self.assertTrue(empty["complete"])
        self.assertEqual(empty["total_usd"], 0.0)

    def test_claude_cache_write_read_and_output(self):
        usage = {"input_tokens": 1000, "cache_creation_input_tokens": 3000, "cache_read_input_tokens": 50000,
                 "cache_creation": {"ephemeral_5m_input_tokens": 1000, "ephemeral_1h_input_tokens": 2000},
                 "output_tokens": 2000}
        cost = cost_from_response({"model": "claude-opus-5-5", "usage": usage}, "unknown-model")
        # 1000*4 + 1000*5 (ghi 5 phút) + 2000*8 (ghi 1 giờ) + 50000*0.20 (đọc cache) + 2000*20
        self.assertAlmostEqual(cost["total_usd"], (4000 + 5000 + 16000 + 10000 + 40000) / 1e6)
        self.assertEqual(cost["tokens"], {"total_input_tokens": 54000, "total_cached_tokens": 50000,
                                          "total_output_tokens": 2000, "total_thought_tokens": 0,
                                          "total_cache_write_tokens": 3000})
        self.assertIn("claude.com", cost["pricing_source"])

    def test_claude_haiku_long_prompt_uses_higher_rates(self):
        short = estimate_cost("claude-haiku-5-5", {"input_tokens": 100_000, "output_tokens": 1000})
        self.assertAlmostEqual(short["total_usd"], (100_000 * 0.10 + 1000 * 0.50) / 1e6)
        # cache đọc cũng tính vào độ dài prompt: vượt 100K thì cả request theo giá cao
        long = estimate_cost("claude-haiku-5-5", {"input_tokens": 1, "cache_read_input_tokens": 100_000, "output_tokens": 1000})
        self.assertAlmostEqual(long["total_usd"], (1 * 0.50 + 100_000 * 0.05 + 1000 * 2.50) / 1e6)

    def test_claude_unpriced_and_mixed_summary(self):
        self.assertIn("bảng giá", estimate_cost("claude-unknown", {"input_tokens": 1, "output_tokens": 1})["reason"])
        self.assertIn("không hợp lệ", estimate_cost("claude-opus-5-5", {"output_tokens": 1})["reason"])
        claude = estimate_cost("claude-opus-5-5", {"input_tokens": 10, "cache_creation_input_tokens": 5, "output_tokens": 1})
        gemini = estimate_cost("gemini-3.8-flash", USAGE, date(2026, 10, 5))
        total = sum_costs([claude, gemini])
        self.assertAlmostEqual(total["total_usd"], claude["total_usd"] + gemini["total_usd"])
        self.assertEqual(total["tokens"]["total_cache_write_tokens"], 5)
        self.assertEqual(total["tokens"]["total_input_tokens"], 15 + 1000)


if __name__ == "__main__":
    unittest.main()
