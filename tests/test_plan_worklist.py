"""The Plan view's work list: the same day as the timeline, with more per task.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

From the report: *"Man auf eine Arbeitsliste umstellen koennen, die mehr
informationen der tasks zeigt, als der kalender platz hat so das man nicht erst
in die tasks klicken muss um sie zu bearbeiten"*. Pinned here:

* the switch is remembered on the user (``plan_layout``) like the grouping, and
  an unknown value falls back to the timeline instead of reaching the template;
* the list carries the SAME tasks the timeline does (timed ones and the Due
  part), and what the calendar has no room for: the note, the latest comments,
  where the task is filed, labels, attachment count;
* the edits on a card go to the endpoints the rest of the app uses, so the
  ones the card's JavaScript sends are checked here as plain requests.
"""
import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover, no mongoengine installed
    connect = None

BERLIN = ZoneInfo("Europe/Berlin")
DAY = datetime(2026, 9, 24, tzinfo=BERLIN).date()
NOON = datetime(2026, 9, 24, 12, 0, tzinfo=BERLIN)


def _clock(fixed):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)

    return Clock


@unittest.skipIf(connect is None, "mongoengine not installed")
class PlanWorklistTests(unittest.TestCase):

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
        from application.models.item import Item
        from application.models.user import User
        Item.all_objects.delete()
        User.objects.delete()

    def setUp(self):
        from application.models.item import Comment, Item, NoteFile
        from application.models.user import User
        with self.app.app_context():
            self._wipe()
            self.user = User(username="wl-test", email="wl@test.invalid",
                             password_hash="x", level="premium",
                             timezone="Europe/Berlin",
                             plan_day_start=9, plan_day_end=18).save()
            self.project = Item(owner=self.user, kind="project", title="Webshop",
                                project_type="parallel").save()
            self.timed = Item(
                owner=self.user, kind="task", title="Rechnung schreiben",
                parent=self.project, tags=["buero"],
                plan_date=datetime(2026, 9, 24, 10, 0, tzinfo=BERLIN),
                estimated_minutes=45,
                note="Kunde **Mueller** wartet auf die Rechnung.",
                comments=[Comment(author=self.user, body="erste Rueckfrage"),
                          Comment(author=self.user, body="zweite Rueckfrage"),
                          Comment(author=self.user, body="dritte Antwort")],
                note_files=[NoteFile(token="t1", filename="angebot.pdf",
                                     data=b"%PDF")],
            ).save()
            # On the day, no time: the Due part of the day.
            self.untimed = Item(
                owner=self.user, kind="task", title="Server patchen",
                plan_date=datetime(2026, 9, 24, 0, 0, tzinfo=BERLIN)).save()
            # Not on this day at all.
            self.other = Item(
                owner=self.user, kind="task", title="Urlaub planen",
                plan_date=datetime(2026, 10, 3, 0, 0, tzinfo=BERLIN)).save()
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.pk)
            sess["_fresh"] = True

    def _page(self, query=""):
        with mock.patch("application.views.tasks.datetime", _clock(NOON)):
            resp = self.client.get(
                f"/tasks/today?date={DAY.isoformat()}&view=plan{query}")
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def _user(self):
        from application.models.user import User
        with self.app.app_context():
            return User.objects(pk=self.user.pk).first()

    def _task(self, task):
        from application.models.item import Item
        with self.app.app_context():
            return Item.objects(pk=task.pk).first()

    # -- the switch --------------------------------------------------------

    def test_timeline_is_the_default(self):
        html = self._page()
        self.assertIn('id="plan-track"', html)
        self.assertNotIn('id="plan-worklist"', html)
        self.assertIn("playout=list", html)  # the switch is offered

    def test_switch_is_remembered(self):
        html = self._page("&playout=list")
        self.assertIn('id="plan-worklist"', html)
        self.assertNotIn('id="plan-track"', html)
        self.assertEqual(self._user().plan_layout, "list")
        # Next visit without the parameter comes back as the list.
        self.assertIn('id="plan-worklist"', self._page())
        self._page("&playout=timeline")
        self.assertEqual(self._user().plan_layout, "timeline")

    def test_unknown_layout_falls_back_to_the_timeline(self):
        html = self._page("&playout=../../etc")
        self.assertIn('id="plan-track"', html)
        self.assertEqual(self._user().plan_layout, "timeline")

    # -- what is on it -----------------------------------------------------

    def test_same_tasks_as_the_day(self):
        html = self._page("&playout=list")
        self.assertIn("Rechnung schreiben", html)
        self.assertIn("Server patchen", html)
        self.assertNotIn("Urlaub planen", html)
        # Timed first (with its slot), then the Due part.
        self.assertLess(html.index("Rechnung schreiben"), html.index("Server patchen"))
        self.assertIn("10:00 to 10:45", html)

    def test_card_carries_what_the_calendar_has_no_room_for(self):
        html = self._page("&playout=list")
        self.assertIn("<strong>Mueller</strong>", html)       # rendered note
        self.assertIn("Webshop", html)                        # where it is filed
        self.assertIn("buero", html)                          # label
        self.assertIn('<i class="bi bi-paperclip"></i> 1', html)
        # The latest two comments, and a way to the rest.
        self.assertIn("zweite Rueckfrage", html)
        self.assertIn("dritte Antwort", html)
        self.assertNotIn("erste Rueckfrage", html)
        self.assertIn("All 3 comments", html)
        # Everything else is one click away in the inspector.
        self.assertIn(f"/tasks/{self.timed.pk}/edit", html)

    # -- the edits the card sends ------------------------------------------

    def test_card_edits_reach_the_existing_endpoints(self):
        xhr = {"X-Requested-With": "XMLHttpRequest"}
        r = self.client.post(f"/tasks/{self.untimed.pk}/schedule",
                             data={"date": DAY.isoformat(), "hour": "14", "minute": "30"},
                             headers=xhr)
        self.assertEqual(r.status_code, 200)
        pd = self._task(self.untimed).plan_date.astimezone(BERLIN)
        self.assertEqual((pd.hour, pd.minute), (14, 30))

        r = self.client.post(f"/tasks/{self.timed.pk}/quick-edit",
                             data={"tags_submitted": "1", "tags": ["buero", "kunde"]},
                             headers=xhr)
        self.assertEqual(r.get_json()["tags"], ["buero", "kunde"])

        r = self.client.post(f"/tasks/{self.timed.pk}/note",
                             data={"note": "neu *geschrieben*"},
                             headers={"X-Partial": "1"})
        self.assertIn("<em>geschrieben</em>", r.get_data(as_text=True))

        r = self.client.post(f"/tasks/{self.timed.pk}/comment",
                             data={"body": "vom Arbeitsblatt"},
                             headers={"X-Inspector-Request": "1"})
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(self._task(self.timed).comments[-1].body, "vom Arbeitsblatt")

        r = self.client.post(f"/tasks/{self.timed.pk}/complete", headers=xhr)
        self.assertEqual(self._task(self.timed).status, "done")
        # A done task that was planned with a time stays on the list, struck.
        html = self._page("&playout=list")
        self.assertIn("wl-card is-done", html)


