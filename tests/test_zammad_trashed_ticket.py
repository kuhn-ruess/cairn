"""services/zammad_import + zammad_team: a ticket whose task is in the trash.

The sync used to look a ticket's task up through ``Item.objects``, which hides
the trash, so the next update of a ticket whose task had been trashed created a
second task for it. Now the trashed task is found and updated where it lies:
it stays in the trash (no automatic restore), and a new customer reply rings
the bell of whoever works the task and marks the row in the trash.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``) and is skipped without one. ``zammad_import._request``
is replaced by a fixture; nothing talks to a real Zammad.
"""
import unittest
from datetime import datetime, timedelta, timezone

from application.models.item import Item

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None

ZID = "4811"
TICKET = {"id": int(ZID), "number": "70111", "title": "Lizenzfrage, neu",
          "state": "open", "owner_id": 9}
FIRST = {"id": 1, "body": "Erste Mail", "created_at": "2026-10-01T08:00:00Z"}


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _fixture_request(articles):
    def _request(url, token, *, params=None, method="GET", body=None):
        if "/ticket_articles/by_ticket/" in url:
            return list(articles)
        if url.endswith("/tags"):
            return {"tags": []}
        raise AssertionError(f"unexpected Zammad call: {url}")
    return _request


@unittest.skipIf(connect is None, "mongoengine not installed")
class TrashedTicketTask(unittest.TestCase):

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
            from application.models.user import User
            Item.all_objects.delete()
            User.objects.delete()
        disconnect(alias="default")

    def setUp(self):
        from application.models.user import User
        from application.services.trash import trash_item
        with self.app.app_context():
            Item.all_objects.delete()
            User.objects.delete()
            self.user = User(username="ztrash", email="zt@test.invalid",
                             password_hash="x", level="premium",
                             timezone="UTC", zammad_import_time=False).save()
            self.task = Item(owner=self.user, kind="task", title="Lizenzfrage",
                             source="zammad", external_id=f"zammad:{ZID}",
                             external_ref="70111", status="open").save()
            trash_item(self.task, source="web", via="test")
            self.task.reload()
        self.trashed_at = self.task.trashed_at

    def _sync(self, articles):
        from application.services import zammad_import as zi
        original = zi._request
        zi._request = _fixture_request(articles)
        self.addCleanup(lambda: setattr(zi, "_request", original))
        stats = zi.ZammadStats()
        with self.app.app_context():
            zi._upsert_ticket(self.user, dict(TICKET),
                              "https://z.test.invalid/api/v1", "tok", None,
                              stats, dry_run=False, me_id=9)
        return stats

    def _tasks(self):
        with self.app.app_context():
            return list(Item.all_objects(external_id=f"zammad:{ZID}"))

    def _bell(self):
        self.user.reload()
        return [n for n in self.user.notifications
                if n.kind == "trashed_reply"]

    def _reply(self, **extra):
        later = datetime.now(timezone.utc) + timedelta(minutes=1)
        return {"id": 2, "body": "Kunde antwortet", "from": "Kunde <k@x.invalid>",
                "created_at": _iso(later), "created_by_id": 77, **extra}

    def test_update_while_trashed_makes_no_second_task(self):
        stats = self._sync([FIRST, self._reply()])
        tasks = self._tasks()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(stats.tickets_created, 0)
        self.assertEqual(stats.tickets_updated, 1)
        task = tasks[0]
        self.assertEqual(task.pk, self.task.pk)
        # The update reached the trashed task, and it stayed in the trash.
        self.assertEqual(task.title, "Lizenzfrage, neu")
        self.assertEqual([c.body for c in task.comments], ["Kunde antwortet"])
        self.assertIsNotNone(task.trashed_at)
        self.assertEqual(task.trashed_at, self.trashed_at)

    def test_a_customer_reply_rings_the_bell_and_marks_the_trash_row(self):
        from application.views.trash import reply_since_trashed
        self._sync([FIRST, self._reply()])
        bell = self._bell()
        self.assertEqual(len(bell), 1)
        self.assertEqual(bell[0].link, f"/trash/{self.task.pk}")
        self.assertIsNotNone(reply_since_trashed(self._tasks()[0]))
        # The same reply on the next pass is not news.
        self._sync([FIRST, self._reply()])
        self.assertEqual(len(self._bell()), 1)

    def test_an_internal_note_or_my_own_answer_stays_quiet(self):
        from application.views.trash import reply_since_trashed
        self._sync([FIRST, self._reply(internal=True),
                    self._reply(id=3, created_by_id=9)])
        self.assertEqual(self._bell(), [])
        self.assertIsNone(reply_since_trashed(self._tasks()[0]))

    def test_the_team_sync_finds_the_trashed_task(self):
        from application.services.zammad_team import _existing_task
        with self.app.app_context():
            found = _existing_task([self.user], f"zammad:{ZID}")
        self.assertIsNotNone(found)
        self.assertEqual(found.pk, self.task.pk)


if __name__ == "__main__":
    unittest.main()
