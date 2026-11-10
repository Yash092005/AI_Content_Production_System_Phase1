"""Quota is per Google PROJECT and per Groq ORGANISATION (and model): never assumed to be independent per key."""
import os
import unittest
from unittest import mock

from tests import helpers as H
from app.ai import budget
from app.ai.base import ApiError, ProvidersUnavailable
from app.ai.budget import Tracker, limits_for, plan
from app.ai.health import Health
from app.ai.pool import KeyPool
from app.ai.quota import GROUPS, Groups, UNVERIFIED
from app.ai.router import Router
from app.config import runtime
from app.config.settings import Settings
from app.metrics.usage import UsageLog

MODEL = "gemini-3.6-flash"
LIMIT = lambda group, rpd=20, rpm=5: {"model": MODEL, "group": group, "rpm": rpm, "tpm": 250000, "rpd": rpd}


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t


class Resolution(unittest.TestCase):
    def setUp(self):
        H.reset_state()

    def test_without_any_information_every_key_is_unverified_and_shares_one_group(self):
        self.assertEqual(GROUPS.resolve("gemini", "k1"), ("gemini:unverified", "unverified"))
        self.assertEqual(GROUPS.label("gemini", "k1"), GROUPS.label("gemini", "k2"))
        self.assertFalse(GROUPS.verified("gemini", "k1"))

    def test_groups_can_be_declared_in_the_environment(self):
        with mock.patch.dict(os.environ, {"GEMINI_KEY_GROUPS": "k1=proj-a, k2=proj-a ,k3=proj-b"}):
            self.assertEqual(GROUPS.resolve("gemini", "k1"), ("gemini:proj-a", "declared"))
            self.assertEqual(GROUPS.label("gemini", "k2"), "gemini:proj-a")
            self.assertEqual(GROUPS.label("gemini", "k3"), "gemini:proj-b")
            self.assertEqual(GROUPS.label("gemini", "k9"), "gemini:unverified")
            self.assertEqual(GROUPS.label("groq", "k1"), "groq:unverified")          # providers never mix

    def test_groups_can_be_declared_in_the_configuration_and_the_environment_wins(self):
        cfg = Settings({}).with_overrides(**{"provider.key_groups": [{"provider": "groq", "key": "g1", "group": "org-one"},
                                                                    {"provider": "gemini", "key": "k1", "group": "from-config"}]})
        with runtime.use_settings(cfg):
            self.assertEqual(GROUPS.label("groq", "g1"), "groq:org-one")
            with mock.patch.dict(os.environ, {"GEMINI_KEY_GROUPS": "k1=from-env"}):
                self.assertEqual(GROUPS.label("gemini", "k1"), "gemini:from-env")

    def test_the_project_is_learned_from_a_google_error_that_names_it(self):
        body = '{"error":{"code":429,"details":[{"metadata":{"consumer":"projects/123456789012"}}]}}'
        found = GROUPS.observe_from_error("gemini", "k1", ApiError(429, body))
        self.assertEqual(found, "project-123456789012")
        self.assertEqual(GROUPS.resolve("gemini", "k1"), ("gemini:project-123456789012", "observed"))
        self.assertEqual(GROUPS.resolve("gemini", "k2")[1], "unverified")           # other keys stay unverified

    def test_the_organisation_is_learned_from_a_groq_limit_error(self):
        msg = ("Rate limit reached for model `openai/gpt-oss-120b` in organization `org_01abcXYZ9` service tier `on_demand` "
               "on tokens per minute (TPM): Limit 8000, Used 7900, Requested 700.")
        self.assertEqual(GROUPS.observe_from_error("groq", "g1", ApiError(429, msg)), "org_01abcXYZ9")
        self.assertEqual(GROUPS.resolve("groq", "g1"), ("groq:org_01abcXYZ9", "observed"))

    def test_an_error_that_names_nothing_teaches_nothing(self):
        self.assertIsNone(GROUPS.observe_from_error("gemini", "k1", ApiError(429, "You exceeded your current quota")))
        self.assertEqual(GROUPS.resolve("gemini", "k1")[1], "unverified")

    def test_a_declared_group_beats_an_observed_one_but_the_disagreement_is_recorded(self):
        with mock.patch.dict(os.environ, {"GEMINI_KEY_GROUPS": "k1=proj-a"}):
            GROUPS.observe_from_error("gemini", "k1", ApiError(429, "consumer projects/999999999"))
            self.assertEqual(GROUPS.resolve("gemini", "k1"), ("gemini:proj-a", "declared"))
            self.assertTrue(GROUPS.conflicts)

    def test_cooldown_scope_is_the_group_when_known_and_the_single_key_when_not(self):
        with mock.patch.dict(os.environ, {"GEMINI_KEY_GROUPS": "k1=proj-a"}):
            self.assertEqual(GROUPS.cooldown_scope("gemini", "k1"), "gemini:proj-a")
            self.assertEqual(GROUPS.cooldown_scope("gemini", "k2"), "gemini:unverified#key:k2")

    def test_two_unverified_keys_that_behave_independently_get_their_own_groups(self):
        GROUPS.separate("gemini", "k1", "k2")
        self.assertNotEqual(GROUPS.label("gemini", "k1"), GROUPS.label("gemini", "k2"))
        self.assertEqual(GROUPS.resolve("gemini", "k1")[1], "observed")

    def test_separating_never_overrides_a_declared_group(self):
        with mock.patch.dict(os.environ, {"GEMINI_KEY_GROUPS": "k1=proj-a"}):
            GROUPS.separate("gemini", "k1", "k2")
            self.assertEqual(GROUPS.label("gemini", "k1"), "gemini:proj-a")

    def test_the_snapshot_shows_fingerprints_only(self):
        rows = GROUPS.snapshot("gemini", ["39a3c5"], env={"GEMINI_KEY_GROUPS": "39a3c5=proj-a"})
        self.assertEqual(rows, [{"provider": "gemini", "key": "39a3c5", "group": "proj-a", "status": "declared", "evidence": ""}])


