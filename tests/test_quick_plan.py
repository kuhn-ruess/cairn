"""Quick plan and the NEXT arrow are one idea.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

The report: the inspector's Quick plan and the NEXT arrow on a list row both
mean "this is what I do next", but Quick plan set a plan DATE (today, tomorrow,
the day after) while the arrow set the status. A time cannot be chosen
sensibly from either, so Quick plan now puts the task into Next as well and
keeps the step it was given as the task's name in every list. The concrete
time is chosen in Today. Ticking such a row off ends the STEP, not the task:
a log comment says which step was done, the status the quick plan replaced
comes back, and a plan date of today or earlier (what kept it in Today) goes.
"""
import unittest
from datetime import datetime, timedelta, timezone

try:
    from mongoengine import disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover (no mongoengine installed)
    disconnect = None

EMAIL = "quickplan@test.invalid"


@unittest.skipIf(disconnect is None, "mongoengine not installed")
class QuickPlanTests(unittest.TestCase):

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
        for user in User.objects(email=EMAIL):
            Item.all_objects(owner=user).delete()
            user.delete()

    def setUp(self):
        from application.models.item import Item
        from application.models.user import User
        with self.app.app_context():
            self._wipe()
            self.user = User(username="qp-test", email=EMAIL, password_hash="x",
                             level="premium", timezone="UTC").save()
            self.task = Item(owner=self.user, kind="task", title="Angebot Website",
                             status="waiting").save()
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.pk)
            sess["_fresh"] = True

    def _reload(self):
        from application.models.item import Item
        with self.app.app_context():
            return Item.objects(pk=self.task.pk).first()

    def _post(self, **form):
        return self.client.post(f"/tasks/{self.task.pk}/quick-plan", data=form)

    # -- setting the step ----------------------------------------------------

    def test_quick_plan_puts_the_task_into_next_and_sets_no_date(self):
        resp = self._post(when="next", plan_note="Preis nachrechnen")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["status"], "next")
        task = self._reload()
        self.assertEqual(task.status, "next")
        self.assertEqual(task.plan_note, "Preis nachrechnen")
        self.assertIsNone(task.plan_date)
        self.assertEqual(task.plan_prev_status, "waiting")

    def test_the_old_day_buttons_are_gone(self):
        self.assertEqual(self._post(when="today", plan_note="x").status_code, 400)
        self.assertIsNone(self._reload().plan_date)

    def test_a_second_step_keeps_the_first_status(self):
        self._post(when="next", plan_note="eins")
        self._post(when="next", plan_note="zwei")
        task = self._reload()
        self.assertEqual(task.plan_note, "zwei")
        self.assertEqual(task.plan_prev_status, "waiting")

    # -- ticking it off ------------------------------------------------------

    def test_done_ends_the_step_logs_it_and_hands_the_status_back(self):
        self._post(when="next", plan_note="Preis nachrechnen")
        resp = self._post(when="done")
        self.assertEqual(resp.status_code, 200)
        task = self._reload()
        self.assertEqual(task.status, "waiting")
        self.assertEqual(task.plan_note, "")
        self.assertEqual(task.plan_prev_status, "")
        self.assertIsNone(task.completed_at)
        self.assertEqual(task.comments[-1].body, "Quick plan done: Preis nachrechnen")
        self.assertEqual(task.comments[-1].author.pk, self.user.pk)

    def test_a_task_already_next_stays_next(self):
        from application.models.item import Item
        with self.app.app_context():
            Item.objects(pk=self.task.pk).update_one(set__status="next")
        self._post(when="next", plan_note="anrufen")
        self._post(when="done")
        self.assertEqual(self._reload().status, "next")

    def test_a_status_changed_by_hand_meanwhile_is_kept(self):
        from application.models.item import Item
        self._post(when="next", plan_note="anrufen")
        with self.app.app_context():
            Item.objects(pk=self.task.pk).update_one(set__status="review")
        self._post(when="done")
        self.assertEqual(self._reload().status, "review")

    def test_done_takes_todays_plan_off_but_keeps_a_later_one(self):
        from application.models.item import Item
        now = datetime.now(timezone.utc)
        self._post(when="next", plan_note="schreiben")
        with self.app.app_context():
            Item.objects(pk=self.task.pk).update_one(set__plan_date=now)
        self._post(when="done")
        self.assertIsNone(self._reload().plan_date)

        later = (now + timedelta(days=3)).replace(microsecond=0)
        self._post(when="next", plan_note="nochmal")
        with self.app.app_context():
            Item.objects(pk=self.task.pk).update_one(set__plan_date=later)
        self._post(when="done")
        self.assertEqual(self._reload().plan_date.replace(tzinfo=timezone.utc), later)

    def test_clear_drops_the_step_without_a_comment(self):
        self._post(when="next", plan_note="vergessen")
        self._post(when="clear")
        task = self._reload()
        self.assertEqual(task.plan_note, "")
        self.assertEqual(task.status, "waiting")
        self.assertEqual(len(task.comments), 0)

    def test_done_without_a_step_changes_nothing(self):
        self._post(when="done")
        task = self._reload()
        self.assertEqual(task.status, "waiting")
        self.assertEqual(len(task.comments), 0)

    def test_an_old_quick_plan_with_a_date_still_ends(self):
        """Steps set before Quick plan meant Next carry a plan date and no
        remembered status: ending them clears both and leaves the status."""
        from application.models.item import Item
        with self.app.app_context():
            Item.objects(pk=self.task.pk).update_one(
                set__status="open", set__plan_note="alt",
                set__plan_date=datetime.now(timezone.utc))
        self._post(when="done")
        task = self._reload()
        self.assertEqual(task.status, "open")
        self.assertEqual(task.plan_note, "")
        self.assertIsNone(task.plan_date)
        self.assertEqual(task.comments[-1].body, "Quick plan done: alt")

    # -- what Today shows ----------------------------------------------------

    def test_today_lists_the_step_as_the_name_with_a_step_check(self):
        from application.models.user import User
        with self.app.app_context():
            User.objects(pk=self.user.pk).update_one(set__today_view_mode="list")
        self._post(when="next", plan_note="Preis nachrechnen")
        html = self.client.get("/tasks/today?view=list").get_data(as_text=True)
        rail = html[html.index('id="today-next"'):]
        self.assertIn("Preis nachrechnen", rail)
        self.assertIn("(Angebot Website)", rail)
        self.assertIn(f'action="/tasks/{self.task.pk}/quick-plan"', rail)
        self.assertIn('name="when" value="done"', rail)

    def test_a_plain_next_task_still_completes(self):
        from application.models.item import Item
        with self.app.app_context():
            Item.objects(pk=self.task.pk).update_one(set__status="next")
        html = self.client.get("/tasks/today?view=list").get_data(as_text=True)
        rail = html[html.index('id="today-next"'):]
        self.assertIn(f'action="/tasks/{self.task.pk}/complete"', rail)
        self.assertNotIn('name="when" value="done"', rail)

    def test_the_plan_view_shows_the_step_too(self):
        self._post(when="next", plan_note="Preis nachrechnen")
        html = self.client.get("/tasks/today?view=plan").get_data(as_text=True)
        self.assertIn("Preis nachrechnen", html)
        self.assertIn(f'action="/tasks/{self.task.pk}/quick-plan"', html)


if __name__ == "__main__":
    unittest.main()
