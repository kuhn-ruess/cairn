"""The trash can be looked into, and everything in it says why it is there.

Run with ``python -m unittest discover tests``. The database tests need a
MongoDB (``TestConfig`` points at ``bktodo_test``) and are skipped without one.

Two things are pinned here:

* **Every way into the trash records a reason.** ``trash_item`` takes
  ``source`` and ``via`` as required keywords, and a static scan holds every
  caller in ``application/`` to it and finds any code writing ``trashed_at``
  around it. A new path that trashes without saying why fails here.
* **A trashed item opens read only** (``/trash/<id>``) for its owner or
  assignee, offers Restore and nothing that edits or deletes, and
  the list shows where, when and why, without a query per row.
"""
import ast
import inspect
import pathlib
import re
import traceback
import unittest

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None

ROOT = pathlib.Path(__file__).resolve().parent.parent / "application"


class TrashReasonIsRequiredTests(unittest.TestCase):

    def test_source_and_via_are_required_keywords(self):
        from application.services.trash import trash_item
        params = inspect.signature(trash_item).parameters
        for name in ("source", "via"):
            self.assertIs(params[name].kind, inspect.Parameter.KEYWORD_ONLY)
            self.assertIs(params[name].default, inspect.Parameter.empty, name)
        with self.assertRaises(TypeError):
            trash_item(object())  # no reason given: refused before any work

    def test_every_caller_says_why(self):
        """Each ``trash_item(...)`` call in the app passes ``source`` and
        ``via`` (by name, or through ``web_trash_args`` / ``api_trash_args``
        unpacked as ``**``)."""
        bad = []
        for path in ROOT.rglob("*.py"):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                name = getattr(fn, "id", None) or getattr(fn, "attr", None)
                if name != "trash_item":
                    continue
                kws = {k.arg for k in node.keywords if k.arg}
                splat = any(k.arg is None for k in node.keywords)
                if not ({"source", "via"} <= kws or splat):
                    bad.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(bad, [], "trash_item called without a reason")

    def test_nothing_writes_trashed_at_around_the_helper(self):
        """``trashed_at`` is set in ``services/trash.py`` and nowhere else, so
        there is no second way into the trash that skips the reason."""
        pat = re.compile(r"\.trashed_at\s*=(?!=)\s*(?!None)|set__trashed_at")
        bad = []
        for path in ROOT.rglob("*.py"):
            if path.relative_to(ROOT).as_posix() == "services/trash.py":
                continue
            for n, line in enumerate(path.read_text().splitlines(), 1):
                if pat.search(line):
                    bad.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
        self.assertEqual(bad, [])


