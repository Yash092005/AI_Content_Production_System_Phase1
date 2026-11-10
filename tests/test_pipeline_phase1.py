import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from tests import helpers as H
from app.ai import budget
from app.ai.base import CapabilityUnavailable
from app.ai.gemini_client import GeminiClient
from app.ai.groq_client import GroqClient
from app.ai.health import HEALTH
from app.ai.router import Router
from app.pipeline import orchestrator as O
from app.rules import loader as L

REQUEST = "Write a blog about Kashi Vishwanath temple darshan timings"
FULL_RULES = len(L.build_system_prompt(L.load_rules()))


def long_source(n=45):
    """A realistic page: a few facts among plenty of descriptive prose (~11.7k characters, under the 12,000 cap)."""
    return "\n".join(f"Section {i}\nFact: The temple performs ritual number {i} at 6:{i % 60:02d} AM every day.\n"
                     "This sacred place welcomes pilgrims from across the world with warmth, devotion and reverence.\n"
                     "Generations of families have walked these lanes seeking peace and blessings." for i in range(n))


class Base(unittest.TestCase):
    def setUp(self):
        H.reset_state()
        self.env = H.env_patch()
        self.env.start()
        self.addCleanup(self.env.stop)

    def stubs(self, g=None, q=None):
        self.g = g or H.Stub("gemini", "model-a")
        self.q = q or H.Stub("groq", "oss-120b", search=True)
        self.clients, self.router = H.make_clients(self.g, self.q)
        return self.clients

    def run_new(self, request=REQUEST, mode="New Content", **kw):
        return O.run_pipeline(request, mode, clients=self.stubs() if "clients" not in kw else kw.pop("clients"), **kw)


class Routing(Base):
    def test_research_goes_to_groq_search_and_everything_else_to_gemini(self):
        r = self.run_new()
        self.assertEqual(r["status"], "COMPLETE")
        self.assertEqual([c["stage"] for c in self.q.calls], ["research"])
        self.assertEqual([c["stage"] for c in self.g.calls], ["blueprint", "generate", "qa"])

    def test_no_strategy_call_for_an_explicit_request_and_call_count_stays_low(self):
        self.run_new()
        self.assertEqual(len(self.g.of("strategy")) + len(self.q.of("strategy")), 0)
        self.assertLessEqual(len(self.g.calls) + len(self.q.calls), 5)

    def test_an_ambiguous_request_spends_exactly_one_strategy_call(self):
        self.run_new("Kashi Vishwanath")
        self.assertEqual(len(self.g.of("strategy")) + len(self.q.of("strategy")), 1)

    def test_every_groq_request_keeps_the_required_headroom(self):
        self.run_new()
        for c in self.q.calls:
            total = self.q.est(c) + c["opts"]["max_tokens"] + 1000          # + provisional search overhead
            self.assertLessEqual(total, 8000 * 0.75, c["stage"])

    def test_groq_gets_the_narrow_excerpt_and_gemini_the_stage_excerpt(self):
        self.run_new()
        self.assertLess(len(self.q.calls[0]["system"]), FULL_RULES * 0.35)
        self.assertIn("excerpts below are the binding rules for this stage", self.q.calls[0]["system"])
        self.assertLess(len(self.g.of("generate")[0]["system"]), FULL_RULES)

    def test_scoping_off_sends_the_full_documents_everywhere(self):
        with mock.patch.dict(os.environ, {"RULES_SCOPING": "off"}):
            self.run_new()
        self.assertEqual(self.g.of("generate")[0]["system"], L.build_system_prompt(L.load_rules()))

    def test_groq_only_scoping_leaves_gemini_on_the_full_documents(self):
        with mock.patch.dict(os.environ, {"RULES_SCOPING": "groq"}):
            self.run_new()
        self.assertEqual(self.g.of("generate")[0]["system"], L.build_system_prompt(L.load_rules()))
        self.assertLess(len(self.q.of("research")[0]["system"]), FULL_RULES * 0.35)

    def test_single_provider_clients_keep_the_previous_flow(self):
        g = H.Stub("gemini", "model-a")
        clients = {k: g for k in ("gemini", "reasoning", "context", "research", "generation", "qa")}
        O.run_pipeline(REQUEST, "New Content", clients=clients)
        self.assertEqual([c["stage"] for c in g.calls], ["research_blueprint", "generate", "qa"])


