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


if __name__ == "__main__":
    unittest.main()
