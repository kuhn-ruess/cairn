"""Every task list view has a refresh button, and a refresh drops what is gone.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

"Es ist nicht effizient wenn man immer die komplette Seite neu laden muss, dass
erledigte oder aus dem Plan genommene Tasks verschwinden." The button
(``tasks/_refresh_button.html``) re-renders the view's ``<main>`` in place
through the app's existing background refresh (``sortable-tasks.js``): one GET
of the same URL with ``X-Requested-With: background-refresh``. These tests pin
that the button is on every list view and that the answer to that request no
longer carries a task that was completed or taken out of the plan meanwhile.
``test_list_refresh_browser.py`` clicks it in a real browser.
"""
import re
import unittest
from datetime import datetime, time
from zoneinfo import ZoneInfo

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None

BERLIN = ZoneInfo("Europe/Berlin")
REFRESH = {"X-Requested-With": "background-refresh"}


@unittest.skipIf(connect is None, "mongoengine not installed")
class ListRefreshButtonTests(unittest.TestCase):

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
        from application.models.item import Item
        from application.models.perspective import CustomPerspective
        from application.models.tag import Tag
        from application.models.user import User
        today = datetime.now(BERLIN).date()
        self.planned = datetime.combine(today, time(0, 0)).replace(tzinfo=BERLIN)
        with self.app.app_context():
            self._wipe()
            self.user = User(username="rf-test", email="rf@test.invalid",
                             password_hash="x", level="premium",
                             timezone="Europe/Berlin",
                             tags=[Tag(name="kunde", color="#6c757d")],
                             custom_perspectives=[CustomPerspective(name="Alles")]
                             ).save()
            self.project = Item(owner=self.user, kind="project", title="Webshop",
                                project_type="parallel").save()
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.pk)
            sess["_fresh"] = True

    def _task(self, title, **kw):
        from application.models.item import Item
        with self.app.app_context():
            return Item(owner=self.user, kind="task", title=title, **kw).save()

    def _ids(self, url, headers=None):
        resp = self.client.get(url, headers=headers or {})
        self.assertEqual(resp.status_code, 200, url)
        return set(re.findall(r'data-task-id="([0-9a-f]{24})"',
                              resp.get_data(as_text=True)))

    def _views(self):
        pid = self.user.custom_perspectives[0].pid
        return [
            "/tasks/inbox",
            "/tasks/next",
            "/tasks/flagged",
            "/tasks/completed",
            "/tasks/today?view=list",
            "/tasks/today?view=calendar",
            "/tasks/today?view=plan",
            f"/projects/{self.project.pk}",
            "/tags/kunde",
            f"/perspectives/{pid}",
        ]

    def test_every_list_view_has_the_button(self):
        self._task("irgendwas", parent=self.project, tags=["kunde"])
        for url in self._views():
            with self.subTest(url=url):
                resp = self.client.get(url)
                self.assertEqual(resp.status_code, 200)
                html = resp.get_data(as_text=True)
                self.assertEqual(html.count("data-refresh-list"), 1)
                # Inside <main>: that is what the swap replaces, so the
                # spinning stops when the fresh list is in.
                main = html[html.index("<main"):html.index("</main>")]
                self.assertIn("data-refresh-list", main)

    def test_refresh_drops_a_completed_task_from_the_inbox(self):
        keep = self._task("bleibt")
        done = self._task("wird erledigt")
        self.assertTrue({str(keep.pk), str(done.pk)}
                        <= self._ids("/tasks/inbox"))
        with self.app.app_context():
            done.reload().complete(by_user=self.user)
        ids = self._ids("/tasks/inbox", REFRESH)
        self.assertIn(str(keep.pk), ids)
        self.assertNotIn(str(done.pk), ids)

    def test_refresh_drops_unplanned_and_completed_tasks_from_today(self):
        url = "/tasks/today?view=list&days=1&group=none"
        keep = self._task("bleibt", parent=self.project, plan_date=self.planned)
        unplan = self._task("aus dem Plan", parent=self.project,
                            plan_date=self.planned)
        done = self._task("erledigt", parent=self.project, plan_date=self.planned)
        self.assertTrue({str(keep.pk), str(unplan.pk), str(done.pk)}
                        <= self._ids(url))
        with self.app.app_context():
            t = unplan.reload()
            t.plan_date = None
            t.save()
            done.reload().complete(by_user=self.user)
        ids = self._ids(url, REFRESH)
        self.assertIn(str(keep.pk), ids)
        self.assertNotIn(str(unplan.pk), ids)
        self.assertNotIn(str(done.pk), ids)

    def test_refresh_drops_a_completed_task_from_the_project(self):
        url = f"/projects/{self.project.pk}"
        keep = self._task("bleibt", parent=self.project)
        done = self._task("erledigt", parent=self.project)
        self.assertTrue({str(keep.pk), str(done.pk)} <= self._ids(url))
        with self.app.app_context():
            done.reload().complete(by_user=self.user)
        ids = self._ids(url, REFRESH)
        self.assertIn(str(keep.pk), ids)
        self.assertNotIn(str(done.pk), ids)


if __name__ == "__main__":
    unittest.main()