@unittest.skipIf(connect is None, "mongoengine not installed")
class TrashViewTests(unittest.TestCase):

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
        cls.ctx = cls.app.app_context()
        cls.ctx.push()

    @classmethod
    def tearDownClass(cls):
        cls._wipe()
        cls.ctx.pop()
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
        self.Item = Item
        self._wipe()
        mk = lambda n: User(username=n, email=f"{n}@test.invalid",  # noqa: E731
                            password_hash="x", level="premium").save()
        self.owner, self.mate, self.stranger = mk("owner"), mk("mate"), mk("stranger")
        self.project = Item(owner=self.owner, kind="project", title="Kundenprojekt",
                            status="active").save()
        self.task = Item(
            owner=self.owner, kind="task", parent=self.project,
            title="Angebot schreiben", note="Some **context** for the offer",
            tags=["urgent"], assigned_to=self.mate, status="open",
            comments=[Comment(author=self.owner, body="first remark")],
            note_files=[NoteFile(token="tok1", filename="offer.pdf",
                                 content_type="application/pdf", data=b"%PDF")],
        ).save()
        self.sub = Item(owner=self.owner, kind="task", parent=self.task,
                        title="Preise klären", status="open").save()
        self.note = Item(owner=self.owner, kind="note", title="Meeting notes",
                         note="- agreed on the plan\n- next call friday").save()
        self.client = self.app.test_client()
        self.login(self.owner)

    def login(self, user):
        # The class keeps one app context pushed, so ``g`` outlives a request
        # and Flask-Login would answer with the user it cached last time.
        from flask import g
        g.pop("_login_user", None)
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(user.pk)
            sess["_fresh"] = True

    def fresh(self, item):
        return self.Item.all_objects(pk=item.pk).first()

    def trash_task_from_web(self, reason="duplicate"):
        resp = self.client.post(f"/tasks/{self.task.pk}/delete",
                                data={"trash_via": "shortcut", "trash_reason": reason},
                                headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(resp.status_code, 200)

    # -- recording the reason ---------------------------------------------

    def test_web_delete_records_who_how_and_why(self):
        self.trash_task_from_web()
        t = self.fresh(self.task)
        self.assertEqual(t.trashed_source, "web")
        self.assertEqual(t.trashed_via, "Web: shortcut d")
        self.assertEqual(t.trashed_reason, "duplicate")
        self.assertEqual(t.trashed_by, self.owner)
        s = self.fresh(self.sub)
        self.assertEqual(s.trashed_source, "parent")
        self.assertIn("Angebot schreiben", s.trashed_via)
        self.assertEqual(s.trashed_at, t.trashed_at)

    def test_an_unknown_gesture_falls_back_to_the_route(self):
        self.client.post(f"/tasks/{self.task.pk}/delete",
                         data={"trash_via": "<script>"},
                         headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(self.fresh(self.task).trashed_via, "Web: trash button")

    def test_api_delete_names_the_token(self):
        _, token = self.owner.issue_api_token("cli")
        resp = self.client.delete(f"/api/v1/tasks/{self.task.pk}",
                                  headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status_code, 204)
        t = self.fresh(self.task)
        self.assertEqual((t.trashed_source, t.trashed_via), ("api", "API (token cli)"))

    def test_caldav_delete_goes_through_the_helper(self):
        from application.services.trash import trash_item
        trash_item(self.note, source="caldav", via="CalDAV (Apple Reminders)",
                   by=self.owner)
        self.assertEqual(self.fresh(self.note).trashed_source, "caldav")

    def test_unknown_source_is_refused(self):
        from application.services.trash import trash_item
        with self.assertRaises(ValueError):
            trash_item(self.note, source="whatever", via="x")
        with self.assertRaises(ValueError):
            trash_item(self.note, source="web", via="  ")
        self.assertIsNone(self.fresh(self.note).trashed_at)

    # -- the list ----------------------------------------------------------

    def test_list_shows_reason_place_type_and_source_filter(self):
        from datetime import datetime, timezone
        self.trash_task_from_web()
        # One from before reasons were recorded.
        self.Item.all_objects(pk=self.note.pk).update(
            set__trashed_at=datetime(2026, 1, 2, 10, 0, tzinfo=timezone.utc))
        html = self.client.get("/trash/").get_data(as_text=True)
        self.assertIn("Web: shortcut d: duplicate", html)
        self.assertIn("in Kundenprojekt", html)
        self.assertIn("Reason unknown", html)
        self.assertIn("Some context for the offer", html)
        self.assertIn("2026-01-02", html)
        self.assertIn(f'href="/trash/{self.task.pk}"', html)
        filtered = self.client.get("/trash/?source=web").get_data(as_text=True)
        self.assertIn("Angebot schreiben", filtered)
        self.assertNotIn("Meeting notes</a>", filtered)

    def test_list_costs_the_same_for_more_rows(self):
        from pymongo.collection import Collection
        from application.services.trash import trash_item

        def count():
            calls, original = [], Collection.find

            def counting(self_, *a, **kw):
                # Only what this page asks for: the sidebar and the rest of
                # the layout (``filters.inject_helpers``) are not its doing.
                files = [f.filename for f in traceback.extract_stack()]
                if (any(f.endswith("/trash.py") for f in files)
                        and not any(f.endswith("views/filters.py") for f in files)):
                    calls.append(self_.name)
                return original(self_, *a, **kw)
            Collection.find = counting
            try:
                self.assertEqual(self.client.get("/trash/").status_code, 200)
            finally:
                Collection.find = original
            return len(calls)

        why = {"source": "web", "via": "Web: bulk action", "by": self.owner}
        trash_item(self.task, **why)
        few = count()
        for i in range(15):
            t = self.Item(owner=self.owner, kind="task", parent=self.project,
                          title=f"bulk {i}", status="open").save()
            trash_item(t, **why)
        self.assertEqual(count(), few, "the trash list queries per row")

    # -- the read only view -----------------------------------------------

    def test_trashed_task_opens_read_only(self):
        self.trash_task_from_web()
        resp = self.client.get(f"/trash/{self.task.pk}")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        for text in ("In the trash", "Angebot schreiben", "<strong>context</strong>",
                     "+urgent", "Kundenprojekt", "first remark", "offer.pdf",
                     "Preise klären", "Web: shortcut d: duplicate", "mate"):
            self.assertIn(text, html)
        # The only thing that posts is Restore.
        actions = set(re.findall(r'<form[^>]*action="([^"]+)"', html))
        page_forms = {a for a in actions if f"/trash/{self.task.pk}" in a}
        self.assertEqual(page_forms, {f"/trash/{self.task.pk}/restore"})
        self.assertFalse(any(f"/tasks/{self.task.pk}" in a for a in actions))
        body = html.split('class="trash-view"', 1)[1].split("<!-- /trash-view -->")[0]
        self.assertNotIn("<textarea", body)
        self.assertNotRegex(body, r'<input(?![^>]*type="hidden")')
        # The attachment is served from the trash, not the (404ing) task route.
        f = self.client.get(f"/trash/{self.task.pk}/note-file/tok1")
        self.assertEqual(f.status_code, 200)

    def test_trashed_note_opens_read_only(self):
        from application.services.trash import trash_item
        trash_item(self.note, source="notes_mirror",
                   via="Notes mirror: file deleted on the share",
                   reason="Meeting notes.md")
        html = self.client.get(f"/trash/{self.note.pk}").get_data(as_text=True)
        self.assertIn("agreed on the plan", html)
        self.assertIn("Notes mirror", html)

    def test_who_may_look(self):
        self.trash_task_from_web()
        self.login(self.stranger)
        self.assertEqual(self.client.get(f"/trash/{self.task.pk}").status_code, 404)
        self.assertEqual(
            self.client.get(f"/trash/{self.task.pk}/note-file/tok1").status_code, 404)
        self.assertEqual(
            self.client.post(f"/trash/{self.task.pk}/restore").status_code, 404)
        # The assignee may look, but restoring is the owner's.
        self.login(self.mate)
        html = self.client.get(f"/trash/{self.task.pk}").get_data(as_text=True)
        self.assertIn("Angebot schreiben", html)
        self.assertNotIn(f"/trash/{self.task.pk}/restore", html)
        self.assertEqual(
            self.client.post(f"/trash/{self.task.pk}/restore").status_code, 404)

    def test_a_live_item_is_not_in_the_trash_view(self):
        self.assertEqual(self.client.get(f"/trash/{self.task.pk}").status_code, 404)

    def test_trashed_items_stay_out_of_listings(self):
        self.trash_task_from_web()
        _, token = self.owner.issue_api_token("cli")
        data = self.client.get("/api/v1/tasks",
                               headers={"Authorization": f"Bearer {token}"}).get_json()
        rows = data["items"]
        self.assertNotIn(str(self.task.pk), [r.get("id") for r in rows])
        self.assertIsNone(self.Item.objects(pk=self.task.pk).first())

    def test_restore_from_the_view(self):
        self.trash_task_from_web()
        resp = self.client.post(f"/trash/{self.task.pk}/restore")
        self.assertEqual(resp.status_code, 302)
        t = self.fresh(self.task)
        self.assertIsNone(t.trashed_at)
        # Why it was trashed is kept as history.
        self.assertEqual(t.trashed_reason, "duplicate")
        self.assertIsNone(self.fresh(self.sub).trashed_at)

    def test_restore_selected(self):
        from application.services.trash import trash_item
        why = {"source": "web", "via": "Web: bulk action", "by": self.owner}
        trash_item(self.task, **why)
        trash_item(self.note, **why)
        self.client.post("/trash/restore-selected",
                         data={"item_ids": [str(self.task.pk), str(self.note.pk)]})
        self.assertIsNone(self.fresh(self.task).trashed_at)
        self.assertIsNone(self.fresh(self.note).trashed_at)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
