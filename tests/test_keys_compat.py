"""The existing multi-key architecture and every existing environment variable keep working exactly as before."""
import os
import re
import unittest
from pathlib import Path
from unittest import mock

from tests import helpers as H
from app.ai import get_clients
from app.ai.gemini_client import GeminiClient
from app.ai.keys import (PURPOSES, dedicated_keys, fingerprint, keys_for, stage_model_env_found, stage_model_from_env)
from app.ai.pool import KeyPool
from app.ai.router import Router
from app.config import runtime, schema
from app.config.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
# The complete variable surface of commit be86007 (pinned): none of these may disappear.
BASELINE_ENV_MAP = ['CACHE_DIR', 'GEMINI_FALLBACK_MODEL', 'GEMINI_GROUNDING', 'GEMINI_MIN_INTERVAL', 'GEMINI_MODEL', 'GROQ_BROWSER_SEARCH',
                    'GROQ_MODEL', 'GROQ_REASONING_EFFORT', 'GROQ_RESEARCH_MODEL', 'GROQ_REVISION_TPM', 'GROQ_TPM', 'MAX_REVISIONS',
                    'MODE', 'PIPELINE_CACHE', 'PIPELINE_LEAN', 'SOURCE_CHECK', 'STRATEGY_PREDICT']
BASELINE_ENV_EXAMPLE = ['APP_PASSWORD', 'CONFIG_DIR', 'CONFIG_PASSWORD', 'GEMINI_API_KEY', 'GEMINI_API_KEY_CONTEXT',
                        'GEMINI_API_KEY_GENERATION', 'GEMINI_API_KEY_QA', 'GEMINI_API_KEY_RESEARCH', 'GEMINI_FALLBACK_MODEL',
                        'GEMINI_GROUNDING', 'GEMINI_MIN_INTERVAL', 'GEMINI_MODEL', 'GROQ_API_KEY', 'GROQ_API_KEY_CONTEXT',
                        'GROQ_API_KEY_GENERATION', 'GROQ_API_KEY_QA', 'GROQ_API_KEY_RESEARCH', 'GROQ_BROWSER_SEARCH', 'GROQ_MODEL',
                        'GROQ_REASONING_EFFORT', 'GROQ_RESEARCH_MODEL', 'GROQ_REVISION_TPM', 'GROQ_TPM', 'MAX_REVISIONS', 'MODE',
                        'PIPELINE_CACHE', 'PIPELINE_LEAN', 'SOURCE_CHECK', 'STRATEGY_PREDICT']


def prod_env(**extra):
    """A production-like .env: shared key plus purpose-specific keys and per-stage model variables, for both providers."""
    env = {}
    for prov, pre in (("GEMINI", "AIza"), ("GROQ", "gsk_")):
        env[f"{prov}_API_KEY"] = f"{pre}SHARED{prov}0000000000000001"
        for i, name in enumerate(("CONTEXT", "GENERATE", "GENERATION", "QA", "RESEARCH", "STRATEGY"), 2):
            env[f"{prov}_API_KEY_{name}"] = f"{pre}{name}{prov}00000000000000{i:02d}"
    env.update({"GEMINI_MODEL": "gemini-3.6-flash", "GEMINI_MODEL_QA": "flash-lite-qa", "GEMINI_MODEL_RESEARCH": "flash-lite-research",
                "GEMINI_MODEL_GENERATE": "stage-generate", "GEMINI_MODEL_STRATEGY": "stage-strategy",
                "GROQ_MODEL": "openai/gpt-oss-120b", "GROQ_RESEARCH_MODEL": "openai/gpt-oss-120b", "MODE": "auto"})
    env.update(extra)
    return env


def k(env, prov, name):
    return env[f"{prov}_API_KEY_{name}"] if name else env[f"{prov}_API_KEY"]


