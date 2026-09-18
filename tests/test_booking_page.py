"""The booking page end to end: settings form, public page, confirmation.

Run with ``python -m unittest discover tests``. Needs a MongoDB (``TestConfig``
points at ``bktodo_test``); skipped when there isn't one. No network: the
calendar layer is replaced, so what's under test is the bookkeeping around it
— who may see the page, what the form refuses to save, and the three ways two
visitors can end up fighting over one slot.
"""
import io
import unittest
from datetime import datetime, timedelta, timezone

try:
    from mongoengine import connect, disconnect
    from pymongo.errors import PyMongoError, ServerSelectionTimeoutError
except ImportError:  # pragma: no cover - no mongoengine installed
    connect = None

BERLIN = "Europe/Berlin"


def png(width: int, height: int) -> bytes:
    """A real PNG — the upload path sniffs the bytes, so a fake won't do."""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (80, 120, 160)).save(buf, format="PNG")
    return buf.getvalue()


def form_data(**over):
    data = {
        "enabled": "y", "slug": "alice", "title": "Talk to me", "intro": "Hi.",
        "durations": "30, 60", "days_ahead": "14",
        "write_calendar_id": "", "busy_calendar_ids": [],
        "min_notice_hours": "0", "buffer_minutes": "0", "max_per_day": "0",
        "listed": "y",
    }
    for day in range(5):
        data[f"day-{day}"] = "y"
        data[f"start-{day}"] = "09:00"
        data[f"end-{day}"] = "17:00"
    data.update(over)
    return data


