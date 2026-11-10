"""The retry count that actually reaches the HTTP layer.

Routed calls (planned=True, what the Router sends) must use 0 internal retries: a limited key is routed around, never waited for.
Direct / unrouted calls keep the client's own retry setting exactly as before. Grounded Gemini requests are a one-shot probe in both
cases (an unsupported request is never retried). Every request path of both clients is covered."""
import os
import unittest
from unittest import mock

from tests import helpers as H
from app.ai import get_clients
from app.ai.base import ApiError
from app.ai.gemini_client import GeminiClient
from app.ai.groq_client import GroqClient
from app.ai.router import Router

GEMINI_OK = {"candidates": [{"content": {"parts": [{"text": "hello"}]}, "finishReason": "STOP"}],
             "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15}}
GROQ_OK = {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}], "usage": {"total_tokens": 10}}
GEM, GRQ = "AIzaFakeFakeFakeFakeFake12345", "gsk_fakefakefakefakefake123456"


class Capture:
    """Records the `retries` argument of every post_json call (and returns/raises what it is told to)."""

    def __init__(self, gemini=GEMINI_OK, groq=GROQ_OK, groq_script=()):
        self.calls, self.gemini, self.groq, self.groq_script = [], gemini, groq, list(groq_script)

    def gemini_post(self, url, payload, headers, retries=None, **kw):
        self.calls.append(("gemini", retries, "tools" in payload, url.rsplit("/", 1)[-1].split(":")[0]))
        if isinstance(self.gemini, Exception):
            raise self.gemini
        return self.gemini

    def groq_post(self, url, payload, headers, retries=None, meta=None, **kw):
        self.calls.append(("groq", retries, "reasoning_effort" in payload, payload["model"]))
        if self.groq_script:
            out = self.groq_script.pop(0)
            if isinstance(out, Exception):
                raise out
        return self.groq

    def patch(self):
        return [mock.patch("app.ai.gemini_client.post_json", self.gemini_post), mock.patch("app.ai.groq_client.post_json", self.groq_post)]

    def retries(self, provider=None):
        return [r for p, r, *_ in self.calls if provider in (None, p)]