class PurposeKeys(unittest.TestCase):
    def setUp(self):
        H.reset_state()
        self.env = prod_env()
        self.clients = get_clients(env=self.env)

    def pool_keys(self, router, side):
        pool = getattr(router, side)
        return [c.api_key for c in (pool.clients if isinstance(pool, KeyPool) else [pool])]

    def test_every_purpose_keeps_its_own_key_then_the_shared_key_as_backup_for_both_providers(self):
        expect = {"context": "CONTEXT", "research": "RESEARCH", "generation": "GENERATION", "qa": "QA"}
        for purpose, name in expect.items():
            r = self.clients[purpose]
            self.assertIsInstance(r, Router)
            self.assertEqual(self.pool_keys(r, "gemini"), [k(self.env, "GEMINI", name), k(self.env, "GEMINI", None)], purpose)
            self.assertEqual(self.pool_keys(r, "groq"), [k(self.env, "GROQ", name), k(self.env, "GROQ", None)], purpose)

    def test_purposes_are_not_collapsed_into_one_primary_key(self):
        firsts = {p: self.pool_keys(self.clients[p], "gemini")[0] for p in (*PURPOSES, "strategy")}
        self.assertEqual(len(set(firsts.values())), 5)
        self.assertNotIn(k(self.env, "GEMINI", None), firsts.values())

    def test_the_older_generate_spelling_never_overrides_the_standard_generation_variable(self):
        self.assertNotIn(k(self.env, "GEMINI", "GENERATE"), self.pool_keys(self.clients["generation"], "gemini"))
        self.assertNotIn(k(self.env, "GROQ", "GENERATE"), self.pool_keys(self.clients["generation"], "groq"))

    def test_the_older_generate_spelling_is_used_only_when_generation_is_empty(self):
        env = prod_env()
        del env["GEMINI_API_KEY_GENERATION"]
        del env["GROQ_API_KEY_GENERATION"]
        c = get_clients(env=env)
        self.assertEqual(self.pool_keys(c["generation"], "gemini"), [k(env, "GEMINI", "GENERATE"), k(env, "GEMINI", None)])
        self.assertEqual(self.pool_keys(c["generation"], "groq"), [k(env, "GROQ", "GENERATE"), k(env, "GROQ", None)])

    def test_the_strategy_call_uses_the_strategy_keys_when_they_are_set_otherwise_the_context_client(self):
        self.assertIsNot(self.clients["strategy"], self.clients["context"])
        self.assertEqual(self.pool_keys(self.clients["strategy"], "gemini"), [k(self.env, "GEMINI", "STRATEGY"), k(self.env, "GEMINI", None)])
        env = prod_env()
        del env["GEMINI_API_KEY_STRATEGY"]
        del env["GROQ_API_KEY_STRATEGY"]
        c = get_clients(env=env)
        self.assertIs(c["strategy"], c["context"])

    def test_several_keys_for_one_purpose_stay_in_the_order_given(self):
        env = prod_env(GEMINI_API_KEY_QA="AIzaQAone00000000000000001, AIzaQAtwo00000000000000002")
        got = self.pool_keys(get_clients(env=env)["qa"], "gemini")
        self.assertEqual(got, ["AIzaQAone00000000000000001", "AIzaQAtwo00000000000000002", k(env, "GEMINI", None)])

    def test_a_purpose_without_its_own_key_uses_the_shared_key_as_before(self):
        env = prod_env()
        for p in ("GEMINI", "GROQ"):
            del env[f"{p}_API_KEY_QA"]
        c = get_clients(env=env)
        self.assertEqual(self.pool_keys(c["qa"], "gemini"), [k(env, "GEMINI", None)])

    def test_the_router_tries_a_purposes_own_key_first(self):
        qa = self.clients["qa"]
        first = qa.candidates("qa")[0]
        self.assertEqual((first.provider, first.key), ("gemini", fingerprint(k(self.env, "GEMINI", "QA"))))
        order = [c.key for c in qa.candidates("qa") if c.provider == "gemini"]
        self.assertEqual(order, [fingerprint(k(self.env, "GEMINI", "QA")), fingerprint(k(self.env, "GEMINI", None))])

    def test_research_searches_with_the_research_groq_key_first(self):
        first = self.clients["research"].candidates("research")[0]
        self.assertEqual((first.provider, first.mode, first.key), ("groq", "search", fingerprint(k(self.env, "GROQ", "RESEARCH"))))

    def test_dedicated_key_lookup_is_unchanged_for_the_baseline_variables(self):
        env = {"GEMINI_API_KEY_RESEARCH": "r1,r2", "GEMINI_API_KEY": "shared", "GROQ_API_KEY_QA": "q"}
        self.assertEqual(dedicated_keys("gemini", "research", env), ["r1", "r2"])
        self.assertEqual(keys_for("gemini", "research", env)[0], ["r1", "r2", "shared"])
        self.assertEqual(keys_for("groq", "qa", env)[0], ["q"])
        self.assertEqual(keys_for("gemini", "qa", env)[0], ["shared"])


