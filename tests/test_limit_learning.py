"""Regression tests for the API-reliability investigation (see the API Reliability Investigation handover).

Defect: one 429 could exclude a provider for the life of the process. The refused request was counted as the quota's
ceiling (ceiling 1 = no request ever fits), token limits were stored as request ceilings, and learned ceilings never expired.
No network or API keys are used."""
import datetime as dt
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import helpers as H                                   # noqa: E402
from app.ai import base, budget                                  # noqa: E402
from app.ai.base import ApiError                                 # noqa: E402
from app.ai.budget import Tracker, limits_for, plan              # noqa: E402
from app.ai.groq_client import GroqClient                        # noqa: E402
from app.ai.gemini_client import GeminiClient                    # noqa: E402
from app.ai.health import HEALTH, Health, limit_unit, seconds_until_daily_reset  # noqa: E402
from app.metrics.usage import UsageLog                           # noqa: E402

GROQ = "openai/gpt-oss-120b"
GEM = "gemini-3.6-flash"
GROQ_TPM = ('{"error":{"message":"Rate limit reached for model `openai/gpt-oss-120b` in organization `org_01abc` service tier '
            '`on_demand` on tokens per minute (TPM): Limit 8000, Used 3100, Requested 6200. Please try again in 9.8s.",'
            '"type":"tokens","code":"rate_limit_exceeded"}}')
GROQ_RPM = GROQ_TPM.replace("tokens per minute (TPM)", "requests per minute (RPM)").replace('"tokens"', '"requests"')


def google_429(quota_id, delay="35s"):
    return json.dumps({"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "You exceeded your current quota.",
                                 "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                              "violations": [{"quotaMetric": "generativelanguage.googleapis.com/x",
                                                              "quotaId": quota_id}]},
                                             {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay}]}})


GEM_RPM = google_429("GenerateRequestsPerMinutePerProjectPerModel-FreeTier")
GEM_DAY = google_429("GenerateRequestsPerDayPerProjectPerModel-FreeTier", "21s")
GEM_TPM = google_429("GenerateContentInputTokensPerModelPerMinute-FreeTier")


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class Resp:
    def __init__(self, code, body, headers=None):
        self.status_code, self.text, self.headers = code, body, headers or {}

    def json(self):
        return json.loads(self.text)


class LimitUnit(unittest.TestCase):
    def test_token_and_request_limits_are_told_apart(self):
        self.assertEqual(limit_unit(ApiError(429, GROQ_TPM)), "tokens")
        self.assertEqual(limit_unit(ApiError(429, GROQ_RPM)), "requests")
        self.assertEqual(limit_unit(ApiError(429, GEM_TPM)), "tokens")
        self.assertEqual(limit_unit(ApiError(429, GEM_RPM)), "requests")
        self.assertEqual(limit_unit(ApiError(429, GEM_DAY)), "requests")
        self.assertEqual(limit_unit(ApiError(429, '{"error":{"message":"Too many requests"}}')), "unknown")


