"""Customers as one concept for the whole app — and billing a meeting.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

Four things are pinned here, and each one is a decision rather than an
implementation detail.

**There is one customer entity.** ``Customer`` — the record the billing module
already had — is what a folder, a project and a meeting are filed under. A
folder either names it (``Item.customer``) or is bound to one of its billing
projects, and either way everything inside inherits it. The two can never
disagree: binding a billing project stamps that project's customer on the
container, so "the folder says Acme while its hours invoice to Globex" is not a
state the app can reach.

**A domain match is allowed to be silent, never wrong.** An appointment booked
from ``anna@acme.de`` is Acme's. A free-mail address is never matched, and
neither is a domain two customers claim — both leave the field empty for
somebody to fill, because a guess here surfaces as a wrong invoice.

**Nothing bills itself.** Taking a booking writes no time. The time is logged
from the list, one press at a time, exactly as on a task — which is also why a
cancelled, moved or never-held meeting needs no special state.

**A meeting that ran long is billed long.** The duration in the panel is what
gets booked, and the correction sticks to the appointment
(``Booking.actual_minutes``) so the list keeps saying so afterwards.
"""
import unittest
from datetime import datetime, timedelta, timezone

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
        from application.models.booking import Booking
        from application.models.item import Item
        from application.models.team import Team
        from application.models.timetracking import (
            Customer, TimeBucket, TimeTrackingProject,
        )
        from application.models.user import User
        Booking.objects.delete()
        TimeBucket.objects.delete()
        Item.all_objects.delete()
        TimeTrackingProject.objects.delete()
        Customer.objects.delete()
        Team.objects.delete()
        User.objects.delete()

    def _team_user(self, username="cu-test"):
        """A team admin with a team — customers only exist inside one."""
        from application.models.team import Team
        from application.models.user import User
        user = User(username=username, email=f"{username}@test.invalid",
                    password_hash="x", level="team_admin").save()
        team = Team(name="Kuhn IT", admin=user, admins=[user]).save()
        user.team = team
        user.save()
        return user, team

    def _login(self, user):
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(user.pk)
            sess["_fresh"] = True


class DomainParsingTests(unittest.TestCase):
    """Pure: no database, no app."""

    def test_a_bare_domain_is_lowercased_and_stripped(self):
        from application.services.customers import normalise_domain
        self.assertEqual(normalise_domain("  @Acme.DE "), "acme.de")

    def test_a_whole_address_pasted_in_still_yields_the_domain(self):
        """Half the rows would otherwise read ``anna@acme.de`` and match
        nothing, for ever, without saying so."""
        from application.services.customers import normalise_domain
        self.assertEqual(normalise_domain("Anna@Acme.de"), "acme.de")

    def test_something_that_is_not_a_domain_is_dropped(self):
        from application.services.customers import normalise_domain
        for junk in ("", "acme", "acme de", "acme.de/x", "@"):
            self.assertEqual(normalise_domain(junk), "", junk)

    def test_the_field_takes_commas_spaces_and_newlines(self):
        from application.services.customers import parse_domains
        self.assertEqual(
            parse_domains("acme.de, ACME.de\nacme-group.com ; x"),
            ["acme.de", "acme-group.com"])

    def test_domain_of_needs_an_at_sign(self):
        from application.services.customers import domain_of
        self.assertEqual(domain_of("anna@acme.de"), "acme.de")
        self.assertEqual(domain_of("acme.de"), "")


