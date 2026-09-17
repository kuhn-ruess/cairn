"""REST API v1 — the /notes endpoints.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

Driven through the real Flask test client with real tokens, because what is
worth testing here is the boundary: a note is visible to its owner and to
whoever it was shared with, sharing is the OWNER's to set, and the set of
people it can be shared with is the team — not "any account whose id I can
guess". Getting that wrong hands someone else's notes out.
"""
import unittest

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None


@unittest.skipIf(connect is None, "mongoengine not installed")
class NotesApiTests(unittest.TestCase):

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
        self.client = self.app.test_client()

        self.owner = self._user("owner")
        self.mate = self._user("mate", level="premium")
        self.stranger = self._user("stranger", level="premium")
        self.team = Team(name="Kuhn & Rueß", admin=self.owner,
                         admins=[self.owner]).save()
        self.owner.team = self.team
        self.owner.save()
        self.mate.team = self.team
        self.mate.save()

        _, self.token = self.owner.issue_api_token("test")
        _, self.mate_token = self.mate.issue_api_token("test")
        _, self.ro_token = self.owner.issue_api_token("ro", read_only=True)

    def tearDown(self):
        self._wipe()

    def _wipe(self):
        from application.models.item import Item
        Item.all_objects.delete()
        self.Team.objects.delete()
        self.User.objects.delete()

    # -- helpers ---------------------------------------------------------

    def _user(self, name, level="team_admin"):
        return self.User(username=name, email=f"{name}@test.invalid",
                         password_hash="x", level=level).save()

    def _auth(self, token=None):
        return {"Authorization": f"Bearer {token or self.token}"}

    def _get(self, path, token=None):
        return self.client.get(path, headers=self._auth(token))

    def _post(self, path, json=None, token=None):
        return self.client.post(path, json=json or {}, headers=self._auth(token))

    def _patch(self, path, json=None, token=None):
        return self.client.patch(path, json=json or {}, headers=self._auth(token))

    def _put(self, path, json=None, token=None):
        return self.client.put(path, json=json or {}, headers=self._auth(token))

    def _folder(self, title="Kunden", token=None):
        return self._post("/api/v1/notes",
                          {"kind": "folder", "title": title}, token).json

    # -- notes -----------------------------------------------------------

    def test_a_token_is_required(self):
        self.assertEqual(self.client.get("/api/v1/notes").status_code, 401)

    def test_a_note_takes_its_name_from_its_first_line(self):
        r = self._post("/api/v1/notes", {"note": "- Termin mit Meier\n"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json["title"], "Termin mit Meier")
        self.assertTrue(r.json["title_auto"])
        # …until it is named, and then the name is the user's.
        named = self._patch(f"/api/v1/notes/{r.json['id']}", {"title": "Meier"})
        self.assertFalse(named.json["title_auto"])
        again = self._patch(f"/api/v1/notes/{r.json['id']}",
                            {"note": "- Etwas ganz anderes\n"})
        self.assertEqual(again.json["title"], "Meier")

    def test_a_body_written_through_the_api_is_indexed(self):
        """The links are what the note is worth in the tree — a body written
        without indexing them is a note nothing points at."""
        target = self._post("/api/v1/notes", {"note": "- Kunde Meier\n"}).json
        self._post("/api/v1/notes", {"note": "- siehe [[Kunde Meier]]\n"})
        item = self.Item.objects(pk=target["id"]).first()
        from application.services import note_links
        refs = note_links.linked_references(item, self.owner)
        self.assertTrue(refs, "the [[link]] did not reach the target's backlinks")

    def test_the_notes_list_leaves_the_project_tree_alone(self):
        self._folder()
        self.Item(owner=self.owner, kind="folder", folder_domain="tasks",
                  title="Projekte").save()
        titles = [n["title"] for n in self._get("/api/v1/notes").json["items"]]
        self.assertIn("Kunden", titles)
        self.assertNotIn("Projekte", titles)

    def test_delete_moves_a_note_to_the_trash(self):
        note = self._post("/api/v1/notes", {"note": "- weg damit\n"}).json
        self.assertEqual(self.client.delete(f"/api/v1/notes/{note['id']}",
                                            headers=self._auth()).status_code, 204)
        self.assertEqual(self._get(f"/api/v1/notes/{note['id']}").status_code, 404)
        # Soft: still there for the trash view to restore.
        self.assertIsNotNone(self.Item.all_objects(pk=note["id"]).first().trashed_at)

    # -- sharing ---------------------------------------------------------

    def test_a_folder_share_reaches_what_is_filed_under_it(self):
        folder = self._folder()
        note = self._post("/api/v1/notes",
                          {"parent_id": folder["id"], "note": "- drin\n"}).json
        self.assertEqual(self._get(f"/api/v1/notes/{note['id']}",
                                   self.mate_token).status_code, 404)
        r = self._put(f"/api/v1/notes/{folder['id']}/share",
                      {"member_ids": [str(self.mate.pk)]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual([m["username"] for m in r.json["members"]], ["mate"])
        self.assertEqual(self._get(f"/api/v1/notes/{note['id']}",
                                   self.mate_token).status_code, 200)
        titles = [n["title"] for n in
                  self._get("/api/v1/notes", self.mate_token).json["items"]]
        self.assertIn("Kunden", titles)

    def test_a_member_can_be_named_by_username_or_email(self):
        folder = self._folder()
        for name in ("mate", "mate@test.invalid"):
            r = self._put(f"/api/v1/notes/{folder['id']}/share",
                          {"member_ids": [name]})
            self.assertEqual(r.status_code, 200, name)
            self.assertEqual([m["username"] for m in r.json["members"]], ["mate"])

    def test_sharing_stops_at_the_team(self):
        """The id is caller-supplied, so this is the whole boundary."""
        folder = self._folder()
        r = self._put(f"/api/v1/notes/{folder['id']}/share",
                      {"member_ids": [str(self.stranger.pk)]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.Item.objects(pk=folder["id"]).first().members, [])

    def test_a_team_share_must_be_a_team_the_caller_is_in(self):
        folder = self._folder()
        theirs = self.Team(name="Somebody else", admin=self.stranger,
                           admins=[self.stranger]).save()
        self.assertEqual(
            self._put(f"/api/v1/notes/{folder['id']}/share",
                      {"team_ids": [str(theirs.pk)]}).status_code, 400)
        ok = self._put(f"/api/v1/notes/{folder['id']}/share",
                       {"team_ids": [str(self.team.pk)]})
        self.assertEqual([t["name"] for t in ok.json["teams"]], ["Kuhn & Rueß"])

    def test_an_empty_body_unshares(self):
        folder = self._folder()
        self._put(f"/api/v1/notes/{folder['id']}/share",
                  {"member_ids": ["mate"], "team_ids": [str(self.team.pk)]})
        r = self._put(f"/api/v1/notes/{folder['id']}/share", {})
        self.assertEqual((r.json["members"], r.json["teams"]), ([], []))

    def test_only_the_owner_sees_or_sets_a_share(self):
        folder = self._folder()
        self._put(f"/api/v1/notes/{folder['id']}/share", {"member_ids": ["mate"]})
        path = f"/api/v1/notes/{folder['id']}/share"
        self.assertEqual(self._get(path, self.mate_token).status_code, 404)
        self.assertEqual(self._put(path, {"member_ids": []},
                                   self.mate_token).status_code, 404)
        self.assertEqual(
            [m.username for m in self.Item.objects(pk=folder["id"]).first().members],
            ["mate"])

    def test_a_read_only_token_may_not_share(self):
        folder = self._folder()
        self.assertEqual(self._put(f"/api/v1/notes/{folder['id']}/share",
                                   {"member_ids": []},
                                   self.ro_token).status_code, 403)

    # -- importing -------------------------------------------------------
    #
    # What an importer needs beyond the editor's own create: file it by name,
    # keep its real age, and be safe to run twice. See create_note's docstring.

    def test_folder_path_files_a_note_by_name(self):
        r = self._post("/api/v1/notes",
                       {"folder_path": "Archiv/Agenda/2026",
                        "title": "Kickoff", "note": "- Kickoff\n"})
        self.assertEqual(r.status_code, 201)
        note = self.Item.objects(pk=r.json["id"]).first()
        chain = []
        node = note.parent
        while node:
            chain.append(node.title)
            node = node.parent
        self.assertEqual(chain, ["2026", "Agenda", "Archiv"])
        self.assertTrue(all(f.folder_domain == "notes" for f in
                            self.Item.objects(kind="folder")))

    def test_folder_path_reuses_the_tree_it_finds(self):
        self._post("/api/v1/notes", {"folder_path": "Archiv/Agenda", "note": "- a\n"})
        self._post("/api/v1/notes", {"folder_path": "Archiv/Agenda", "note": "- b\n"})
        self.assertEqual(self.Item.objects(kind="folder", title="Agenda").count(), 1)
        self.assertEqual(self.Item.objects(kind="folder", title="Archiv").count(), 1)

    def test_folder_path_and_parent_id_together_are_refused(self):
        folder = self._folder()
        r = self._post("/api/v1/notes",
                       {"parent_id": folder["id"], "folder_path": "Archiv"})
        self.assertEqual(r.status_code, 400)

    def test_an_import_brings_its_own_dates(self):
        r = self._post("/api/v1/notes",
                       {"note": "- alt\n", "created": "2026-04-07T07:39:12Z",
                        "updated": "2026-04-07T09:00:00Z"})
        self.assertEqual(r.status_code, 201)
        note = self.Item.objects(pk=r.json["id"]).first()
        self.assertEqual(note.created_at.year, 2026)
        self.assertEqual(note.created_at.month, 4)
        self.assertEqual(note.created_at.hour, 7)      # Z is UTC, not local
        self.assertEqual(note.updated_at.hour, 9)

    def test_a_bad_timestamp_is_a_bad_request(self):
        r = self._post("/api/v1/notes", {"note": "- x\n", "created": "gestern"})
        self.assertEqual(r.status_code, 400)

    def test_external_id_makes_the_import_repeatable(self):
        first = self._post("/api/v1/notes",
                           {"external_id": "agenda:ABC", "source": "agenda",
                            "title": "Walk", "note": "- eins\n",
                            "folder_path": "Archiv/Agenda"})
        self.assertEqual(first.status_code, 201)
        again = self._post("/api/v1/notes",
                           {"external_id": "agenda:ABC", "source": "agenda",
                            "title": "Walk", "note": "- eins\n- zwei\n",
                            "folder_path": "Archiv/Agenda"})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json["id"], first.json["id"])
        self.assertEqual(self.Item.objects(kind="note").count(), 1)
        self.assertIn("zwei", self.Item.objects(pk=first.json["id"]).first().note)

    def test_an_imported_note_says_where_it_came_from(self):
        """The way back to the original — and the only copy of its id once the
        source app has deleted its own."""
        r = self._post("/api/v1/notes",
                       {"external_id": "agenda:XYZ", "source": "agenda",
                        "note": "- her damit\n"})
        self.assertEqual(r.json["external_id"], "agenda:XYZ")
        self.assertEqual(r.json["source"], "agenda")
        listed = self._get("/api/v1/notes?compact=true").json["items"][0]
        self.assertEqual(listed["external_id"], "agenda:XYZ")

    def test_an_import_may_choose_the_editor(self):
        r = self._post("/api/v1/notes",
                       {"note": "Prosa, keine Bullets.\n", "note_format": "markdown"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.Item.objects(pk=r.json["id"]).first().note_format,
                         "markdown")
        self.assertEqual(self._post("/api/v1/notes",
                                    {"note": "x", "note_format": "gedicht"}).status_code,
                         400)

    # -- docs ------------------------------------------------------------

    def test_every_notes_route_is_in_the_published_spec(self):
        """The spec is served publicly; a route missing from it is a route
        nobody can discover."""
        spec = self.client.get("/api/v1/openapi.json").json
        documented = {p.replace("{note_id}", "<note_id>")
                      for p in spec["paths"] if p.startswith("/notes")}
        registered = {r.rule.replace("/api/v1", "")
                      for r in self.app.url_map.iter_rules()
                      if r.rule.startswith("/api/v1/notes")}
        self.assertEqual(documented, registered)


if __name__ == "__main__":
    unittest.main()