class Accounting(unittest.TestCase):
    def setUp(self):
        H.reset_state()
        self.clock = Clock()
        self.tr = Tracker(self.clock)

    def settings(self, *entries):
        return runtime.use_settings(Settings({}).with_overrides(**{"budget.gemini_limits": list(entries)}))

    def head(self, group):
        return plan("gemini", MODEL, "qa", "s", "p", tracker=self.tr, usage=UsageLog(), group=group)

    def test_usage_in_one_project_does_not_use_up_another_projects_quota(self):
        with self.settings(LIMIT("proj-a"), LIMIT("proj-b")):
            for _ in range(15):
                self.tr.reserve("gemini", MODEL, 100, group="gemini:proj-a")
            a, b = self.head("gemini:proj-a"), self.head("gemini:proj-b")
        self.assertLess(a.headroom["rpd"], 0.2)
        self.assertAlmostEqual(b.headroom["rpd"], 1 - 1 / 20)

    def test_each_project_has_its_own_limits_for_the_same_model(self):
        with self.settings(LIMIT("proj-a", rpd=20), LIMIT("proj-b", rpd=500)):
            self.assertEqual(limits_for("gemini", MODEL, "gemini:proj-a", self.tr).rpd, 20)
            self.assertEqual(limits_for("gemini", MODEL, "gemini:proj-b", self.tr).rpd, 500)

    def test_a_learned_ceiling_belongs_to_one_group_only(self):
        for _ in range(7):
            self.tr.reserve("gemini", MODEL, 10, group="gemini:proj-a")
        self.tr.note_limit_hit("gemini", MODEL, "daily", group="gemini:proj-a")
        with self.settings(LIMIT("proj-a"), LIMIT("proj-b")):
            self.assertEqual(limits_for("gemini", MODEL, "gemini:proj-a", self.tr).rpd, 7)
            self.assertEqual(limits_for("gemini", MODEL, "gemini:proj-b", self.tr).rpd, 20)

    def test_usage_is_tracked_per_model_too(self):
        self.tr.reserve("gemini", "model-a", 500, group="gemini:p")
        self.assertEqual(self.tr.tokens_last("gemini", "model-b", group="gemini:p"), 0)
        self.assertEqual(self.tr.tokens_last("gemini", "model-a", group="gemini:p"), 500)

    def test_unverified_keys_pool_their_usage_because_independence_is_never_assumed(self):
        a, b = GROUPS.label("gemini", "kA"), GROUPS.label("gemini", "kB")
        self.assertEqual(a, b)
        for _ in range(16):
            self.tr.reserve("gemini", MODEL, 100, group=a)
        self.assertLess(self.head(b).headroom["rpd"], 0.2)

    def test_groq_limits_are_per_organisation_and_model(self):
        self.tr.reserve("groq", "m1", 7000, group="groq:org-1")
        busy = plan("groq", "m1", "qa", "s" * 400, "p" * 400, tracker=self.tr, usage=UsageLog(), group="groq:org-1")
        other_org = plan("groq", "m1", "qa", "s" * 400, "p" * 400, tracker=self.tr, usage=UsageLog(), group="groq:org-2")
        other_model = plan("groq", "m2", "qa", "s" * 400, "p" * 400, tracker=self.tr, usage=UsageLog(), group="groq:org-1")
        self.assertLessEqual(busy.headroom["tpm"], 0.0)
        self.assertGreater(other_org.headroom["tpm"], 0.6)
        self.assertGreater(other_model.headroom["tpm"], 0.6)


