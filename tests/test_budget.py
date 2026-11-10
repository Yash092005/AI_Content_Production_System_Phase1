import os
import unittest
from unittest import mock

from tests import helpers  # noqa: F401  (sets sys.path)
from app.ai import budget
from app.ai.budget import Tracker, plan, policy, out_cap_for, tool_reserve, est_input_tokens, limits_for
from app.config import runtime
from app.config.settings import Settings
from app.metrics.usage import UsageLog


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def groq_plan(system_chars=4000, prompt_chars=2000, cap=2000, search=True, tracker=None, usage=None, stage="research"):
    return plan("groq", "m", stage, "s" * system_chars, "p" * prompt_chars, out_cap=cap, search=search,
                tracker=tracker or Tracker(FakeClock()), usage=usage or UsageLog())


class Policy(unittest.TestCase):
    def test_defaults_are_the_approved_values(self):
        p = policy()
        self.assertEqual((p.target, p.floor, p.ceiling), (0.25, 0.20, 0.80))

    def test_ceiling_is_also_a_floor_on_headroom(self):
        with mock.patch.dict(os.environ, {"BUDGET_FLOOR_HEADROOM": "0.05", "BUDGET_HARD_CEILING": "0.80"}):
            self.assertGreaterEqual(policy().floor, 0.20)          # 1 - ceiling

    def test_nonsense_values_are_clamped_not_trusted(self):
        with mock.patch.dict(os.environ, {"BUDGET_HARD_CEILING": "5", "BUDGET_TARGET_HEADROOM": "-1"}):
            p = policy()
            self.assertLessEqual(p.ceiling, 0.95)
            self.assertGreaterEqual(p.target, p.floor)


class PlanMath(unittest.TestCase):
    def test_input_output_and_tool_overhead_are_all_counted(self):
        p = groq_plan()                                   # 1500 in + 2000 out + 1000 provisional tool
        self.assertEqual((p.in_tokens, p.out_cap, p.tool, p.total), (1500, 2000, 1000, 4500))
        self.assertAlmostEqual(p.headroom["tpm"], 1 - 4500 / 8000)
        self.assertEqual(p.binding, "tpm")

    def test_no_tool_reserve_for_plain_calls(self):
        self.assertEqual(groq_plan(search=False).tool, 0)

    def test_the_old_incident_request_is_below_the_floor(self):
        # ~8.7k tokens of input on Groq's 8K per-minute limit: must never be sent
        p = plan("groq", "m", "research_blueprint", "s" * 15000, "p" * 14000, out_cap=1500, search=True,
                 tracker=Tracker(FakeClock()), usage=UsageLog())
        self.assertFalse(p.fits(policy().floor))
        self.assertLess(p.min_headroom, 0)

    def test_requests_already_used_this_minute_reduce_headroom(self):
        clock = FakeClock()
        tr = Tracker(clock)
        base = groq_plan(tracker=tr)
        tr.reserve("groq", "m", 2500)
        busy = groq_plan(tracker=tr)
        self.assertAlmostEqual(base.min_headroom - busy.min_headroom, 2500 / 8000)

    def test_usage_older_than_a_minute_no_longer_counts(self):
        clock = FakeClock()
        tr = Tracker(clock)
        tr.reserve("groq", "m", 6000)
        clock.t += 61
        self.assertAlmostEqual(groq_plan(tracker=tr).headroom["tpm"], 1 - 4500 / 8000)

    def test_daily_tokens_and_requests_are_checked(self):
        clock = FakeClock()
        tr = Tracker(clock)
        for _ in range(4):
            tr.reserve("groq", "m", 40000)
            clock.t += 120
        p = groq_plan(tracker=tr)
        self.assertAlmostEqual(p.headroom["tpd"], 1 - (160000 + 4500) / 200000)

    def test_provider_headers_beat_our_own_window_when_they_show_more_use(self):
        clock = FakeClock()
        tr = Tracker(clock)
        tr.headers("groq", "m", remaining_tokens=1500, reset_seconds=30)       # provider says 6500 used
        self.assertAlmostEqual(groq_plan(tracker=tr).headroom["tpm"], 1 - (6500 + 4500) / 8000)
        clock.t += 31                                                           # stale header is ignored
        self.assertAlmostEqual(groq_plan(tracker=tr).headroom["tpm"], 1 - 4500 / 8000)

    def test_gemini_checks_every_known_dimension_for_the_verified_model(self):
        p = plan("gemini", "gemini-3.6-flash", "generate", "s" * 14000, "p" * 20000, tracker=Tracker(FakeClock()), usage=UsageLog())
        self.assertEqual(set(p.headroom), {"context", "tpm", "rpm", "rpd"})
        self.assertAlmostEqual(p.headroom["rpm"], 1 - 1 / 5)
        self.assertAlmostEqual(p.headroom["rpd"], 1 - 1 / 20)

    def test_the_verified_gemini_3_6_flash_limits_are_the_shipped_default_for_unverified_keys(self):
        lim = limits_for("gemini", "gemini-3.6-flash", "gemini:unverified", Tracker(FakeClock()))
        self.assertEqual((lim.rpm, lim.tpm, lim.rpd), (5, 250000, 20))
        self.assertIn("AI Studio", lim.source)

    def test_other_models_and_projects_are_not_assumed_to_have_those_limits_but_are_treated_conservatively(self):
        tr = Tracker(FakeClock())
        other_model = limits_for("gemini", "gemini-x-lite", "gemini:unverified", tr)
        other_project = limits_for("gemini", "gemini-3.6-flash", "gemini:proj-b", tr)
        for lim in (other_model, other_project):
            self.assertTrue(lim.source.startswith("assumed"), lim.source)
            self.assertEqual((lim.rpm, lim.rpd), (5, 20))                # no better than the strictest known entry

    def test_a_configured_entry_for_a_project_and_model_is_used_exactly(self):
        s = Settings({}).with_overrides(**{"budget.gemini_limits": [
            {"model": "gemini-3.6-flash", "group": "unverified", "rpm": 5, "tpm": 250000, "rpd": 20},
            {"model": "gemini-3.6-flash", "group": "proj-b", "rpm": 15, "tpm": 1000000, "rpd": 500, "source": "confirmed"}]})
        with runtime.use_settings(s):
            tr = Tracker(FakeClock())
            self.assertEqual(limits_for("gemini", "gemini-3.6-flash", "gemini:proj-b", tr).rpd, 500)
            self.assertEqual(limits_for("gemini", "models/Gemini-3.6-Flash", "gemini:unverified", tr).rpd, 20)

    def test_with_no_entries_at_all_nothing_is_guessed(self):
        with runtime.use_settings(Settings({}).with_overrides(**{"budget.gemini_limits": []})):
            lim = limits_for("gemini", "gemini-3.6-flash", "gemini:unverified", Tracker(FakeClock()))
        self.assertEqual((lim.rpm, lim.tpm, lim.rpd), (0, 0, 0))

    def test_a_ceiling_is_learned_from_a_limit_error_and_kept_below_it(self):
        clock = FakeClock()
        tr = Tracker(clock)
        for _ in range(7):
            tr.reserve("gemini", "g", 100)
        tr.note_limit_hit("gemini", "g", "daily")
        self.assertEqual(limits_for("gemini", "g", tracker=tr).rpd, 7)
        p = plan("gemini", "g", "qa", "s", "p", tracker=tr, usage=UsageLog())
        self.assertLess(p.headroom["rpd"], 0.0)                  # 8 of a learned 7: over the ceiling


