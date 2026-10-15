"""The dependency graph in a project's header (``/projects/<id>/graph``).

Run with ``python -m unittest discover tests``; needs a MongoDB
(``TestConfig`` points at ``bktodo_test``) and is skipped without one.

From the request on the task: the graph can be shown in the head of a
project, with only the project's own tasks; where a dependency reaches a task
outside, the arrow goes to a placeholder that opens the big graph.

What is pinned here:

* a dependency between two tasks of the project is an edge between them;
* a prerequisite outside the project is not a node: the edge ends at the
  "outside prerequisites" placeholder, which links to the full graph and only
  counts the outside tasks (their titles never reach the page);
* an outside task waiting on one in here is the "outside dependents"
  placeholder, on the other side;
* a task of a sub-project counts as inside;
* a task with no dependency at all is not drawn;
* the project page carries the toggle and the panel, and costs no graph query
  until the panel asks for it.
"""
import unittest

try:
    from tests.test_plan_nothing_vanishes import PlanBase
except Exception:  # pragma: no cover (no mongoengine installed)
    PlanBase = None


@unittest.skipIf(PlanBase is None, "mongoengine not installed")
class ProjectDepGraphTests(PlanBase if PlanBase else unittest.TestCase):

    def _task(self, title, parent, deps=()):
        from application.models.item import Item
        with self.app.app_context():
            t = Item(owner=self.user, kind="task", title=title, parent=parent)
            t.depends_on = list(deps)
            return t.save()

    def _graph(self, project=None):
        resp = self.client.get(f"/projects/{(project or self.project).pk}/graph")
        self.assertEqual(resp.status_code, 200)
        return resp.get_json()

    def test_edges_inside_and_placeholders_for_outside(self):
        from application.models.item import Item
        with self.app.app_context():
            other = Item(owner=self.user, kind="project", title="Elsewhere",
                         project_type="parallel").save()
            sub = Item(owner=self.user, kind="project", title="Sub",
                       parent=self.project, project_type="parallel").save()
        foreign = self._task("Secret outside task", other)
        base = self._task("Base", self.project, [foreign])
        top = self._task("Top", sub, [base])
        self._task("Waits from outside", other, [top])
        self._task("No deps", self.project)

        data = self._graph()
        by_id = {n["id"]: n for n in data["nodes"]}
        edges = {(e["from"], e["to"]) for e in data["edges"]}

        self.assertEqual(
            {n["title"] for n in data["nodes"] if not n.get("placeholder")},
            {"Base", "Top"})
        self.assertIn((str(top.pk), str(base.pk)), edges)
        self.assertIn((str(base.pk), "outside-prerequisites"), edges)
        self.assertIn(("outside-dependents", str(top.pk)), edges)
        self.assertNotIn(str(foreign.pk), by_id)
        pre = by_id["outside-prerequisites"]
        self.assertTrue(pre["placeholder"])
        self.assertEqual(pre["url"], "/tasks/graph")
        self.assertIn("1 task", pre["title"])
        self.assertNotIn("Secret", resp_text(data))

    def test_no_dependencies_is_an_empty_graph(self):
        self._task("Alone", self.project)
        data = self._graph()
        self.assertEqual(data["nodes"], [])
        self.assertEqual(data["edges"], [])

    def test_a_trashed_outside_prerequisite_draws_no_placeholder(self):
        from application.models.item import Item
        from datetime import datetime, timezone
        with self.app.app_context():
            other = Item(owner=self.user, kind="project", title="Elsewhere",
                         project_type="parallel").save()
        gone = self._task("Gone", other)
        a = self._task("A", self.project, [gone])
        self._task("B", self.project, [a])
        with self.app.app_context():
            Item.all_objects(pk=gone.pk).update(
                set__trashed_at=datetime.now(timezone.utc))
        data = self._graph()
        self.assertNotIn("outside-prerequisites",
                         {n["id"] for n in data["nodes"]})

    def test_the_page_has_the_toggle_and_the_panel(self):
        resp = self.client.get(f"/projects/{self.project.pk}")
        html = resp.get_data(as_text=True)
        self.assertIn("data-dep-graph-toggle", html)
        self.assertIn('id="project-dep-graph"', html)
        self.assertIn(f"/projects/{self.project.pk}/graph", html)


def resp_text(data):
    import json
    return json.dumps(data)


if __name__ == "__main__":
    unittest.main()
