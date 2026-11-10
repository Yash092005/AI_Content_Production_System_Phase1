import os
import unittest
from unittest import mock

from tests import helpers  # noqa: F401
from app.config import runtime
from app.config.settings import Settings
from app.rules import loader as L

DOC = "PREAMBLE LINE\n\n1. FIRST\nalpha text\n\n2. SECOND\nbeta text\n\n3. THIRD\ngamma text\n"
RULES = {"master": DOC, "canonical": DOC, "brand": DOC}
PROFILES = {"qa": {"master": ["preamble", "1", "3"], "canonical": ["2"], "brand": ["preamble"]}}


class Sections(unittest.TestCase):
    def test_a_document_is_split_into_preamble_and_numbered_sections(self):
        pre, secs = L.split_sections(DOC)
        self.assertEqual(pre, "PREAMBLE LINE")
        self.assertEqual(sorted(secs), ["1", "2", "3"])
        self.assertTrue(secs["2"].startswith("2. SECOND"))

    def test_scoping_keeps_only_the_named_sections(self):
        scoped, notes = L.scope_rules(RULES, "qa", PROFILES)
        self.assertEqual(notes, [])
        self.assertIn("alpha", scoped["master"])
        self.assertNotIn("beta", scoped["master"])
        self.assertIn("gamma", scoped["master"])
        self.assertEqual(scoped["canonical"].count("beta"), 1)
        self.assertNotIn("alpha", scoped["canonical"])
        self.assertEqual(scoped["brand"], "PREAMBLE LINE")

    def test_a_missing_section_sends_the_whole_document_never_less(self):
        edited = dict(RULES, master="No numbered sections here at all.")
        scoped, notes = L.scope_rules(edited, "qa", PROFILES)
        self.assertEqual(scoped["master"], edited["master"])
        self.assertTrue(any("master" in n for n in notes))

    def test_a_profile_naming_a_section_that_no_longer_exists_falls_back_to_the_full_document(self):
        scoped, notes = L.scope_rules(RULES, "qa", {"qa": {"master": ["1", "9"]}})
        self.assertEqual(scoped["master"], DOC)
        self.assertEqual(len(notes), 1)

    def test_stages_without_a_profile_get_everything(self):
        scoped, notes = L.scope_rules(RULES, "revise", PROFILES)
        self.assertEqual(scoped, RULES)
        self.assertEqual(notes, [])

    def test_a_document_the_profile_does_not_mention_is_passed_in_full(self):
        scoped, _ = L.scope_rules(RULES, "qa", {"qa": {"master": ["1"]}})
        self.assertEqual(scoped["canonical"], DOC)


class StagePrompt(unittest.TestCase):
    def test_unscoped_prompt_is_exactly_the_original(self):
        r = L.load_rules()
        self.assertEqual(L.build_stage_system_prompt(r, "x", "qa", scoped=False), L.build_system_prompt(r, "x"))
        self.assertEqual(L.build_stage_system_prompt(r, None, "revise", scoped=True), L.build_system_prompt(r, None))

    def test_scoped_prompt_says_it_is_an_excerpt_and_keeps_task_constraints(self):
        r = L.load_rules()
        text = L.build_stage_system_prompt(r, "TASK RULE", "research", scoped=True)
        self.assertIn("excerpts below are the binding rules for this stage", text)
        self.assertIn("TASK RULE", text)
        self.assertNotIn("the documents below are binding and complete", text.lower())

    def test_missing_documents_still_fail_loudly(self):
        r = dict(L.load_rules(), brand="")
        with self.assertRaises(ValueError):
            L.build_stage_system_prompt(r, None, "qa", scoped=True)


class RealRules(unittest.TestCase):
    """The shipped profiles against the shipped rule documents."""

    def setUp(self):
        self.rules = L.load_rules()
        self.full = len(L.build_system_prompt(self.rules))

    def test_every_profile_resolves_cleanly_with_no_fallbacks(self):
        for stage in ("research", "research_blueprint", "blueprint", "generate", "qa"):
            _, notes = L.scope_rules(self.rules, stage)
            self.assertEqual(notes, [], f"{stage}: {notes}")

    def test_every_scoped_prompt_is_smaller_than_the_full_documents(self):
        for stage in ("research", "research_blueprint", "blueprint", "generate", "qa"):
            self.assertLess(len(L.build_stage_system_prompt(self.rules, None, stage, scoped=True)), self.full, stage)

    def test_the_research_excerpt_is_small_enough_for_the_search_route(self):
        tokens = len(L.build_stage_system_prompt(self.rules, None, "research", scoped=True)) / 4
        self.assertLess(tokens, 1100)

    def test_evidence_rules_are_in_every_stage_that_deals_with_evidence(self):
        for stage in ("research", "blueprint", "generate", "qa"):
            text = L.build_stage_system_prompt(self.rules, None, stage, scoped=True)
            self.assertIn("EVIDENCE RULES", text.upper(), stage)

    def test_the_qa_excerpt_contains_the_qa_gate_rules(self):
        text = L.build_stage_system_prompt(self.rules, None, "qa", scoped=True).upper()
        self.assertIn("QA", text)
        self.assertIn("EVIDENCE", text)


class Modes(unittest.TestCase):
    def test_default_is_all(self):
        self.assertEqual(L.rules_scoping_mode(), "all")

    def test_environment_and_settings(self):
        with mock.patch.dict(os.environ, {"RULES_SCOPING": "groq"}):
            self.assertEqual(L.rules_scoping_mode(), "groq")
        with mock.patch.dict(os.environ, {"RULES_SCOPING": "bogus"}):
            self.assertEqual(L.rules_scoping_mode(), "all")
        with runtime.use_settings(Settings({}).with_overrides(**{"budget.rules_scoping": "off"})):
            self.assertEqual(L.rules_scoping_mode(), "off")


if __name__ == "__main__":
    unittest.main()