class GroundingCapability(Base):
    def test_confirmed_grounding_allows_the_single_merged_call(self):
        HEALTH.mark_grounding("model-a", "k1", True)
        self.run_new()
        self.assertEqual(self.q.calls, [])
        rb = self.g.of("research_blueprint")
        self.assertEqual(len(rb), 1)
        self.assertTrue(rb[0]["opts"]["grounded"])
        self.assertEqual(self.g.of("research"), [])

    def test_unconfirmed_grounding_is_never_assumed(self):
        self.run_new()
        self.assertTrue(all(c["opts"].get("grounded") is not True for c in self.g.calls))


def verbatim_extraction(prompt, system, opts):
    """What a faithful model returns: the page's fact lines, word for word."""
    return "\n".join("Section :: " + l for l in prompt.splitlines() if l.startswith("Fact:"))


class Revamp(Base):
    def test_a_long_source_page_is_digested_by_gemini_and_nothing_is_dropped(self):
        src = long_source()
        self.assertGreater(len(src), 8000)
        g = H.Stub("gemini", "model-a", script={"extract": [verbatim_extraction]})
        r = self.run_new("Revamp the Kashi Vishwanath darshan page", "Revamp", source_text=src, clients=self.stubs(g=g))
        self.assertEqual(len(self.g.of("extract")), 1)
        research = self.q.of("research")[0]
        self.assertIn("SOURCE PAGE FACTS", research["prompt"])
        self.assertNotIn("SOURCE PAGE (existing content to revamp)", research["prompt"])
        for i in (0, 22, 44):                                    # first, middle and last facts are all present
            self.assertIn(f"ritual number {i} at", research["prompt"])
        self.assertLess(len(research["prompt"]), len(src))
        self.assertLessEqual(self.q.est(research) + research["opts"]["max_tokens"] + 1000, 8000 * 0.75)
        blueprint = self.g.of("blueprint")[0]
        self.assertIn(src, blueprint["prompt"])                  # Gemini still gets the complete page, byte for byte
        self.assertTrue(r["source_digest"]["accepted"])
        self.assertEqual(r["source_digest"]["model_lines_dropped_not_in_page"], 0)

    def test_an_untrustworthy_digest_is_never_used_the_full_page_is(self):
        src = long_source()
        bad = lambda prompt, system, opts: "Main :: The temple is open daily.\nMain :: Entry is free for everyone."     # not in the page
        g = H.Stub("gemini", "model-a", script={"extract": [bad]})
        r = self.run_new("Revamp the page", "Revamp", source_text=src, clients=self.stubs(g=g))
        self.assertFalse(r["source_digest"]["accepted"])
        self.assertTrue(r["source_digest"]["reasons"])
        for c in self.g.calls + self.q.calls:                    # no route ever saw a digest
            self.assertNotIn("SOURCE PAGE FACTS", c["prompt"])
        # the full page went to a route that can hold it (Groq cannot, so Gemini); nothing was silently shortened
        research_calls = self.g.of("research") + self.q.of("research")
        self.assertTrue(research_calls)
        for c in research_calls:
            self.assertIn(src, c["prompt"])

    def test_a_short_source_page_needs_no_extra_call(self):
        self.run_new("Revamp the page", "Revamp", source_text="Darshan\nThe temple opens at 5:30 AM.\nEntry is free.")
        self.assertEqual(self.g.of("extract"), [])
        self.assertIn("SOURCE PAGE (existing content to revamp)", self.q.of("research")[0]["prompt"])

    def test_when_the_digest_cannot_be_made_and_the_page_fits_no_safe_route_the_run_pauses_and_later_completes(self):
        src = long_source()
        g = H.Stub("gemini", "model-a", script={"extract": [H.api_error(429), verbatim_extraction]})
        r1 = self.run_new("Revamp the page", "Revamp", source_text=src, clients=self.stubs(g=g))
        # Gemini is limited (so no digest) and the full page does not fit Groq with headroom: pause, never shorten the evidence
        self.assertEqual(r1["status"], "PAUSED")
        self.assertEqual(r1["paused"]["stage"], "research")
        for c in self.g.calls + self.q.calls:
            self.assertNotIn("SOURCE PAGE FACTS", c["prompt"])
            if c["stage"] == "research":
                self.fail("research must not have run with an incomplete page")
        HEALTH.reset()                                           # Gemini recovers
        r2 = self.run_new("Revamp the page", "Revamp", source_text=src, clients=self.stubs(g=g))
        self.assertTrue(r2["source_digest"]["accepted"])
        self.assertIn(r2["status"], ("COMPLETE", "PARTIAL", "NEEDS_INPUT", "BLUEPRINT_FAILED", "READY"))


