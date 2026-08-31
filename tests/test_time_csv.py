"""services/time_csv.parse — a mite CSV export, read back in.

Run with ``python -m unittest discover tests``. No database needed: parsing is
where this can go wrong quietly. A comma read as a dot turns half an hour into
five, and a note that spans four lines read as four rows turns one booking into
four — both land on an invoice before anybody notices.
"""
import unittest

from application.services.time_csv import parse

SAMPLE = (
    "Datum;Start;Ende;Kunde;Projekt;Leistung;Benutzer;Bemerkung;Stunden;"
    "Umsatz (EUR);Stundensatz (EUR);Abgeschlossen\n"
    "2026-08-28;2026-08-28 09:45:00;2026-08-28 10:15:00;Kuhn & Rueß GmbH;Büro;"
    'Team;Jasmin Rueß;"Mails\nZahlungsverkehr\nÜberweisungen";0,5;0,0;0,0;Nein\n'
    "2026-08-27;2026-08-27 08:00:00;2026-08-27 14:00:00;Acme AG;Support;"
    "Beratung;Bastian Kuhn;Serverumzug;6,0;600,0;100,0;Ja\n"
)


class ParseTests(unittest.TestCase):

    def setUp(self):
        self.result = parse(SAMPLE)
        self.rows = self.result.rows

    def test_a_multiline_note_stays_one_booking(self):
        self.assertEqual(self.result.errors, [])
        self.assertEqual(len(self.rows), 2)
        self.assertEqual(self.rows[0].note,
                         "Mails\nZahlungsverkehr\nÜberweisungen")

    def test_the_decimal_comma_is_hours(self):
        self.assertEqual(self.rows[0].minutes, 30)
        self.assertEqual(self.rows[1].minutes, 360)

    def test_clock_columns_lose_the_date(self):
        self.assertEqual((self.rows[0].start_time, self.rows[0].end_time),
                         ("09:45", "10:15"))

    def test_money_says_whether_it_is_billable(self):
        self.assertFalse(self.rows[0].billable)     # internal time, 0,0
        self.assertTrue(self.rows[1].billable)
        self.assertEqual(self.rows[1].rate_cents, 10000)
        self.assertEqual(self.rows[1].amount_cents, 60000)

    def test_abgeschlossen_is_billed(self):
        self.assertFalse(self.rows[0].locked)
        self.assertTrue(self.rows[1].locked)

    def test_the_key_is_stable_and_per_row(self):
        again = parse(SAMPLE).rows
        self.assertEqual([r.key for r in self.rows], [r.key for r in again])
        self.assertNotEqual(self.rows[0].key, self.rows[1].key)

    def test_a_bad_line_is_reported_not_raised(self):
        broken = SAMPLE + "irgendwas;;;;;;;;;;;\n"
        res = parse(broken)
        self.assertEqual(len(res.rows), 2)
        self.assertTrue(any("unreadable date" in e for e in res.errors))

    def test_a_foreign_file_is_refused_whole(self):
        res = parse("a;b;c\n1;2;3\n")
        self.assertEqual(res.rows, [])
        self.assertTrue(res.errors and "not a mite time export" in res.errors[0])
