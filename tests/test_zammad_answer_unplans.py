"""services/zammad_import — an answered ticket leaves Today.

You plan the ticket's task for today, answer the ticket in Zammad, and the
sync moves the task to ``waiting_reply``. The plan date that brought it into
Today goes in that same pass, so it neither stays in Today nor turns up
tomorrow as "planned, overdue".

Run with ``python -m unittest discover tests``. The end-to-end half needs a
MongoDB (``TestConfig`` points at ``bktodo_test``) and is skipped without one.
Nothing here talks to a real Zammad: ``zammad_import._request`` is replaced by
a fixture.
"""
import unittest
from datetime import datetime, timedelta, timezone

from application.models.item import Item
from application.services.zammad_import import _drop_plan_on_answer

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None


class _User:
    timezone = "UTC"


NOW = datetime.now(timezone.utc)


def _task(status="waiting_reply", plan=NOW, note="Antwort schreiben"):
    return Item(kind="task", title="t", status=status, plan_date=plan,
                plan_note=note)


class DropPlanOnAnswer(unittest.TestCase):

    def test_the_way_into_waiting_reply_drops_todays_plan(self):
        t = _task()
        self.assertTrue(_drop_plan_on_answer(t, "open", _User()))
        self.assertIsNone(t.plan_date)
        self.assertEqual(t.plan_note, "")

    def test_a_timed_plan_from_yesterday_goes_too(self):
        t = _task(plan=NOW - timedelta(days=1))
        self.assertTrue(_drop_plan_on_answer(t, "next", _User()))
        self.assertIsNone(t.plan_date)

    def test_a_plan_after_today_stays(self):
        later = NOW + timedelta(days=3)
        t = _task(plan=later)
        self.assertFalse(_drop_plan_on_answer(t, "open", _User()))
        self.assertEqual(t.plan_date, later)

    def test_already_waiting_keeps_a_plan_set_to_chase(self):
        # Every later sync pass sees waiting_reply again; a plan date set
        # while waiting is deliberate and survives them.
        t = _task()
        self.assertFalse(_drop_plan_on_answer(t, "waiting_reply", _User()))
        self.assertIsNotNone(t.plan_date)

    def test_any_other_status_keeps_the_plan(self):
        t = _task(status="next")
        self.assertFalse(_drop_plan_on_answer(t, "open", _User()))
        self.assertIsNotNone(t.plan_date)


TICKET = {"id": 4712, "number": "70002", "title": "Rückfrage Lizenz",
          "state": "open", "owner_id": 9}


def _fixture_request(tags):
    def _request(url, token, *, params=None, method="GET", body=None):
        if "/ticket_articles/by_ticket/" in url:
            return []
        if url.endswith("/tags"):
            return {"tags": list(tags)}
        raise AssertionError(f"unexpected Zammad call: {url}")
    return _request


@unittest.skipIf(connect is None, "mongoengine not installed")
class SyncUnplansAnsweredTicket(unittest.TestCase):

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
            from application.models.user import User
            Item.all_objects.delete()
            User.objects.delete()
        disconnect(alias="default")

    def setUp(self):
        from application.models.tag import Tag
        from application.models.user import User
        with self.app.app_context():
            Item.all_objects.delete()
            User.objects.delete()
            self.user = User(username="zanswer", email="za@test.invalid",
                             password_hash="x", level="premium",
                             timezone="UTC", zammad_import_time=False,
                             tags=[Tag(name="Warte auf Kunde",
                                       maps_to_status="waiting_reply")]).save()
            self.task = Item(owner=self.user, kind="task", title="old",
                             source="zammad", external_id="zammad:4712",
                             external_ref="70002", status="next",
                             plan_date=NOW, plan_note="antworten").save()

    def _sync(self, tags):
        from application.services import zammad_import as zi
        original = zi._request
        zi._request = _fixture_request(tags)
        self.addCleanup(lambda: setattr(zi, "_request", original))
        stats = zi.ZammadStats()
        with self.app.app_context():
            zi._upsert_ticket(self.user, dict(TICKET),
                              "https://z.test.invalid/api/v1", "tok", None,
                              stats, dry_run=False)
        self.task.reload()

    def test_the_answer_takes_the_task_out_of_today(self):
        self._sync(["Warte auf Kunde"])
        self.assertEqual(self.task.status, "waiting_reply")
        self.assertIsNone(self.task.plan_date)
        self.assertEqual(self.task.plan_note, "")

    def test_a_later_pass_keeps_a_plan_set_while_waiting(self):
        self._sync(["Warte auf Kunde"])
        self.task.plan_date = NOW
        self.task.save()
        self._sync(["Warte auf Kunde"])
        self.assertEqual(self.task.status, "waiting_reply")
        self.assertIsNotNone(self.task.plan_date)

    def test_no_answer_keeps_the_plan(self):
        self._sync([])
        self.assertNotEqual(self.task.status, "waiting_reply")
        self.assertIsNotNone(self.task.plan_date)


if __name__ == "__main__":
    unittest.main()
