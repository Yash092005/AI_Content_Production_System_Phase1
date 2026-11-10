import os
import unittest
from unittest import mock

from tests import helpers as H
from app.ai import budget
from app.ai.base import ApiError, CapabilityUnavailable, OutputTruncated
from app.ai.gemini_client import GeminiClient, UNGROUNDED_NOTE
from app.ai.groq_client import GroqClient
from app.ai.health import HEALTH
from app.ai.pool import KeyPool
from app.config import runtime
from app.config.settings import Settings
from app.metrics.usage import LOG

GEMINI_OK = {"candidates": [{"content": {"parts": [{"text": "hello"}]}, "finishReason": "STOP"}],
             "usageMetadata": {"promptTokenCount": 900, "candidatesTokenCount": 120, "thoughtsTokenCount": 30,
                               "totalTokenCount": 1050, "cachedContentTokenCount": 400}}
GROQ_OK = {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
           "usage": {"prompt_tokens": 700, "completion_tokens": 90, "total_tokens": 790,
                     "prompt_tokens_details": {"cached_tokens": 512}}}


class GeminiBase(unittest.TestCase):
    def setUp(self):
        H.reset_state()
        self.env = mock.patch.dict(os.environ, {"GEMINI_GROUNDING": "on", "GEMINI_GROUNDING_CAPABILITY": "auto",
                                                "GEMINI_MIN_INTERVAL": "0", "GEMINI_MODEL": "gem-x"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.posts = []

    def client(self):
        return GeminiClient(api_key="AIzaFakeFakeFakeFakeFake12345")

    def fake(self, *responses):
        queue = list(responses)

        def post(url, payload, headers, retries=0, **kw):
            self.posts.append({"url": url, "payload": payload, "retries": retries})
            r = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(r, Exception):
                raise r
            return r
        return mock.patch("app.ai.gemini_client.post_json", post)

    def grounded_posts(self):
        return [p for p in self.posts if "tools" in p["payload"]]


class GeminiUsage(GeminiBase):
    def test_real_usage_is_recorded_and_the_cap_is_sent(self):
        with self.fake(GEMINI_OK):
            self.client().generate("hi", system="rules", stage="qa", max_tokens=800)
        row = LOG.rows()[-1]
        self.assertEqual((row["provider"], row["stage"], row["ok"]), ("gemini", "qa", True))
        self.assertEqual((row["prompt_tokens"], row["completion_tokens"], row["total_tokens"], row["cached_tokens"]),
                         (900, 150, 1050, 400))
        self.assertEqual(row["thinking_tokens"], 30)
        self.assertEqual(self.posts[0]["payload"]["generationConfig"]["maxOutputTokens"], 800)    # exactly the reservation
        self.assertNotIn("AIza", str(row))

    def test_usage_is_counted_in_the_rolling_window(self):
        with self.fake(GEMINI_OK):
            self.client().generate("hi", stage="qa")
        self.assertEqual(budget.TRACKER.tokens_last("gemini", "gem-x"), 1050)

    def test_a_failed_call_is_logged_with_its_class(self):
        with self.fake(H.api_error(503, "busy")), self.assertRaises(ApiError):
            self.client().generate("hi", stage="qa")
        self.assertFalse(LOG.rows()[-1]["ok"])
        self.assertTrue(LOG.rows()[-1]["error"].startswith("overload"))

    def test_a_reply_cut_at_the_output_limit_is_not_returned_as_an_answer(self):
        cut = {"candidates": [{"content": {"parts": [{"text": "{\"a\":"}]}, "finishReason": "MAX_TOKENS"}]}
        with self.fake(cut), self.assertRaises(OutputTruncated):
            self.client().generate("hi", stage="qa")

    def test_no_cap_means_no_generation_config_like_before(self):
        with self.fake(GEMINI_OK):
            self.client().generate("hi", stage="qa")
        self.assertNotIn("generationConfig", self.posts[0]["payload"])


class GeminiGrounding(GeminiBase):
    BODY = '{"error":{"code":429,"message":"You exceeded your current quota","status":"RESOURCE_EXHAUSTED","details":[{"@type":"type.googleapis.com/google.rpc.Help"}]}}'

    def test_the_incident_pattern_marks_grounding_unavailable_after_ONE_attempt(self):
        c = self.client()
        with self.fake(H.api_error(429, self.BODY)), self.assertRaises(CapabilityUnavailable):
            c.generate("q", system="s", stage="research", grounded=True)
        self.assertEqual(len(self.grounded_posts()), 1)
        self.assertEqual(self.grounded_posts()[0]["retries"], 0)               # no 15 s retries on a probe
        self.assertEqual(HEALTH.grounding_state("gem-x", c.key_id), "unavailable")

    def test_an_unsupported_grounding_request_is_never_repeated(self):
        c = self.client()
        with self.fake(H.api_error(429, self.BODY), GEMINI_OK):
            with self.assertRaises(CapabilityUnavailable):
                c.generate("q", stage="research", grounded=True)
            for _ in range(5):
                with self.assertRaises(CapabilityUnavailable):
                    c.generate("q", stage="research", grounded=True)           # refused locally: no HTTP call
        self.assertEqual(len(self.posts), 1)

    def test_direct_use_falls_back_to_a_flagged_plain_call_and_remembers(self):
        c = self.client()
        with self.fake(H.api_error(429, self.BODY), GEMINI_OK):
            out = c.generate("q", stage="research")
            out2 = c.generate("q", stage="research")
        self.assertTrue(out.endswith(UNGROUNDED_NOTE) and out2.endswith(UNGROUNDED_NOTE))
        self.assertEqual(len(self.grounded_posts()), 1)                        # the second call went straight to plain

    def test_success_confirms_the_capability(self):
        c = self.client()
        with self.fake(GEMINI_OK):
            c.generate("q", stage="research", grounded=True)
        self.assertEqual(HEALTH.grounding_state("gem-x", c.key_id), "available")

    def test_a_real_rate_limit_is_not_mistaken_for_missing_grounding(self):
        c = self.client()
        daily = H.api_error(429, "quota")
        daily.quota_id = "GenerateRequestsPerDayPerProjectPerModel"
        with self.fake(daily), self.assertRaises(ApiError):
            c.generate("q", stage="research", grounded=True)
        self.assertEqual(HEALTH.grounding_state("gem-x", c.key_id), "unknown")

    def test_overload_is_not_a_capability_problem_either(self):
        c = self.client()
        with self.fake(H.api_error(503, "busy")), self.assertRaises(ApiError):
            c.generate("q", stage="research", grounded=True)
        self.assertEqual(HEALTH.grounding_state("gem-x", c.key_id), "unknown")

    def test_plain_stages_never_use_grounding(self):
        with self.fake(GEMINI_OK):
            self.client().generate("q", stage="generate")
        self.assertEqual(self.grounded_posts(), [])

    def test_a_grounded_request_is_refused_locally_when_switched_off(self):
        with mock.patch.dict(os.environ, {"GEMINI_GROUNDING": "off"}), self.fake(GEMINI_OK), self.assertRaises(CapabilityUnavailable):
            self.client().generate("q", stage="research", grounded=True)
        self.assertEqual(self.posts, [])

    def test_each_key_is_judged_separately_by_the_pool(self):
        c1, c2 = GeminiClient(api_key="AIzaKEYoneKEYoneKEYoneKEY1"), GeminiClient(api_key="AIzaKEYtwoKEYtwoKEYtwoKEY2")
        pool = KeyPool("gemini", [c1, c2])
        with self.fake(H.api_error(429, self.BODY), GEMINI_OK):
            out = pool.generate("q", stage="research", grounded=True)
        self.assertEqual(out, "hello")
        self.assertEqual(HEALTH.grounding_state("gem-x", c1.key_id), "unavailable")
        self.assertEqual(HEALTH.grounding_state("gem-x", c2.key_id), "available")


class GeminiModels(GeminiBase):
    def with_settings(self, **over):
        s = Settings({}).with_overrides(**over)
        return runtime.use_settings(s)

    def test_default_is_one_model_for_every_stage_like_before(self):
        c = self.client()
        self.assertEqual(c.model_chain("qa"), ["gem-x"])
        self.assertEqual(c.model_chain("research"), ["gem-x"])

    def test_the_old_per_stage_environment_variables_stay_ignored(self):
        with mock.patch.dict(os.environ, {"GEMINI_MODEL_QA": "some-flash-lite", "GEMINI_MODEL_RESEARCH": "some-flash-lite"}):
            self.assertEqual(self.client().model_chain("qa"), ["gem-x"])

    def test_stage_models_and_pool_are_used_only_when_configured(self):
        with self.with_settings(**{"provider.gemini_model": "gem-x",
                                   "provider.gemini_stage_models": {"qa": "confirmed-lite"},
                                   "provider.gemini_model_pool": ["second", "gem-x"]}):
            c = self.client()
            self.assertEqual(c.model_chain("qa"), ["confirmed-lite", "gem-x", "second"])
            self.assertEqual(c.model_chain("generate"), ["gem-x", "second"])

    def test_an_explicit_model_gets_no_hidden_internal_fallback(self):
        c = self.client()
        c.fallback = "other"
        with self.fake(H.api_error(429, "quota")), self.assertRaises(ApiError):
            c.generate("q", stage="qa", model="gem-x")
        self.assertEqual(len(self.posts), 1)


class GroqBase(unittest.TestCase):
    def setUp(self):
        H.reset_state()
        self.env = mock.patch.dict(os.environ, {"GROQ_BROWSER_SEARCH": "on", "GROQ_MODEL": "openai/gpt-oss-120b",
                                                "GROQ_RESEARCH_MODEL": "openai/gpt-oss-120b", "GROQ_TPM": "8000"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.posts = []

    def fake(self, data, headers=None):
        def post(url, payload, hdrs, retries=0, meta=None, **kw):
            self.posts.append(payload)
            if meta is not None:
                meta["headers"] = headers or {}
            if isinstance(data, Exception):
                raise data
            return data
        return mock.patch("app.ai.groq_client.post_json", post)


class GroqCalls(GroqBase):
    def client(self):
        return GroqClient(api_key="gsk_fakefakefakefakefake123456")

    def test_research_enforces_the_cap_and_uses_browser_search(self):
        with self.fake(GROQ_OK):
            self.client().generate("q", system="s", stage="research", max_tokens=2000, planned=True)
        p = self.posts[0]
        self.assertEqual(p["max_completion_tokens"], 2000)
        self.assertEqual(p["tools"], [{"type": "browser_search"}])

    def test_plain_stages_have_no_tool(self):
        with self.fake(GROQ_OK):
            self.client().generate("q", stage="qa", max_tokens=800, planned=True)
        self.assertNotIn("tools", self.posts[0])

    def test_real_usage_cached_tokens_and_search_flag_are_recorded(self):
        with self.fake(GROQ_OK):
            self.client().generate("q" * 100, system="s" * 100, stage="research", max_tokens=2000, planned=True)
        r = LOG.rows()[-1]
        self.assertEqual((r["prompt_tokens"], r["completion_tokens"], r["cached_tokens"]), (700, 90, 512))
        self.assertTrue(r["search"])
        self.assertEqual(budget.TRACKER.tokens_last("groq", "openai/gpt-oss-120b"), 790)

    def test_the_providers_remaining_token_counter_feeds_the_planner(self):
        with self.fake(GROQ_OK, headers={"x-ratelimit-remaining-tokens": "2500", "x-ratelimit-reset-tokens": "30s"}):
            self.client().generate("q", stage="qa", max_tokens=800, planned=True)
        self.assertEqual(budget.TRACKER.used_from_headers("groq", "openai/gpt-oss-120b", 8000), 5500)
        self.assertEqual(LOG.rows()[-1]["remaining_tokens"], 2500)

    def test_a_reply_cut_by_the_cap_raises_and_is_logged_as_failed(self):
        cut = {"choices": [{"message": {"content": "{\"a\":"}, "finish_reason": "length"}], "usage": {"total_tokens": 2100}}
        with self.fake(cut), self.assertRaises(OutputTruncated):
            self.client().generate("q", stage="research", max_tokens=2000, planned=True)
        self.assertFalse(LOG.rows()[-1]["ok"])

    def test_unrouted_use_still_refuses_what_can_never_fit(self):
        with self.fake(GROQ_OK), self.assertRaises(RuntimeError) as cm:
            self.client().generate("p" * 40000, system="s" * 4000, stage="qa")
        self.assertIn("configured Groq limit", str(cm.exception))
        self.assertEqual(self.posts, [])

    def test_routed_use_skips_that_guard_because_the_router_already_planned(self):
        with self.fake(GROQ_OK):
            self.client().generate("p" * 100, stage="qa", planned=True, max_tokens=300)
        self.assertEqual(len(self.posts), 1)


if __name__ == "__main__":
    unittest.main()