class PerStageModelVariables(unittest.TestCase):
    ENV = {"GEMINI_MODEL_QA": "qa-model", "GEMINI_MODEL_RESEARCH": "research-model", "GEMINI_MODEL_GENERATE": "gen-old",
           "GEMINI_MODEL_GENERATION": "gen-new", "GEMINI_MODEL_STRATEGY": "strat-model", "GEMINI_MODEL": "main-model"}

    def chain(self, stage, setting=None, **cfg):
        over = {**cfg}
        if setting:
            over["provider.gemini_stage_models_env"] = setting
        with mock.patch.dict(os.environ, self.ENV), runtime.use_settings(Settings({}).with_overrides(**{"provider.gemini_model": "main-model", **over})):
            return GeminiClient(api_key="AIzaFakeFakeFakeFakeFake12345").model_for(stage)

    def test_the_variables_are_preserved_and_listed_but_not_applied_by_default(self):
        with mock.patch.dict(os.environ, self.ENV):
            found = dict(stage_model_env_found())
        self.assertEqual(found["GEMINI_MODEL_QA"], "qa-model")
        self.assertEqual(self.chain("qa"), "main-model")                 # exactly the behaviour of the deployed code
        self.assertEqual(self.chain("generate"), "main-model")

    def test_when_switched_on_each_stage_uses_its_own_variable(self):
        self.assertEqual(self.chain("qa", "apply"), "qa-model")
        self.assertEqual(self.chain("research", "apply"), "research-model")
        self.assertEqual(self.chain("strategy", "apply"), "strat-model")
        self.assertEqual(self.chain("generate", "apply"), "gen-new")       # GENERATION wins over GENERATE
        self.assertEqual(self.chain("revise", "apply"), "gen-new")
        self.assertEqual(self.chain("reference", "apply"), "main-model")   # no variable for it

    def test_an_explicit_per_stage_setting_beats_the_variables(self):
        self.assertEqual(self.chain("qa", "apply", **{"provider.gemini_stage_models": {"qa": "explicit"}}), "explicit")

    def test_the_older_generate_variable_is_used_when_generation_is_absent(self):
        env = {k_: v for k_, v in self.ENV.items() if k_ != "GEMINI_MODEL_GENERATION"}
        with mock.patch.dict(os.environ, env, clear=False), runtime.use_settings(Settings({}).with_overrides(**{"provider.gemini_stage_models_env": "apply"})):
            os.environ.pop("GEMINI_MODEL_GENERATION", None)
            self.assertEqual(stage_model_from_env("generate"), "gen-old")


class BackwardCompatibility(unittest.TestCase):
    def test_every_baseline_config_seed_variable_still_exists(self):
        self.assertTrue(set(BASELINE_ENV_MAP) <= set(schema.ENV_MAP), set(BASELINE_ENV_MAP) - set(schema.ENV_MAP))

    def test_every_baseline_variable_is_still_documented_in_env_example(self):
        names = set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]{3,})\s*=", (ROOT / ".env.example").read_text(encoding="utf-8"), re.M))
        self.assertTrue(set(BASELINE_ENV_EXAMPLE) <= names, set(BASELINE_ENV_EXAMPLE) - names)

    def test_the_baseline_environment_alone_gives_the_baseline_key_layout(self):
        env = {"GEMINI_API_KEY": "gs", "GROQ_API_KEY": "qs", "GEMINI_API_KEY_RESEARCH": "gr", "GROQ_API_KEY_QA": "qq"}
        c = get_clients(env=env)
        self.assertEqual(set(c), {"gemini", "reasoning", "strategy", *PURPOSES})
        self.assertIs(c["strategy"], c["context"])
        self.assertEqual([x.api_key for x in c["research"].gemini.clients], ["gr", "gs"])
        self.assertEqual([x.api_key for x in c["qa"].groq.clients], ["qq", "qs"])
        self.assertEqual([x.api_key for x in c["context"].gemini.clients] if hasattr(c["context"].gemini, "clients") else [c["context"].gemini.api_key], ["gs"])

    def test_the_old_per_stage_variables_do_not_change_which_models_are_used(self):
        with mock.patch.dict(os.environ, {"GEMINI_MODEL_QA": "lite", "GEMINI_MODEL": "main"}):
            self.assertEqual(GeminiClient(api_key="AIzaX0000000000000000000000").model_chain("qa")[0], "main")

    def test_the_default_configuration_validates_and_keeps_the_baseline_behaviour_settings(self):
        from app.config import validate
        cfg = schema.default_config()
        self.assertEqual(validate.validate(cfg)[0], [])
        self.assertTrue(cfg["research"]["gemini_grounding"])
        self.assertEqual(cfg["provider"]["gemini_stage_models_env"], "ignore")
        self.assertEqual(cfg["provider"]["gemini_stage_models"], {})
        self.assertEqual(cfg["provider"]["key_groups"], [])

    def test_a_saved_older_configuration_still_loads_and_gets_the_new_defaults(self):
        old = {"provider": {"gemini_model": "gemini-3.6-flash", "groq_tpm": 8000}, "generation": {"strategy_predict": "ai"}}
        merged = Settings(old)
        self.assertEqual(merged.get("provider.gemini_model"), "gemini-3.6-flash")
        self.assertEqual(merged.get("budget.target_headroom"), 0.25)
        self.assertEqual(merged.get("budget.gemini_limits")[0]["rpd"], 20)


if __name__ == "__main__":
    unittest.main()