class Waiting(unittest.TestCase):
    def test_a_short_window_reports_how_long_to_wait(self):
        clock = FakeClock()
        tr = Tracker(clock)
        tr.reserve("groq", "m", 3000)
        clock.t += 20
        p = groq_plan(tracker=tr)                       # 4500 + 3000 = 7500 of 8000: below the floor
        self.assertFalse(p.fits(0.2))
        self.assertTrue(0 < p.wait_s <= 41)

    def test_no_wait_is_offered_when_the_request_alone_is_too_big(self):
        p = groq_plan(system_chars=20000, cap=3000, tracker=Tracker(FakeClock()))
        self.assertEqual(p.wait_s, 0)


class CapsAndCalibration(unittest.TestCase):
    def test_generate_cap_scales_with_the_word_target(self):
        self.assertGreater(out_cap_for("generate", 1500), out_cap_for("generate", 450))
        self.assertGreaterEqual(out_cap_for("generate", 900), 2600)

    def test_cap_for_qa_is_small(self):
        self.assertLessEqual(out_cap_for("qa"), 1000)

    def test_measured_output_replaces_the_default_after_enough_samples(self):
        usage = UsageLog()
        for _ in range(4):
            usage.record(provider="groq", stage="research", ok=True, completion_tokens=1000)
        self.assertEqual(out_cap_for("research", None, "groq", usage), 2000)       # 4 samples: still the default
        usage.record(provider="groq", stage="research", ok=True, completion_tokens=1000)
        self.assertEqual(out_cap_for("research", None, "groq", usage), 1500)       # 1.5 x p90

    def test_tool_reserve_is_provisional_until_measured(self):
        usage = UsageLog()
        self.assertEqual(tool_reserve("groq", "research", True, usage), 1000)
        for _ in range(5):
            usage.record(provider="groq", stage="research", ok=True, search=True, chars_in=4000, prompt_tokens=2600)
        self.assertEqual(tool_reserve("groq", "research", True, usage), int((2600 - 1000) * 1.2))

    def test_input_estimate_uses_the_measured_ratio(self):
        usage = UsageLog()
        self.assertEqual(est_input_tokens("a" * 400, "b" * 400, "groq", usage), 200)
        for _ in range(5):
            usage.record(provider="groq", stage="qa", ok=True, chars_in=3000, prompt_tokens=1000)     # 3 chars/token
        self.assertEqual(est_input_tokens("a" * 400, "b" * 400, "groq", usage), 267)


if __name__ == "__main__":
    unittest.main()
