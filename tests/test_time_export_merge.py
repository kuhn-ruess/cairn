"""The report export's "merge per day" option (views.timetracking._export_table).

Run with ``python -m unittest discover tests``. No database: the merge works on
the report rows alone, and what it gets wrong lands on an invoice.
"""
import unittest
from datetime import date

from application.views.timetracking import _export_table

D1, D2 = date(2026, 9, 1), date(2026, 9, 2)


def row(day, minutes, note, start="", end="", **kw):
    r = {
        "spent_on": day, "customer": "Acme", "project": "Support",
        "user_name": "Bastian", "minutes": minutes,
        "amount_cents": minutes * 100, "billable": True, "locked": False,
        "note": note, "description": note, "source_label": "", "service": "",
        "start_time": start, "end_time": end, "external_ref": "",
        "custom": [{"key": "ticket", "label": "Ticket", "value": note}],
    }
    r.update(kw)
    return r


def table(rows, merge=True):
    fields = [{"key": "ticket", "label": "Ticket", "type": "text", "options": []}]
    header, out, _ = _export_table({"rows": rows, "field_columns": fields},
                                   merge=merge)
    return header, [dict(zip(header, r)) for r in out]


class MergePerDayTests(unittest.TestCase):

    def test_without_clock_times_a_day_is_one_line(self):
        _, out = table([row(D1, 30, "Mails"), row(D1, 90, "Call"),
                        row(D2, 60, "Docs")])
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["Minutes"], 120)
        self.assertEqual(out[0]["Hours"], "2.00")
        self.assertEqual(out[0]["Amount (EUR)"], "120.00")
        self.assertEqual(out[0]["Description"], "Mails, Call")
        self.assertEqual(out[1]["Description"], "Docs")

    def test_custom_fields_are_left_out(self):
        header, _ = table([row(D1, 30, "Mails")])
        self.assertNotIn("Ticket", header)
        header, _ = table([row(D1, 30, "Mails")], merge=False)
        self.assertIn("Ticket", header)

    def test_back_to_back_times_merge_separate_blocks_stay(self):
        _, out = table([
            row(D1, 60, "A", "09:00", "10:00"),
            row(D1, 30, "B", "10:00", "10:30"),
            row(D1, 60, "C", "13:00", "14:00"),
            row(D1, 30, "D", "13:30", "14:30"),   # overlaps C
        ])
        self.assertEqual(
            [(r["Start"], r["End"], r["Minutes"], r["Description"]) for r in out],
            [("09:00", "10:30", 90, "A, B"), ("13:00", "14:30", 90, "C, D")])

    def test_a_gap_is_a_new_block(self):
        _, out = table([row(D1, 60, "A", "09:00", "10:00"),
                        row(D1, 60, "B", "10:05", "11:05")])
        self.assertEqual(len(out), 2)

    def test_untimed_bookings_beside_timed_ones_get_their_own_line(self):
        _, out = table([row(D1, 60, "A", "09:00", "10:00"),
                        row(D1, 15, "Mails")])
        self.assertEqual([(r["Start"], r["Minutes"]) for r in out],
                         [("", 15), ("09:00", 60)])

    def test_different_people_or_projects_do_not_merge(self):
        _, out = table([row(D1, 30, "A"), row(D1, 30, "B", user_name="Jasmin"),
                        row(D1, 30, "C", project="Hosting"),
                        row(D1, 30, "D", locked=True)])
        self.assertEqual(len(out), 4)

    def test_a_repeated_description_is_named_once(self):
        _, out = table([row(D1, 30, "Support"), row(D1, 30, "Support"),
                        row(D1, 30, "")])
        self.assertEqual(out[0]["Description"], "Support")
        self.assertEqual(out[0]["Minutes"], 90)

    def test_ticket_references_ride_in_the_description(self):
        _, out = table([row(D1, 30, "Fix", external_ref="zammad:4711"),
                        row(D1, 30, "Call")])
        self.assertIn("Call", out[0]["Description"])
        self.assertTrue(out[0]["Description"].endswith("Fix, Call"))
        self.assertNotEqual(out[0]["Description"], "Fix, Call")

    def test_crossing_midnight(self):
        _, out = table([row(D1, 60, "A", "23:00", "00:00"),
                        row(D1, 30, "B", "22:30", "23:00")])
        self.assertEqual([(r["Start"], r["End"]) for r in out],
                         [("22:30", "00:00")])


if __name__ == "__main__":
    unittest.main()
