"""The left sidebar is sorted into groups the user can fold (services/sidebar.py).

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

Pinned here:

- every link the flat sidebar had is still in exactly one group, so the
  regrouping removed nothing;
- folding a group is stored on the user and the next page is rendered folded
  by the server (no localStorage, same on every device); an unknown group is
  refused;
- "Plan & review" and "More" start folded, the rest open;
- the current page's link is marked active, and a folded group that holds it
  is shown open on that page without changing what is stored.
"""
import unittest

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover (no mongoengine installed)
    connect = None


@unittest.skipIf(connect is None, "mongoengine not installed")
class SidebarGroupTests(unittest.TestCase):

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
            User.objects.delete()
        disconnect(alias="default")

    def setUp(self):
        from application.models.user import User
        with self.app.app_context():
            User.objects.delete()
            self.me = User(username="sb-me", email="sb-me@test.invalid",
                           password_hash="x", level="premium").save()
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.me.pk)
            sess["_fresh"] = True

    def _html(self, url="/"):
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def _group(self, html, key):
        part = html.split(f'data-nav-group="{key}"', 1)[1]
        return part.split("data-nav-group=", 1)[0].split("nav-sidebar-footer", 1)[0]

    def _build(self, path="/"):
        from application.models.user import User
        from application.services import sidebar
        from application.services.perspectives import ALL_PERSPECTIVES
        with self.app.test_request_context(path):
            user = User.objects(pk=self.me.pk).first()
            return sidebar.build(user, perspectives=list(ALL_PERSPECTIVES),
                                 custom_perspectives=[], favorites=[],
                                 counts={}, path=path)

    def test_nothing_was_dropped(self):
        groups = self._build()
        labels = [e.label for g in groups for e in g.entries]
        self.assertEqual(len(labels), len(set(labels)) + 1)  # two "Review"s
        for expected in ("Home", "Inbox", "Today", "Next", "Scheduled", "Flagged",
                         "Forecast (14d)", "Waiting", "Waiting on reply",
                         "Follow-up", "Assigned to me", "Assigned to others",
                         "Someday", "Completed", "Projects", "Notes",
                         "Scratchpad", "Tree", "Tags / Contexts", "Perspectives",
                         "12 Week Year", "Templates", "Dependency graph",
                         "Trash", "Customise sidebar"):
            self.assertIn(expected, labels)

    def test_default_folding(self):
        html = self._html("/settings/")
        for key in ("plan", "more"):
            self.assertIn('aria-expanded="false"', self._group(html, key))
        for key in ("focus", "followup", "organize"):
            self.assertIn('aria-expanded="true"', self._group(html, key))

    def test_fold_is_stored_on_the_user_and_rendered(self):
        resp = self.client.post("/settings/sidebar-group",
                                data={"group": "organize", "collapsed": "1"})
        self.assertEqual(resp.status_code, 200)
        from application.models.user import User
        with self.app.app_context():
            self.assertTrue(User.objects(pk=self.me.pk).first().sidebar_groups["organize"])
        org = self._group(self._html("/settings/"), "organize")
        self.assertIn('aria-expanded="false"', org)
        self.assertIn(" hidden>", org)
        # Unfolding a default-folded group sticks too.
        self.client.post("/settings/sidebar-group", data={"group": "more", "collapsed": "0"})
        self.assertIn('aria-expanded="true"', self._group(self._html("/settings/"), "more"))

    def test_unknown_group_is_refused(self):
        resp = self.client.post("/settings/sidebar-group",
                                data={"group": "nope", "collapsed": "1"})
        self.assertEqual(resp.status_code, 400)

    def test_active_page_opens_its_folded_group(self):
        html = self._html("/trash/")
        more = self._group(html, "more")
        self.assertIn('aria-expanded="true"', more)
        self.assertIn('aria-current="page"', more)
        from application.models.user import User
        with self.app.app_context():
            self.assertFalse(User.objects(pk=self.me.pk).first().sidebar_groups)

    def test_longest_url_wins(self):
        from application.services.sidebar import Entry, SidebarGroup, _mark_active
        g = SidebarGroup("work", "Work", collapsed=True, entries=[
            Entry("Customers", "i", "/time/"), Entry("Time tracking", "i", "/time/day"),
            Entry("Home", "i", "/")])
        _mark_active([g], "/time/day")
        self.assertEqual([e.label for e in g.entries if e.active], ["Time tracking"])
        self.assertFalse(g.collapsed)
        _mark_active([g], "/projects/")
        self.assertFalse(g.entries[2].active)  # "/" only matches the home page
        active = [e.label for grp in self._build("/") for e in grp.entries if e.active]
        self.assertEqual(active, ["Home"])


if __name__ == "__main__":
    unittest.main()
