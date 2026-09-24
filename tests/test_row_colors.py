"""Colouring rows by project or customer.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one — the palette half is
pure and runs anywhere.

What is pinned here is the set of decisions, not the hex values:

**A colour is derived when nobody picked one.** ``Item.color`` existed for years
and was rendered nowhere, so almost every project has none. An empty field would
make the feature invisible for everybody who never opened the colour picker, so
the colour falls back to one derived from the container — and it has to be the
SAME one in every worker and after every restart, which is why ``hashlib`` is
used and not Python's salted ``hash()``.

**A colour is inherited like the customer is.** Set one on the "Kunden" folder
and every project under it carries it; that is the same climb
``customer_labels`` makes, and it is what makes one colour per client possible
without touching thirty projects.

**A row that resolves to nothing gets nothing.** No grey default: a swatch
nobody can name is worse than no swatch.

**One resolve per list, never one per row.** The resolver is batched and the
Jinja global caches per item on ``flask.g``, because this is asked inside the
loop that renders a list.
"""
import unittest
from datetime import datetime, timezone

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None

from application.services import colors


class ThePaletteTests(unittest.TestCase):
    """Pure: no database, no app."""

    def test_the_same_key_always_gets_the_same_colour(self):
        """``hash()`` is salted per process: it would repaint the whole app on
        every restart and disagree between workers on the same page load."""
        first = colors.palette_color("project:6ab5629aeeb1bc4235bc8012")
        self.assertEqual(first, colors.palette_color("project:6ab5629aeeb1bc4235bc8012"))
        self.assertIn(first, colors.PALETTE)

    def test_different_keys_spread_over_the_palette(self):
        seen = {colors.palette_color(f"project:{i}") for i in range(200)}
        self.assertEqual(seen, set(colors.PALETTE))

    def test_nothing_in_nothing_out(self):
        self.assertEqual(colors.palette_color(""), "")
        self.assertEqual(colors.palette_color(None), "")

    def test_only_a_hex_colour_reaches_a_style_attribute(self):
        self.assertEqual(colors.normalise("#fabd2f"), "#fabd2f")
        self.assertEqual(colors.normalise(" #abc "), "#abc")
        for junk in ("", None, "red", "fabd2f", "#12345",
                     'x; background: url(javascript:alert(1))'):
            self.assertEqual(colors.normalise(junk), "", junk)

    def test_off_resolves_nothing_at_all(self):
        self.assertEqual(colors.row_colors([object()], "none"), {})


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