class RoutingAcrossKeys(unittest.TestCase):
    def setUp(self):
        H.reset_state()
        self.clock = Clock()
        self.health, self.tracker = Health(self.clock), Tracker(self.clock)
        self.sleeps = []
        env = mock.patch.dict(os.environ, {"GEMINI_GROUNDING": "on", "GEMINI_GROUNDING_CAPABILITY": "auto"})
        env.start()
        self.addCleanup(env.stop)

    def keys(self, provider, *ids, **scripts):
        stubs = [H.Stub(provider, MODEL if provider == "gemini" else "oss-120b", key_id=k, search=(provider == "groq"),
                        script=scripts.get(k)) for k in ids]
        return KeyPool(provider, stubs, clock=self.clock), stubs

    def router(self, gpool, qpool):
        return Router(gpool, qpool, health=self.health, tracker=self.tracker, usage=UsageLog(),
                      sleep=lambda s: (self.sleeps.append(s), setattr(self.clock, "t", self.clock.t + s)))

    def limits(self, *entries):
        s = runtime.use_settings(Settings({}).with_overrides(**{"budget.gemini_limits": list(entries)}))
        s.__enter__()
        self.addCleanup(lambda: s.__exit__(None, None, None))

    # --- isolation -------------------------------------------------------------------------------------------
    def test_a_key_in_another_project_is_used_when_the_first_projects_quota_is_spent(self):
        gp, (ga, gb) = self.keys("gemini", "kA", "kB")
        qp, _ = self.keys("groq", "g1")
        self.limits(LIMIT("proj-a"), LIMIT("proj-b"))
        with mock.patch.dict(os.environ, {"GEMINI_KEY_GROUPS": "kA=proj-a,kB=proj-b"}):
            for _ in range(16):
                self.tracker.reserve("gemini", MODEL, 100, group="gemini:proj-a")
            self.router(gp, qp).generate("p" * 800, system="s" * 800, stage="generate")
        self.assertEqual((len(ga.calls), len(gb.calls)), (0, 1))

    def test_when_every_declared_project_is_spent_the_request_pauses_instead_of_breaking_a_limit(self):
        gp, (ga, gb) = self.keys("gemini", "kA", "kB")
        qp, (q1,) = self.keys("groq", "g1")
        self.limits(LIMIT("proj-a"), LIMIT("proj-b"))
        with mock.patch.dict(os.environ, {"GEMINI_KEY_GROUPS": "kA=proj-a,kB=proj-b"}):
            for g in ("gemini:proj-a", "gemini:proj-b"):
                for _ in range(16):
                    self.tracker.reserve("gemini", MODEL, 100, group=g)
            with self.assertRaises(ProvidersUnavailable):
                self.router(gp, qp).generate("p" * 30000, system="s" * 14000, stage="generate")
        self.assertEqual(len(ga.calls) + len(gb.calls), 0)

    def test_unverified_keys_are_not_assumed_to_add_capacity(self):
        gp, (ga, gb) = self.keys("gemini", "kA", "kB")
        qp, _ = self.keys("groq", "g1")
        for _ in range(16):                                     # the pooled 'unverified' project is nearly spent
            self.tracker.reserve("gemini", MODEL, 100, group="gemini:unverified")
        with self.assertRaises(ProvidersUnavailable):
            self.router(gp, qp).generate("p" * 30000, system="s" * 14000, stage="generate")
        self.assertEqual(len(ga.calls) + len(gb.calls), 0)

    def test_groq_organisations_are_isolated(self):
        gp, _ = self.keys("gemini", "kA")
        qp, (g1, g2) = self.keys("groq", "gO1", "gO2")
        with mock.patch.dict(os.environ, {"GROQ_KEY_GROUPS": "gO1=org-1,gO2=org-2"}):
            self.tracker.reserve("groq", "oss-120b", 7000, group="groq:org-1")
            self.router(gp, qp).generate("p" * 1500, system="s" * 3000, stage="research")
        self.assertEqual((len(g1.calls), len(g2.calls)), (0, 1))

    # --- rotation and cooldowns ----------------------------------------------------------------------------
    def test_rotation_the_purposes_own_key_first_then_the_next_key(self):
        gp, (own, shared) = self.keys("gemini", "own", "shared")
        qp, _ = self.keys("groq", "g1")
        self.router(gp, qp).generate("p" * 800, system="s" * 800, stage="qa")
        self.assertEqual((len(own.calls), len(shared.calls)), (1, 0))

    def test_a_limited_key_rests_and_the_next_key_serves_the_request(self):
        rpm = H.api_error(429, "Rate limit reached ... tokens per minute (TPM). Please try again in 20s")
        gp, (own, shared) = self.keys("gemini", "own", "shared", own={"qa": [rpm]})
        qp, _ = self.keys("groq", "g1")
        r = self.router(gp, qp)
        self.assertEqual(r.generate("p" * 800, system="s" * 800, stage="qa"), "PASS")
        self.assertEqual((len(own.calls), len(shared.calls)), (1, 1))
        r.generate("p" * 800, system="s" * 800, stage="qa")
        self.assertEqual(len(own.calls), 1)                      # still resting: not hit again
        self.assertEqual(len(shared.calls), 2)

    def test_the_key_comes_back_after_its_cooldown(self):
        rpm = H.api_error(429, "tokens per minute (TPM). try again in 20s")
        gp, (own, shared) = self.keys("gemini", "own", "shared", own={"qa": [rpm]})
        qp, _ = self.keys("groq", "g1")
        r = self.router(gp, qp)
        r.generate("p" * 800, system="s" * 800, stage="qa")
        self.clock.t += 100                                      # past both the pool's 90 s and the health rest
        r.generate("p" * 800, system="s" * 800, stage="qa")
        self.assertEqual(len(own.calls), 2)

    def test_keys_of_one_declared_project_rest_together(self):
        rpm = H.api_error(429, "tokens per minute (TPM). try again in 20s")
        gp, (ka, kb) = self.keys("gemini", "kA", "kB", kA={"qa": [rpm]})
        qp, (q1,) = self.keys("groq", "g1")
        with mock.patch.dict(os.environ, {"GEMINI_KEY_GROUPS": "kA=proj-a,kB=proj-a"}):
            self.router(gp, qp).generate("p" * 800, system="s" * 800, stage="qa")
        self.assertEqual(len(kb.calls), 0)                       # same project: it shares the exhausted quota
        self.assertEqual(len(q1.calls), 1)                       # so the request went to the other provider

    def test_unverified_keys_are_each_tried_so_rotation_still_works(self):
        rpm = H.api_error(429, "tokens per minute (TPM). try again in 20s")
        gp, (ka, kb) = self.keys("gemini", "kA", "kB", kA={"qa": [rpm]})
        qp, _ = self.keys("groq", "g1")
        self.assertEqual(self.router(gp, qp).generate("p" * 800, system="s" * 800, stage="qa"), "PASS")
        self.assertEqual((len(ka.calls), len(kb.calls)), (1, 1))

    def test_a_key_that_works_right_after_another_was_limited_is_recorded_as_independent(self):
        daily = H.api_error(429, "quota")
        daily.quota_id = "GenerateRequestsPerDayPerProjectPerModel"
        gp, (ka, kb) = self.keys("gemini", "kA", "kB", kA={"qa": [daily]})
        qp, _ = self.keys("groq", "g1")
        r = self.router(gp, qp)
        r.generate("p" * 800, system="s" * 800, stage="qa")
        self.assertNotEqual(GROUPS.label("gemini", "kA"), GROUPS.label("gemini", "kB"))
        self.assertTrue(any("independently" in e for e in r.events))

    def test_a_key_that_also_fails_is_not_recorded_as_independent(self):
        rpm = H.api_error(429, "tokens per minute (TPM). try again in 20s")
        gp, (ka, kb) = self.keys("gemini", "kA", "kB", kA={"qa": [rpm]}, kB={"qa": [rpm]})
        qp, _ = self.keys("groq", "g1")
        self.router(gp, qp).generate("p" * 800, system="s" * 800, stage="qa")
        self.assertEqual(GROUPS.label("gemini", "kA"), GROUPS.label("gemini", "kB"))

    # --- real limits ------------------------------------------------------------------------------------------
    def test_with_5_requests_a_minute_the_router_waits_instead_of_sending_the_fifth(self):
        gp, (k1,) = self.keys("gemini", "k1")
        qp, _ = self.keys("groq", "g1")
        for _ in range(4):                                       # 4 requests in the last minute already
            self.tracker.reserve("gemini", MODEL, 100, group="gemini:unverified")
            self.clock.t += 5
        self.router(gp, qp).generate("p" * 30000, system="s" * 14000, stage="generate")
        self.assertEqual(len(self.sleeps), 1)
        self.assertLessEqual(self.sleeps[0], 75)
        self.assertEqual(len(k1.calls), 1)

    def test_the_target_tier_allows_three_requests_a_minute_and_the_floor_a_fourth(self):
        gp, _ = self.keys("gemini", "k1")
        qp, _ = self.keys("groq", "g1")
        r = self.router(gp, qp)
        heads = []
        for _ in range(4):
            c = r.candidates("generate")[0]
            heads.append(round(r._plan(c, "generate", "s", "p", None).min_headroom, 2))
            self.tracker.reserve("gemini", MODEL, 100, group=c.group)
        self.assertEqual(heads, [0.8, 0.6, 0.4, 0.2])


if __name__ == "__main__":
    unittest.main()
