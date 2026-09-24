"""The review queue leaves EMPTY projects and folders out — unless asked.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

Reported as: "die Review funktion soll keine leeren projekte/ordner anzeigen,
nur wenn man es gezielt aktiviert". A container with nothing open below it has
nothing to walk through, and walking a queue of them is what made the weekly
review not worth starting.

Two things this pins down. The rule is counted over the WHOLE subtree, so a
folder whose only content is an empty project drops out WITH it (it is only in
the review sidebar because something under it is due — with the empty project
gone the folder has nothing left to hold). And the toggle is a deliberate
opt-in that is remembered on the user, so the queue comes back the way it was
left.
"""
import unittest

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None


@unittest.skipIf(connect is None, "mongoengine not installed")
class ReviewEmptyContainersTests(unittest.TestCase):

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
        from application.models.user import User
        with self.app.app_context():
            self._wipe()
            self.user = User(username="rv-owner", email="rv-owner@test.invalid",
                            password_hash="x", level="premium").save()

            def mk(kind, title, parent=None, **kw):
                return Item(owner=self.user, kind=kind, title=title,
                            parent=parent, **kw).save()

            # Work
            #  ├── BusyProject      (one open task)        → due, kept
            #  ├── DoneProject      (one DONE task)        → due, empty
            #  ├── HollowProject    (nothing at all)       → due, empty
            #  └── OldFolder
            #       └── BuriedProject (nothing at all)     → due, empty
            # ``status="active"`` on a folder is what the app writes when one is
            # created — the sidebar navigator only walks active containers.
            self.work = mk("folder", "ZWork", status="active")
            self.busy = mk("project", "ZBusyProject", self.work,
                           project_type="parallel", status="active")
            mk("task", "ZOpenAction", self.busy, status="open")
            self.done = mk("project", "ZDoneProject", self.work,
                           project_type="parallel", status="active")
            mk("task", "ZFinishedAction", self.done, status="done")
            self.hollow = mk("project", "ZHollowProject", self.work,
                             project_type="parallel", status="active")
            self.old = mk("folder", "ZOldFolder", self.work, status="active")
            self.buried = mk("project", "ZBuriedProject", self.old,
                             project_type="parallel", status="active")
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.pk)
            sess["_fresh"] = True

    # -- helpers -----------------------------------------------------------

    def _queue_names(self):
        from datetime import datetime, timezone
        from application.models.user import User
        from application.views.review import _review_queue
        with self.app.test_request_context():
            user = User.objects(pk=self.user.pk).first()
            return [p.name for p in
                    _review_queue(user, datetime.now(timezone.utc))]

    def _page(self, query=""):
        """The rendered review index. Names alone can't be asserted on: the
        layout also ships a quick-add dialog whose project picker lists every
        container. So the two places that matter are addressed by pk — the
        queue row's link and the sidebar row's ``data-project-id``."""
        resp = self.client.get("/review/" + query)
        self.assertEqual(resp.status_code, 200)
        return resp.data.decode()

    @staticmethod
    def _queued(body, item):
        return f'href="/review/{item.pk}"' in body

    @staticmethod
    def _in_sidebar(body, item):
        # Only the navigator itself — the layout's bulk-move dropdown carries a
        # data-project-id per container as well.
        start = body.index('class="nav flex-column ps-list"')
        sidebar = body[start:body.index("</ul>", start)]
        return f'data-project-id="{item.pk}"' in sidebar

    def _badge(self):
        from datetime import datetime, timezone
        from application.models.user import User
        from application.views.review import review_due_count
        with self.app.test_request_context():
            user = User.objects(pk=self.user.pk).first()
            return review_due_count(user, datetime.now(timezone.utc))

    # -- the default: empty containers are out ------------------------------

    def test_only_the_project_with_open_work_is_queued(self):
        self.assertEqual(self._queue_names(), ["ZBusyProject"])

    def test_a_project_whose_tasks_are_all_done_is_empty(self):
        self.assertNotIn("ZDoneProject", self._queue_names())

    def test_the_badge_counts_what_the_queue_lists(self):
        self.assertEqual(self._badge(), len(self._queue_names()))

    def test_the_index_lists_only_the_busy_project(self):
        body = self._page()
        self.assertTrue(self._queued(body, self.busy))
        self.assertFalse(self._queued(body, self.hollow))
        self.assertFalse(self._queued(body, self.done))

    def test_the_nested_empty_folder_goes_with_its_empty_project(self):
        """The folder is only in the review sidebar as the empty project's
        ancestor, so it must not stay behind once the project is gone —
        otherwise a parent hangs there with nothing under it."""
        body = self._page()
        self.assertFalse(self._queued(body, self.buried))
        self.assertFalse(self._in_sidebar(body, self.buried))
        self.assertFalse(self._in_sidebar(body, self.old))
        # The ancestor of something that IS due stays, of course.
        self.assertTrue(self._in_sidebar(body, self.work))
        self.assertTrue(self._in_sidebar(body, self.busy))

    def test_no_queue_row_reads_zero_open(self):
        self.assertNotIn("0 open", self._page())

    # -- the toggle: they come back ----------------------------------------

    def test_show_empty_brings_them_back(self):
        body = self._page("?show_empty=1")
        for item in (self.busy, self.done, self.hollow, self.buried):
            self.assertTrue(self._queued(body, item),
                            f"{item.name} missing from the queue with show_empty=1")
        # …and with the buried project back, so is the folder above it.
        self.assertTrue(self._in_sidebar(body, self.old))

    def test_the_toggle_is_remembered_on_the_user(self):
        from application.models.user import User
        self.client.get("/review/?show_empty=1")
        with self.app.app_context():
            self.assertTrue(User.objects(pk=self.user.pk).first().review_show_empty)
        # …and the queue built later honours it without the query string.
        self.assertIn("ZHollowProject", self._queue_names())
        self.assertTrue(self._queued(self._page(), self.hollow))

    def test_switching_it_off_again_hides_them(self):
        from application.models.user import User
        self.client.get("/review/?show_empty=1")
        self.client.get("/review/?show_empty=0")
        with self.app.app_context():
            self.assertFalse(User.objects(pk=self.user.pk).first().review_show_empty)
        self.assertEqual(self._queue_names(), ["ZBusyProject"])

    def test_the_toggle_is_offered_on_the_page(self):
        body = self._page()
        self.assertIn("show_empty=1", body)
        self.assertIn("Show empty", body)


if __name__ == "__main__":
    unittest.main()