class RowColorTests(_Base):

    def setUp(self):
        from application.models.item import Item
        from application.models.team import Team
        from application.models.timetracking import Customer, TimeTrackingProject
        from application.models.user import User
        with self.app.app_context():
            self._wipe()
            self.user = User(username="rc-test", email="rc@test.invalid",
                             password_hash="x", level="team_admin").save()
            self.team = Team(name="Kuhn IT", admin=self.user,
                             admins=[self.user]).save()
            self.user.team = self.team
            self.user.save()
            acme = Customer(team=self.team, name="Acme GmbH").save()
            billing = TimeTrackingProject(team=self.team, customer=acme,
                                          name="Wartung").save()
            # A client folder, bound to its billing project, with a colour of
            # its own — and a project under it that has none.
            self.folder = Item(owner=self.user, kind="folder", title="Kunden",
                              time_project=billing, color="#8ec07c").save()
            self.project = Item(owner=self.user, kind="project",
                                title="Relaunch", project_type="parallel",
                                parent=self.folder).save()
            # Planned for right now, so both the Today list and the Plan
            # timeline actually show them.
            planned = datetime.now(timezone.utc).replace(microsecond=0)
            self.task = Item(owner=self.user, kind="task", parent=self.project,
                             title="Cache leeren", plan_date=planned,
                             estimated_minutes=30).save()
            # A project nobody ever gave a colour, outside any client folder.
            self.plain = Item(owner=self.user, kind="project", title="Intern",
                              project_type="parallel").save()
            self.plain_task = Item(owner=self.user, kind="task",
                                   parent=self.plain, title="Backup prüfen",
                                   plan_date=planned,
                                   estimated_minutes=30).save()
            # Filed nowhere at all.
            self.inbox_task = Item(owner=self.user, kind="task",
                                   title="Rückruf").save()
        self._login(self.user)

    def _resolve(self, mode, *tasks):
        from application.models.item import Item
        with self.app.app_context():
            items = list(Item.objects(pk__in=[t.pk for t in tasks]))
            return colors.row_colors(items, mode)

    # -- project mode ------------------------------------------------------

    def test_a_colour_set_on_the_folder_reaches_the_task(self):
        """One colour per client, set in one place, inherited by the subtree."""
        out = self._resolve("project", self.task)
        self.assertEqual(out[str(self.task.pk)]["color"], "#8ec07c")
        self.assertEqual(out[str(self.task.pk)]["label"], "Kunden / Relaunch")

    def test_a_project_without_one_still_gets_a_colour(self):
        out = self._resolve("project", self.plain_task)
        self.assertIn(out[str(self.plain_task.pk)]["color"], colors.PALETTE)
        self.assertEqual(out[str(self.plain_task.pk)]["label"], "Intern")

    def test_the_derived_colour_is_stable_across_calls(self):
        first = self._resolve("project", self.plain_task)
        self.assertEqual(self._resolve("project", self.plain_task), first)

    def test_renaming_the_project_does_not_repaint_it(self):
        """The derived colour is keyed on the container's id, not its name — a
        list that changes colour because somebody fixed a typo is noise."""
        from application.models.item import Item
        before = self._resolve("project", self.plain_task)[str(self.plain_task.pk)]
        with self.app.app_context():
            Item.objects(pk=self.plain.pk).update_one(set__title="Interna")
        after = self._resolve("project", self.plain_task)[str(self.plain_task.pk)]
        self.assertEqual(after["color"], before["color"])
        self.assertEqual(after["label"], "Interna")

    def test_a_task_filed_nowhere_gets_no_swatch(self):
        self.assertEqual(self._resolve("project", self.inbox_task), {})

    def test_a_junk_colour_on_the_container_is_not_trusted(self):
        """``Item.color`` is free text written by a form, the API and importers,
        and it ends up inside a style attribute — so anything that is not a hex
        colour falls back to the derived one (``normalise`` is what refuses the
        payload shapes; that is pinned in ``ThePaletteTests``)."""
        from application.models.item import Item
        with self.app.app_context():
            Item.objects(pk=self.folder.pk).update_one(set__color="red")
        out = self._resolve("project", self.task)
        self.assertIn(out[str(self.task.pk)]["color"], colors.PALETTE)

    # -- customer mode -----------------------------------------------------

    def test_the_customer_colours_the_row(self):
        out = self._resolve("customer", self.task)
        self.assertEqual(out[str(self.task.pk)]["label"], "Acme GmbH")
        self.assertIn(out[str(self.task.pk)]["color"], colors.PALETTE)

    def test_two_rows_of_the_same_customer_look_alike(self):
        """Which is the entire point of colouring by customer."""
        from application.models.item import Item
        with self.app.app_context():
            second = Item(owner=self.user, kind="task", parent=self.folder,
                          title="Rechnung").save()
        out = self._resolve("customer", self.task, second)
        self.assertEqual(out[str(self.task.pk)]["color"],
                         out[str(second.pk)]["color"])

    def test_a_subtree_that_bills_to_nobody_gets_no_swatch(self):
        self.assertEqual(self._resolve("customer", self.plain_task), {})

    def test_project_and_customer_are_not_the_same_answer(self):
        """A task under a client folder has both, and they are different
        groupings — the project one names the project."""
        self.assertEqual(self._resolve("project", self.task)[str(self.task.pk)]["label"],
                         "Kunden / Relaunch")
        self.assertEqual(self._resolve("customer", self.task)[str(self.task.pk)]["label"],
                         "Acme GmbH")

    # -- the setting, and what the pages render ----------------------------

    def test_the_setting_is_saved_from_the_profile_form(self):
        from application.models.user import User
        self.client.post("/settings/profile", data={
            "email": "rc@test.invalid", "timezone": "",
            "color_by": "customer", "reminder_lead_min": "30",
        }, follow_redirects=True)
        with self.app.app_context():
            user = User.objects(pk=self.user.pk).first()
        self.assertEqual(user.color_by, "customer")
        self.assertEqual(user.reminder_lead_min, 30)

    def test_a_lead_time_nobody_offered_falls_back_to_the_default(self):
        """Never silently to OFF — that would turn reminders off on a form
        somebody submitted for an unrelated field."""
        from application.models.user import User
        from application.services.reminders import DEFAULT_LEAD_MIN
        self.client.post("/settings/profile", data={
            "email": "rc@test.invalid", "timezone": "",
            "reminder_lead_min": "7",
        }, follow_redirects=True)
        with self.app.app_context():
            self.assertEqual(User.objects(pk=self.user.pk).first().reminder_lead_min,
                             DEFAULT_LEAD_MIN)

    def test_a_task_row_carries_the_swatch(self):
        body = self.client.get("/tasks/today").get_data(as_text=True)
        self.assertIn("item-dot", body)

    def test_switching_it_off_removes_every_swatch(self):
        from application.models.user import User
        with self.app.app_context():
            User.objects(pk=self.user.pk).update_one(set__color_by="none")
        body = self.client.get("/tasks/today").get_data(as_text=True)
        self.assertNotIn("item-dot", body)

    def test_no_task_is_resolved_twice_on_one_page(self):
        """The Jinja global caches per item on ``flask.g``. A page includes the
        row partial once per group and the plan view asks for the union of three
        rails, so without the cache the ancestor walk would run several times
        over the same tasks."""
        calls = []
        real = colors.row_colors

        def counting(items, mode):
            items = list(items)
            calls.append([str(i.pk) for i in items])
            return real(items, mode)

        colors.row_colors = counting
        try:
            self.client.get("/tasks/today?view=plan")
            self.client.get("/tasks/today?group=project")
        finally:
            colors.row_colors = real
        for page in (calls[:1], calls[1:]):
            resolved = [pk for call in page for pk in call]
            self.assertEqual(len(resolved), len(set(resolved)))
