"""One-click NEXT, and grouping a day's work by project / customer.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

Two things are pinned here.

**The NEXT mark is one POST.** Picking work together meant opening the inspector
and finding one entry in a nine-value status select, so ``/tasks/<id>/next``
takes the mark on, takes it off, and sets a duration in the same call. Three
rules are behaviour, not preference:

* a duration comes along with the mark (``minutes``), because "next, and it's
  about an hour" is one decision;
* taking the mark off only touches a task that carries it — a row is drawn from
  a list that may be a refresh behind, and a stale click must not quietly pull a
  ``waiting`` task out of its status;
* the duration SURVIVES un-marking. How long something takes is a property of
  the task, not of it being next.

**Customer grouping is inherited, not stored.** There is no customer field on a
project: a folder is bound to a billing project and that belongs to a customer
(``services.containers.customer_labels``), so the customer arrives down the
subtree the same way the billing project does — and a subtree with no binding
lands in one bucket instead of inventing a customer.
"""
import unittest

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None


@unittest.skipIf(connect is None, "mongoengine not installed")
class _Base(unittest.TestCase):

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
        from application.models.team import Team
        from application.models.timetracking import Customer, TimeTrackingProject
        from application.models.user import User
        Item.all_objects.delete()
        TimeTrackingProject.objects.delete()
        Customer.objects.delete()
        Team.objects.delete()
        User.objects.delete()

    def _login(self, user):
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(user.pk)
            sess["_fresh"] = True


class NextOneClickTests(_Base):

    def setUp(self):
        from application.models.item import Item
        from application.models.user import User
        with self.app.app_context():
            self._wipe()
            self.user = User(username="nx-test", email="nx@test.invalid",
                             password_hash="x", level="premium").save()
            self.project = Item(owner=self.user, kind="project", title="Website",
                                project_type="parallel").save()
            self.task = Item(owner=self.user, kind="task", title="Kontaktformular",
                             parent=self.project).save()
        self._login(self.user)

    def _reload(self):
        from application.models.item import Item
        with self.app.app_context():
            return Item.objects(pk=self.task.pk).first()

    def _post(self, **form):
        return self.client.post(f"/tasks/{self.task.pk}/next", data=form)

    # -- the mark ----------------------------------------------------------

    def test_one_post_marks_it_next(self):
        resp = self._post()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.get_json()["is_next"])
        self.assertEqual(self._reload().status, "next")

    def test_a_duration_comes_along_with_the_mark(self):
        """"Next, and it takes about 45 minutes" is one click, not two."""
        self._post(minutes="45")
        task = self._reload()
        self.assertEqual(task.status, "next")
        self.assertEqual(task.estimated_minutes, 45)

    def test_off_takes_the_mark_back_to_open(self):
        self._post()
        self._post(off="1")
        self.assertEqual(self._reload().status, "open")

    def test_off_leaves_a_task_that_is_not_next_alone(self):
        """A row can be a refresh behind. A stale click must not pull a
        ``waiting`` task out of the status somebody put it in."""
        from application.models.item import Item
        with self.app.app_context():
            Item.objects(pk=self.task.pk).update_one(set__status="waiting")
        self._post(off="1")
        self.assertEqual(self._reload().status, "waiting")

    def test_the_duration_survives_un_marking(self):
        """How long something takes belongs to the task, not to it being next."""
        self._post(minutes="90")
        self._post(off="1")
        task = self._reload()
        self.assertEqual(task.status, "open")
        self.assertEqual(task.estimated_minutes, 90)

    def test_a_silly_duration_is_clamped_not_stored(self):
        self._post(minutes="99999")
        self.assertEqual(self._reload().estimated_minutes, 1440)

    def test_a_duration_that_is_not_a_number_is_refused(self):
        resp = self._post(minutes="soon")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self._reload().status, "open")

    def test_somebody_elses_task_is_a_404(self):
        from application.models.item import Item
        from application.models.user import User
        with self.app.app_context():
            other = User(username="nx-other", email="nxo@test.invalid",
                         password_hash="x", level="premium").save()
            theirs = Item(owner=other, kind="task", title="Nicht meins").save()
        resp = self.client.post(f"/tasks/{theirs.pk}/next", data={})
        self.assertEqual(resp.status_code, 404)

    def test_a_project_is_not_a_next_action(self):
        resp = self.client.post(f"/tasks/{self.project.pk}/next", data={})
        self.assertEqual(resp.status_code, 400)

    # -- the row control ---------------------------------------------------

    def test_the_row_carries_the_toggle_and_the_durations(self):
        html = self.client.get(f"/projects/{self.project.pk}").get_data(as_text=True)
        self.assertIn("task-next-toggle", html)
        self.assertIn("task-next-dur", html)

    def test_the_chip_reads_back_the_duration_that_was_set(self):
        """The estimate has to be visible somewhere without the inspector —
        otherwise a one-click duration is a click with no answer."""
        self._post(minutes="45")
        html = self.client.get(f"/projects/{self.project.pk}").get_data(as_text=True)
        self.assertIn("NEXT · 45m", html)


