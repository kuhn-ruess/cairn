"""Filing a note into a SHARED notebook — ``POST /notes/<id>/move``.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

Driven through the real Flask test client, because the bug this covers lived
exactly in the gap between the two ends: the navigator draws every notebook
the user may see as a drop target, while the endpoint only accepted the ones
they OWN — so a drop into a notebook shared by two people came back as
"not found", the one thing on screen that is certainly wrong.
"""
import unittest

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None


@unittest.skipIf(connect is None, "mongoengine not installed")
class NotesMoveTests(unittest.TestCase):

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
        cls.ctx = cls.app.app_context()
        cls.ctx.push()

    @classmethod
    def tearDownClass(cls):
        cls.ctx.pop()
        disconnect(alias="default")

    def setUp(self):
        from application.models.item import Item
        from application.models.team import Team
        from application.models.user import User
        self.Item, self.Team, self.User = Item, Team, User
        self._wipe()
        self.kuhn = self._user("kuhn")
        self.ruess = self._user("ruess")
        self.stranger = self._user("stranger")
        # The notebook the two of them share: Rueß owns it, Kuhn is a member.
        self.shared = self.Item(owner=self.ruess, kind="folder",
                                folder_domain="notes", title="Kuhn & Rueß",
                                status="active", members=[self.kuhn]).save()

    def tearDown(self):
        self._wipe()

    def _wipe(self):
        from application.models.item import Item
        from application.models.team import Team
        from application.models.user import User
        Item.all_objects.delete()
        Team.objects.delete()
        User.objects.delete()

    # -- helpers ---------------------------------------------------------

    def _user(self, name):
        return self.User(username=name, email=f"{name}@test.invalid",
                         password_hash="x", level="premium").save()

    def _client(self, user):
        """A test client logged in as ``user``.

        The app context is the class's, and a test request reuses the one
        already on the stack — so Flask-Login's per-context cache
        (``g._login_user``) would hand the NEXT request the user of the
        previous one, long after this test's users were wiped. Dropping it is
        what makes "log in as somebody else" mean it."""
        from flask import g
        g.pop("_login_user", None)
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user.pk)
            sess["_fresh"] = True
        return client

    def _note(self, owner, title="Notiz", parent=None):
        return self.Item(owner=owner, kind="note", title=title, note="",
                         parent=parent).save()

    def _move(self, user, item, parent):
        return self._client(user).post(
            f"/notes/{item.pk}/move",
            data={"parent_id": "" if parent is None else str(parent.pk)},
        )

    def _parent_of(self, item):
        fresh = self.Item.objects(pk=item.pk).first()
        return None if fresh.parent is None else fresh.parent.pk

    # -- the reported bug ------------------------------------------------

    def test_a_note_can_be_moved_into_a_notebook_shared_with_me(self):
        note = self._note(self.kuhn)
        resp = self._move(self.kuhn, note, self.shared)
        self.assertEqual(resp.status_code, 200, resp.get_data(as_text=True))
        self.assertEqual(self._parent_of(note), self.shared.pk)

    def test_the_owner_of_the_notebook_can_file_into_it_too(self):
        note = self._note(self.ruess)
        self.assertEqual(self._move(self.ruess, note, self.shared).status_code, 200)
        self.assertEqual(self._parent_of(note), self.shared.pk)

    def test_a_note_inside_the_shared_notebook_can_be_moved_by_both(self):
        """It is not only the destination: a note that came with the share is
        not owned by the person tidying up, and that was the other half of the
        404."""
        note = self._note(self.ruess, parent=self.shared)
        self.assertEqual(self._move(self.kuhn, note, None).status_code, 200)
        self.assertIsNone(self._parent_of(note))

    # -- the boundary is still a boundary --------------------------------

    def test_a_stranger_gets_nothing(self):
        note = self._note(self.kuhn)
        self.assertEqual(self._move(self.stranger, note, self.shared).status_code, 404)
        self.assertEqual(self._move(self.stranger, note, None).status_code, 404)
        self.assertIsNone(self._parent_of(note))

    def test_a_notebook_nobody_shared_is_not_a_destination(self):
        private = self.Item(owner=self.ruess, kind="folder",
                            folder_domain="notes", title="Privat",
                            status="active").save()
        note = self._note(self.kuhn)
        self.assertEqual(self._move(self.kuhn, note, private).status_code, 400)
        self.assertIsNone(self._parent_of(note))

    def test_a_project_folder_is_not_a_notes_destination(self):
        projects = self.Item(owner=self.kuhn, kind="folder",
                             folder_domain="tasks", title="Projekte",
                             status="active").save()
        note = self._note(self.kuhn)
        self.assertEqual(self._move(self.kuhn, note, projects).status_code, 400)
        self.assertIsNone(self._parent_of(note))

    def test_a_folder_cannot_be_moved_into_its_own_subtree(self):
        sub = self.Item(owner=self.kuhn, kind="folder", folder_domain="notes",
                        title="Unter", status="active",
                        parent=self.shared).save()
        self.assertEqual(self._move(self.kuhn, self.shared, sub).status_code, 400)


if __name__ == "__main__":
    unittest.main()