class Base(unittest.TestCase):
    def setUp(self):
        H.reset_state()
        self.env = mock.patch.dict(os.environ, {"GEMINI_MIN_INTERVAL": "0", "GEMINI_MODEL": "gem-x", "GEMINI_GROUNDING": "on",
                                                "GEMINI_GROUNDING_CAPABILITY": "auto", "GROQ_BROWSER_SEARCH": "on",
                                                "GROQ_MODEL": "openai/gpt-oss-120b", "GROQ_RESEARCH_MODEL": "openai/gpt-oss-120b",
                                                "GROQ_REASONING_EFFORT": "low"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.cap = Capture()
        for p in self.cap.patch():
            p.start()
            self.addCleanup(p.stop)

    def gemini(self, retries=None):
        c = GeminiClient(api_key=GEM)
        if retries is not None:
            c.retries = retries
        return c

    def groq(self, retries=None):
        c = GroqClient(api_key=GRQ)
        if retries is not None:
            c.retries = retries
        return c


class GeminiRetries(Base):
    def test_defaults_are_unchanged(self):
        self.assertEqual((self.gemini().retries, self.groq().retries), (4, 4))

    def test_a_planned_plain_call_uses_zero_retries(self):
        self.gemini().generate("q", system="s", stage="qa", planned=True, max_tokens=800)
        self.assertEqual(self.cap.retries(), [0])

    def test_an_unplanned_plain_call_keeps_the_clients_retries(self):
        self.gemini().generate("q", system="s", stage="qa")
        self.assertEqual(self.cap.retries(), [4])

    def test_the_clients_own_setting_is_what_a_direct_call_uses(self):
        for n in (0, 1, 2, 7):
            self.cap.calls.clear()
            self.gemini(retries=n).generate("q", stage="qa")
            self.assertEqual(self.cap.retries(), [n])

    def test_a_planned_call_ignores_a_high_client_setting(self):
        self.gemini(retries=9).generate("q", stage="qa", planned=True)
        self.assertEqual(self.cap.retries(), [0])

    def test_planned_ungrounded_research_uses_zero_retries(self):
        self.gemini().generate("q", stage="research", planned=True, grounded=False, max_tokens=2000)
        self.assertEqual(self.cap.calls[0][1:3], (0, False))           # zero retries, no search tool

    def test_unplanned_ungrounded_research_keeps_the_clients_retries(self):
        with mock.patch.dict(os.environ, {"GEMINI_GROUNDING": "off"}):
            self.gemini().generate("q", stage="research")
        self.assertEqual(self.cap.calls[0][1:3], (4, False))

    def test_a_grounded_request_is_a_single_attempt_planned_or_not(self):
        self.gemini().generate("q", stage="research", planned=True, grounded=True)
        self.assertEqual(self.cap.calls[-1][1:3], (0, True))
        H.reset_state()
        self.cap.calls.clear()
        self.gemini().generate("q", stage="research")                  # direct call: also a one-shot grounded probe
        self.assertEqual(self.cap.calls[0][1:3], (0, True))

    def test_the_plain_call_after_a_failed_direct_grounded_probe_keeps_the_clients_retries(self):
        self.cap.gemini = H.api_error(429, '{"error":{"code":429,"message":"You exceeded your current quota","status":"RESOURCE_EXHAUSTED"}}')
        outcomes = [self.cap.gemini, GEMINI_OK]

        def post(url, payload, headers, retries=None, **kw):
            self.cap.calls.append(("gemini", retries, "tools" in payload, "m"))
            out = outcomes.pop(0)
            if isinstance(out, Exception):
                raise out
            return out
        with mock.patch("app.ai.gemini_client.post_json", post):
            self.gemini().generate("q", stage="research")
        self.assertEqual([c[1:3] for c in self.cap.calls], [(0, True), (4, False)])

    def test_the_fallback_model_attempt_uses_the_same_retries(self):
        calls = []

        def post(url, payload, headers, retries=None, **kw):
            calls.append(retries)
            if len(calls) == 1:
                raise H.api_error(503, "busy")
            return GEMINI_OK
        c = self.gemini()
        c.fallback = "gem-fallback"
        with mock.patch("app.ai.gemini_client.post_json", post):
            c.generate("q", stage="qa")                               # direct: legacy fallback model, legacy retries
        self.assertEqual(calls, [4, 4])

    def test_a_planned_call_has_no_hidden_fallback_model_and_no_retries(self):
        calls = []

        def post(url, payload, headers, retries=None, **kw):
            calls.append(retries)
            raise H.api_error(503, "busy")
        c = self.gemini()
        c.fallback = "gem-fallback"
        with mock.patch("app.ai.gemini_client.post_json", post), self.assertRaises(ApiError):
            c.generate("q", stage="qa", planned=True, model="gem-x", max_tokens=800)
        self.assertEqual(calls, [0])


class GroqRetries(Base):
    def test_planned_reasoning_path_uses_zero_retries(self):
        self.groq().generate("q", stage="qa", planned=True, max_tokens=800)
        self.assertEqual(self.cap.calls[0][1:3], (0, True))              # zero retries, reasoning_effort sent

    def test_planned_non_reasoning_path_uses_zero_retries(self):
        with mock.patch.dict(os.environ, {"GROQ_REASONING_EFFORT": ""}):
            self.groq().generate("q", stage="qa", planned=True, max_tokens=800)
        self.assertEqual(self.cap.calls[0][1:3], (0, False))

    def test_planned_non_gpt_oss_model_takes_the_non_reasoning_path_with_zero_retries(self):
        with mock.patch.dict(os.environ, {"GROQ_MODEL": "llama-x"}):
            self.groq().generate("q", stage="qa", planned=True, max_tokens=800)
        self.assertEqual(self.cap.calls[0][1:3], (0, False))

    def test_planned_search_research_uses_zero_retries(self):
        self.groq().generate("q", stage="research", planned=True, max_tokens=2000)
        self.assertEqual(self.cap.retries(), [0])

    def test_unplanned_reasoning_path_keeps_the_clients_retries(self):
        self.groq().generate("q", stage="qa")
        self.assertEqual(self.cap.calls[0][1:3], (4, True))

    def test_unplanned_non_reasoning_path_keeps_the_clients_retries(self):
        with mock.patch.dict(os.environ, {"GROQ_REASONING_EFFORT": ""}):
            self.groq().generate("q", stage="qa")
        self.assertEqual(self.cap.calls[0][1:3], (4, False))

    def test_the_clients_own_setting_is_what_a_direct_call_uses(self):
        for n in (0, 2, 6):
            self.cap.calls.clear()
            self.groq(retries=n).generate("q", stage="qa")
            self.assertEqual(self.cap.retries(), [n])

    def test_a_planned_call_ignores_a_high_client_setting(self):
        self.groq(retries=9).generate("q", stage="qa", planned=True)
        self.assertEqual(self.cap.retries(), [0])

    def test_when_groq_rejects_the_reasoning_setting_both_attempts_use_the_same_retries(self):
        self.cap.groq_script = [H.api_error(400, "reasoning_effort not supported"), None]
        self.groq().generate("q", stage="qa", planned=True, max_tokens=800)
        self.assertEqual([c[1:3] for c in self.cap.calls], [(0, True), (0, False)])
        self.cap.calls.clear()
        self.cap.groq_script = [H.api_error(400, "reasoning_effort not supported"), None]
        self.groq().generate("q", stage="qa")
        self.assertEqual([c[1:3] for c in self.cap.calls], [(4, True), (4, False)])


class ThroughTheRouter(Base):
    def router(self, gem=None, groq=None):
        return Router(gem or self.gemini(), groq or self.groq(), sleep=lambda s: None)

    def test_constructing_a_router_does_not_change_the_clients_retry_setting(self):
        g, q = self.gemini(), self.groq()
        self.router(g, q)
        self.assertEqual((g.retries, q.retries), (4, 4))

    def test_every_routed_request_on_every_path_carries_zero_retries(self):
        r = self.router()
        for stage in ("qa", "generate", "blueprint", "research", "strategy", "extract"):
            r.generate("p" * 600, system="s" * 600, stage=stage)
        self.assertTrue(self.cap.calls)
        self.assertEqual(set(self.cap.retries()), {0}, self.cap.calls)
        self.assertEqual({p for p, *_ in self.cap.calls}, {"gemini", "groq"})      # both providers were exercised

    def test_a_routed_failure_is_not_retried_inside_the_client(self):
        self.cap.gemini = H.api_error(429, "quota")
        self.cap.groq = GROQ_OK
        r = self.router()
        r.generate("p" * 600, system="s" * 600, stage="qa")             # gemini 429 -> next route
        self.assertEqual(self.cap.retries("gemini"), [0])                # one HTTP attempt, no 15-150 s sleeps

    def test_direct_calls_through_the_same_client_objects_keep_their_retries_after_routing(self):
        g, q = self.gemini(), self.groq()
        self.router(g, q).generate("p" * 600, system="s" * 600, stage="qa")
        self.cap.calls.clear()
        g.generate("q", stage="qa")
        q.generate("q", stage="qa")
        self.assertEqual(self.cap.retries(), [4, 4])

    def test_through_get_clients_routed_calls_use_zero_and_the_pools_keep_their_settings(self):
        env = {"GEMINI_API_KEY": GEM, "GROQ_API_KEY": GRQ, "GEMINI_API_KEY_QA": GEM + "2", "GROQ_API_KEY_QA": GRQ + "2"}
        router = get_clients(env=env)["qa"]
        self.assertTrue(all(c.retries == 4 for pool in (router.gemini, router.groq) for c in pool.clients))
        router.generate("p" * 600, system="s" * 600, stage="qa")
        self.assertEqual(set(self.cap.retries()), {0})
        self.cap.calls.clear()
        router.gemini.generate("direct", stage="qa")                     # the pool used directly: legacy behaviour
        self.assertEqual(self.cap.retries(), [4])

    def test_the_router_reports_zero_retries(self):
        self.assertEqual(self.router().retries, 0)


if __name__ == "__main__":
    unittest.main()
