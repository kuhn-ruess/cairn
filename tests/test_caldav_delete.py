"""views/caldav: completing and deleting a reminder, and the journal of both.

Completing a reminder is a PUT with ``STATUS:COMPLETED``: the task is done in
Cairn, stays in its list and is served as completed. Deleting one is a DELETE:
the task goes to the trash with its subtree, open or closed, because deleting
on the device is deleting. There is no CalDAV-only state in between.

A DELETE made against an older copy (``If-Match`` names an ETag the task no
longer has) is refused with 412 and trashes nothing. Every request lands in
the CalDAV journal with the client, the ETag it acted on and the outcome,
which is what explains a vanished reminder after the fact.

Run with ``python -m unittest discover tests``. Needs a MongoDB
(``TestConfig`` points at ``bktodo_test``) and is skipped without one.
"""
import base64
import unittest

try:
    from mongoengine import disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover (no mongoengine installed)
    disconnect = None

PASSWORD = "reminders-pw"
UA = "iOS/26.0 (23A341) remindd/1.0"


@unittest.skipIf(disconnect is None, "mongoengine not installed")
class CalDavDeleteTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from application import create_app
        disconnect(alias="default")
        try:
            cls.app = create_app("application.config.TestConfig")
            with cls.app.app_context():
                from application.models.user import User
                User.objects.first()
        except (ServerSelectionTimeoutError, PyMongoError) as exc:
            raise unittest.SkipTest(f"no MongoDB for the test database: {exc}")
        cls.ctx = cls.app.app_context()
        cls.ctx.push()

    @classmethod
    def tearDownClass(cls):
        cls._wipe()
        cls.ctx.pop()
        disconnect(alias="default")

    @staticmethod
    def _wipe():
        from application.models.caldav_log import CalDavRequest
        from application.models.item import Item
        from application.models.user import User
        Item.all_objects.delete()
        User.objects.delete()
        CalDavRequest.objects.delete()

    def setUp(self):
        from application.models.item import Item
        from application.models.user import User
        self.Item = Item
        self._wipe()
        self.user = User(username="owner", email="owner@test.invalid",
                         level="premium")
        self.user.set_password(PASSWORD)
        self.user.save()
        self.project = Item(owner=self.user, kind="project", title="Project",
                            status="active").save()
        self.task = Item(owner=self.user, kind="task", parent=self.project,
                         title="Task", status="open").save()
        self.sub = Item(owner=self.user, kind="task", parent=self.task,
                        title="Sub task", status="open").save()
        self.client = self.app.test_client()
        cred = base64.b64encode(f"owner:{PASSWORD}".encode()).decode()
        self.headers = {"Authorization": f"Basic {cred}", "User-Agent": UA}

    def fresh(self, item):
        return self.Item.all_objects(pk=item.pk).first()

    def href(self, item):
        return f"/caldav/calendars/owner/{self.project.pk}/cairn-{item.pk}.ics"

    def get(self, item):
        resp = self.client.get(self.href(item), headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        return resp.get_data(as_text=True), resp.headers["ETag"]

    def complete(self, item):
        """What Reminders sends when a reminder is ticked off: its copy, with
        the completion, against the ETag it holds."""
        ics, etag = self.get(item)
        body = ics.replace(
            "STATUS:NEEDS-ACTION",
            "STATUS:COMPLETED\r\nCOMPLETED:20261010T052400Z\r\n"
            "PERCENT-COMPLETE:100")
        resp = self.client.put(
            self.href(item), data=body.encode(),
            headers={**self.headers, "If-Match": etag,
                     "Content-Type": "text/calendar; charset=utf-8"})
        self.assertEqual(resp.status_code, 204)
        return resp

    def delete(self, item, **headers):
        return self.client.delete(self.href(item),
                                  headers={**self.headers, **headers})

    def listed(self):
        resp = self.client.open(
            f"/caldav/calendars/owner/{self.project.pk}/", method="PROPFIND",
            headers={**self.headers, "Depth": "1"})
        self.assertEqual(resp.status_code, 207)
        return resp.get_data(as_text=True)

    def journal(self, method):
        from application.models.caldav_log import CalDavRequest
        return list(CalDavRequest.objects(user=self.user, method=method))

    # -- completing --------------------------------------------------------

    def test_completing_a_reminder_completes_the_task(self):
        self.complete(self.task)
        t = self.fresh(self.task)
        self.assertEqual(t.status, "done")
        self.assertEqual(t.completed_via, "caldav")
        self.assertIsNone(t.trashed_at)
        self.assertIsNone(self.fresh(self.sub).trashed_at)

    def test_a_completed_task_stays_in_its_list_as_completed(self):
        self.complete(self.task)
        self.assertIn(f"cairn-{self.task.pk}.ics", self.listed())
        ics, _ = self.get(self.task)
        self.assertIn("STATUS:COMPLETED", ics)

    # -- deleting ----------------------------------------------------------

    def test_deleting_an_open_reminder_trashes_the_task(self):
        _, etag = self.get(self.task)
        self.assertEqual(self.delete(self.task, **{"If-Match": etag})
                         .status_code, 204)
        t = self.fresh(self.task)
        self.assertIsNotNone(t.trashed_at)
        self.assertEqual(t.trashed_source, "caldav")
        self.assertEqual(t.trashed_via, "CalDAV (Apple Reminders)")
        self.assertIsNotNone(self.fresh(self.sub).trashed_at)

    def test_deleting_a_completed_reminder_trashes_it_too(self):
        """No CalDAV special state: clearing a finished reminder on the
        device is a delete like any other."""
        self.complete(self.task)
        _, etag = self.get(self.task)
        self.assertEqual(self.delete(self.task, **{"If-Match": etag})
                         .status_code, 204)
        self.assertIsNotNone(self.fresh(self.task).trashed_at)
        self.assertNotIn(f"cairn-{self.task.pk}.ics", self.listed())

    def test_a_delete_without_if_match_is_taken_as_it_is(self):
        self.assertEqual(self.delete(self.task).status_code, 204)
        self.assertIsNotNone(self.fresh(self.task).trashed_at)

    def test_a_delete_against_an_older_copy_trashes_nothing(self):
        stale = f'"{self.task.pk}-1"'
        resp = self.delete(self.task, **{"If-Match": stale})
        self.assertEqual(resp.status_code, 412)
        self.assertEqual(resp.headers["ETag"], self.get(self.task)[1])
        self.assertIsNone(self.fresh(self.task).trashed_at)
        self.assertIsNone(self.fresh(self.sub).trashed_at)

    def test_a_reminder_moved_away_is_not_trashed_by_the_old_list(self):
        other = self.Item(owner=self.user, kind="project", title="Other",
                          status="active").save()
        t = self.fresh(self.task)
        t.parent = other
        t.save()
        self.assertEqual(self.delete(self.task).status_code, 204)
        self.assertIsNone(self.fresh(self.task).trashed_at)

    # -- the journal -------------------------------------------------------

    def test_the_journal_records_what_the_client_sent(self):
        self.complete(self.task)
        put, = self.journal("PUT")
        self.assertEqual(put.item_id, str(self.task.pk))
        self.assertEqual(put.item_status, "open")
        self.assertEqual(put.sent_status, "COMPLETED")
        self.assertEqual(put.status, 204)
        self.assertEqual(put.user_agent, UA)

        self.delete(self.task, **{"If-Match": '"old-1"'})
        _, etag = self.get(self.task)
        self.delete(self.task, **{"If-Match": etag})
        rows = sorted(self.journal("DELETE"), key=lambda r: r.created_at)
        self.assertEqual([r.outcome for r in rows], ["stale", "trashed"])
        self.assertEqual(rows[0].if_match, '"old-1"')
        self.assertEqual(rows[0].etag, etag)
        self.assertEqual(rows[1].item_status, "done")

    def test_unauthenticated_requests_are_not_journalled(self):
        self.client.delete(self.href(self.task), headers={"User-Agent": UA})
        self.assertEqual(self.journal("DELETE"), [])

    def test_the_api_serves_the_callers_journal(self):
        raw = self.user.issue_api_token("t")[1]
        self.user.save()
        self.complete(self.task)
        resp = self.client.get("/api/v1/caldav-log?method=PUT",
                               headers={"Authorization": f"Bearer {raw}"})
        self.assertEqual(resp.status_code, 200)
        items = resp.get_json()["items"]
        self.assertEqual([i["sent_status"] for i in items], ["COMPLETED"])


if __name__ == "__main__":
    unittest.main()
