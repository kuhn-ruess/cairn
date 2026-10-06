"""services/capture: the project page's Markdown sketch.

Run with ``python -m unittest discover tests``. Needs a MongoDB for the test
database (``bktodo_test``); skipped when there isn't one.

A sketch is a nested list typed into a project: new lines become tasks,
indentation becomes sub-tasks, an existing task's line put elsewhere moves
it, and a line the sketch opened with that is gone closes its task. Nothing
on an existing item's fields is ever changed, except what an ``@person``
or ``+tag`` typed on its line adds.
"""
import unittest


class ProjectSketchTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            from mongoengine import disconnect
            from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
        except ImportError:  # pragma: no cover (no mongoengine installed)
            raise unittest.SkipTest("mongoengine not installed")
        from application import create_app
        disconnect(alias="default")
        try:
            cls.app = create_app("application.config.TestConfig")
            with cls.app.app_context():
                from application.models.user import User
                User.objects.first()          # forces a real round trip
        except (ServerSelectionTimeoutError, PyMongoError) as exc:
            raise unittest.SkipTest(f"no MongoDB for the test database: {exc}")
        cls.ctx = cls.app.app_context()
        cls.ctx.push()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "ctx"):
            cls.ctx.pop()

    def setUp(self):
        from application.models.item import Item
        from application.models.user import User
        self.Item = Item
        self._wipe()
        self.owner = User(username="sk-bastian", email="sk-bastian@example.com",
                          password_hash="x").save()
        self.project = Item(owner=self.owner, kind="project",
                            title="Relaunch", status="active").save()

    def tearDown(self):
        self._wipe()

    def _wipe(self):
        from application.models.item import Item
        from application.models.team import Team
        from application.models.user import User
        for t in Team.objects(name="sk-team"):
            t.delete()
        for u in User.objects(username__in=("sk-bastian", "sk-sven")):
            Item.all_objects(owner=u).delete()
            u.delete()

    def _teammate_and_tag(self):
        """A teammate ``sk-sven`` and the owner's tag ``Buchhaltung``: what
        ``@sk-sven`` and ``+Buchhaltung`` on a line resolve to."""
        from application.models.tag import Tag
        from application.models.team import Team
        from application.models.user import User
        self.owner.tags = [Tag(name="Buchhaltung", color="#6c757d"),
                           Tag(name="Alt", color="#6c757d")]
        self.owner.save()
        mate = User(username="sk-sven", email="sk-sven@example.com",
                    display_name="Sven", password_hash="x").save()
        mate.team = Team(name="sk-team", admin=self.owner).save()
        mate.save()
        return mate

    def _sketch(self, text, shown=None, apply=True):
        from application.services.capture import sketch_tasks
        stats = sketch_tasks(text, self.owner, self.project, shown, apply)
        self.last = stats
        return {k: stats[k] for k in
                ("tasks_created", "tasks_skipped", "sections_created")}

    def _client(self):
        """A test client logged in as the owner. The class's app context is
        reused by every request, so Flask-Login's ``g._login_user`` would
        otherwise hand this test the user of the previous one."""
        from flask import g
        g.pop("_login_user", None)
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(self.owner.pk)
            sess["_fresh"] = True
        return client

    def _open(self):
        """The sketch as the page opens it: its text and the shown ids."""
        from application.services.capture import sketch_outline
        shown = []
        return sketch_outline(self.project, shown), shown

    def _child(self, parent, title):
        return self.Item.objects(parent=parent, title=title).first()

    def test_nesting_becomes_sub_tasks(self):
        stats = self._sketch("- Konzept\n  - Anforderungen\n    - Interviews\n- Umsetzung")
        self.assertEqual(stats, {"tasks_created": 4, "tasks_skipped": 0, "sections_created": 0})
        konzept = self._child(self.project, "Konzept")
        self.assertEqual(konzept.kind, "task")      # stays a task, holds sub-tasks
        anf = self._child(konzept, "Anforderungen")
        self.assertIsNotNone(self._child(anf, "Interviews"))
        self.assertIsNotNone(self._child(self.project, "Umsetzung"))

    def test_existing_tasks_are_left_untouched(self):
        old = self.Item(owner=self.owner, kind="task", title="Konzept",
                        parent=self.project, priority=1, status="next").save()
        stats = self._sketch("- konzept p4 +neu\n  - Anforderungen")
        self.assertEqual(stats, {"tasks_created": 1, "tasks_skipped": 1, "sections_created": 0})
        old.reload()
        self.assertEqual((old.priority, old.status, list(old.tags)), (1, "next", []))
        self.assertEqual(self.Item.objects(parent=self.project).count(), 1)
        self.assertIsNotNone(self._child(old, "Anforderungen"))

    def test_same_sketch_twice_creates_nothing_new(self):
        text = "- A\n  - B\n- C"
        self._sketch(text)
        self.assertEqual(self._sketch(text), {"tasks_created": 0, "tasks_skipped": 3, "sections_created": 0})

    def test_sketch_extends_a_section(self):
        section = self.Item(owner=self.owner, kind="section", title="Phase 1",
                            parent=self.project).save()
        self._sketch("- Phase 1\n  - Kickoff")
        self.assertIsNotNone(self._child(section, "Kickoff"))

    def test_heading_makes_a_section(self):
        stats = self._sketch("- Lose\n\n## Phase 1\n- Kickoff\n  - Agenda\n"
                             "## Phase 2\n- Abnahme")
        self.assertEqual(stats, {"tasks_created": 4, "tasks_skipped": 0,
                                 "sections_created": 2})
        self.assertEqual(self._child(self.project, "Lose").kind, "task")
        p1 = self._child(self.project, "Phase 1")
        self.assertEqual(p1.kind, "section")
        self.assertIsNotNone(self._child(self._child(p1, "Kickoff"), "Agenda"))
        p2 = self._child(self.project, "Phase 2")
        self.assertEqual(p2.kind, "section")
        self.assertIsNotNone(self._child(p2, "Abnahme"))
        self.assertIsNone(self._child(self.project, "Abnahme"))

    def test_heading_uses_an_existing_section(self):
        section = self.Item(owner=self.owner, kind="section", title="Phase 1",
                            parent=self.project, status="active").save()
        stats = self._sketch("# phase 1\n- Kickoff")
        self.assertEqual(stats["sections_created"], 0)
        self.assertEqual(self.Item.objects(parent=self.project).count(), 1)
        self.assertIsNotNone(self._child(section, "Kickoff"))
        section.reload()
        self.assertEqual(section.title, "Phase 1")

    def test_hash_token_is_not_a_heading(self):
        self._sketch("- Anruf\n#Relaunch")
        self.assertEqual(self.Item.objects(parent=self.project,
                                           kind="section").count(), 0)

    def test_outline_shows_sections_as_headings(self):
        from application.services.capture import sketch_outline
        text = "- Lose\n\n## Phase 1\n- Kickoff\n  - Agenda"
        self._sketch(text)
        self.assertEqual(sketch_outline(self.project), text)
        # Saved back unchanged it creates nothing.
        self.assertEqual(self._sketch(sketch_outline(self.project)),
                         {"tasks_created": 0, "tasks_skipped": 3,
                          "sections_created": 0})

    def test_outline_round_trips(self):
        from application.services.capture import sketch_outline
        self._sketch("- A\n  - B\n- C")
        done = self._child(self.project, "C")
        done.status = "done"
        done.save()
        self.assertEqual(sketch_outline(self.project), "- A\n  - B")

    def test_moving_a_line_to_another_section_moves_the_task(self):
        from application.services.capture import sketch_outline
        self._sketch("## Phase 1\n- Kickoff\n  - Agenda\n## Phase 2\n- Abnahme")
        p1 = self._child(self.project, "Phase 1")
        p2 = self._child(self.project, "Phase 2")
        kickoff = self._child(p1, "Kickoff")
        _, shown = self._open()
        stats = self._sketch("## Phase 1\n## Phase 2\n- Abnahme\n- Kickoff\n"
                             "  - Agenda\n  - Raum buchen", shown)
        self.assertEqual(stats, {"tasks_created": 1, "tasks_skipped": 3,
                                 "sections_created": 0})
        self.assertEqual((self.last["moved"], self.last["closed"]),
                         (["Kickoff"], []))
        self.assertEqual(self.Item.objects(title="Kickoff").count(), 1)
        kickoff.reload()
        self.assertEqual(kickoff.parent.pk, p2.pk)
        # Its sub-task came along, and what is new under it lands beneath it.
        self.assertIsNotNone(self._child(kickoff, "Agenda"))
        self.assertIsNotNone(self._child(kickoff, "Raum buchen"))
        self.assertEqual(kickoff.status, "open")
        self.assertIn("## Phase 2\n- Abnahme\n- Kickoff", sketch_outline(self.project))

    def test_moving_a_line_to_another_parent_moves_the_task(self):
        self._sketch("- A\n  - B\n- C")
        b = self._child(self._child(self.project, "A"), "B")
        _, shown = self._open()
        stats = self._sketch("- A\n- C\n  - B", shown)
        self.assertEqual(stats["tasks_created"], 0)
        self.assertEqual((self.last["moved"], self.last["closed"]), (["B"], []))
        self.assertEqual(self.Item.objects(title="B").count(), 1)
        b.reload()
        self.assertEqual(b.parent.title, "C")
        self.assertEqual(b.status, "open")

    def test_moving_out_of_a_section_to_the_top(self):
        self._sketch("## Phase 1\n- Kickoff")
        kickoff = self.Item.objects(title="Kickoff").first()
        _, shown = self._open()
        self._sketch("- Kickoff\n## Phase 1", shown)
        kickoff.reload()
        self.assertEqual(kickoff.parent.pk, self.project.pk)

    def test_a_task_never_moves_under_its_own_sub_task(self):
        from application.services.capture import _sketch_move
        self._sketch("- A\n  - B")
        a = self._child(self.project, "A")
        b = self._child(a, "B")
        stats = {"moved": [], "refused": []}
        self.assertFalse(_sketch_move(a, b, 0, stats, {"mode": "apply"}))
        self.assertEqual(stats, {"moved": [], "refused": ["A"]})
        a.reload()
        self.assertEqual(a.parent.pk, self.project.pk)

    def test_removing_a_line_closes_the_task(self):
        self._sketch("- A\n  - B\n- C\n## Phase 1\n- D")
        _, shown = self._open()
        stats = self._sketch("- A\n## Phase 1", shown)
        self.assertEqual(stats["tasks_created"], 0)
        self.assertEqual(sorted(self.last["closed"]), ["B", "C", "D"])
        for title in ("B", "C", "D"):
            item = self.Item.objects(title=title).first()
            self.assertIsNotNone(item)            # closed, never deleted
            self.assertEqual(item.status, "done")
        self.assertEqual(self.Item.objects(title="A").first().status, "open")

    def test_removing_a_parent_closes_its_sub_tasks(self):
        self._sketch("- A\n  - B")
        _, shown = self._open()
        a = self._child(self.project, "A")
        # Added after the sketch was opened: not in the snapshot, but it
        # closes with its parent (the model's cascade), and the plan says so.
        late = self.Item(owner=self.owner, kind="task", title="Spaet",
                         parent=a, status="open").save()
        self._sketch("", shown, apply=False)
        self.assertEqual((sorted(self.last["closed"]), self.last["closed_with"]),
                         (["A", "B"], ["Spaet"]))
        self._sketch("", shown)
        late.reload()
        self.assertEqual(late.status, "done")

    def test_tasks_not_in_the_snapshot_are_untouched(self):
        self._sketch("- A\n## Phase 1\n- C")
        _, shown = self._open()
        done = self.Item(owner=self.owner, kind="task", title="Alt",
                         parent=self.project, status="done").save()
        late = self.Item(owner=self.owner, kind="task", title="Neu",
                         parent=self.project, status="open").save()
        # The page also sends ids that don't belong to the project.
        stranger = self.Item(owner=self.owner, kind="task", title="Fremd",
                             status="open").save()
        self._sketch("- A\n## Phase 1\n- C", shown + [str(stranger.pk),
                                                       str(done.pk)])
        self.assertEqual(self.last["closed"], [])
        for item, status in ((late, "open"), (stranger, "open"), (done, "done")):
            item.reload()
            self.assertEqual(item.status, status)

    def test_without_a_snapshot_a_removed_line_changes_nothing(self):
        self._sketch("- A\n  - B\n## Phase 1\n- C")
        stats = self._sketch("## Phase 1")
        self.assertEqual(stats["tasks_created"], 0)
        self.assertEqual(self.Item.objects(kind="task", title__in=["A", "B", "C"],
                                           status="open").count(), 3)

    def test_saving_unchanged_changes_nothing(self):
        self._sketch("- A\n  - B\n- C\n## Phase 1\n- D\n  - E")
        before = {(i.title, i.status, i.parent.pk, i.sort_order, i.updated_at)
                  for i in self.Item.objects(owner=self.owner)
                  if i.pk != self.project.pk}
        text, shown = self._open()
        stats = self._sketch(text, shown)
        self.assertEqual(stats, {"tasks_created": 0, "tasks_skipped": 5,
                                 "sections_created": 0})
        self.assertEqual([self.last[k] for k in
                          ("created", "moved", "closed", "closed_with")],
                         [[], [], [], []])
        after = {(i.title, i.status, i.parent.pk, i.sort_order, i.updated_at)
                 for i in self.Item.objects(owner=self.owner)
                 if i.pk != self.project.pk}
        self.assertEqual(before, after)

    def test_a_plan_writes_nothing(self):
        self._sketch("## Phase 1\n- Kickoff\n- Weg")
        _, shown = self._open()
        stats = self._sketch("## Phase 2\n- Kickoff\n  - Neu", shown, apply=False)
        self.assertEqual(stats, {"tasks_created": 1, "tasks_skipped": 1,
                                 "sections_created": 1})
        self.assertEqual((self.last["moved"], self.last["closed"]),
                         (["Kickoff"], ["Weg"]))
        self.assertIsNone(self._child(self.project, "Phase 2"))
        self.assertIsNone(self.Item.objects(title="Neu").first())
        self.assertEqual(self.Item.objects(title="Weg").first().status, "open")
        self.assertEqual(self.Item.objects(title="Kickoff").first().parent.title,
                         "Phase 1")

    def test_same_title_in_two_places_is_still_new(self):
        # The existing one is still listed at its place, so the second line
        # is a new task, not the existing one moved.
        self._sketch("## Phase 1\n- Testen")
        stats = self._sketch("- Testen\n## Phase 1\n- Testen")
        self.assertEqual(stats["tasks_created"], 1)
        self.assertEqual(self.Item.objects(title="Testen").count(), 2)

    def test_a_done_task_elsewhere_does_not_block_a_new_line(self):
        self._sketch("## Phase 1\n- Testen")
        done = self.Item.objects(title="Testen").first()
        done.status = "done"
        done.save()
        stats = self._sketch("## Phase 2\n- Testen")
        self.assertEqual(stats["tasks_created"], 1)

    def test_person_and_tag_on_a_new_line(self):
        mate = self._teammate_and_tag()
        self._sketch("- Angebot @sk-sven +Buchhaltung\n  - Preise @sk-sven")
        task = self._child(self.project, "Angebot")
        self.assertEqual(task.assigned_to.pk, mate.pk)
        self.assertEqual(task.tags, ["Buchhaltung"])
        self.assertEqual(self._child(task, "Preise").assigned_to.pk, mate.pk)

    def test_person_and_tag_on_an_existing_line(self):
        mate = self._teammate_and_tag()
        self._sketch("## Phase 1\n- Angebot +Alt\n- Rechnung")
        task = self._child(self._child(self.project, "Phase 1"), "Angebot")
        task.priority, task.flagged = 2, True
        task.save()
        text, shown = self._open()
        self.assertIn("- Angebot\n", text + "\n")      # tokens are not prefilled
        text = text.replace("- Angebot", "- Angebot @sk-sven +Buchhaltung")
        stats = self._sketch(text, shown, apply=False)    # a plan writes nothing
        self.assertEqual(self.last["updated"], ["Angebot"])
        task.reload()
        self.assertIsNone(task.assigned_to)
        stats = self._sketch(text, shown)
        self.assertEqual(stats, {"tasks_created": 0, "tasks_skipped": 2,
                                 "sections_created": 0})
        self.assertEqual(self.last["updated"], ["Angebot"])
        task.reload()
        self.assertEqual(task.assigned_to.pk, mate.pk)
        self.assertEqual(task.tags, ["Alt", "Buchhaltung"])   # added, not replaced
        self.assertEqual((task.priority, task.flagged, task.status), (2, True, "open"))
        self.assertIsNone(self._child(self.project, "Rechnung"))
        # Saved again: nothing left to do.
        self._sketch(text, shown)
        self.assertEqual(self.last["updated"], [])

    def test_a_moved_line_takes_its_tokens_along(self):
        mate = self._teammate_and_tag()
        self._sketch("## Phase 1\n- Angebot\n## Phase 2")
        text, shown = self._open()
        text = "## Phase 1\n## Phase 2\n- Angebot @sk-sven"
        self._sketch(text, shown)
        task = self.Item.objects(title="Angebot").first()
        self.assertEqual(task.parent.title, "Phase 2")
        self.assertEqual(task.assigned_to.pk, mate.pk)
        self.assertEqual(self.last["moved"], ["Angebot"])

    def test_a_title_holding_a_token_is_left_alone(self):
        self._teammate_and_tag()
        task = self.Item(owner=self.owner, kind="task", parent=self.project,
                         title="Call @sk-sven", status="open").save()
        text, shown = self._open()
        self.assertIn("- Call @sk-sven", text)
        self._sketch(text, shown)
        self.assertEqual(self.last["updated"], [])
        task.reload()
        self.assertIsNone(task.assigned_to)

    def test_page_and_post(self):
        client = self._client()
        pid = str(self.project.pk)
        page = client.get(f"/projects/{pid}?view=sketch")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b'id="project-sketch"', page.data)
        resp = client.post(f"/projects/{pid}/sketch", data={"text": "- A\n  - B"})
        self.assertEqual(resp.status_code, 302)
        self.assertIsNotNone(self._child(self._child(self.project, "A"), "B"))
        listing = client.get(f"/projects/{pid}")
        self.assertEqual(listing.status_code, 200)
        self.assertIn(b"bi-markdown", listing.data)

    def test_closing_asks_first(self):
        client = self._client()
        pid = str(self.project.pk)
        self._sketch("- A\n- B")
        page = client.get(f"/projects/{pid}?view=sketch")
        b = self._child(self.project, "B")
        self.assertIn(str(b.pk).encode(), page.data)        # in ``shown``
        _, shown = self._open()
        form = {"text": "- A", "shown": ",".join(shown)}
        resp = client.post(f"/projects/{pid}/sketch", data=form)
        self.assertEqual(resp.status_code, 200)              # the confirmation
        self.assertIn(b"will be closed: B", resp.data)
        b.reload()
        self.assertEqual(b.status, "open")
        resp = client.post(f"/projects/{pid}/sketch", data=dict(form, confirm="1"))
        self.assertEqual(resp.status_code, 302)
        b.reload()
        self.assertEqual(b.status, "done")


if __name__ == "__main__":
    unittest.main()
