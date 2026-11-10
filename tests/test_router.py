import os
import unittest
from unittest import mock

from tests import helpers as H
from app.ai import budget
from app.ai.base import ProvidersUnavailable, CapabilityUnavailable
from app.ai.budget import Tracker
from app.ai.health import Health
from app.ai.router import Router
from app.metrics.usage import UsageLog


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


class RouterBase(unittest.TestCase):
    def setUp(self):
        H.reset_state()
        self.clock = Clock()
        self.health = Health(self.clock)
        self.tracker = Tracker(self.clock)
        self.sleeps = []
        self.env = mock.patch.dict(os.environ, {"GEMINI_GROUNDING": "on", "GEMINI_GROUNDING_CAPABILITY": "auto"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def router(self, gemini, groq):
        return Router(gemini, groq, health=self.health, tracker=self.tracker, usage=UsageLog(),
                      sleep=lambda s: (self.sleeps.append(s), setattr(self.clock, "t", self.clock.t + s)))


class Order(RouterBase):
    def test_reading_and_writing_stages_go_to_gemini_first(self):
        g, q = H.Stub("gemini"), H.Stub("groq", search=True)
        self.router(g, q).generate("p" * 4000, system="s" * 4000, stage="generate")
        self.assertEqual((len(g.calls), len(q.calls)), (1, 0))

    def test_research_goes_to_groq_search_when_gemini_grounding_is_not_confirmed(self):
        g, q = H.Stub("gemini"), H.Stub("groq", search=True)
        self.router(g, q).generate("p" * 1000, system="s" * 2000, stage="research")
        self.assertEqual((len(g.calls), len(q.calls)), (0, 1))

    def test_research_uses_gemini_grounding_only_when_it_is_confirmed(self):
        self.health.mark_grounding("model-a", "k1", True)
        g, q = H.Stub("gemini"), H.Stub("groq", search=True)
        self.router(g, q).generate("p" * 1000, system="s" * 2000, stage="research")
        self.assertEqual(len(g.calls), 1)
        self.assertTrue(g.calls[0]["opts"]["grounded"])
        self.assertEqual(len(q.calls), 0)

    def test_grounding_switched_off_never_reaches_gemini_grounded(self):
        with mock.patch.dict(os.environ, {"GEMINI_GROUNDING": "off"}):
            g, q = H.Stub("gemini"), H.Stub("groq", search=True, script={"research": [H.api_error(429)]})
            out = self.router(g, q).generate("p" * 500, system="s" * 500, stage="research")
        # groq failed, so the flagged degraded route (Gemini without search) served it; grounded was never asked for
        self.assertTrue(all(c["opts"].get("grounded") is False for c in g.calls))
        self.assertEqual(len(g.calls), 1)
        self.assertIsNotNone(out)

    def test_an_unconfirmed_grounding_probe_happens_only_after_groq_search_cannot_serve(self):
        g = H.Stub("gemini", script={"research": [CapabilityUnavailable("gemini", "grounding"), "plain research text"]})
        q = H.Stub("groq", search=True, script={"research": [H.api_error(429)]})
        r = self.router(g, q)
        out = r.generate("p" * 500, system="s" * 500, stage="research")
        modes = [c["opts"].get("grounded") for c in g.calls]
        self.assertEqual(modes, [True, False])           # one probe, then the plain (degraded) route
        self.assertEqual(len(q.calls), 1)
        self.assertEqual(out, "plain research text")
        self.assertTrue(any("WITHOUT web search" in e for e in r.events))


class Fallbacks(RouterBase):
    def test_a_limited_provider_is_skipped_by_the_next_request(self):
        g = H.Stub("gemini", script={"qa": [H.api_error(429)]})
        q = H.Stub("groq", search=True)
        r = self.router(g, q)
        r.generate("p" * 800, system="s" * 800, stage="qa")           # gemini 429 -> groq serves it
        self.assertEqual((len(g.calls), len(q.calls)), (1, 1))
        r.generate("p" * 800, system="s" * 800, stage="qa")           # gemini is resting: not called again
        self.assertEqual(len(g.calls), 1)
        self.assertEqual(len(q.calls), 2)

    def test_all_routes_failing_raises_providers_unavailable_with_reasons_and_a_wait(self):
        g = H.Stub("gemini", default=lambda *a: H.api_error(429))
        q = H.Stub("groq", search=True, default=lambda *a: H.api_error(429))
        with self.assertRaises(ProvidersUnavailable) as cm:
            self.router(g, q).generate("p" * 800, system="s" * 800, stage="qa")
        self.assertEqual(cm.exception.stage, "qa")
        self.assertGreaterEqual(len(cm.exception.reasons), 2)
        self.assertIsNotNone(cm.exception.retry_after)

    def test_an_empty_reply_is_not_accepted(self):
        g = H.Stub("gemini", script={"qa": [""]})
        q = H.Stub("groq", search=True)
        self.assertEqual(self.router(g, q).generate("p" * 500, system="s" * 500, stage="qa"), "PASS")
        self.assertEqual(len(q.calls), 1)

    def test_a_bad_request_is_raised_not_routed_around(self):
        g = H.Stub("gemini", script={"qa": [H.api_error(401, "API key not valid")]})
        q = H.Stub("groq", search=True)
        with self.assertRaises(Exception) as cm:
            self.router(g, q).generate("p" * 500, system="s" * 500, stage="qa")
        self.assertNotIsInstance(cm.exception, ProvidersUnavailable)
        self.assertEqual(len(q.calls), 0)

    def test_success_clears_a_providers_rest(self):
        g = H.Stub("gemini", script={"qa": [H.api_error(503, "busy")]})
        q = H.Stub("groq", search=True)
        r = self.router(g, q)
        r.generate("p" * 500, system="s" * 500, stage="qa")
        self.clock.t += 60
        r.generate("p" * 500, system="s" * 500, stage="qa")
        self.assertEqual(len(g.calls), 2)


class Headroom(RouterBase):
    def test_a_request_below_the_floor_is_never_sent_to_groq(self):
        # needs ~8.1k tokens in + out: Groq has no headroom, Gemini is down -> pause, do not send
        g = H.Stub("gemini", default=lambda *a: H.api_error(429))
        q = H.Stub("groq", search=True)
        with self.assertRaises(ProvidersUnavailable):
            self.router(g, q).generate("p" * 14000, system="s" * 14000, stage="generate", target_words=900)
        self.assertEqual(len(q.calls), 0)

    def test_every_call_that_is_made_keeps_the_required_headroom(self):
        g = H.Stub("gemini", default=lambda *a: H.api_error(429))
        q = H.Stub("groq", search=True)
        r = self.router(g, q)
        r.generate("p" * 3000, system="s" * 4000, stage="qa")
        call = q.calls[0]
        total = q.est(call) + call["opts"]["max_tokens"]
        self.assertLessEqual(total, 8000 * 0.75)

    def test_the_output_cap_is_enforced_and_marked_planned(self):
        g, q = H.Stub("gemini"), H.Stub("groq", search=True)
        self.router(g, q).generate("p" * 500, system="s" * 500, stage="qa")
        self.assertGreater(g.calls[0]["opts"]["max_tokens"], 0)
        self.assertTrue(g.calls[0]["opts"]["planned"])

    def test_waits_for_the_minute_window_instead_of_giving_up(self):
        self.tracker.reserve("groq", "model-a", 3200)
        self.clock.t += 5
        g = H.Stub("gemini")
        q = H.Stub("groq", search=True)
        # research can only use groq search; 1.2k in + 2k cap + 1k tool = 4.2k; window holds 3.2k -> 7.4k of 8k: wait
        self.router(g, q).generate("p" * 1500, system="s" * 3300, stage="research")
        self.assertEqual(len(self.sleeps), 1)
        self.assertLessEqual(self.sleeps[0], 75)
        self.assertEqual(len(q.calls), 1)

    def test_it_does_not_wait_longer_than_the_configured_maximum(self):
        self.tracker.reserve("groq", "model-a", 7900)
        g = H.Stub("gemini", default=lambda *a: H.api_error(429))
        q = H.Stub("groq", search=True)
        with mock.patch.dict(os.environ, {}), self.assertRaises(ProvidersUnavailable):
            self.router(g, q).generate("p" * 3000, system="s" * 9000, stage="research")
        self.assertEqual(self.sleeps, [])


class Alternatives(RouterBase):
    def test_the_full_prompt_is_preferred_when_it_fits(self):
        g, q = H.Stub("gemini"), H.Stub("groq", search=True)
        self.router(g, q).generate("FULL", system="s", stage="qa", alt_prompts=["ALT"])
        self.assertEqual(g.calls[0]["prompt"], "FULL")

    def test_an_alternative_is_used_only_when_the_full_prompt_fits_nowhere(self):
        g = H.Stub("gemini", default=lambda *a: H.api_error(429))
        q = H.Stub("groq", search=True)
        full, alt = "p" * 30000, "p" * 3000
        self.router(g, q).generate(full, system="s" * 2000, stage="qa", alt_prompts=[alt])
        self.assertEqual(q.calls[0]["prompt"], alt)

    def test_groq_gets_its_narrower_rules_gemini_keeps_the_full_text(self):
        g = H.Stub("gemini", default=lambda *a: H.api_error(429))
        q = H.Stub("groq", search=True)
        self.router(g, q).generate("p" * 500, system="FULL RULES" * 400, stage="qa", groq_system="NARROW")
        self.assertTrue(g.calls[0]["system"].startswith("FULL RULES"))
        self.assertEqual(q.calls[0]["system"], "NARROW")


class Preview(RouterBase):
    def test_preview_reports_whether_the_preferred_route_fits(self):
        g, q = H.Stub("gemini"), H.Stub("groq", search=True)
        r = self.router(g, q)
        small = r.preview("research", "s" * 3000, "p" * 1500)
        big = r.preview("research", "s" * 3000, "p" * 20000)
        self.assertTrue(small["fits"])
        self.assertIn("groq", small["candidate"])
        self.assertFalse(big["fits"])
        self.assertEqual(len(g.calls) + len(q.calls), 0)           # a preview never calls anything


if __name__ == "__main__":
    unittest.main()
