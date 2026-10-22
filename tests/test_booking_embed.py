"""The embed options of the public booking page (services/booking_embed.py).

Pure: no app, no database. What matters is that nothing a stranger types into
the URL reaches the stylesheet unchecked, and that the options only apply
inside an embed.
"""
import unittest

from application.services import booking_embed as be


class AccentTests(unittest.TestCase):

    def test_six_and_three_digits_with_or_without_hash(self):
        self.assertEqual(be.accent("4F8A2C")[0], "4f8a2c")
        self.assertEqual(be.accent("#4f8a2c")[1], (79, 138, 44))
        self.assertEqual(be.accent("abc")[0], "aabbcc")

    def test_anything_else_is_no_colour(self):
        for bad in ("", None, "red", "12345", "1234567", "4f8a2c;}", "url(x)"):
            self.assertIsNone(be.accent(bad), bad)

    def test_the_text_on_it_stays_readable(self):
        self.assertEqual(be.accent("ffffff")[2], "#1d2021")
        self.assertEqual(be.accent("4f8a2c")[2], "#ffffff")
        self.assertEqual(be.accent("8cc767")[2], "#1d2021")


class OptionTests(unittest.TestCase):

    def test_layout_options_need_an_embed(self):
        opts = be.options({"bare": "1", "compact": "1", "accent": "fff", "lang": "de"})
        self.assertFalse(opts["bare"] or opts["compact"] or opts["accent"])
        self.assertEqual(opts["lang"], "de")

    def test_inside_an_embed_they_count(self):
        opts = be.options({"embed": "1", "bare": "1", "compact": "true",
                           "accent": "4f8a2c", "lang": "DE"})
        self.assertTrue(opts["bare"] and opts["compact"])
        self.assertEqual(opts["lang"], "de")

    def test_unknown_language_is_english(self):
        self.assertEqual(be.options({"lang": "fr"})["lang"], "")

    def test_the_query_carried_on_is_only_what_was_set(self):
        self.assertEqual(be.carried(be.options({}), ""), {})
        opts = be.options({"embed": "1", "bare": "1", "accent": "#ABC", "lang": "de"})
        self.assertEqual(be.carried(opts, "light"),
                         {"embed": 1, "bare": 1, "accent": "aabbcc",
                          "lang": "de", "theme": "light"})


class WordingTests(unittest.TestCase):

    def test_english_is_the_text_itself(self):
        t = be.translator("")
        self.assertEqual(t("Pick a time"), "Pick a time")
        self.assertIn("Microsoft Teams", t("teams_hint"))
        self.assertEqual(t("{n} minutes with {name}", n=30, name="Ann"),
                         "30 minutes with Ann")

    def test_german(self):
        t = be.translator("de")
        self.assertEqual(t("Pick a time"), "Zeit wählen")
        self.assertEqual(t("{n} minutes with {name}", n=30, name="Ann"),
                         "30 Minuten mit Ann")

    def test_the_fields_are_escaped_and_the_wording_is_not(self):
        t = be.translator("")
        self.assertEqual(str(t("{n} minutes with {name}", n=30, name="<b>")),
                         "30 minutes with &lt;b&gt;")
        self.assertEqual(str(t("You're booked")), "You're booked")

    def test_every_script_string_has_a_german_one(self):
        self.assertEqual(set(be.js_strings("de")), set(be.JS_KEYS))
        self.assertEqual(be.js_strings("en"), {})

    def test_no_dashes_as_punctuation_in_the_german(self):
        for text in be.GERMAN.values():
            self.assertNotIn("—", text)
            self.assertNotIn("–", text)


if __name__ == "__main__":
    unittest.main()