class DomainMatchTests(_Base):

    def setUp(self):
        from application.models.timetracking import Customer
        with self.app.app_context():
            self._wipe()
            self.user, self.team = self._team_user()
            self.acme = Customer(team=self.team, name="Acme GmbH",
                                 email_domains=["acme.de"]).save()

    def _match(self, email):
        with self.app.app_context():
            from application.services.customers import customer_for_email
            return customer_for_email(email, [self.team])

    def test_the_domain_names_the_customer(self):
        self.assertEqual(self._match("anna@acme.de").name, "Acme GmbH")

    def test_the_match_ignores_case(self):
        self.assertEqual(self._match("Anna@ACME.De").name, "Acme GmbH")

    def test_an_unknown_domain_matches_nothing(self):
        self.assertIsNone(self._match("anna@globex.com"))

    def test_a_subdomain_is_not_the_domain(self):
        """``mail.acme.de`` may well be somebody else entirely. A match that
        guesses at the hierarchy is the kind that ends up on an invoice."""
        self.assertIsNone(self._match("anna@mail.acme.de"))

    def test_a_freemail_domain_never_matches_even_when_typed_in(self):
        """One gmail.com row would otherwise adopt every stranger who books."""
        from application.models.timetracking import Customer
        with self.app.app_context():
            Customer.objects(pk=self.acme.pk).update_one(
                set__email_domains=["gmail.com"])
        self.assertIsNone(self._match("anna@gmail.com"))

    def test_two_customers_claiming_one_domain_match_nothing(self):
        from application.models.timetracking import Customer
        with self.app.app_context():
            Customer(team=self.team, name="Acme Nord",
                     email_domains=["acme.de"]).save()
        self.assertIsNone(self._match("anna@acme.de"))

    def test_an_archived_customer_is_out_of_the_match(self):
        from application.models.timetracking import Customer
        with self.app.app_context():
            Customer.objects(pk=self.acme.pk).update_one(set__archived=True)
        self.assertIsNone(self._match("anna@acme.de"))

    def test_another_teams_customer_is_never_matched(self):
        with self.app.app_context():
            from application.services.customers import customer_for_email
            self.assertIsNone(customer_for_email("anna@acme.de", []))

    def test_the_customer_page_saves_and_flags_the_domains(self):
        self._login(self.user)
        resp = self.client.post(
            f"/time/customers/{self.acme.pk}/edit",
            data={"name": "Acme GmbH", "team": str(self.team.pk),
                  "email_domains": "ACME.de, gmail.com"},
            follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        from application.models.timetracking import Customer
        with self.app.app_context():
            fresh = Customer.objects(pk=self.acme.pk).first()
        self.assertEqual(fresh.email_domains, ["acme.de", "gmail.com"])
        # And the page says which of them will never fire, rather than leaving
        # it to be discovered on a wrong invoice.
        html = self.client.get(
            f"/time/customers/{self.acme.pk}?team={self.team.pk}"
        ).get_data(as_text=True)
        self.assertIn("Never matched (free mail)", html)


class ContainerCustomerTests(_Base):

    def setUp(self):
        from application.models.item import Item
        from application.models.timetracking import Customer, TimeTrackingProject
        with self.app.app_context():
            self._wipe()
            self.user, self.team = self._team_user("cu-cont")
            self.acme = Customer(team=self.team, name="Acme GmbH").save()
            self.globex = Customer(team=self.team, name="Globex").save()
            self.acme_billing = TimeTrackingProject(
                team=self.team, customer=self.acme, name="Wartung").save()
            self.folder = Item(owner=self.user, kind="folder",
                               title="Kunden").save()
            self.project = Item(owner=self.user, kind="project", title="Relaunch",
                                project_type="parallel", parent=self.folder).save()
            self.task = Item(owner=self.user, kind="task", title="Cache leeren",
                             parent=self.project).save()
        self._login(self.user)

    def _labels(self):
        from application.models.item import Item
        from application.services.containers import customer_labels
        with self.app.app_context():
            return customer_labels(list(Item.objects(pk=self.task.pk)))

    def _reload(self, item):
        from application.models.item import Item
        with self.app.app_context():
            return Item.objects(pk=item.pk).first()

    def test_a_customer_named_on_a_folder_reaches_the_task_below(self):
        from application.models.item import Item
        with self.app.app_context():
            Item.objects(pk=self.folder.pk).update_one(set__customer=self.acme)
        self.assertEqual(self._labels()[str(self.task.pk)], "Acme GmbH")

    def test_the_nearer_container_wins(self):
        from application.models.item import Item
        with self.app.app_context():
            Item.objects(pk=self.folder.pk).update_one(set__customer=self.acme)
            Item.objects(pk=self.project.pk).update_one(set__customer=self.globex)
        self.assertEqual(self._labels()[str(self.task.pk)], "Globex")

    def test_a_billing_binding_still_answers_it(self):
        """The old route in — a folder bound to a billing project — is
        unchanged. There is no migration and nothing to re-enter."""
        from application.models.item import Item
        with self.app.app_context():
            Item.objects(pk=self.folder.pk).update_one(
                set__time_project=self.acme_billing)
        self.assertEqual(self._labels()[str(self.task.pk)], "Acme GmbH")

    def test_an_archived_customer_groups_nothing(self):
        from application.models.item import Item
        from application.models.timetracking import Customer
        with self.app.app_context():
            Item.objects(pk=self.folder.pk).update_one(set__customer=self.acme)
            Customer.objects(pk=self.acme.pk).update_one(set__archived=True)
        self.assertEqual(self._labels(), {})

    def test_nothing_bound_means_no_customer(self):
        self.assertEqual(self._labels(), {})

    # -- the inspector, and the one rule that keeps it honest ---------------

    def _save(self, item, **over):
        data = {"name": item.title, "kind": item.kind, "parent": "",
                "note": "", "color": "", "status": "active",
                "project_type": "parallel", "customer": "", "time_project": "",
                "notes_folder": ""}
        data.update(over)
        return self.client.post(f"/projects/{item.pk}/edit?inspector=1",
                                data=data, headers={"X-Inspector-Request": "1"})

    def test_the_inspector_names_a_customer(self):
        resp = self._save(self.folder, customer=str(self.acme.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._reload(self.folder).customer, self.acme)

    def test_a_billing_project_stamps_its_own_customer(self):
        """One truth. Picking Acme's billing project says whose work this is,
        so the customer follows it instead of being a second field to keep in
        step by hand."""
        self._save(self.folder, time_project=str(self.acme_billing.pk))
        self.assertEqual(self._reload(self.folder).customer, self.acme)

    def test_the_binding_wins_over_a_contradicting_pick(self):
        """Globex named on a folder whose hours invoice to Acme is exactly the
        drift this field is not allowed to have.

        The picker no longer OFFERS that pair (see ``BillingScopeTests``), but
        the one row it still lets through is the project already bound — it
        carries hours — and then the binding is what names the customer."""
        from application.models.item import Item
        with self.app.app_context():
            Item.objects(pk=self.folder.pk).update_one(
                set__time_project=self.acme_billing)
        self._save(self.folder, customer=str(self.globex.pk),
                   time_project=str(self.acme_billing.pk))
        self.assertEqual(self._reload(self.folder).customer, self.acme)

    def test_a_stranger_s_customer_cannot_be_bound(self):
        from application.models.team import Team
        from application.models.timetracking import Customer
        from application.models.user import User
        with self.app.app_context():
            other = User(username="cu-other", email="cuo@test.invalid",
                         password_hash="x", level="team_admin").save()
            their_team = Team(name="Somebody Else", admin=other,
                              admins=[other]).save()
            theirs = Customer(team=their_team, name="Not mine").save()
        self._save(self.folder, customer=str(theirs.pk))
        self.assertIsNone(self._reload(self.folder).customer)

    def test_the_sidebar_reaches_the_customers(self):
        """Customers are one concept for the whole app, so they are a row in the
        sidebar and not a page you have to enter the time module to find."""
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("org:customers", html)          # the favourite star
        self.assertIn("bi-person-vcard", html)        # the row's icon
        self.assertIn("Customers", html)

    # -- a binding is never a customer you have to enter a second time -------

    def test_a_binding_made_anywhere_else_still_names_the_customer(self):
        """The inspector is one of four places that bind a billing project —
        the link button on the project's page, filing unassigned time and the
        Zammad import are the others, and none of them knew about the customer.
        The rule lives on the way into the database instead."""
        from application.models.item import Item
        with self.app.app_context():
            folder = Item.objects(pk=self.folder.pk).first()
            folder.time_project = self.acme_billing
            folder.save()
        self.assertEqual(self._reload(self.folder).customer, self.acme)

    def test_the_link_button_names_the_customer_too(self):
        """``/time/link`` — the same binding, made from the billing project's
        own page."""
        resp = self.client.post(
            "/time/link",
            data={"item_id": str(self.folder.pk), "team": str(self.team.pk),
                  "project_id": str(self.acme_billing.pk)},
            follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._reload(self.folder).customer, self.acme)

    def test_a_container_bound_before_the_field_existed_shows_its_customer(self):
        """What the report was about: a folder that already bills to Acme opened
        with an empty Customer picker, asking for an answer it was carrying all
        along. Nothing is saved to get there and no backfill runs — the picker
        stands on the binding."""
        from application.models.item import Item
        with self.app.app_context():
            # update_one, so the container is exactly what a pre-existing one
            # looks like: a binding and no customer.
            Item.objects(pk=self.folder.pk).update_one(
                set__time_project=self.acme_billing, unset__customer=True)
            self.assertIsNone(Item.objects(pk=self.folder.pk).first().customer)
        html = self.client.get(f"/projects/{self.folder.pk}/edit?inspector=1",
                               headers={"X-Inspector-Request": "1"}
                               ).get_data(as_text=True)
        self.assertIn(f'<option selected value="{self.acme.pk}">', html)

    def test_opening_and_saving_that_container_writes_it_down(self):
        from application.models.item import Item
        with self.app.app_context():
            Item.objects(pk=self.folder.pk).update_one(
                set__time_project=self.acme_billing, unset__customer=True)
        self._save(self.folder, time_project=str(self.acme_billing.pk),
                   customer=str(self.acme.pk))
        self.assertEqual(self._reload(self.folder).customer, self.acme)


class OneBindingOneDoorTests(_Base):
    """A binding is made in one place: the container's own inspector.

    The billing project's page used to ask the same question from the other end
    — a card headed "Where its time comes from", with its own picker and an
    unlink button — for the very same ``Item.time_project`` the folder sets as
    its *Billing project*. One link asked from both ends reads as two
    mechanisms, and the reverse picker was the weaker of the two: it offered
    only the viewer's own containers, said nothing about a container that
    already billed somewhere else, and re-stamped its customer on the way. The
    page now reads the binding back and points at where it is set.

    The LIST is not redundant and stays: it is the only place that shows every
    container feeding a billing project, colleagues' and private ones included.
    """

    def setUp(self):
        from application.models.item import Item
        from application.models.timetracking import Customer, TimeTrackingProject
        with self.app.app_context():
            self._wipe()
            self.user, self.team = self._team_user("cu-door")
            self.acme = Customer(team=self.team, name="Acme GmbH").save()
            self.billing = TimeTrackingProject(
                team=self.team, customer=self.acme, name="Wartung").save()
            self.folder = Item(owner=self.user, kind="folder",
                               title="Acme Relaunch",
                               time_project=self.billing).save()
        self._login(self.user)

    def _page(self):
        resp = self.client.get(f"/time/projects/{self.billing.pk}"
                               f"?team={self.team.pk}")
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True)

    def test_the_page_still_says_what_books_here(self):
        html = self._page()
        self.assertIn("What books here", html)
        self.assertIn("Acme Relaunch", html)

    def test_it_no_longer_offers_a_picker_of_its_own(self):
        html = self._page()
        self.assertNotIn("Link a folder / project", html)
        self.assertNotIn("/time/link", html)

    def test_the_row_goes_through_to_where_the_binding_is_set(self):
        """Read-only is only half an answer — the page has to say where the
        answer is given."""
        self.assertIn(f"/projects/{self.folder.pk}", self._page())

    def test_a_colleagues_private_container_is_named_but_not_linked(self):
        """It is listed (that is the point of the list), but the viewer cannot
        open it, and a link that 404s is worse than plain text."""
        from application.models.item import Item
        from application.models.user import User
        with self.app.app_context():
            other = User(username="cu-door2", email="cud2@test.invalid",
                         password_hash="x", level="premium").save()
            hidden = Item(owner=other, kind="folder", title="Privat",
                          time_project=self.billing).save()
        html = self._page()
        self.assertIn("Privat", html)
        self.assertNotIn(f"/projects/{hidden.pk}", html)

    def test_with_nothing_bound_the_page_says_where_to_go(self):
        from application.models.item import Item
        with self.app.app_context():
            Item.objects(pk=self.folder.pk).update_one(unset__time_project=True)
        self.assertIn("Nothing books here yet", self._page())

    def test_the_binding_itself_is_untouched(self):
        """Only the second door closed: a task below the folder still bills
        here without anyone picking a project."""
        from application.models.item import Item
        from application.services.timetracking import resolve_time_project
        with self.app.app_context():
            task = Item(owner=self.user, kind="task", title="Cache leeren",
                        parent=self.folder).save()
            self.assertEqual(resolve_time_project(task), self.billing)


class CustomerPickerTests(_Base):
    """One company, one row. A picker that offers the same name twice with
    nothing to choose between them is how a folder ends up under the wrong
    team's client — and the customer list is the one list in the app that
    reaches across teams."""

    def setUp(self):
        with self.app.app_context():
            self._wipe()
            self.user, self.team = self._team_user("cu-pick")

    def _choices(self):
        with self.app.app_context():
            from application.services.customers import customer_choices
            from application.models.user import User
            return customer_choices(User.objects(pk=self.user.pk).first())

    def test_one_team_offers_plain_names(self):
        from application.models.timetracking import Customer
        with self.app.app_context():
            Customer(team=self.team, name="Acme GmbH").save()
            Customer(team=self.team, name="Globex").save()
        self.assertEqual([lbl for _, lbl in self._choices()],
                         ["Acme GmbH", "Globex"])

    def test_the_same_client_in_two_teams_is_told_apart(self):
        from application.models.team import Team
        from application.models.timetracking import Customer
        with self.app.app_context():
            second = Team(name="Zweites Team", admin=self.user,
                          admins=[self.user]).save()
            Customer(team=self.team, name="Acme GmbH").save()
            Customer(team=second, name="Acme GmbH").save()
            Customer(team=second, name="Globex").save()
        labels = [lbl for _, lbl in self._choices()]
        self.assertEqual(sorted(labels),
                         sorted(["Acme GmbH · Kuhn IT", "Acme GmbH · Zweites Team",
                                 "Globex"]))

    def test_every_customer_is_offered_exactly_once(self):
        from application.models.team import Team
        from application.models.timetracking import Customer
        with self.app.app_context():
            second = Team(name="Zweites Team", admin=self.user,
                          admins=[self.user]).save()
            a = Customer(team=self.team, name="Acme GmbH").save()
            b = Customer(team=second, name="Acme GmbH").save()
        ids = [cid for cid, _ in self._choices()]
        self.assertEqual(sorted(ids), sorted([str(a.pk), str(b.pk)]))

    def test_an_archived_customer_is_not_offered(self):
        from application.models.timetracking import Customer
        with self.app.app_context():
            Customer(team=self.team, name="Acme GmbH", archived=True).save()
        self.assertEqual(self._choices(), [])

    def test_the_billing_project_picker_is_told_apart_the_same_way(self):
        """The same list, one level down: "Acme › Support" twice is the row
        that books the hours to the wrong team."""
        from application.models.team import Team
        from application.models.timetracking import Customer, TimeTrackingProject
        from application.models.user import User
        with self.app.app_context():
            second = Team(name="Zweites Team", admin=self.user,
                          admins=[self.user]).save()
            for team in (self.team, second):
                cust = Customer(team=team, name="Acme GmbH").save()
                TimeTrackingProject(team=team, customer=cust,
                                    name="Support").save()
            from application.services.timetracking import pickable_projects
            rows = pickable_projects(User.objects(pk=self.user.pk).first())
        labels = sorted(f"{r['customer']} › {r['name']}" for r in rows)
        self.assertEqual(labels, ["Acme GmbH · Kuhn IT › Support",
                                  "Acme GmbH · Zweites Team › Support"])

    def test_one_team_keeps_the_billing_labels_plain(self):
        from application.models.timetracking import Customer, TimeTrackingProject
        from application.models.user import User
        with self.app.app_context():
            cust = Customer(team=self.team, name="Acme GmbH").save()
            TimeTrackingProject(team=self.team, customer=cust,
                                name="Support").save()
            from application.services.timetracking import pickable_projects
            rows = pickable_projects(User.objects(pk=self.user.pk).first())
        self.assertEqual([r["customer"] for r in rows], ["Acme GmbH"])


class BillingScopeTests(_Base):
    """A chosen customer scopes the Billing-project picker.

    A billing project belongs to exactly one customer, so once a container says
    whose work it is, every other client's projects are noise in that dropdown —
    and worse than noise: binding one of them is the drift the stamping exists
    to keep unreachable. So it is a **hard** filter, and the escape hatch is the
    Customer field one line above rather than a loophole in the picker.

    Three things have to hold, and each is a decision:

    * **No customer chosen → everything, as before.** The filter narrows, it
      never becomes a requirement.
    * **A project already bound is never filtered away**, even when it belongs
      to somebody else. It carries logged hours and a whole subtree bills
      through it; dropping it out of the options would unbind it on the next
      save. It is listed, marked as another customer's, and the binding then
      still wins on the way into the database.
    * **A customer with no billing project says so.** An empty dropdown reads
      like a list that failed to load, so the field stays on the page and names
      where a project is created.
    """

    def setUp(self):
        from application.models.item import Item
        from application.models.timetracking import Customer, TimeTrackingProject
        with self.app.app_context():
            self._wipe()
            self.user, self.team = self._team_user("cu-scope")
            self.acme = Customer(team=self.team, name="Acme GmbH").save()
            self.globex = Customer(team=self.team, name="Globex").save()
            self.leer = Customer(team=self.team, name="Ohne Projekt").save()
            self.acme_billing = TimeTrackingProject(
                team=self.team, customer=self.acme, name="Wartung").save()
            self.globex_billing = TimeTrackingProject(
                team=self.team, customer=self.globex, name="Relaunch").save()
            self.folder = Item(owner=self.user, kind="folder",
                               title="Kunden").save()
        self._login(self.user)

    def _scope(self, customer_id="", bound_id=""):
        from application.models.user import User
        from application.services.customers import billing_choices
        with self.app.app_context():
            return billing_choices(User.objects(pk=self.user.pk).first(),
                                   customer_id=customer_id, bound_id=bound_id)

    def _inspector_html(self):
        return self.client.get(f"/projects/{self.folder.pk}/edit?inspector=1",
                               headers={"X-Inspector-Request": "1"}
                               ).get_data(as_text=True)

    def _save(self, **over):
        data = {"name": self.folder.title, "kind": "folder", "parent": "",
                "note": "", "color": "", "status": "active",
                "project_type": "parallel", "customer": "", "time_project": "",
                "notes_folder": ""}
        data.update(over)
        return self.client.post(f"/projects/{self.folder.pk}/edit?inspector=1",
                                data=data, headers={"X-Inspector-Request": "1"})

    def _reload(self):
        from application.models.item import Item
        with self.app.app_context():
            return Item.objects(pk=self.folder.pk).first()

    # -- the filter ---------------------------------------------------------

    def test_a_chosen_customer_narrows_the_picker(self):
        scope = self._scope(customer_id=str(self.acme.pk))
        self.assertEqual([lbl for _, lbl in scope["choices"]],
                         ["Acme GmbH › Wartung"])
        self.assertEqual(scope["out_of_scope"], [])
        self.assertFalse(scope["empty"])

    def test_no_customer_chosen_still_offers_everything(self):
        """The one case that must not change: a container that names nobody
        bills wherever it likes, exactly as before this existed."""
        scope = self._scope()
        self.assertEqual(sorted(lbl for _, lbl in scope["choices"]),
                         ["Acme GmbH › Wartung", "Globex › Relaunch"])
        self.assertFalse(scope["empty"])

    def test_the_inspector_offers_only_that_customers_projects(self):
        from application.models.item import Item
        with self.app.app_context():
            Item.objects(pk=self.folder.pk).update_one(set__customer=self.acme)
        html = self._inspector_html()
        self.assertIn("Acme GmbH › Wartung", html)
        self.assertNotIn("Globex › Relaunch", html)

    def test_a_project_of_another_customer_cannot_be_posted(self):
        """The filter has teeth: the pair the picker refuses to offer is the
        pair the save refuses to take, so a stale page cannot bind around it."""
        resp = self._save(customer=str(self.globex.pk),
                          time_project=str(self.acme_billing.pk))
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(self._reload().time_project)

    # -- the binding already there ------------------------------------------

    def test_a_bound_project_of_another_customer_is_kept_and_marked(self):
        scope = self._scope(customer_id=str(self.globex.pk),
                            bound_id=str(self.acme_billing.pk))
        labels = [lbl for _, lbl in scope["choices"]]
        self.assertIn("Acme GmbH › Wartung — other customer", labels)
        self.assertIn("Globex › Relaunch", labels)
        self.assertEqual(scope["out_of_scope"], [str(self.acme_billing.pk)])
        self.assertFalse(scope["empty"])

    def test_saving_an_out_of_scope_binding_never_drops_it(self):
        """A binding carries logged hours and a whole subtree bills through it.
        Naming another customer on top of it is a contradiction — and the answer
        is the documented one (the binding wins), not a silent unbind."""
        from application.models.item import Item
        with self.app.app_context():
            Item.objects(pk=self.folder.pk).update_one(
                set__time_project=self.acme_billing)
        resp = self._save(customer=str(self.globex.pk),
                          time_project=str(self.acme_billing.pk))
        self.assertEqual(resp.status_code, 200)
        fresh = self._reload()
        self.assertEqual(fresh.time_project, self.acme_billing)
        self.assertEqual(fresh.customer, self.acme)

    def test_the_bound_project_is_still_in_the_form_it_opens_with(self):
        """Nothing is out of scope on open — the stamp keeps the two in step —
        but the option has to be there, or an untouched save would unbind it."""
        from application.models.item import Item
        with self.app.app_context():
            Item.objects(pk=self.folder.pk).update_one(
                set__time_project=self.acme_billing)
        html = self._inspector_html()
        self.assertIn(f'<option selected value="{self.acme_billing.pk}">', html)

    # -- the dead end, named ------------------------------------------------

    def test_a_customer_without_billing_projects_reports_it(self):
        scope = self._scope(customer_id=str(self.leer.pk))
        self.assertEqual(scope["choices"], [])
        self.assertTrue(scope["empty"])

    def test_the_form_says_so_and_offers_the_way_out(self):
        """An empty dropdown is a dead end. The field stays on the page, names
        the customer and links to where a billing project is made."""
        from application.models.item import Item
        with self.app.app_context():
            Item.objects(pk=self.folder.pk).update_one(set__customer=self.leer)
        html = self._inspector_html()
        self.assertIn("has no billing project yet", html)
        self.assertIn(f"/time/customers/{self.leer.pk}", html)

    # -- the same list, asked for mid-edit ----------------------------------

    def test_the_endpoint_answers_with_exactly_the_forms_list(self):
        """The dropdown is rebuilt from this while the form is open, so it has
        to be the same answer the save will validate against."""
        resp = self.client.get(
            f"/time/projects/for-customer?customer={self.acme.pk}"
            f"&bound={self.globex_billing.pk}")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual([p["label"] for p in data["projects"]],
                         ["Acme GmbH › Wartung",
                          "Globex › Relaunch — other customer"])
        self.assertEqual(data["out_of_scope"], [str(self.globex_billing.pk)])
        self.assertFalse(data["empty"])

    def test_the_endpoint_without_a_customer_is_the_whole_list(self):
        data = self.client.get("/time/projects/for-customer").get_json()
        self.assertEqual(sorted(p["label"] for p in data["projects"]),
                         ["Acme GmbH › Wartung", "Globex › Relaunch"])


class MergeLeavesNothingDanglingTests(_Base):
    """Merging the duplicate away must take everything pointing at it along —
    a folder filed under it, and a meeting booked by its people."""

    def test_a_merge_re_points_folders_and_meetings(self):
        from datetime import datetime, timedelta, timezone
        from application.models.booking import Booking
        from application.models.item import Item
        from application.models.timetracking import Customer
        with self.app.app_context():
            self._wipe()
            user, team = self._team_user("cu-merge")
            keep = Customer(team=team, name="Acme GmbH").save()
            dupe = Customer(team=team, name="Acme GmbH (alt)").save()
            folder = Item(owner=user, kind="folder", title="Acme",
                          customer=dupe).save()
            start = datetime.now(timezone.utc) - timedelta(days=1)
            booking = Booking(owner=user, name="Anna", email="anna@acme.de",
                              start=start, end=start + timedelta(minutes=30),
                              duration_minutes=30, customer=dupe).save()
            from application.services.timetracking import merge_customers
            merge_customers(dupe, keep)
            self.assertEqual(Item.objects(pk=folder.pk).first().customer, keep)
            self.assertEqual(Booking.objects(pk=booking.pk).first().customer, keep)
            self.assertIsNone(Customer.objects(pk=dupe.pk).first())


class BookingCustomerTests(_Base):
    """Taking a booking files it — and books nothing."""

    def setUp(self):
        from application.models.timetracking import Customer
        with self.app.app_context():
            self._wipe()
            self.user, self.team = self._team_user("cu-book")
            self.acme = Customer(team=self.team, name="Acme GmbH",
                                 email_domains=["acme.de"]).save()

    def _confirm(self, email):
        """The real ``confirm`` path with the calendar and the slot check
        stubbed — what is under test is the filing around them."""
        from application.services import booking as svc
        from application.models.booking import BookingHour

        start = (datetime.now(timezone.utc) + timedelta(days=1)).replace(
            hour=9, minute=0, second=0, microsecond=0)
        page = self.user.booking
        page.enabled = True
        page.slug = "cu-book"
        page.durations = [30]
        page.hours = [BookingHour(weekday=d) for d in range(7)]
        with self.app.app_context():
            self.user.save()
            slots, write, meet = svc.slots_for, svc._write_event, svc._attach_meeting
            svc.slots_for = lambda user, duration, now=None, busy=None: [start]
            svc._write_event = lambda user, page, booking: None
            svc._attach_meeting = lambda user, page, booking, guest_link="": None
            try:
                return svc.confirm(self.user, start=start, duration_minutes=30,
                                   name="Anna", email=email, note="")
            finally:
                svc.slots_for, svc._write_event = slots, write
                svc._attach_meeting = meet

    def test_a_booking_is_filed_by_the_domain_it_came_from(self):
        booking = self._confirm("anna@acme.de")
        from application.models.booking import Booking
        with self.app.app_context():
            fresh = Booking.objects(pk=booking.pk).first()
        self.assertEqual(fresh.customer, self.acme)

    def test_a_freemail_booking_waits_to_be_assigned(self):
        booking = self._confirm("anna@gmail.com")
        from application.models.booking import Booking
        with self.app.app_context():
            self.assertIsNone(Booking.objects(pk=booking.pk).first().customer)

    def test_taking_a_booking_books_no_time(self):
        """The whole point: billing is a decision somebody takes from the
        list, so a meeting that never happens needs no unwinding."""
        self._confirm("anna@acme.de")
        from application.models.timetracking import TimeBucket
        with self.app.app_context():
            self.assertEqual(TimeBucket.objects.count(), 0)


class BillAMeetingTests(_Base):

    def setUp(self):
        from application.models.booking import Booking, BookingHour
        from application.models.timetracking import Customer, TimeTrackingProject
        with self.app.app_context():
            self._wipe()
            self.user, self.team = self._team_user("cu-bill")
            self.user.booking.enabled = True
            self.user.booking.slug = "cu-bill"
            self.user.booking.hours = [BookingHour(weekday=d) for d in range(5)]
            self.user.save()
            self.acme = Customer(team=self.team, name="Acme GmbH",
                                 email_domains=["acme.de"]).save()
            self.calls = TimeTrackingProject(team=self.team, customer=self.acme,
                                             name="Calls", intake=True,
                                             hourly_rate_cents=12000).save()
            TimeTrackingProject(team=self.team, customer=self.acme,
                                name="Wartung").save()
            now = datetime.now(timezone.utc)
            self.past = Booking(owner=self.user,
                                start=now - timedelta(days=1, hours=2),
                                end=now - timedelta(days=1, hours=1),
                                duration_minutes=30, name="Anna",
                                email="anna@acme.de",
                                customer=self.acme).save()
            self.soon = Booking(owner=self.user, start=now + timedelta(days=2),
                                end=now + timedelta(days=2, minutes=30),
                                duration_minutes=30, name="Bob",
                                email="bob@globex.com").save()
        self._login(self.user)

    def _entries(self):
        from application.models.timetracking import TimeBucket
        with self.app.app_context():
            out = []
            for b in TimeBucket.objects:
                out.extend(e for e in b.entries if e.status == "active")
            return out

    def _reload(self, booking):
        from application.models.booking import Booking
        with self.app.app_context():
            return Booking.objects(pk=booking.pk).first()

    # -- the list ----------------------------------------------------------

    def test_the_list_shows_what_is_past_as_well(self):
        """A meeting is billed after it happened, so the list has to hold it."""
        html = self.client.get("/bookings/").get_data(as_text=True)
        self.assertIn("Past appointments", html)
        self.assertIn("Anna", html)
        self.assertIn("Bill this meeting", html)

    def test_the_list_preselects_the_customer_s_intake_project(self):
        html = self.client.get("/bookings/").get_data(as_text=True)
        self.assertIn(f'value="{self.calls.pk}" selected', html)

    def test_a_meeting_with_no_customer_preselects_nothing(self):
        """Bob booked from a free-mail address, so nobody claims him. The
        picker must not helpfully offer Acme's project — that is the one
        mistake that reaches an invoice."""
        html = self.client.get("/bookings/").get_data(as_text=True)
        self.assertIn("— pick a project —", html)

    def test_a_customer_without_a_billing_project_preselects_nothing(self):
        """Same rule one step further in: knowing whose meeting it is, and
        having nowhere to book it, is not a reason to offer somebody else's."""
        from application.models.timetracking import Customer
        with self.app.app_context():
            globex = Customer(team=self.team, name="Globex").save()
        self.client.post(f"/bookings/{self.soon.pk}/customer",
                         data={"customer_id": str(globex.pk)})
        from application.services.timetracking import booking_default_project_id
        with self.app.app_context():
            self.assertEqual(
                booking_default_project_id(self.user, self._reload(self.soon)), "")

    # -- billing it --------------------------------------------------------

    def test_logging_the_booked_duration(self):
        resp = self.client.post(f"/bookings/{self.past.pk}/time",
                                data={"project_id": str(self.calls.pk),
                                      "duration": "30", "billable": "1"},
                                follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        entries = self._entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].minutes, 30)
        self.assertEqual(entries[0].source_kind, "booking")
        self.assertEqual(entries[0].source_item_id, str(self.past.pk))
        # Nothing was "corrected" — it took as long as it was booked for.
        self.assertIsNone(self._reload(self.past).actual_minutes)

    def test_a_meeting_that_ran_long_is_billed_long_and_says_so(self):
        self.client.post(f"/bookings/{self.past.pk}/time",
                         data={"project_id": str(self.calls.pk),
                               "duration": "50", "billable": "1"})
        self.assertEqual(self._entries()[0].minutes, 50)
        fresh = self._reload(self.past)
        self.assertEqual(fresh.actual_minutes, 50)
        self.assertEqual(fresh.billed_minutes, 50)
        html = self.client.get("/bookings/").get_data(as_text=True)
        self.assertIn("booked 30m", html)

    def test_the_entry_lands_on_the_day_the_meeting_was(self):
        self.client.post(f"/bookings/{self.past.pk}/time",
                         data={"project_id": str(self.calls.pk),
                               "duration": "30"})
        from application.services.tz import user_zone
        with self.app.app_context():
            want = self.past.start.astimezone(user_zone(self.user)).date()
        self.assertEqual(self._entries()[0].spent_on.date(), want)

    def test_un_ticking_billable_books_it_non_billable(self):
        self.client.post(f"/bookings/{self.past.pk}/time",
                         data={"project_id": str(self.calls.pk),
                               "duration": "30"})
        self.assertFalse(self._entries()[0].billable)

    def test_without_a_project_nothing_is_booked(self):
        resp = self.client.post(f"/bookings/{self.past.pk}/time",
                                data={"duration": "30"},
                                follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._entries(), [])

    def test_without_a_duration_nothing_is_booked(self):
        self.client.post(f"/bookings/{self.past.pk}/time",
                         data={"project_id": str(self.calls.pk), "duration": ""},
                         follow_redirects=True)
        self.assertEqual(self._entries(), [])

    def test_a_stranger_s_billing_project_is_refused(self):
        from application.models.team import Team
        from application.models.timetracking import Customer, TimeTrackingProject
        from application.models.user import User
        with self.app.app_context():
            other = User(username="cu-bo", email="cubo@test.invalid",
                         password_hash="x", level="team_admin").save()
            their_team = Team(name="Else", admin=other, admins=[other]).save()
            their_cust = Customer(team=their_team, name="Not mine").save()
            theirs = TimeTrackingProject(team=their_team, customer=their_cust,
                                         name="Nope").save()
        self.client.post(f"/bookings/{self.past.pk}/time",
                         data={"project_id": str(theirs.pk), "duration": "30"})
        self.assertEqual(self._entries(), [])

    def test_somebody_elses_booking_is_a_404(self):
        from application.models.booking import Booking
        from application.models.user import User
        with self.app.app_context():
            other = User(username="cu-b2", email="cub2@test.invalid",
                         password_hash="x", level="premium").save()
            now = datetime.now(timezone.utc)
            theirs = Booking(owner=other, start=now - timedelta(days=1),
                             end=now - timedelta(days=1) + timedelta(minutes=30),
                             duration_minutes=30, name="Nicht meins").save()
        resp = self.client.post(f"/bookings/{theirs.pk}/time",
                                data={"project_id": str(self.calls.pk),
                                      "duration": "30"})
        self.assertEqual(resp.status_code, 404)

    def test_an_entry_can_be_taken_back(self):
        self.client.post(f"/bookings/{self.past.pk}/time",
                         data={"project_id": str(self.calls.pk),
                               "duration": "30"})
        from application.models.timetracking import TimeBucket
        with self.app.app_context():
            bucket = TimeBucket.objects.first()
            entry_id = bucket.entries[0].entry_id
        resp = self.client.post(
            f"/bookings/{self.past.pk}/time/{bucket.pk}/{entry_id}/cancel",
            follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._entries(), [])

    # -- filing it by hand -------------------------------------------------

    def test_a_customer_can_be_set_and_cleared_from_the_list(self):
        resp = self.client.post(f"/bookings/{self.soon.pk}/customer",
                                data={"customer_id": str(self.acme.pk)},
                                follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._reload(self.soon).customer, self.acme)
        self.client.post(f"/bookings/{self.soon.pk}/customer",
                         data={"customer_id": ""}, follow_redirects=True)
        self.assertIsNone(self._reload(self.soon).customer)

    def test_a_row_comes_back_on_its_own_for_a_fetch(self):
        """The list swaps the one row it acted on — no navigation, so Back
        doesn't walk through every appointment you touched."""
        resp = self.client.post(f"/bookings/{self.soon.pk}/customer",
                                data={"customer_id": str(self.acme.pk)},
                                headers={"X-Requested-With": "fetch"})
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn(f'data-booking-row="{self.soon.pk}"', html)
        self.assertNotIn("<html", html)

    def test_the_customer_page_lists_the_meeting(self):
        html = self.client.get(
            f"/time/customers/{self.acme.pk}?team={self.team.pk}"
        ).get_data(as_text=True)
        self.assertIn("Meetings", html)
        self.assertIn("Anna", html)


if __name__ == "__main__":
    unittest.main()