class LearnedCeilings(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.tr = Tracker(self.clock)

    def test_the_refused_request_alone_teaches_nothing(self):
        rid = self.tr.reserve("gemini", GEM, 100)
        self.tr.settle("gemini", GEM, rid, 0, failed=True)
        self.tr.note_limit_hit("gemini", GEM, "rpm")
        self.assertEqual(self.tr.learned("gemini", GEM), {})

    def test_completed_requests_still_teach_a_ceiling_but_the_refused_one_is_not_counted(self):
        for _ in range(3):
            self.tr.reserve("gemini", GEM, 100)
        rid = self.tr.reserve("gemini", GEM, 100)
        self.tr.settle("gemini", GEM, rid, 0, failed=True)
        self.tr.note_limit_hit("gemini", GEM, "rpm")
        self.assertEqual(self.tr.learned("gemini", GEM), {"rpm": 3})

    def test_a_token_limit_never_becomes_a_request_ceiling(self):
        for _ in range(2):
            self.tr.reserve("groq", GROQ, 3000)
        self.tr.note_limit_hit("groq", GROQ, "rpm", unit="tokens")
        self.tr.note_limit_hit("groq", GROQ, "rpm", unit="unknown")
        self.assertEqual(self.tr.learned("groq", GROQ), {})

    def test_a_learned_per_minute_ceiling_expires(self):
        self.tr.reserve("gemini", GEM, 100)
        self.tr.note_limit_hit("gemini", GEM, "rpm")
        self.assertEqual(self.tr.learned("gemini", GEM), {"rpm": 1})
        self.clock.t += 11 * 60
        self.assertEqual(self.tr.learned("gemini", GEM), {})
        self.assertEqual(limits_for("gemini", GEM, tracker=self.tr).rpm, 5)       # back to the configured limit

    def test_a_learned_daily_ceiling_expires_at_the_daily_reset(self):
        for _ in range(7):
            self.tr.reserve("gemini", GEM, 100)
        self.tr.note_limit_hit("gemini", GEM, "daily")
        self.assertEqual(limits_for("gemini", GEM, tracker=self.tr).rpd, 7)
        self.clock.t += seconds_until_daily_reset("gemini", self.clock.t) + 1
        self.assertEqual(self.tr.learned("gemini", GEM), {})

    def test_after_expiry_a_tiny_request_fits_again(self):
        self.tr.reserve("groq", GROQ, 100)
        self.tr.note_limit_hit("groq", GROQ, "rpm")
        self.clock.t += 3600
        p = plan("groq", GROQ, "qa", "s", "tiny", tracker=self.tr, usage=UsageLog())
        self.assertTrue(p.fits(0.20))


class DailyReset(unittest.TestCase):
    def test_gemini_resets_at_midnight_pacific(self):
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Los_Angeles")
        now = dt.datetime(2026, 11, 19, 22, 30, tzinfo=tz).timestamp()
        self.assertAlmostEqual(seconds_until_daily_reset("gemini", now), 90 * 60, delta=1)

    def test_a_gemini_daily_limit_rests_until_the_reset_not_for_googles_few_seconds(self):
        from zoneinfo import ZoneInfo
        clock = Clock(dt.datetime(2026, 11, 19, 10, 0, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp())
        h = Health(clock)
        info = h.note_error("gemini", GEM, ApiError(429, GEM_DAY))
        self.assertEqual(info["kind"], "daily")
        self.assertAlmostEqual(info["cooldown_s"], 14 * 3600 + 60, delta=2)


class ClientsEndToEnd(unittest.TestCase):
    """Real clients, fake HTTP: the exact failure seen in production must not lock a provider out."""

    def setUp(self):
        H.reset_state()
        self.env = mock.patch.dict(os.environ, {"USAGE_LOG": "off", "GEMINI_MIN_INTERVAL": "0"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _post(self, *responses):
        seq = list(responses)
        return mock.patch.object(base.requests, "post", side_effect=lambda *a, **k: seq.pop(0))

    def test_repeated_groq_token_limits_leave_groq_usable(self):
        groq = GroqClient(api_key="gsk_test")
        with self._post(Resp(429, GROQ_TPM), Resp(429, GROQ_TPM)):
            for _ in range(2):
                with self.assertRaises(ApiError):
                    groq.generate("x", system="s", stage="qa", planned=True)
        self.assertEqual(budget.TRACKER.learned("groq", GROQ, groq.group), {})
        p = budget.plan("groq", GROQ, "qa", "s", "tiny", group=groq.group)
        self.assertTrue(p.fits(0.20))

    def test_a_first_request_gemini_rate_limit_does_not_disable_gemini(self):
        gem = GeminiClient(api_key="AIza_test")
        gem.model = GEM
        with self._post(Resp(429, GEM_RPM)):
            with self.assertRaises(ApiError):
                gem.generate("x", system="s", stage="generate", planned=True, model=GEM)
        self.assertEqual(budget.TRACKER.learned("gemini", GEM, gem.group), {})


class RetryInstructions(unittest.TestCase):
    def _run(self, *responses):
        seq, slept = list(responses), []
        with mock.patch.object(base.requests, "post", side_effect=lambda *a, **k: seq.pop(0)):
            try:
                base.post_json("u", {}, {}, retries=4, sleep=slept.append)
                return "ok", slept
            except ApiError as e:
                return e.status, slept

    def test_a_groq_daily_limit_is_not_retried(self):
        body = '{"error":{"message":"Rate limit reached on tokens per day (TPD): Limit 200000. Please try again in 7m12s."}}'
        self.assertEqual(self._run(*[Resp(429, body)] * 5), (429, []))

    def test_a_longer_retry_after_is_honoured(self):
        status, slept = self._run(Resp(429, "{}", {"retry-after": "58"}), Resp(200, "{}"))
        self.assertEqual(status, "ok")
        self.assertGreaterEqual(slept[0], 58)

    def test_googles_retry_delay_in_the_body_is_honoured(self):
        body = json.dumps({"error": {"details": [{"retryDelay": "35s"}]}})
        status, slept = self._run(Resp(429, body), Resp(200, "{}"))
        self.assertEqual(status, "ok")
        self.assertGreaterEqual(slept[0], 35)

    def test_a_wait_too_long_for_one_request_is_not_slept_on(self):
        self.assertEqual(self._run(Resp(429, "{}", {"retry-after": "600"})), (429, []))

    def test_overload_without_a_hint_keeps_the_bounded_backoff(self):
        status, slept = self._run(Resp(503, "{}"), Resp(503, "{}"), Resp(200, "{}"))
        self.assertEqual((status, slept), ("ok", [15, 30]))


if __name__ == "__main__":
    unittest.main()
