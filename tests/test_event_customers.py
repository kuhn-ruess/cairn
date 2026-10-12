"""Calendar events filed under a customer — by hand, or suggested from the title.

Run with ``python -m unittest discover tests``. The DB half needs a MongoDB
(``TestConfig`` points at ``bktodo_test``) and is skipped without one.

Asked as: *"Termine wäre cool Kunden zuzuordnen, dass wenn man den Plan-Modus
verwendet, der sich daran orientieren kann"* and *"anhand des Namens sollte er
einen Vorschlag machen"*. Pinned here: the title suggests only an unambiguous
customer; a hand-filed answer (including "none") beats the suggestion and holds
for the whole series; the panel lists that customer's open tasks; "Plan my day"
puts the Next actions of a customer met that day first; Log time preselects
the customer's project.
"""
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

from application.services.event_customers import core_name, series_uid, suggest

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None

CUSTOMERS = [("a", "Acme GmbH & Co. KG"), ("g", "Globex AG"),
             ("s", "Acme Services GmbH"), ("i", "IT")]


class SuggestFromTitle(unittest.TestCase):

    def test_a_name_in_the_title_suggests_its_customer(self):
        self.assertEqual(suggest("Globex Jour Fixe", CUSTOMERS), "g")

    def test_legal_forms_and_case_do_not_matter(self):
        self.assertEqual(suggest("call with ACME about invoices", CUSTOMERS), "a")

    def test_whole_words_only(self):
        self.assertIsNone(suggest("Globexpo Messe", CUSTOMERS))

    def test_the_longer_of_two_nested_names_is_meant(self):
        self.assertEqual(suggest("Acme Services: Review", CUSTOMERS), "s")

    def test_two_customers_in_one_title_suggest_nothing(self):
        self.assertIsNone(suggest("Acme / Globex Abstimmung", CUSTOMERS))

    def test_a_name_two_customers_share_suggests_nothing(self):
        twice = [("x", "Acme GmbH"), ("y", "Acme GmbH")]
        self.assertIsNone(suggest("Acme Weekly", twice))

    def test_a_name_too_short_is_never_matched(self):
        self.assertEqual(core_name("IT"), "")
        self.assertIsNone(suggest("IT Meeting", CUSTOMERS))

    def test_no_name_no_suggestion(self):
        self.assertIsNone(suggest("Mittagessen", CUSTOMERS))


class SeriesUid(unittest.TestCase):

    def test_caldav_occurrence(self):
        self.assertEqual(series_uid("abc-123#20261012T080000Z"), "abc-123")

    def test_google_occurrence(self):
        self.assertEqual(series_uid("k3j4h5_20261012T080000Z"), "k3j4h5")

    def test_single_event_is_its_own_series(self):
        self.assertEqual(series_uid("abc-123"), "abc-123")


BERLIN = ZoneInfo("Europe/Berlin")
DAY = datetime(2026, 9, 24, tzinfo=BERLIN).date()


def _clock(fixed):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)
    return Clock


def _event(uid, title, h0, h1):
    from application.services.calendar_sync import CalendarEvent
    return CalendarEvent(uid=uid, title=title,
                         start=datetime(2026, 9, 24, h0, tzinfo=BERLIN),
                         end=datetime(2026, 9, 24, h1, tzinfo=BERLIN))