class QAIsComplete(Base):
    def test_qa_sees_the_whole_draft_and_every_claim(self):
        draft = ("The temple opens early and many devotees visit for darshan. " * 150) + " ENDMARKER"
        g = H.Stub("gemini", "model-a", script={"generate": [draft]})
        self.run_new(clients=self.stubs(g=g))
        qa = self.g.of("qa")[0]["prompt"]
        self.assertGreater(len(draft), 6000)
        self.assertIn("ENDMARKER", qa)
        self.assertNotIn("TRUNCATED", qa.upper())
        for i in range(16):
            self.assertIn(f"rule number {i} during darshan hours", qa)


class IncidentReplay(Base):
    """The reported failure through the REAL clients (only HTTP is stubbed): Gemini answers the grounded request with the
    429 that has no quota detail, and Groq has a 8K tokens/minute limit."""
    BODY = '{"error":{"code":429,"message":"You exceeded your current quota","status":"RESOURCE_EXHAUSTED","details":[{"@type":"type.googleapis.com/google.rpc.Help"}]}}'

    def real_clients(self, groq_fails=False):
        self.gemini_posts, self.groq_posts = [], []

        self.retry_args = []

        def gemini_post(url, payload, headers, retries=0, **kw):
            self.retry_args.append(("gemini", retries))
            self.gemini_posts.append(payload)
            if "tools" in payload:
                raise H.api_error(429, self.BODY)
            text = payload["contents"][0]["parts"][0]["text"]
            body = (H.blueprint_json() if "JSON blueprint" in text else H.WORDS)
            if "Audit this draft" in text:
                body = "PASS"
            return {"candidates": [{"content": {"parts": [{"text": body}]}, "finishReason": "STOP"}],
                    "usageMetadata": {"promptTokenCount": 1000, "candidatesTokenCount": 500, "totalTokenCount": 1500}}

        def groq_post(url, payload, headers, retries=0, meta=None, **kw):
            self.retry_args.append(("groq", retries))
            self.groq_posts.append(payload)
            if groq_fails:
                raise H.api_error(429, "Rate limit reached for model on tokens per minute (TPM): Limit 8000. Please try again in 9s")
            return {"choices": [{"message": {"content": H.research_json()}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 2200, "completion_tokens": 900, "total_tokens": 3100}}
        env = mock.patch.dict(os.environ, {"GEMINI_MIN_INTERVAL": "0", "GEMINI_MODEL": "gem-x", "GROQ_MODEL": "openai/gpt-oss-120b",
                                           "GROQ_RESEARCH_MODEL": "openai/gpt-oss-120b", "GROQ_BROWSER_SEARCH": "on",
                                           "GEMINI_GROUNDING": "on", "GEMINI_GROUNDING_CAPABILITY": "auto"})
        env.start()
        self.addCleanup(env.stop)
        gem = GeminiClient(api_key="AIzaFakeFakeFakeFakeFake12345")
        groq = GroqClient(api_key="gsk_fakefakefakefakefake123456")
        generous = mock.patch("app.ai.budget.GEMINI_LIMITS", [{"model": "gem-x", "group": "unverified", "rpm": 1000, "tpm": 50_000_000, "rpd": 100000}])
        generous.start()
        self.addCleanup(generous.stop)
        p1 = mock.patch("app.ai.gemini_client.post_json", gemini_post)
        p2 = mock.patch("app.ai.groq_client.post_json", groq_post)
        p1.start(), p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)
        # NOTE: the clients keep their default retries (4). The router's calls must still use 0 on their own.
        self.assertEqual((gem.retries, groq.retries), (4, 4))
        router = Router(gem, groq, sleep=lambda s: None)
        return {k: router for k in ("gemini", "reasoning", "context", "research", "generation", "qa")}

    def test_the_run_completes_and_gemini_grounding_is_never_requested(self):
        clients = self.real_clients()
        r = O.run_pipeline(REQUEST, "New Content", clients=clients)
        self.assertEqual(r["status"], "COMPLETE")
        self.assertEqual([p for p in self.gemini_posts if "tools" in p], [])
        self.assertEqual(len(self.groq_posts), 1)
        self.assertTrue(self.retry_args and all(n == 0 for _, n in self.retry_args), self.retry_args)   # whole run, both providers
        self.assertIn({"type": "browser_search"}, self.groq_posts[0]["tools"])
        self.assertEqual(r["qa"]["status"], "PASS")

    def test_when_groq_is_limited_grounding_is_probed_once_and_never_repeated(self):
        clients = self.real_clients(groq_fails=True)
        r1 = O.run_pipeline(REQUEST, "New Content", clients=clients)
        r2 = O.run_pipeline(REQUEST + " today", "New Content", clients=clients)
        grounded = [p for p in self.gemini_posts if "tools" in p]
        self.assertEqual(len(grounded), 1)                                    # the single probe, across both runs
        self.assertEqual(HEALTH.grounding_state("gem-x", GeminiClient(api_key="AIzaFakeFakeFakeFakeFake12345").key_id), "unavailable")
        self.assertTrue(any("WITHOUT web search" in e for e in r1["fallbacks"]))
        self.assertIn(r2["status"], ("COMPLETE", "PARTIAL"))

    def test_usage_is_recorded_for_every_call(self):
        clients = self.real_clients()
        O.run_pipeline(REQUEST, "New Content", clients=clients)
        rows = H.LOG.rows()
        self.assertGreaterEqual(len(rows), 4)
        self.assertEqual({r["provider"] for r in rows}, {"gemini", "groq"})
        self.assertTrue(all(r["ok"] for r in rows))
        self.assertTrue(any(r.get("search") and r["provider"] == "groq" for r in rows))
        self.assertNotIn("AIza", json.dumps(rows))


