"""services/timetracking.merge_customers — two records for one client, folded.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one, so the suite still
runs anywhere.

This one is worth a database, unlike the rest of ``tests/``: the bucket key is
``(team, customer, day)``, so a merge is the one operation that has to *move*
logged time between documents rather than re-label it in place. The two ways
that goes wrong both cost money — an hour left behind in a bucket whose customer
was deleted, or the same booking landing twice because both records were
importing it.
"""
import unittest

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None


def _boot():
    """Connect to the test database, or return the failure to skip on."""
    from application.config import TestConfig
    disconnect(alias="default")
    conn = connect(
        db=TestConfig.MONGO_DB, host=getattr(TestConfig, "MONGO_HOST", "localhost"),
        port=int(getattr(TestConfig, "MONGO_PORT", 27017)),
        alias="default", tz_aware=True, serverSelectionTimeoutMS=1500,
    )
    conn.server_info()
    return conn


@unittest.skipIf(connect is None, "mongoengine not installed")
class MergeCustomersTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        try:
            cls.conn = _boot()
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
        self.Bucket = TimeBucket
        for doc in (TimeBucket, TimeTrackingProject, Customer, Team, User):
            doc.objects.delete()
        self.owner = User(username="merge-test", email="merge@test.invalid",
                          level="team_admin", password_hash="x").save()
        self.team = Team(name="Test team", admin=self.owner,
                         admins=[self.owner]).save()

    def tearDown(self):
        from application.models.team import Team
        from application.models.user import User
        for doc in (self.Bucket, self.Project, self.Customer, Team, User):
            doc.objects.delete()

    # -- helpers ---------------------------------------------------------

    def _customer(self, name, **kw):
        return self.Customer(team=self.team, name=name, **kw).save()

    def _project(self, customer, name, **kw):
        return self.Project(team=self.team, customer=customer, name=name,
                            **kw).save()

    def _log(self, project, period, minutes, **kw):
        """Append one entry to ``(team, customer, period)`` the way the app does."""
        from application.models.timetracking import TimeEntry
        from application.services.timetracking import _append_entries
        entry = TimeEntry(project_id=str(project.id), project_name=project.name,
                          minutes=minutes, **kw)
        _append_entries(self.team, project.customer, period, [entry])
        return entry

    def _entries(self, customer):
        out = []
        for b in self.Bucket.objects(team=self.team, customer=customer):
            out.extend(b.entries)
        return out

    def _active_minutes(self, customer):
        return sum(e.minutes for e in self._entries(customer)
                   if e.status == "active")

    # -- the cases -------------------------------------------------------

    def test_hours_move_and_the_source_is_gone(self):
        """The whole point: no hour is left under a customer that no longer is."""
        from application.services.timetracking import merge_customers
        keep = self._customer("Quartz Healthcare", external_id="mite:41")
        dupe = self._customer("Quartz Healthcare", external_id="zammad:org:7")
        kept_p = self._project(keep, "Default Projekt")
        dupe_p = self._project(dupe, "Beratung")
        self._log(kept_p, "2026-08-05", 60)
        self._log(dupe_p, "2026-08-05", 90)   # same day → same bucket after merge
        self._log(dupe_p, "2026-08-21", 30)   # a day the target has nothing on

        res = merge_customers(dupe, keep)

        self.assertEqual(self._active_minutes(keep), 180)
        self.assertEqual(res["entries_moved"], 2)
        self.assertEqual(res["projects_moved"], 1)
        self.assertIsNone(self.Customer.objects(pk=dupe.pk).first())
        self.assertEqual(self.Bucket.objects(team=self.team, customer=dupe).count(), 0)
        # The moved project is re-parented, not duplicated.
        self.assertEqual(self.Project.objects(team=self.team, customer=keep).count(), 2)

    def test_same_day_lands_in_one_bucket(self):
        """The bucket key is unique on (team, customer, day) — the merged day
        has to be *one* document, not a second one that the index would reject."""
        from application.services.timetracking import merge_customers
        keep, dupe = self._customer("Acme"), self._customer("ACME")
        self._log(self._project(keep, "A"), "2026-08-05", 60)
        self._log(self._project(dupe, "B"), "2026-08-05", 45)

        merge_customers(dupe, keep)

        buckets = list(self.Bucket.objects(team=self.team, customer=keep,
                                           period="2026-08-05"))
        self.assertEqual(len(buckets), 1)
        self.assertEqual(len(buckets[0].entries), 2)
        self.assertEqual(buckets[0].total_minutes, 105)
        self.assertEqual(buckets[0].entry_count, 2)

    def test_same_name_projects_fold_and_entries_follow(self):
        from application.services.timetracking import merge_customers
        keep, dupe = self._customer("Acme"), self._customer("Acme")
        survivor = self._project(keep, "Beratung")
        folded = self._project(dupe, "beratung")   # same project, other spelling
        self._log(folded, "2026-08-05", 60)

        res = merge_customers(dupe, keep)

        self.assertEqual(res["projects_folded"], 1)
        self.assertIsNone(self.Project.objects(pk=folded.pk).first())
        moved = self._entries(keep)
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0].project_id, str(survivor.id))
        self.assertEqual(moved[0].project_name, "Beratung")

    def test_a_booking_both_records_carry_arrives_cancelled(self):
        """The duplicate-time guard. Same external key on both sides → the hour
        moves (nothing is deleted here) but does not count."""
        from application.services.timetracking import merge_customers
        keep, dupe = self._customer("Acme"), self._customer("Acme GmbH")
        self._log(self._project(keep, "A"), "2026-08-05", 60,
                  external_id="zammad:1001")
        self._log(self._project(dupe, "B"), "2026-08-05", 60,
                  external_id="zammad:1001")

        res = merge_customers(dupe, keep)

        self.assertEqual(res["duplicates_cancelled"], 1)
        self.assertEqual(self._active_minutes(keep), 60)      # not 120
        self.assertEqual(len(self._entries(keep)), 2)          # still both there
        cancelled = [e for e in self._entries(keep) if e.status == "cancelled"]
        self.assertEqual(len(cancelled), 1)

    def test_a_day_key_matching_a_covered_booking_counts_as_the_same(self):
        """Zammad sums a day's bookings into one entry whose ``external_ids``
        list the parts. A record still holding a part is that same time."""
        from application.services.timetracking import merge_customers
        keep, dupe = self._customer("Acme"), self._customer("Acme GmbH")
        self._log(self._project(keep, "A"), "2026-08-05", 60,
                  external_id="zammad:day:9:2026-08-05",
                  external_ids=["zammad:1001", "zammad:1002"])
        self._log(self._project(dupe, "B"), "2026-08-05", 20,
                  external_id="zammad:1002")

        res = merge_customers(dupe, keep)

        self.assertEqual(res["duplicates_cancelled"], 1)
        self.assertEqual(self._active_minutes(keep), 60)

    def test_a_billed_duplicate_is_left_alone_and_reported(self):
        """A locked entry is frozen — cancelling it behind someone's back would
        rewrite a settled invoice."""
        from application.services.timetracking import merge_customers
        keep, dupe = self._customer("Acme"), self._customer("Acme GmbH")
        self._log(self._project(keep, "A"), "2026-08-05", 60,
                  external_id="zammad:1001")
        self._log(self._project(dupe, "B"), "2026-08-05", 60,
                  external_id="zammad:1001", locked=True)

        res = merge_customers(dupe, keep)

        self.assertEqual(res["duplicates_kept"], 1)
        self.assertEqual(res["duplicates_cancelled"], 0)
        self.assertEqual(self._active_minutes(keep), 120)   # flagged, not hidden

    def test_billed_entries_are_not_stranded(self):
        """``move_entries`` refuses to move locked time; a merge cannot, or the
        hours would outlive the customer document they hang under."""
        from application.services.timetracking import merge_customers
        keep, dupe = self._customer("Acme"), self._customer("Acme GmbH")
        self._log(self._project(keep, "A"), "2026-08-05", 60)
        self._log(self._project(dupe, "B"), "2026-08-06", 45, locked=True)

        merge_customers(dupe, keep)

        moved = [e for e in self._entries(keep) if e.locked]
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0].minutes, 45)

    def test_the_rate_an_entry_was_captured_at_survives(self):
        """A project move re-freezes the rate onto the target's; a merge must
        not — the project travelled with the entry, so its terms still apply."""
        from application.services.timetracking import merge_customers
        keep, dupe = self._customer("Acme"), self._customer("Acme GmbH")
        self._project(keep, "A", hourly_rate_cents=20000)
        moved_p = self._project(dupe, "B", hourly_rate_cents=9000)
        self._log(moved_p, "2026-08-05", 60, hourly_rate_cents=9000,
                  amount_cents=9000)

        merge_customers(dupe, keep)

        entry = [e for e in self._entries(keep)][0]
        self.assertEqual(entry.hourly_rate_cents, 9000)
        self.assertEqual(entry.amount_cents, 9000)

    def test_import_keys_move_so_the_next_sync_stays_put(self):
        """Without this the importer re-creates the customer that was merged
        away on its very next run — which is how the duplicates arose."""
        from application.services.timetracking import merge_customers
        keep = self._customer("Acme", external_id="mite:41")
        dupe = self._customer("Acme GmbH", external_id="zammad:org:7",
                              external_ids=["mite:99"])

        merge_customers(dupe, keep)

        keep.reload()
        self.assertEqual(keep.external_id, "mite:41")
        self.assertTrue(keep.knows_key("zammad:org:7"))
        self.assertTrue(keep.knows_key("mite:99"))

    def test_only_one_project_stays_the_intake(self):
        from application.services.timetracking import intake_project, merge_customers
        keep, dupe = self._customer("Acme"), self._customer("Acme GmbH")
        mine = self._project(keep, "Beratung", intake=True)
        theirs = self._project(dupe, "Acme GmbH", intake=True)

        merge_customers(dupe, keep)

        theirs.reload()
        self.assertFalse(theirs.intake)
        self.assertEqual(str(intake_project(self.team, keep).id), str(mine.id))

    def test_billing_details_fill_gaps_without_overwriting(self):
        from application.services.timetracking import merge_customers
        keep = self._customer("Acme", vat_id="DE111")
        dupe = self._customer("Acme GmbH", vat_id="DE999", address="Hauptstr. 1")

        merge_customers(dupe, keep)

        keep.reload()
        self.assertEqual(keep.vat_id, "DE111")          # the target's own wins
        self.assertEqual(keep.address, "Hauptstr. 1")   # the gap is filled

    def test_merging_into_itself_is_refused(self):
        from application.services.timetracking import merge_customers
        c = self._customer("Acme")
        with self.assertRaises(ValueError):
            merge_customers(c, c)


if __name__ == "__main__":
    unittest.main()
