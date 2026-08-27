"""services/mite_import — the name index has to see what the run creates.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

The importer adopts an existing customer by name rather than opening a second
file on the same client. That index was built once, before the loop, so it
could only ever recognise what existed *before* the import started: two mite
rows carrying the same name each created their own Cairn customer, and the
guard against exactly that never fired. It is how "Quartz Healthcare" came to
be in the books twice, and the hours split across both — one of which is not
something you can simply delete, which is what makes this expensive.
"""
import unittest

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None


class _FakeClient:
    """Enough of ``MiteClient`` for the two importers: canned GET responses."""

    def __init__(self, **routes):
        self.routes = routes

    def get(self, path):
        return self.routes.get(path, [])


# ``MiteClient.get`` already unwraps ``[{"customer": {…}}]`` → ``[{…}]``,
# so the importers see flat rows and so does the fake.
def _customer_row(mite_id, name, **kw):
    return {"id": mite_id, "name": name, **kw}


def _project_row(mite_id, name, customer_id, **kw):
    return {"id": mite_id, "name": name, "customer_id": customer_id, **kw}


@unittest.skipIf(connect is None, "mongoengine not installed")
class MiteImportDedupTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from application.config import TestConfig
        disconnect(alias="default")
        try:
            conn = connect(db=TestConfig.MONGO_DB,
                           host=getattr(TestConfig, "MONGO_HOST", "localhost"),
                           port=int(getattr(TestConfig, "MONGO_PORT", 27017)),
                           alias="default", tz_aware=True,
                           serverSelectionTimeoutMS=1500)
            conn.server_info()
        except (ServerSelectionTimeoutError, PyMongoError) as exc:
            raise unittest.SkipTest(f"no MongoDB for the test database: {exc}")

    @classmethod
    def tearDownClass(cls):
        disconnect(alias="default")

    def setUp(self):
        from application.models.team import Team
        from application.models.timetracking import (
            Customer, TimeBucket, TimeTrackingProject)
        from application.models.user import User
        self.Customer, self.Project = Customer, TimeTrackingProject
        for doc in (TimeBucket, TimeTrackingProject, Customer, Team, User):
            doc.objects.delete()
        self.owner = User(username="mite-test", email="mite@test.invalid",
                          password_hash="x", level="team_admin").save()
        self.team = Team(name="Test team", admin=self.owner,
                         admins=[self.owner]).save()
        self.stats = self._stats()

    def tearDown(self):
        from application.models.team import Team
        from application.models.timetracking import (
            Customer, TimeBucket, TimeTrackingProject)
        from application.models.user import User
        for doc in (TimeBucket, TimeTrackingProject, Customer, Team, User):
            doc.objects.delete()

    def _stats(self):
        from application.services.mite_import import MiteStats
        return MiteStats()

    def _import_customers(self, rows, archived=(), **kw):
        from application.services.mite_import import _import_customers
        client = _FakeClient(**{"/customers.json": list(rows),
                                "/customers/archived.json": list(archived)})
        return _import_customers(client, self.team, self.stats, False, **kw)

    # -- the bug ---------------------------------------------------------

    def test_two_mite_rows_of_the_same_name_become_one_customer(self):
        """The whole finding: one run, two rows, one client."""
        self._import_customers([_customer_row(921404, "Quartz Healthcare"),
                                _customer_row(922883, "Quartz Healthcare")])

        found = list(self.Customer.objects(team=self.team))
        self.assertEqual(len(found), 1)
        # Both mite ids reach it, so neither row re-creates its own next run.
        self.assertTrue(found[0].knows_key("mite:921404"))
        self.assertTrue(found[0].knows_key("mite:922883"))

    def test_the_name_match_ignores_case_and_padding(self):
        self._import_customers([_customer_row(1, "Quartz Healthcare"),
                                _customer_row(2, "  quartz healthcare ")])
        self.assertEqual(self.Customer.objects(team=self.team).count(), 1)

    def test_a_row_in_both_lists_is_recognised_not_re_created(self):
        """A customer mite reports as active *and* archived is one row twice —
        same key, and the second pass must find the first."""
        self._import_customers([_customer_row(921404, "Quartz Healthcare")],
                               archived=[_customer_row(921404, "Quartz Healthcare")])
        found = list(self.Customer.objects(team=self.team))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].external_id, "mite:921404")
        # The archived list is mite's statement about the row, and it came last.
        self.assertTrue(found[0].archived)

    def test_genuinely_different_names_stay_apart(self):
        """The guard must not over-reach: 'Germany' is its own client, which is
        why it is a third customer in the books and not a duplicate."""
        self._import_customers([_customer_row(921404, "Quartz Healthcare"),
                                _customer_row(894436, "Quartz Healthcare Germany")])
        self.assertEqual(
            sorted(c.name for c in self.Customer.objects(team=self.team)),
            ["Quartz Healthcare", "Quartz Healthcare Germany"])

    def test_a_customer_already_here_is_adopted_not_duplicated(self):
        """The case the index always handled — kept as a guard on the fix."""
        self.Customer(team=self.team, name="Quartz Healthcare",
                      external_id="zammad:org:52").save()
        self._import_customers([_customer_row(921404, "Quartz Healthcare")])
        found = list(self.Customer.objects(team=self.team))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].external_id, "zammad:org:52")
        self.assertTrue(found[0].knows_key("mite:921404"))

    # -- the same shape one level down -----------------------------------

    def test_two_projects_of_one_name_under_one_customer_become_one(self):
        from application.services.mite_import import _import_projects
        customers = self._import_customers([_customer_row(1, "Acme")])
        client = _FakeClient(**{
            "/projects.json": [_project_row(10, "Beratung", 1),
                               _project_row(11, "Beratung", 1)],
            "/projects/archived.json": [],
        })
        _import_projects(client, self.team, customers, self.stats, False)

        found = list(self.Project.objects(team=self.team))
        self.assertEqual(len(found), 1)
        keys = {found[0].external_id, *(found[0].external_ids or [])}
        self.assertEqual(keys, {"mite:10", "mite:11"})

    def test_the_same_project_name_under_two_customers_stays_apart(self):
        """Project identity is name *under its customer* — every client has a
        'Beratung' and they are not one project."""
        from application.services.mite_import import _import_projects
        customers = self._import_customers([_customer_row(1, "Acme"),
                                            _customer_row(2, "Beta")])
        client = _FakeClient(**{
            "/projects.json": [_project_row(10, "Beratung", 1),
                               _project_row(11, "Beratung", 2)],
            "/projects/archived.json": [],
        })
        _import_projects(client, self.team, customers, self.stats, False)
        self.assertEqual(self.Project.objects(team=self.team).count(), 2)


if __name__ == "__main__":
    unittest.main()
