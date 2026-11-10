"""Configuration screen with a production-style environment (needs streamlit; skipped where it is not installed)."""
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import helpers as H
from tests.test_keys_compat import prod_env

HAVE_STREAMLIT = importlib.util.find_spec("streamlit") is not None
ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(HAVE_STREAMLIT, "streamlit not installed")
class ConfigScreen(unittest.TestCase):
    def setUp(self):
        H.reset_state()
        env = {**prod_env(), "CACHE_DIR": tempfile.mkdtemp(), "CONFIG_DIR": tempfile.mkdtemp(), "USAGE_LOG": "off",
               "APP_PASSWORD": "", "CONFIG_PASSWORD": ""}
        self.patch = mock.patch.dict(os.environ, env)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def open_config(self):
        from streamlit.testing.v1 import AppTest
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=90).run()
        view = [r for r in at.radio if r.label == "View"][0]
        return view.set_value(view.options[-1]).run()

    def test_the_screen_renders_with_every_key_variable_present(self):
        at = self.open_config()
        self.assertEqual([e.value for e in at.exception], [])
        text = " | ".join(m.value for m in at.markdown)
        for heading in ("Quota groups", "Usage and routing", "Request budget and routing"):
            self.assertIn(heading, text)

    def test_every_key_is_listed_by_fingerprint_with_its_group_and_how_that_is_known(self):
        at = self.open_config()
        frames = [df.value for df in at.dataframe]
        quota = [f for f in frames if "Quota group" in list(f.columns)]
        self.assertTrue(quota)
        rows = quota[0]
        self.assertEqual(set(rows["Provider"]), {"gemini", "groq"})
        self.assertEqual(set(rows["How known"]), {"unverified"})                 # nothing declared or observed yet
        used = " ".join(rows["Used for"])
        for purpose in ("context", "research", "generation", "qa", "strategy"):
            self.assertIn(purpose, used)
        self.assertEqual(len(rows[rows["Provider"] == "gemini"]), 6)             # shared + 5 purpose keys, one row each
        self.assertNotIn("AIza", rows.to_string())
        self.assertNotIn("gsk_", rows.to_string())

    def test_the_verified_gemini_limits_are_shown_for_the_configured_model(self):
        with mock.patch.dict(os.environ, {"GEMINI_MODEL": "gemini-3.6-flash"}):
            at = self.open_config()
        rows = [df.value for df in at.dataframe if "Limits applied" in list(df.value.columns)][0]
        gem = " ".join(rows[rows["Provider"] == "gemini"]["Limits applied"])
        self.assertTrue("RPM" in gem and "RPD" in gem)

    def test_unverified_keys_trigger_the_shared_quota_warning_and_stage_variables_are_reported_as_preserved(self):
        at = self.open_config()
        warnings = " ".join(w.value for w in at.warning)
        self.assertIn("treated as ONE shared quota", warnings)
        captions = " ".join(c.value for c in at.caption)
        self.assertIn("preserved, not applied", captions)
        self.assertIn("GEMINI_MODEL_QA", captions)

    def test_the_new_settings_widgets_exist(self):
        at = self.open_config()
        labels = [s.label for s in at.selectbox] + [t.label for t in at.text_area]
        for want in ("Per-stage Gemini model variables", "Gemini limits per project and model", "Which project / organisation each key belongs to"):
            self.assertTrue(any(want in l for l in labels), want)


@unittest.skipUnless(HAVE_STREAMLIT, "streamlit not installed")
class JsonFields(unittest.TestCase):
    def test_limits_and_key_groups_survive_a_seed_collect_round_trip(self):
        from app.config import schema as S
        from app.ui import config_page as C
        cfg = S.default_config()
        cfg["provider"]["key_groups"] = [{"provider": "gemini", "key": "39a3c5", "group": "proj-a"}]
        cfg["budget"]["gemini_limits"].append({"model": "gemini-3.6-flash", "group": "proj-a", "rpm": 15, "tpm": 1000000, "rpd": 500})
        ss = {}
        C.seed_widgets(ss, cfg)
        back = C.collect(ss, base=cfg)
        self.assertEqual(back["provider"]["key_groups"], cfg["provider"]["key_groups"])
        self.assertEqual(back["budget"]["gemini_limits"], cfg["budget"]["gemini_limits"])

    def test_invalid_json_is_reported_not_silently_dropped(self):
        from app.config import schema as S, validate
        from app.ui import config_page as C
        ss = {}
        C.seed_widgets(ss, S.default_config())
        ss[C.K("budget.gemini_limits")] = "[{not json"
        errors = validate.validate(C.collect(ss))[0]
        self.assertTrue(any("not valid JSON" in e for e in errors), errors)

    def test_a_malformed_entry_is_rejected(self):
        from app.config import schema as S, validate
        cfg = S.default_config()
        cfg["budget"]["gemini_limits"] = [{"model": "", "rpm": -1}]
        self.assertTrue(validate.validate(cfg)[0])


if __name__ == "__main__":
    unittest.main()
