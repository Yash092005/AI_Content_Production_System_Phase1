import json
import tempfile
import unittest
from pathlib import Path

from tests import helpers  # noqa: F401
from app.metrics.usage import UsageLog, redact


class Usage(unittest.TestCase):
    def test_keys_never_reach_the_log(self):
        log = UsageLog()
        row = log.record(provider="gemini", stage="qa", ok=False, api_key="AIzaSyDUMMYDUMMYDUMMYDUMMYDUMMY12345",
                         error="403 key AIzaSyDUMMYDUMMYDUMMYDUMMYDUMMY12345 and Bearer gsk_abcdefghijklmnopqrstuv")
        text = json.dumps(row)
        self.assertNotIn("AIza", text)
        self.assertNotIn("gsk_", text)
        self.assertNotIn("api_key", row)
        self.assertIn("[redacted]", row["error"])

    def test_only_known_fields_are_stored(self):
        row = UsageLog().record(provider="groq", prompt="SECRET PROMPT", reply="SECRET REPLY", stage="qa")
        self.assertNotIn("prompt", row)
        self.assertNotIn("reply", row)

    def test_total_is_derived_when_missing(self):
        row = UsageLog().record(provider="groq", prompt_tokens=100, completion_tokens=40)
        self.assertEqual(row["total_tokens"], 140)

    def test_summary_shares_add_up(self):
        log = UsageLog()
        log.record(provider="gemini", stage="generate", ok=True, total_tokens=600)
        log.record(provider="groq", stage="research", ok=True, total_tokens=300, cached_tokens=100)
        log.record(provider="groq", stage="research", ok=False, est_in=100, out_cap=0)
        s = log.summary()
        self.assertEqual(s["calls"], 3)
        self.assertAlmostEqual(sum(v["share"] for v in s["by_provider"].values()), 100, delta=0.2)
        self.assertEqual(s["by_provider"]["groq"]["failed"], 1)
        self.assertEqual(s["by_provider"]["groq"]["cached"], 100)

    def test_calibration_needs_five_samples_and_ignores_search_calls(self):
        log = UsageLog()
        for _ in range(4):
            log.record(provider="gemini", stage="qa", ok=True, chars_in=3000, prompt_tokens=1000)
        self.assertIsNone(log.chars_per_token("gemini"))
        log.record(provider="gemini", stage="qa", ok=True, chars_in=3000, prompt_tokens=1000)
        self.assertEqual(log.chars_per_token("gemini"), 3.0)
        for _ in range(9):
            log.record(provider="gemini", stage="research", ok=True, search=True, chars_in=3000, prompt_tokens=9000)
        self.assertEqual(log.chars_per_token("gemini"), 3.0)

    def test_failed_calls_do_not_pollute_calibration(self):
        log = UsageLog()
        for _ in range(6):
            log.record(provider="groq", stage="qa", ok=False, chars_in=3000, prompt_tokens=10)
        self.assertIsNone(log.chars_per_token("groq"))

    def test_json_lines_file_is_written_when_configured(self):
        path = Path(tempfile.mkdtemp()) / "usage.jsonl"
        log = UsageLog()
        log.configure(path)
        log.record(provider="groq", stage="qa", ok=True, total_tokens=5)
        self.assertEqual(json.loads(path.read_text().splitlines()[0])["total_tokens"], 5)

    def test_an_unwritable_log_path_never_breaks_a_call(self):
        log = UsageLog()
        log.configure("/nonexistent-dir/x/usage.jsonl")
        log.record(provider="groq", stage="qa", ok=True)          # must not raise

    def test_redact_helper(self):
        self.assertEqual(redact("ok"), "ok")


if __name__ == "__main__":
    unittest.main()