@unittest.skipIf(connect is None, "mongoengine not installed")
class EventCustomerDbTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from application import create_app
        disconnect(alias="default")
        try:
            cls.app = create_app("application.config.TestConfig")
            with cls.app.app_context():
                from application.models.user import User
                User.objects.first()
        except (ServerSelectionTimeoutError, PyMongoError) as exc:
            raise unittest.SkipTest(f"no MongoDB for the test database: {exc}")

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            cls._wipe()
        disconnect(alias="default")

    @staticmethod
    def _wipe():
        from application.models.calendar import EventCustomer
        from application.models.item import Item
        from application.models.team import Team
        from application.models.timetracking import (
            Customer, TimeBucket, TimeTrackingProject)
        from application.models.user import User
        for doc in (EventCustomer, TimeBucket, TimeTrackingProject, Customer,
                    Team, User):
            doc.objects.delete()
        Item.all_objects.delete()

    def setUp(self):
        from application.models.calendar import ExternalCalendar
        from application.models.item import Item
        from application.models.team import Team
        from application.models.timetracking import Customer, TimeTrackingProject
        from application.models.user import User
        self.ctx = self.app.app_context()
        self.ctx.push()
        self._wipe()
        self.user = User(username="evc", email="evc@test.invalid",
                         password_hash="x", level="team_admin",
                         timezone="Europe/Berlin",
                         plan_day_start=9, plan_day_end=18,
                         calendars=[ExternalCalendar(name="Work", provider="ical",
                                                     show_in_today=True)]).save()
        team = Team(name="T", admin=self.user, admins=[self.user]).save()
        self.user.team = team
        self.user.save()
        self.acme = Customer(team=team, name="Acme GmbH").save()
        self.globex = Customer(team=team, name="Globex").save()
        self.acme_support = TimeTrackingProject(team=team, customer=self.acme,
                                                name="Support").save()
        self.globex_web = TimeTrackingProject(team=team, customer=self.globex,
                                              name="Website").save()
        self.user.tt_last_project = str(self.globex_web.pk)
        self.user.save()
        acme_f = Item(owner=self.user, kind="project", title="Acme work",
                      customer=self.acme).save()
        globex_f = Item(owner=self.user, kind="project", title="Globex work",
                        customer=self.globex).save()
        self.globex_task = Item(owner=self.user, kind="task", title="Globex thing",
                                parent=globex_f, status="next", sort_order=0,
                                estimated_minutes=60).save()
        self.acme_task = Item(owner=self.user, kind="task", title="Acme thing",
                              parent=acme_f, status="next", sort_order=1,
                              estimated_minutes=60).save()
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.pk)
            sess["_fresh"] = True

    def tearDown(self):
        self.ctx.pop()

    def _for(self, *events):
        from application.services.event_customers import for_events
        return for_events(self.user, list(events))

    # -- resolve -----------------------------------------------------------

    def test_title_suggests_and_says_so(self):
        info = self._for(SimpleNamespace(uid="e1", title="Acme Weekly"))["e1"]
        self.assertEqual((info["id"], info["suggested"]), (str(self.acme.pk), True))

    def test_filed_by_hand_wins_for_the_whole_series(self):
        from application.services.event_customers import assign
        assign(self.user, "e1#20260924T080000Z", str(self.globex.pk), "Acme Weekly")
        out = self._for(SimpleNamespace(uid="e1#20261001T080000Z", title="Acme Weekly"))
        info = out["e1#20261001T080000Z"]
        self.assertEqual((info["id"], info["suggested"]), (str(self.globex.pk), False))

    def test_none_on_purpose_silences_the_suggestion(self):
        from application.services.event_customers import assign
        assign(self.user, "e1", "none")
        self.assertEqual(self._for(SimpleNamespace(uid="e1", title="Acme Weekly")), {})

    def test_forgetting_brings_the_suggestion_back(self):
        from application.services.event_customers import assign
        assign(self.user, "e1", "none")
        assign(self.user, "e1", "")
        self.assertIn("e1", self._for(SimpleNamespace(uid="e1", title="Acme Weekly")))

    # -- endpoints ---------------------------------------------------------

    def test_a_stranger_customer_is_refused(self):
        from bson import ObjectId
        resp = self.client.post("/tasks/event-customer", data={
            "uid": "e1", "customer": str(ObjectId())})
        self.assertEqual(resp.status_code, 400)

    def test_panel_lists_the_customers_open_tasks(self):
        resp = self.client.get("/tasks/event-customer?uid=e1&title=Acme+Weekly")
        data = resp.get_json()
        self.assertEqual(data["current"]["name"], "Acme GmbH")
        self.assertFalse(data["filed"])
        self.assertEqual([t["title"] for t in data["tasks"]], ["Acme thing"])
        self.assertEqual({c["label"] for c in data["customers"]},
                         {"Acme GmbH", "Globex"})

    def test_saving_answers_what_the_chip_shows(self):
        resp = self.client.post("/tasks/event-customer", data={
            "uid": "e1", "title": "Weekly", "customer": str(self.globex.pk)})
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["current"]["name"], "Globex")

    def test_log_time_with_the_event_customer_preselects_its_project(self):
        resp = self.client.get(f"/time/quick?customer={self.acme.pk}")
        html = resp.get_data(as_text=True)
        self.assertRegex(html, rf'value="{self.acme_support.pk}"\s+selected')

    def test_the_plan_page_shows_the_chip(self):
        events = [_event("e1", "Acme Weekly", 15, 16)]
        with mock.patch("application.services.calendar_sync.fetch_events",
                        return_value=events):
            html = self.client.get(
                f"/tasks/today?date={DAY.isoformat()}&view=plan").get_data(as_text=True)
        self.assertIn("event-cust-btn has-customer is-suggested", html)
        self.assertIn("Acme GmbH?", html)
        self.assertIn(f'data-log-customer="{self.acme.pk}"', html)

    # -- Plan my day -------------------------------------------------------

    def _plan(self, events):
        at = datetime(2026, 9, 24, 8, 0, tzinfo=BERLIN)
        with mock.patch("application.views.tasks.datetime", _clock(at)), \
                mock.patch("application.services.calendar_sync.fetch_events",
                           return_value=events):
            resp = self.client.post("/tasks/auto-plan", data={"date": DAY.isoformat()})
        self.assertEqual(resp.status_code, 200)
        from application.models.item import Item
        return {t.title: t.plan_date for t in
                Item.objects(pk__in=[self.acme_task.pk, self.globex_task.pk])}

    def test_without_a_meeting_next_actions_keep_their_order(self):
        when = self._plan([])
        self.assertLess(when["Globex thing"], when["Acme thing"])

    def test_the_customer_met_today_comes_first(self):
        when = self._plan([_event("e1", "Acme Weekly", 15, 16)])
        self.assertLess(when["Acme thing"], when["Globex thing"])


if __name__ == "__main__":
    unittest.main()