@unittest.skipIf(connect is None, "mongoengine not installed")
class BookingPageTests(unittest.TestCase):

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
            from application.models.booking import Booking, BookingPhoto
            from application.models.user import User
            for name in ("book-test", "book-other"):
                for u in User.objects(username=name):
                    Booking.objects(owner=u).delete()
                    BookingPhoto.objects(owner=u).delete()
                    u.delete()
        disconnect(alias="default")

    def setUp(self):
        from application.models.booking import Booking, BookingPhoto
        from application.models.calendar import ExternalCalendar
        from application.models.user import User
        from application.services import booking as svc
        self.User = User
        self.Booking = Booking
        self.svc = svc
        with self.app.app_context():
            for name in ("book-test", "book-other"):
                for u in User.objects(username=name):
                    Booking.objects(owner=u).delete()
                    BookingPhoto.objects(owner=u).delete()
                    u.delete()
            user = User(username="book-test", email="b@example.com",
                        level="premium", timezone=BERLIN)
            user.set_password("x")
            self.cal = ExternalCalendar(provider="caldav", name="Work",
                                        can_write=True,
                                        caldav_url="https://dav.example/w/")
            user.calendars = [self.cal]
            user.save()
            self.user = user
            self.cal_id = str(self.cal.pk_ref)
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["_user_id"] = str(self.user.pk)
            sess["_fresh"] = True

        # No calendar is ever reached: busy times and event writes are stubbed.
        self._busy = svc.busy_intervals
        self._write = svc._write_event
        self.busy = []
        self.written = []
        svc.busy_intervals = lambda user, page, since, until: list(self.busy)

        def fake_write(user, page, booking):
            self.written.append(booking)
            booking.event_id = "evt-1"
            booking.calendar_id = page.write_calendar_id
            booking.save()

        svc._write_event = fake_write

    def tearDown(self):
        self.svc.busy_intervals = self._busy
        self.svc._write_event = self._write

    # -- helpers ----------------------------------------------------------
    def save_page(self, **over):
        over.setdefault("write_calendar_id", self.cal_id)
        return self.client.post("/settings/booking", data=form_data(**over),
                                content_type="multipart/form-data",
                                follow_redirects=True)

    def reload(self):
        with self.app.app_context():
            return self.User.objects.get(username="book-test")

    def first_slot(self, duration=30):
        resp = self.client.get(f"/book/alice/slots?duration={duration}")
        return resp.get_json()["slots"][0]

    def book(self, start, duration=30, **over):
        data = {"start": start, "duration": str(duration), "name": "Bob",
                "email": "bob@example.com", "note": "Hallo", "tz": "America/New_York"}
        data.update(over)
        return self.client.post("/book/alice/book", data=data)

    # -- the settings form ------------------------------------------------
    def test_saving_the_form_configures_the_page(self):
        self.save_page()
        page = self.reload().booking
        self.assertTrue(page.enabled)
        self.assertEqual(page.slug, "alice")
        self.assertEqual(page.ordered_durations(), [30, 60])
        self.assertEqual(len(page.hours), 5)
        self.assertEqual(page.write_calendar_id, self.cal_id)

    def test_a_live_page_without_a_write_calendar_is_refused(self):
        resp = self.save_page(write_calendar_id="")
        self.assertIn(b"calendar for new appointments", resp.data)
        self.assertFalse(self.reload().booking.enabled)

    def test_an_end_before_its_start_is_refused_and_saves_nothing(self):
        self.save_page()
        resp = self.save_page(**{"end-1": "08:00", "title": "Changed"})
        self.assertIn(b"end has to be after the start", resp.data)
        self.assertEqual(self.reload().booking.title, "Talk to me")

    def test_a_slug_someone_else_uses_is_refused(self):
        self.save_page()
        with self.app.app_context():
            other = self.User(username="book-other", email="o@example.com",
                              level="premium")
            other.set_password("x")
            other.booking.slug = "taken"
            other.save()
        resp = self.save_page(slug="taken")
        self.assertIn(b"already uses that link name", resp.data)
        self.assertEqual(self.reload().booking.slug, "alice")

    def test_a_slug_with_spaces_is_refused(self):
        resp = self.save_page(slug="my page")
        self.assertIn(b"lowercase letters, digits and dashes", resp.data)

    # -- the public page --------------------------------------------------
    def test_the_page_404s_until_it_is_switched_on(self):
        self.save_page(enabled="")
        self.assertEqual(self.client.get("/book/alice").status_code, 404)
        self.save_page()
        self.assertEqual(self.client.get("/book/alice").status_code, 200)

    def test_an_unknown_slug_is_a_404(self):
        self.assertEqual(self.client.get("/book/nobody").status_code, 404)

    def test_slots_are_utc_instants_and_respect_busy_times(self):
        self.save_page()
        data = self.client.get("/book/alice/slots?duration=60").get_json()
        self.assertTrue(data["slots"])
        self.assertTrue(all(s.endswith("+00:00") for s in data["slots"]))
        taken = datetime.fromisoformat(data["slots"][0])
        self.busy = [(taken, taken + timedelta(minutes=60))]
        again = self.client.get("/book/alice/slots?duration=60").get_json()
        self.assertNotIn(data["slots"][0], again["slots"])

    def test_a_length_that_is_not_offered_is_rejected(self):
        self.save_page()
        self.assertEqual(self.client.get("/book/alice/slots?duration=45").status_code,
                         400)

    def test_an_unreadable_calendar_stops_the_page_rather_than_guessing(self):
        self.save_page()

        def broken(user, page, since, until):
            raise self.svc.BookingError("The calendar “Work” could not be read")

        self.svc.busy_intervals = broken
        resp = self.client.get("/book/alice/slots?duration=30")
        self.assertEqual(resp.status_code, 503)
        self.assertIn("could not be read", resp.get_json()["error"])

    # -- booking ----------------------------------------------------------
    def test_a_booking_is_stored_written_and_announced(self):
        self.save_page()
        start = self.first_slot()
        resp = self.book(start)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"You're booked", resp.data)
        with self.app.app_context():
            booking = self.Booking.objects.get(owner=self.user)
            self.assertEqual(booking.name, "Bob")
            self.assertEqual(booking.duration_minutes, 30)
            self.assertEqual(booking.event_id, "evt-1")
            self.assertEqual(booking.guest_timezone, "America/New_York")
        self.assertEqual(len(self.written), 1)
        bell = [n for n in self.reload().notifications if n.kind == "booking"]
        self.assertEqual(len(bell), 1)
        self.assertIn("Bob", bell[0].text)

    def test_a_booked_slot_disappears_from_the_next_visitor(self):
        self.save_page()
        start = self.first_slot()
        self.book(start)
        self.assertNotIn(start,
                         self.client.get("/book/alice/slots?duration=30")
                         .get_json()["slots"])

    def test_the_same_slot_cannot_be_booked_twice(self):
        self.save_page()
        start = self.first_slot()
        self.book(start)
        resp = self.book(start)
        self.assertEqual(resp.status_code, 400)
        self.assertIn(b"just taken", resp.data)
        with self.app.app_context():
            self.assertEqual(self.Booking.objects(owner=self.user).count(), 1)

    def test_an_overlapping_slot_is_refused_even_at_a_different_start(self):
        """10:00–11:00 booked, then 10:30 for 30 minutes: different start, so
        the unique index says nothing — the availability check has to."""
        self.save_page()
        hour = self.first_slot(60)
        self.book(hour, 60)
        half = (datetime.fromisoformat(hour) + timedelta(minutes=30)).isoformat()
        resp = self.book(half, 30)
        self.assertEqual(resp.status_code, 400)
        with self.app.app_context():
            self.assertEqual(self.Booking.objects(owner=self.user).count(), 1)

    def test_a_time_outside_the_offered_hours_is_refused(self):
        self.save_page()
        start = datetime.fromisoformat(self.first_slot())
        midnight = start.replace(hour=2, minute=0)
        resp = self.book(midnight.isoformat())
        self.assertEqual(resp.status_code, 400)
        with self.app.app_context():
            self.assertEqual(self.Booking.objects(owner=self.user).count(), 0)

    def test_the_notice_window_is_enforced_on_the_post_too(self):
        """A stale page still shows this morning's slots; the POST must not
        take one."""
        self.save_page(min_notice_hours="0")
        start = self.first_slot()
        self.save_page(min_notice_hours="720")
        resp = self.book(start)
        self.assertEqual(resp.status_code, 400)

    def test_a_booking_without_an_email_is_refused(self):
        self.save_page()
        resp = self.book(self.first_slot(), email="not-an-address")
        self.assertEqual(resp.status_code, 400)
        self.assertIn(b"valid email", resp.data)

    def test_a_failed_event_write_still_keeps_the_slot(self):
        """The visitor was told yes; the owner is the one who learns it didn't
        reach the calendar."""
        self.save_page()
        self.svc._write_event = self._write   # the real one, with no calendar
        with self.app.app_context():
            user = self.User.objects.get(username="book-test")
            user.booking.write_calendar_id = ""
            user.save()
        start = self.first_slot()
        resp = self.book(start)
        self.assertEqual(resp.status_code, 200)
        with self.app.app_context():
            booking = self.Booking.objects.get(owner=self.user)
            self.assertIn("No calendar", booking.event_error)
        self.assertNotIn(start,
                         self.client.get("/book/alice/slots?duration=30")
                         .get_json()["slots"])

    # -- the directory and the portrait -----------------------------------
    def test_the_directory_lists_a_live_page(self):
        self.save_page()
        body = self.client.get("/book/").data
        self.assertIn(b"book-test", body)
        self.assertIn(b"/book/alice", body)

    def test_a_page_whose_document_predates_the_switch_is_still_listed(self):
        """``listed`` defaults to True, and a field at its default is never
        written — so the stored document has no such key at all. Querying for
        it in Mongo found nobody; the default only exists after a load."""
        self.save_page()
        with self.app.app_context():
            self.User._get_collection().update_one(
                {"username": "book-test"}, {"$unset": {"booking.listed": ""}})
        self.assertIn(b"/book/alice", self.client.get("/book/").data)

    def test_a_page_that_asks_not_to_be_listed_is_not(self):
        self.save_page(listed="")
        self.assertNotIn(b"/book/alice", self.client.get("/book/").data)
        # …and is still reachable for anyone holding the link.
        self.assertEqual(self.client.get("/book/alice").status_code, 200)

    def test_a_page_that_is_switched_off_is_not_listed(self):
        self.save_page(enabled="")
        self.assertNotIn(b"/book/alice", self.client.get("/book/").data)

    def test_a_photo_is_stored_square_and_served_as_webp(self):
        self.save_page(photo=(io.BytesIO(png(600, 300)), "me.png"))
        resp = self.client.get("/book/alice/photo")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "image/webp")
        from PIL import Image
        img = Image.open(io.BytesIO(resp.data))
        self.assertEqual(img.size, (320, 320))

    def test_no_photo_is_a_404_not_a_broken_page(self):
        self.save_page()
        self.assertEqual(self.client.get("/book/alice/photo").status_code, 404)
        self.assertEqual(self.client.get("/book/alice").status_code, 200)

    def test_a_file_that_is_not_an_image_is_refused(self):
        resp = self.save_page(photo=(io.BytesIO(b"<svg onload=alert(1)>"), "x.svg"))
        # The apostrophe in the message is HTML-escaped by the time it renders.
        self.assertIn(b"a PNG, JPEG, GIF or WebP image", resp.data)
        self.assertEqual(self.client.get("/book/alice/photo").status_code, 404)

    def test_a_photo_can_be_removed_again(self):
        self.save_page(photo=(io.BytesIO(png(64, 64)), "me.png"))
        self.assertEqual(self.client.get("/book/alice/photo").status_code, 200)
        self.save_page(remove_photo="y")
        self.assertEqual(self.client.get("/book/alice/photo").status_code, 404)

    def test_the_day_cap_closes_the_day_for_everyone(self):
        self.save_page(max_per_day="1")
        start = self.first_slot()
        self.book(start)
        day = datetime.fromisoformat(start).date()
        left = [s for s in self.client.get("/book/alice/slots?duration=30")
                .get_json()["slots"]
                if datetime.fromisoformat(s).date() == day]
        self.assertEqual(left, [])


if __name__ == "__main__":
    unittest.main()
