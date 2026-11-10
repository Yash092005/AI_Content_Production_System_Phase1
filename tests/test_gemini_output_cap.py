"""The output cap a Gemini request carries is exactly what the planner reserved (including Gemini's minimum)."""
import os
import unittest
from unittest import mock

from tests import helpers as H
from app.ai import budget
from app.ai.budget import GEMINI_MODEL_MAX_OUTPUT, out_cap_for, plan
from app.ai.gemini_client import GeminiClient
from app.ai.router import Router
from app.config import runtime
from app.config.settings import Settings
from app.metrics.usage import UsageLog

OK = {"candidates": [{"content": {"parts": [{"text": "hello"}]}, "finishReason": "STOP"}],
      "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20, "totalTokenCount": 120}}


class Planner(unittest.TestCase):
    def test_gemini_reserves_its_minimum_for_hidden_thinking_groq_does_not(self):
        self.assertEqual(out_cap_for("qa", provider="gemini"), 8192)
        self.assertEqual(out_cap_for("qa", provider="groq"), 800)
        self.assertGreaterEqual(out_cap_for("generate", 1500, "gemini"), 8192)
        self.assertLess(out_cap_for("generate", 1500, "groq"), 8192)

    def test_the_minimum_is_configurable_and_bounded_by_the_models_maximum(self):
        with runtime.use_settings(Settings({}).with_overrides(**{"budget.gemini_min_output_tokens": 4096})):
            self.assertEqual(out_cap_for("qa", provider="gemini"), 4096)
        with runtime.use_settings(Settings({}).with_overrides(**{"budget.gemini_min_output_tokens": 999999})):
            self.assertEqual(out_cap_for("qa", provider="gemini"), GEMINI_MODEL_MAX_OUTPUT)
        self.assertLessEqual(out_cap_for("generate", 100000, "gemini"), GEMINI_MODEL_MAX_OUTPUT)

    def test_the_planned_total_includes_the_minimum(self):
        p = plan("gemini", "gemini-3.6-flash", "qa", "s" * 400, "p" * 400, tracker=budget.Tracker(), usage=UsageLog())
        self.assertEqual(p.out_cap, 8192)
        self.assertEqual(p.total, p.in_tokens + 8192)

    def test_measured_output_never_lowers_a_gemini_request_below_the_minimum(self):
        usage = UsageLog()
        for _ in range(6):
            usage.record(provider="gemini", stage="qa", ok=True, completion_tokens=50)
        self.assertEqual(out_cap_for("qa", None, "gemini", usage), 8192)


class OnTheWire(unittest.TestCase):
    def setUp(self):
        H.reset_state()
        env = mock.patch.dict(os.environ, {"GEMINI_MIN_INTERVAL": "0", "GEMINI_MODEL": "gemini-3.6-flash",
                                           "GEMINI_GROUNDING_CAPABILITY": "auto", "GEMINI_GROUNDING": "on"})
        env.start()
        self.addCleanup(env.stop)
        self.seen = []

    def post(self, url, payload, headers, retries=0, **kw):
        self.seen.append({"payload": payload,
                          "reserved": budget.TRACKER.tokens_last("gemini", "gemini-3.6-flash", group="gemini:unverified")})
        return OK

    def router(self):
        gem = GeminiClient(api_key="AIzaFakeFakeFakeFakeFake12345")
        groq = H.Stub("groq", "oss", search=True)
        return Router(gem, groq, sleep=lambda s: None), gem

    def test_the_request_carries_exactly_the_reserved_output_cap(self):
        r, gem = self.router()
        with mock.patch("app.ai.gemini_client.post_json", self.post):
            for stage, words in (("qa", None), ("generate", 900), ("generate", 1500), ("blueprint", 900), ("revise", 450)):
                self.seen.clear()
                r.generate("p" * 3000, system="s" * 3000, stage=stage, target_words=words)
                cand = [c for c in r.candidates(stage) if c.provider == "gemini"][0]
                planned = r._plan(cand, stage, "s" * 3000, "p" * 3000, words).out_cap
                sent = self.seen[0]["payload"]["generationConfig"]["maxOutputTokens"]
                self.assertEqual(sent, planned, stage)
                self.assertGreaterEqual(sent, 8192, stage)
                self.assertLessEqual(sent, GEMINI_MODEL_MAX_OUTPUT)
                budget.TRACKER.reset()

    def test_the_tokens_reserved_for_the_request_include_that_same_cap(self):
        r, gem = self.router()
        with mock.patch("app.ai.gemini_client.post_json", self.post):
            r.generate("p" * 3000, system="s" * 3000, stage="qa")
        sent = self.seen[0]["payload"]["generationConfig"]["maxOutputTokens"]
        est_in = budget.est_input_tokens("s" * 3000, "p" * 3000, "gemini")
        self.assertEqual(self.seen[0]["reserved"], est_in + sent)

    def test_no_cap_is_ever_larger_than_the_reservation(self):
        r, gem = self.router()
        with mock.patch("app.ai.gemini_client.post_json", self.post):
            r.generate("p" * 500, system="s" * 500, stage="qa")
        reserved_out = out_cap_for("qa", provider="gemini")
        self.assertLessEqual(self.seen[0]["payload"]["generationConfig"]["maxOutputTokens"], reserved_out)

    def test_an_unrouted_call_without_a_cap_sends_none_as_before(self):
        gem = GeminiClient(api_key="AIzaFakeFakeFakeFakeFake12345")
        with mock.patch("app.ai.gemini_client.post_json", self.post):
            gem.generate("hi", stage="qa")
        self.assertNotIn("generationConfig", self.seen[0]["payload"])


if __name__ == "__main__":
    unittest.main()
