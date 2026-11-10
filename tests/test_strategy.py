import os
import tempfile
import unittest
from unittest import mock

from tests import helpers as H
from app.strategy import predict


class Counting:
    live = True

    def __init__(self, reply='{"content_type": "Blog / Informational", "audience": "First-time visitor"}'):
        self.calls, self.reply = 0, reply

    def generate(self, prompt, system=None, stage=None, **kw):
        self.calls += 1
        return self.reply


def run(request, mode="New Content", setting="ai"):
    c = Counting()
    with mock.patch.dict(os.environ, {"STRATEGY_PREDICT": setting, "CACHE_DIR": tempfile.mkdtemp()}):
        fields, source = predict.predict_fields(request, mode, c)
    return c.calls, source


class Confidence(unittest.TestCase):
    def test_explicit_content_types_are_confident(self):
        for text in ("Write a blog about darshan", "homepage for Kashi", "a landing page", "CTA for booking", "an article on rituals"):
            self.assertTrue(predict.heuristic_confident(text), text)

    def test_vague_requests_are_not(self):
        for text in ("Kashi", "something about the temple", "help me with content"):
            self.assertFalse(predict.heuristic_confident(text), text)

    def test_a_revamp_is_always_confident(self):
        self.assertTrue(predict.heuristic_confident("Kashi", "Revamp"))


class CallDiscipline(unittest.TestCase):
    def test_default_asks_the_model_only_when_the_request_is_ambiguous(self):
        self.assertEqual(run("Write a blog about Kashi Vishwanath darshan timings"), (0, "heuristic"))
        calls, source = run("Kashi Vishwanath")
        self.assertEqual((calls, source), (1, "ai"))

    def test_revamp_never_spends_a_call_by_default(self):
        self.assertEqual(run("Kashi", "Revamp")[0], 0)

    def test_always_ai_keeps_the_previous_behaviour(self):
        self.assertEqual(run("Write a blog about Kashi", setting="always_ai"), (1, "ai"))

    def test_heuristic_never_asks(self):
        self.assertEqual(run("Kashi Vishwanath", setting="heuristic")[0], 0)

    def test_a_bad_model_reply_falls_back_to_the_keywords(self):
        c = Counting(reply="not json at all")
        with mock.patch.dict(os.environ, {"STRATEGY_PREDICT": "always_ai", "CACHE_DIR": tempfile.mkdtemp()}):
            fields, source = predict.predict_fields("Kashi", "New Content", c)
        self.assertEqual(source, "heuristic")
        self.assertTrue(fields)


if __name__ == "__main__":
    unittest.main()
