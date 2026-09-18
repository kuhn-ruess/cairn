"""Connecting and refreshing a CalDAV account — what lands on the user.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one. The network is not
touched: ``discover_caldav_calendars`` is replaced, because what's under test
is the bookkeeping around it — every calendar becomes its own row, only the
default one starts visible in Today, the address is remembered so a later
refresh needs no password, and connecting the same account twice doesn't
duplicate anything.
"""
import unittest

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None

HOME = "https://cloud.example.com/remote.php/dav/calendars/alice/"
FOUND = [
    {"url": HOME + "arbeit/", "name": "Arbeit", "color": "#b16286", "default": False},
    {"url": HOME + "privat/", "name": "Privat", "color": "", "default": True},
]


@unittest.skipIf(connect is None, "mongoengine not installed")
class CalDavConnectTests(unittest.TestCase):

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

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            from application.models.user import User
            User.objects(username="caldav-test").delete()
        disconnect(alias="default")

    def setUp(self):
        from application.models.user import User
        from application.services import calendar_sync as cs
        self.User = User
        self.cs = cs
        with self.app.app_context():
            User.objects(username="caldav-test").delete()
            user = User(username="caldav-test", email="c@example.com",
                        level="premium")
            user.set_password("x")
            user.save()
            self.user = user
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.pk)
            sess["_fresh"] = True
        self._real = cs.discover_caldav_calendars
        self.answer = (HOME, FOUND)
        self.asked = []

        def fake(url, username, password):
            self.asked.append((url, username, password))
            return self.answer

        cs.discover_caldav_calendars = fake

    def tearDown(self):
        self.cs.discover_caldav_calendars = self._real

    def connect(self, url="https://cloud.example.com", **extra):
        data = {"caldav_url": url, "username": "alice", "password": "app-pw",
                "color": "#458588", "show_in_today": "y"}
        data.update(extra)
        return self.client.post("/settings/calendars/caldav/new", data=data,
                                follow_redirects=True)

    def reload(self):
        with self.app.app_context():
            return sorted(self.User.objects.get(username="caldav-test").calendars,
                          key=lambda c: c.name)

    def test_every_discovered_calendar_becomes_its_own_row(self):
        self.connect()
        cals = self.reload()
        self.assertEqual([c.name for c in cals], ["Arbeit", "Privat"])
        self.assertEqual([c.caldav_url for c in cals],
                         [HOME + "arbeit/", HOME + "privat/"])
        self.assertTrue(all(c.provider == "caldav" and c.can_write for c in cals))

    def test_only_the_default_calendar_starts_visible_in_today(self):
        self.connect()
        self.assertEqual([c.name for c in self.reload() if c.show_in_today],
                         ["Privat"])

    def test_the_servers_colour_is_kept_when_none_was_picked(self):
        self.connect()
        arbeit, privat = self.reload()
        self.assertEqual(arbeit.color, "#b16286")
        self.assertEqual(privat.color, "#458588")  # none offered, the default

    def test_a_picked_colour_beats_the_servers(self):
        self.connect(color="#d79921")
        self.assertEqual([c.color for c in self.reload()], ["#d79921", "#d79921"])

    def test_the_address_is_remembered_for_a_later_refresh(self):
        self.connect()
        self.assertTrue(all(c.caldav_home == HOME for c in self.reload()))

    def test_connecting_the_same_account_again_adds_nothing(self):
        self.connect()
        resp = self.connect()
        self.assertEqual(len(self.reload()), 2)
        self.assertIn(b"already connected", resp.data)

    def test_a_password_is_never_stored_in_the_clear(self):
        self.connect()
        self.assertTrue(all(c.caldav_password and "app-pw" not in c.caldav_password
                            for c in self.reload()))

    def test_refresh_uses_the_stored_credentials_and_adds_what_is_new(self):
        self.connect()
        self.asked.clear()
        self.answer = (HOME, FOUND + [
            {"url": HOME + "urlaub/", "name": "Urlaub", "color": "#98971a",
             "default": False}])
        resp = self.client.post("/settings/calendars/caldav/refresh",
                                follow_redirects=True)
        self.assertEqual([c.name for c in self.reload()],
                         ["Arbeit", "Privat", "Urlaub"])
        # One connection, asked once, from the home that was recorded — and
        # nobody typed the password again.
        self.assertEqual([(u, n) for u, n, _p in self.asked], [(HOME, "alice")])
        self.assertEqual(self.asked[0][2], "app-pw")
        self.assertIn(b"1 added, 2 updated", resp.data)

    def test_a_calendar_arriving_at_a_refresh_stays_out_of_today(self):
        self.connect()
        self.answer = (HOME, FOUND + [
            {"url": HOME + "urlaub/", "name": "Urlaub", "color": "",
             "default": True}])
        self.client.post("/settings/calendars/caldav/refresh")
        self.assertEqual([c.name for c in self.reload() if c.show_in_today],
                         ["Privat"])

    def test_refresh_renames_a_calendar_renamed_on_the_server(self):
        self.connect()
        self.answer = (HOME, [dict(FOUND[0], name="Arbeit 2026"), FOUND[1]])
        self.client.post("/settings/calendars/caldav/refresh")
        self.assertEqual([c.name for c in self.reload()], ["Arbeit 2026", "Privat"])

    def test_a_calendar_gone_from_the_server_keeps_its_row(self):
        self.connect()
        self.answer = (HOME, [FOUND[1]])
        self.client.post("/settings/calendars/caldav/refresh")
        self.assertEqual([c.name for c in self.reload()], ["Arbeit", "Privat"])

    def test_a_refresh_that_fails_says_so_and_changes_nothing(self):
        self.connect()

        def broken(url, username, password):
            raise self.cs.CalendarError("401 Unauthorized")

        self.cs.discover_caldav_calendars = broken
        resp = self.client.post("/settings/calendars/caldav/refresh",
                                follow_redirects=True)
        self.assertIn(b"Refresh failed", resp.data)
        self.assertEqual([c.name for c in self.reload()], ["Arbeit", "Privat"])


if __name__ == "__main__":
    unittest.main()
