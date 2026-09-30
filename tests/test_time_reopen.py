"""Reopening billed time — a biller may, not only a manager.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one.

*"biller muessen das koennen"*: the person who bills the month had no way to
correct a booking that went out wrong — the report showed a billed entry
without a checkbox, and ``set_locked`` refused the unlock, unless a manager
came to do it. Reopening is part of settling the month, so it follows
``can_bill`` like billing does. A plain member still can't do either.
"""
import unittest
from datetime import datetime, timezone

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None


@unittest.skipIf(connect is None, "mongoengine not installed")
class ReopenTests(unittest.TestCase):

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
        from application.models.team import Team
        from application.models.timetracking import (
            Customer, TimeBucket, TimeTrackingProject)
        from application.models.user import User
        from application.services.timetracking import add_entry
        self.docs = (TimeBucket, TimeTrackingProject, Customer, Team, User)
        self._wipe()
        mk = lambda name: User(username=name, email=f"{name}@test.invalid",
                               password_hash="x", level="premium").save()
        self.boss, self.biller, self.mate = mk("boss"), mk("biller"), mk("mate")
        self.team = Team(name="Kuhn & Rueß", admin=self.boss, admins=[self.boss],
                         billers=[self.biller]).save()
        for u in (self.biller, self.mate):
            u.team = self.team
            u.save()
        customer = Customer(team=self.team, name="Acme").save()
        project = TimeTrackingProject(team=self.team, customer=customer,
                                      name="Support").save()
        entry = add_entry(project=project, user=self.mate, minutes=60,
                          spent_on=datetime(2026, 9, 1, tzinfo=timezone.utc))
        bucket = TimeBucket.objects(entries__entry_id=entry.entry_id).first()
        self.pair = [(str(bucket.pk), entry.entry_id)]
        self.TimeBucket = TimeBucket

    def tearDown(self):
        self._wipe()

    def _wipe(self):
        for doc in self.docs:
            doc.objects.delete()

    def _locked(self):
        bid, eid = self.pair[0]
        bucket = self.TimeBucket.objects(pk=bid).first()
        return next(e for e in bucket.entries if e.entry_id == eid).locked

    def _set(self, user, locked):
        from application.services.timetracking import set_locked
        return set_locked(team=self.team, user=user, items=self.pair, locked=locked)

    def test_a_biller_reopens_what_was_billed(self):
        self.assertEqual(self._set(self.boss, True), 1)
        self.assertEqual(self._set(self.biller, False), 1)
        self.assertFalse(self._locked())

    def test_a_plain_member_can_neither_bill_nor_reopen(self):
        self.assertEqual(self._set(self.mate, True), 0)
        self._set(self.boss, True)
        self.assertEqual(self._set(self.mate, False), 0)
        self.assertTrue(self._locked())

    def test_the_report_offers_a_biller_the_reopen_button(self):
        self._set(self.boss, True)
        client = self.app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(self.biller.pk)
            sess["_fresh"] = True
        html = client.get(f"/time/report?team={self.team.pk}&period=custom"
                          "&from=2026-09-01&to=2026-09-30").get_data(as_text=True)
        self.assertIn('value="unlock"', html)
        self.assertIn(f'value="{self.pair[0][0]}:{self.pair[0][1]}"', html)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