class PauseAndResume(Base):
    def setUp(self):
        super().setUp()
        self.disk = tempfile.mkdtemp()
        self.cache = mock.patch.dict(os.environ, {"PIPELINE_CACHE": "true", "CACHE_DIR": self.disk})
        self.cache.start()
        self.addCleanup(self.cache.stop)

    def test_no_safe_provider_pauses_instead_of_crashing_and_resumes_from_memory(self):
        g = H.Stub("gemini", "model-a", script={"generate": [H.api_error(429)]})
        q = H.Stub("groq", "oss-120b", search=True)
        stage_cache = {}
        r1 = O.run_pipeline(REQUEST, "New Content", clients=self.stubs(g, q), stage_cache=stage_cache)
        self.assertEqual(r1["status"], "PAUSED")
        self.assertEqual(r1["paused"]["stage"], "generate")
        self.assertTrue(r1["blueprint"])                                     # the work so far is kept
        self.assertEqual(q.of("generate"), [])                               # Groq could not take it with headroom: not tried
        self.assertTrue(stage_cache)

        HEALTH.reset()                                                       # time passes, Gemini recovers
        shutil.rmtree(self.disk, ignore_errors=True)                         # ...and the server's disk was wiped by a restart
        r2 = O.run_pipeline(REQUEST, "New Content", clients=self.stubs(g, q), stage_cache=stage_cache)
        self.assertEqual(r2["status"], "COMPLETE")
        self.assertEqual(len(q.of("research")), 1)                           # research was NOT paid for again
        self.assertEqual(len(g.of("blueprint")), 1)

    def test_a_paused_result_has_everything_the_screen_needs(self):
        g = H.Stub("gemini", "model-a", default=lambda *a: H.api_error(429))
        r = O.run_pipeline(REQUEST, "New Content", clients=self.stubs(g), stage_cache={})
        self.assertEqual(r["status"], "PAUSED")
        for key in ("paused", "qa", "draft", "blueprint", "fallbacks"):
            self.assertIn(key, r)
        self.assertIn("message", r["paused"])


if __name__ == "__main__":
    unittest.main()
