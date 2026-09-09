"""services/html_to_markdown — HTML email bodies to Markdown.

The case this pins: an email signature is built from nested layout ``<table>``s.
Converted straight, every one of them becomes Markdown table syntax, and nested
that collapses into a single line of ``| | --- | |`` which no Markdown parser
reads back as a table — so it reaches the reader as a wall of literal pipes.
A real data table has to survive that cleanup untouched.

Run with ``python -m unittest discover tests``.
"""
import unittest

from application.services.html_to_markdown import html_to_markdown

SIGNATURE = """<p>Hallo Bastian,<br>keine Probleme bekannt.</p>
<table><tbody><tr><td>
  <table><tbody><tr><td><b>Felix</b></td></tr><tr><td>Bernefeld</td></tr></tbody></table>
  <table><tbody><tr><td>Email:</td>
  <td><a href="mailto:f@example.com">f@example.com</a></td></tr></tbody></table>
</td></tr></tbody></table>"""

DATA_TABLE = """<p>Preise:</p>
<table><thead><tr><th>Posten</th><th>Preis</th></tr></thead>
<tbody><tr><td>Lizenz</td><td>100 EUR</td></tr></tbody></table>"""


class LayoutTableTest(unittest.TestCase):

    def test_a_signature_brings_no_pipes(self):
        out = html_to_markdown(SIGNATURE)
        self.assertNotIn("|", out)
        self.assertIn("Felix", out)
        self.assertIn("Bernefeld", out)
        self.assertIn("mailto:f@example.com", out)

    def test_rows_stay_on_their_own_lines(self):
        out = html_to_markdown(
            "<table><tr><td>eins</td></tr><tr><td>zwei</td></tr></table>")
        self.assertEqual([l for l in out.splitlines() if l.strip()],
                         ["eins", "zwei"])

    def test_a_real_table_survives(self):
        out = html_to_markdown(DATA_TABLE)
        self.assertIn("| Posten | Preis |", out)
        self.assertIn("| Lizenz | 100 EUR |", out)

    def test_a_real_table_inside_a_layout_table_survives(self):
        out = html_to_markdown(
            "<table><tr><td>" + DATA_TABLE + "</td></tr></table>")
        self.assertIn("| Posten | Preis |", out)
        self.assertIn("| Lizenz | 100 EUR |", out)

    def test_cells_of_one_row_stay_on_one_line(self):
        out = html_to_markdown(
            "<table><tr><td>Büro:</td><td>Wuppertal</td></tr></table>")
        self.assertEqual(out, "Büro: Wuppertal")

    def test_html_without_tables_is_unchanged_in_behaviour(self):
        self.assertEqual(html_to_markdown("<p>Hallo <b>Welt</b></p>"),
                         "Hallo **Welt**")

    def test_empty_input(self):
        self.assertEqual(html_to_markdown(""), "")
        self.assertEqual(html_to_markdown(None), "")


if __name__ == "__main__":
    unittest.main()