class CustomerGroupingTests(_Base):

    def setUp(self):
        from application.models.item import Item
        from application.models.team import Team
        from application.models.timetracking import Customer, TimeTrackingProject
        from application.models.user import User
        with self.app.app_context():
            self._wipe()
            self.user = User(username="cg-test", email="cg@test.invalid",
                             password_hash="x", level="team_admin").save()
            # Membership is a reference on the USER, not a list on the team
            # (services.permissions.participating_teams).
            self.team = Team(name="Kuhn IT", admin=self.user,
                             admins=[self.user]).save()
            self.user.team = self.team
            self.user.save()
            acme = Customer(team=self.team, name="Acme GmbH").save()
            billing = TimeTrackingProject(team=self.team, customer=acme,
                                          name="Wartung").save()
            # The binding sits on the FOLDER; everything under it inherits.
            self.folder = Item(owner=self.user, kind="folder", title="Kunden",
                               time_project=billing).save()
            self.project = Item(owner=self.user, kind="project", title="Relaunch",
                                project_type="parallel", parent=self.folder).save()
            self.billed = Item(owner=self.user, kind="task", title="Cache leeren",
                               parent=self.project, flagged=True).save()
            # Deliberately outside any bound subtree.
            self.own = Item(owner=self.user, kind="project", title="Intern",
                            project_type="parallel").save()
            self.unbilled = Item(owner=self.user, kind="task", title="Backup prüfen",
                                 parent=self.own, flagged=True).save()
        self._login(self.user)

    def test_the_customer_is_inherited_down_the_subtree(self):
        from application.services.containers import customer_labels
        with self.app.app_context():
            from application.models.item import Item
            tasks = list(Item.objects(pk__in=[self.billed.pk, self.unbilled.pk]))
            labels = customer_labels(tasks)
        self.assertEqual(labels.get(str(self.billed.pk)), "Acme GmbH")
        self.assertNotIn(str(self.unbilled.pk), labels)

    def test_archiving_the_billing_project_leaves_the_customer_named(self):
        """Binding the folder said "this is Acme's" — archiving the project it
        bills to retires a way of INVOICING, not the client. The name goes when
        the CUSTOMER is archived, which is the next test."""
        from application.models.item import Item
        from application.models.timetracking import TimeTrackingProject
        from application.services.containers import customer_labels
        with self.app.app_context():
            TimeTrackingProject.objects.update(set__archived=True)
            labels = customer_labels(list(Item.objects(pk=self.billed.pk)))
        self.assertEqual(labels.get(str(self.billed.pk)), "Acme GmbH")

    def test_an_archived_customer_names_nobody(self):
        from application.models.item import Item
        from application.models.timetracking import Customer
        from application.services.containers import customer_labels
        with self.app.app_context():
            Customer.objects.update(set__archived=True)
            labels = customer_labels(list(Item.objects(pk=self.billed.pk)))
        self.assertEqual(labels, {})

    def test_a_binding_made_to_an_archived_project_names_nobody(self):
        """The other half of the rule: a project already out of circulation is
        not an answer, so binding to one names no customer at all."""
        from application.models.item import Item
        from application.models.timetracking import Customer, TimeTrackingProject
        from application.services.containers import customer_labels
        with self.app.app_context():
            old = Customer(team=self.team, name="Globex").save()
            dead = TimeTrackingProject(team=self.team, customer=old,
                                       name="Alt", archived=True).save()
            folder = Item(owner=self.user, kind="folder", title="Alt",
                          time_project=dead).save()
            task = Item(owner=self.user, kind="task", title="X",
                        parent=folder).save()
            self.assertIsNone(Item.objects(pk=folder.pk).first().customer)
            labels = customer_labels(list(Item.objects(pk=task.pk)))
        self.assertEqual(labels, {})

    def test_the_path_is_resolved_for_the_whole_list_at_once(self):
        from application.models.item import Item
        from application.services.containers import container_paths
        with self.app.app_context():
            paths = container_paths(list(Item.objects(
                pk__in=[self.billed.pk, self.unbilled.pk])))
        self.assertEqual(paths[str(self.billed.pk)], "Kunden / Relaunch")
        self.assertEqual(paths[str(self.unbilled.pk)], "Intern")

    def test_today_groups_by_customer(self):
        html = self.client.get("/tasks/today?group=customer").get_data(as_text=True)
        self.assertIn("Acme GmbH", html)
        self.assertIn("No customer", html)

    def test_the_plan_rails_can_be_grouped(self):
        html = self.client.get(
            "/tasks/today?view=plan&pgroup=customer").get_data(as_text=True)
        self.assertIn("plan-rail-group-head", html)
        self.assertIn("Acme GmbH", html)

    def test_the_rail_grouping_is_remembered(self):
        self.client.get("/tasks/today?view=plan&pgroup=project")
        from application.models.user import User
        with self.app.app_context():
            self.assertEqual(User.objects(pk=self.user.pk).first().plan_grouping,
                             "project")

    def test_a_flat_rail_has_no_section_heads(self):
        html = self.client.get(
            "/tasks/today?view=plan&pgroup=none").get_data(as_text=True)
        self.assertNotIn("plan-rail-group-head", html)

    def test_a_made_up_grouping_falls_back(self):
        resp = self.client.get("/tasks/today?view=plan&pgroup=; drop")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("plan-rail-group-head", resp.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
