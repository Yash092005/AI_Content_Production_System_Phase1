import os
import unittest
from unittest import mock

from tests import helpers
from app.ai.base import ApiError, CapabilityUnavailable, OutputTruncated
from app.ai.fallback import EmptyResponseError
from app.ai.health import Health, classify, grounding_allowed, grounding_policy, parse_wait


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def err(status, text, **attrs):
    e = ApiError(status, text)
    for k, v in attrs.items():
        setattr(e, k, v)
    return e


class Classify(unittest.TestCase):
    def kind(self, e):
        return classify(e)["kind"]

    def test_the_incident_body_is_an_unknown_quota(self):
        body = '{"error":{"code":429,"message":"You exceeded your current quota","status":"RESOURCE_EXHAUSTED","details":[{"@type":"type.googleapis.com/google.rpc.Help"}]}}'
        self.assertEqual(self.kind(err(429, body)), "unknown_quota")

    def test_daily_per_minute_and_grounding_are_told_apart(self):
        self.assertEqual(self.kind(err(429, "x", quota_id="GenerateRequestsPerDayPerProjectPerModel")), "daily")
        self.assertEqual(self.kind(err(429, "Rate limit reached ... tokens per minute (TPM): Limit 8000. Please try again in 7.5s")), "rpm")
        self.assertEqual(self.kind(err(429, "x", quota_metric="generativelanguage.googleapis.com/search_requests")), "grounding")
        self.assertEqual(self.kind(err(429, "tokens per day (TPD) exceeded. try again in 23m4s")), "daily")

    def test_retry_delay_is_read(self):
        self.assertEqual(classify(err(429, "x", retry_delay="43s"))["retry_after"], 43.0)
        self.assertAlmostEqual(parse_wait("Please try again in 2m3.5s"), 123.5)

    def test_other_failures(self):
        self.assertEqual(self.kind(err(503, "overloaded")), "overload")
        self.assertEqual(self.kind(err(401, "bad key")), "client")
        self.assertEqual(self.kind(err(413, "too large")), "too_large")
        self.assertEqual(self.kind(RuntimeError("Request is about 8722 tokens but the configured Groq limit is 8000")), "too_large")
        self.assertEqual(self.kind(CapabilityUnavailable("gemini", "grounding")), "capability")
        self.assertEqual(self.kind(OutputTruncated("cap")), "truncated")
        self.assertEqual(self.kind(EmptyResponseError("empty")), "empty")
        self.assertEqual(self.kind(ConnectionError("network down")), "overload")


class Cooldowns(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.h = Health(self.clock)

    def test_a_limited_provider_rests_then_returns(self):
        self.h.note_error("groq", "m", err(429, "tokens per minute (TPM). try again in 20s"))
        self.assertGreaterEqual(self.h.cooling("groq", "m"), 30)         # never shorter than 30 s
        self.clock.t += 40
        self.assertEqual(self.h.cooling("groq", "m"), 0)

    def test_repeated_unexplained_429s_rest_longer_each_time(self):
        rests = []
        for _ in range(4):
            self.h.note_error("gemini", "g", err(429, "quota"))
            rests.append(self.h.cooling("gemini", "g"))
            self.clock.t += 7200
        self.assertEqual([round(r) for r in rests], [60, 300, 900, 3600])

    def test_success_clears_the_rest_and_the_escalation(self):
        self.h.note_error("gemini", "g", err(429, "quota"))
        self.h.note_success("gemini", "g")
        self.assertEqual(self.h.cooling("gemini", "g"), 0)
        self.h.note_error("gemini", "g", err(429, "quota"))
        self.assertEqual(round(self.h.cooling("gemini", "g")), 60)

    def test_planning_errors_and_bad_requests_do_not_make_a_provider_rest(self):
        for e in (RuntimeError("configured Groq limit"), err(400, "bad"), OutputTruncated("x")):
            self.h.note_error("groq", "m", e)
        self.assertEqual(self.h.cooling("groq", "m"), 0)

    def test_models_rest_independently(self):
        self.h.note_error("gemini", "a", err(429, "quota"))
        self.assertGreater(self.h.cooling("gemini", "a"), 0)
        self.assertEqual(self.h.cooling("gemini", "b"), 0)


class Grounding(unittest.TestCase):
    def setUp(self):
        helpers.reset_state()
        self.clock = Clock()
        self.h = Health(self.clock)

    def policy(self, **env):
        return mock.patch.dict(os.environ, env)

    def test_default_policy_is_auto_nothing_is_assumed(self):
        with self.policy(GEMINI_GROUNDING="on", GEMINI_GROUNDING_CAPABILITY="auto"):
            self.assertEqual(grounding_policy(), "auto")
            self.assertFalse(grounding_allowed("m", "k", probe=False, health=self.h))     # unknown and not a probe
            self.assertTrue(grounding_allowed("m", "k", probe=True, health=self.h))       # a single probe is allowed

    def test_the_old_off_switch_still_means_never(self):
        with self.policy(GEMINI_GROUNDING="off"):
            self.assertEqual(grounding_policy(), "no")
            self.assertFalse(grounding_allowed("m", "k", probe=True, health=self.h))

    def test_a_failed_probe_is_remembered_and_not_repeated(self):
        with self.policy(GEMINI_GROUNDING="on", GEMINI_GROUNDING_CAPABILITY="auto"):
            self.h.mark_grounding("m", "k", False)
            self.assertFalse(grounding_allowed("m", "k", probe=True, health=self.h))
            self.assertFalse(grounding_allowed("m", "k", probe=True, health=self.h))

    def test_even_when_configured_yes_a_known_failure_is_not_repeated(self):
        with self.policy(GEMINI_GROUNDING="on", GEMINI_GROUNDING_CAPABILITY="yes"):
            self.assertTrue(grounding_allowed("m", "k", health=self.h))
            self.h.mark_grounding("m", "k", False)
            self.assertFalse(grounding_allowed("m", "k", health=self.h))

    def test_memory_expires_so_it_is_probed_again_later(self):
        with self.policy(GEMINI_GROUNDING="on", GEMINI_GROUNDING_CAPABILITY="auto"):
            self.h.mark_grounding("m", "k", False, ttl_hours=24)
            self.clock.t += 25 * 3600
            self.assertEqual(self.h.grounding_state("m", "k"), "unknown")

    def test_a_confirmed_capability_is_used_without_probing(self):
        with self.policy(GEMINI_GROUNDING="on", GEMINI_GROUNDING_CAPABILITY="auto"):
            self.h.mark_grounding("m", "k", True)
            self.assertTrue(grounding_allowed("m", "k", probe=False, health=self.h))

    def test_state_is_per_key_and_model(self):
        self.h.mark_grounding("m", "k1", False)
        self.assertEqual(self.h.grounding_state("m", "k2"), "unknown")
        self.assertEqual(self.h.grounding_state("other", "k1"), "unknown")


if __name__ == "__main__":
    unittest.main()
