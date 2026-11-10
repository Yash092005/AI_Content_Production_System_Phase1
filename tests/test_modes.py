"""Single-provider and demo modes keep working (they bypass the router and keep the earlier trimming)."""
import os
import unittest
from unittest import mock

from tests import helpers as H
from app.ai import get_clients
from app.ai.gemini_client import GeminiClient
from app.ai.groq_client import GroqClient
from app.ai.mock_client import MockClient
from app.ai.pool import KeyPool
from app.ai.router import Router
from app.pipeline import orchestrator as O

REQUEST = "Write a blog about Kashi Vishwanath temple darshan timings"
GEM = "AIzaFakeFakeFakeFakeFake12345"
GRQ = "gsk_fakefakefakefakefake123456"


class Modes(unittest.TestCase):
    def setUp(self):
        H.reset_state()
        self.env = H.env_patch(GEMINI_MIN_INTERVAL="0", GEMINI_MODEL="gem-x")
        self.env.start()
        self.addCleanup(self.env.stop)

    def kinds(self, **keys):
        c = get_clients(env=keys)
        return {type(v).__name__ for v in c.values()}, c

    def test_no_keys_gives_the_demo_clients_and_a_complete_run(self):
        kinds, c = self.kinds()
        self.assertEqual(kinds, {"MockClient"})
        r = O.run_pipeline(REQUEST, "New Content", clients=c)
        self.assertEqual(r["status"], "MOCK_RUN")          # same as before the change
        self.assertFalse(r.get("live", True))

    def test_both_keys_route_through_the_router(self):
        kinds, _ = self.kinds(GEMINI_API_KEY=GEM, GROQ_API_KEY=GRQ)
        self.assertEqual(kinds, {"Router"})

    def test_one_provider_is_used_directly(self):
        kinds, _ = self.kinds(GEMINI_API_KEY=GEM)
        self.assertTrue(kinds <= {"GeminiClient", "KeyPool"})
        kinds, _ = self.kinds(GROQ_API_KEY=GRQ)
        self.assertTrue(kinds <= {"GroqClient", "KeyPool"})

    def test_gemini_only_mode_runs_end_to_end_on_the_legacy_path(self):
        posts = []

        def post(url, payload, headers, retries=0, **kw):
            posts.append(payload)
            text = payload["contents"][0]["parts"][0]["text"]
            body = H.blueprint_json() if "JSON blueprint" in text else ("PASS" if "Audit this draft" in text else H.WORDS)
            return {"candidates": [{"content": {"parts": [{"text": body}]}, "finishReason": "STOP"}],
                    "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 10, "totalTokenCount": 20}}
        _, clients = self.kinds(GEMINI_API_KEY=GEM)
        with mock.patch("app.ai.gemini_client.post_json", post), mock.patch.dict(os.environ, {"GEMINI_GROUNDING_CAPABILITY": "auto"}):
            r = O.run_pipeline(REQUEST, "New Content", clients=clients)
        self.assertEqual(r["status"], "COMPLETE")
        grounded = [p for p in posts if "tools" in p]
        self.assertLessEqual(len(grounded), 1)                  # a single probe at most, with no router involved

    def test_gemini_only_mode_never_repeats_a_failed_grounded_request(self):
        body = '{"error":{"code":429,"message":"You exceeded your current quota","status":"RESOURCE_EXHAUSTED"}}'
        posts = []

        def post(url, payload, headers, retries=0, **kw):
            posts.append(payload)
            if "tools" in payload:
                raise H.api_error(429, body)
            text = payload["contents"][0]["parts"][0]["text"]
            out = H.blueprint_json() if "JSON blueprint" in text else ("PASS" if "Audit this draft" in text else H.WORDS)
            return {"candidates": [{"content": {"parts": [{"text": out}]}, "finishReason": "STOP"}]}
        _, clients = self.kinds(GEMINI_API_KEY=GEM)
        with mock.patch("app.ai.gemini_client.post_json", post):
            for _ in range(3):
                O.run_pipeline(REQUEST, "New Content", clients=clients)
        self.assertEqual(len([p for p in posts if "tools" in p]), 1)


if __name__ == "__main__":
    unittest.main()


class ProviderTestButton(unittest.TestCase):
    def test_a_shared_client_is_tested_once_not_once_per_purpose(self):
        from app.config import health
        g, q = H.Stub("gemini"), H.Stub("groq", search=True)
        router = Router(g, q)
        clients = {k: router for k in ("gemini", "reasoning", "context", "research", "generation", "qa")}
        rows = health.test_providers(settings=None, clients=clients) if "settings" in health.test_providers.__code__.co_varnames else health.test_providers(clients=clients)
        self.assertEqual(len(rows), 6)
        self.assertEqual(len(g.calls) + len(q.calls), 1)
