import unittest

from tests import helpers  # noqa: F401
from app.pipeline import source_digest as D

PAGE = """Darshan Timings
The temple opens at 5:30 AM and closes at 9:00 PM.
This sacred place has welcomed pilgrims from across the world for many centuries.
Entry is free for all devotees.

Special Pass
A special pass costs Rs 300 per person.
Many families treasure the quiet moments they spend here with their loved ones.
Book online at www.example.org/booking before arriving.
For help write to help@example.org or call the helpline.

Rules
Devotees must remove footwear before entering.
Photography is strictly prohibited inside the sanctum.
The air is full of devotion and the sound of bells carries across the lanes.

Festivals
Maha Shivratri falls on 8 March this year.
The Kashi Vishwanath Corridor connects the temple to the Ganga ghats.
Every visitor leaves with a feeling of peace and gratitude.
"""
FILLER = ["This sacred place has welcomed pilgrims from across the world for many centuries.",
          "Many families treasure the quiet moments they spend here with their loved ones.",
          "The air is full of devotion and the sound of bells carries across the lanes.",
          "Every visitor leaves with a feeling of peace and gratitude."]
FACTS = ["The temple opens at 5:30 AM and closes at 9:00 PM.", "Entry is free for all devotees.",
         "A special pass costs Rs 300 per person.", "Book online at www.example.org/booking before arriving.",
         "For help write to help@example.org or call the helpline.", "Devotees must remove footwear before entering.",
         "Photography is strictly prohibited inside the sanctum.", "Maha Shivratri falls on 8 March this year.",
         "The Kashi Vishwanath Corridor connects the temple to the Ganga ghats."]


def good_extraction(*lines):
    return "\n".join(f"Page :: {l}" for l in lines)


class FactSignals(unittest.TestCase):
    def test_each_kind_of_fact_is_recognised_not_only_numbers(self):
        for text in ("Photography is strictly prohibited inside the sanctum.",       # prohibition
                     "Devotees must remove footwear before entering.",               # obligation
                     "Entry is free for all devotees.",                              # price / availability
                     "Book online at www.example.org/booking before arriving.",      # URL
                     "Write to help@example.org for details.",                       # e-mail
                     "The Kashi Vishwanath Corridor connects the temple to the ghats.",   # names
                     "Maha Shivratri falls in March.",                               # date
                     "The pass costs Rs 300.",                                       # price
                     "Open on Mondays only."):
            self.assertTrue(D.has_fact_signal(text), text)

    def test_purely_descriptive_sentences_are_not(self):
        for text in FILLER:
            self.assertFalse(D.has_fact_signal(text), text)

    def test_the_page_is_split_into_headings_and_sentences_in_order(self):
        units = D.split_units(PAGE)
        self.assertEqual(units[0], ("Darshan Timings", True))
        self.assertEqual(units[1], ("The temple opens at 5:30 AM and closes at 9:00 PM.", False))
        self.assertEqual([u for u, h in units if h], ["Darshan Timings", "Special Pass", "Rules", "Festivals"])


class Completeness(unittest.TestCase):
    def build(self, extraction):
        return D.build_digest(PAGE, extraction)

    def test_every_fact_bearing_sentence_is_kept_even_if_the_model_missed_it(self):
        res = self.build(good_extraction(FACTS[0], FACTS[2]))           # the model listed only two facts
        self.assertTrue(res.ok, res.reasons)
        for fact in FACTS:
            self.assertIn(fact, res.text)

    def test_descriptive_filler_is_left_out_so_the_digest_is_smaller(self):
        res = self.build(good_extraction(*FACTS))
        for text in FILLER:
            self.assertNotIn(text, res.text)
        self.assertLess(len(res.text), len(PAGE))
        self.assertEqual(res.report["descriptive_units_left_out"], 4)

    def test_dates_prices_urls_prohibitions_and_names_survive_without_any_digit_test(self):
        res = self.build(good_extraction(FACTS[0]))
        for must in ("www.example.org/booking", "help@example.org", "strictly prohibited", "must remove footwear",
                     "Rs 300", "8 March", "Kashi Vishwanath Corridor", "Entry is free"):
            self.assertIn(must, res.text)

    def test_the_digest_is_made_of_the_pages_own_text_in_page_order_with_headings(self):
        res = self.build(good_extraction(*FACTS))
        lines = res.text.splitlines()
        self.assertTrue(all(l in PAGE for l in lines))
        self.assertEqual([l for l in lines if l in ("Darshan Timings", "Special Pass", "Rules", "Festivals")],
                         ["Darshan Timings", "Special Pass", "Rules", "Festivals"])
        positions = [PAGE.index(l) for l in lines]
        self.assertEqual(positions, sorted(positions))

    def test_text_the_model_invented_never_enters_the_digest(self):
        res = self.build(good_extraction(*FACTS, "The temple also offers free helicopter rides on Sundays."))
        self.assertNotIn("helicopter", res.text)
        self.assertEqual(res.report["model_lines_dropped_not_in_page"], 1)
        self.assertTrue(res.ok)

    def test_a_model_list_that_is_mostly_not_in_the_page_is_rejected(self):
        res = self.build(good_extraction("The temple is open every day.", "Entry costs nothing at all.",
                                         "There is a big car park.", FACTS[0]))
        self.assertFalse(res.ok)
        self.assertTrue(any("cannot be trusted" in r for r in res.reasons))

    def test_an_empty_or_useless_model_reply_is_rejected_not_silently_accepted(self):
        for junk in ("", "I cannot help with that.", "   \n  "):
            res = self.build(junk)
            self.assertFalse(res.ok, junk)
            self.assertTrue(res.reasons)

    def test_a_digest_that_is_not_clearly_smaller_is_rejected(self):
        dense = "\n".join(f"Fact {i}: The temple performs ritual number {i} at 6:{i % 60:02d} AM." for i in range(40))
        res = D.build_digest(dense, good_extraction(*dense.splitlines()))
        self.assertFalse(res.ok)
        self.assertTrue(any("not clearly smaller" in r for r in res.reasons))

    def test_the_report_says_what_was_done(self):
        res = self.build(good_extraction(*FACTS))
        for key in ("page_chars", "digest_chars", "units", "kept", "kept_by_check", "model_lines",
                    "model_lines_found_in_page", "model_lines_dropped_not_in_page"):
            self.assertIn(key, res.report)

    def test_empty_inputs_are_safe(self):
        res = D.build_digest("", "")
        self.assertFalse(res.ok)
        self.assertEqual(D.split_units(None), [])

    def test_prompts_and_blocks(self):
        self.assertIn("verbatim", D.extraction_prompt(PAGE))
        self.assertTrue(D.extraction_prompt(PAGE).endswith(PAGE))
        self.assertIn("system keeps the full page", D.digest_block("fact"))


if __name__ == "__main__":
    unittest.main()