@unittest.skipIf(connect is None, "mongoengine not installed")
class PlanWorklistCustomerTimeTests(unittest.TestCase):
    """The second round on the same task: *"die seite soll noch nach kunden
    gruppieren"* and *"zeit tracking fehlt auch auf der Seite"*.

    * the whole list is grouped by customer (named on the container, or
      through its billing project), "No customer" last, timed tasks first
      inside a group;
    * a team user gets the tracked total and the stopwatch on every card, and
      the inspector's own time section on demand from ``/time/task/<id>``;
      somebody without a team gets neither (there is nothing to book on).
    """

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
        from application.models.item import Item
        from application.models.team import Team
        from application.models.timetracking import (
            Customer, TimeBucket, TimeTrackingProject)
        from application.models.user import User
        TimeBucket.objects.delete()
        Item.all_objects.delete()
        TimeTrackingProject.objects.delete()
        Customer.objects.delete()
        Team.objects.delete()
        User.objects.delete()

    def setUp(self):
        from application.models.item import Item
        from application.models.team import Team
        from application.models.timetracking import Customer, TimeTrackingProject
        from application.models.user import User
        from application.services.timetracking import add_entry

        def at(h, m=0):
            return datetime(2026, 9, 24, h, m, tzinfo=BERLIN)

        with self.app.app_context():
            self._wipe()
            user = User(username="wl-team", email="wl-team@test.invalid",
                        password_hash="x", level="team_admin",
                        timezone="Europe/Berlin", plan_layout="list",
                        plan_day_start=9, plan_day_end=18).save()
            team = Team(name="Kuhn IT", admin=user, admins=[user]).save()
            user.team = team
            user.save()
            self.user = user
            zeta = Customer(team=team, name="Zeta AG").save()
            acme = Customer(team=team, name="Acme GmbH").save()
            billing = TimeTrackingProject(team=team, customer=zeta,
                                          name="Support").save()
            # Acme is named on the folder, Zeta through the billing binding.
            acme_proj = Item(owner=user, kind="project", title="Acme Webshop",
                             project_type="parallel", customer=acme).save()
            zeta_proj = Item(owner=user, kind="project", title="Zeta Betrieb",
                             project_type="parallel", time_project=billing).save()
            self.acme_late = Item(owner=user, kind="task", title="Acme spaet",
                                  parent=acme_proj, plan_date=at(15)).save()
            self.acme_untimed = Item(owner=user, kind="task", title="Acme ohne Zeit",
                                     parent=acme_proj, plan_date=at(0)).save()
            self.acme_early = Item(owner=user, kind="task", title="Acme frueh",
                                   parent=acme_proj, plan_date=at(9)).save()
            self.zeta = Item(owner=user, kind="task", title="Zeta Patch",
                             parent=zeta_proj, plan_date=at(8)).save()
            self.loose = Item(owner=user, kind="task", title="Einkaufen",
                              plan_date=at(7)).save()
            add_entry(project=billing, user=user, minutes=90, spent_on=at(8),
                      source_item=self.zeta)
            add_entry(project=billing, user=user, minutes=15, spent_on=at(9),
                      source_item=self.zeta)
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.pk)
            sess["_fresh"] = True

    def _page(self):
        with mock.patch("application.views.tasks.datetime", _clock(NOON)):
            resp = self.client.get(f"/tasks/today?date={DAY.isoformat()}&view=plan")
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def test_grouped_by_customer_no_customer_last(self):
        html = self._page()
        order = [html.index(s) for s in (
            "Acme GmbH", "Acme frueh", "Acme spaet", "Acme ohne Zeit",
            "Zeta AG", "Zeta Patch", "No customer", "Einkaufen")]
        self.assertEqual(order, sorted(order))
        # The rails' Flat / Project / Customer switch is not offered: the
        # list has no rails and is always grouped by customer.
        self.assertNotIn('aria-label="Group the rails"', html)

    def test_card_shows_tracked_time_and_stopwatch(self):
        html = self._page()
        self.assertIn("1h 45m", html)                  # 90 + 15 on Zeta Patch
        self.assertIn("nothing tracked yet", html)
        self.assertIn(f'data-task-id="{self.acme_early.pk}"', html)
        self.assertIn("tt-timer-start", html)
        self.assertIn(f"/time/task/{self.zeta.pk}", html)

    def test_running_timer_shows_stop_on_its_card(self):
        r = self.client.post("/time/timer/start",
                             data={"task_id": str(self.zeta.pk)},
                             headers={"X-Partial": "task"})
        self.assertEqual(r.status_code, 200)
        html = self._page()
        card = html[html.index(f'data-task-id="{self.zeta.pk}"'):]
        card = card[:card.index("</article>")]
        self.assertIn("tt-timer-stop", card)
        self.assertIn("tt-timer-elapsed", card)

    def test_block_endpoint_is_the_inspector_section(self):
        r = self.client.get(f"/time/task/{self.zeta.pk}")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("Time tracking", body)
        self.assertIn("time-add", body)                # manual entry
        self.assertIn("1h 30m", body)                  # an entry in the list

    def test_item_minutes_is_one_batched_answer(self):
        from application.services.timetracking import item_minutes
        from application.models.user import User
        with self.app.app_context():
            user = User.objects(pk=self.user.pk).first()
            got = item_minutes(user, [self.zeta, self.loose])
        self.assertEqual(got, {str(self.zeta.pk): 105})

    def test_without_a_team_there_is_no_time_section(self):
        from application.models.user import User
        with self.app.app_context():
            solo = User(username="wl-solo", email="solo@test.invalid",
                        password_hash="x", level="premium",
                        timezone="Europe/Berlin", plan_layout="list").save()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(solo.pk)
        html = self._page()
        self.assertIn('id="plan-worklist"', html)
        self.assertNotIn("wl-time", html)


if __name__ == "__main__":
    unittest.main()
