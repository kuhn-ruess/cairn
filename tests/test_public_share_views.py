"""The public share page: three views, search and filter — and a hard fence.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

Why this exists: ``/p/<token>`` is the only page in Cairn that answers an
anonymous request with somebody's project, and it now takes arguments. Every
argument is a chance to widen the query, so the fence is what is pinned hardest
here: a second project, an internal sub-project, a task from elsewhere and a
regex in the search box must all stay out, in *every* view.
"""
import unittest

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None


@unittest.skipIf(connect is None, "mongoengine not installed")
class PublicShareViewTests(unittest.TestCase):

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
            from application.models.item import Item
            from application.models.user import User
            Item.all_objects.delete()
            User.objects.delete()
        disconnect(alias="default")

    def setUp(self):
        from datetime import datetime, timedelta, timezone
        from application.models.item import Item
        from application.models.user import User
        self.Item = Item
        now = datetime.now(timezone.utc)
        with self.app.app_context():
            Item.all_objects.delete()
            User.objects.delete()
            self.user = User(username="share-owner", email="owner@test.invalid",
                             password_hash="x", level="premium").save()
            self.project = Item(owner=self.user, kind="project",
                                title="Kundenprojekt", status="active").save()
            self.project.regenerate_share_token()
            self.project.share_enabled = True
            self.project.save()
            self.token = self.project.share_token

            self.section = Item(owner=self.user, kind="section",
                                parent=self.project, title="Phase One").save()
            # An internal sub-project and its task: neither may ever appear.
            self.secret_section = Item(owner=self.user, kind="section",
                                       parent=self.project, is_internal=True,
                                       title="Internal margins").save()
            Item(owner=self.user, kind="task", parent=self.secret_section,
                 title="secret-internal-task").save()
            # A separate project of the same owner — the other side of the fence.
            self.other = Item(owner=self.user, kind="project",
                              title="Anderes Projekt", status="active").save()
            Item(owner=self.user, kind="task", parent=self.other,
                 title="other-project-task").save()

            self.visible = [
                Item(owner=self.user, kind="task", parent=self.project,
                     title="write the offer", status="next",
                     start_date=now, end_date=now + timedelta(days=5)).save(),
                Item(owner=self.user, kind="task", parent=self.section,
                     title="build the prototype", status="open").save(),
                Item(owner=self.user, kind="task", parent=self.section,
                     title="ship it", status="done").save(),
                Item(owner=self.user, kind="task", parent=self.project,
                     title="hidden away", status="open",
                     is_internal=True).save(),
            ]
        self.anon = self.app.test_client()

    def get(self, **args):
        from urllib.parse import urlencode
        url = f"/p/{self.token}"
        if args:
            url += "?" + urlencode(args)
        resp = self.anon.get(url)
        self.assertEqual(resp.status_code, 200, url)
        return resp.get_data(as_text=True)

    # -- the three views --------------------------------------------------

    def test_every_view_renders_the_project_tasks(self):
        for view in ("list", "board", "timeline"):
            html = self.get(view=view)
            self.assertIn("write the offer", html, view)
            self.assertIn("build the prototype", html, view)

    def test_the_page_offers_all_three_views(self):
        html = self.get()
        for view in ("board", "timeline"):
            self.assertIn(f"view={view}", html,
                          f"no way to reach the {view} view")

    def test_an_unknown_view_falls_back_to_the_list(self):
        # A view name reaches render_template; an unchecked one is a file probe.
        html = self.get(view="../../etc/passwd")
        self.assertIn("write the offer", html)

    def test_the_timeline_uses_outward_facing_status_words(self):
        html = self.get(view="timeline")
        self.assertIn("In progress", html)
        self.assertNotIn(">next<", html.lower())

    def test_the_timeline_does_not_link_into_the_logged_in_app(self):
        html = self.get(view="timeline")
        for leak in ("/projects/", "/tasks/", "data-inspector-url"):
            self.assertNotIn(leak, html,
                             f"the public timeline exposes {leak}")

    # -- search and filter ------------------------------------------------

    def test_search_narrows_to_matching_tasks(self):
        html = self.get(q="prototype")
        self.assertIn("build the prototype", html)
        self.assertNotIn("write the offer", html)

    def test_search_also_reads_the_note(self):
        with self.app.app_context():
            t = self.Item.objects(title="write the offer").first()
            t.note = "needs the architektur diagram"
            t.save()
        html = self.get(q="architektur")
        self.assertIn("write the offer", html)

    def test_status_filter_keeps_only_that_status(self):
        html = self.get(status="done")
        self.assertIn("ship it", html)
        self.assertNotIn("build the prototype", html)

    def test_an_unknown_status_is_ignored_rather_than_queried(self):
        html = self.get(status="totally-made-up")
        self.assertIn("build the prototype", html)
        self.assertIn("ship it", html)

    def test_search_and_filter_survive_a_view_switch(self):
        html = self.get(q="prototype", status="open")
        self.assertIn("view=board", html)
        self.assertIn("q=prototype", html)
        self.assertIn("status=open", html)

    def test_search_and_filter_apply_in_every_view(self):
        for view in ("list", "board", "timeline"):
            html = self.get(view=view, q="prototype")
            self.assertIn("build the prototype", html, view)
            self.assertNotIn("write the offer", html, view)

    # -- the fence --------------------------------------------------------

    def _id_of(self, title):
        with self.app.app_context():
            return str(self.Item.all_objects(title=title).first().pk)

    def test_nothing_from_another_project_is_reachable(self):
        # By id, not by title: a search term is echoed back into the box, and
        # the visitor's own words are not a leak.
        stranger = self._id_of("other-project-task")
        for args in ({}, {"view": "board"}, {"view": "timeline"},
                     {"q": "other"}, {"q": "task"}, {"status": "open"},
                     {"view": "timeline", "q": "other-project-task"}):
            self.assertNotIn(stranger, self.get(**args), args)

    def test_an_internal_subtree_stays_invisible(self):
        secret = self._id_of("secret-internal-task")
        section = self._id_of("Internal margins")
        for args in ({}, {"view": "board"}, {"view": "timeline"},
                     {"q": "secret"}, {"q": "internal"},
                     {"q": "margins"}, {"status": "open"}):
            html = self.get(**args)
            self.assertNotIn(secret, html, args)
            self.assertNotIn(section, html, args)
            self.assertNotIn("Internal margins", html, args)

    def test_an_internal_task_stays_invisible(self):
        hidden = self._id_of("hidden away")
        for args in ({}, {"view": "board"}, {"view": "timeline"},
                     {"q": "hidden"}):
            self.assertNotIn(hidden, self.get(**args), args)

    def test_the_search_term_is_escaped_where_it_is_echoed(self):
        html = self.get(q='"><script>alert(1)</script>')
        self.assertNotIn("<script>alert(1)</script>", html)

    def test_the_search_box_is_not_a_regex(self):
        # A term that matches everything as a regex must match nothing as text.
        for needle in (".*", "^.", "w|b", "[a-z]+"):
            html = self.get(q=needle)
            self.assertNotIn("write the offer", html, needle)
            self.assertNotIn("build the prototype", html, needle)

    def test_a_project_id_in_the_query_string_changes_nothing(self):
        # The page must take its scope from the token alone.
        for key in ("project", "project_id", "parent", "id", "token"):
            html = self.get(**{key: str(self.other.pk)})
            self.assertNotIn("other-project-task", html, key)
            self.assertIn("write the offer", html, key)

    def test_a_task_of_another_project_is_not_openable_by_id(self):
        with self.app.app_context():
            stranger = self.Item.objects(title="other-project-task").first()
            internal = self.Item.objects(title="secret-internal-task").first()
        for pk in (stranger.pk, internal.pk):
            self.assertEqual(
                self.anon.get(f"/p/{self.token}/t/{pk}").status_code, 404)

    def test_a_disabled_share_answers_nothing_in_any_view(self):
        with self.app.app_context():
            p = self.Item.objects(pk=self.project.pk).first()
            p.share_enabled = False
            p.save()
        for view in ("list", "board", "timeline"):
            self.assertEqual(
                self.anon.get(f"/p/{self.token}?view={view}").status_code, 404)

    # -- cost -------------------------------------------------------------

    def test_a_render_costs_a_handful_of_queries_whatever_the_view(self):
        """One query per tree depth plus one for the tasks — never one per row.

        The page is anonymous, so a per-row dereference is a free amplifier for
        anyone with the link.
        """
        from application.models.item import Item
        with self.app.app_context():
            # Twenty more tasks: the query count must not move with them.
            for i in range(20):
                Item(owner=self.user, kind="task", parent=self.section,
                     title=f"bulk task {i}", status="open").save()

        from pymongo.collection import Collection
        original = Collection.find

        for view in ("list", "board", "timeline"):
            calls = []

            def counting_find(self, *a, _orig=original, _calls=calls, **kw):
                _calls.append(self.name)
                return _orig(self, *a, **kw)

            Collection.find = counting_find
            try:
                html = self.get(view=view)
            finally:
                Collection.find = original
            self.assertIn("bulk task 0", html, view)
            self.assertLessEqual(
                len(calls), 6,
                f"the {view} view issued {len(calls)} queries for 24 tasks "
                f"({calls}) — something is querying per row")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
