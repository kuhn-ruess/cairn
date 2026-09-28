"""The report's "Per month" view: the same filtered result, one column per month.

Run with ``python -m unittest discover tests``. The pivot itself is pure and
runs anywhere; the page and the export need a MongoDB (``TestConfig`` points
at ``bktodo_test``) and are skipped without one.

What is pinned: every month of the period gets a column, an empty month reads
0 instead of going missing, a customer's row sums its projects, the totals add
up, time outside the period is not counted, and the page and the CSV show the
same numbers.
"""
import csv
import io
import unittest
from datetime import date, datetime, timezone

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None


def _row(customer, project, day, minutes):
    return {"customer": customer, "customer_id": customer.lower(),
            "project": project, "project_id": f"{customer}/{project}",
            "spent_on": day, "minutes": minutes}


class MonthlyBreakdownTests(unittest.TestCase):

    def _pivot(self, rows, lo=date(2026, 1, 1), hi=date(2026, 12, 31)):
        from application.services.timetracking import monthly_breakdown
        return monthly_breakdown(rows, lo, hi)

    def test_a_year_has_twelve_columns_and_empty_months_read_zero(self):
        out = self._pivot([_row("Aldi", "Support", date(2026, 1, 5), 120),
                           _row("Aldi", "Support", date(2026, 3, 9), 60)])
        self.assertEqual([m["label"] for m in out["months"]][:3],
                         ["Jan 2026", "Feb 2026", "Mar 2026"])
        self.assertEqual(len(out["months"]), 12)
        aldi = out["customers"][0]
        self.assertEqual(aldi["minutes"], [120, 0, 60] + [0] * 9)
        self.assertEqual(aldi["total"], 180)
        self.assertEqual(out["totals"], aldi["minutes"])
        self.assertEqual(out["total"], 180)

    def test_a_customer_sums_its_projects_and_the_footer_sums_customers(self):
        out = self._pivot([_row("Aldi", "Support", date(2026, 2, 1), 30),
                           _row("Aldi", "Rollout", date(2026, 2, 20), 90),
                           _row("Lidl", "Ops", date(2026, 2, 3), 15)])
        aldi, lidl = out["customers"]
        self.assertEqual([p["name"] for p in aldi["projects"]],
                         ["Rollout", "Support"])
        self.assertEqual(aldi["minutes"][1], 120)
        self.assertEqual(lidl["minutes"][1], 15)
        self.assertEqual(out["totals"][1], 135)

    def test_a_range_across_a_year_boundary_gets_each_month_once(self):
        out = self._pivot([_row("Aldi", "S", date(2026, 1, 31), 10)],
                          lo=date(2025, 11, 15), hi=date(2026, 2, 2))
        self.assertEqual([m["label"] for m in out["months"]],
                         ["Nov 2025", "Dec 2025", "Jan 2026", "Feb 2026"])
        self.assertEqual(out["totals"], [0, 0, 10, 0])

    def test_time_outside_the_period_is_not_counted(self):
        out = self._pivot([_row("Aldi", "S", date(2025, 12, 31), 50)])
        self.assertEqual(out["customers"], [])
        self.assertEqual(out["total"], 0)


@unittest.skipIf(connect is None, "mongoengine not installed")
class ReportPerMonthPageTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from application import create_app
        disconnect(alias="default")
        try:
            cls.app = create_app("application.config.TestConfig")
            with cls.app.app_context():
                from application.models.user import User
                User.objects.first()  # forces a real round trip
        except (ServerSelectionTimeoutError, PyMongoError) as exc:
            raise unittest.SkipTest(f"no MongoDB for the test database: {exc}")

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            cls._wipe()
        disconnect(alias="default")

    @staticmethod
    def _wipe():
        from application.models.team import Team
        from application.models.timetracking import (
            Customer, TimeBucket, TimeTrackingProject)
        from application.models.user import User
        for doc in (TimeBucket, TimeTrackingProject, Customer, Team, User):
            doc.objects.delete()

    def setUp(self):
        from application.models.team import Team
        from application.models.timetracking import Customer, TimeTrackingProject
        from application.models.user import User
        from application.services.timetracking import add_entry
        self.ctx = self.app.app_context()
        self.ctx.push()
        self._wipe()
        self.user = User(username="months", email="months@test.invalid",
                         password_hash="x", level="team_admin").save()
        self.team = Team(name="T", admin=self.user, admins=[self.user]).save()
        self.user.team = self.team
        self.user.save()
        self.aldi = Customer(team=self.team, name="Aldi").save()
        lidl = Customer(team=self.team, name="Lidl").save()
        support = TimeTrackingProject(team=self.team, customer=self.aldi,
                                      name="Support").save()
        other = TimeTrackingProject(team=self.team, customer=lidl,
                                    name="Ops").save()
        for proj, month, mins in ((support, 1, 120), (support, 3, 60),
                                  (other, 2, 45)):
            add_entry(project=proj, user=self.user, minutes=mins,
                      spent_on=datetime(2026, month, 10, 12, tzinfo=timezone.utc))
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.pk)
            sess["_fresh"] = True

    def tearDown(self):
        self.ctx.pop()

    def _args(self, **kw):
        return {"team": str(self.team.pk), "view": "month", "period": "custom",
                "from": "2026-01-01", "to": "2026-12-31", **kw}

    def test_the_page_shows_one_column_per_month_for_the_customer(self):
        html = self.client.get("/time/report", query_string=self._args(
            customer=str(self.aldi.pk))).get_data(as_text=True)
        self.assertIn("tt-months", html)
        self.assertIn("Jan 2026", html)
        self.assertIn("Dec 2026", html)
        self.assertIn("Support", html)
        self.assertNotIn("Lidl</td>", html)
        self.assertNotIn('id="tt-report"', html)

    def test_the_csv_carries_the_same_breakdown(self):
        resp = self.client.get("/time/report.csv", query_string=self._args())
        self.assertIn("per month", resp.headers["Content-Disposition"])
        rows = list(csv.reader(io.StringIO(resp.get_data(as_text=True))))
        self.assertEqual(rows[0][:3], ["Customer", "Project", "Jan 2026"])
        self.assertEqual(rows[0][-1], "Total")
        self.assertEqual(len(rows[0]), 2 + 12 + 1)
        aldi = next(r for r in rows if r[0] == "Aldi")
        self.assertEqual(aldi[1], "Support")
        self.assertEqual(aldi[2:5], ["2.00", "0.00", "1.00"])
        self.assertEqual(aldi[-1], "3.00")
        total = rows[-1]
        self.assertEqual(total[0], "Total")
        self.assertEqual(total[2:5], ["2.00", "0.75", "1.00"])
        self.assertEqual(total[-1], "3.75")

    def test_the_excel_flavour_uses_decimal_commas(self):
        resp = self.client.get("/time/report.csv",
                               query_string=self._args(excel="1"))
        body = resp.get_data(as_text=True).lstrip("﻿")
        rows = list(csv.reader(io.StringIO(body), delimiter=";"))
        self.assertEqual(rows[-1][-1], "3,75")

    def test_the_entries_view_is_unchanged_by_default(self):
        html = self.client.get("/time/report", query_string=self._args(
            view="")).get_data(as_text=True)
        self.assertIn('id="tt-report"', html)
        self.assertNotIn("tt-months", html)


if __name__ == "__main__":
    unittest.main()
