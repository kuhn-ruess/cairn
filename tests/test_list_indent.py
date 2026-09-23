"""A sub-task has to READ as one in the project list.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

Nesting a task from the list is a one-gesture thing now, so the list is where
the result is judged — and an un-indented row that sits nowhere near its parent
says nothing happened. Two answers, because the groupings differ in what they
can honestly show:

* Where the order is ours ("Project", and "Flat", whose only order is
  sort_order) the tree is drawn: parent immediately before child, child
  indented by its depth.
* Where a CRITERION owns the order (due / priority / status / tag) a sub-task
  can land in a different group from its parent, so indenting it would be a
  lie. Those rows name their parent in the breadcrumb instead.

The indent itself is pinned as a LEVEL, not as pixels: the width of a level
belongs to the stylesheet, which shrinks it on a narrow screen.
"""
import unittest

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None


@unittest.skipIf(connect is None, "mongoengine not installed")
class ListIndentTests(unittest.TestCase):

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
        from application.models.item import Item
        from application.models.user import User
        with self.app.app_context():
            Item.all_objects.delete()
            User.objects.delete()
            self.user = User(username="ind-test", email="ind@test.invalid",
                             password_hash="x", level="premium").save()
            self.project = Item(owner=self.user, kind="project", title="DRV",
                                project_type="parallel").save()
            self.parent = Item(owner=self.user, kind="task", title="Logwatch Wrapper",
                               parent=self.project, sort_order=1).save()
            # A second root in between, so "adjacent to its parent" is a real
            # claim and not just the seed order.
            self.other = Item(owner=self.user, kind="task", title="Erweiterung CMDB",
                              parent=self.project, sort_order=2).save()
            self.child = Item(owner=self.user, kind="task", title="Paket bauen",
                              parent=self.parent, sort_order=3).save()
            self.grandchild = Item(owner=self.user, kind="task", title="MKP hochladen",
                                   parent=self.child, sort_order=4).save()
            # A pk in collapsed_items marks an EXPANDED parent for tasks.
            self.user.collapsed_items = [str(self.parent.pk), str(self.child.pk)]
            self.user.save()
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.pk)
            sess["_fresh"] = True

    def _html(self, group):
        return self.client.get(
            f"/projects/{self.project.pk}?group={group}").get_data(as_text=True)

    def _rows(self, html):
        """Titles in the order the list renders them."""
        titles = ["Logwatch Wrapper", "Erweiterung CMDB", "Paket bauen", "MKP hochladen"]
        return [t for _, t in sorted((html.index(t), t) for t in titles)]

    # -- where the order is ours, the tree is drawn ------------------------

    def test_flat_keeps_a_sub_task_next_to_its_parent(self):
        """Flat has no criterion — scattering the tree through sort_order
        bought nothing and hid what a nesting drag had just done."""
        rows = self._rows(self._html("none"))
        self.assertEqual(rows.index("Paket bauen"), rows.index("Logwatch Wrapper") + 1)
        self.assertEqual(rows.index("MKP hochladen"), rows.index("Paket bauen") + 1)
        self.assertGreater(rows.index("Erweiterung CMDB"), rows.index("MKP hochladen"))

    def test_flat_indents_by_depth(self):
        html = self._html("none")
        self.assertIn("--indent-level: 1;", html)
        self.assertIn("--indent-level: 2;", html)

    def test_the_nested_view_indents_too(self):
        html = self._html("project")
        self.assertIn("--indent-level: 1;", html)
        self.assertIn("--indent-level: 2;", html)

    def test_the_indent_is_a_level_not_a_pixel_width(self):
        """How wide a level is belongs to the stylesheet, so it can shrink on a
        narrow screen — the row only says how deep it sits."""
        html = self._html("none")
        self.assertNotIn("padding-left: 28px", html)

    def test_a_sub_task_row_is_marked_as_one(self):
        self.assertIn("subtask", self._html("none"))

    # -- where a criterion owns the order, the breadcrumb says it ----------

    def test_a_criterion_grouping_names_the_parent_task_instead(self):
        html = self._html("due")
        self.assertIn("DRV / Logwatch Wrapper", html)
        self.assertIn("DRV / Logwatch Wrapper / Paket bauen", html)

    def test_a_criterion_grouping_does_not_pretend_to_nest(self):
        html = self._html("due")
        self.assertNotIn("--indent-level:", html)

    def test_flat_does_not_repeat_the_chain_the_indent_already_shows(self):
        html = self._html("none")
        self.assertNotIn("DRV / Logwatch Wrapper", html)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
